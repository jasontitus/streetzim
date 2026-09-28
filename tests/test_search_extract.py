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
             "properties": {"name": "Café de Paris", "class": "cafe", "subclass": "cafe"}}]},
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
    assert set(by_name) == {"Café de Paris", "Avenue des Beaux-Arts", "Monte-Carlo"}
    assert by_name["Café de Paris"]["type"] == "poi"
    assert by_name["Café de Paris"]["subtype"] == "cafe"
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
