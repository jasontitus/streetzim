"""cloud/swap_viewer_rust.py keeps each entry's front-article flag. It
wrote every page and redirect as no front article (2026-10-29: Taipei had
5,276 of its 5,963 pages front before a swap and 0 after; Bahrain, Thailand
and Switzerland (de) likewise), and Kiwix counts no articles and opens no
random one in such a file."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
pytest.importorskip("libzim")


def _fronts(path: Path) -> set[str]:
    from tools.langpack.index_pack import front_bitmap, is_front
    from tools.langpack.zimfmt import ZimReader
    z = ZimReader(str(path))
    bits = front_bitmap(z)
    return {d.path.decode() for d in z.iter_dirents() if d.ns == ord("C") and is_front(bits, d.index)}


def test_the_swap_keeps_the_front_articles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("streetzim.zim_writer")
    from tests.langpack import lpzims
    os.environ.setdefault("STREETZIM_ROOT", str(ROOT))
    spec = importlib.util.spec_from_file_location("swap_viewer_rust_front_under_test",
                                                  ROOT / "ops" / "cloud" / "swap_viewer_rust.py")
    assert spec and spec.loader
    svr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svr)
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: False)
    monkeypatch.setattr(svr, "_fs_type", lambda p: "ext4")
    src = tmp_path / "osm-testland-2026-10-13.zim"
    lpzims.write_base(src, slots=lpzims.VIEWER_V1,
                      pages=[("search/manama-73.html", "Manama", "The capital")],
                      front_redirects=[("~7", "Al Manamah", "search/manama-73.html")])
    before = _fronts(src)
    # the fixture's: its articles, the extra page and redirect (front); wiki-article/Gamma (a redirect, not)
    assert {"wiki-article/Alpha_Town", "search/manama-73.html", "~7"} <= before
    assert "wiki-article/Gamma" not in before
    dst = tmp_path / "out" / src.name
    dst.parent.mkdir()
    (tmp_path / "t").mkdir()
    svr.swap_viewer_rust(str(src), str(dst), tmp_dir=str(tmp_path / "t"))
    assert _fronts(dst) == before


def test_a_file_an_earlier_swap_left_without_front_articles_gets_the_builds_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No front article at all (a file the old swap wrote): the build's rule,
    by path, as streetzim/zim_writer.py writes them: index.html, the places'
    pages under search/ and their other names; not places.html nor the
    Wikipedia articles."""
    pytest.importorskip("streetzim.zim_writer")
    from tests.langpack import lpzims
    os.environ.setdefault("STREETZIM_ROOT", str(ROOT))
    spec = importlib.util.spec_from_file_location("swap_viewer_rust_front_restore", ROOT / "ops" / "cloud" / "swap_viewer_rust.py")
    assert spec and spec.loader
    svr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svr)
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: False)
    monkeypatch.setattr(svr, "_fs_type", lambda p: "ext4")
    src = tmp_path / "osm-testland-2026-10-13.zim"
    lpzims.write_base(src, slots=lpzims.VIEWER_V1, fronts=False,
                      pages=[("search/manama-73.html", "Manama", "The capital")],
                      front_redirects=[("search/~7", "Al Manamah", "search/manama-73.html")])
    assert not _fronts(src)
    dst = tmp_path / "out" / src.name
    dst.parent.mkdir()
    (tmp_path / "t").mkdir()
    svr.swap_viewer_rust(str(src), str(dst), tmp_dir=str(tmp_path / "t"))
    # places.html (a viewer file), wiki-article/* and the JSON stay as they were
    assert _fronts(dst) == {"index.html", "search/manama-73.html", "search/~7"}


def _svr(name: str, monkeypatch: pytest.MonkeyPatch):
    os.environ.setdefault("STREETZIM_ROOT", str(ROOT))
    spec = importlib.util.spec_from_file_location(name, ROOT / "ops" / "cloud" / "swap_viewer_rust.py")
    assert spec and spec.loader
    svr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svr)
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: False)
    monkeypatch.setattr(svr, "_fs_type", lambda p: "ext4")
    return svr


def test_some_front_articles_but_not_index_html_gets_the_builds_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An earlier swap that flagged its own new pages front but not
    index.html: the copied flags and the build's rule together."""
    pytest.importorskip("streetzim.zim_writer")
    from tests.langpack import lpzims
    svr = _svr("swap_viewer_rust_front_partial", monkeypatch)
    src = tmp_path / "osm-testland-2026-10-13.zim"
    lpzims.write_base(src, slots=lpzims.VIEWER_V1, fronts=False,
                      pages=[("search/manama-73.html", "Manama", "The capital")],
                      front_redirects=[("search/~7", "Al Manamah", "search/manama-73.html")],
                      front_pages=[("admin/capital.html", "Capital Governorate", "A governorate")])
    assert _fronts(src) == {"admin/capital.html"}
    dst = tmp_path / "out" / src.name
    dst.parent.mkdir()
    (tmp_path / "t").mkdir()
    svr.swap_viewer_rust(str(src), str(dst), tmp_dir=str(tmp_path / "t"))
    assert _fronts(dst) == {"index.html", "search/manama-73.html", "search/~7", "admin/capital.html"}


def test_a_source_listing_every_entry_keeps_front_only_on_html(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Some older builds listed every entry as front: tiles and JSON are
    never front after a swap (zimcheck's title index check)."""
    pytest.importorskip("streetzim.zim_writer")
    from tests.langpack import lpzims
    svr = _svr("swap_viewer_rust_front_all", monkeypatch)
    src = tmp_path / "osm-testland-2026-10-13.zim"
    lpzims.write_base(src, slots=lpzims.VIEWER_V1, all_front=True,
                      front_redirects=[("tiles/latest", "latest tile", "tiles/2/0/0.pbf")])
    assert {"map-config.json", "tiles/0/0/0.pbf", "tiles/latest"} <= _fronts(src)
    dst = tmp_path / "out" / src.name
    dst.parent.mkdir()
    (tmp_path / "t").mkdir()
    svr.swap_viewer_rust(str(src), str(dst), tmp_dir=str(tmp_path / "t"))
    got = _fronts(dst)
    assert "index.html" in got and "wiki-article/Alpha_Town" in got
    assert not {p for p in got if not (p.endswith(".html") or p.startswith("wiki-article/"))}, got
