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
#
# IMPORTANT: Lossy WebP is NOT suitable for Mapbox terrain-RGB tiles.
# The encoding packs elevation into a 24-bit integer across R/G/B channels.
# A single-LSB change in R = 6,553.6m of elevation error because R is the
# high byte (R * 65536 * 0.1). Lossy compression doesn't respect byte
# boundaries, so even quality=99 produces catastrophic elevation errors.
#
# The effective compression lever is "round_meters": pre-rounding elevation
# to a coarser precision before encoding. This reduces entropy in all three
# channels (encoded values change more slowly), improving lossless WebP
# compression significantly while remaining fully MapLibre-compatible.
#
# Source data is Copernicus GLO-30 (30m horizontal resolution), so sub-meter
# vertical precision is already beyond what the data supports.
STRATEGIES = {
    "baseline": {"round_meters": None, "lossless": True, "quality": None},
    "quantized_1m": {"round_meters": 1, "lossless": True, "quality": None},
    "quantized_2m": {"round_meters": 2, "lossless": True, "quality": None},
    "quantized_5m": {"round_meters": 5, "lossless": True, "quality": None},
    "quantized_10m": {"round_meters": 10, "lossless": True, "quality": None},
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


def encode_terrain_rgb(elevation, round_meters=None):
    """Encode elevation array to Mapbox terrain-RGB.

    Always uses the standard Mapbox formula: encoded = (elevation + 10000) / 0.1
    Then splits into R (high byte), G (mid byte), B (low byte).

    If round_meters is set, elevation is pre-rounded to that precision before
    encoding. This reduces entropy in the RGB channels (fewer distinct values)
    which improves both lossless and lossy compression, while remaining fully
    compatible with MapLibre's Mapbox decoder.

    For example, round_meters=1 means encoded values are always multiples of 10,
    so the B channel only takes values 0,10,20,...,250 (26 values vs 256).
    """
    elev = elevation[0].copy()
    if round_meters is not None:
        elev = np.round(elev / round_meters) * round_meters

    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
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


def decode_terrain_rgb(webp_bytes):
    """Decode WebP terrain-RGB bytes back to elevation using Mapbox formula.

    This simulates exactly what MapLibre does on the client:
        elevation = (R * 65536 + G * 256 + B) * 0.1 - 10000
    """
    img = Image.open(BytesIO(webp_bytes))
    rgb = np.array(img, dtype=np.float64)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return (r * 65536 + g * 256 + b) * 0.1 - 10000.0


def generate_tiles_for_strategy(mosaic_path, bbox, max_zoom, strategy_name, strategy_cfg):
    """Generate all tiles for a region+strategy combination.

    Returns dict: {zoom_level: {"count": N, "total_bytes": B, "errors": {...}}}
    Error stats measure round-trip elevation error: source elevation -> encode ->
    WebP compress -> decompress -> decode via Mapbox formula -> compare.
    """
    minlon, minlat, maxlon, maxlat = bbox
    round_meters = strategy_cfg["round_meters"]
    lossless = strategy_cfg["lossless"]
    quality = strategy_cfg["quality"]

    zoom_stats = {}

    for z in range(0, max_zoom + 1):
        tiles_at_z = list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z))
        if not tiles_at_z:
            continue

        total_bytes = 0
        all_abs_errors = []

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

            rgb = encode_terrain_rgb(elevation, round_meters)
            tile_bytes = save_tile_to_bytes(rgb, lossless, quality)
            total_bytes += len(tile_bytes)

            # Round-trip error: decode the WebP back and compare to source elevation
            decoded_elev = decode_terrain_rgb(tile_bytes)
            source_elev = elevation[0].astype(np.float64)
            abs_error = np.abs(decoded_elev - source_elev)
            all_abs_errors.append(abs_error)

        # Aggregate error stats across all tiles at this zoom
        combined_errors = np.concatenate([e.ravel() for e in all_abs_errors])
        zoom_stats[z] = {
            "count": len(tiles_at_z),
            "total_bytes": total_bytes,
            "errors": {
                "mean": float(np.mean(combined_errors)),
                "max": float(np.max(combined_errors)),
                "p50": float(np.median(combined_errors)),
                "p95": float(np.percentile(combined_errors, 95)),
                "p99": float(np.percentile(combined_errors, 99)),
            },
        }

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
            rm = strat_cfg["round_meters"]
            desc.append(f"round={rm}m" if rm else "full precision")
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

    # Elevation error analysis
    print(f"\n\n{'='*70}")
    print("ELEVATION ERROR vs LOSSLESS BASELINE (meters)")
    print("(round-trip: source -> encode -> WebP -> decode via Mapbox formula)")
    print(f"{'='*70}")

    for region_key, region_cfg in REGIONS.items():
        label = region_cfg["label"]
        results = all_results.get(region_key)
        if not results:
            continue

        print(f"\n  {label}")

        # Show error stats at the max zoom (most tiles, most representative)
        max_z = max(results["baseline"]["zoom_stats"].keys())

        print(f"\n    All zoom levels aggregated:")
        print(f"    {'Strategy':<20} {'Mean':>8} {'Median':>8} {'P95':>8} {'P99':>8} {'Max':>8}")
        print(f"    {'-'*54}")

        for strat_name in STRATEGIES:
            # Aggregate errors across all zoom levels
            all_means = []
            all_maxes = []
            all_p95s = []
            all_p99s = []
            all_medians = []
            total_pixels = 0
            weighted_mean = 0.0
            worst_max = 0.0

            for z, zs in results[strat_name]["zoom_stats"].items():
                errs = zs["errors"]
                n_pixels = zs["count"] * 256 * 256
                weighted_mean += errs["mean"] * n_pixels
                total_pixels += n_pixels
                worst_max = max(worst_max, errs["max"])
                all_p95s.append(errs["p95"])
                all_p99s.append(errs["p99"])
                all_medians.append(errs["p50"])

            avg_mean = weighted_mean / total_pixels if total_pixels > 0 else 0
            # Use max-zoom stats for percentiles (most representative)
            max_z_errs = results[strat_name]["zoom_stats"][max_z]["errors"]

            print(f"    {strat_name:<20} {avg_mean:>7.2f}m {max_z_errs['p50']:>7.2f}m "
                  f"{max_z_errs['p95']:>7.2f}m {max_z_errs['p99']:>7.2f}m {worst_max:>7.2f}m")

        # Per-zoom error detail for last few zoom levels
        show_zooms = range(max(0, max_z - 3), max_z + 1)
        print(f"\n    Per-zoom detail:")
        print(f"    {'Zoom':<6} {'Strategy':<20} {'Mean':>8} {'P95':>8} {'Max':>8}")
        print(f"    {'-'*54}")

        for z in show_zooms:
            for strat_name in STRATEGIES:
                zs = results[strat_name]["zoom_stats"].get(z)
                if not zs:
                    continue
                errs = zs["errors"]
                print(f"    z{z:<5} {strat_name:<20} {errs['mean']:>7.2f}m "
                      f"{errs['p95']:>7.2f}m {errs['max']:>7.2f}m")
            if z < max_z:
                print()

    print()


if __name__ == "__main__":
    main()
