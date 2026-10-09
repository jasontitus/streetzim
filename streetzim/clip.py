"""Clip a build to a region's outline instead of its bounding box (--clip-poly).

A region's box holds a lot that is not the region: the Mexico box reaches San
Diego and El Paso, the Argentina box holds all of Chile. With a clip, tiles
from zoom `min_zoom + 1` up, satellite and terrain tiles, search records and
the PBF cuts keep only what touches the region's outline (an Osmosis .poly,
e.g. Geofabrik's), widened by `buffer_km` so border towns and crossings stay.

Tiles up to `min_zoom` are kept over the whole box, as before: they are the
map's context, and they are small (Egypt's z0-10 are about 6 MB). Past the
outline the viewer then shows those tiles overzoomed under a grey mask
(map-config.json "clipMask") instead of blank land: openzim/maps moved from
outline to box cuts because of the blank areas (openzim/maps#90, #25).

The outline must not cross the antimeridian (longitudes in [-180, 180]).

One clip is active per build, set by create_osm_zim.py (set_active) and read
where tiles and records are written (active); streetzim.area reads its .poly
path for every `osmium extract`, from the environment in a spawned child.
The routing build reads the unbuffered outline the same way (active_border):
streetzim.routing.margin closes the margin's cut-off roads to cars.
"""
from __future__ import annotations

import math
import os
from collections.abc import Sequence
from typing import Any

from streetzim import area as _area

KM_PER_DEG = 111.32
BORDER_ENV = "STREETZIM_CLIP_BORDER"     # the unbuffered outline's .poly, for a spawned child


def parse_poly(text: str):
    """The (Multi)Polygon of an Osmosis .poly file; '!' rings are holes."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    outers, holes = [], []
    lines = [ln.strip() for ln in text.splitlines()]
    i = 1                                   # line 0 is the file's name
    while i < len(lines):
        name = lines[i]
        i += 1
        if not name:
            continue
        if name == "END":
            break
        ring: list[tuple[float, float]] = []
        while i < len(lines) and lines[i] != "END":
            if lines[i]:
                x, y = lines[i].split()[:2]
                ring.append((float(x), float(y)))
            i += 1
        i += 1                              # past the ring's END
        if len(ring) < 3:
            raise ValueError(f"ring {name!r} has fewer than 3 points")
        if any(not -180.0 <= x <= 180.0 for x, _ in ring):
            raise ValueError(f"ring {name!r} leaves [-180, 180]: an outline across "
                             "the antimeridian is not supported")
        (holes if name.startswith("!") else outers).append(Polygon(ring).buffer(0))
    if not outers:
        raise ValueError("no outer ring in .poly file")
    geom = unary_union(outers)
    if holes:
        geom = geom.difference(unary_union(holes))
    return geom


def buffer_km(geom, km: float):
    """`geom` widened by about `km` kilometres. Longitudes are scaled by the
    cosine of the outline's middle latitude first, which is close enough for
    a margin of a few km on a country."""
    if km <= 0:
        return geom
    from shapely import affinity
    lat = (geom.bounds[1] + geom.bounds[3]) / 2
    k = max(math.cos(math.radians(lat)), 0.05)
    flat = affinity.scale(geom, xfact=k, yfact=1.0, origin=(0, 0))
    return affinity.scale(flat.buffer(km / KM_PER_DEG, quad_segs=4),
                          xfact=1 / k, yfact=1.0, origin=(0, 0))


def tile_box(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) of an XYZ tile, in degrees."""
    n = 2.0 ** z
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (x / n * 360.0 - 180.0, south, (x + 1) / n * 360.0 - 180.0, north)


OUTSIDE, PARTIAL, INSIDE = 0, 1, 2


class Clip:
    """A region outline (already buffered) and the zoom it starts at.
    `border` is the outline before the buffer, when known: what the region
    itself covers, as opposed to the margin around it."""

    def __init__(self, geom, min_zoom: int = 10, border=None):
        import shapely
        self.geom = geom
        self.min_zoom = min_zoom
        self.border = border
        shapely.prepare(self.geom)
        # (z, x, y) -> OUTSIDE/PARTIAL/INSIDE for tiles above the deepest
        # zoom: a child of an OUTSIDE or INSIDE tile is the same, so only
        # tiles along the outline are ever tested.
        self._state: dict[tuple[int, int, int], int] = {}
        self.poly_path: str | None = None

    @classmethod
    def from_poly_file(cls, path: str, *, buffer: float = 10.0, min_zoom: int = 10):
        with open(path, encoding="utf-8") as f:
            geom = parse_poly(f.read())
        return cls(buffer_km(geom, buffer), min_zoom, border=geom)

    def _tile_state(self, z: int, x: int, y: int) -> int:
        if z <= self.min_zoom:
            return PARTIAL                  # not tested: its children are
        key = (z, x, y)
        st = self._state.get(key)
        if st is not None:
            return st
        st = self._tile_state(z - 1, x // 2, y // 2)
        if st == PARTIAL:
            from shapely.geometry import box
            b = box(*tile_box(z, x, y))
            st = (INSIDE if self.geom.contains(b)
                  else PARTIAL if self.geom.intersects(b) else OUTSIDE)
        if z < 14:
            self._state[key] = st
        return st

    def keeps_tile(self, z: int, x: int, y: int) -> bool:
        """Whether tile z/x/y belongs in the ZIM: every tile up to min_zoom,
        and past it the tiles that touch the outline."""
        return z <= self.min_zoom or self._tile_state(z, x, y) != OUTSIDE

    def contains(self, lon: float, lat: float) -> bool:
        import shapely
        return bool(shapely.contains_xy(self.geom, lon, lat))

    def write_poly(self, path: str) -> str:
        """The outline as an Osmosis .poly (for osmium extract -p)."""
        self.poly_path = _write_poly(self.geom, path)
        return path

    def _simplified(self, tolerance: float):
        return self.geom.simplify(tolerance, preserve_topology=True)

    def mask_geojson(self, bbox: Sequence[float], tolerance: float = 0.005) -> dict:
        """GeoJSON geometry of what the viewer greys out: the build's box
        (widened, so the edge is never on screen) minus the outline."""
        from shapely.geometry import box, mapping
        w, s, e, n = bbox
        pad = max(e - w, n - s)
        world = box(max(-180.0, w - pad), max(-85.0, s - pad),
                    min(180.0, e + pad), min(85.0, n + pad))
        return _round_coords(mapping(world.difference(self._simplified(tolerance))), 5)

    def area_geojson(self, tolerance: float = 0.005) -> dict:
        """GeoJSON geometry of the outline as the mask leaves it (the same
        simplification, so the two meet without a gap): the viewer covers
        the overzoomed context map with it where the full map is."""
        from shapely.geometry import mapping
        return _round_coords(mapping(self._simplified(tolerance)), 5)


def _write_poly(geom, path: str) -> str:
    """`geom` as an Osmosis .poly file at `path`."""
    from shapely.geometry import MultiPolygon
    polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    lines = ["clip"]
    n = 0
    for p in polys:
        for hole, ring in [(False, p.exterior)] + [(True, r) for r in p.interiors]:
            n += 1
            lines.append(("!" if hole else "") + str(n))
            lines += [f"   {x:.7f}   {y:.7f}" for x, y in ring.coords]
            lines.append("END")
    lines.append("END")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _round_coords(obj: Any, nd: int) -> Any:
    if isinstance(obj, dict):
        return {k: _round_coords(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], float):
            return [round(v, nd) for v in obj]
        return [_round_coords(v, nd) for v in obj]
    return obj


_ACTIVE: Clip | None = None


def set_active(clip: Clip | None, workdir: str | None = None) -> None:
    """Make `clip` the build's clip; with `workdir`, also write its .poly
    there so every `osmium extract` cuts to it (streetzim.area)."""
    global _ACTIVE
    _ACTIVE = clip
    _area.CLIP_POLY_PATH = None
    os.environ.pop(_area.CLIP_POLY_ENV, None)
    os.environ.pop(BORDER_ENV, None)
    if clip is not None and workdir:
        _area.CLIP_POLY_PATH = clip.write_poly(os.path.join(workdir, "clip.poly"))
        os.environ[_area.CLIP_POLY_ENV] = _area.CLIP_POLY_PATH
        if clip.border is not None:
            os.environ[BORDER_ENV] = _write_poly(clip.border,
                                                 os.path.join(workdir, "clip-border.poly"))


def active() -> Clip | None:
    return _ACTIVE


def active_border():
    """The active clip's outline before the buffer, or None: from the clip
    in this process, else (a spawned child) from the .poly that set_active
    named in the environment."""
    if _ACTIVE is not None:
        return _ACTIVE.border
    path = os.environ.get(BORDER_ENV)
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return parse_poly(f.read())
