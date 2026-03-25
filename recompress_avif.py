#!/usr/bin/env python3
"""Recompress AVIF satellite tiles from JPEG sources at a new quality level.

Reads from satellite_cache_sources/ (JPEG), writes to satellite_cache_avif_256/.
Overwrites existing tiles. Uses multiprocessing for CPU parallelism.

Usage:
    python3 recompress_avif.py --quality 30 --workers 20
    python3 recompress_avif.py --quality 30 --workers 20 --dry-run
"""

import argparse
import os
import sys
import time
from multiprocessing import Pool, Value, Lock
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
SOURCE_DIR = SCRIPT_DIR / "satellite_cache_sources"
DEST_DIR = SCRIPT_DIR / "satellite_cache_avif_256"

# Shared counters
counter = None
counter_lock = None


def init_worker(c, l):
    global counter, counter_lock
    counter = c
    counter_lock = l


def recompress_tile(args):
    """Recompress a single JPEG source tile to AVIF."""
    src_path, dst_path, quality = args
    try:
        from PIL import Image
        img = Image.open(src_path)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        img.save(dst_path, "AVIF", quality=quality, speed=6)
        with counter_lock:
            counter.value += 1
        return os.path.getsize(dst_path)
    except Exception as e:
        return 0


def main():
    parser = argparse.ArgumentParser(description="Recompress AVIF tiles from JPEG sources")
    parser.add_argument("--quality", type=int, default=30, help="AVIF quality (default: 30)")
    parser.add_argument("--workers", type=int, default=20, help="Number of parallel workers (default: 20)")
    parser.add_argument("--source-dir", type=str, default=str(SOURCE_DIR), help="JPEG source directory")
    parser.add_argument("--dest-dir", type=str, default=str(DEST_DIR), help="AVIF output directory")
    parser.add_argument("--dry-run", action="store_true", help="Count tiles without recompressing")
    parser.add_argument("--zoom", type=str, default=None, help="Only process specific zoom levels (e.g. '12,13,14')")
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    dest_dir = Path(args.dest_dir)

    if not source_dir.exists():
        print(f"Error: source directory not found: {source_dir}")
        sys.exit(1)

    # Collect all JPEG source tiles
    print(f"Scanning {source_dir} for JPEG tiles...")
    zoom_filter = set(int(z) for z in args.zoom.split(",")) if args.zoom else None

    tasks = []
    for root, dirs, files in os.walk(source_dir):
        for f in files:
            if not f.endswith(".jpg"):
                continue
            src = os.path.join(root, f)
            # Extract z/x/y from path: source_dir/z/x/y.jpg
            rel = os.path.relpath(src, source_dir)
            parts = rel.split(os.sep)
            if len(parts) != 3:
                continue
            z_str, x_str, y_ext = parts
            try:
                z = int(z_str)
            except ValueError:
                continue
            if zoom_filter and z not in zoom_filter:
                continue
            y_stem = y_ext.replace(".jpg", "")
            dst = os.path.join(dest_dir, z_str, x_str, f"{y_stem}.avif")
            tasks.append((src, dst, args.quality))

    print(f"Found {len(tasks):,} JPEG tiles to recompress")

    if zoom_filter:
        print(f"Zoom filter: {sorted(zoom_filter)}")

    # Count by zoom level
    zoom_counts = {}
    for src, dst, q in tasks:
        z = src.split(os.sep)[-3]
        zoom_counts[z] = zoom_counts.get(z, 0) + 1
    for z in sorted(zoom_counts, key=int):
        print(f"  z{z}: {zoom_counts[z]:,} tiles")

    if args.dry_run:
        print("Dry run — no files written.")
        return

    print(f"\nRecompressing to AVIF q{args.quality} with {args.workers} workers...")
    print(f"Output: {dest_dir}")

    c = Value('i', 0)
    l = Lock()
    start = time.time()
    total_out_bytes = 0

    # Progress reporter
    total = len(tasks)

    with Pool(args.workers, initializer=init_worker, initargs=(c, l)) as pool:
        for result in pool.imap_unordered(recompress_tile, tasks, chunksize=64):
            total_out_bytes += result
            done = c.value
            if done % 10000 == 0 and done > 0:
                elapsed = time.time() - start
                rate = done / elapsed
                eta = (total - done) / rate if rate > 0 else 0
                pct = 100 * done / total
                out_gb = total_out_bytes / 1e9
                print(f"\r  {done:,}/{total:,} ({pct:.1f}%) — {rate:.0f} tiles/s — "
                      f"{out_gb:.1f} GB written — ETA {eta:.0f}s", end="", flush=True)

    elapsed = time.time() - start
    out_gb = total_out_bytes / 1e9
    print(f"\n\nDone! Recompressed {total:,} tiles in {elapsed:.0f}s ({total/elapsed:.0f} tiles/s)")
    print(f"Total output: {out_gb:.2f} GB")


if __name__ == "__main__":
    main()
