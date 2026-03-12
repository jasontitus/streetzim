#!/usr/bin/env python3
"""Download all Copernicus GLO-30 DEM tiles (~26K land tiles, ~30-35 GB).

Downloads 1-degree GeoTIFF tiles from the public AWS S3 bucket.
Ocean tiles return 404 and are skipped. Uses parallel threads for speed.

Usage:
    python3 download_dem.py [--threads 32] [--dest terrain_cache/dem_sources]
"""
import os
import sys
import time
import argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

COPERNICUS_DEM_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)


def download_tile(args):
    """Download a single DEM tile. Returns (status, lat, lon, size_bytes)."""
    lat, lon, dest_dir = args
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    abs_lat = abs(lat)
    abs_lon = abs(lon)

    fname = f"dem_{ns}{abs_lat:02d}_{ew}{abs_lon:03d}.tif"
    fpath = os.path.join(dest_dir, fname)

    # Skip if already cached
    if os.path.exists(fpath) and os.path.getsize(fpath) > 1000:
        return ("cached", lat, lon, os.path.getsize(fpath))

    url = COPERNICUS_DEM_URL.format(ns=ns, lat=abs_lat, ew=ew, lon=abs_lon)
    req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(fpath, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        return ("downloaded", lat, lon, os.path.getsize(fpath))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ("ocean", lat, lon, 0)
        return ("error", lat, lon, 0)
    except Exception:
        return ("error", lat, lon, 0)


def main():
    parser = argparse.ArgumentParser(description="Download Copernicus GLO-30 DEM tiles")
    parser.add_argument("--threads", type=int, default=32, help="Download threads (default: 32)")
    parser.add_argument("--dest", default="terrain_cache/dem_sources",
                        help="Destination directory (default: terrain_cache/dem_sources)")
    args = parser.parse_args()

    os.makedirs(args.dest, exist_ok=True)

    # Build task list: all 1-degree cells
    tasks = []
    for lat in range(-90, 90):
        for lon in range(-180, 180):
            tasks.append((lat, lon, args.dest))

    print(f"Downloading Copernicus GLO-30 DEM tiles")
    print(f"  Cells to check: {len(tasks):,}")
    print(f"  Threads: {args.threads}")
    print(f"  Destination: {args.dest}")
    print()

    cached = 0
    downloaded = 0
    ocean = 0
    errors = 0
    total_bytes = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futures = {pool.submit(download_tile, t): t for t in tasks}
        done = 0
        for future in as_completed(futures):
            status, lat, lon, size = future.result()
            done += 1
            if status == "cached":
                cached += 1
                total_bytes += size
            elif status == "downloaded":
                downloaded += 1
                total_bytes += size
            elif status == "ocean":
                ocean += 1
            else:
                errors += 1

            if done % 500 == 0:
                elapsed = time.time() - start
                rate = done / elapsed if elapsed > 0 else 0
                remaining = (len(tasks) - done) / rate if rate > 0 else 0
                print(
                    f"\r  Progress: {done:,}/{len(tasks):,} "
                    f"({downloaded} new, {cached} cached, {ocean} ocean, {errors} errors) "
                    f"[{total_bytes/1024/1024/1024:.1f} GB] "
                    f"~{remaining/60:.0f}m left",
                    end="", flush=True,
                )

    elapsed = time.time() - start
    print()
    print()
    print(f"Done in {elapsed/60:.1f} minutes")
    print(f"  Downloaded: {downloaded:,} tiles")
    print(f"  Cached:     {cached:,} tiles")
    print(f"  Ocean:      {ocean:,} tiles (404, no data)")
    print(f"  Errors:     {errors:,} tiles")
    print(f"  Total size: {total_bytes/1024/1024/1024:.1f} GB")


if __name__ == "__main__":
    main()
