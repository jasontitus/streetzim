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
