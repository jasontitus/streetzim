#!/usr/bin/env python3
"""Fix terrain tiles at DEM cell boundaries.

Tiles that straddle 1-degree DEM cell boundaries may have partial/zero
data if they were generated from a VRT that didn't include all needed
DEM cells. This script:

1. Builds a comprehensive VRT from ALL available DEM sources
2. Identifies tiles at 1-degree boundaries (lat and lon)
3. Regenerates ONLY those boundary tiles from the comprehensive VRT
4. Skips ocean tiles (no DEM data)

Usage:
    python3 fix_boundary_terrain.py                    # fix all cached regions
    python3 fix_boundary_terrain.py --bbox "122.9,24.0,146.0,45.6"  # fix specific bbox
"""
import argparse
import glob
import math
import os
import subprocess
import sys
import tempfile
from multiprocessing import Pool

CACHE = 'terrain_cache'
DEM_DIR = os.path.join(CACHE, 'dem_sources')


def build_comprehensive_vrt():
    """Build a VRT from ALL DEM source files."""
    tif_paths = sorted(p for p in glob.glob(os.path.join(DEM_DIR, 'dem_*.tif'))
                       if os.path.getsize(p) > 1000)
    if not tif_paths:
        print("No DEM sources found")
        sys.exit(1)
    vrt_path = os.path.join(DEM_DIR, 'comprehensive.vrt')
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write('\n'.join(tif_paths))
        flist = f.name
    subprocess.run(['gdalbuildvrt', '-overwrite', '-input_file_list', flist, vrt_path],
                   capture_output=True, check=True)
    os.unlink(flist)
    print(f'Built comprehensive VRT from {len(tif_paths)} DEM files')
    return vrt_path


def get_boundary_tiles(bbox, max_zoom=12):
    """Find all tiles that straddle a 1-degree DEM cell boundary."""
    import mercantile
    w, s, e, n = bbox
    boundary_tiles = []

    for z in range(0, max_zoom + 1):
        tiles = list(mercantile.tiles(w, s, e, n, zooms=z))
        for t in tiles:
            tb = mercantile.bounds(t)
            # Check if tile crosses a 1-degree boundary
            crosses_lon = math.floor(tb.west) != math.floor(tb.east)
            crosses_lat = math.floor(tb.south) != math.floor(tb.north)
            if crosses_lon or crosses_lat:
                boundary_tiles.append((t.z, t.x, t.y, tb))
    return boundary_tiles


def regen_tile(args):
    """Regenerate a single terrain tile from the comprehensive VRT."""
    import rasterio
    from rasterio.warp import reproject, Resampling, transform_bounds
    from rasterio.transform import from_bounds
    import numpy as np
    from PIL import Image

    vrt_path, z, x, y, cache_dir = args
    tb_west, tb_south, tb_east, tb_north = None, None, None, None

    import mercantile
    tb = mercantile.bounds(x, y, z)
    tb_west, tb_south, tb_east, tb_north = tb.west, tb.south, tb.east, tb.north

    tile_bounds_3857 = transform_bounds('EPSG:4326', 'EPSG:3857',
                                        tb_west, tb_south, tb_east, tb_north)
    tile_transform = from_bounds(*tile_bounds_3857, 256, 256)

    elevation = np.zeros((1, 256, 256), dtype=np.float32)
    with rasterio.open(vrt_path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=elevation,
            dst_transform=tile_transform,
            dst_crs='EPSG:3857',
            resampling=Resampling.cubic,
        )

    elev = elevation[0]
    if (elev == 0).all():
        return 0  # All ocean — skip writing

    elev = np.round(elev / 10.0) * 10.0
    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)
    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)

    img = Image.fromarray(np.stack([r, g, b], axis=-1))
    tile_dir = os.path.join(cache_dir, str(z), str(x))
    os.makedirs(tile_dir, exist_ok=True)
    img.save(os.path.join(tile_dir, f'{y}.webp'), 'WEBP', lossless=True)
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bbox', help='Specific bbox to fix (default: all cached regions)')
    parser.add_argument('--max-zoom', type=int, default=12)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()

    vrt_path = build_comprehensive_vrt()

    # Define regions to fix
    if args.bbox:
        bboxes = [tuple(float(x) for x in args.bbox.split(','))]
    else:
        # All our uploaded regions
        bboxes = [
            (-125.0, 24.4, -66.9, 49.4),    # US
            (-25.0, 34.0, 50.5, 72.0),       # Europe
            (25.0, 12.0, 62.5, 42.0),        # West Asia
            (-18.0, -35.0, 52.0, 38.0),      # Africa
            (122.9, 24.0, 146.0, 45.6),      # Japan
            (110.0, -50.0, 180.0, -8.0),     # Aus/NZ
            (-104.1, 36.0, -80.5, 49.4),     # Midwest
            (-124.48, 32.53, -114.13, 42.01), # California
            (-106.65, 25.84, -93.51, 36.50), # Texas
            (-124.8, 32.5, -114.1, 49.0),    # West Coast
            (-81.7, 24.5, -66.9, 47.5),      # East Coast
        ]

    # Collect all boundary tiles across all regions (deduplicated)
    all_boundary = set()
    for bbox in bboxes:
        tiles = get_boundary_tiles(bbox, args.max_zoom)
        for z, x, y, tb in tiles:
            all_boundary.add((z, x, y))

    print(f'Found {len(all_boundary)} unique boundary tiles across {len(bboxes)} regions')

    # Prepare work items
    work = [(vrt_path, z, x, y, CACHE) for z, x, y in sorted(all_boundary)]

    # Process in parallel
    regenerated = 0
    skipped_ocean = 0
    with Pool(args.workers) as pool:
        for i, result in enumerate(pool.imap_unordered(regen_tile, work)):
            if result:
                regenerated += 1
            else:
                skipped_ocean += 1
            if (i + 1) % 5000 == 0:
                print(f'  Processed {i+1}/{len(work)}, regenerated {regenerated}, ocean {skipped_ocean}...')

    print(f'\nDone: regenerated {regenerated} boundary tiles, skipped {skipped_ocean} ocean tiles')


if __name__ == '__main__':
    main()
