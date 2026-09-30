"""Areas across the antimeridian (Fiji, Chukotka, Kiribati).

The area is one unwrapped box, east past 180 (streetzim/area.py). What
must hold: the box splits into its two sides for the tools that only take
[-180, 180]; tiles are selected on both sides, each once and in order; the
map's bounds and centre are ones MapLibre accepts; and a box that does not
cross goes through every one of these unchanged (byte-identical builds).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim import area  # noqa: E402
from streetzim.common import parse_bbox  # noqa: E402

FIJI = (172.8, -23.2, 183.5, -11.2)
MONACO = [7.40, 43.72, 7.44, 43.76]


def test_normalize_both_spellings_and_leaves_others_alone():
    assert area.normalize((172.8, -23.2, -176.5, -11.2)) == pytest.approx(FIJI)
    assert area.normalize(FIJI) == FIJI
    assert area.normalize((532.8, -23.2, 543.5, -11.2)) == pytest.approx(FIJI)
    assert area.normalize(MONACO) == tuple(MONACO)
    assert area.normalize((-180, -90, 180, 90)) == (-180, -90, 180, 90)
    assert area.normalize((10, 0, 10 + 360, 1)) == (-180, 0, 180, 1)
    for bad in ((1, 2, 3), (0, 5, 1, 5), (0, 0, 0, 1), (0, -91, 1, 1)):
        with pytest.raises(ValueError):
            area.normalize(bad)


def test_parse_bbox_unwraps_only_a_crossing_box():
    assert parse_bbox("7.40,43.72,7.44,43.76") == MONACO
    assert parse_bbox("-180,-85,180,85") == [-180, -85, 180, 85]
    assert parse_bbox("172.8,-23.2,-176.5,-11.2") == pytest.approx(list(FIJI))
    assert parse_bbox("172.8,-23.2,183.5,-11.2") == pytest.approx(list(FIJI))


def test_split_contains_and_wrap():
    assert area.split(MONACO) == [tuple(MONACO)]
    west, east = area.split(FIJI)
    assert west == (172.8, -23.2, 180.0, -11.2)
    assert east == pytest.approx((-180.0, -23.2, -176.5, -11.2))
    assert area.crosses(FIJI) and not area.crosses(MONACO)
    assert area.contains(FIJI, -179.9, -16.8) and area.contains(FIJI, 178.4, -18.1)
    assert not area.contains(FIJI, -170.0, -16.0) and not area.contains(FIJI, 170.0, -16.0)
    # A non-crossing box: exactly the old test, even at -180.
    assert not area.contains_lon((170.0, 0, 180.0, 1), -180.0)
    assert area.unwrap_lon(FIJI, -179.0) == 181.0 and area.unwrap_lon(FIJI, 179.0) == 179.0
    assert area.unwrap_lon(MONACO, -179.0) == -179.0
    assert area.wrap_lon(181.0) == -179.0 and area.wrap_lon(180.0) == 180.0


def test_osmium_args_unchanged_for_one_box_and_a_poly_across(tmp_path):
    assert area.osmium_extract_args(MONACO, str(tmp_path)) == ["-b", "7.4,43.72,7.44,43.76"]
    assert area.osmium_extract_args(MONACO, str(tmp_path), bbox_arg="7.40,43.72,7.44,43.76",
                                    flag="--bbox") == ["--bbox", "7.40,43.72,7.44,43.76"]
    assert not list(tmp_path.iterdir())
    flag, path = area.osmium_extract_args(FIJI, str(tmp_path))
    assert flag == "-p"
    text = Path(path).read_text()
    rings = text.split("END")
    assert text.startswith("area\n1\n") and text.count("END") == 3
    assert "180.0000000" in rings[0] and "-180.0000000" in rings[1]
    assert "-176.5000000" in rings[1]


def _mbtiles(path: Path, tiles):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, "
                "tile_data BLOB)")
    for z, x, y in tiles:
        con.execute("INSERT INTO tiles VALUES (?,?,?,?)",
                    (z, x, (1 << z) - 1 - y, f"{z}/{x}/{y}".encode()))
    con.commit()
    con.close()


def test_tiles_on_both_sides_of_180_once_each_in_order(tmp_path):
    from streetzim.tiles import estimate_tile_total, iter_tiles_from_mbtiles
    import mercantile
    db = tmp_path / "t.mbtiles"
    both = [(0, 0, 0), (1, 0, 1), (1, 1, 1), (3, 0, 4), (3, 7, 4), (3, 3, 4)]
    _mbtiles(db, both)
    got = [(z, x, y) for z, x, y, _ in iter_tiles_from_mbtiles(db, bbox=FIJI, max_zoom=3)]
    # z3 column 3 is Africa: outside. Column 0 (179W) and 7 (172E) are in.
    assert got == [(0, 0, 0), (1, 0, 1), (1, 1, 1), (3, 0, 4), (3, 7, 4)]
    # The estimate counts the same rectangles: each side, z0 once.
    want = 0
    for z in range(0, 4):
        cols = set()
        rows = set()
        for part in area.split(FIJI):
            for t in mercantile.tiles(*part, zooms=z):
                cols.add(t.x)
                rows.add(t.y)
        want += len(cols) * len(rows)
    assert estimate_tile_total(db, bbox=FIJI, max_zoom=3) == want
    # A box on one side selects that side only, as before.
    got = [(z, x, y) for z, x, y, _ in
           iter_tiles_from_mbtiles(db, bbox=(172.8, -23.2, 180.0, -11.2), max_zoom=3)]
    assert got == [(0, 0, 0), (1, 1, 1), (3, 7, 4)]


def test_tilemaker_runs_once_per_side_and_merges(tmp_path, monkeypatch):
    from streetzim import tiles
    runs = []
    monkeypatch.setattr(tiles.subprocess, "run", lambda cmd, check: runs.append(cmd))
    monkeypatch.setattr(tiles.os, "remove", lambda p: None)
    monkeypatch.setattr(tiles.os.path, "getsize", lambda p: 0)
    monkeypatch.setattr(tiles, "required_shapefiles", lambda: [])
    out = str(tmp_path / "t.mbtiles")
    tiles.generate_tiles("in.pbf", out, bbox="172.8,-23.2,-176.5,-11.2")
    osm = [c for c in runs if c[0] == "osmium"]
    tm = [c for c in runs if c[0] == "tilemaker"]
    assert len(osm) == 2 and len(tm) == 2
    assert tm[0][tm[0].index("--bbox") + 1] == "172.800000,-23.200000,180.000000,-11.200000"
    assert tm[1][tm[1].index("--bbox") + 1] == "-180.000000,-23.200000,-176.500000,-11.200000"
    assert "--merge" not in tm[0] and "--merge" in tm[1]
    assert tm[0][tm[0].index("--input") + 1] == osm[0][osm[0].index("-o") + 1]
    # One box: the one command it always was.
    runs.clear()
    monkeypatch.setattr(tiles, "build_cpus", lambda: 3)
    tiles.generate_tiles("in.pbf", out, bbox="7.40,43.72,7.44,43.76")
    assert runs == [["tilemaker", "--input", "in.pbf", "--output", out,
                     "--config", str(tiles.TILEMAKER_CONFIG),
                     "--process", str(tiles.TILEMAKER_PROCESS), "--skip-integrity",
                     "--threads", "3", "--bbox", "7.40,43.72,7.44,43.76"]]
    runs.clear()
    tiles.extract_bbox_from_pbf("in.pbf", "7.40,43.72,7.44,43.76", out)
    assert runs[0][:4] == ["osmium", "extract", "--bbox", "7.40,43.72,7.44,43.76"]
    runs.clear()
    tiles.extract_bbox_from_pbf("in.pbf", "172.8,-23.2,-176.5,-11.2", out)
    assert runs[0][2] == "-p" and runs[0][3].endswith(".poly")


def test_map_centre_and_bounds_across_the_antimeridian(tmp_path):
    import create_osm_zim as c
    centre, zoom = c.get_center_and_zoom(list(FIJI))
    assert centre[0] == pytest.approx(178.15) and zoom == 6
    centre, _ = c.get_center_and_zoom([175.0, -20.0, 187.0, -10.0])
    assert centre[0] == pytest.approx(-179.0)
    assert c.get_center_and_zoom(MONACO) == ([(7.40 + 7.44) / 2, (43.72 + 43.76) / 2], 13)
    # The place median is taken in the box's frame: two thirds of the
    # places are east of 180, so the median is too (not in the Atlantic).
    feats = tmp_path / "f.jsonl"
    with open(feats, "w") as f:
        for i in range(100):
            lon = -179.5 if i % 3 else 178.0
            f.write(json.dumps({"lat": -16.5, "lon": lon}) + "\n")
        f.write(json.dumps({"lat": -16.5, "lon": 0.0}) + "\n")     # outside
    assert c.center_from_places(str(feats), list(FIJI)) == [-179.5, -16.5]


def test_validator_counts_both_sides():
    sys.path.insert(0, str(ROOT / "cloud"))
    import validate_zim as v
    assert v._expected_tile_count((172.8, -23.2, 183.5, -11.2), 0) == 1
    west = v._expected_tile_count((172.8, -23.2, 180.0, -11.2), 10)
    east = v._expected_tile_count((-180.0, -23.2, -176.5, -11.2), 10)
    assert v._expected_tile_count(FIJI, 10) == west + east
    assert v._expected_tile_count(MONACO, 12) == v._expected_tile_count(tuple(MONACO), 12)


def _graph_counts(path):
    import struct
    with open(path, "rb") as f:
        assert f.read(4) == b"SZRG"
        _, nodes, edges = struct.unpack("<3I", f.read(12))
    return nodes, edges


def test_routing_joins_a_road_split_at_the_antimeridian(tmp_path):
    """OSM ends a road on a node at 180 and continues it from a different
    node at -180. Across the antimeridian those become one graph vertex;
    anywhere else the graph is built as before."""
    import shutil
    if not shutil.which("osmium"):
        pytest.skip("osmium CLI not on PATH")
    pytest.importorskip("osmium")
    from streetzim.routing.build import extract_routing_graph
    opl = tmp_path / "road.opl"
    opl.write_text(
        "n1 v1 x179.99 y-16.8\n"
        "n2 v1 x180.0 y-16.8\n"
        "n3 v1 x-180.0 y-16.8\n"
        "n4 v1 x-179.99 y-16.8\n"
        "w10 v1 Thighway=primary Nn1,n2\n"
        "w11 v1 Thighway=primary Nn3,n4\n")
    across = tmp_path / "across"
    across.mkdir()
    g = extract_routing_graph(str(opl), str(across), bbox=list(area.normalize(
        (179.9, -17.0, -179.9, -16.5))))
    assert _graph_counts(g) == (3, 4)          # n2 and n3 are one vertex
    alone = tmp_path / "alone"
    alone.mkdir()
    g = extract_routing_graph(str(opl), str(alone))
    assert _graph_counts(g) == (4, 4)          # no area across: unchanged
