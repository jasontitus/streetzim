"""Regenerate stale zero-filled terrain tiles anywhere in ``terrain_cache/``.

The "stale" case we target: tiles generated before the DEM cache had
full world coverage. They have large zero-filled regions where real
land is now available in ``terrain_cache/dem_sources/``. Examples:
Iran's 33°N stripe (pre-fix), Butte MT stripe on central-us, and
similar striping across 6+ other regions.

This is a peer of ``fix_terrain_seams.py`` but reuses the already-built
``comprehensive.vrt`` (world-spanning DEM mosaic) instead of rebuilding
per-bbox — that way low-zoom tiles (z0-z4) that span beyond any region
still get real elevation everywhere DEMs exist, not zeroes outside the
region being fixed.

Usage:
  python cloud/fix_stale_terrain_tiles.py               # sweep all regions
  python cloud/fix_stale_terrain_tiles.py --zero-pct 40 --max-elev 500
  python cloud/fix_stale_terrain_tiles.py --region central-us
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
import glob
from concurrent.futures import ProcessPoolExecutor as Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEM_DIR = ROOT / "terrain_cache" / "dem_sources"
TERRAIN_DIR = ROOT / "terrain_cache"

# World-spanning VRT already built by the main pipeline. Covers every
# DEM that was ever downloaded, not just one region.
WORLD_VRT = DEM_DIR / "comprehensive.vrt"

REGIONS = {
    "australia-nz":  (112.0, -47.5, 179.5, -10.0),
    "texas":         (-107.0, 25.5, -93.4, 36.6),
    "west-coast-us": (-125.0, 32.0, -114.0, 46.0),
    "east-coast-us": (-82.0, 24.0, -66.5, 47.6),
    "central-us":    (-120.0, 31.3, -104.0, 49.0),
    "iran":          (44.0, 25.0, 63.5, 39.8),
    "japan":         (122.9, 24.0, 146.0, 45.6),
}


def ll_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 1 << z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def tile_in_bbox(z: int, x: int, y: int, bbox: tuple[float, float, float, float]) -> bool:
    xmin = ll_to_tile(bbox[0], bbox[3], z)[0]
    xmax = ll_to_tile(bbox[2], bbox[1], z)[0]
    ymin = ll_to_tile(bbox[0], bbox[3], z)[1]
    ymax = ll_to_tile(bbox[0], bbox[1], z)[1]
    return xmin <= x <= xmax and ymin <= y <= ymax


def tile_to_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z
    lon_w = x / n * 360.0 - 180.0
    lon_e = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (lon_w, lat_s, lon_e, lat_n)


def scan_bad_tile(args) -> tuple | None:
    """Worker: open tile, return bad record if zero%>threshold AND max>min_elev."""
    z, x, y, path, zero_pct_min, max_elev_min = args
    import numpy as np
    from PIL import Image
    try:
        im = Image.open(path).convert("RGB")
        arr = np.array(im).astype(np.int64)
        elev = -10000 + (arr[:, :, 0] * 65536 + arr[:, :, 1] * 256 + arr[:, :, 2]) * 0.1
        pct_zero = 100.0 * np.sum(np.abs(elev) < 5) / elev.size
        if pct_zero >= zero_pct_min and elev.max() >= max_elev_min:
            return (z, x, y, path, float(pct_zero), float(elev.max()))
    except Exception:
        return None
    return None


def regen_one(args) -> tuple[int, int, int, str]:
    """Regenerate a single tile using the world VRT, with 2-pixel halo.

    Halo eliminates seams at tile boundaries (cubic resampling's 4-pixel
    kernel otherwise samples different neighbors on adjacent tiles).
    """
    z, x, y, vrt_path, terrain_dir = args
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject, transform_bounds

    tb_west, tb_south, tb_east, tb_north = tile_to_bounds(z, x, y)
    tile_bounds_3857 = transform_bounds(
        "EPSG:4326", "EPSG:3857", tb_west, tb_south, tb_east, tb_north
    )
    w3, s3, e3, n3 = tile_bounds_3857
    px_w = (e3 - w3) / 256.0
    px_h = (n3 - s3) / 256.0
    HALO = 2
    buf = (w3 - HALO * px_w, s3 - HALO * px_h, e3 + HALO * px_w, n3 + HALO * px_h)
    BUF = 256 + 2 * HALO
    tile_transform = from_bounds(*buf, BUF, BUF)

    elevation = np.zeros((1, BUF, BUF), dtype=np.float32)
    with rasterio.open(vrt_path) as src:
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
    out = os.path.join(tile_dir, f"{y}.webp")
    img.save(out, "WEBP", lossless=True)
    return (z, x, y, out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="all",
                    choices=["all"] + list(REGIONS))
    ap.add_argument("--zooms", default="0-8")
    ap.add_argument("--zero-pct", type=float, default=40,
                    help="Min %% of zero-elevation pixels to flag as bad")
    ap.add_argument("--max-elev", type=float, default=500,
                    help="Min max-elevation to confirm real land nearby")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--vrt", default=str(WORLD_VRT))
    ap.add_argument("--dry-run", action="store_true",
                    help="Identify bad tiles but don't regenerate")
    args = ap.parse_args()

    zoom_lo, _, zoom_hi = args.zooms.partition("-")
    zoom_lo, zoom_hi = int(zoom_lo), int(zoom_hi or zoom_lo)

    if not os.path.isfile(args.vrt):
        print(f"[FATAL] world VRT not found: {args.vrt}", file=sys.stderr)
        return 2

    regions = [args.region] if args.region != "all" else list(REGIONS)

    # Pass 1: scan.
    print(f"scanning for bad tiles (zero%>={args.zero_pct}, max>={args.max_elev}) "
          f"across {len(regions)} region(s), z={zoom_lo}-{zoom_hi}...")
    scan_jobs = []
    seen = set()
    for z in range(zoom_lo, zoom_hi + 1):
        for x_dir in glob.glob(f"{TERRAIN_DIR}/{z}/*"):
            try: x = int(os.path.basename(x_dir))
            except: continue
            for f in glob.glob(f"{x_dir}/*.webp"):
                try: y = int(os.path.basename(f)[:-5])
                except: continue
                if (z, x, y) in seen: continue
                # Keep tiles that fall in any target region.
                for rname in regions:
                    if tile_in_bbox(z, x, y, REGIONS[rname]):
                        seen.add((z, x, y))
                        scan_jobs.append((z, x, y, f, args.zero_pct, args.max_elev))
                        break
    print(f"  {len(scan_jobs):,} tiles to inspect")

    t0 = time.time()
    bad = []
    with Pool(max_workers=args.workers) as pool:
        for res in pool.map(scan_bad_tile, scan_jobs, chunksize=64):
            if res: bad.append(res)
    print(f"  scan done in {time.time()-t0:.0f}s — {len(bad)} bad tile(s)")

    if not bad:
        print("no bad tiles found, nothing to do")
        return 0

    # Report bad tiles grouped by zoom.
    by_z = {}
    for z, x, y, *_ in bad:
        by_z.setdefault(z, []).append((x, y))
    for z in sorted(by_z):
        print(f"  z={z}: {len(by_z[z])} tile(s)")
    if args.dry_run:
        print("(dry-run) not regenerating")
        return 0

    # Pass 2: regenerate using the world VRT.
    regen_jobs = [(z, x, y, args.vrt, str(TERRAIN_DIR)) for z, x, y, *_ in bad]
    print(f"regenerating {len(regen_jobs):,} tiles with {args.workers} workers...")
    t1 = time.time()
    done = 0
    with Pool(max_workers=args.workers) as pool:
        for _ in pool.map(regen_one, regen_jobs, chunksize=16):
            done += 1
            if done % 50 == 0:
                rate = done / max(0.001, time.time() - t1)
                print(f"  {done}/{len(regen_jobs)} ({rate:.1f}/s)", flush=True)
    print(f"done in {time.time()-t1:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
