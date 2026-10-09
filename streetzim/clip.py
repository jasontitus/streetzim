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
After the routing graph is built, streetzim.routing.margin closes the road
pieces the cut left past the border (Clip.border) to cars.
"""
from __future__ import annotations

import math
import os
from collections.abc import Sequence
from typing import Any

from streetzim import area as _area

KM_PER_DEG = 111.32


def parse_poly(text: str, *, antimeridian_edge: bool = False):
    """The (Multi)Polygon of an Osmosis .poly file, even-odd over all rings
    as osmium reads a well-formed one: a '!' ring is a hole, an island in it
    is land again and a lake in that island water, whatever the order in the
    file. (A malformed file can differ: osmium ignores a '!' ring before any
    outer one, or the part of a hole past the outer rings' envelope.) A ring is made valid first, so a self-crossing one keeps both
    lobes. An outline at the antimeridian is refused: one whose rings reach
    +-180 (Geofabrik splits them there) or jump across it. With
    `antimeridian_edge`, vertices ON +-180 are accepted (for boxed_outline,
    which cuts them off); a ring past it or jumping across it still is not."""
    import shapely
    from shapely.geometry import Polygon

    geom = Polygon()
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
        lo, hi = (-180.0, 180.0)
        if any(not (lo <= x <= hi if antimeridian_edge else lo < x < hi)
               or not -90.0 <= y <= 90.0 for x, y in ring):
            raise ValueError(f"ring {name!r} reaches the antimeridian or leaves the "
                             "globe: an outline there is not supported")
        if any(abs(a[0] - b[0]) > 180.0 for a, b in zip(ring, ring[1:] + ring[:1])):
            raise ValueError(f"ring {name!r} jumps across the antimeridian: "
                             "an outline there is not supported")
        geom = _polygonal(geom.symmetric_difference(
            _polygonal(shapely.make_valid(Polygon(ring)))))
    if geom.is_empty:
        raise ValueError("the .poly file encloses no area")
    return geom


# How far inside +-180 boxed_outline keeps an outline: parse_poly refuses
# vertices on the antimeridian itself.
ANTIMERIDIAN_INSET = 1e-4


def boxed_outline(texts: str | Sequence[str], bbox: Sequence[float]):
    """The outline in `texts` (one Osmosis .poly, or several, joined) cut to
    a build's box (west, south, east, north), for --clip-poly.

    Several outlines make one region from Geofabrik's pieces (Europe plus
    Armenia and Azerbaijan, which Geofabrik files under Asia). Geofabrik's
    outlines for Russia and the United States (the Aleutians) are split at
    the antimeridian, so parse_poly refuses them as they come; a build box
    never crosses it (from_poly_file refuses that), so cutting to the box,
    kept ANTIMERIDIAN_INSET inside +-180, gives an outline parse_poly
    accepts. Cutting first costs nothing: from_poly_file cuts the widened
    outline to the same box anyway."""
    from shapely.geometry import box
    from shapely.ops import unary_union
    w, s, e, n = (float(v) for v in bbox)
    if w > e:
        raise ValueError(f"box {tuple(bbox)} crosses the antimeridian: not supported")
    if w < -180.0 or e > 180.0:
        raise ValueError(f"box {tuple(bbox)} leaves the globe")
    if isinstance(texts, str):
        texts = [texts]
    geom = _polygonal(unary_union([parse_poly(t, antimeridian_edge=True) for t in texts]))
    cut = _polygonal(geom.intersection(box(max(w, -180.0 + ANTIMERIDIAN_INSET), s,
                                           min(e, 180.0 - ANTIMERIDIAN_INSET), n)))
    if cut.is_empty:
        raise ValueError(f"the outline does not meet the box {tuple(bbox)}")
    return cut

def _polygonal(geom):
    """The polygons of `geom` (make_valid can add stray lines and points)."""
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.ops import unary_union
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    parts = [g for g in getattr(geom, "geoms", ()) if isinstance(g, (Polygon, MultiPolygon))]
    return unary_union(parts) if parts else Polygon()


BUFFER_STRIP_DEG = 6.0


def buffer_km(geom, km: float):
    """`geom` widened by about `km` kilometres, within the globe. The buffer
    is taken in a sinusoidal projection, which scales each point's
    longitude by the cosine of its own latitude (a single cosine for the
    whole outline made Norway's margin 12 km at its southern end and 8 km
    at its northern one), strip by strip of BUFFER_STRIP_DEG of longitude,
    each about its own middle meridian: far from it the projection shears
    (one projection for Canada gave 7 km of 10 at the Alaska panhandle;
    6-degree strips and 8-segment arcs keep it above 9.5)."""
    if km <= 0:
        return geom
    from shapely.geometry import box
    from shapely.ops import unary_union
    west, _, east, _ = geom.bounds
    parts = []
    x = west
    while x < east:
        x1 = min(x + BUFFER_STRIP_DEG, east)
        piece = _polygonal(geom.intersection(box(x, -90.0, x1, 90.0)))
        if not piece.is_empty:
            parts.append(_buffer_sinusoidal(piece, km, (x + x1) / 2))
        x = x1
    return _polygonal(unary_union(parts).intersection(box(-180.0, -90.0, 180.0, 90.0)))


def _buffer_sinusoidal(geom, km: float, lon0: float):
    """`geom` buffered by `km` in a sinusoidal projection about `lon0`."""
    import numpy as np
    import shapely

    def scale(c, inverse):
        k = np.maximum(np.cos(np.radians(c[:, 1])), 0.05)
        x = c[:, 0] / k + lon0 if inverse else (c[:, 0] - lon0) * k
        return np.column_stack((x, c[:, 1]))

    # Densified both ways (0.05 deg), so a long edge follows its latitudes'
    # cosines instead of one straight line between its ends.
    flat = shapely.transform(shapely.segmentize(geom, 0.05), lambda c: scale(c, False))
    grown = shapely.segmentize(flat.buffer(km / KM_PER_DEG, quad_segs=8), 0.05)
    return shapely.transform(grown, lambda c: scale(c, True))


def tile_box(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) of an XYZ tile, in degrees."""
    n = 2.0 ** z
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (x / n * 360.0 - 180.0, south, (x + 1) / n * 360.0 - 180.0, north)


OUTSIDE, PARTIAL, INSIDE = 0, 1, 2
# How far inside the outline a clip-zoom tile must lie to need no context
# copy: the viewer's clipArea is the outline simplified by 0.005 deg.
CONTEXT_EDGE_DEG = 0.01


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
        # (z, x, y) -> OUTSIDE/PARTIAL/INSIDE for the tiles tested against
        # the outline: those whose parent is PARTIAL. A child of an OUTSIDE
        # or INSIDE tile is the same and is not stored, so the memo grows
        # with the outline's length, not the box's area.
        self._state: dict[tuple[int, int, int], int] = {}
        self.poly_path: str | None = None
        self._inner = None

    @classmethod
    def from_poly_file(cls, path: str, *, buffer: float = 10.0, min_zoom: int = 10,
                       bbox: Sequence[float] | None = None):
        """The outline in `path`, widened by `buffer` km and, with `bbox`
        (west, south, east, north), cut to the build's box: nothing past
        the box is cut out of the PBF, routed or masked."""
        with open(path, encoding="utf-8") as f:
            geom = parse_poly(f.read())
        grown = buffer_km(geom, buffer)
        if bbox is not None:
            from shapely.geometry import box
            # Polygons only: an outline touching the box's edge leaves
            # lines and points in the intersection (write_poly has rings).
            grown = _polygonal(grown.intersection(box(bbox[0], bbox[1], bbox[2], bbox[3])))
            if grown.is_empty:
                raise ValueError(f"{path}: the outline does not meet the box {tuple(bbox)}")
        return cls(grown, min_zoom, border=geom)

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

    def needs_context(self, x: int, y: int) -> bool:
        """Whether clip-zoom tile x/y reaches past the outline, so the viewer
        may show it overzoomed there (ctx/ redirects, map-config
        "clipContext"); a tile well inside never shows through clip-inside."""
        import shapely
        from shapely.geometry import box
        if self._inner is None:
            self._inner = self.geom.buffer(-CONTEXT_EDGE_DEG)
            shapely.prepare(self._inner)
        return not self._inner.contains(box(*tile_box(self.min_zoom, x, y)))

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
    if clip is not None and workdir:
        _area.CLIP_POLY_PATH = clip.write_poly(os.path.join(workdir, "clip.poly"))
        os.environ[_area.CLIP_POLY_ENV] = _area.CLIP_POLY_PATH


def active() -> Clip | None:
    return _ACTIVE

