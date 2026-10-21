"""pack_gate.py (review S1, r2 #3): a pack is usable only when the official
zimcheck -A finished and reported nothing but one back-to-map link per
article. A stand-in zimcheck (tests/langpack/lpzims.py) prints what the
official one says about a clean pack, or a doctored copy; when the official
zimcheck is installed, it is compared with the stand-in too."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tests.langpack.conftest import TOOLS, tool  # noqa: E402


def _doctor(clean: str) -> dict[str, tuple[str, int]]:
    lines = clean.splitlines()
    hdr = [i for i, ln in enumerate(lines) if ln.startswith("[ERROR] Internal URL: Dangling link(s) in article ")]
    apo = next(i for i in hdr if lines[i].count("'") > 2)          # a title with an apostrophe
    plain = next(i for i in hdr if lines[i].count("'") == 2)
    j = lambda xs: "\n".join(xs) + "\n"  # noqa: E731
    out: dict[str, tuple[str, int]] = {
        "control": (clean, 1),
        "no_overall": (j([ln for ln in lines if "Overall Test Status:" not in ln]), 1),
        "empty_rc0": ("", 0),
        "drop_block": (j(lines[:apo] + lines[apo + 2:]), 1),
    }
    x = list(lines)
    x.insert(plain + 2, "  - '../Foo' (resolves to 'wiki-article/Foo')")
    out["extra_link"] = (j(x), 1)
    x = list(lines)
    x[plain + 1] = x[plain + 1].replace("(resolves to 'index.html')", "(resolves to 'foo.html')")
    out["wrong_target"] = (j(x), 1)
    x = list(lines)
    x.insert(plain, "[ERROR] Metadata: Description is too long")
    out["extra_error"] = (j(x), 1)
    x = list(lines)
    x.insert(plain, "[WARNING] Some check: something odd")
    out["extra_warning"] = (j(x), 1)
    x = list(lines)
    x[plain + 2:plain + 2] = lines[plain:plain + 2]
    out["dup_block"] = (j(x), 1)
    return out


EXPECT = {"control": True, "false": False, "empty_rc0": False, "no_overall": False, "drop_block": False,
          "extra_link": False, "wrong_target": False, "extra_error": False, "extra_warning": False,
          "dup_block": False, "not_official": False}


@pytest.fixture(scope="module")
def gate_case(lp: SimpleNamespace, tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    d = tmp_path_factory.mktemp("packgate")
    pack = d / lp.packs["fr"].name                      # fr has an apostrophe title
    shutil.copy(lp.packs["fr"], pack)
    clean = lpzims.zimcheck_report(pack)
    fakes = {"false": "/bin/false",
             "not_official": lpzims.fake_zimcheck(d / "fake", "zimru", clean, version="zimcheck (zimru) 0.1.0")}
    for name, (text, rc) in _doctor(clean).items():
        fakes[name] = lpzims.fake_zimcheck(d / "fake", name, text, rc=rc)
    return SimpleNamespace(dir=d, pack=pack, fakes=fakes)


def _run(gate: str, pack: Path, zc: str, base: Path | None = None,
         env: dict[str, str] | None = None) -> tuple[bool | None, dict]:
    gj = Path(str(pack) + ".gate.json")
    gj.unlink(missing_ok=True)
    args = [str(pack)] + (["--base", str(base)] if base else [])
    r = subprocess.run([sys.executable, gate, *args], capture_output=True, text=True, timeout=300,
                       env={**os.environ, "PACK_GATE_ZIMCHECK": zc, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})})
    if not gj.exists():
        return None, {"stderr": r.stderr[-2000:]}
    g = json.loads(gj.read_text())
    if (r.returncode == 0) != g["ok"]:
        return None, g
    return g["ok"], g


@pytest.mark.parametrize("case", sorted(EXPECT))
def test_gate_verdict(case: str, gate_case: SimpleNamespace, lp: SimpleNamespace) -> None:
    ok, g = _run(str(TOOLS / "pack_gate.py"), gate_case.pack, gate_case.fakes[case], lp.base)
    assert ok is EXPECT[case], g.get("problems", g)
    if case == "control":
        assert g["zimcheck_summary"]["headers"] == g["zimcheck_summary"]["parsed"] == g["articles"] == 4
        assert g["pack_md5"] == lpzims.md5_of(gate_case.pack)
        assert g["checks"]["base_uuid_fits"] is True


def test_gate_needs_the_base_lineage(gate_case: SimpleNamespace, lp: SimpleNamespace, tmp_path: Path) -> None:
    other = tmp_path / "other.zim"
    lpzims.write_base(other, description="another base")
    ok, g = _run(str(TOOLS / "pack_gate.py"), gate_case.pack, gate_case.fakes["control"], other)
    assert ok is False and g["checks"]["base_uuid_fits"] is False
    child = tmp_path / "child.zim"
    lpzims.child_base(lp.base, child)
    ok, g = _run(str(TOOLS / "pack_gate.py"), gate_case.pack, gate_case.fakes["control"], child)
    assert ok is True, g["problems"]
    lpzims.child_base(lp.base, child, child_md5="0" * 32)
    ok, g = _run(str(TOOLS / "pack_gate.py"), gate_case.pack, gate_case.fakes["control"], child)
    assert ok is False and g["checks"]["base_uuid_fits"] is False


def test_pack_gate_ok_follows_the_bytes(gate_case: SimpleNamespace, lp: SimpleNamespace, tmp_path: Path) -> None:
    from tools.langpack.gatelib import pack_gate_ok
    pack = tmp_path / gate_case.pack.name
    shutil.copy(gate_case.pack, pack)
    assert pack_gate_ok(str(pack))[0] is False                       # no gate file
    ok, _ = _run(str(TOOLS / "pack_gate.py"), pack, gate_case.fakes["control"], lp.base)
    assert ok is True and pack_gate_ok(str(pack)) == (True, "")
    with open(pack, "r+b") as fh:                                     # the pack changes after its gate
        fh.seek(100)
        b = fh.read(1)
        fh.seek(100)
        fh.write(bytes([b[0] ^ 1]))
    assert pack_gate_ok(str(pack)) == (False, "pack changed since its gate")


# Source mutants of pack_gate.py: each must give a wrong verdict on some case.
MUTANTS = {
    "old non-greedy header regex": (r"article '(.*)':$", r"article '([^']*)':$"),
    "Overall check removed": ("    if overall is None:\n", "    if False:\n"),
    "duplicate-header check removed": ("    if len(arts) != len(set(arts)):", "    if False:"),
    "resolves-to check removed": ('lm.group(2) == "index.html" and ', ""),
    "other [ERROR] ignored": ("    if other:\n        problems += other[:5]", "    if False:\n        pass"),
    "links-per-block check removed": ("len(links) == 1", "len(links) >= 1"),
    "only [ERROR] lines examined": ('if ln.startswith("[") and not ln.startswith("[INFO] "):',
                                    'if ln.startswith("[ERROR] "):'),
    "official-zimcheck check removed": ('checks["zimcheck_is_official"] = zver.startswith("zim-tools ")',
                                        'checks["zimcheck_is_official"] = True'),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_gate_mutant_is_killed(name: str, gate_case: SimpleNamespace, lp: SimpleNamespace, tmp_path: Path) -> None:
    a, b = MUTANTS[name]
    src = (TOOLS / "pack_gate.py").read_text()
    assert src.count(a) == 1, name
    mp = tmp_path / "pack_gate.py"
    mp.write_text(src.replace(a, b))
    env = {"PYTHONPATH": str(TOOLS.parents[1])}                       # the mutant lives outside the checkout
    wrong = []
    for case in sorted(EXPECT):
        ok, _ = _run(str(mp), gate_case.pack, gate_case.fakes[case], lp.base, env)
        if ok is not EXPECT[case]:
            wrong.append(case)
            break
    assert wrong, f"mutant survived: {name}"


def test_the_stand_in_matches_the_official_zimcheck(lp: SimpleNamespace) -> None:
    zc = lpzims.official_zimcheck()
    if zc is None:
        pytest.skip("no official zimcheck (zim-tools): set PACK_GATE_ZIMCHECK or put it on PATH")
    for pack in lp.packs.values():
        r = subprocess.run([zc, "-A", str(pack)], capture_output=True, text=True, timeout=300)
        assert (r.stdout + r.stderr).splitlines()[:-1] == lpzims.zimcheck_report(pack).splitlines()[:-1]


def test_gate_with_the_official_zimcheck(lp: SimpleNamespace, tmp_path: Path) -> None:
    zc = lpzims.official_zimcheck()
    if zc is None:
        pytest.skip("no official zimcheck (zim-tools): set PACK_GATE_ZIMCHECK or put it on PATH")
    pack = tmp_path / lp.packs["de"].name
    shutil.copy(lp.packs["de"], pack)
    r = tool("pack_gate.py", pack, "--base", lp.base, env={"PACK_GATE_ZIMCHECK": zc})
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(Path(str(pack) + ".gate.json").read_text())["zimcheck"].startswith("zim-tools ")
