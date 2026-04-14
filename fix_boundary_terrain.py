#!/usr/bin/env python3
"""Fix terrain tiles at DEM cell boundaries.

Reads directly from individual DEM cells (not VRT) to avoid the
26K-file VRT performance disaster. Each boundary tile overlaps at most
4 DEM cells — we read from each and composite.
"""
import argparse
import math
import os
import sys
from multiprocessing import Pool

CACHE = 'terrain_cache'
DEM_DIR = os.path.join(CACHE, 'dem_sources')


def _build_dem_index():
    """Build {(lat,lon): path} index of all DEM source files."""
    idx = {}
    for f in os.listdir(DEM_DIR):
        if not f.startswith('dem_') or not f.endswith('.tif'):
            continue
        path = os.path.join(DEM_DIR, f)
        if os.path.getsize(path) < 1000:
            continue
        parts = f.replace('dem_', '').replace('.tif', '').split('_')
        if len(parts) != 2:
            continue
        try:
            lat = int(parts[0][1:]) * (1 if parts[0][0] == 'N' else -1)
            lon = int(parts[1][1:]) * (1 if parts[1][0] == 'E' else -1)
            idx[(lat, lon)] = path
        except ValueError:
            continue
    return idx


# Shared across workers via fork
DEM_INDEX = _build_dem_index()


def _regen_tile(args):
    """Regenerate one boundary tile from individual DEM cells."""
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling, transform_bounds
    from rasterio.transform import from_bounds
    from PIL import Image
    import mercantile

    z, x, y = args
    tb = mercantile.bounds(x, y, z)
    tile_bounds_3857 = transform_bounds('EPSG:4326', 'EPSG:3857',
                                        tb.west, tb.south, tb.east, tb.north)
    tile_transform = from_bounds(*tile_bounds_3857, 256, 256)

    composite = np.zeros((256, 256), dtype=np.float32)
    any_data = False

    for lat in range(int(math.floor(tb.south)), int(math.floor(tb.north)) + 1):
        for lon in range(int(math.floor(tb.west)), int(math.floor(tb.east)) + 1):
            dem_path = DEM_INDEX.get((lat, lon))
            if not dem_path:
                continue
            elev = np.zeros((1, 256, 256), dtype=np.float32)
            try:
                with rasterio.open(dem_path) as src:
                    reproject(source=rasterio.band(src, 1), destination=elev,
                              dst_transform=tile_transform, dst_crs='EPSG:3857',
                              resampling=Resampling.cubic)
            except Exception:
                continue
            mask = elev[0] != 0
            if mask.any():
                composite[mask] = elev[0][mask]
                any_data = True

    if not any_data:
        return 0

    elev = np.round(composite / 10.0) * 10.0
    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)
    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)

    from PIL import Image
    img = Image.fromarray(np.stack([r, g, b], axis=-1))
    tile_dir = os.path.join(CACHE, str(z), str(x))
    os.makedirs(tile_dir, exist_ok=True)
    img.save(os.path.join(tile_dir, f'{y}.webp'), 'WEBP', lossless=True)
    return 1


def get_boundary_tiles(bboxes, max_zoom=12):
    import mercantile
    boundary = set()
    for bbox in bboxes:
        for z in range(0, max_zoom + 1):
            for t in mercantile.tiles(*bbox, zooms=z):
                tb = mercantile.bounds(t)
                if math.floor(tb.west) != math.floor(tb.east) or \
                   math.floor(tb.south) != math.floor(tb.north):
                    boundary.add((t.z, t.x, t.y))
    return sorted(boundary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bbox', help='Specific bbox')
    parser.add_argument('--max-zoom', type=int, default=12)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()

    if args.bbox:
        bboxes = [tuple(float(x) for x in args.bbox.split(','))]
    else:
        bboxes = [
            (-125.0, 24.4, -66.9, 49.4), (-25.0, 34.0, 50.5, 72.0),
            (25.0, 12.0, 62.5, 42.0), (-18.0, -35.0, 52.0, 38.0),
            (122.9, 24.0, 146.0, 45.6), (110.0, -50.0, 180.0, -8.0),
            (-104.1, 36.0, -80.5, 49.4), (-124.48, 32.53, -114.13, 42.01),
            (-106.65, 25.84, -93.51, 36.50), (-124.8, 32.5, -114.1, 49.0),
            (-81.7, 24.5, -66.9, 47.5),
        ]

    print(f'DEM index: {len(DEM_INDEX)} cells')
    tiles = get_boundary_tiles(bboxes, args.max_zoom)
    print(f'Found {len(tiles)} boundary tiles', flush=True)

    # Use threads — rasterio releases GIL during GDAL I/O, so threads
    # provide real parallelism without macOS spawn/serialization issues.
    from concurrent.futures import ThreadPoolExecutor

    regen = 0
    ocean = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, result in enumerate(pool.map(_regen_tile, tiles, chunksize=50)):
            if result:
                regen += 1
            else:
                ocean += 1
            if (i + 1) % 2000 == 0:
                print(f'  {i+1}/{len(tiles)}: {regen} land, {ocean} ocean', flush=True)

    print(f'\nDone: {regen} regenerated, {ocean} ocean (of {len(tiles)} total)')
