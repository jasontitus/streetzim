"""build_packs.sh with a stand-in builder (BUILDER=...) and zimcheck.

Review r3: a stats file left by an earlier build must never excuse this
build, so each mode starts with a STALE url_internal-only stats file in OUT.
Review r4: one build per region+language at a time."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tests.langpack.conftest import TOOLS  # noqa: E402

if not (shutil.which("bash") and shutil.which("flock")):
    pytest.skip("build_packs.sh needs bash and flock (util-linux)", allow_module_level=True)

DATE = "2026-10-13"
F = f"osm_testland_wiki-th_{DATE}"
LINKS = {"ok": False, "problems": ["zimcheck: url_internal: The following links:\n- ./X_&amp;_Y"],
         "zimcheck_findings": {"url_internal": 1}, "zimcheck": "FAIL (1 findings)"}

FAKE = r'''
import argparse, json, os, shutil, sys, time
ap = argparse.ArgumentParser(); ap.add_argument("-o"); ap.add_argument("--stats")
a, _ = ap.parse_known_args()
mode, src = os.environ["FAKE_MODE"], os.environ["FAKE_PACK"]
open(os.path.join(os.path.dirname(a.stats), "zimcheck_bin.txt"), "w").write(os.environ.get("ZIMCHECK_BIN", ""))
def pack(): shutil.copyfile(src, a.o)
def stats():
    st = {"zim_bytes": os.path.getsize(a.o), "check": json.loads(os.environ["FAKE_CHECK"])}
    open(a.stats, "w").write(json.dumps(st))
if mode == "crash": sys.exit(1)
if mode == "crash_after_pack": pack(); sys.exit(1)
if mode == "stats_first": open(a.o, "wb").close(); time.sleep(0.05); stats(); time.sleep(0.05); pack(); sys.exit(1)
if mode == "good": pack(); time.sleep(0.01); stats(); sys.exit(1)
if mode == "slow_good": pack(); time.sleep(float(os.environ.get("FAKE_SLEEP", "5"))); stats(); sys.exit(1)
sys.exit(3)
'''


@pytest.fixture(scope="module")
def bp(lp: SimpleNamespace, tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    d = tmp_path_factory.mktemp("buildpacks")
    fake = d / "fake_builder.py"
    fake.write_text(FAKE)
    zc = lpzims.fake_zimcheck(d / "zc", "clean", None)
    env = {**os.environ, "BUILDER": str(fake), "FAKE_PACK": str(lp.packs["th"]), "FAKE_CHECK": json.dumps(LINKS),
           "PACK_GATE_ZIMCHECK": zc, "WIKI_SRC": str(d / "wiki-src"), "LANGPACK_WORK": str(d / "work"),
           "PACK_DATE": DATE, "PYTHON": sys.executable, "PYTHONDONTWRITEBYTECODE": "1"}
    return SimpleNamespace(dir=d, env=env, base=lp.base)


def _build(bp: SimpleNamespace, out: Path, mode: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(TOOLS / "build_packs.sh"), "testland", str(bp.base), str(out), "th"],
                          capture_output=True, text=True, timeout=600, env={**bp.env, "FAKE_MODE": mode, **extra})


def _stale(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{F}.stats.json"
    p.write_text(json.dumps({"zim_bytes": 1, "check": LINKS}))
    os.utime(p, (1_700_000_000, 1_700_000_000))
    return p


@pytest.mark.parametrize("mode,lands", [("crash", False), ("crash_after_pack", False), ("stats_first", False),
                                        ("good", True)])
def test_only_this_runs_stats_count(mode: str, lands: bool, bp: SimpleNamespace, tmp_path: Path) -> None:
    out = tmp_path / "out"
    stale = _stale(out)
    r = _build(bp, out, mode)
    bok = (out / "builder-ok-th.log").read_text().strip()
    landed = (out / f"{F}.zim").exists()
    assert landed is lands, r.stdout + r.stderr
    assert bok.startswith("PROCEED" if lands else "REJECT"), bok
    assert ("gate=PASS" in r.stdout) is lands
    assert not (stale.exists() and stale.stat().st_mtime == 1_700_000_000)      # the stale file is gone
    # the builder's own check uses the same (official) zimcheck as the gate
    assert (out / "zimcheck_bin.txt").read_text() == bp.env["PACK_GATE_ZIMCHECK"]
    if lands:
        g = json.loads((out / f"{F}.zim.gate.json").read_text())
        assert g["ok"] and g["pack_md5"] == lpzims.md5_of(out / f"{F}.zim")
    else:
        assert not (out / f"{F}.zim.gate.json").exists()


def test_one_build_per_region_and_language(bp: SimpleNamespace, tmp_path: Path) -> None:
    out, out2 = tmp_path / "out", tmp_path / "out2"
    p1 = subprocess.Popen([str(TOOLS / "build_packs.sh"), "testland", str(bp.base), str(out), "th"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          env={**bp.env, "FAKE_MODE": "slow_good", "FAKE_SLEEP": "6"})
    staged, mark = out / ".staging" / f"{F}.zim", out / ".staging" / ".run-th"
    t0 = time.time()
    while not staged.exists() and p1.poll() is None and time.time() - t0 < 120:
        time.sleep(0.1)
    assert staged.exists()
    m0, s0 = mark.stat().st_mtime_ns, staged.stat().st_mtime_ns
    for o in (out, out2):                       # the same OUT, then another one: refused at once
        t = time.time()
        r = _build(bp, o, "good")
        assert r.returncode == 2 and "REFUSED" in r.stdout, r.stdout + r.stderr
        assert time.time() - t < 30
        assert mark.stat().st_mtime_ns == m0 and staged.stat().st_mtime_ns == s0
        assert not (out / f"{F}.stats.json").exists()
    o1, _ = p1.communicate(timeout=600)
    assert p1.returncode == 0 and "gate=PASS" in o1 and (out / f"{F}.zim").exists(), o1
    r = _build(bp, out2, "good")                # released: a new run gets the lock
    assert r.returncode == 0 and "gate=PASS" in r.stdout and "REFUSED" not in r.stdout, r.stdout


def test_wiki_src_is_required(bp: SimpleNamespace, tmp_path: Path) -> None:
    env = {k: v for k, v in bp.env.items() if k != "WIKI_SRC"}
    r = subprocess.run([str(TOOLS / "build_packs.sh"), "testland", str(bp.base), str(tmp_path / "o"), "th"],
                       capture_output=True, text=True, timeout=60, env={**env, "FAKE_MODE": "good"})
    assert r.returncode != 0 and "WIKI_SRC" in r.stderr


def test_refuses_a_zimcheck_that_is_not_the_official_one(bp: SimpleNamespace, tmp_path: Path) -> None:
    zimru = lpzims.fake_zimcheck(tmp_path / "zc", "zimru", "", rc=0, version="zimcheck (zimru) 0.1.0")
    out, work = tmp_path / "out", tmp_path / "work"
    r = subprocess.run([str(TOOLS / "build_packs.sh"), "testland", str(bp.base), str(out), "th"], capture_output=True,
                       text=True, timeout=60, env={**bp.env, "FAKE_MODE": "good", "PACK_GATE_ZIMCHECK": zimru,
                                                   "LANGPACK_WORK": str(work)})
    assert r.returncode == 1 and "not the official zimcheck" in r.stderr, r.stderr
    assert not out.exists() and not work.exists()          # refused before creating anything
