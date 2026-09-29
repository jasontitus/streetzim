"""--mbtiles-url: the Zimfarm flag, the resumable download and its checks,
and the cut of a larger MBTiles to the area (streetzim/mbtiles.py)."""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import cli, mbtiles  # noqa: E402
from tests.mbtiles_fixture import make_mbtiles, read_tiles  # noqa: E402

DEF = json.loads((ROOT / "offliner-definition.json").read_text())
MONACO = (7.40, 43.72, 7.44, 43.76)
FIJI = (172.8, -23.2, -176.5, -11.2)          # across the antimeridian


# ------------------------------------------------ maps2zim's rule, verbatim


def m2z_tile_to_bbox(z, x, y):
    n = 2.0**z
    lon_min = (x / n) * 360.0 - 180.0
    lon_max = ((x + 1) / n) * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (lon_min, lat_min, lon_max, lat_max)


def m2z_intersects(bbox, z, x, y):
    """maps2zim TileFilter.tile_intersects; its box across the antimeridian
    has min_lon > max_lon."""
    west, south, east, north = m2z_tile_to_bbox(z, x, y)
    min_lon, min_lat, max_lon, max_lat = bbox
    if north < min_lat or south > max_lat:
        return False
    if min_lon > max_lon:
        return east >= min_lon or west <= max_lon
    return not (east < min_lon or west > max_lon)


def m2z_box(bbox):
    w, s, e, n = mbtiles.area.normalize(bbox)
    return (w, s, e - 360.0 if e > 180 else e, n)


def selected(bbox, z):
    return {(x, y) for c0, c1, r0, r1 in mbtiles.tile_ranges(bbox, z)
            for x in range(c0, c1 + 1) for y in range(r0, r1 + 1)}


BOXES = [MONACO, FIJI, (0.0, 0.0, 90.0, 45.0),          # on tile edges
         (-180.0, -90.0, 180.0, 90.0), (179.9, 60.0, -179.9, 70.0),
         (-10.0, 85.2, 10.0, 89.0), (-73.7, 40.5, -73.6, 40.6),
         # latitude edges exactly on tile edges (the equator; z4 rows 1 and 3)
         (-90.0, -85.0511287798066, 90.0, 0.0),
         (-157.5, 74.01954331150226, -112.5, 82.67628497834903)]


@pytest.mark.parametrize("bbox", BOXES)
def test_tile_ranges_are_maps2zims_rule(bbox):
    box = m2z_box(bbox)
    for z in range(0, 8):                     # every tile of the world
        n = 1 << z
        want = {(x, y) for x in range(n) for y in range(n) if m2z_intersects(box, z, x, y)}
        assert selected(bbox, z) == want, z
    for z in range(8, 15):                    # the edges: a margin round each block
        n = 1 << z
        ranges = mbtiles.tile_ranges(bbox, z)
        cand_x = {(x + d) % n for c0, c1, _, _ in ranges for x in (c0, c1)
                  for d in range(-3, 4)}
        cand_y = {y + d for _, _, r0, r1 in ranges for y in (r0, r1)
                  for d in range(-3, 4) if 0 <= y + d < n}
        if not ranges:                         # nothing selected: check the poles
            cand_x, cand_y = {0, n // 2, n - 1}, {0, 1, n - 2, n - 1}
        for x in cand_x:
            for y in cand_y:
                assert mbtiles.tile_touches(bbox, z, x, y) == m2z_intersects(box, z, x, y), \
                    (z, x, y)


def test_tile_ranges_by_hand():
    assert mbtiles.tile_ranges(MONACO, 0) == [(0, 0, 0, 0)]
    # Fiji at z1: both western... and eastern columns, the southern row.
    assert mbtiles.tile_ranges(FIJI, 1) == [(0, 1, 1, 1)]
    # z3: columns 7 (to 180) and 0 (from -180), not joined.
    assert mbtiles.tile_ranges(FIJI, 3) == [(0, 0, 4, 4), (7, 7, 4, 4)]
    assert mbtiles.tile_ranges((-10.0, 85.2, 10.0, 89.0), 3) == []   # north of Mercator


# ------------------------------------------------ the cut, on a fixture file


def _window(bbox, z, margin):
    """The tiles around bbox's selection at z, `margin` tiles each way."""
    n = 1 << z
    out = set()
    for c0, c1, r0, r1 in mbtiles.tile_ranges(bbox, z):
        for x in range(c0 - margin, c1 + margin + 1):
            for y in range(max(r0 - margin, 0), min(r1 + margin, n - 1) + 1):
                out.add((z, x % n, y))
    return out


@pytest.mark.parametrize("ofm", [True, False])
@pytest.mark.parametrize("bbox", [MONACO, FIJI])
def test_cut_keeps_exactly_the_touching_tiles(tmp_path, bbox, ofm):
    world = set()
    for z in range(0, 15):
        world |= _window(bbox, z, 3)
    src = make_mbtiles(tmp_path / "big.mbtiles", world, ofm=ofm)
    counts = mbtiles.cut(src, tmp_path / "cut.mbtiles", bbox)
    got = read_tiles(tmp_path / "cut.mbtiles")
    box = m2z_box(bbox)
    for z in range(0, 15):
        want = {t for t in world if t[0] == z and m2z_intersects(box, *t)}
        assert {t for t in got if t[0] == z} == want, z
        assert counts[z] == len(want)
    assert len(got) < len(world)
    if bbox == FIJI:                          # both sides of the antimeridian
        assert {x for z, x, _ in got if z == 14} >= {0, (1 << 14) - 1}
    # Tile data and metadata come across unchanged.
    conn = sqlite3.connect(str(tmp_path / "cut.mbtiles"))
    for z, x, r, data in conn.execute("SELECT * FROM tiles"):
        assert data == f"{z}/{x}/{(1 << z) - 1 - r}".encode()
    assert dict(conn.execute("SELECT * FROM metadata"))["name"] == "OpenFreeMap"
    conn.close()


def test_cut_is_an_index_search(tmp_path):
    for ofm in (True, False):
        src = make_mbtiles(tmp_path / f"{ofm}.mbtiles", [(0, 0, 0)], ofm=ofm)
        plan = mbtiles.query_plan(src)
        assert "tile_column=? AND tile_row>? AND tile_row<?" in plan[0], plan
        assert plan[0].startswith("SEARCH src."), plan
        # Only the cut's own new table is ever scanned, never the source.
        assert not [p for p in plan if p.startswith("SCAN")
                    and not p.startswith(("SCAN main.", "SCAN s"))], plan
        if ofm:
            assert "SEARCH d USING INTEGER PRIMARY KEY (rowid=?)" in plan, plan


def test_no_area_keeps_every_tile(tmp_path):
    tiles = {(z, x, y) for z in range(0, 4) for x in range(1 << z) for y in range(1 << z)}
    src = make_mbtiles(tmp_path / "all.mbtiles", tiles)
    path, meta = cli.prepare_mbtiles(src, None, tmp_path / "work", None)
    assert path == src and meta["name"] == "OpenFreeMap"
    assert not (tmp_path / "work").exists()
    # and the builder, given no area, loads all of them, as before
    from streetzim.tiles import extract_tiles_from_mbtiles
    loaded, _ = extract_tiles_from_mbtiles(src)
    assert set(loaded) == tiles


def test_prepare_always_cuts_to_z14(tmp_path, capsys):
    world = set()
    for z in range(0, 15):
        world |= _window(MONACO, z, 2)
    # bounds that claim the file is inside the area: not trusted
    src = make_mbtiles(tmp_path / "m.mbtiles", world,
                       meta={"name": "OpenFreeMap", "bounds": "7.41,43.73,7.43,43.75"})
    path, _ = cli.prepare_mbtiles(src, MONACO, tmp_path / "work", 12)
    assert path == tmp_path / "work" / "area.mbtiles"
    got = read_tiles(path)
    assert max(z for z, _, _ in got) == 14          # --max-zoom caps the ZIM, not search
    assert len(got) < len(world)
    out = capsys.readouterr().out
    assert "MBTiles: OpenFreeMap" in out and "Cut to the area" in out


def test_cut_keeps_openfreemaps_deduplication(tmp_path):
    # Every tile the same blob, as OpenFreeMap stores its ocean tiles.
    tiles = set()
    for z in range(0, 15):
        tiles |= _window(MONACO, z, 2)
    src = make_mbtiles(tmp_path / "ofm.mbtiles", tiles)
    conn = sqlite3.connect(str(src))
    conn.execute("UPDATE tiles_shallow SET tile_data_id = 0")
    conn.commit()
    conn.close()
    counts = mbtiles.cut(src, tmp_path / "cut.mbtiles", MONACO)
    conn = sqlite3.connect(str(tmp_path / "cut.mbtiles"))
    assert conn.execute("SELECT count(*) FROM tiles").fetchone()[0] == sum(counts.values())
    assert conn.execute("SELECT count(*) FROM tiles_data").fetchone()[0] == 1
    conn.close()


def test_cut_leaves_nothing_when_it_fails(tmp_path, monkeypatch):
    src = make_mbtiles(tmp_path / "s.mbtiles", _window(MONACO, 14, 1))

    def boom(*a, **k):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(mbtiles, "column_queries", boom)
    with pytest.raises(sqlite3.OperationalError):
        mbtiles.cut(src, tmp_path / "cut.mbtiles", MONACO)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.mbtiles"]


def test_cut_reads_utf16_files(tmp_path):
    # tilemaker writes UTF-16 MBTiles; SQLite attaches only same-encoding files.
    p = tmp_path / "u16.mbtiles"
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA encoding = 'UTF-16le'")
    conn.execute("CREATE TABLE metadata (name text, value text)")
    conn.execute("INSERT INTO metadata VALUES ('name', 'tm')")
    conn.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, "
                 "tile_row integer, tile_data blob)")
    conn.execute("INSERT INTO tiles VALUES (0, 0, 0, x'00')")
    conn.commit()
    conn.close()
    assert mbtiles.cut(p, tmp_path / "c.mbtiles", MONACO)[0] == 1


def test_flag_offered_to_zimfarm_as_a_url():
    f = DEF["flags"]["mbtiles_url"]
    assert f["type"] == "url" and f["required"] is False
    assert "mbtiles" not in DEF["flags"]            # the local path stays CLI-only


def test_source_record_and_credit():
    meta = {"name": "OpenFreeMap", "description": "https://openfreemap.org",
            "version": "3.16.0", "planetiler:version": "0.10.3",
            "planetiler:osm:osmosisreplicationtime": "2026-09-27T20:23:36Z"}
    assert mbtiles.source_record(meta, "https://x/t.mbtiles") == {
        "name": "OpenFreeMap", "version": "3.16.0", "osmDate": "2026-09-27",
        "generator": "planetiler 0.10.3", "homepage": "https://openfreemap.org",
        "url": "https://x/t.mbtiles"}
    assert "url" not in mbtiles.source_record(meta)
    assert mbtiles.license_text(meta) == "Vector tiles: OpenFreeMap (https://openfreemap.org)"
    assert "OSM data 2026-09-27" in mbtiles.describe(meta)


@pytest.mark.parametrize("tables", [["t"], ["tiles"], ["metadata"]])
def test_check_needs_both_tables(tmp_path, tables):
    p = tmp_path / "other.sqlite"
    conn = sqlite3.connect(str(p))
    for t in tables:
        conn.execute(f"CREATE TABLE {t} (name, value)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="no tiles and metadata"):
        mbtiles.check(p)


def test_check_needs_the_sqlite_header(tmp_path):
    p = tmp_path / "x.mbtiles"
    p.write_bytes(b"PK\x03\x04" + b"\0" * 200)
    with pytest.raises(ValueError, match="not SQLite"):
        mbtiles.check(p)
    assert mbtiles.looks_like_sqlite(mbtiles.SQLITE_MAGIC + b"x")
    assert not mbtiles.looks_like_sqlite(b"SQLite format 2\x00")


def test_recorded_values_are_cleaned():
    meta = {"name": "Evil\n\x1b[31mTiles " + "x" * 200, "description": "https://a.b\nc"}
    text = mbtiles.license_text(meta)
    assert "\n" not in text and "\x1b" not in text and len(text) < 120
    assert "(https" not in text                    # a homepage with a space is dropped
    assert all(len(v) <= 200 for v in mbtiles.source_record(meta).values())
