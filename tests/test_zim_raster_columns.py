"""The ZIM writer's raster packing lists only the cache columns the build's
box reaches. The satellite and terrain caches are shared by every region;
listing every column (Poland: 16 M satellite and 22 M terrain files outside
its box) ran the packing at 2 tiles/s on a busy disk (2026-10-19)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
mercantile = pytest.importorskip("mercantile")

from streetzim import area as A  # noqa: E402
from streetzim import zim_writer as W  # noqa: E402


class _Creator:
    """No add_alias: tile aliasing stays off."""
    def __init__(self):
        self.items = []

    def add_item(self, item):
        self.items.append(item)


def _map_item(path, title, mimetype, content, compress=True):
    return path


def _cache(root, bbox, zooms):
    """The box's tiles, plus other regions' columns and a tile of the box's
    column at another latitude; returns the ZIM paths of the box's tiles."""
    inside = set()
    for z in zooms:
        for t in mercantile.tiles(*bbox, zooms=z):
            d = root / str(z) / str(t.x)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{t.y}.webp").write_bytes(b"x")
            inside.add(f"satellite/{z}/{t.x}/{t.y}.webp")
        for x in (0, 2 ** z - 1):
            d = root / str(z) / str(x)
            d.mkdir(parents=True, exist_ok=True)
            (d / "0.webp").write_bytes(b"x")
        t = next(iter(mercantile.tiles(*bbox, zooms=z)))
        (root / str(z) / str(t.x) / "0.webp").write_bytes(b"x")
    return inside


def _pack(monkeypatch, sat, bbox, max_zoom):
    listed = []
    real = os.listdir

    def spy(p):
        listed.append(str(p))
        return real(p)

    monkeypatch.setattr(os, "listdir", spy)
    c = _Creator()
    W._add_raster_layers(c, _map_item, satellite_dir=str(sat), satellite_max_zoom=max_zoom,
                         satellite_format="webp", terrain_dir=None, terrain_max_zoom=None,
                         bbox=bbox)
    return set(c.items), listed


def test_only_the_columns_the_box_reaches_are_listed(tmp_path, monkeypatch):
    bbox = (5.72, 50.08, 5.92, 50.28)
    sat = tmp_path / "sat"
    inside = _cache(sat, bbox, (8, 10, 12))
    items, listed = _pack(monkeypatch, sat, bbox, 12)
    assert items == inside                          # the same tiles as before
    far = {str(sat / str(z) / str(x)) for z in (8, 10, 12) for x in (0, 2 ** z - 1)}
    assert not far & set(listed), sorted(far & set(listed))


def test_a_box_across_the_antimeridian_keeps_both_sides(tmp_path, monkeypatch):
    bbox = (178.5, -18.5, 181.5, -15.5)             # Fiji-like, unwrapped (streetzim.area)
    assert A.crosses(bbox)
    sat = tmp_path / "sat"
    inside = set()
    for z in (6, 8):
        for minlon, minlat, maxlon, maxlat in A.split(bbox):
            for t in mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z):
                d = sat / str(z) / str(t.x)
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{t.y}.webp").write_bytes(b"x")
                inside.add(f"satellite/{z}/{t.x}/{t.y}.webp")
        mid = sat / str(z) / str(2 ** z // 2)        # Greenwich: neither side
        mid.mkdir(parents=True, exist_ok=True)
        (mid / "0.webp").write_bytes(b"x")
    items, listed = _pack(monkeypatch, sat, bbox, 8)
    assert items == inside
    assert not {str(sat / str(z) / str(2 ** z // 2)) for z in (6, 8)} & set(listed)


def test_the_columns_cover_every_tile_the_box_touches():
    # _tile_in_bbox keeps a tile that only touches the box: the column range
    # must hold it (one column each way).
    for bbox in ((5.72, 50.08, 5.92, 50.28), (-180.0, -10.0, -179.0, 10.0),
                 (179.0, -10.0, 180.0, 10.0), (179.5, -10.0, 180.5, 10.0),
                 (0.0, 0.0, 0.0001, 0.0001), (-5.625, 1, 5.625, 2)):
        for z in (0, 3, 9, 14):
            cols = W._bbox_columns(z, bbox)
            for minlon, minlat, maxlon, maxlat in A.split(bbox):
                for t in mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z):
                    assert any(lo <= t.x <= hi for lo, hi in cols), (bbox, z, t)


def test_a_tile_that_only_touches_the_west_edge_is_listed():
    # The writer keeps a tile whose east edge equals the box's west edge
    # (_tile_in_bbox counts touching), but mercantile.tiles starts at the tile
    # containing the west edge and never yields it, so the test above cannot
    # see a lost west column. At z8, x 63 ends at -90 and x 128 starts at 0.
    cols = W._bbox_columns(8, (-90.0, -10.0, 0.0, 10.0))
    assert any(lo <= 63 <= hi for lo, hi in cols), cols
    assert any(lo <= 128 <= hi for lo, hi in cols), cols
