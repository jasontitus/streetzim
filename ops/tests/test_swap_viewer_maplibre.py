"""cloud/swap_viewer_rust.py --maplibre gives a map built before 2026-11
what a build now writes: the vendored MapLibre (maplibre-gl.js, .css),
maplibre-gl-legacy.js and the script fonts with their licence, byte for
byte, with every other entry as it was. Without the option nothing of
MapLibre changes."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
pytest.importorskip("libzim")


def _swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw) -> tuple[Path, Path]:
    pytest.importorskip("streetzim.zim_writer")
    from tests.langpack import lpzims
    os.environ.setdefault("STREETZIM_ROOT", str(ROOT))
    spec = importlib.util.spec_from_file_location("swap_viewer_rust_maplibre_under_test",
                                                  ROOT / "ops" / "cloud" / "swap_viewer_rust.py")
    assert spec and spec.loader
    svr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svr)
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: False)
    monkeypatch.setattr(svr, "_fs_type", lambda p: "ext4")
    src = tmp_path / "osm-testland-2026-10-13.zim"
    lpzims.write_base(src, slots=lpzims.VIEWER_V1)
    dst = tmp_path / "out" / src.name
    dst.parent.mkdir()
    (tmp_path / "t").mkdir()
    svr.swap_viewer_rust(str(src), str(dst), tmp_dir=str(tmp_path / "t"), **kw)
    return src, dst


def _entries(path: Path) -> dict[str, tuple[str, bytes]]:
    from libzim.reader import Archive
    a = Archive(str(path))
    out = {}
    for i in range(a.entry_count):
        e = a._get_entry_by_id(i)
        if not e.is_redirect:
            item = e.get_item()
            out[e.path] = (item.mimetype, bytes(item.content))
    return out


def test_maplibre_swaps_in_the_vendored_files_and_adds_the_rest(tmp_path, monkeypatch):
    from streetzim import viewer_assets as va
    src, dst = _swap(tmp_path, monkeypatch, maplibre=True)
    before, after = _entries(src), _entries(dst)
    vendor = va.VENDOR
    want = {
        "maplibre-gl.js": ("application/javascript", (vendor / "maplibre-gl/maplibre-gl.js").read_bytes()),
        "maplibre-gl.css": ("text/css", (vendor / "maplibre-gl/maplibre-gl.css").read_bytes()),
        va.MAPLIBRE_LEGACY_ENTRY: ("application/javascript",
                                   (vendor / va.MAPLIBRE_LEGACY / va.MAPLIBRE_LEGACY_ENTRY).read_bytes()),
        va.SCRIPT_FONTS_LICENCE: ("text/plain", (vendor / va.SCRIPT_FONTS / "OFL.txt").read_bytes()),
    }
    for f in (vendor / va.SCRIPT_FONTS).glob("*.otf"):
        want[f.name] = ("font/otf", f.read_bytes())
    assert len([p for p in want if p.endswith(".otf")]) == 12
    for path, (mime, data) in want.items():
        assert after[path] == (mime, data), path
    # Everything else is the source's, apart from the swapped viewer pages.
    viewer = {"index.html", "places.html", "routing-worker.js"}
    for path, value in before.items():
        if path in want or path in viewer:
            continue
        if path.endswith(".json"):   # the swap writes wiki-geo-index.json compactly
            assert after[path][0] == value[0] and json.loads(after[path][1]) == json.loads(value[1]), path
        else:
            assert after.get(path) == value, path
    assert set(after) - set(before) - set(want) == set()


def test_without_the_option_maplibre_is_left_alone(tmp_path, monkeypatch):
    src, dst = _swap(tmp_path, monkeypatch)
    before, after = _entries(src), _entries(dst)
    for path in ("maplibre-gl.js", "maplibre-gl.css"):
        assert after.get(path) == before.get(path)
    assert not any(p.endswith(".otf") or p == "maplibre-gl-legacy.js" for p in after)


# ---- --back-bar --------------------------------------------------------

def _svr():
    spec = importlib.util.spec_from_file_location("swap_viewer_rust_backbar_under_test",
                                                  ROOT / "ops" / "cloud" / "swap_viewer_rust.py")
    assert spec and spec.loader
    svr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svr)
    return svr


@pytest.mark.parametrize("path,title,depth,script", [
    ("wiki-article/Vaduz", "Vaduz", 0, "../sz-back.js"),
    ("wiki-article/AC/DC", "AC/DC", 0, "../../sz-back.js"),
    ("~w/hi/wiki-article/शिलांग", "शिलांग", 2, "../sz-back.js"),
    ("~w/hi/wiki-article/A/B/C", "A/B/C", 2, "../../../sz-back.js"),
])
def test_back_bar_retrofit_gives_an_old_page_what_a_build_writes(path, title, depth, script):
    from cloud import wiki_articles as wa
    svr = _svr()
    built_now = wa.back_to_map_bar(title, depth)
    old_bar = built_now.split("<script")[0]                        # as built before the script
    page = f"<html><body>{old_bar}<h1>{title}</h1></body></html>".encode()
    out = svr.back_bar_retrofit(path, page)
    assert out == f"<html><body>{built_now}<h1>{title}</h1></body></html>".encode()
    assert f'<script src="{script}"></script>' in out.decode()
    assert svr.back_bar_retrofit(path, out) is out                 # once only


@pytest.mark.parametrize("path,page", [
    ("index.html", b'<nav class="sz-back"><a href="x">&#8592; Back to map</a></nav>'),
    ("wiki-image/a.webp", b'&#8592; Back to map</a></nav>'),
    ("wiki-article/Foo", b"<html><body><a href='../index.html'>map</a></body></html>"),
    ("search/foo-1.html", b'&#8592; Back to map</a></nav>'),
])
def test_back_bar_retrofit_leaves_everything_else(path, page):
    assert _svr().back_bar_retrofit(path, page) is page


def test_back_bar_swap_adds_the_script_beside_each_article_folder(tmp_path, monkeypatch):
    from cloud.wiki_articles import back_bar_script
    src, dst = _swap(tmp_path, monkeypatch, back_bar=True)
    before, after = _entries(src), _entries(dst)
    roots = {p.split("wiki-article/")[0] for p in before if "wiki-article/" in p}
    assert roots                                                   # the fixture has articles
    for root in roots:
        assert after[root + "sz-back.js"] == ("application/javascript", back_bar_script())
    assert {p for p in after if p.endswith("sz-back.js")} == {r + "sz-back.js" for r in roots}
