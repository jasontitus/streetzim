"""streetzim.search_extract works on any OpenMapTiles MBTiles without the
builder (the openzim/maps integration relies on this).

The fixture is a hand-made MBTiles with one z14 tile holding a named POI, a
named street and a place, gzipped like tilemaker and OpenFreeMap tiles.
Extraction runs in a subprocess, as a library user would call it (spawn
workers need an importable __main__), and must not import create_osm_zim.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

mvt = pytest.importorskip("mapbox_vector_tile")


def _make_mbtiles(path: Path, zoom: int = 14) -> None:
    # Tile 14/8529/5973 covers Monaco; MVT coordinates are 0..4096 in tile space.
    tile = mvt.encode([
        {"name": "poi", "features": [
            {"geometry": "POINT(2000 2000)",
             "properties": {"name": "Café de Paris", "class": "cafe", "subclass": "cafe"}},
            # tilemaker's profile has no class for amenity=pharmacy, so it
            # writes the OSM key; shop is a real OpenMapTiles class.
            {"geometry": "POINT(3000 2000)",
             "properties": {"name": "Pharmacie Centrale", "class": "amenity",
                            "subclass": "pharmacy"}},
            {"geometry": "POINT(3000 3000)",
             "properties": {"name": "Boulangerie", "class": "shop",
                            "subclass": "bakery"}}]},
        {"name": "transportation_name", "features": [
            {"geometry": "LINESTRING(100 100, 2000 100, 4000 100)",
             "properties": {"name": "Avenue des Beaux-Arts", "class": "tertiary"}}]},
        {"name": "place", "features": [
            {"geometry": "POINT(1000 3000)",
             "properties": {"name": "Monte-Carlo", "class": "suburb"}}]},
    ])
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
    con.executemany("INSERT INTO metadata VALUES (?, ?)",
                    [("format", "pbf"), ("minzoom", str(zoom)), ("maxzoom", str(zoom))])
    divisor = 1 << (14 - zoom)
    column, y = 8529 // divisor, 5973 // divisor
    tms_row = (1 << zoom) - 1 - y
    con.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)",
                (zoom, column, tms_row, gzip.compress(tile)))
    con.commit()
    con.close()


@pytest.mark.parametrize("zoom", [13, 14])
def test_extracts_records_without_the_builder(tmp_path: Path, zoom: int):
    mbtiles = tmp_path / "fixture.mbtiles"
    _make_mbtiles(mbtiles, zoom=zoom)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    script = tmp_path / "run.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from streetzim.search_extract import extract_searchable_features
        if __name__ == "__main__":
            path = extract_searchable_features(mbtiles_path={str(mbtiles)!r},
                                               output_dir={str(out_dir)!r})
            assert "create_osm_zim" not in sys.modules, "imported the builder"
            print("PATH=" + path)
    """))
    res = subprocess.run([sys.executable, str(script)], capture_output=True,
                         text=True, timeout=300)
    assert res.returncode == 0, res.stderr[-2000:]
    path = [l for l in res.stdout.splitlines() if l.startswith("PATH=")][0][5:]
    recs = [json.loads(l) for l in open(path, encoding="utf-8")]
    by_name = {r["name"]: r for r in recs}
    assert set(by_name) == {"Café de Paris", "Pharmacie Centrale", "Boulangerie",
                            "Avenue des Beaux-Arts", "Monte-Carlo"}
    assert by_name["Café de Paris"]["type"] == "poi"
    assert by_name["Café de Paris"]["subtype"] == "cafe"
    assert by_name["Pharmacie Centrale"]["subtype"] == "pharmacy"
    assert by_name["Boulangerie"]["subtype"] == "shop"
    assert by_name["Avenue des Beaux-Arts"]["subtype"] == "tertiary"
    assert by_name["Avenue des Beaux-Arts"]["type"] == "street"
    assert by_name["Monte-Carlo"]["type"] == "place"
    import mercantile
    divisor = 1 << (14 - zoom)
    b = mercantile.bounds(8529 // divisor, 5973 // divisor, zoom)
    for r in recs:  # every point lands inside the tile it came from
        assert b.west <= r["lon"] <= b.east and b.south <= r["lat"] <= b.north, r
    # The POI sits at the tile centre (2000/4096 of the way across).
    cafe = by_name["Café de Paris"]
    assert abs(cafe["lon"] - (b.west + (b.east - b.west) * 2000 / 4096)) < 1e-4


@pytest.mark.parametrize("output_to_disk", [False, True])
def test_empty_mbtiles_produces_empty_search_feed(tmp_path, output_to_disk):
    from streetzim.search_extract import extract_searchable_features
    path = tmp_path / "empty.mbtiles"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
    result = extract_searchable_features(mbtiles_path=str(path),
                                         output_dir=str(tmp_path) if output_to_disk else None)
    if output_to_disk:
        assert Path(result).read_bytes() == b""
    else:
        assert result == []


def test_failed_worker_cleans_owned_search_partition_scratch(tmp_path, monkeypatch):
    import multiprocessing
    from streetzim.search_extract import extract_searchable_features

    database = tmp_path / "fixture.mbtiles"
    _make_mbtiles(database)
    out = tmp_path / "out"
    out.mkdir()
    foreign = out / "streetzim_search_another_build"
    foreign.mkdir()
    (foreign / "partial.jsonl").write_text("belongs to another build")
    worker_paths = []

    class FailingPool:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def imap_unordered(self, fn, partitions):
            for partition in partitions:
                path = Path(partition[4])
                worker_paths.append(path)
                path.write_text("incomplete worker output")
            raise RuntimeError("search worker failed")

    class Context:
        def Pool(self, *args):
            return FailingPool()

    monkeypatch.setattr(multiprocessing, "get_context", lambda *args: Context())
    with pytest.raises(RuntimeError, match="search worker failed"):
        extract_searchable_features(mbtiles_path=str(database), output_dir=str(out))
    assert worker_paths and all(not path.parent.exists() for path in worker_paths)
    assert (foreign / "partial.jsonl").read_text() == "belongs to another build"
    assert list(out.iterdir()) == [foreign]


def test_builder_reexports_the_same_functions():
    import create_osm_zim as coz
    import streetzim.search_extract as se
    for name in ("extract_searchable_features", "build_location_index",
                 "_finish_features_streaming", "_process_tile_partition"):
        assert getattr(coz, name) is getattr(se, name)


@pytest.mark.parametrize("mode", ["streaming", "in-memory"])
def test_location_workers_support_scratch_without_unix_sockets(tmp_path, mode):
    """Docker bind mounts and long temp paths cannot host forkserver sockets.

    Exercise real location workers with a forkserver default and a scratch
    path longer than AF_UNIX allows, as the Python 3.14 country build does.
    The tile scan is inline to reach the >100,000-record location pass
    without an expensive MVT fixture.
    """
    import multiprocessing
    if "forkserver" not in multiprocessing.get_all_start_methods():
        pytest.skip("forkserver is unavailable on this platform")
    scratch = tmp_path / ("long-scratch-" + "x" * 100)
    scratch.mkdir()
    script = tmp_path / "location_workers.py"
    script.write_text(textwrap.dedent(f"""
        import json
        import multiprocessing
        import os
        import sys
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        sys.path.insert(0, {str(ROOT)!r})
        import streetzim.search_extract as se

        class InlineTilePool:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def imap_unordered(self, fn, tiles, **kwargs):
                yield [{{"name": "Testville", "type": "place", "subtype": "city",
                         "lat": 0.0, "lon": 0.0}}] + [
                    {{"name": f"Cafe {{i}}", "type": "poi", "subtype": "cafe",
                     "lat": 0.001, "lon": 0.001}} for i in range(100_000)]

        if __name__ == "__main__":
            multiprocessing.set_start_method("forkserver", force=True)
            tempfile.tempdir = {str(scratch)!r}
            with patch.object(se, "build_cpus", return_value=2):
                if {mode!r} == "streaming":
                    raw = Path({str(tmp_path)!r}) / "raw.jsonl"
                    rows = [{{"name": "Testville", "type": "place", "subtype": "city",
                             "lat": 0.0, "lon": 0.0}},
                            {{"name": "Cafe", "type": "poi", "subtype": "cafe",
                             "lat": 0.001, "lon": 0.001}}]
                    raw.write_text("".join(json.dumps(r) + "\\n" for r in rows))
                    result = se._finish_features_streaming(str(raw), {str(tmp_path)!r}, 2)
                    expected = 2
                else:
                    context = multiprocessing.get_context("spawn")
                    with patch.object(context, "Pool", InlineTilePool):
                        result = se.extract_searchable_features(
                            tiles={{(14, 0, 0): b"unused"}}, output_dir={str(tmp_path)!r})
                    expected = 100_001
            rows = [json.loads(line) for line in Path(result).read_text().splitlines()]
            assert len(rows) == expected
            assert all(r.get("location") == "Testville" for r in rows if r["type"] == "poi")
            assert se._place_grid is None
    """))
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-4000:]


@pytest.mark.parametrize(("props", "expected"), [
    # OpenMapTiles classes are kept, as before.
    ({"class": "cafe", "subclass": "cafe"}, "cafe"),
    ({"class": "grocery", "subclass": "supermarket"}, "grocery"),
    ({"class": "shop", "subclass": "bakery"}, "shop"),
    ({"class": "railway", "subclass": "station"}, "railway"),
    ({"class": "office", "subclass": "company"}, "office"),
    ({"class": "primary"}, "primary"),
    # tilemaker's raw-key fallback: the subclass is the POI type.
    ({"class": "amenity", "subclass": "fuel"}, "fuel"),
    ({"class": "amenity", "subclass": "pharmacy"}, "pharmacy"),
    ({"class": "amenity", "subclass": "restaurant"}, "restaurant"),
    ({"class": "tourism", "subclass": "museum"}, "museum"),
    ({"class": "historic", "subclass": "monument"}, "monument"),
    ({"class": "leisure", "subclass": "playground"}, "playground"),
    # Nothing better to use: the key, the subclass alone, or nothing.
    ({"class": "amenity", "subclass": ""}, "amenity"),
    ({"class": "amenity"}, "amenity"),
    ({"subclass": "pharmacy"}, "pharmacy"),
    ({}, ""),
])
def test_feature_subtype(props, expected):
    from streetzim.search_extract import feature_subtype
    assert feature_subtype(props) == expected


def test_in_memory_worker_uses_the_subclass_for_raw_keys():
    from streetzim.search_extract import _process_tile_for_search
    tile = mvt.encode([{"name": "poi", "features": [
        {"geometry": "POINT(2000 2000)",
         "properties": {"name": "Esso", "class": "amenity", "subclass": "fuel"}}]}])
    recs = _process_tile_for_search((14, 8529, 5973, gzip.compress(tile), {"poi": "poi"}))
    assert [(r["name"], r["subtype"]) for r in recs] == [("Esso", "fuel")]


def test_raw_key_list_matches_the_tilemaker_profile():
    """Every key GetPOIRank can fall back to as a class (the keys of
    poiTags, plus the shop catch-all) is either in RAW_OSM_KEY_CLASSES or a
    real OpenMapTiles class Planetiler also writes, which must stay as is so
    OpenFreeMap builds do not change."""
    import re

    from streetzim.search_extract import RAW_OSM_KEY_CLASSES
    src = (ROOT / "resources/tilemaker/process-openmaptiles.lua").read_text(encoding="utf-8")
    # Without comments: --[[ ... ]] / --[==[ ... ]==] blocks, then -- lines.
    lua = re.sub(r"--\[(=*)\[.*?\]\1\]", "", src, flags=re.S)
    lua = re.sub(r"--[^\n]*", "", lua)
    reshape = ("the tilemaker profile's poiTags / GetPOIRank changed shape; "
               "re-check RAW_OSM_KEY_CLASSES in streetzim/search_extract.py "
               "and update this test")
    assert re.search(r"class\s*=\s*poiClasses\[v\]\s+or\s+k\b", lua), (
        f"{reshape}: GetPOIRank's `class = poiClasses[v] or k` fallback is gone")
    defs = list(re.finditer(r"^[ \t]*(?:local\s+)?poiTags\s*=\s*\{", lua, re.M))
    assert len(defs) == 1, f"{reshape}: poiTags is defined {len(defs)} times"
    start = defs[0].start()
    end = lua.find("poiClasses", start)
    assert end > start, f"{reshape}: poiClasses no longer follows poiTags"
    block = lua[start:end]
    # Keys as `amenity = Set {` or `["amenity"] = Set {`.
    keys = {a or b for a, b in re.findall(
        r"""(?:\b(\w+)|\[\s*["'](\w+)["']\s*\])\s*=\s*Set\s*\{""", block)}
    keys.discard("poiTags")
    assert "amenity" in keys and "tourism" in keys, (
        f"{reshape}: read poiTags keys {sorted(keys)}")
    # No key added to poiTags anywhere else (poiTags.x = / poiTags["x"] =),
    # which the block above would miss: the table is defined once and
    # otherwise only iterated.
    later = re.findall(r"poiTags\s*(?:\.\s*\w+|\[[^\]]*\])\s*=", lua)
    assert not later, f"{reshape}: poiTags is assigned to later: {later}"
    uses = len(re.findall(r"\bpoiTags\b", lua))
    iterations = len(re.findall(r"pairs\(\s*poiTags\s*\)", lua))
    assert uses == 1 + iterations, (
        f"{reshape}: poiTags is used other than defined once and iterated")
    # Real OpenMapTiles classes: Planetiler writes them too (OpenFreeMap's
    # Monaco tiles have shop, railway and office POIs), so they stay as the
    # subtype. The profile has no office key today; office is listed so
    # adding one would not turn it into a raw key and change OpenFreeMap.
    omt_classes = {"shop", "railway", "aerialway", "office"}
    assert keys - omt_classes == RAW_OSM_KEY_CLASSES, (
        f"{reshape}: poiTags keys that are not OpenMapTiles classes are "
        f"{sorted(keys - omt_classes)}, RAW_OSM_KEY_CLASSES is "
        f"{sorted(RAW_OSM_KEY_CLASSES)}")
    assert not RAW_OSM_KEY_CLASSES & omt_classes


def test_tilemaker_fuel_and_pharmacy_reach_their_chips():
    from cloud.chip_rules import CHIP_RULES, record_matches_chip
    from streetzim.search_extract import feature_subtype
    chips = {c.id: c for c in CHIP_RULES}
    for sub, chip in (("fuel", "fuel"), ("pharmacy", "health"), ("restaurant", "food")):
        rec = {"t": "poi", "n": "x", "s": feature_subtype({"class": "amenity", "subclass": sub})}
        assert record_matches_chip(rec, chips[chip]), (sub, chip)


def test_search_record_keeps_the_raw_key_internally():
    from streetzim.search_extract import search_record
    tm = search_record("Esso", "poi", {"class": "amenity", "subclass": "fuel"}, 43.7, 7.4)
    assert tm == {"name": "Esso", "type": "poi", "subtype": "fuel",
                  "osm_key": "amenity", "lat": 43.7, "lon": 7.4}
    # OpenFreeMap's classes, and tilemaker's real OpenMapTiles ones, add nothing.
    for props in ({"class": "fuel", "subclass": "fuel"},
                  {"class": "shop", "subclass": "bakery"},
                  {"class": "amenity"}, {}):
        assert "osm_key" not in search_record("x", "poi", props, 0, 0), props


@pytest.mark.parametrize("failure", [False, True])
def test_small_in_memory_extraction_releases_location_grid(monkeypatch, failure):
    import multiprocessing
    import streetzim.search_extract as se

    class InlinePool:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def imap_unordered(self, fn, iterable, **kwargs):
            return map(fn, iterable)

    class Context:
        def Pool(self, *args):
            return InlinePool()

    monkeypatch.setattr(multiprocessing, "get_context", lambda *args: Context())
    tile = mvt.encode([{"name": "place", "features": [
        {"geometry": "POINT(1000 3000)", "properties": {"name": "Monte-Carlo"}},
    ]}])
    if failure:
        def fail(features):
            assert se._place_grid
            raise RuntimeError("location assignment failed")
        monkeypatch.setattr(se, "_assign_location_batch", fail)
        with pytest.raises(RuntimeError, match="location assignment failed"):
            se.extract_searchable_features(tiles={(14, 8529, 5973): tile})
    else:
        rows = se.extract_searchable_features(tiles={(14, 8529, 5973): tile})
        assert len(rows) == 1 and rows[0]["name"] == "Monte-Carlo"
    assert se._place_grid is None


# ---- MultiPoints: one result per point ----------------------------------------
# tilemaker 3.1 and later (the image's 3.2, the world tiles') merge points with
# identical attributes into one MultiPoint: four "Bureau Identification"
# offices in a tile are one feature, and used to become one result at their
# average, between the four.

def _multipoint_tile(layer="poi", cls="amenity"):
    return mvt.encode([{"name": layer, "features": [
        {"geometry": "MULTIPOINT(500 500, 3500 500, 500 3500, 3500 3500)",
         "properties": {"name": "Bureau Identification", "class": cls}}]}])


@pytest.mark.parametrize("layer, cls", [("poi", "amenity"), ("place", "village")])
def test_each_point_of_a_multipoint_is_a_result(layer, cls):
    from streetzim.search_extract import _process_tile_for_search, tile_to_lnglat
    tile = gzip.compress(_multipoint_tile(layer, cls))
    recs = _process_tile_for_search((14, 8529, 5973, tile, {layer: layer}))
    assert [r["name"] for r in recs] == ["Bureau Identification"] * 4
    # mvt.encode flips y (y_coord_down=False); the worker reads y down.
    corners = {(round(lat, 6), round(lon, 6)) for lon, lat in
               (tile_to_lnglat(14, 8529, 5973, x, 4096 - y, 4096)
                for x in (500, 3500) for y in (500, 3500))}
    assert {(r["lat"], r["lon"]) for r in recs} == corners


def test_the_partition_worker_keeps_each_point_too(tmp_path):
    from streetzim.search_extract import _process_tile_partition
    path = tmp_path / "t.mbtiles"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
    con.execute("INSERT INTO tiles VALUES (14, 8529, ?, ?)",
                ((1 << 14) - 1 - 5973, gzip.compress(_multipoint_tile())))
    con.commit()
    con.close()
    out = tmp_path / "out.jsonl"
    _, _, n = _process_tile_partition((str(path), 8529, 8530, {"poi": "poi"}, str(out), 14))
    recs = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert n == 4 and len({(r["lat"], r["lon"]) for r in recs}) == 4


@pytest.mark.parametrize("geom, coords, points", [
    ("Point", [5, 6], [(5, 6)]),
    ("MultiPoint", [[1, 2], [3, 4]], [(1, 2), (3, 4)]),
    ("LineString", [[0, 0], [2, 2], [4, 4]], [(2, 2)]),
    ("MultiLineString", [[[0, 0], [1, 1]], [[5, 5], [6, 6], [7, 7]]], [(6, 6)]),
    ("Polygon", [[[0, 0], [4, 0], [4, 4], [0, 4]]], [(2, 2)]),
    ("MultiPolygon", [[[[0, 0], [2, 0], [2, 2], [0, 2]]]], [(1, 1)]),
    ("MultiPoint", [[1], [3, 4]], []),
    ("GeometryCollection", [[1, 2]], []),
])
def test_feature_points(geom, coords, points):
    from streetzim.search_extract import feature_points
    assert feature_points(geom, coords) == points


def test_the_location_fallback_keeps_each_city_of_a_multipoint(tmp_path, monkeypatch):
    """Without reverse_geocoder, results are labelled with the nearest place
    of the tiles' place layer, which skipped MultiPoints: a town sharing its
    name and rank with another in the same tile was not a candidate at all."""
    from streetzim import search_extract as E
    monkeypatch.setitem(sys.modules, "reverse_geocoder", None)
    tile = mvt.encode([{"name": "place", "features": [
        {"geometry": "MULTIPOINT(500 500, 3500 3500)",
         "properties": {"name:latin": "Springfield", "class": "city"}},
        {"geometry": "POINT(2600 2600)",
         "properties": {"name:latin": "Shelbyville", "class": "city"}}]}])
    path = tmp_path / "t.mbtiles"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
    con.execute("INSERT INTO tiles VALUES (8, 133, ?, ?)", ((1 << 8) - 1 - 93, gzip.compress(tile)))
    con.commit()
    con.close()
    lookup = E.build_location_index(str(path))
    assert lookup is not None

    def at(x, y):                   # near a point as written (mvt.encode flips y)
        lon, lat = E.tile_to_lnglat(8, 133, 93, x, 4096 - y, 4096)
        return lookup(lat, lon)
    assert "Springfield" in at(3400, 3400) and "Springfield" in at(600, 600)
    assert "Shelbyville" in at(2600, 2600)
