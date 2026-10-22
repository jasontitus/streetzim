"""Checks that need real StreetZim files or the browser stack; each skips
unless its environment variables point at them (docs/language-pack-pipeline.md):

  LANGPACK_TEST_SWAP_BASE   a published base whose viewer differs from this
                            checkout's resources/viewer (swap_base_viewer.py)
  LANGPACK_TEST_GATES       "BASE PACKS_DIR LANGS REGION_LANGS REGION REGION_JSON PORT"
                            for run_gates.sh end to end (kiwix-serve, node,
                            playwright-core and Chromium: see run_gates.sh)
  LANGPACK_TEST_TMP         scratch directory on the same filesystem as the
                            files (reflink copies); default pytest's tmp_path
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("libzim")

from tests.langpack.conftest import ROOT, TOOLS, tool  # noqa: E402


def _scratch(tmp_path: Path) -> Path:
    t = os.environ.get("LANGPACK_TEST_TMP")
    if not t:
        return tmp_path
    d = Path(t) / f"pytest-{os.getpid()}-{tmp_path.name}"
    d.mkdir(parents=True)
    return d


def _copy(src: Path, dst: Path) -> None:
    subprocess.run(["cp", "--reflink=auto", str(src), str(dst)], check=True)
    dst.chmod(0o644)


@pytest.fixture
def swap_base() -> Path:
    p = os.environ.get("LANGPACK_TEST_SWAP_BASE")
    if not p:
        pytest.skip("set LANGPACK_TEST_SWAP_BASE to a base whose viewer differs from this checkout's")
    return Path(p)


def _swap(src: Path, dst: Path, vexp: Path) -> subprocess.CompletedProcess[str]:
    return tool("swap_base_viewer.py", src, dst, vexp, "test", timeout=3600)


def test_swap_records_lineage_and_ignores_a_forged_sidecar(swap_base: Path, tmp_path: Path) -> None:
    from tests.langpack import lpzims
    from tools.langpack.merge_zim import base_lineage
    d = _scratch(tmp_path)
    try:
        src = d / "forged-src.zim"
        _copy(swap_base, src)
        Path(str(src) + ".parent.json").write_text(json.dumps(
            {"child_uuid": "f" * 32, "child_md5": "0" * 32, "parent_uuid": "a" * 32, "parents": ["b" * 32]}))
        r = _swap(src, d / "from-forged.zim", ROOT)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        side = json.loads(Path(str(d / "from-forged.zim") + ".parent.json").read_text())
        assert side["parent_uuid"] == lpzims.read_uuid(src) and side["parents"] == []
        src2 = d / "valid-src.zim"
        _copy(swap_base, src2)
        Path(str(src2) + ".parent.json").write_text(json.dumps(
            {"child_uuid": lpzims.read_uuid(src2), "child_md5": lpzims.md5_of(src2), "parent_uuid": "c" * 32,
             "parents": []}))
        out = d / "from-valid.zim"
        r = _swap(src2, out, ROOT)
        assert r.returncode == 0, r.stdout[-2000:]
        lin = base_lineage(str(out), bytes.fromhex(lpzims.read_uuid(out)))
        assert {"c" * 32, lpzims.read_uuid(src2)} <= lin
    finally:
        if d != tmp_path:
            shutil.rmtree(d)


def test_failing_swap_leaves_nothing(swap_base: Path, tmp_path: Path) -> None:
    d = _scratch(tmp_path)
    try:
        vexp = d / "vexp"
        shutil.copytree(ROOT / "resources", vexp / "resources", symlinks=True)
        shutil.copytree(ROOT / "cloud", vexp / "cloud", symlinks=True)
        shutil.copytree(ROOT / "streetzim", vexp / "streetzim", symlinks=True)
        ih = vexp / "resources" / "viewer" / "index.html"
        ih.write_text(ih.read_text(encoding="utf-8").replace("WIKI_PACKS", "WIKI_PACKZ"), encoding="utf-8")
        out = d / "bad.zim"
        r = _swap(swap_base, out, vexp)
        assert r.returncode != 0
        assert not out.exists() and not Path(str(out) + ".parent.json").exists()
    finally:
        if d != tmp_path:
            shutil.rmtree(d)


def test_run_gates_end_to_end(tmp_path: Path) -> None:
    spec = os.environ.get("LANGPACK_TEST_GATES")
    if not spec:
        pytest.skip("set LANGPACK_TEST_GATES to 'BASE PACKS_DIR LANGS REGION_LANGS REGION REGION_JSON PORT'")
    args = shlex.split(spec)
    base, langs = Path(args[0]), args[2].split(",")
    work = Path(os.environ.get("LANGPACK_WORK") or _scratch(tmp_path) / "work")
    r = subprocess.run([str(TOOLS / "run_gates.sh"), *args], capture_output=True, text=True, timeout=4 * 3600,
                       env={**os.environ, "LANGPACK_WORK": str(work)})
    tag = base.name[:-4]
    for L in langs:
        v = json.loads((work / "verdicts" / tag / f"{L}.json").read_text())
        assert v["all_pass"], (L, v["checks"], r.stdout[-3000:])
