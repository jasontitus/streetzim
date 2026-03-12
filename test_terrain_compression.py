#!/usr/bin/env python3
"""Test terrain tile compression strategies for elevation data.

Generates terrain-RGB tiles for two regions (Colorado and Washington DC) using
four encoding strategies, then compares file sizes:

  1. baseline    — Current approach: 0.1m precision, lossless WebP
  2. quantized   — 1m precision (blue channel zeroed), lossless WebP
  3. lossy       — 0.1m precision, lossy WebP quality 92
  4. combined    — 1m precision + lossy WebP quality 92

Usage:
    python3 test_terrain_compression.py [--max-zoom 12]
"""

import argparse
import math
import os
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import mercantile
import numpy as np
import rasterio
from PIL import Image
from rasterio.merge import merge
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds

COPERNICUS_DEM_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)

REGIONS = {
    "colorado": {
        "bbox": "-105.35,38.75,-104.65,39.05",  # Colorado Springs area (mountains + plains)
        "label": "Colorado (CO Springs)",
    },
    "dc": {
        "bbox": "-77.15,38.82,-76.90,38.98",  # Washington DC metro
        "label": "Washington DC",
    },
}

# Compression strategies
STRATEGIES = {
    "baseline": {"precision": 0.1, "lossless": True, "quality": None},
    "quantized_1m": {"precision": 1.0, "lossless": True, "quality": None},
    "lossy_q92": {"precision": 0.1, "lossless": False, "quality": 92},
    "combined": {"precision": 1.0, "lossless": False, "quality": 92},
}


def download_dem_tiles(bbox, cache_dir):
    """Download Copernicus DEM tiles needed for the bbox. Returns list of file paths."""
    minlon, minlat, maxlon, maxlat = bbox
    os.makedirs(cache_dir, exist_ok=True)
    tif_paths = []

    for lat in range(math.floor(minlat), math.floor(maxlat) + 1):
        for lon in range(math.floor(minlon), math.floor(maxlon) + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            abs_lat, abs_lon = abs(lat), abs(lon)
            fname = f"dem_{ns}{abs_lat:02d}_{ew}{abs_lon:03d}.tif"
            fpath = os.path.join(cache_dir, fname)

            if not os.path.exists(fpath) or os.path.getsize(fpath) < 1000:
                url = COPERNICUS_DEM_URL.format(ns=ns, lat=abs_lat, ew=ew, lon=abs_lon)
                print(f"  Downloading {ns}{abs_lat:02d} {ew}{abs_lon:03d}...")
                req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        with open(fpath, "wb") as f:
                            while True:
                                chunk = resp.read(1024 * 1024)
                                if not chunk:
                                    break
                                f.write(chunk)
                    print(f"    {os.path.getsize(fpath) / (1024*1024):.1f} MB")
                except Exception as e:
                    print(f"    Warning: failed to download: {e}")
                    continue
            else:
                print(f"  Cached: {ns}{abs_lat:02d} {ew}{abs_lon:03d}")
            tif_paths.append(fpath)

    return tif_paths


def build_mosaic(tif_paths, cache_dir):
    """Mosaic DEM tiles into a single GeoTIFF. Returns path to mosaic file."""
    mosaic_path = os.path.join(cache_dir, "mosaic_4326.tif")
    if os.path.exists(mosaic_path) and os.path.getsize(mosaic_path) > 1000:
        print("  Using cached mosaic")
        return mosaic_path

    print("  Mosaicing DEM tiles...")
    datasets = [rasterio.open(p) for p in tif_paths]
    mosaic, mosaic_transform = merge(datasets)
    meta = datasets[0].meta.copy()
    for ds in datasets:
        ds.close()

    meta.update({
        "height": mosaic.shape[1],
        "width": mosaic.shape[2],
        "transform": mosaic_transform,
        "count": 1,
    })

    with rasterio.open(mosaic_path, "w", **meta) as dst:
        dst.write(mosaic[0], 1)
    del mosaic
    return mosaic_path


def encode_terrain_rgb(elevation, precision):
    """Encode elevation array to terrain-RGB using given precision.

    Mapbox terrain-RGB: encoded = (elevation + 10000) / precision
    Then split into R (high byte), G (mid byte), B (low byte).

    With precision=1.0, the low byte is always 0, making it compress much better.
    """
    elev = elevation[0]
    encoded = ((elev + 10000.0) / precision).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)

    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)

    return np.stack([r, g, b], axis=-1)


def save_tile_to_bytes(rgb_array, lossless, quality):
    """Save an RGB array to WebP and return the bytes (for size measurement)."""
    img = Image.fromarray(rgb_array)
    buf = BytesIO()
    if lossless:
        img.save(buf, "WEBP", lossless=True)
    else:
        img.save(buf, "WEBP", lossless=False, quality=quality)
    return buf.getvalue()


def generate_tiles_for_strategy(mosaic_path, bbox, max_zoom, strategy_name, strategy_cfg):
    """Generate all tiles for a region+strategy combination.

    Returns dict: {zoom_level: {"count": N, "total_bytes": B}}
    """
    minlon, minlat, maxlon, maxlat = bbox
    precision = strategy_cfg["precision"]
    lossless = strategy_cfg["lossless"]
    quality = strategy_cfg["quality"]

    zoom_stats = {}

    for z in range(0, max_zoom + 1):
        tiles_at_z = list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z))
        if not tiles_at_z:
            continue

        total_bytes = 0
        for tile in tiles_at_z:
            tb = mercantile.bounds(tile)
            tile_bounds_3857 = transform_bounds(
                "EPSG:4326", "EPSG:3857", tb.west, tb.south, tb.east, tb.north
            )
            tile_transform = from_bounds(*tile_bounds_3857, 256, 256)

            elevation = np.zeros((1, 256, 256), dtype=np.float32)
            with rasterio.open(mosaic_path) as src:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=elevation,
                    dst_transform=tile_transform,
                    dst_crs="EPSG:3857",
                    resampling=Resampling.cubic,
                )

            rgb = encode_terrain_rgb(elevation, precision)
            tile_bytes = save_tile_to_bytes(rgb, lossless, quality)
            total_bytes += len(tile_bytes)

        zoom_stats[z] = {"count": len(tiles_at_z), "total_bytes": total_bytes}

    return zoom_stats


def format_bytes(n):
    """Format byte count as human-readable string."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    else:
        return f"{n / (1024 * 1024):.2f} MB"


def main():
    parser = argparse.ArgumentParser(description="Test terrain tile compression strategies")
    parser.add_argument("--max-zoom", type=int, default=12,
                        help="Maximum zoom level to generate (default: 12)")
    args = parser.parse_args()

    cache_dir = os.path.join(os.path.dirname(__file__), "terrain_compression_test")
    os.makedirs(cache_dir, exist_ok=True)

    # Results: {region: {strategy: {zoom: stats}}}
    all_results = {}

    for region_key, region_cfg in REGIONS.items():
        label = region_cfg["label"]
        bbox = [float(x) for x in region_cfg["bbox"].split(",")]
        dem_cache = os.path.join(cache_dir, f"dem_{region_key}")

        print(f"\n{'='*70}")
        print(f"Region: {label}  bbox={region_cfg['bbox']}")
        print(f"{'='*70}")

        # Download and mosaic DEMs (shared across strategies)
        tif_paths = download_dem_tiles(bbox, dem_cache)
        if not tif_paths:
            print(f"  No DEM data for {label}, skipping")
            continue
        mosaic_path = build_mosaic(tif_paths, dem_cache)

        region_results = {}
        for strat_name, strat_cfg in STRATEGIES.items():
            desc = []
            desc.append(f"precision={strat_cfg['precision']}m")
            desc.append("lossless" if strat_cfg["lossless"] else f"lossy q{strat_cfg['quality']}")
            print(f"\n  Strategy: {strat_name} ({', '.join(desc)})")

            t0 = time.time()
            zoom_stats = generate_tiles_for_strategy(
                mosaic_path, bbox, args.max_zoom, strat_name, strat_cfg
            )
            elapsed = time.time() - t0

            total_tiles = sum(s["count"] for s in zoom_stats.values())
            total_bytes = sum(s["total_bytes"] for s in zoom_stats.values())
            print(f"    {total_tiles} tiles, {format_bytes(total_bytes)} total ({elapsed:.1f}s)")

            region_results[strat_name] = {
                "zoom_stats": zoom_stats,
                "total_tiles": total_tiles,
                "total_bytes": total_bytes,
            }

        all_results[region_key] = region_results

    # Print summary report
    print(f"\n\n{'='*70}")
    print("COMPRESSION RESULTS SUMMARY")
    print(f"{'='*70}")

    for region_key, region_cfg in REGIONS.items():
        label = region_cfg["label"]
        results = all_results.get(region_key)
        if not results:
            continue

        baseline_bytes = results["baseline"]["total_bytes"]

        print(f"\n  {label}")
        print(f"  {'Strategy':<20} {'Total Size':>12} {'Savings':>10} {'Ratio':>8}")
        print(f"  {'-'*52}")

        for strat_name in STRATEGIES:
            r = results[strat_name]
            total = r["total_bytes"]
            savings_pct = (1 - total / baseline_bytes) * 100 if baseline_bytes > 0 else 0
            ratio = baseline_bytes / total if total > 0 else float("inf")
            marker = " (baseline)" if strat_name == "baseline" else ""
            print(f"  {strat_name:<20} {format_bytes(total):>12} {savings_pct:>9.1f}% {ratio:>7.2f}x{marker}")

    # Per-zoom breakdown for the highest-impact zoom levels
    print(f"\n\n{'='*70}")
    print("PER-ZOOM BREAKDOWN (highest zoom levels)")
    print(f"{'='*70}")

    for region_key, region_cfg in REGIONS.items():
        label = region_cfg["label"]
        results = all_results.get(region_key)
        if not results:
            continue

        print(f"\n  {label}")

        # Show last 4 zoom levels (where most data lives)
        max_z = max(results["baseline"]["zoom_stats"].keys())
        show_zooms = range(max(0, max_z - 3), max_z + 1)

        for z in show_zooms:
            baseline_z = results["baseline"]["zoom_stats"].get(z)
            if not baseline_z:
                continue
            n_tiles = baseline_z["count"]
            print(f"\n    Zoom {z} ({n_tiles} tiles):")
            print(f"    {'Strategy':<20} {'Total':>10} {'Avg/tile':>10} {'Savings':>10}")
            print(f"    {'-'*52}")

            for strat_name in STRATEGIES:
                zs = results[strat_name]["zoom_stats"].get(z)
                if not zs:
                    continue
                total = zs["total_bytes"]
                avg = total / zs["count"] if zs["count"] else 0
                base_total = baseline_z["total_bytes"]
                savings = (1 - total / base_total) * 100 if base_total > 0 else 0
                print(f"    {strat_name:<20} {format_bytes(total):>10} {format_bytes(int(avg)):>10} {savings:>9.1f}%")

    print()


if __name__ == "__main__":
    main()
