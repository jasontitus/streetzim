"""Regenerate EVERY existing z=0..N terrain cache tile from the
world-coverage DEM.

Use this when region-bbox-based regen leaves behind stale tiles that
the build emitted beyond the declared bbox (e.g. terrain/7/19/43 at
50°N for a 32-46°N region — create_osm_zim pads the tile selection
slightly for map-edge smoothing).

No bbox filter — we walk the filesystem. If it exists at z<=max-z,
it gets regen'd atomically from the VRT.
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor as Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERRAIN = ROOT / "terrain_cache"


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
    z, x, y, vrt_path, terrain_dir = args
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
    tile_dir = os.path.join(terrain_dir, str(z), str(x))
    os.makedirs(tile_dir, exist_ok=True)
    dst = os.path.join(tile_dir, f"{y}.webp")
    tmp = dst + ".regen-tmp"
    img.save(tmp, "WEBP", lossless=True)
    os.replace(tmp, dst)
    return (z, x, y)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-z", type=int, default=7)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--vrt", required=True)
    args = ap.parse_args()

    if not Path(args.vrt).is_file():
        print(f"[FATAL] {args.vrt} missing", file=sys.stderr)
        return 2

    jobs = []
    for z in range(0, args.max_z + 1):
        for x_dir in glob.glob(str(TERRAIN / str(z) / "*")):
            try:
                x = int(os.path.basename(x_dir))
            except ValueError:
                continue
            for f in glob.glob(f"{x_dir}/*.webp"):
                try:
                    y = int(os.path.basename(f)[:-5])
                except ValueError:
                    continue
                jobs.append((z, x, y, args.vrt, str(TERRAIN)))

    print(f"regenerating {len(jobs):,} existing z0-z{args.max_z} tiles "
          f"from {args.vrt} with {args.workers} workers")
    t0 = time.time()
    done = 0
    with Pool(max_workers=args.workers) as pool:
        for _ in pool.map(regen, jobs, chunksize=1):
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(jobs)}", flush=True)
    print(f"done in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
