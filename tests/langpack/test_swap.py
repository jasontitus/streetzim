"""swap_base_viewer.py's copy of the base: a reflink, or a full copy only
when --allow-full-copy says so (a silent multi-GB copy doubles the disk use
of a base). The swap itself needs a real base: test_real_data.py."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

from tests.langpack.conftest import tool
from tools.langpack import swap_base_viewer
from tools.langpack.swap_base_viewer import compare, copy_base


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


@pytest.mark.parametrize("how", ["same path", "relative path", "symlink", "hard link"])
def test_dst_that_is_src_is_refused_before_anything_is_removed(how: str, tmp_path: Path) -> None:
    """`swap_base_viewer.py SRC SRC` must not delete the base."""
    import os
    src = tmp_path / "base.zim"
    src.write_bytes(b"z" * 1000)
    if how == "same path":
        dst = src
    elif how == "relative path":
        (tmp_path / "sub").mkdir()
        dst = tmp_path / "sub" / ".." / "base.zim"
    elif how == "symlink":
        dst = tmp_path / "link.zim"
        dst.symlink_to(src)
    else:
        dst = tmp_path / "hard.zim"
        os.link(src, dst)
    with pytest.raises(SystemExit, match="is SRC"):
        copy_base(str(src), str(dst), True, cp=_cp(tmp_path, True))
    assert src.read_bytes() == b"z" * 1000 and dst.exists()


def test_cli_swap_onto_itself_keeps_the_base(tmp_path: Path) -> None:
    src = tmp_path / "base.zim"
    src.write_bytes(b"z" * 1000)
    r = tool("swap_base_viewer.py", src, src, tmp_path, "test", "--allow-full-copy")
    assert r.returncode != 0 and "is SRC" in r.stderr and "reflink" not in r.stderr
    assert src.read_bytes() == b"z" * 1000


def test_missing_src(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="is not a file"):
        copy_base(str(tmp_path / "nope.zim"), str(tmp_path / "b.zim"), True, cp=_cp(tmp_path, True))


def test_compare_same_file(lp) -> None:
    same, n, differ = compare(str(lp.base), str(lp.base))
    assert same and n > 3 and differ == []


def test_compare_other_entries(lp) -> None:
    assert compare(str(lp.base), str(lp.packs["de"]))[0] is False


def test_compare_streams(lp, monkeypatch: pytest.MonkeyPatch) -> None:
    # the two files' dirents are read in step, never one list then the other:
    # Russia's or Canada's would not fit in memory
    log: list[tuple[str, int]] = []

    class Logged(swap_base_viewer.ZimReader):
        def iter_dirents(self, *a, **k):
            for i, d in enumerate(super().iter_dirents(*a, **k)):
                log.append((self.tag, i))
                yield d

    tags = iter("ab")

    def reader(path: str) -> Logged:
        r = Logged(path)
        r.tag = next(tags)
        return r
    monkeypatch.setattr(swap_base_viewer, "ZimReader", reader)
    compare(str(lp.base), str(lp.base))
    assert log[:4] == [("a", 0), ("b", 0), ("a", 1), ("b", 1)]


def _second(monkeypatch: pytest.MonkeyPatch, **over) -> None:
    """compare()'s second reader (dst) with methods overridden."""
    real = swap_base_viewer.ZimReader
    B = type("B", (real,), over)
    made: list[str] = []

    def reader(path: str):
        made.append(path)
        return (real if len(made) == 1 else B)(path)
    monkeypatch.setattr(swap_base_viewer, "ZimReader", reader)


def test_compare_extra_trailing_dirent(lp, monkeypatch: pytest.MonkeyPatch) -> None:
    real = swap_base_viewer.ZimReader.iter_dirents

    def more(self, *a, **k):
        last = None
        for d in real(self, *a, **k):
            last = d
            yield d
        yield last
    _second(monkeypatch, iter_dirents=more)
    assert compare(str(lp.base), str(lp.base))[0] is False


@pytest.mark.parametrize("field", ["mime", "title_raw", "cluster", "blob", "target", "offset"])
def test_compare_each_dirent_field(lp, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    real = swap_base_viewer.ZimReader.iter_dirents
    hit: list[object] = []

    def changed(self, *a, **k):
        for d in real(self, *a, **k):
            v = getattr(d, field)
            if not hit and v is not None:
                hit.append((d.path, field))
                d = dataclasses.replace(d, **{field: v + (b"x" if isinstance(v, bytes) else 1)})
            yield d
    _second(monkeypatch, iter_dirents=changed)
    assert compare(str(lp.base), str(lp.base))[0] is False, hit


def test_compare_redirect_target(lp, monkeypatch: pytest.MonkeyPatch) -> None:
    real = swap_base_viewer.ZimReader.iter_dirents

    def changed(self, *a, **k):
        for d in real(self, *a, **k):
            if d.is_redirect:
                d = dataclasses.replace(d, target=d.target + 1)
            yield d
    _second(monkeypatch, iter_dirents=changed)
    assert compare(str(lp.base), str(lp.base))[0] is False


def test_compare_reads_every_namespace(lp, monkeypatch: pytest.MonkeyPatch) -> None:
    real = swap_base_viewer.ZimReader.blob_bytes
    _second(monkeypatch, blob_bytes=lambda self, c, b: real(self, c, b) + b"!")
    same, _n, differ = compare(str(lp.base), str(lp.base))
    assert same and "Title" in differ, differ


def test_compare_names_every_changed_item(tmp_path: Path) -> None:
    pytest.importorskip("libzim")
    from tests.langpack import lpzims
    src, dst = tmp_path / "a.zim", tmp_path / "b.zim"
    lpzims.write_base(src, slots={"index.html": b"<html>viewer-one</html>", "places.html": b"p",
                                  "routing-worker.js": b"worker-one"})
    data = bytearray(src.read_bytes())
    for marker in (b"viewer-one", b"worker-one"):
        at = data.find(marker)
        assert at > 0 and data.count(marker) == 1
        data[at] = ord("V")
    dst.write_bytes(bytes(data))
    same, _n, differ = compare(str(src), str(dst))
    assert same and differ == ["index.html", "routing-worker.js"], differ
