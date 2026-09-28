#!/usr/bin/env python3
"""Process one chunk of boundary tiles. Run 4 instances in parallel."""
import math
import os
import sys

import mercantile
import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling, transform_bounds
from rasterio.transform import from_bounds
from PIL import Image

CACHE = 'terrain_cache'
DEM_DIR = os.path.join(CACHE, 'dem_sources')

# Build DEM index
DEM_INDEX = {}
for f in os.listdir(DEM_DIR):
    if not f.startswith('dem_') or not f.endswith('.tif') or os.path.getsize(os.path.join(DEM_DIR, f)) < 1000:
        continue
    parts = f.replace('dem_', '').replace('.tif', '').split('_')
    if len(parts) == 2:
        try:
            lat = int(parts[0][1:]) * (1 if parts[0][0] == 'N' else -1)
            lon = int(parts[1][1:]) * (1 if parts[1][0] == 'E' else -1)
            DEM_INDEX[(lat, lon)] = os.path.join(DEM_DIR, f)
        except ValueError:
            pass

chunk_file = sys.argv[1]
tiles = []
with open(chunk_file) as f:
    for line in f:
        z, x, y = [int(v) for v in line.strip().split(',')]
        tiles.append((z, x, y))

print(f'[{chunk_file}] Processing {len(tiles)} tiles', flush=True)

regen = 0
ocean = 0
for i, (z, x, y) in enumerate(tiles):
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
        ocean += 1
    else:
        elev = np.round(composite / 10.0) * 10.0
        encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
        encoded = np.clip(encoded, 0, 16777215)
        r = ((encoded >> 16) & 0xFF).astype(np.uint8)
        g = ((encoded >> 8) & 0xFF).astype(np.uint8)
        b = (encoded & 0xFF).astype(np.uint8)
        img = Image.fromarray(np.stack([r, g, b], axis=-1))
        tile_dir = os.path.join(CACHE, str(z), str(x))
        os.makedirs(tile_dir, exist_ok=True)
        img.save(os.path.join(tile_dir, f'{y}.webp'), 'WEBP', lossless=True)
        regen += 1

    if (i + 1) % 2000 == 0:
        print(f'  [{chunk_file}] {i+1}/{len(tiles)}: {regen} land, {ocean} ocean', flush=True)

print(f'[{chunk_file}] Done: {regen} regenerated, {ocean} ocean', flush=True)
