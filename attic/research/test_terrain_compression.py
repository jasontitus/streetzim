#!/usr/bin/env python3
"""Test terrain tile compression strategies for elevation data.

Generates terrain tiles for multiple regions using various encoding strategies,
then compares file sizes both raw and after zstd compression (simulating ZIM
cluster compression).

Strategies tested:
  - Mapbox terrain-RGB WebP (baseline, and quantized 1m/2m/5m/10m)
  - Mapbox terrain-RGB AVIF lossless (baseline, and quantized 1m/2m/5m/10m)
  - Raw int16 binary (elevation as 16-bit integers)
  - Raw int16 delta-encoded (store differences between adjacent pixels)
  - LERC (Limited Error Raster Compression) at various max error tolerances

Usage:
    python3 test_terrain_compression.py [--max-zoom 12]
"""

import argparse
import math
import os
import struct
import subprocess
import tempfile
import time
import urllib.request
from io import BytesIO

import lerc
import mercantile
import numpy as np
import rasterio
import zstandard as zstd
from PIL import Image
from rasterio.merge import merge
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds

try:
    import pillow_avif  # noqa: F401
except ImportError:
    pass

COPERNICUS_DEM_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)

REGIONS = {
    "colorado": {
        "bbox": "-105.35,38.75,-104.65,39.05",
        "label": "Colorado (CO Springs)",
    },
    "kansas": {
        "bbox": "-97.00,38.50,-96.40,38.90",
        "label": "Kansas (Flint Hills)",
    },
    "dc": {
        "bbox": "-77.15,38.82,-76.90,38.98",
        "label": "Washington DC",
    },
}


def download_dem_tiles(bbox, cache_dir):
    """Download Copernicus DEM tiles needed for the bbox."""
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
    """Mosaic DEM tiles into a single GeoTIFF."""
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


def get_elevation_for_tile(mosaic_path, tile):
    """Read elevation data for a single mercantile tile."""
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
    return elevation


# --- Encoding strategies ---

def encode_webp_baseline(elevation):
    """Standard Mapbox terrain-RGB, 0.1m precision, lossless WebP."""
    elev = elevation[0]
    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)
    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)
    img = Image.fromarray(np.stack([r, g, b], axis=-1))
    buf = BytesIO()
    img.save(buf, "WEBP", lossless=True)
    return buf.getvalue()


def encode_webp_quantized(elevation, round_meters):
    """Mapbox terrain-RGB with elevation pre-rounded, lossless WebP."""
    elev = np.round(elevation[0] / round_meters) * round_meters
    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)
    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)
    img = Image.fromarray(np.stack([r, g, b], axis=-1))
    buf = BytesIO()
    img.save(buf, "WEBP", lossless=True)
    return buf.getvalue()


def encode_raw_int16(elevation):
    """Raw 16-bit signed integers, 1m precision. 256x256 = 128 KB uncompressed."""
    elev_int16 = np.clip(np.round(elevation[0]), -32768, 32767).astype(np.int16)
    return elev_int16.tobytes()


def encode_raw_int16_delta(elevation):
    """Delta-encoded 16-bit integers: store row-wise differences.
    First column stored as-is, rest as deltas from previous pixel.
    Deltas have much lower magnitude -> compresses better with zstd."""
    elev_int16 = np.clip(np.round(elevation[0]), -32768, 32767).astype(np.int16)
    # Delta encode: first column stays, rest are diffs
    delta = np.zeros_like(elev_int16)
    delta[:, 0] = elev_int16[:, 0]
    delta[:, 1:] = np.diff(elev_int16, axis=1)
    return delta.tobytes()


def encode_lerc(elevation, max_z_error):
    """LERC compression with configurable max error tolerance.
    LERC is purpose-built for raster elevation data."""
    elev = elevation[0].astype(np.float32)
    # First pass: get required buffer size
    _, nb = lerc.encode(elev, 1, False, None, max_z_error, 0)
    # Second pass: encode into buffer
    _, _, buf = lerc.encode(elev, 1, False, None, max_z_error, nb)
    return bytes(buf)


def encode_avif_baseline(elevation):
    """Standard Mapbox terrain-RGB, 0.1m precision, lossless AVIF."""
    elev = elevation[0]
    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)
    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)
    img = Image.fromarray(np.stack([r, g, b], axis=-1))
    return _encode_avif_lossless(img)


def encode_avif_quantized(elevation, round_meters):
    """Mapbox terrain-RGB with elevation pre-rounded, lossless AVIF."""
    elev = np.round(elevation[0] / round_meters) * round_meters
    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)
    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)
    img = Image.fromarray(np.stack([r, g, b], axis=-1))
    return _encode_avif_lossless(img)


def _encode_avif_lossless(img):
    """Encode a PIL Image to truly lossless AVIF using avifenc CLI.
    Pillow's AVIF encoder has ±3 LSB errors even at quality=100,
    so we shell out to avifenc --lossless for exact round-trip."""
    with tempfile.TemporaryDirectory() as td:
        png_path = os.path.join(td, "tile.png")
        avif_path = os.path.join(td, "tile.avif")
        img.save(png_path)
        subprocess.run(
            ["avifenc", "--lossless", "-s", "6", png_path, avif_path],
            capture_output=True,
            check=True,
        )
        with open(avif_path, "rb") as f:
            return f.read()


def decode_avif_to_elevation(tile_bytes):
    """Decode AVIF terrain-RGB back to elevation via Mapbox formula."""
    img = Image.open(BytesIO(tile_bytes))
    rgb = np.array(img, dtype=np.float64)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return (r * 65536 + g * 256 + b) * 0.1 - 10000.0


def decode_webp_to_elevation(tile_bytes):
    """Decode WebP terrain-RGB back to elevation via Mapbox formula."""
    img = Image.open(BytesIO(tile_bytes))
    rgb = np.array(img, dtype=np.float64)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return (r * 65536 + g * 256 + b) * 0.1 - 10000.0


def decode_raw_int16(tile_bytes):
    """Decode raw int16 back to elevation."""
    return np.frombuffer(tile_bytes, dtype=np.int16).reshape(256, 256).astype(np.float64)


def decode_raw_int16_delta(tile_bytes):
    """Decode delta-encoded int16 back to elevation."""
    delta = np.frombuffer(tile_bytes, dtype=np.int16).reshape(256, 256)
    elev = np.cumsum(delta, axis=1)
    return elev.astype(np.float64)


def decode_lerc(tile_bytes):
    """Decode LERC back to elevation."""
    _, arr, _ = lerc.decode(tile_bytes)
    return arr.astype(np.float64)


# Strategy definitions: (name, encode_fn, decode_fn, description, needs_custom_decoder)
STRATEGIES = [
    # Mapbox terrain-RGB WebP variants (MapLibre compatible)
    ("webp_baseline",     lambda e: encode_webp_baseline(e),         decode_webp_to_elevation,   "WebP lossless, 0.1m",    False),
    ("webp_quant_1m",     lambda e: encode_webp_quantized(e, 1),     decode_webp_to_elevation,   "WebP lossless, round 1m",  False),
    ("webp_quant_2m",     lambda e: encode_webp_quantized(e, 2),     decode_webp_to_elevation,   "WebP lossless, round 2m",  False),
    ("webp_quant_5m",     lambda e: encode_webp_quantized(e, 5),     decode_webp_to_elevation,   "WebP lossless, round 5m",  False),
    ("webp_quant_10m",    lambda e: encode_webp_quantized(e, 10),    decode_webp_to_elevation,   "WebP lossless, round 10m", False),
    # AVIF lossless terrain-RGB variants (MapLibre compatible with addProtocol decoder)
    ("avif_baseline",     lambda e: encode_avif_baseline(e),         decode_avif_to_elevation,   "AVIF lossless, 0.1m",    False),
    ("avif_quant_1m",     lambda e: encode_avif_quantized(e, 1),     decode_avif_to_elevation,   "AVIF lossless, round 1m",  False),
    ("avif_quant_2m",     lambda e: encode_avif_quantized(e, 2),     decode_avif_to_elevation,   "AVIF lossless, round 2m",  False),
    ("avif_quant_5m",     lambda e: encode_avif_quantized(e, 5),     decode_avif_to_elevation,   "AVIF lossless, round 5m",  False),
    ("avif_quant_10m",    lambda e: encode_avif_quantized(e, 10),    decode_avif_to_elevation,   "AVIF lossless, round 10m", False),
    # Custom binary formats (need JS decoder via addProtocol)
    ("raw_int16",         lambda e: encode_raw_int16(e),             decode_raw_int16,           "Raw int16, 1m",          True),
    ("raw_int16_delta",   lambda e: encode_raw_int16_delta(e),       decode_raw_int16_delta,     "Raw int16 delta-enc",    True),
    # LERC (need JS decoder)
    ("lerc_0.1m",         lambda e: encode_lerc(e, 0.1),            decode_lerc,                "LERC, max err 0.1m",     True),
    ("lerc_1m",           lambda e: encode_lerc(e, 1.0),            decode_lerc,                "LERC, max err 1m",       True),
    ("lerc_5m",           lambda e: encode_lerc(e, 5.0),            decode_lerc,                "LERC, max err 5m",       True),
    ("lerc_10m",          lambda e: encode_lerc(e, 10.0),           decode_lerc,                "LERC, max err 10m",      True),
]


def format_bytes(n):
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

    zstd_compressor = zstd.ZstdCompressor(level=3)  # ZIM default level

    # Results: {region: {strategy_name: {zoom: stats}}}
    all_results = {}

    for region_key, region_cfg in REGIONS.items():
        label = region_cfg["label"]
        bbox = [float(x) for x in region_cfg["bbox"].split(",")]
        dem_cache = os.path.join(cache_dir, f"dem_{region_key}")

        print(f"\n{'='*70}")
        print(f"Region: {label}  bbox={region_cfg['bbox']}")
        print(f"{'='*70}")

        tif_paths = download_dem_tiles(bbox, dem_cache)
        if not tif_paths:
            print(f"  No DEM data for {label}, skipping")
            continue
        mosaic_path = build_mosaic(tif_paths, dem_cache)

        region_results = {}

        for strat_name, encode_fn, decode_fn, desc, needs_decoder in STRATEGIES:
            print(f"\n  {strat_name} ({desc})" +
                  (" [needs custom JS decoder]" if needs_decoder else ""))

            t0 = time.time()
            zoom_stats = {}

            for z in range(0, args.max_zoom + 1):
                tiles_at_z = list(mercantile.tiles(*bbox, zooms=z))
                if not tiles_at_z:
                    continue

                total_raw_bytes = 0
                total_zstd_bytes = 0
                all_abs_errors = []

                for tile in tiles_at_z:
                    elevation = get_elevation_for_tile(mosaic_path, tile)

                    # Encode
                    tile_bytes = encode_fn(elevation)
                    total_raw_bytes += len(tile_bytes)

                    # Simulate ZIM cluster: zstd compress
                    zstd_bytes = zstd_compressor.compress(tile_bytes)
                    total_zstd_bytes += len(zstd_bytes)

                    # Round-trip error
                    decoded_elev = decode_fn(tile_bytes)
                    source_elev = elevation[0].astype(np.float64)
                    abs_error = np.abs(decoded_elev - source_elev)
                    all_abs_errors.append(abs_error)

                combined_errors = np.concatenate([e.ravel() for e in all_abs_errors])
                zoom_stats[z] = {
                    "count": len(tiles_at_z),
                    "raw_bytes": total_raw_bytes,
                    "zstd_bytes": total_zstd_bytes,
                    "errors": {
                        "mean": float(np.mean(combined_errors)),
                        "max": float(np.max(combined_errors)),
                        "p95": float(np.percentile(combined_errors, 95)),
                    },
                }

            elapsed = time.time() - t0
            total_raw = sum(s["raw_bytes"] for s in zoom_stats.values())
            total_zstd = sum(s["zstd_bytes"] for s in zoom_stats.values())
            total_tiles = sum(s["count"] for s in zoom_stats.values())
            print(f"    {total_tiles} tiles: raw={format_bytes(total_raw)}, "
                  f"zstd={format_bytes(total_zstd)} ({elapsed:.1f}s)")

            region_results[strat_name] = {
                "zoom_stats": zoom_stats,
                "total_tiles": total_tiles,
                "total_raw": total_raw,
                "total_zstd": total_zstd,
            }

        all_results[region_key] = region_results

    # --- Summary Report ---

    print(f"\n\n{'='*80}")
    print("COMPRESSION RESULTS SUMMARY")
    print(f"{'='*80}")

    strat_names = [s[0] for s in STRATEGIES]
    strat_needs_decoder = {s[0]: s[4] for s in STRATEGIES}

    for region_key, region_cfg in REGIONS.items():
        label = region_cfg["label"]
        results = all_results.get(region_key)
        if not results:
            continue

        baseline_raw = results["webp_baseline"]["total_raw"]
        baseline_zstd = results["webp_baseline"]["total_zstd"]

        print(f"\n  {label}")
        print(f"  {'Strategy':<22} {'Raw Size':>10} {'+ zstd':>10} {'Raw Svgs':>9} {'Zstd Svgs':>10} {'Decoder':>8}")
        print(f"  {'-'*72}")

        for sn in strat_names:
            r = results[sn]
            raw = r["total_raw"]
            zs = r["total_zstd"]
            raw_sav = (1 - raw / baseline_raw) * 100 if baseline_raw > 0 else 0
            zstd_sav = (1 - zs / baseline_zstd) * 100 if baseline_zstd > 0 else 0
            decoder = "custom" if strat_needs_decoder[sn] else "native"
            base = " *" if sn == "webp_baseline" else ""
            print(f"  {sn:<22} {format_bytes(raw):>10} {format_bytes(zs):>10} "
                  f"{raw_sav:>8.1f}% {zstd_sav:>9.1f}% {decoder:>8}{base}")

        print(f"\n  * = current baseline")

    # --- Per-zoom at max zoom ---

    print(f"\n\n{'='*80}")
    print(f"PER-ZOOM DETAIL AT Z{args.max_zoom}")
    print(f"{'='*80}")

    for region_key, region_cfg in REGIONS.items():
        label = region_cfg["label"]
        results = all_results.get(region_key)
        if not results:
            continue

        z = args.max_zoom
        baseline_zstd_z = results["webp_baseline"]["zoom_stats"].get(z)
        if not baseline_zstd_z:
            continue

        n_tiles = baseline_zstd_z["count"]
        print(f"\n  {label} — z{z} ({n_tiles} tiles)")
        print(f"  {'Strategy':<22} {'Raw/tile':>10} {'Zstd/tile':>10} {'Zstd Svgs':>10} {'Zstd ratio':>11}")
        print(f"  {'-'*66}")

        base_zstd_total = baseline_zstd_z["zstd_bytes"]
        for sn in strat_names:
            zs = results[sn]["zoom_stats"].get(z)
            if not zs:
                continue
            raw_avg = zs["raw_bytes"] / zs["count"]
            zstd_avg = zs["zstd_bytes"] / zs["count"]
            zstd_sav = (1 - zs["zstd_bytes"] / base_zstd_total) * 100 if base_zstd_total > 0 else 0
            # How well does zstd compress this format?
            zstd_ratio = zs["raw_bytes"] / zs["zstd_bytes"] if zs["zstd_bytes"] > 0 else 0
            print(f"  {sn:<22} {format_bytes(int(raw_avg)):>10} {format_bytes(int(zstd_avg)):>10} "
                  f"{zstd_sav:>9.1f}% {zstd_ratio:>10.2f}x")

    # --- Error analysis ---

    print(f"\n\n{'='*80}")
    print("ELEVATION ERROR (meters)")
    print(f"{'='*80}")

    for region_key, region_cfg in REGIONS.items():
        label = region_cfg["label"]
        results = all_results.get(region_key)
        if not results:
            continue

        print(f"\n  {label} (z{args.max_zoom} tiles)")
        print(f"  {'Strategy':<22} {'Mean':>8} {'P95':>8} {'Max':>8}")
        print(f"  {'-'*48}")

        for sn in strat_names:
            zs = results[sn]["zoom_stats"].get(args.max_zoom)
            if not zs:
                continue
            errs = zs["errors"]
            print(f"  {sn:<22} {errs['mean']:>7.2f}m {errs['p95']:>7.2f}m {errs['max']:>7.2f}m")

    # --- Best options summary ---

    print(f"\n\n{'='*80}")
    print("BEST OPTIONS (sorted by zstd-compressed size)")
    print(f"{'='*80}")

    for region_key, region_cfg in REGIONS.items():
        label = region_cfg["label"]
        results = all_results.get(region_key)
        if not results:
            continue

        print(f"\n  {label}")
        print(f"  {'Strategy':<22} {'In ZIM':>10} {'Savings':>9} {'Max Err':>8} {'Decoder':>8}")
        print(f"  {'-'*60}")

        baseline_zstd = results["webp_baseline"]["total_zstd"]
        sorted_strats = sorted(strat_names, key=lambda sn: results[sn]["total_zstd"])

        for sn in sorted_strats:
            r = results[sn]
            zs = r["total_zstd"]
            sav = (1 - zs / baseline_zstd) * 100 if baseline_zstd > 0 else 0
            # Get max error from highest zoom
            max_z = max(r["zoom_stats"].keys())
            max_err = r["zoom_stats"][max_z]["errors"]["max"]
            decoder = "custom" if strat_needs_decoder[sn] else "native"
            print(f"  {sn:<22} {format_bytes(zs):>10} {sav:>8.1f}% {max_err:>7.2f}m {decoder:>8}")

    print()


if __name__ == "__main__":
    main()
