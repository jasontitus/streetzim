"""Builds capped at --max-zoom 13 draw footways, cycleways and other paths
(streetzim/path_fold.py): each z13 tile carries its z14 children's class=path
features, halved into its grid, after everything it already had.

Checked on synthetic tiles and MBTiles:
  * a child's paths land at half their coordinates in the right quadrant,
    names too; other classes are not copied;
  * the parent's layers stay byte for byte (new features, keys and values
    are appended), and the same object comes back when nothing is added;
  * a layer that already has paths at z13 is left alone;
  * geometry the halving collapses is dropped;
  * the MBTiles reader finds children by TMS row, folds only with max_zoom
    13 and fold_paths, in one process or several, keeping tile order;
  * create_osm_zim folds by default with --max-zoom 13, --no-fold-paths
    turns it off, and a real create_zim build stores the folded tile.
"""
from __future__ import annotations

import argparse
import gzip
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

mvt = pytest.importorskip("mapbox_vector_tile")

from streetzim import path_fold  # noqa: E402

OPTS = {"y_coord_down": True, "extents": 4096}


def tile(layers: dict[str, list[tuple[str, dict]]]) -> bytes:
    return mvt.encode([{"name": n, "features": [{"geometry": g, "properties": p} for g, p in fs]}
                       for n, fs in layers.items()], default_options=OPTS)


def decode(data: bytes) -> dict:
    return mvt.decode(data, default_options={"y_coord_down": True})


def layer_bytes(data: bytes) -> dict[str, bytes]:
    return {path_fold._layer_name(v).decode(): bytes(v)
            for fno, wt, v, _s, _e in path_fold._fields(data) if fno == 3 and wt == 2}


ROAD = ("LINESTRING (0 0, 4096 4096)", {"class": "minor", "name": "Main St"})
WATER = ("POLYGON ((10 10, 500 10, 500 500, 10 500, 10 10))", {"class": "lake"})
PARENT = tile({"transportation": [ROAD], "water": [WATER]})
FOOTWAY = ("LINESTRING (100 200, 300 400)", {"class": "path", "subclass": "footway"})
SERVICE = ("LINESTRING (0 0, 50 50)", {"class": "service"})
TRAIL_NAME = ("LINESTRING (1000 1000, 1200 1000)", {"class": "path", "name": "Trail A"})


def test_paths_from_children_are_added_halved_in_their_quadrant():
    right = tile({"transportation": [FOOTWAY, SERVICE]})              # dx=1, dy=0
    below = tile({"transportation_name": [TRAIL_NAME]})              # dx=0, dy=1
    out = decode(path_fold.fold(PARENT, [(1, 0, right), (0, 1, below)]))
    feats = out["transportation"]["features"]
    assert feats[0]["properties"] == ROAD[1]
    assert [f["properties"] for f in feats[1:]] == [{"class": "path", "subclass": "footway"}]
    assert feats[1]["geometry"]["coordinates"] == [[2098, 100], [2198, 200]]
    names = out["transportation_name"]["features"]
    assert [f["properties"]["name"] for f in names] == ["Trail A"]
    assert names[0]["geometry"]["coordinates"] == [[500, 2548], [600, 2548]]
    assert out["water"] == decode(PARENT)["water"]


def test_the_parents_bytes_stay_and_new_ones_are_appended():
    child = tile({"transportation": [FOOTWAY]})
    before = layer_bytes(PARENT)
    after = layer_bytes(path_fold.fold(PARENT, [(0, 0, child)]))
    assert after["water"] == before["water"]
    assert after["transportation"].startswith(before["transportation"])
    keys = path_fold._Layer(after["transportation"]).keys
    assert len(keys) == len(set(keys))                    # "class" reused, not repeated


def test_nothing_to_add_returns_the_same_tile():
    no_paths = tile({"transportation": [SERVICE]})
    assert path_fold.fold(PARENT, [(0, 0, no_paths), (1, 1, b"")]) is PARENT
    assert path_fold.fold(PARENT, []) is PARENT


def test_a_layer_that_already_has_paths_is_left_alone():
    parent = tile({"transportation": [ROAD, FOOTWAY]})
    child = tile({"transportation": [("LINESTRING (5 5, 900 900)", {"class": "path"})]})
    assert path_fold.fold(parent, [(0, 0, child)]) is parent


def test_geometry_the_halving_collapses_is_dropped():
    child = tile({"transportation": [
        ("LINESTRING (11 10, 12 10)", {"class": "path", "subclass": "steps"}),
        ("POLYGON ((21 21, 22 21, 22 22, 21 22, 21 21))", {"class": "path", "subclass": "pedestrian"}),
        ("POLYGON ((0 0, 400 0, 400 400, 0 400, 0 0))", {"class": "path", "subclass": "pedestrian"}),
    ]})
    out = decode(path_fold.fold(PARENT, [(0, 0, child)]))
    added = out["transportation"]["features"][1:]
    assert [f["geometry"]["type"] for f in added] == ["Polygon"]


def _mbtiles(path: Path, tiles: dict[tuple[int, int, int], bytes]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                 "tile_row INTEGER, tile_data BLOB)")
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    for (z, x, y), data in tiles.items():                          # XYZ in, TMS stored
        conn.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)",
                     (z, x, (1 << z) - 1 - y, gzip.compress(data)))
    conn.commit()
    conn.close()
    return path


X13, Y13 = 4264, 2922


@pytest.fixture()
def mb(tmp_path):
    return _mbtiles(tmp_path / "t.mbtiles", {
        (12, X13 // 2, Y13 // 2): tile({"water": [WATER]}),
        (13, X13, Y13): PARENT,
        # the lower-left child only: dy must come from the TMS row
        (14, 2 * X13, 2 * Y13 + 1): tile({"transportation": [FOOTWAY]}),
        (14, 2 * X13 + 1, 2 * Y13): tile({"transportation": [SERVICE]}),
    })


def test_the_folder_finds_children_by_tms_row(mb):
    folder = path_fold.Folder(mb)
    gz = gzip.compress(PARENT)
    out = decode(folder(13, X13, Y13, gz))
    assert out["transportation"]["features"][1]["geometry"]["coordinates"] == [[50, 2148], [150, 2248]]
    assert folder(12, X13 // 2, Y13 // 2, b"x") == b"x"
    assert folder.stats == {"tiles": 1, "features": 1}
    folder.close()


def _paths_in(data: bytes) -> int:
    d = decode(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)
    return sum(f["properties"].get("class") == "path"
               for f in d.get("transportation", {}).get("features", []))


@pytest.mark.parametrize("max_zoom, fold, want", [(13, True, 1), (13, False, 0), (14, True, 0)])
def test_the_readers_fold_only_with_max_zoom_13(mb, monkeypatch, max_zoom, fold, want):
    from streetzim import tiles as T
    monkeypatch.setattr(T, "build_cpus", lambda: 1)
    streamed = {(z, x, y): d for z, x, y, d in
                T.iter_tiles_from_mbtiles(mb, max_zoom=max_zoom, fold_paths=fold)}
    loaded, _meta = T.extract_tiles_from_mbtiles(mb, max_zoom=max_zoom, fold_paths=fold)
    for got in (streamed, loaded):
        assert _paths_in(got[(13, X13, Y13)]) == want
        assert _paths_in(got[(12, X13 // 2, Y13 // 2)]) == 0


def test_several_processes_fold_the_same_and_keep_the_order(mb):
    items = [(12, X13 // 2, Y13 // 2, b"a"), (13, X13, Y13, gzip.compress(PARENT)),
             (13, X13 + 7, Y13, gzip.compress(PARENT)), (11, 1, 1, b"b")]
    one, two = {}, {}
    a = list(path_fold.fold_tiles(mb, items, workers=1, stats=one, batch=2))
    b = list(path_fold.fold_tiles(mb, items, workers=2, stats=two, batch=2))
    assert a == b and [t[:3] for t in a] == [t[:3] for t in items]
    assert one == two == {"tiles": 1, "features": 1}
    assert a[2][3] == items[2][3]                       # no children: unchanged


def test_create_osm_zim_folds_by_default_with_max_zoom_13():
    import create_osm_zim as C
    on = C.fold_paths_on
    assert on(argparse.Namespace(max_zoom=13, fold_paths=None))
    assert not on(argparse.Namespace(max_zoom=13, fold_paths=False))
    assert not on(argparse.Namespace(max_zoom=14, fold_paths=True))
    assert on(argparse.Namespace(max_zoom=13))                     # namespaces without it


def test_a_create_zim_build_stores_the_folded_tile(tmp_path, mb, monkeypatch):
    pytest.importorskip("libzim.writer")
    from libzim.reader import Archive
    from streetzim import tiles as T
    from streetzim.zim_writer import create_zim
    monkeypatch.setattr(T, "build_cpus", lambda: 1)
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    out = tmp_path / "f.zim"
    create_zim(
        out, tiles=None, tile_metadata={}, mbtiles_path=str(mb),
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"), maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "T"}, name="OSM - T", bbox=None,
        search_features=[{"name": "Casino", "type": "poi", "subtype": "casino",
                          "lat": 43.739, "lon": 7.428}],
        xapian_mode="none", max_zoom=13, fold_paths=True)
    data = bytes(Archive(out).get_entry_by_path(f"tiles/13/{X13}/{Y13}.pbf").get_item().content)
    assert _paths_in(data) == 1
