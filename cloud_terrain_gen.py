#!/usr/bin/env python3
"""
Standalone terrain tile generator for cloud instances.

Reads Copernicus GLO-30 DEM directly from AWS S3 (no local DEM files needed),
generates terrain-RGB WebP tiles, and stores them locally for rsync back.

Usage:
    # Install dependencies
    pip install rasterio mercantile Pillow numpy boto3

    # Generate all world z12 tiles (skip existing)
    python3 cloud_terrain_gen.py --zoom 12 --workers 64 --output terrain_tiles/

    # Generate a specific zoom range
    python3 cloud_terrain_gen.py --zoom 10,11,12 --workers 128 --output terrain_tiles/

    # Then rsync results back to your local machine:
    rsync -avz terrain_tiles/ user@local:experiments/streetzim/terrain_cache/

Architecture:
    - Reads DEM tiles on-demand from s3://copernicus-dem-30m/ (public, no credentials needed)
    - Each worker opens its own S3 connection (no shared state)
    - Tiles are written as lossless WebP to output/{z}/{x}/{y}.webp
    - Existing tiles are skipped (safe to restart)
"""

import argparse
import math
import os
import sys
import time
from multiprocessing import Pool, Value, Lock

# Copernicus GLO-30 DEM on AWS (public bucket, requester-pays NOT required)
S3_BUCKET = "copernicus-dem-30m"
S3_PREFIX = "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"

counter = None
counter_lock = None


def init_worker(c, l):
    global counter, counter_lock
    counter = c
    counter_lock = l


def _elev_to_terrain_rgb(elev):
    """Convert elevation in meters to Mapbox terrain-RGB encoding."""
    import numpy as np
    elev = np.round(elev / 10.0) * 10.0  # quantize to 10m
    v = ((elev + 10000) * 10).astype(np.int32)
    v = np.clip(v, 0, 16777215)
    r = (v >> 16) & 0xFF
    g = (v >> 8) & 0xFF
    b = v & 0xFF
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def generate_tile(args):
    """Generate a single terrain-RGB tile from S3-hosted DEM data."""
    tile_x, tile_y, z, output_dir = args
    import rasterio
    from rasterio.warp import reproject, Resampling
    from rasterio.transform import from_bounds
    import mercantile
    import numpy as np
    from PIL import Image
    import io

    tile_path = os.path.join(output_dir, str(z), str(tile_x), f"{tile_y}.webp")
    if os.path.isfile(tile_path):
        with counter_lock:
            counter.value += 1
        return True  # cached

    # Get tile bounds
    bounds = mercantile.bounds(mercantile.Tile(tile_x, tile_y, z))

    # Determine which DEM tiles we need
    dem_urls = []
    for lat in range(math.floor(bounds.south), math.floor(bounds.north) + 1):
        for lon in range(math.floor(bounds.west), math.floor(bounds.east) + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            key = S3_PREFIX.format(ns=ns, lat=abs(lat), ew=ew, lon=abs(lon))
            dem_urls.append(f"/vsis3/{S3_BUCKET}/{key}")

    # Read and reproject DEM data to tile
    tile_size = 256
    dst_transform = from_bounds(bounds.west, bounds.south, bounds.east, bounds.north,
                                 tile_size, tile_size)
    dst_crs = "EPSG:4326"
    dst_arr = np.full((tile_size, tile_size), -10000, dtype=np.float32)

    for dem_url in dem_urls:
        try:
            with rasterio.open(dem_url) as src:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=dst_arr,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs=dst_crs,
                    resampling=Resampling.bilinear,
                    dst_nodata=-10000,
                )
        except Exception:
            continue  # DEM doesn't exist (ocean tile)

    # Skip all-nodata tiles (open ocean)
    if (dst_arr <= -9999).all():
        with counter_lock:
            counter.value += 1
        return False

    # Convert to terrain-RGB
    dst_arr[dst_arr <= -9999] = 0
    rgb = _elev_to_terrain_rgb(dst_arr)
    img = Image.fromarray(rgb, "RGB")

    # Save as lossless WebP
    os.makedirs(os.path.dirname(tile_path), exist_ok=True)
    img.save(tile_path, "WEBP", lossless=True)

    with counter_lock:
        counter.value += 1
    return True


def main():
    parser = argparse.ArgumentParser(description="Generate terrain-RGB tiles from Copernicus S3")
    parser.add_argument("--zoom", default="12", help="Zoom level(s), comma-separated (default: 12)")
    parser.add_argument("--workers", type=int, default=64, help="Number of parallel workers")
    parser.add_argument("--output", default="terrain_tiles", help="Output directory")
    parser.add_argument("--bbox", default="-180,-85,180,85", help="Bounding box (default: world)")
    parser.add_argument("--dry-run", action="store_true", help="Count tiles without generating")
    args = parser.parse_args()

    zooms = [int(z) for z in args.zoom.split(",")]
    bbox = [float(x) for x in args.bbox.split(",")]
    minlon, minlat, maxlon, maxlat = bbox

    # Configure GDAL for S3 access (public bucket, no credentials needed)
    os.environ["AWS_NO_SIGN_REQUEST"] = "YES"
    os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"
    os.environ["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"] = ".tif"
    os.environ["GDAL_HTTP_MAX_RETRY"] = "3"
    os.environ["GDAL_HTTP_RETRY_DELAY"] = "1"

    import mercantile

    for z in zooms:
        # Build task list, skipping cached tiles
        tasks = []
        cached = 0
        for tile in mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z):
            tile_path = os.path.join(args.output, str(z), str(tile.x), f"{tile.y}.webp")
            if os.path.isfile(tile_path):
                cached += 1
                continue
            tasks.append((tile.x, tile.y, z, args.output))

        total = len(tasks) + cached
        print(f"z{z}: {total:,} tiles ({cached:,} cached, {len(tasks):,} to generate)")

        if args.dry_run or len(tasks) == 0:
            continue

        c = Value('i', 0)
        l = Lock()
        start = time.time()

        last_report = time.time()
        with Pool(args.workers, initializer=init_worker, initargs=(c, l)) as pool:
            for result in pool.imap_unordered(generate_tile, tasks, chunksize=64):
                done = c.value
                now = time.time()
                # Report every 30 seconds or every 1000 tiles
                if (now - last_report >= 30) or (done % 1000 == 0 and done > 0):
                    last_report = now
                    elapsed = now - start
                    rate = done / elapsed
                    eta = (len(tasks) - done) / rate if rate > 0 else 0
                    ts = time.strftime("%H:%M:%S")
                    print(f"\r  [{ts}] z{z}: {done:,}/{len(tasks):,} ({100*done/len(tasks):.1f}%) "
                          f"— {rate:.0f} tiles/s — ETA {eta/60:.0f}m", end="", flush=True)

        elapsed = time.time() - start
        print(f"\n  z{z}: Done in {elapsed/3600:.1f}h ({len(tasks)/elapsed:.0f} tiles/s)")

    # Write completion marker with tile count for verification
    total_tiles = 0
    for root, dirs, files in os.walk(args.output):
        total_tiles += sum(1 for f in files if f.endswith('.webp'))
    marker_path = os.path.join(args.output, 'COMPLETED')
    with open(marker_path, 'w') as f:
        f.write(f"{total_tiles}\n")
    print(f"\nCompleted! {total_tiles} tiles written to {args.output}/")
    print(f"Marker file: {marker_path}")


if __name__ == "__main__":
    main()
