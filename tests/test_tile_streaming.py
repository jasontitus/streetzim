"""Regional MBTiles iteration must have bounded setup memory and close SQLite."""
import random
import sqlite3

import mercantile
import pytest

from streetzim import area, tiles
from tests.mbtiles_fixture import make_mbtiles


BOXES = [
    (7.40, 43.72, 7.44, 43.76),
    (172.8, -23.2, -176.5, -11.2),
    (172.8, -23.2, 183.5, -11.2),
    (-90.0, -45.0, 0.0, 0.0),
    (0.0, 0.0, 180.0, 85.0511287798066),
    (-10.0, 85.2, 10.0, 89.0),
    (-180.0, -90.0, 180.0, 90.0),
    tuple(mercantile.bounds(mercantile.Tile(3, 2, 4))),
]


def covered(bbox, z):
    return {(t.x, (1 << z) - 1 - t.y)
            for part in area.split(bbox) for t in mercantile.tiles(*part, zooms=z)}


@pytest.mark.parametrize('bbox', BOXES)
def test_arithmetic_bounds_match_mercantile_at_edges_and_antimeridian(bbox):
    for z in range(6):
        got = {(x, r) for c0, c1, r0, r1 in tiles._bbox_tile_ranges(bbox, z)
               for x in range(c0, c1 + 1) for r in range(r0, r1 + 1)}
        assert got == covered(bbox, z), z


def test_arithmetic_bounds_match_random_regions():
    rng = random.Random(2317)
    for _ in range(100):
        west = rng.uniform(-180, 180)
        east = west + rng.uniform(0.001, 140)
        south = rng.uniform(-89, 89)
        north = rng.uniform(south + 0.001, 90)
        bbox = (west, south, east, north)
        for z in range(5):
            got = {(x, r) for c0, c1, r0, r1 in tiles._bbox_tile_ranges(bbox, z)
                   for x in range(c0, c1 + 1) for r in range(r0, r1 + 1)}
            assert got == covered(bbox, z), (bbox, z)


@pytest.mark.parametrize('ofm', [False, True])
@pytest.mark.parametrize('bbox', BOXES[:6])
def test_iterator_returns_same_tiles_and_estimate(tmp_path, bbox, ofm):
    rows = [(z, x, y) for z in range(6) for x in range(1 << z) for y in range(1 << z)]
    path = make_mbtiles(tmp_path / 'world.mbtiles', rows, ofm=ofm)
    got = [(z, x, y) for z, x, y, _ in tiles.iter_tiles_from_mbtiles(
        path, bbox=bbox, max_zoom=5)]
    want = [(z, x, (1 << z) - 1 - r) for z in range(6)
            for x, r in sorted(covered(bbox, z))]
    assert got == want
    assert tiles.estimate_tile_total(path, bbox=bbox, max_zoom=5) == len(want)
    for z in (0, 3, 5):
        got_z = [(zz, x, y) for zz, x, y, _ in tiles.iter_tiles_from_mbtiles(
            path, bbox=bbox, zoom_level=z)]
        assert got_z == [t for t in want if t[0] == z]


def test_large_bbox_setup_never_enumerates_tiles(tmp_path, monkeypatch):
    path = make_mbtiles(tmp_path / 'empty.mbtiles', [])

    def enumerate_forbidden(*args, **kwargs):
        raise AssertionError('setup enumerated a tile rectangle')

    monkeypatch.setattr(mercantile, 'tiles', enumerate_forbidden)
    # Nearly four million z14 tiles are in this rectangle; the input is
    # deliberately sparse so setup cannot hide behind streaming its rows.
    assert list(tiles.iter_tiles_from_mbtiles(
        path, bbox=(-125, 25, -66, 49), max_zoom=14)) == []
    assert tiles.estimate_tile_total(path, bbox=(-125, 25, -66, 49), max_zoom=14) > 4_000_000


@pytest.mark.parametrize('termination', ['exhausted', 'closed', 'error'])
def test_iterator_closes_connection_on_every_exit(tmp_path, monkeypatch, termination):
    path = make_mbtiles(tmp_path / 'world.mbtiles', [(0, 0, 0), (1, 0, 0)])
    connect = sqlite3.connect
    closed = []

    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(tiles.sqlite3, 'connect',
                        lambda path: connect(path, factory=Connection))
    if termination == 'error':
        with connect(path) as conn:
            conn.execute('DROP VIEW tiles')
        with pytest.raises(sqlite3.OperationalError):
            list(tiles.iter_tiles_from_mbtiles(path))
    else:
        gen = tiles.iter_tiles_from_mbtiles(path)
        assert next(gen)[:3] == (0, 0, 0)
        if termination == 'closed':
            gen.close()
        else:
            list(gen)
    assert closed == [True]


@pytest.mark.parametrize('layout', ['rowid', 'view', 'without-rowid'])
def test_capped_world_scan_supports_all_mbtiles_layouts(tmp_path, layout):
    path = make_mbtiles(tmp_path / 'world.mbtiles', [(0, 0, 0), (1, 1, 1), (2, 3, 3)],
                        ofm=layout == 'view')
    if layout == 'without-rowid':
        with sqlite3.connect(path) as conn:
            conn.executescript('ALTER TABLE tiles RENAME TO old_tiles; '
                               'CREATE TABLE tiles (zoom_level INT, tile_column INT, '
                               'tile_row INT, tile_data BLOB, '
                               'PRIMARY KEY (zoom_level,tile_column,tile_row)) WITHOUT ROWID; '
                               'INSERT INTO tiles SELECT * FROM old_tiles; DROP TABLE old_tiles;')
    for bbox in (None, (-180, -90, 180, 90)):
        assert [(z, x, y) for z, x, y, _ in tiles.iter_tiles_from_mbtiles(
            path, bbox=bbox, max_zoom=1)] == [(0, 0, 0), (1, 1, 1)]
