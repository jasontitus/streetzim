"""Bounding boxes that may cross the antimeridian (±180°).

An area is one box (west, south, east, north). A box that crosses the
antimeridian (Fiji, Chukotka, Kiribati) is kept UNWRAPPED: west is in
[-180, 180) and east is past 180, so west < east still holds and the width
is east - west. Fiji is (172.8, -23.2, 183.5, -11.2). A box written the
RFC 7946 way, with west > east (172.8,-23.2,-176.5,-11.2), means the same
and is normalised to the unwrapped form.

The unwrapped form is also what map-config.json's "bounds" carries: MapLibre
takes maxBounds past 180 (it wraps them), and its own getBounds() reports
longitudes the same way. Tools that only take longitudes in [-180, 180]
(osmium, tilemaker, mercantile, MapLibre's source bounds) get split(): the
part east of 180 moved back by 360°.

A box that does not cross is returned unchanged by every function here, so
builds of such areas are byte-identical to before.

Stdlib only: streetzim.cli imports this before STREETZIM_CACHE_DIR is set.
"""
from __future__ import annotations

import os
from collections.abc import Sequence

BBox = tuple[float, float, float, float]

# The .poly of the build's clip (streetzim/clip.py), or None. Also in the
# environment, for the steps that run in a spawned child (routing:
# streetzim/isolate.py), which do not see this module's state.
CLIP_POLY_PATH: str | None = None
CLIP_POLY_ENV = "STREETZIM_CLIP_POLY"


def normalize(b: Sequence[float]) -> BBox:
    """The unwrapped form of a box: -180 <= west < 180, west < east <= west + 360.

    A box given with west > east crosses the antimeridian (RFC 7946); one
    given with east > 180 already is unwrapped. Raises ValueError for a box
    that is not four numbers with south < north and a positive width."""
    if len(b) != 4:
        raise ValueError(f"{list(b)} is not minlon,minlat,maxlon,maxlat")
    w, s, e, n = (float(v) for v in b)
    if w > e:
        e += 360.0
    if not (-90.0 <= s < n <= 90.0) or not (w < e <= w + 360.0):
        raise ValueError(f"{list(b)} is not minlon,minlat,maxlon,maxlat with "
                         "min < max in range")
    if not (-180.0 <= w < 180.0):           # e.g. 181,..,185 -> -179,..,-175
        shift = 360.0 * ((w + 180.0) // 360.0)
        w, e = w - shift, e - shift
    if e - w >= 360.0:                       # the whole world, whatever it said
        w, e = -180.0, 180.0
    return (w, s, e, n)


def crosses(b: Sequence[float]) -> bool:
    """Whether an unwrapped box crosses the antimeridian."""
    return b[2] > 180.0


def split(b: Sequence[float]) -> list[BBox]:
    """The box as one or two boxes inside [-180, 180]: the part west of the
    antimeridian first, then the part east of it moved back by 360°."""
    w, s, e, n = (float(v) for v in b)
    if e <= 180.0:
        return [(w, s, e, n)]
    return [(w, s, 180.0, n), (-180.0, s, e - 360.0, n)]


def contains_lon(b: Sequence[float], lon: float) -> bool:
    """Whether longitude `lon` (in [-180, 180]) is inside the unwrapped box."""
    w, e = b[0], b[2]
    return w <= lon <= e or (e > 180.0 and w <= lon + 360.0 <= e)


def contains(b: Sequence[float], lon: float, lat: float) -> bool:
    return b[1] <= lat <= b[3] and contains_lon(b, lon)


def unwrap_lon(b: Sequence[float], lon: float) -> float:
    """`lon` in the box's frame: past 180 when the box crosses and the point
    is east of the antimeridian. Unchanged for a box that does not cross."""
    return lon + 360.0 if b[2] > 180.0 and lon < b[0] else lon


def wrap_lon(lon: float) -> float:
    """A longitude back in [-180, 180]."""
    if -180.0 <= lon <= 180.0:
        return lon
    return (lon + 180.0) % 360.0 - 180.0


def to_str(b: Sequence[float]) -> str:
    return ",".join(f"{v:.6f}" for v in b)


def poly_text(b: Sequence[float], name: str = "area") -> str:
    """An Osmosis .poly file with one ring per part of split(b)."""
    lines = [name]
    for i, (w, s, e, n) in enumerate(split(b), 1):
        lines.append(str(i))
        for x, y in ((w, s), (e, s), (e, n), (w, n), (w, s)):
            lines.append(f"   {x:.7f}   {y:.7f}")
        lines.append("END")
    lines.append("END")
    return "\n".join(lines) + "\n"


def osmium_extract_args(b: Sequence[float], workdir: str, bbox_arg: str | None = None,
                        flag: str = "-b") -> list[str]:
    """The area arguments for `osmium extract`.

    A box that does not cross gives [flag, bbox_arg] exactly as the caller
    always passed it (bbox_arg defaults to "w,s,e,n" of the floats). One that
    crosses gives a .poly with a ring on each side of the antimeridian,
    written into `workdir`: osmium takes no box past 180.

    With a clip active (streetzim.clip.set_active), every cut is to the
    clip's outline instead (it lies inside the box)."""
    clip_poly = CLIP_POLY_PATH or os.environ.get(CLIP_POLY_ENV)
    if clip_poly:
        return ["-p", clip_poly]
    if not crosses(b):
        return [flag, bbox_arg if bbox_arg is not None else
                f"{b[0]},{b[1]},{b[2]},{b[3]}"]
    path = os.path.join(workdir, "area-antimeridian.poly")
    with open(path, "w", encoding="utf-8") as f:
        f.write(poly_text(b))
    return ["-p", path]


def sides(b: Sequence[float]) -> list[BBox]:
    """A box as written anywhere (either spelling), as the one or two boxes
    inside [-180, 180] it covers. A box that does not cross comes back as
    itself, unchanged (no rounding, no validation)."""
    w, s, e, n = (float(v) for v in b)
    if w <= e <= 180.0:
        return [(w, s, e, n)]
    return split(normalize((w, s, e, n)))


def _main(argv: list[str]) -> int:
    """For shell scripts (ops/):

        python -m streetzim.area poly BBOX FILE   # write a two-ring .poly
        python -m streetzim.area sides BBOX       # one box per line, w,s,e,n
    """
    if len(argv) == 3 and argv[0] == "poly":
        with open(argv[2], "w", encoding="utf-8") as f:
            f.write(poly_text(normalize([float(v) for v in argv[1].split(",")])))
        return 0
    if len(argv) == 2 and argv[0] == "sides":
        for part in sides([float(v) for v in argv[1].split(",")]):
            print(",".join(repr(v) for v in part))
        return 0
    print(_main.__doc__)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
