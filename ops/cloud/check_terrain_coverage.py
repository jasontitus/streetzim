#!/usr/bin/env python3
"""Pre-upload gate: fail if a ZIM has BLANK terrain over land.

The bug this catches (Carolinas 2026-07-01): terrain tiles rendered as
44-byte blank webps over the western-NC mountains (Asheville/Smokies)
were cached and packed into the ZIM. Coverage counts looked complete
(every tile present) and the sampled validator passed, but the tiles had
no elevation. Metadata/coverage checks miss this; only per-tile content
+ a land mask catches it.

Method: for each terrain tile in the ZIM across the region bbox, sample
the DEM VRT at the tile center. If it's land (elevation above a sea
threshold) the terrain tile must be non-trivial (> MIN_BYTES). Blank
land tiles => FAIL (exit 3) with a sample list.

A ZIM built with --clip-poly has no tiles past its clip zoom outside the
outline (map-config.json clipMinZoom, clipArea): those are not counted.
The outline in map-config is simplified (by 0.005 deg), so tiles within
CLIP_EDGE_DEG of its edge are not counted either.

Usage:
  check_terrain_coverage.py ZIM "minlon,minlat,maxlon,maxlat" \
      [--zooms 10-12] [--min-bytes 200] [--sea-level 2] \
      [--vrt terrain_cache/dem_sources/comprehensive.vrt]
"""
import argparse, io, math, sys
from libzim.reader import Archive
import rasterio
from rasterio.warp import transform


def deg2tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def tile_columns(w, s, e, n, z):
    """Tile columns of the bbox at zoom z. Across the antimeridian
    (minlon > maxlon, or maxlon past 180): the columns at both ends of the
    row, not the ones between."""
    x0 = deg2tile(n, w, z)[0]
    x1 = deg2tile(s, e, z)[0]
    if w > e or e > 180:
        xe = deg2tile(s, e - 360 if e > 180 else e, z)[0]
        return sorted(set(range(x0, 2 ** z)) | set(range(0, xe + 1)))
    return range(min(x0, x1), max(x0, x1) + 1)


CLIP_EDGE_DEG = 0.01


def tile_bounds(x, y, z):
    """(west, south, east, north) of tile z/x/y."""
    n = 2 ** z
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return x / n * 360 - 180, south, (x + 1) / n * 360 - 180, north


def clip_filter(arc):
    """(clip zoom, keeps(x, y, z)) for a --clip-poly ZIM, else None. keeps
    is False for a tile the clip dropped: past the clip zoom and clear of the
    outline (clipArea; or, in early trial builds, inside clipMask)."""
    import json
    try:
        mc = json.loads(bytes(arc.get_entry_by_path("map-config.json").get_item().content))
    except Exception:  # noqa: BLE001 — no map-config: not a clipped build
        return None
    if mc.get("clipMinZoom") is None or not (mc.get("clipArea") or mc.get("clipMask")):
        return None
    import shapely
    from shapely.geometry import Polygon, box, shape
    from shapely.ops import unary_union
    if mc.get("clipArea"):
        area = shape(mc["clipArea"])
    else:
        # The mask is the widened box minus the outline: its holes.
        mask = shape(mc["clipMask"])
        polys = getattr(mask, "geoms", [mask])
        area = unary_union([Polygon(r) for p in polys for r in p.interiors])
    area = area.buffer(-CLIP_EDGE_DEG)
    shapely.prepare(area)
    return int(mc["clipMinZoom"]), lambda x, y, z: area.intersects(box(*tile_bounds(x, y, z)))


def clip_skips(clip, x, y, z):
    """Whether the gate leaves tile z/x/y out: the clip (clip_filter) dropped
    it. Tiles up to the clip zoom are all in a clipped ZIM."""
    return clip is not None and z > clip[0] and not clip[1](x, y, z)


def tile_center(x, y, z):
    n = 2 ** z
    lon = (x + 0.5) / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
    return lat, lon


def tile_elevation_ok(raw, dem_elev, tol_m=400.0):
    """Does this small terrain tile actually carry elevation?

    Returns (ok, detail). A genuinely blank tile decodes to the all-zero
    RGB sentinel, i.e. -10000 m everywhere. A real one decodes near the
    DEM's own reading for the same spot, however flat it is.
    """
    if not raw:
        return False, "unreadable"
    try:
        from PIL import Image
        import numpy as np
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        a = np.asarray(im).astype(np.float64)
        h = -10000.0 + (a[:, :, 0] * 65536.0 + a[:, :, 1] * 256.0 + a[:, :, 2]) * 0.1
    except Exception as exc:  # noqa: BLE001 — a tile we cannot decode is not proof of elevation
        return False, f"undecodable ({exc})"
    hmin, hmax, hmean = float(h.min()), float(h.max()), float(h.mean())
    if hmax <= -9000.0:
        return False, "decodes to the all-zero sentinel"
    if abs(hmean - dem_elev) > tol_m:
        return False, (f"mean {hmean:.0f} m disagrees with the DEM's "
                       f"{dem_elev:.0f} m")
    return True, f"flat but real ({hmin:.0f}-{hmax:.0f} m, DEM {dem_elev:.0f} m)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zim")
    ap.add_argument("bbox", help="minlon,minlat,maxlon,maxlat")
    ap.add_argument("--zooms", default="10-12")
    ap.add_argument("--min-bytes", type=int, default=200)
    ap.add_argument("--sea-level", type=float, default=2.0,
                    help="DEM elevation (m) above which a tile counts as land")
    ap.add_argument("--vrt", default="terrain_cache/dem_sources/comprehensive.vrt")
    ap.add_argument("--max-report", type=int, default=20)
    a = ap.parse_args()

    z0, z1 = (int(v) for v in a.zooms.split("-")) if "-" in a.zooms else (int(a.zooms), int(a.zooms))
    w, s, e, n = (float(v) for v in a.bbox.split(","))
    arc = Archive(a.zim)

    def zsize(p):
        try:
            return arc.get_entry_by_path(p).get_item().size
        except Exception:
            return None

    def zbytes(p):
        try:
            return bytes(arc.get_entry_by_path(p).get_item().content)
        except Exception:
            return None

    dem = rasterio.open(a.vrt)
    def elev(lat, lon):
        # 1x1 windowed read — the VRT is a planet-scale mosaic, never read whole.
        xs, ys = transform("EPSG:4326", dem.crs, [lon], [lat])
        r, c = dem.index(xs[0], ys[0])
        if 0 <= r < dem.height and 0 <= c < dem.width:
            v = dem.read(1, window=((r, r + 1), (c, c + 1)))
            return float(v[0, 0]) if v.size else -32768.0
        return -32768.0

    land = blank_land = missing_land = ocean = flat_land = outside_clip = 0
    offenders = []
    clip = clip_filter(arc)
    for z in range(z0, z1 + 1):
        x0, y0 = deg2tile(n, w, z)
        x1, y1 = deg2tile(s, e, z)
        for x in tile_columns(w, s, e, n, z):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                if clip_skips(clip, x, y, z):
                    outside_clip += 1
                    continue
                lat, lon = tile_center(x, y, z)
                if elev(lat, lon) <= a.sea_level:
                    ocean += 1
                    continue
                land += 1
                sz = zsize(f"terrain/{z}/{x}/{y}.webp")
                if sz is None:
                    missing_land += 1
                    if len(offenders) < a.max_report:
                        offenders.append(f"z{z}/{x}/{y} MISSING (~{lat:.2f},{lon:.2f})")
                elif sz <= a.min_bytes:
                    # Small is suspicious, not proof. Terrain-RGB over flat
                    # land is nearly a constant image and webp crushes it:
                    # the San Joaquin Valley and the Modoc Plateau produce
                    # 58-198 byte tiles that carry perfectly good elevation
                    # (20-40 m and 1230-1240 m respectively). Judging on
                    # size alone failed California and would fail the Great
                    # Plains, the Sahara, the Amazon basin and Siberia.
                    # Decode it and ask whether the elevation is real.
                    verdict, detail = tile_elevation_ok(
                        zbytes(f"terrain/{z}/{x}/{y}.webp"), elev(lat, lon))
                    if verdict:
                        flat_land += 1
                        continue
                    blank_land += 1
                    if len(offenders) < a.max_report:
                        offenders.append(
                            f"z{z}/{x}/{y} {sz}B (~{lat:.2f},{lon:.2f}) {detail}")

    bad = blank_land + missing_land
    print(f"terrain coverage {a.zim} z{a.zooms}: land={land} ocean={ocean} "
          f"blank-land={blank_land} missing-land={missing_land} "
          f"flat-but-real={flat_land}"
          + (f" outside-clip={outside_clip}" if clip is not None else ""))
    if bad:
        print(f"[FAIL] {bad} land tiles with no terrain:")
        for o in offenders:
            print("   ", o)
        if bad > len(offenders):
            print(f"    ... and {bad - len(offenders)} more")
        sys.exit(3)
    print("[OK] every land tile has real terrain")


if __name__ == "__main__":
    main()
