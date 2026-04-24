"""Structural pre-build check: prove every cached terrain tile is as
fresh as the DEM data that covers it.

The bug this prevents: stale tiles whose underlying DEMs were updated
after the tile was rasterised, then reused on a subsequent build because
a ``COMPLETED_z12_{bbox}`` marker told ``create_osm_zim.py`` to skip
regeneration.

Guarantee this script provides: if the script exits 0, every terrain
tile in the affected region bboxes has ``mtime >= max(mtime of covering
DEMs)``. If it exits nonzero, the count of stale tiles and the list of
``(z, x, y)`` is printed.

When ``--regenerate`` is passed, stale tiles are regenerated from
``terrain_cache/dem_sources/comprehensive.vrt`` (world-spanning DEM
mosaic) using the same 2-pixel-halo buffered technique as
``fix_terrain_seams.py`` — adjacent tiles sample identical halo pixels
so edges stay continuous.

A build wrapper (``cloud/preflight_build.sh``) runs this BEFORE
``create_osm_zim.py``. If this returns nonzero, the build is refused.
That is the structural guarantee the user asked for: we cannot start a
build that would ship stale terrain, because we check first and fail
loudly.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
import glob
import multiprocessing
from concurrent.futures import ProcessPoolExecutor as Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEM_DIR = ROOT / "terrain_cache" / "dem_sources"
TERRAIN_DIR = ROOT / "terrain_cache"
WORLD_VRT = DEM_DIR / "comprehensive.vrt"

REGIONS = {
    "australia-nz":  (112.0, -47.5, 179.5, -10.0),
    "texas":         (-107.0, 25.5, -93.4, 36.6),
    "west-coast-us": (-125.0, 32.0, -114.0, 46.0),
    "east-coast-us": (-82.0, 24.0, -66.5, 47.6),
    "central-us":    (-120.0, 31.3, -104.0, 49.0),
    "iran":          (44.0, 25.0, 63.5, 39.8),
    "japan":         (122.9, 24.0, 146.0, 45.6),
    "silicon-valley":(-122.6, 37.2, -121.7, 37.9),
    "colorado":      (-109.06, 36.99, -102.04, 41.00),
    "hispaniola":    (-74.5, 17.5, -68.3, 20.1),
    "washington-dc": (-77.12, 38.79, -76.91, 38.99),
    "baltics":       (20.9, 53.9, 28.3, 59.7),
}


def tile_to_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z
    lon_w = x / n * 360.0 - 180.0
    lon_e = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (lon_w, lat_s, lon_e, lat_n)


def covering_dem_names(z: int, x: int, y: int) -> list[str]:
    """Every 1°×1° DEM name (format ``dem_N##_W###.tif``) that overlaps
    the tile's geographic extent. Copernicus DEMs are keyed by the
    southwest corner in whole-degree bands; a tile spanning lat [40.98,
    48.92] & lon [-123.75, -112.5] covers DEMs from N41..N48 × W112..W124.
    """
    lon_w, lat_s, lon_e, lat_n = tile_to_bounds(z, x, y)
    lat_lo = int(math.floor(lat_s))
    lat_hi = int(math.floor(lat_n))
    lon_lo = int(math.floor(lon_w))
    lon_hi = int(math.floor(lon_e))
    names = []
    for lat in range(lat_lo, lat_hi + 1):
        for lon in range(lon_lo, lon_hi + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            names.append(f"dem_{ns}{abs(lat):02d}_{ew}{abs(lon):03d}.tif")
    return names


def build_dem_mtime_index() -> dict[str, float]:
    """Scan ``terrain_cache/dem_sources/*.tif`` once. Returns
    ``{filename: mtime}``."""
    index = {}
    for f in glob.glob(str(DEM_DIR / "*.tif")):
        try:
            index[os.path.basename(f)] = os.path.getmtime(f)
        except OSError:
            pass
    return index


def _check_tile(args) -> tuple:
    """Worker: return (z, x, y, tile_path, stale_reason_or_None).

    Stale reasons:
      'tile_missing' — file doesn't exist
      'dem_newer'    — covering DEM has newer mtime than tile
      'zero_fill'    — tile has >20% zero pixels AND max elev > 100
                       (the VRT-edge-coverage bug pattern)
    """
    z, x, y, tile_path, dem_index, newest_dem_mtime, check_content = args
    try:
        tile_mtime = os.path.getmtime(tile_path)
    except OSError:
        return (z, x, y, tile_path, "tile_missing")
    # Freshness check: if ANY covering DEM is newer than tile, stale.
    # Short-circuit: if tile is newer than ANY DEM ever written,
    # skip the per-DEM walk.
    if tile_mtime < newest_dem_mtime:
        for name in covering_dem_names(z, x, y):
            m = dem_index.get(name)
            if m is None:
                continue
            if m > tile_mtime:
                return (z, x, y, tile_path, "dem_newer")
    # Content check: flag the VRT-edge-coverage bug — zero pixels
    # form a rectangular swath at the LEFT or RIGHT edge of the tile
    # (east/west bbox boundary) while the rest has real elevation.
    #
    # Key distinction from ocean tiles and polar-zero rows:
    #   - Polar (ocean at high lat): zero TOP or BOTTOM rows → OK
    #   - Coast: zero pixels scattered, not contiguous full-edge → OK
    #   - Bbox-edge bug (the Iran 33°N + Butte MT stripe, and the
    #     eastern Iran z4 tile going past 63.5°E): contiguous zero
    #     COLUMNS at left (col 0..N) or right (col 256-N..255) edge
    #     → flag.
    #
    # Also catches tiles truncated by a killed-mid-write regen worker:
    # PIL fails to decode the partial WEBP → return "decode_error"
    # which the --regenerate pass cleans up atomically.
    if check_content:
        try:
            import numpy as np
            from PIL import Image
            im = Image.open(tile_path)
            im.load()  # force decode so truncated files raise here
            im = im.convert("RGB")
            arr = np.array(im).astype(np.int64)
            elev = -10000 + (arr[:, :, 0] * 65536 + arr[:, :, 1] * 256
                             + arr[:, :, 2]) * 0.1
            max_elev = elev.max()
            if max_elev > 100:
                h, w = elev.shape
                col_all_zero = (np.sum(np.abs(elev) < 5, axis=0) == h)
                # Left-edge zero block: leading all-zero columns (0..k)
                left = 0
                while left < w and col_all_zero[left]:
                    left += 1
                # Right-edge zero block: trailing all-zero columns (k..w-1)
                right = 0
                while right < w and col_all_zero[w - 1 - right]:
                    right += 1
                # Flag if either edge has a substantial block (>10
                # columns = ~4% of tile), which signals the bbox-edge
                # bug — never caused by real ocean (which scatters
                # within land, not in a full vertical column).
                if left >= 10 or right >= 10:
                    return (z, x, y, tile_path, "zero_fill")
        except Exception:
            # Typical cause: tile truncated by a killed-mid-write
            # regen worker. Tagged separately from zero_fill so we
            # can report it clearly; cleanup path is the same (regen
            # overwrites it atomically now).
            return (z, x, y, tile_path, "decode_error")
    return (z, x, y, tile_path, None)


_VRT_CACHE = {}  # per-worker cache: vrt_path -> open rasterio dataset


def _get_vrt(vrt_path: str):
    """Cache the open rasterio dataset per-worker. The 11 MB
    comprehensive.vrt references 26K TIFs; GDAL's XML parse on each
    open was a ~1s-per-tile bottleneck. Reusing the same handle drops
    it to ~0."""
    import rasterio
    src = _VRT_CACHE.get(vrt_path)
    if src is None:
        src = rasterio.open(vrt_path)
        _VRT_CACHE[vrt_path] = src
    return src


def _regen_tile(args) -> tuple[int, int, int]:
    """Worker: regenerate a single tile from the world VRT with a
    2-pixel halo (same technique as ``fix_terrain_seams.py``)."""
    z, x, y, vrt_path, terrain_dir = args
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject, transform_bounds

    tb_west, tb_south, tb_east, tb_north = tile_to_bounds(z, x, y)
    t3 = transform_bounds("EPSG:4326", "EPSG:3857",
                          tb_west, tb_south, tb_east, tb_north)
    w3, s3, e3, n3 = t3
    px_w = (e3 - w3) / 256.0
    px_h = (n3 - s3) / 256.0
    HALO = 2
    buf = (w3 - HALO * px_w, s3 - HALO * px_h,
           e3 + HALO * px_w, n3 + HALO * px_h)
    BUF = 256 + 2 * HALO
    tile_transform = from_bounds(*buf, BUF, BUF)

    elevation = np.zeros((1, BUF, BUF), dtype=np.float32)
    src = _get_vrt(vrt_path)
    reproject(
        source=rasterio.band(src, 1),
        destination=elevation,
        dst_transform=tile_transform,
        dst_crs="EPSG:3857",
        resampling=Resampling.cubic,
    )
    elev = elevation[0, HALO:HALO + 256, HALO:HALO + 256]
    elev = np.round(elev / 10.0) * 10.0
    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)
    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)
    img = Image.fromarray(np.stack([r, g, b], axis=-1))
    tile_dir = os.path.join(terrain_dir, str(z), str(x))
    os.makedirs(tile_dir, exist_ok=True)
    # Atomic write: PIL.Image.save() isn't atomic — if this worker is
    # killed mid-write (which happened when I was iterating on worker
    # counts), the destination file is truncated and libwebp fails to
    # decode it. Write to a sibling .tmp and os.replace() — the replace
    # is atomic on POSIX, so readers either see the full old file or
    # the full new file, never a partial.
    dst = os.path.join(tile_dir, f"{y}.webp")
    tmp = dst + ".regen-tmp"
    img.save(tmp, "WEBP", lossless=True)
    os.replace(tmp, dst)
    return (z, x, y)


def iter_tiles_in_bbox(bbox, zooms):
    """Yield ``(z, x, y, path)`` for every terrain tile the build *will*
    emit for this bbox — i.e., every Mercator tile intersecting the
    bbox, whether or not the tile currently exists on disk. A missing
    tile is as much a pre-build failure as a stale one; the build will
    need to generate it, which takes minutes-to-hours. Better to know
    before we start."""
    import mercantile
    for z in zooms:
        for t in mercantile.tiles(*bbox, zooms=z):
            p = os.path.join(TERRAIN_DIR, str(z), str(t.x), f"{t.y}.webp")
            yield (z, t.x, t.y, p)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="all",
                    help="region name or 'all'")
    ap.add_argument("--bbox",
                    help="explicit bbox, minlon,minlat,maxlon,maxlat — "
                         "overrides --region")
    ap.add_argument("--zooms", default="0-12",
                    help="zoom range, e.g. '0-12' or '0-8'")
    ap.add_argument("--workers", type=int,
                    default=max(2, multiprocessing.cpu_count() - 1))
    ap.add_argument("--regenerate", action="store_true",
                    help="Regenerate every stale tile from "
                         "comprehensive.vrt (world-spanning mosaic). "
                         "Without this flag, the script only AUDITS — "
                         "exits nonzero on staleness so build scripts "
                         "can refuse to proceed.")
    ap.add_argument("--check-content", action="store_true",
                    help="Also flag tiles with the zero-fill pattern "
                         "(>20%% zero pixels and max elev > 100m) — "
                         "cheap per-tile check that catches the "
                         "Iran-stripe / Butte-MT-stripe bug.")
    ap.add_argument("--vrt", default=str(WORLD_VRT))
    ap.add_argument("--json",  action="store_true",
                    help="Emit the list of stale tiles as JSON on stdout")
    args = ap.parse_args()

    # Gather bbox list.
    if args.bbox:
        bboxes = [("cli", tuple(float(v) for v in args.bbox.split(",")))]
    elif args.region == "all":
        bboxes = list(REGIONS.items())
    else:
        if args.region not in REGIONS:
            ap.error(f"unknown region {args.region!r}; "
                     f"pick from {sorted(REGIONS)}")
        bboxes = [(args.region, REGIONS[args.region])]

    zoom_lo, _, zoom_hi = args.zooms.partition("-")
    zoom_lo, zoom_hi = int(zoom_lo), int(zoom_hi or zoom_lo)
    zooms = range(zoom_lo, zoom_hi + 1)

    if not os.path.isfile(args.vrt):
        print(f"[FATAL] world VRT not found: {args.vrt}", file=sys.stderr)
        return 2

    print(f"building DEM mtime index from {DEM_DIR}...")
    dem_index = build_dem_mtime_index()
    newest = max(dem_index.values()) if dem_index else 0.0
    import datetime
    print(f"  {len(dem_index):,} DEM files; "
          f"newest = {datetime.datetime.fromtimestamp(newest).isoformat()}")

    # Enumerate tiles to check.
    jobs = []
    seen = set()
    for name, bbox in bboxes:
        for z, x, y, p in iter_tiles_in_bbox(bbox, zooms):
            if (z, x, y) in seen: continue
            seen.add((z, x, y))
            jobs.append((z, x, y, p, dem_index, newest, args.check_content))
    print(f"checking {len(jobs):,} existing terrain tiles "
          f"across {len(bboxes)} region(s), z={zoom_lo}-{zoom_hi} "
          f"(using {args.workers} workers)...")

    t0 = time.time()
    stale = []
    with Pool(max_workers=args.workers) as pool:
        for res in pool.map(_check_tile, jobs, chunksize=256):
            _, _, _, _, reason = res
            if reason is not None:
                stale.append(res)
    dt = time.time() - t0
    rate = len(jobs) / max(0.001, dt)
    print(f"audit done in {dt:.1f}s ({rate:,.0f} tiles/s)")

    # Group by zoom for the report.
    by_reason = {}
    by_zoom = {}
    for z, x, y, p, r in stale:
        by_reason[r] = by_reason.get(r, 0) + 1
        by_zoom[z] = by_zoom.get(z, 0) + 1

    if not stale:
        print("RESULT: all tiles fresh — no covering DEM newer than its tile")
        if args.json:
            import json as _j
            print(_j.dumps({"stale": 0, "checked": len(jobs)}))
        return 0

    print(f"RESULT: {len(stale):,} stale tile(s) across "
          f"z={sorted(by_zoom)}:")
    for z in sorted(by_zoom):
        print(f"  z={z}: {by_zoom[z]:,}")
    for r, n in sorted(by_reason.items()):
        print(f"  reason {r}: {n:,}")

    if args.json:
        import json as _j
        print(_j.dumps({
            "stale":   len(stale),
            "checked": len(jobs),
            "by_zoom": by_zoom,
            "by_reason": by_reason,
            "sample": [(z, x, y, r) for z, x, y, p, r in stale[:50]],
        }))

    if not args.regenerate:
        return 1

    # Regeneration.
    regen_jobs = [(z, x, y, args.vrt, str(TERRAIN_DIR))
                  for z, x, y, _, _ in stale]
    print(f"regenerating {len(regen_jobs):,} tiles from {args.vrt}...")
    t1 = time.time()
    done = 0
    with Pool(max_workers=args.workers) as pool:
        for _ in pool.map(_regen_tile, regen_jobs, chunksize=16):
            done += 1
            if done % 200 == 0:
                rate = done / max(0.001, time.time() - t1)
                print(f"  {done}/{len(regen_jobs)} ({rate:.1f}/s)",
                      flush=True)
    print(f"regen done in {time.time()-t1:.1f}s")

    # Re-audit to prove 0 stale now.
    print("re-auditing to confirm zero stale remaining...")
    # Build fresh mtime index (mtimes changed).
    dem_index2 = build_dem_mtime_index()
    newest2 = max(dem_index2.values()) if dem_index2 else 0.0
    jobs2 = [(z, x, y, p, dem_index2, newest2, args.check_content)
             for z, x, y, p, _ in stale]
    still_stale = []
    with Pool(max_workers=args.workers) as pool:
        for res in pool.map(_check_tile, jobs2, chunksize=256):
            _, _, _, _, reason = res
            if reason is not None:
                still_stale.append(res)
    if still_stale:
        print(f"[FAIL] {len(still_stale):,} tile(s) still stale after regen")
        return 1
    print("RESULT: all regen'd tiles now fresh — OK to build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
