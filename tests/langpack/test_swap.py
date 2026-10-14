"""swap_base_viewer.py's copy of the base: a reflink, or a full copy only
when --allow-full-copy says so (a silent multi-GB copy doubles the disk use
of a base). The swap itself needs a real base: test_real_data.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.langpack.conftest import tool
from tools.langpack.swap_base_viewer import copy_base


def _cp(tmp_path: Path, reflink_ok: bool) -> str:
    """A cp that can (or cannot) reflink."""
    p = tmp_path / "cp"
    p.write_text(f"#!{sys.executable}\nimport shutil, sys\n"
                 f"a = sys.argv[1:]\nif '--reflink=always' in a and not {reflink_ok}: sys.exit(1)\n"
                 "a = [x for x in a if not x.startswith('--')]\nshutil.copyfile(a[0], a[1])\n")
    p.chmod(0o755)
    return str(p)


def test_reflink(tmp_path: Path) -> None:
    src, dst = tmp_path / "a.zim", tmp_path / "b.zim"
    src.write_bytes(b"z" * 1000)
    assert copy_base(str(src), str(dst), False, cp=_cp(tmp_path, True)) == "reflink"
    assert dst.read_bytes() == src.read_bytes()


def test_no_silent_full_copy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src, dst = tmp_path / "a.zim", tmp_path / "b.zim"
    src.write_bytes(b"z" * 1000)
    cp = _cp(tmp_path, False)
    with pytest.raises(SystemExit, match="--allow-full-copy"):
        copy_base(str(src), str(dst), False, cp=cp)
    assert not dst.exists()
    assert copy_base(str(src), str(dst), True, cp=cp) == "full"
    assert "WARNING" in capsys.readouterr().err and dst.read_bytes() == src.read_bytes()


def test_cli_stops_without_the_flag_when_cp_cannot_reflink(tmp_path: Path) -> None:
    src = tmp_path / "a.zim"
    src.write_bytes(b"z" * 1000)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "cp").symlink_to(_cp(tmp_path, False))
    import os
    r = tool("swap_base_viewer.py", src, tmp_path / "b.zim", tmp_path, "test",
             env={"PATH": f"{bindir}:{os.environ['PATH']}"})
    assert r.returncode != 0 and "--allow-full-copy" in r.stderr
    assert not (tmp_path / "b.zim").exists()
