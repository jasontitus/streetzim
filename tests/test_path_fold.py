"""Builds capped at --max-zoom 13 draw footways, cycleways and other paths
(streetzim/path_fold.py): each z13 tile carries its z14 children's class=path
features, halved into its grid, after everything it already had.

Checked on synthetic tiles and MBTiles:
  * a child's paths land at half their coordinates in the right quadrant,
    names too, whatever the two layers' extents; other classes are not
    copied; a feature two children both carry is added once;
  * the parent's layers stay byte for byte (new features, keys and values
    are appended, existing keys and values reused), and the same object
    comes back when nothing is added;
  * a layer that already has paths at z13 is left alone;
  * geometry: multi-part cursors, ClosePath, multipoints; rings the halving
    collapses or turns inside out are dropped (an outer ring with its
    holes), the rest kept; unpacked or split repeated fields read; a
    "path" value listed twice; a new layer has version 2;
  * the MBTiles reader finds children by TMS row, only for z13, folds only
    with max_zoom 13 and fold_paths, in one process or several in tile
    order with no workers left behind, keeps gzip tiles gzip, skips a z14
    tile that will not decode, and opens a path with '#' in it;
  * create_osm_zim folds by default with --max-zoom 13 on both its paths
    (in-memory tiles and create_zim), --no-fold-paths turns it off, and a
    real create_zim build stores the folded tile.
"""
from __future__ import annotations

import argparse
import gzip
import multiprocessing
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

mvt = pytest.importorskip("mapbox_vector_tile")

from streetzim import path_fold as pf  # noqa: E402


def tile(layers: dict[str, list[tuple[str, dict]]], extent: int = 4096) -> bytes:
    return mvt.encode([{"name": n, "features": [{"geometry": g, "properties": p} for g, p in fs]}
                       for n, fs in layers.items()],
                      default_options={"y_coord_down": True, "extents": extent})


def decode(data: bytes) -> dict:
    return mvt.decode(pf._decoded(data), default_options={"y_coord_down": True})


def layers_raw(data: bytes) -> dict[str, bytes]:
    return {pf._layer_name(v).decode(): bytes(v)
            for fno, wt, v, _s, _e in pf._fields(pf._decoded(data)) if fno == 3 and wt == 2}


def value(s: str) -> bytes:
    return pf._enc_len(1, s.encode())


def raw_tile(name: str, feats: list[tuple[int, list[tuple[str, str]], list[int]]], *,
             extent: int | None = 4096, version: bool = True, unpacked: bool = False,
             dup_path: bool = False) -> bytes:
    """A tile written field by field: (geometry type, tags, command ints)."""
    keys: list[bytes] = []
    vals: list[bytes] = []
    body = b""
    for gtype, tags, cmds in feats:
        idx = []
        for k, v in tags:
            if k.encode() not in keys:
                keys.append(k.encode())
            if value(v) not in vals:
                vals.append(value(v))
            vi = vals.index(value(v))
            if dup_path and v == "path":
                vals.append(value("path"))      # a second "path" entry, used instead
                vi = len(vals) - 1
            idx += [keys.index(k.encode()), vi]
        if unpacked:
            f = b"".join(pf._enc_varint(2 << 3) + pf._enc_varint(i) for i in idx)
            f += pf._enc_varint(3 << 3) + pf._enc_varint(gtype)
            half = len(cmds) // 2
            f += pf._enc_packed(4, cmds[:half]) + pf._enc_packed(4, cmds[half:])
        else:
            f = pf._enc_packed(2, idx) + pf._enc_varint(3 << 3) + pf._enc_varint(gtype) \
                + pf._enc_packed(4, cmds)
        body += pf._enc_len(2, f)
    layer = (pf._enc_varint(15 << 3) + pf._enc_varint(2) if version else b"") + pf._enc_len(1, name.encode())
    layer += body + b"".join(pf._enc_len(3, k) for k in keys) + b"".join(pf._enc_len(4, v) for v in vals)
    if extent is not None:
        layer += pf._enc_varint(5 << 3) + pf._enc_varint(extent)
    return pf._enc_len(3, layer)


def line(*pts: tuple[int, int]) -> list[int]:
    cmds, cx, cy = [], 0, 0
    for i, (x, y) in enumerate(pts):
        if i == 0:
            cmds.append(pf._MOVE_TO | 1 << 3)
        elif i == 1:
            cmds.append(pf._LINE_TO | (len(pts) - 1) << 3)
        cmds += [pf._zigzag(x - cx), pf._zigzag(y - cy)]
        cx, cy = x, y
    return cmds


def added_features(out: bytes, name: str = "transportation", skip: int = 1) -> list[dict]:
    return decode(out)[name]["features"][skip:]


ROAD = ("LINESTRING (0 0, 4096 4096)", {"class": "minor", "name": "Main St"})
WATER = ("POLYGON ((10 10, 500 10, 500 500, 10 500, 10 10))", {"class": "lake"})
PARENT = tile({"transportation": [ROAD], "water": [WATER]})
FOOTWAY = ("LINESTRING (100 200, 300 400)", {"class": "path", "subclass": "footway"})
SERVICE = ("LINESTRING (0 0, 50 50)", {"class": "service"})
TRAIL_NAME = ("LINESTRING (1000 1000, 1200 1000)", {"class": "path", "name": "Trail A"})


def test_paths_from_children_are_added_halved_in_their_quadrant():
    right = tile({"transportation": [FOOTWAY, SERVICE]})              # dx=1, dy=0
    below = tile({"transportation_name": [TRAIL_NAME]})              # dx=0, dy=1
    out = decode(pf.fold(PARENT, [(1, 0, right), (0, 1, below)]))
    feats = out["transportation"]["features"]
    assert feats[0]["properties"] == ROAD[1]
    assert [f["properties"] for f in feats[1:]] == [{"class": "path", "subclass": "footway"}]
    assert feats[1]["geometry"]["coordinates"] == [[2098, 100], [2198, 200]]
    names = out["transportation_name"]["features"]
    assert [f["properties"]["name"] for f in names] == ["Trail A"]
    assert names[0]["geometry"]["coordinates"] == [[500, 2548], [600, 2548]]
    assert out["water"] == decode(PARENT)["water"]


def test_the_parents_bytes_stay_and_its_keys_and_values_are_reused():
    parent = tile({"transportation": [ROAD, ("LINESTRING (9 9, 99 99)",
                                             {"class": "service", "subclass": "footway"})],
                   "water": [WATER]})
    child = tile({"transportation": [FOOTWAY]})
    before = layers_raw(parent)
    after = layers_raw(pf.fold(parent, [(0, 0, child)]))
    assert after["water"] == before["water"]
    assert after["transportation"].startswith(before["transportation"])
    layer = pf._Layer(after["transportation"])
    assert len(layer.keys) == len(set(layer.keys))           # "class", "subclass" reused
    assert len(layer.values) == len(set(layer.values))       # "footway" reused


def test_extents_of_both_layers_are_honoured():
    parent = tile({"transportation": [ROAD]}, extent=512)
    child = tile({"transportation": [("LINESTRING (200 400, 600 800)", {"class": "path"})]},
                 extent=8192)
    got = added_features(pf.fold(parent, [(1, 0, child)]))
    # (x + 8192) * 512 / 16384, y * 512 / 16384, rounded half up
    assert got[0]["geometry"]["coordinates"] == [[262, 13], [275, 25]]
    new = raw_tile("water", [], extent=None)                          # parent without the layer
    got = added_features(pf.fold(new, [(0, 0, tile({"transportation": [FOOTWAY]}))]), skip=0)
    assert got[0]["geometry"]["coordinates"] == [[50, 100], [150, 200]]


def test_nothing_to_add_returns_the_same_tile():
    no_paths = tile({"transportation": [SERVICE]})
    assert pf.fold(PARENT, [(0, 0, no_paths), (1, 1, b"")]) is PARENT
    assert pf.fold(PARENT, []) is PARENT


def test_a_layer_that_already_has_paths_is_left_alone():
    parent = tile({"transportation": [ROAD, FOOTWAY]})
    child = tile({"transportation": [("LINESTRING (5 5, 900 900)", {"class": "path"})]})
    assert pf.fold(parent, [(0, 0, child)]) is parent


def test_a_feature_two_children_carry_is_added_once():
    # A path lying in the buffer both children share lands on the same z13 spot.
    a = raw_tile("transportation", [(2, [("class", "path")], line((4100, 10), (4120, 10)))])
    b = raw_tile("transportation", [(2, [("class", "path")], line((4, 10), (24, 10)))])
    stats: dict = {}
    got = added_features(pf.fold(PARENT, [(0, 0, a), (1, 0, b)], stats))
    assert len(got) == 1 and stats["features"] == 1


def test_multi_part_lines_keep_the_cursor_across_parts():
    child = tile({"transportation": [("MULTILINESTRING ((100 100, 200 100), (300 300, 400 300))",
                                      {"class": "path"})]})
    got = added_features(pf.fold(PARENT, [(0, 0, child)]))
    assert got[0]["geometry"]["coordinates"] == [[[50, 50], [100, 50]], [[150, 150], [200, 150]]]


def test_polygons_end_rings_with_closepath():
    child = tile({"transportation": [("POLYGON ((0 0, 400 0, 400 400, 0 400, 0 0))",
                                      {"class": "path", "subclass": "pedestrian"})]})
    out = pf.fold(PARENT, [(0, 0, child)])
    layer = pf._Layer(layers_raw(out)["transportation"])
    geom = [pf._packed(wt, v) for fno, wt, v, _s, _e in pf._fields(layer.features[-1]) if fno == 4][0]
    assert geom[-1] == pf._CLOSE_PATH | 1 << 3


def test_rings_the_halving_breaks_are_dropped_and_the_rest_kept():
    child = tile({"transportation": [
        ("LINESTRING (11 10, 12 10)", {"class": "path", "subclass": "steps"}),           # collapses
        ("POLYGON ((21 21, 22 21, 22 22, 21 22, 21 21))", {"class": "path"}),             # collapses
        ("POLYGON ((0 0, 400 0, 400 400, 0 400, 0 0))", {"class": "path", "subclass": "pedestrian"}),
        # a big square with a 1x1 hole: the hole collapses, the square stays
        ("POLYGON ((1000 1000, 3000 1000, 3000 3000, 1000 3000, 1000 1000), "
         "(1501 1501, 1501 1502, 1502 1502, 1502 1501, 1501 1501))", {"class": "path", "subclass": "square"}),
    ]})
    got = added_features(pf.fold(PARENT, [(0, 0, child)]))
    assert [(f["geometry"]["type"], f["properties"].get("subclass")) for f in got] == \
        [("Polygon", "pedestrian"), ("Polygon", "square")]
    assert len(got[1]["geometry"]["coordinates"]) == 1                          # hole gone


def test_a_ring_turned_inside_out_is_dropped():
    # (0,0) (2,1) (3,2): area +1 (outer); halved to (0,0) (1,1) (2,1): area -1.
    ring = [pf._MOVE_TO | 1 << 3, 0, 0, pf._LINE_TO | 2 << 3,
            pf._zigzag(2), pf._zigzag(1), pf._zigzag(1), pf._zigzag(1), pf._CLOSE_PATH | 1 << 3]
    child = raw_tile("transportation", [(3, [("class", "path")], ring)])
    assert pf.fold(PARENT, [(0, 0, child)]) is PARENT


def test_multipoints_are_halved():
    child = raw_tile("transportation", [(1, [("class", "path")],
                                         [pf._MOVE_TO | 2 << 3, pf._zigzag(100), pf._zigzag(100),
                                          pf._zigzag(100), pf._zigzag(0)])])
    got = added_features(pf.fold(PARENT, [(0, 0, child)]))
    assert got[0]["geometry"]["coordinates"] == [[50, 50], [100, 50]]


def test_unpacked_split_fields_and_a_second_path_value_are_read():
    feats = [(2, [("class", "path"), ("subclass", "cycleway")], line((100, 100), (300, 100), (300, 300)))]
    for kw in ({"unpacked": True}, {"dup_path": True}, {"version": False}):
        got = added_features(pf.fold(PARENT, [(0, 0, raw_tile("transportation", feats, **kw))]))
        assert [f["geometry"]["coordinates"] for f in got] == [[[50, 50], [150, 50], [150, 150]]], kw


def test_a_new_layer_is_version_2():
    out = pf.fold(raw_tile("water", []), [(0, 0, tile({"transportation_name": [TRAIL_NAME]}))])
    layer = layers_raw(out)["transportation_name"]
    assert [v for fno, wt, v, _s, _e in pf._fields(layer) if fno == 15] == [2]


def _mbtiles(path: Path, tiles: dict[tuple[int, int, int], bytes], *, gz: bool = True) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                 "tile_row INTEGER, tile_data BLOB)")
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    for (z, x, y), data in tiles.items():                          # XYZ in, TMS stored
        conn.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)",
                     (z, x, (1 << z) - 1 - y, gzip.compress(data) if gz else data))
    conn.commit()
    conn.close()
    return path


X13, Y13 = 4264, 2922
X12, Y12 = 2131, 1460          # its doubled position holds a z14 tile with paths too


@pytest.fixture()
def mb(tmp_path):
    return _mbtiles(tmp_path / "t.mbtiles", {
        (12, X12, Y12): PARENT,
        (13, X13, Y13): PARENT,
        # the lower-left child only: dy must come from the TMS row
        (14, 2 * X13, 2 * Y13 + 1): tile({"transportation": [FOOTWAY]}),
        (14, 2 * X13 + 1, 2 * Y13): tile({"transportation": [SERVICE]}),
        (14, 2 * X12, 2 * Y12): tile({"transportation": [FOOTWAY]}),
    })


def test_the_folder_finds_children_by_tms_row_and_keeps_gzip(mb):
    folder = pf.Folder(mb)
    gz = gzip.compress(PARENT)
    out = folder(13, X13, Y13, gz)
    assert out[:2] == b"\x1f\x8b"
    assert decode(out)["transportation"]["features"][1]["geometry"]["coordinates"] == [[50, 2148], [150, 2248]]
    assert folder(13, X13, Y13, PARENT)[:2] != b"\x1f\x8b"
    assert folder(12, X12, Y12, PARENT) is PARENT            # only z13 is folded
    assert folder.stats == {"tiles": 2, "features": 2}
    folder.close()


def test_a_bad_child_is_skipped_and_a_bad_parent_returned(tmp_path):
    mb = _mbtiles(tmp_path / "b.mbtiles", {
        (14, 2 * X13, 2 * Y13): b"\x1f\x8b" + b"broken",
        (14, 2 * X13 + 1, 2 * Y13): gzip.compress(tile({"transportation": [FOOTWAY]})),
    }, gz=False)
    folder = pf.Folder(mb)
    out = folder(13, X13, Y13, PARENT)
    assert len(added_features(out)) == 1 and folder.stats["bad_children"] == 1
    bad = b"\x1f\x8b" + b"not gzip"
    assert folder(13, X13, Y13, bad) is bad
    folder.close()


def test_a_path_with_a_hash_opens_and_creates_nothing(tmp_path):
    d = tmp_path / "a#b?c%d"
    d.mkdir()
    mb = _mbtiles(d / "t.mbtiles", {(14, 2 * X13, 2 * Y13): tile({"transportation": [FOOTWAY]})})
    assert pf.has_source_zoom(mb)
    folder = pf.Folder(mb)
    assert len(added_features(folder(13, X13, Y13, PARENT))) == 1
    folder.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a#b?c%d"]


def _paths_in(data: bytes) -> int:
    return sum(f["properties"].get("class") == "path"
               for f in decode(data).get("transportation", {}).get("features", []))


@pytest.mark.parametrize("max_zoom, fold, want", [(13, True, 1), (13, False, 0), (14, True, 0)])
def test_the_readers_fold_only_with_max_zoom_13(mb, monkeypatch, max_zoom, fold, want):
    from streetzim import tiles as T
    monkeypatch.setattr(T, "build_cpus", lambda: 1)
    streamed = {(z, x, y): d for z, x, y, d in
                T.iter_tiles_from_mbtiles(mb, max_zoom=max_zoom, fold_paths=fold)}
    loaded, _meta = T.extract_tiles_from_mbtiles(mb, max_zoom=max_zoom, fold_paths=fold)
    for got in (streamed, loaded):
        assert _paths_in(got[(13, X13, Y13)]) == want
        assert _paths_in(got[(12, X12, Y12)]) == 0


def test_several_processes_fold_the_same_in_order_and_leave_no_workers(tmp_path):
    src = {}
    for i in range(60):                          # every third z13 tile has a child with paths
        src[(13, X13 + i, Y13)] = PARENT
        if i % 3 == 0:
            src[(14, 2 * (X13 + i), 2 * Y13)] = tile({"transportation": [FOOTWAY]})
    mb = _mbtiles(tmp_path / "m.mbtiles", src)
    items = [(z, x, y, gzip.compress(PARENT)) for (z, x, y) in src if z == 13]
    items = [items[i] if i % 2 else (12, 7, 7, b"x%d" % i) for i in range(len(items))] + items
    one, two = {}, {}
    a = list(pf.fold_tiles(mb, items, workers=1, stats=one, batch=17))
    b = list(pf.fold_tiles(mb, items, workers=3, stats=two, batch=17))
    assert a == b and [t[:3] for t in a] == [t[:3] for t in items]
    assert one == two and one["tiles"] == sum(1 for t in a if t[0] == 13 and _paths_in(t[3]))
    assert multiprocessing.active_children() == []
    gen = pf.fold_tiles(mb, items, workers=2, batch=5)
    next(gen)
    gen.close()                                  # stopped early: the pool goes too
    assert multiprocessing.active_children() == []


def test_create_osm_zim_folds_by_default_with_max_zoom_13():
    import create_osm_zim as C
    on = C.fold_paths_on
    assert on(argparse.Namespace(max_zoom=13, fold_paths=None))
    assert not on(argparse.Namespace(max_zoom=13, fold_paths=False))
    assert not on(argparse.Namespace(max_zoom=14, fold_paths=True))
    assert on(argparse.Namespace(max_zoom=13))                     # namespaces without it
    p = C.build_parser() if hasattr(C, "build_parser") else None
    if p is not None:
        assert p.parse_args(["--max-zoom", "13"]).fold_paths is None
        assert p.parse_args(["--max-zoom", "13", "--no-fold-paths"]).fold_paths is False


def test_create_osm_zim_folds_in_memory_tiles(mb, monkeypatch):
    import create_osm_zim as C
    from streetzim import tiles as T
    monkeypatch.setattr(T, "build_cpus", lambda: 1)
    monkeypatch.setattr(C, "generate_sdf_font_glyphs", lambda **_k: {})
    monkeypatch.setattr(C, "fallback_scripts_in_tiles", lambda *_a, **_k: set())
    for fold, want in ((None, 1), (False, 0)):
        _f, _m, tiles, _n, streaming = C._process_tiles(
            args=argparse.Namespace(max_zoom=13, fold_paths=fold), mbtiles_path=mb, total_steps=1)
        assert not streaming and _paths_in(tiles[(13, X13, Y13)]) == want


def test_create_osm_zim_passes_the_fold_to_create_zim(monkeypatch, tmp_path):
    import create_osm_zim as C
    seen = []
    monkeypatch.setattr(C, "create_zim", lambda **kw: seen.append(kw["fold_paths"]))
    common = {"address_count": 0, "bbox_str": None, "fonts": {}, "map_config": {},
              "maplibre_css": "", "maplibre_js": "", "mbtiles_path": None, "name": "T",
              "output_path": str(tmp_path / "o.zim"), "overture_sources": None,
              "overture_themes": None, "routing_graph_path": None, "satellite_dir": None,
              "satellite_format": None, "satellite_max_zoom": None, "search_features": [],
              "terrain_dir": None, "terrain_max_zoom": None, "tile_metadata": {}, "tiles": {},
              "tmpdir": str(tmp_path), "total_tile_count": 0, "use_streaming": False,
              "wiki_cross_refs": None, "wikidata_data": None, "zim_illustration": None,
              "zim_metadata": None}
    for zoom, fold in ((13, None), (13, False), (14, None)):
        C._write_zim(args=argparse.Namespace(max_zoom=zoom, fold_paths=fold, cluster_size=2048,
                                             workers=None, language="en"), **common)
    assert seen == [True, False, False]


def test_the_streetzim_command_passes_no_fold_paths():
    from streetzim import cli
    p = cli.build_parser()
    assert p.parse_args(["--name", "x", "--title", "x", "--description", "x",
                         "--bbox", "1,1,2,2"]).fold_paths is True


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


# ---- round-2 review: children with broken paths, holes, wiring --------------

def test_a_child_whose_paths_cannot_be_read_is_skipped_and_its_sibling_folds():
    # gunzips fine, but its feature's packed tags end in an unterminated varint
    broken = pf._enc_len(3, pf._enc_varint(15 << 3) + pf._enc_varint(2)
                         + pf._enc_len(1, b"transportation")
                         + pf._enc_len(2, pf._enc_len(2, b"\x80"))
                         + pf._enc_len(3, b"class") + pf._enc_len(4, pf.PATH_VALUE))
    good = tile({"transportation": [FOOTWAY]})
    stats: dict = {}
    got = added_features(pf.fold(PARENT, [(0, 0, broken), (1, 0, good)], stats))
    assert len(got) == 1 and stats["bad_children"] == 1


def test_a_bug_in_the_fold_is_not_swallowed(monkeypatch):
    def boom(*_a, **_k):
        raise IndexError("a bug")
    monkeypatch.setattr(pf, "_encode_geometry", boom)
    with pytest.raises(IndexError):
        pf.fold(PARENT, [(0, 0, tile({"transportation": [FOOTWAY]}))])


def test_bad_children_are_counted_across_processes_and_warned_about(tmp_path, capsys):
    from streetzim import tiles as T
    mb = _mbtiles(tmp_path / "bad.mbtiles", {
        (14, 2 * X13, 2 * Y13): b"\x1f\x8b" + b"broken",
        (14, 2 * X13 + 1, 2 * Y13): gzip.compress(tile({"transportation": [FOOTWAY]})),
    }, gz=False)
    for workers in (1, 2):
        stats: dict = {}
        out = list(pf.fold_tiles(mb, [(13, X13, Y13, PARENT)], workers=workers, stats=stats))
        assert stats["bad_children"] == 1 and _paths_in(out[0][3]) == 1
    T._print_fold({"tiles": 1, "features": 1, "bad_children": 1})
    assert "1 z14 tiles would not decode" in capsys.readouterr().out


def test_the_streaming_reader_hands_over_decoded_tiles(mb, monkeypatch):
    from streetzim import tiles as T
    monkeypatch.setattr(T, "build_cpus", lambda: 1)
    got = {(z, x, y): d for z, x, y, d in T.iter_tiles_from_mbtiles(mb, max_zoom=13, fold_paths=True)}
    assert got[(13, X13, Y13)][:2] != b"\x1f\x8b" and _paths_in(got[(13, X13, Y13)]) == 1
    loaded, _m = T.extract_tiles_from_mbtiles(mb, max_zoom=13, fold_paths=True)
    assert loaded[(13, X13, Y13)][:2] == b"\x1f\x8b"


def _ring(*pts: tuple[int, int]) -> list[int]:
    cmds = line(*pts)
    return cmds + [pf._CLOSE_PATH | 1 << 3]


def test_holes_that_survive_are_kept_and_broken_ones_dropped():
    outer = _ring((0, 0), (2000, 0), (2000, 2000), (0, 2000))           # area > 0
    hole = _ring((500, 500), (500, 1500), (1500, 1500), (1500, 500))    # area < 0
    # a hole the halving turns inside out: area -1 (surveyor's formula, y
    # down), halved to (300,300) (302,301) (301,301): area +1
    flip = _ring((600, 600), (603, 602), (602, 601))
    assert pf._ring_area2([(600, 600), (603, 602), (602, 601)]) < 0
    child = raw_tile("transportation", [(3, [("class", "path")], outer + hole + flip)])
    got = added_features(pf.fold(PARENT, [(0, 0, child)]))
    assert got[0]["geometry"]["type"] == "Polygon"
    assert len(got[0]["geometry"]["coordinates"]) == 2      # outer + surviving hole; flip dropped


def test_holes_go_with_an_outer_ring_that_turns_inside_out():
    flip_outer = _ring((0, 0), (2, 1), (3, 2))               # +1, halved to -1
    big_hole = _ring((100, 100), (100, 900), (900, 900), (900, 100))
    child = raw_tile("transportation", [(3, [("class", "path")], flip_outer + big_hole)])
    assert pf.fold(PARENT, [(0, 0, child)]) is PARENT


def test_rings_are_written_without_their_closing_point():
    # an encoder that repeats the first point before ClosePath
    ring = _ring((0, 0), (400, 0), (400, 400), (0, 400), (0, 0))
    child = raw_tile("transportation", [(3, [("class", "path")], ring)])
    layer = pf._Layer(layers_raw(pf.fold(PARENT, [(0, 0, child)]))["transportation"])
    geom = [pf._packed(wt, v) for fno, wt, v, _s, _e in pf._fields(layer.features[-1]) if fno == 4][0]
    assert geom[3] == pf._LINE_TO | 3 << 3                  # 4 corners: MoveTo + 3 LineTo


def test_features_with_one_geometry_but_different_tags_are_both_added():
    a = raw_tile("transportation", [(2, [("class", "path"), ("subclass", "footway")], line((10, 10), (300, 10))),
                                    (2, [("class", "path"), ("subclass", "cycleway")], line((10, 10), (300, 10)))])
    assert len(added_features(pf.fold(PARENT, [(0, 0, a)]))) == 2


def test_the_note_for_fold_paths_with_another_zoom(mb, monkeypatch, capsys):
    import create_osm_zim as C
    monkeypatch.setattr(C, "generate_sdf_font_glyphs", lambda **_k: {})
    monkeypatch.setattr(C, "fallback_scripts_in_tiles", lambda *_a, **_k: set())
    C._process_tiles(args=argparse.Namespace(max_zoom=14, fold_paths=True), mbtiles_path=mb, total_steps=1)
    assert "--fold-paths applies to --max-zoom 13 only" in capsys.readouterr().out


def test_the_streetzim_command_forwards_no_fold_paths(tmp_path):
    from streetzim import cli
    p = cli.build_parser()
    common = ["--name", "x", "--title", "x", "--description", "x", "--bbox", "1,1,2,2",
              "--profile", "basic", "--max-zoom", "13"]
    for extra, want in (([], False), (["--no-fold-paths"], True)):
        argv = cli._builder_argv(p.parse_args(common + extra), "1,1,2,2", None, tmp_path, None)  # pyright: ignore[reportPrivateUsage]
        assert ("--no-fold-paths" in argv) is want

