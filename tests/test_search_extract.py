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


def _make_mbtiles(path: Path) -> None:
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
                    [("format", "pbf"), ("minzoom", "14"), ("maxzoom", "14")])
    tms_row = (1 << 14) - 1 - 5973
    con.execute("INSERT INTO tiles VALUES (14, 8529, ?, ?)", (tms_row, gzip.compress(tile)))
    con.commit()
    con.close()


def test_extracts_records_without_the_builder(tmp_path: Path):
    mbtiles = tmp_path / "fixture.mbtiles"
    _make_mbtiles(mbtiles)
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
    b = mercantile.bounds(8529, 5973, 14)
    for r in recs:  # every point lands inside the tile it came from
        assert b.west <= r["lon"] <= b.east and b.south <= r["lat"] <= b.north, r
    # The POI sits at the tile centre (2000/4096 of the way across).
    cafe = by_name["Café de Paris"]
    assert abs(cafe["lon"] - (b.west + (b.east - b.west) * 2000 / 4096)) < 1e-4


def test_builder_reexports_the_same_functions():
    import create_osm_zim as coz
    import streetzim.search_extract as se
    for name in ("extract_searchable_features", "build_location_index",
                 "_finish_features_streaming", "_process_tile_partition"):
        assert getattr(coz, name) is getattr(se, name)


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
    lua = (ROOT / "resources/tilemaker/process-openmaptiles.lua").read_text(encoding="utf-8")
    assert "class = poiClasses[v] or k" in lua
    defs = list(re.finditer(r"^poiTags\s*=\s*\{", lua, re.M))
    assert len(defs) == 1
    start = defs[0].start()
    block = lua[start:lua.index("poiClasses", start)]
    # Keys as `amenity = Set {` or `["amenity"] = Set {`.
    keys = set(re.findall(
        r"""(?:\b(\w+)|\[\s*["'](\w+)["']\s*\])\s*=\s*Set\s*\{""", block))
    keys = {a or b for a, b in keys}
    assert "amenity" in keys and "tourism" in keys
    # No key added to poiTags anywhere else (poiTags.x = / poiTags["x"] =),
    # which the block above would miss: the table is defined once and
    # otherwise only iterated.
    assert not re.search(r"poiTags\s*(?:\.\s*\w+|\[[^\]]*\])\s*=", lua)
    assert len(re.findall(r"\bpoiTags\b", lua)) == 1 + len(
        re.findall(r"pairs\(\s*poiTags\s*\)", lua))
    # Real OpenMapTiles classes: Planetiler writes them too (OpenFreeMap's
    # Monaco tiles have shop, railway and office POIs), so they stay as the
    # subtype. The profile has no office key today; office is listed so
    # adding one would not turn it into a raw key and change OpenFreeMap.
    omt_classes = {"shop", "railway", "aerialway", "office"}
    assert keys - omt_classes == RAW_OSM_KEY_CLASSES
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
