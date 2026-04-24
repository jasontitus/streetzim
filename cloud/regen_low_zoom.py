"""Regenerate every z0-z4 terrain tile in any of our region bboxes,
using the world-coverage comprehensive.vrt — not a regional VRT.

Why: z0-z4 tiles each cover MUCH more than any single region's bbox.
If generated from a regional VRT (Iran-only, etc), the parts of the
tile that extend beyond the region appear as zero-elevation stripes.
That's the visible vertical stripe the user sees at ~65°E in the Iran
and West-Asia ZIMs — the east edge of the z=4 tile extends past Iran's
63.5°E bbox, so those columns are all zero.

Fix: for low-zoom tiles, always use comprehensive.vrt (covers every
1° cell with a downloaded DEM). Cost: ~45 tiles after dedup × slow
reprojection = ~5-10 min on 4 workers.

Usage:
  python cloud/regen_low_zoom.py          # all regions, z0-z4
  python cloud/regen_low_zoom.py --max-z 4
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor as Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERRAIN = ROOT / "terrain_cache"
VRT = TERRAIN / "dem_sources" / "comprehensive.vrt"

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
    "midwest-us":    (-104.1, 36.0, -80.5, 49.4),
    "central-asia":  (51.0, 35.0, 88.0, 56.0),
    "west-asia":     (25.0, 12.0, 63.0, 45.0),
    "africa":        (-18.0, -35.0, 52.0, 38.0),
    "europe":        (-25.0, 34.0, 50.5, 72.0),
    "indian-subcontinent": (60.0, 5.0, 97.5, 37.5),
}

import math

def tile_to_bounds(z, x, y):
    n = 1 << z
    lon_w = x / n * 360.0 - 180.0
    lon_e = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (lon_w, lat_s, lon_e, lat_n)


_VRT = {}


def _get_vrt(vrt_path):
    import rasterio
    v = _VRT.get(vrt_path)
    if v is None:
        v = rasterio.open(vrt_path)
        _VRT[vrt_path] = v
    return v


def regen(args):
    z, x, y, vrt_path = args
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject, transform_bounds

    tb = tile_to_bounds(z, x, y)
    t3 = transform_bounds("EPSG:4326", "EPSG:3857", *tb)
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
    tile_dir = str(TERRAIN / str(z) / str(x))
    os.makedirs(tile_dir, exist_ok=True)
    dst = os.path.join(tile_dir, f"{y}.webp")
    tmp = dst + ".regen-tmp"
    img.save(tmp, "WEBP", lossless=True)
    os.replace(tmp, dst)
    return (z, x, y)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-z", type=int, default=4,
                    help="Regen zooms 0..max-z inclusive. Default 4 — "
                         "z=5+ tiles are small enough that regional VRTs "
                         "are fine.")
    ap.add_argument("--workers", type=int, default=4,
                    help="Keep at 4: rasterio+comprehensive.vrt hits "
                         "5+ GB RSS per worker on low-zoom tiles.")
    ap.add_argument("--vrt", default=str(VRT),
                    help="DEM source. Default comprehensive.vrt (full "
                         "26K DEMs, slow). Pass a downsampled DEM like "
                         "terrain_cache/dem_sources/world_dem_8k.tif "
                         "for ~30-60× faster regen at z=0-4 (each output "
                         "pixel at z=4 is 9.8 km, so 5 km source is more "
                         "than enough).")
    args = ap.parse_args()

    vrt_path = Path(args.vrt)
    if not vrt_path.is_file():
        print(f"[FATAL] {vrt_path} missing", file=sys.stderr)
        return 2

    import mercantile
    seen = set()
    jobs = []
    for bbox in REGIONS.values():
        for z in range(0, args.max_z + 1):
            for t in mercantile.tiles(*bbox, zooms=z):
                k = (z, t.x, t.y)
                if k in seen: continue
                seen.add(k)
                jobs.append((z, t.x, t.y, str(vrt_path)))
    print(f"regenerating {len(jobs):,} z0-z{args.max_z} tiles from "
          f"{VRT.name} with {args.workers} workers")
    t0 = time.time()
    done = 0
    with Pool(max_workers=args.workers) as pool:
        for _ in pool.map(regen, jobs, chunksize=1):
            done += 1
            if done % 10 == 0:
                print(f"  {done}/{len(jobs)}", flush=True)
    print(f"done in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
