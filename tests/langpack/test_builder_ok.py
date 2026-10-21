"""builder_ok.py (review r2 #4, r3): whether a pack build may go on to
pack_gate.py. The builder's exit code counts; only zimru url_internal
findings are excused, and only from a stats file this run wrote (newer than
the run marker, no older than the staged pack, its zim_bytes the pack's)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.langpack.conftest import TOOLS
from tools.langpack.builder_ok import decide

LINKS = ["zimcheck: url_internal: The following links:\n- ./Mojave_Air_&amp;_Space_Port ..."]
URL_ONLY = {"ok": False, "problems": LINKS, "zimcheck_findings": {"url_internal": 45}, "zimcheck": "FAIL (45 findings)"}


class Run:
    """A staged pack and a run marker, then stats files written after both."""

    def __init__(self, d: Path):
        self.d = d
        self.pack = d / "staged.zim"
        self.pack.write_bytes(b"x" * 1234)
        time.sleep(0.01)
        self.mark = d / ".run"
        self.mark.touch()
        time.sleep(0.01)
        self.n = 0

    def stats(self, check: dict, zim_bytes: int = 1234, stale: bool = False) -> Path:
        self.n += 1
        p = self.d / f"s{self.n}.json"
        p.write_text(json.dumps({"zim_bytes": zim_bytes, "check": check}))
        if stale:                                  # left by an earlier build
            t = self.mark.stat().st_mtime_ns - 10**9
            os.utime(p, ns=(t, t))
        return p


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


CASES = [
    ("rc=0, check ok", 0, {"ok": True, "problems": []}, True),
    ("rc=1, url_internal only", 1, URL_ONLY, True),
    ("rc=1, geo-index mismatch + links", 1,
     {"ok": False, "problems": LINKS + ["the geo-index does not list exactly the articles"],
      "zimcheck_findings": {"url_internal": 3}}, False),
    ("rc=1, a finding of another kind", 1,
     {"ok": False, "problems": LINKS * 3, "zimcheck_findings": {"url_internal": 40, "metadata": 1}}, False),
    ("rc=1, zimcheck gave no JSON", 1,
     {"ok": False, "problems": ["zimcheck gave no JSON (exit 1)"], "zimcheck": "error"}, False),
    ("rc=1, pack.json article count wrong", 1,
     {"ok": False, "problems": ["pack.json says 5 de articles; ..."], "zimcheck_findings": {}}, False),
    ("rc=2 (crash) with an ok-looking stats file", 2, {"ok": True, "problems": []}, False),
    ("rc=0 but check not ok", 0, {"ok": False, "problems": ["has a Xapian index"]}, False),
]


@pytest.mark.parametrize("name,rc,check,want", CASES, ids=[c[0] for c in CASES])
def test_decide(name: str, rc: int, check: dict, want: bool, run: Run) -> None:
    ok, why = decide(rc, str(run.stats(check)), str(run.pack), str(run.mark))
    assert ok is want, why


def test_stale_stats_never_excuse_a_build(run: Run) -> None:
    """The r3 hole: a crash plus an earlier run's url_internal-only stats."""
    ok, why = decide(1, str(run.stats(URL_ONLY, stale=True)), str(run.pack), str(run.mark))
    assert not ok and "predates this run" in why
    ok, why = decide(0, str(run.stats({"ok": True, "problems": []}, stale=True)), str(run.pack), str(run.mark))
    assert not ok


def test_stats_must_belong_to_the_staged_pack(run: Run) -> None:
    early = run.stats(URL_ONLY)
    time.sleep(0.01)
    later_pack = run.d / "staged2.zim"
    later_pack.write_bytes(b"x" * 1234)
    assert decide(1, str(early), str(later_pack), str(run.mark)) == (False, "rc=1: stats file is older than the staged pack")
    assert not decide(1, str(run.stats(URL_ONLY)), str(run.d / "nopack.zim"), str(run.mark))[0]
    assert not decide(1, str(run.stats(URL_ONLY, zim_bytes=999)), str(run.pack), str(run.mark))[0]
    assert not decide(1, str(run.stats(URL_ONLY)), str(run.pack), str(run.d / "nomark"))[0]
    assert not decide(1, str(run.d / "missing.json"), str(run.pack), str(run.mark))[0]


def test_cli(run: Run) -> None:
    stale = run.stats(URL_ONLY, stale=True)
    cli = [sys.executable, str(TOOLS / "builder_ok.py")]
    r = subprocess.run(cli + ["1", str(stale)], capture_output=True, text=True)
    assert r.returncode != 0 and "PROCEED" not in r.stdout               # the old 2-argument call
    r = subprocess.run(cli + ["1", str(stale), str(run.pack), str(run.mark)], capture_output=True, text=True)
    assert r.returncode == 1 and r.stdout.startswith("REJECT")
    r = subprocess.run(cli + ["1", str(run.stats(URL_ONLY)), str(run.pack), str(run.mark)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("PROCEED excused")


def test_real_builder_stats_shape() -> None:
    """The keys builder_ok reads are the ones streetzim/wiki_pack.py writes."""
    import re
    src = (TOOLS.parents[1] / "streetzim" / "wiki_pack.py").read_text()
    assert re.search(r"\bzim_bytes=", src)
    for key in ("check", "problems", "zimcheck_findings", "zimcheck", "ok"):
        assert re.search(rf"[\"']{key}[\"']", src), key
