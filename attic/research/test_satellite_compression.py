#!/usr/bin/env python3
"""Test satellite tile compression strategies.

Downloads sample tiles from urban (DC) and rural (Virginia countryside) areas,
then tests: WebP at various qualities, AVIF at various qualities, 256 vs 512 tile sizes.
"""

import io
import os
import sys
import time
import urllib.request
from collections import defaultdict
from PIL import Image

try:
    import pillow_avif  # noqa: F401 — registers AVIF codec
except ImportError:
    print("Warning: pillow-avif-plugin not installed, AVIF tests will be skipped")

SATELLITE_TILE_URL = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/default/g/{z}/{y}/{x}.jpg"


def download_tile(z, x, y):
    """Download a single JPEG satellite tile, return PIL Image."""
    url = SATELLITE_TILE_URL.format(z=z, x=x, y=y)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "streetzim-test/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            return Image.open(io.BytesIO(data)), len(data)
        except Exception as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
            else:
                raise


def compress_image(img, fmt, quality):
    """Compress image to format, return bytes."""
    buf = io.BytesIO()
    if fmt == "webp":
        img.save(buf, "WEBP", quality=quality)
    elif fmt == "avif":
        img.save(buf, "AVIF", quality=quality, speed=6)
    elif fmt == "jpeg":
        img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def stitch_tiles_2x2(tiles):
    """Stitch 4 tiles (2x2) into a single 512x512 image."""
    w, h = tiles[0][0].size
    stitched = Image.new("RGB", (w * 2, h * 2))
    for i, (img, _) in enumerate(tiles):
        x = (i % 2) * w
        y = (i // 2) * h
        stitched.paste(img, (x, y))
    return stitched


# Sample areas at z14 (highest detail)
# Urban: central DC (National Mall area)
# Rural: Virginia countryside west of DC
# Suburban: Bethesda/Silver Spring area
SAMPLE_AREAS = {
    "urban_dc": {
        "z": 14,
        "tiles": [
            # National Mall / downtown DC — dense urban
            (14, 4686, 6424),
            (14, 4687, 6424),
            (14, 4686, 6425),
            (14, 4687, 6425),
            # Capitol area
            (14, 4688, 6424),
            (14, 4689, 6424),
            (14, 4688, 6425),
            (14, 4689, 6425),
        ]
    },
    "rural_va": {
        "z": 14,
        "tiles": [
            # Rural Virginia - farmland/forest west of DC
            (14, 4660, 6430),
            (14, 4661, 6430),
            (14, 4660, 6431),
            (14, 4661, 6431),
            # More rural
            (14, 4658, 6432),
            (14, 4659, 6432),
            (14, 4658, 6433),
            (14, 4659, 6433),
        ]
    },
    "suburban": {
        "z": 14,
        "tiles": [
            # Bethesda / Silver Spring
            (14, 4684, 6420),
            (14, 4685, 6420),
            (14, 4684, 6421),
            (14, 4685, 6421),
            (14, 4686, 6420),
            (14, 4687, 6420),
            (14, 4686, 6421),
            (14, 4687, 6421),
        ]
    },
}

# Compression strategies to test
STRATEGIES = [
    # (label, format, quality, tile_size)
    ("jpeg_source", "jpeg", 85, 256),      # baseline: original JPEG quality
    ("webp_q65", "webp", 65, 256),         # current default
    ("webp_q50", "webp", 50, 256),
    ("webp_q40", "webp", 40, 256),
    ("webp_q30", "webp", 30, 256),
    ("webp_q65_512", "webp", 65, 512),     # 512px tile variants
    ("webp_q50_512", "webp", 50, 512),
    ("webp_q40_512", "webp", 40, 512),
    ("avif_q50", "avif", 50, 256),
    ("avif_q40", "avif", 40, 256),
    ("avif_q30", "avif", 30, 256),
    ("avif_q20", "avif", 20, 256),
    ("avif_q50_512", "avif", 50, 512),     # 512px AVIF
    ("avif_q40_512", "avif", 40, 512),
    ("avif_q30_512", "avif", 30, 512),
    ("avif_q20_512", "avif", 20, 512),
]


def main():
    print("=" * 80)
    print("Satellite Tile Compression Benchmark")
    print("=" * 80)

    all_results = {}

    for area_name, area_info in SAMPLE_AREAS.items():
        print(f"\n--- Downloading {area_name} tiles ---")
        tiles = []
        total_jpeg_bytes = 0
        for z, x, y in area_info["tiles"]:
            img, jpeg_size = download_tile(z, x, y)
            tiles.append((img, jpeg_size))
            total_jpeg_bytes += jpeg_size
            print(f"  z{z}/{x}/{y}: {jpeg_size:,} bytes JPEG, {img.size}")

        print(f"  Total JPEG source: {total_jpeg_bytes:,} bytes ({total_jpeg_bytes/1024:.1f} KB)")

        # Run each strategy
        results = {}
        for label, fmt, quality, tile_size in STRATEGIES:
            if fmt == "avif" and "AVIF" not in Image.registered_extensions().values():
                continue

            total_bytes = 0
            encode_time = 0

            if tile_size == 512:
                # Stitch tiles in pairs of 4 (2x2)
                for i in range(0, len(tiles), 4):
                    group = tiles[i:i+4]
                    if len(group) < 4:
                        break
                    stitched = stitch_tiles_2x2(group)
                    t0 = time.time()
                    data = compress_image(stitched, fmt, quality)
                    encode_time += time.time() - t0
                    total_bytes += len(data)
            else:
                for img, _ in tiles:
                    t0 = time.time()
                    data = compress_image(img, fmt, quality)
                    encode_time += time.time() - t0
                    total_bytes += len(data)

            savings_pct = (1 - total_bytes / total_jpeg_bytes) * 100
            results[label] = {
                "bytes": total_bytes,
                "savings_vs_jpeg": savings_pct,
                "encode_ms": encode_time * 1000,
            }

        all_results[area_name] = (results, total_jpeg_bytes)

    # Print results table
    print("\n" + "=" * 80)
    print("RESULTS")
    print("=" * 80)

    # Get the webp_q65 baseline for each area for comparison
    for area_name, (results, jpeg_bytes) in all_results.items():
        baseline = results.get("webp_q65", {}).get("bytes", jpeg_bytes)

        print(f"\n### {area_name} (JPEG source: {jpeg_bytes/1024:.1f} KB)")
        print(f"{'Strategy':<20} {'Size':>10} {'vs JPEG':>10} {'vs webp_q65':>12} {'Encode':>10}")
        print("-" * 65)

        # Sort by size
        for label in sorted(results, key=lambda k: results[k]["bytes"]):
            r = results[label]
            size_kb = r["bytes"] / 1024
            vs_jpeg = f"{r['savings_vs_jpeg']:.1f}%"
            vs_baseline = f"{(1 - r['bytes'] / baseline) * 100:.1f}%"
            enc = f"{r['encode_ms']:.0f}ms"
            print(f"{label:<20} {size_kb:>8.1f}KB {vs_jpeg:>10} {vs_baseline:>12} {enc:>10}")

    # Summary across all areas
    print("\n" + "=" * 80)
    print("SUMMARY — Average savings vs current (webp_q65 256px)")
    print("=" * 80)

    strategy_totals = defaultdict(lambda: {"bytes": 0, "baseline": 0})
    for area_name, (results, _) in all_results.items():
        baseline = results.get("webp_q65", {}).get("bytes", 1)
        for label, r in results.items():
            strategy_totals[label]["bytes"] += r["bytes"]
            strategy_totals[label]["baseline"] += baseline

    print(f"{'Strategy':<20} {'Avg savings vs webp_q65':>25}")
    print("-" * 48)
    for label in sorted(strategy_totals, key=lambda k: strategy_totals[k]["bytes"] / strategy_totals[k]["baseline"]):
        t = strategy_totals[label]
        savings = (1 - t["bytes"] / t["baseline"]) * 100
        print(f"{label:<20} {savings:>23.1f}%")


if __name__ == "__main__":
    main()
