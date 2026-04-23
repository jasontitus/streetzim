"""Regenerate terrain tiles so adjacent tiles agree at their shared edge.

Fixes the 33°N horizontal line seen on Iran in 3D mode: cubic resampling
in the original _generate_one_terrain_tile uses a 4-pixel kernel that
extends beyond the tile, so tile-above and tile-below sample different
neighbor pixels at their shared boundary. Result: 200–300 m elevation
discontinuity that renders as a visible cliff.

Fix: rasterize each tile on a 260×260 grid with a 2-pixel halo, then
crop the center 256×256. Adjacent tiles' halos cover the same VRT
pixels at their shared edge, so cubic resampling sees identical neighbor
data and produces matching edge values.

Usage:
  python cloud/fix_terrain_seams.py --bbox "44,25,63.5,39.8" --zooms 0-12
  python cloud/fix_terrain_seams.py --bbox "44,25,63.5,39.8" --zooms 7-12  # faster, only user-visible zooms

Operates against the shared ``terrain_cache/`` — overwrites tiles in
place. Rebuild the VRT from existing DEM sources (no re-download).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor as Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEM_DIR = ROOT / "terrain_cache" / "dem_sources"
TERRAIN_DIR = ROOT / "terrain_cache"


def generate_tile_buffered(args) -> None:
    """Buffered counterpart of create_osm_zim._generate_one_terrain_tile.

    Differences:
    - Rasterizes at 260×260 with a 2-pixel halo on each side (total 4 px
      of buffer). Cubic's 4-neighbor kernel can then address its full
      window without crossing tile boundaries.
    - Crops to the center 256×256 before encoding. Adjacent tiles sample
      the SAME VRT pixels in their halos, so their shared edge columns/
      rows come out byte-identical (post quantization).
    """
    mosaic_file, tile_x, tile_y, z, dest_dir_local, tb_west, tb_south, tb_east, tb_north = args
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject, transform_bounds

    # 2-pixel halo: extend the Mercator bounds by 2 tile-pixels on each
    # side. A z-level tile spans one Mercator unit divided by 2^z, so
    # we convert to bounds in 3857 then back.
    tile_bounds_3857 = transform_bounds(
        "EPSG:4326", "EPSG:3857", tb_west, tb_south, tb_east, tb_north
    )
    west3857, south3857, east3857, north3857 = tile_bounds_3857
    px_w = (east3857 - west3857) / 256.0
    px_h = (north3857 - south3857) / 256.0
    HALO = 2
    buf_bounds = (
        west3857  - HALO * px_w,
        south3857 - HALO * px_h,
        east3857  + HALO * px_w,
        north3857 + HALO * px_h,
    )
    BUF = 256 + 2 * HALO
    tile_transform = from_bounds(*buf_bounds, BUF, BUF)

    elevation = np.zeros((1, BUF, BUF), dtype=np.float32)
    with rasterio.open(mosaic_file) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=elevation,
            dst_transform=tile_transform,
            dst_crs="EPSG:3857",
            resampling=Resampling.cubic,
        )
    elev = elevation[0, HALO:HALO + 256, HALO:HALO + 256]   # crop center

    elev = np.round(elev / 10.0) * 10.0
    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)
    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)
    img = Image.fromarray(np.stack([r, g, b], axis=-1))

    tile_dir = os.path.join(dest_dir_local, str(z), str(tile_x))
    os.makedirs(tile_dir, exist_ok=True)
    img.save(os.path.join(tile_dir, f"{tile_y}.webp"), "WEBP", lossless=True)


def build_vrt(bbox: tuple[float, float, float, float], vrt_path: str) -> str:
    """Rebuild a VRT spanning the bbox + 1° buffer from cached DEMs."""
    import subprocess
    minlon, minlat, maxlon, maxlat = bbox
    tifs = []
    for lat in range(math.floor(minlat) - 1, math.floor(maxlat) + 2):
        for lon in range(math.floor(minlon) - 1, math.floor(maxlon) + 2):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            fname = f"dem_{ns}{abs(lat):02d}_{ew}{abs(lon):03d}.tif"
            p = DEM_DIR / fname
            if p.is_file() and p.stat().st_size > 1000:
                tifs.append(str(p))
    if not tifs:
        raise RuntimeError(f"no DEM sources in bbox {bbox}")
    # gdalbuildvrt via subprocess — same tool create_osm_zim uses.
    with open(vrt_path + ".filelist", "w") as fh:
        fh.write("\n".join(tifs))
    subprocess.run(
        ["gdalbuildvrt", "-input_file_list", vrt_path + ".filelist", vrt_path],
        check=True, capture_output=True,
    )
    os.unlink(vrt_path + ".filelist")
    print(f"VRT from {len(tifs)} DEMs → {vrt_path}")
    return vrt_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", required=True,
                    help="minlon,minlat,maxlon,maxlat")
    ap.add_argument("--zooms", default="0-12",
                    help="zoom range, e.g. '0-12' or '7-12'")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--terrain-dir", default=str(TERRAIN_DIR))
    args = ap.parse_args()

    bbox = tuple(float(v) for v in args.bbox.split(","))
    if len(bbox) != 4:
        ap.error("bbox needs 4 floats")
    zoom_lo, _, zoom_hi = args.zooms.partition("-")
    zoom_lo, zoom_hi = int(zoom_lo), int(zoom_hi or zoom_lo)

    import tempfile
    import mercantile
    with tempfile.TemporaryDirectory() as tmp:
        vrt_path = os.path.join(tmp, "fix.vrt")
        build_vrt(bbox, vrt_path)

        jobs = []
        for z in range(zoom_lo, zoom_hi + 1):
            for t in mercantile.tiles(*bbox, zooms=z):
                bnds = mercantile.bounds(t)
                jobs.append((vrt_path, t.x, t.y, z, args.terrain_dir,
                             bnds.west, bnds.south, bnds.east, bnds.north))
        print(f"regenerating {len(jobs):,} tiles with {args.workers} workers...")
        t0 = time.time()
        with Pool(max_workers=args.workers) as pool:
            for i, _ in enumerate(pool.map(generate_tile_buffered, jobs,
                                            chunksize=64)):
                if (i + 1) % 5000 == 0:
                    print(f"  {i+1}/{len(jobs)} "
                          f"({(i+1)/(time.time()-t0):.0f}/s)", flush=True)
        print(f"done in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
