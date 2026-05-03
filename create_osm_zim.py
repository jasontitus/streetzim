#!/usr/bin/env python3
"""
create_osm_zim.py - Create a ZIM file containing an offline OpenStreetMap viewer.

Downloads OSM data for a specified area, generates vector tiles using tilemaker,
and packages everything into a ZIM file that can be opened in the Kiwix app
(including iOS) for fully offline, client-side rendered maps.

Usage:
    python3 create_osm_zim.py --area "austin" --bbox "-97.95,30.10,-97.55,30.50"
    python3 create_osm_zim.py --area "district-of-columbia" --geofabrik "north-america/us/district-of-columbia"
    python3 create_osm_zim.py --pbf mydata.osm.pbf --name "My Area" --bbox "-97.9,30.1,-97.5,30.5"

The resulting .zim file contains:
  - MapLibre GL JS (client-side vector tile renderer)
  - Vector tiles in MVT/PBF format (OpenMapTiles schema)
  - SDF font glyphs for label rendering
  - A lightweight map style

Size comparison (typical city):
  - OSM PBF extract: ~20-50 MB
  - Vector tiles (z0-14): ~10-30 MB
  - Final ZIM file: ~15-40 MB
  - Equivalent raster tiles (z0-18): ~2-10 GB (50-200x larger!)
"""

import argparse
import datetime
import glob
import gzip
import html as html_mod
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

# Wrap print to auto-flush step/progress lines so monitoring never sees stale output.
_builtin_print = print
def print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    _builtin_print(*args, **kwargs)


SCRIPT_DIR = Path(__file__).parent.resolve()
RESOURCES_DIR = SCRIPT_DIR / "resources"
TILEMAKER_CONFIG = RESOURCES_DIR / "tilemaker" / "config-openmaptiles.json"
TILEMAKER_PROCESS = RESOURCES_DIR / "tilemaker" / "process-openmaptiles.lua"
VIEWER_DIR = RESOURCES_DIR / "viewer"


def log_viewer_freshness():
    """Print viewer-HTML fingerprints at the top of every build.

    Reasoning: on 2026-04-22 I lost ~hours of builds because a git-
    worktree's `resources/viewer/index.html` was 2h stale relative to
    the main tree. Every ZIM built from that worktree baked the old
    viewer (no `ws` website rendering, stale Route-button code paths,
    etc.). Nothing warned about it until a user downloaded a ZIM and
    noticed the regression.
    Now every build logs the viewer files' size + mtime + first-512-
    byte SHA-1 prefix + the most recent git commit that touched them.
    If the file is older than the commit or missing expected strings,
    a loud WARNING prints so future-me catches it before packaging.
    """
    import hashlib
    import datetime as _dt
    print("  --- viewer HTML fingerprint ---")
    expected_markers = {
        "index.html": ["enrich.ws", "item.ws", "places-link"],
        "places.html": ["Search near", "near-input"],
    }
    worst_age_mtime = None
    warned = False
    for name in ("index.html", "places.html"):
        p = VIEWER_DIR / name
        if not p.exists():
            print(f"    {name}: MISSING at {p}")
            warned = True
            continue
        st = p.stat()
        mtime = _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        sha = hashlib.sha1(p.read_bytes()[:512]).hexdigest()[:12]
        try:
            gitlog = subprocess.run(
                ["git", "-C", str(SCRIPT_DIR), "log", "-1",
                 "--format=%ai  %h  %s", "--", f"resources/viewer/{name}"],
                capture_output=True, text=True, timeout=5)
            last_commit = (gitlog.stdout or "").strip() or "(no git)"
        except Exception:
            last_commit = "(git unavailable)"
        print(f"    {name}: {st.st_size:>8d} B  mtime={mtime}  sha1={sha}")
        print(f"      last commit: {last_commit}")
        # Marker check — catches "somebody renamed the field, file on
        # disk still has the old name" regressions before ZIM packaging.
        body = p.read_text(errors="replace")
        missing = [m for m in expected_markers[name] if m not in body]
        if missing:
            print(f"    ⚠️  {name}: MISSING EXPECTED STRINGS {missing} — "
                  "viewer is probably stale. Packaging anyway, but the "
                  "resulting ZIM will miss features.")
            warned = True
    if not warned:
        print("    viewer freshness OK")
    print()

# Geofabrik base URL for downloading OSM extracts
GEOFABRIK_BASE = "https://download.geofabrik.de"

# Sentinel-2 Cloudless satellite tile service (EOX, CC BY-NC-SA 4.0 for 2021 vintage)
SATELLITE_TILE_URL = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/default/g/{z}/{y}/{x}.jpg"

# Copernicus GLO-30 DEM tile URL (public S3, no auth)
COPERNICUS_DEM_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)

# Copernicus GLO-90 DEM fallback — broader coverage than GLO-30 (includes
# Georgia, Armenia, Azerbaijan and other restricted-region countries).
# Used when GLO-30 returns 404. 90m resolution vs 30m but fine for hillshade.
COPERNICUS_DEM_URL_GLO90 = (
    "https://copernicus-dem-90m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_30_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_30_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)

# MapLibre GL JS version to bundle
MAPLIBRE_VERSION = "5.23.0"
MAPLIBRE_CDN = f"https://unpkg.com/maplibre-gl@{MAPLIBRE_VERSION}/dist"


def download_file(url, dest, desc=None):
    """Download a file with progress indication."""
    desc = desc or os.path.basename(dest)
    print(f"  Downloading {desc}...")
    print(f"    URL: {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "create_osm_zim/1.0"})
        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded * 100 // total
                        mb = downloaded / (1024 * 1024)
                        print(f"\r    {mb:.1f} MB ({pct}%)", end="", flush=True)
            print()
    except Exception as e:
        print(f"\n    Error downloading: {e}")
        raise


def download_satellite_tiles(bbox_str, dest_dir, max_zoom=14, webp_quality=65,
                              sat_format="webp", sat_quality=None, tile_size=256):
    """Download Sentinel-2 Cloudless satellite tiles for a bounding box.

    Downloads JPEG tiles from the EOX Sentinel-2 Cloudless WMTS service,
    converts them to the specified format, and stores them as
    {dest_dir}/{z}/{x}/{y}.{ext}.

    When tile_size=512, four 256px source tiles are stitched into one 512px
    tile, halving the tile count and improving compression.

    Supported formats: "webp", "avif".

    Returns the number of output tiles produced.
    """
    import io
    import math
    import time
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from PIL import Image

    if sat_format == "avif":
        # Pillow >= 10.0 has native AVIF support; older versions need pillow-avif-plugin
        from PIL import features
        if not features.check("avif"):
            try:
                import pillow_avif  # noqa: F401 — registers AVIF codec with Pillow
            except ImportError:
                print("    Warning: AVIF not supported (need Pillow >= 10 or pillow-avif-plugin), falling back to webp")
                sat_format = "webp"

    quality = sat_quality if sat_quality is not None else webp_quality
    ext = sat_format  # "webp" or "avif"

    bbox = parse_bbox(bbox_str)
    minlon, minlat, maxlon, maxlat = bbox

    os.makedirs(dest_dir, exist_ok=True)
    # Shared source cache for original JPEG tiles (download once, encode to any format)
    source_cache_dir = os.path.join(SCRIPT_DIR, "satellite_cache_sources")
    os.makedirs(source_cache_dir, exist_ok=True)
    total_downloaded = 0
    total_skipped = 0
    total_bytes_jpeg = 0
    total_bytes_out = 0
    lock = threading.Lock()

    # Collect existing format caches for transcoding fallback
    _format_caches = []
    for d in sorted(glob.glob(os.path.join(SCRIPT_DIR, "satellite_cache_*_*"))):
        if os.path.isdir(d) and d != dest_dir and d != source_cache_dir:
            # Extract extension from dir name (e.g. satellite_cache_webp_256 → webp)
            parts = os.path.basename(d).replace("satellite_cache_", "").split("_")
            if parts:
                _format_caches.append((d, parts[0]))
    # Also check the legacy satellite_cache/ (WebP tiles)
    legacy_cache = os.path.join(SCRIPT_DIR, "satellite_cache")
    if os.path.isdir(legacy_cache) and legacy_cache != dest_dir:
        _format_caches.append((legacy_cache, "webp"))

    def _fetch_source_tile(z, x, y):
        """Get a single 256px tile, using source cache if available.
        Returns (PIL.Image, jpeg_bytes_len). Checks: JPEG source cache →
        existing format caches (transcode) → network download."""
        # Check JPEG source cache first
        cache_path = os.path.join(source_cache_dir, str(z), str(x), f"{y}.jpg")
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            try:
                return Image.open(cache_path), os.path.getsize(cache_path)
            except Exception:
                pass  # Corrupted cache file, try next

        # Check existing format caches (transcode from WebP/AVIF rather than re-download)
        for cache_dir, cache_ext in _format_caches:
            cached = os.path.join(cache_dir, str(z), str(x), f"{y}.{cache_ext}")
            if os.path.exists(cached) and os.path.getsize(cached) > 0:
                try:
                    return Image.open(cached), 0
                except Exception:
                    pass

        # Download from network
        url = SATELLITE_TILE_URL.format(z=z, x=x, y=y)
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    jpg_data = resp.read()
                # Save to source cache
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, 'wb') as f:
                    f.write(jpg_data)
                return Image.open(io.BytesIO(jpg_data)), len(jpg_data)
            except Exception as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                else:
                    print(f"\n    Warning: failed to download z{z}/{x}/{y}: {e}")
        return None, 0

    def _save_image(img, path):
        """Save image in the configured format. Returns output file size."""
        if sat_format == "avif":
            img.save(path, "AVIF", quality=quality, speed=6)
        else:
            img.save(path, "WEBP", quality=quality)
        return os.path.getsize(path)

    def _process_tile_256(z, x, y):
        """Download and convert a single 256px tile. Returns (downloaded, jpeg_bytes, out_bytes)."""
        tile_dir = os.path.join(dest_dir, str(z), str(x))
        tile_path = os.path.join(tile_dir, f"{y}.{ext}")

        if os.path.exists(tile_path) and os.path.getsize(tile_path) > 0:
            return (False, 0, 0)

        os.makedirs(tile_dir, exist_ok=True)
        img, jpeg_size = _fetch_source_tile(z, x, y)
        if img is None:
            return (False, 0, 0)
        out_size = _save_image(img, tile_path)
        return (True, jpeg_size, out_size)

    def _process_tile_512(z, x0, y0):
        """Download four 256px source tiles at z+1 and stitch into one 512px tile.

        The output tile is stored at coordinates (z, x0, y0) but contains the
        pixel data of source tiles (z+1, x0*2..x0*2+1, y0*2..y0*2+1).

        Returns (downloaded, jpeg_bytes, out_bytes).
        """
        tile_dir = os.path.join(dest_dir, str(z), str(x0))
        tile_path = os.path.join(tile_dir, f"{y0}.{ext}")

        if os.path.exists(tile_path) and os.path.getsize(tile_path) > 0:
            return (False, 0, 0)

        os.makedirs(tile_dir, exist_ok=True)

        # Fetch 4 source tiles from one zoom level deeper
        sz = z + 1
        sx0, sy0 = x0 * 2, y0 * 2
        stitched = Image.new("RGB", (512, 512))
        total_jpeg = 0
        for dy in range(2):
            for dx in range(2):
                img, jpeg_size = _fetch_source_tile(sz, sx0 + dx, sy0 + dy)
                total_jpeg += jpeg_size
                if img is not None:
                    stitched.paste(img, (dx * 256, dy * 256))

        if total_jpeg == 0:
            return (False, 0, 0)

        out_size = _save_image(stitched, tile_path)
        return (True, total_jpeg, out_size)

    max_workers = min(32, (os.cpu_count() or 4) * 4)

    if tile_size == 512:
        print(f"    Mode: 512px tiles ({sat_format} q{quality})")
        print(f"    Stitching 4x source 256px tiles per output tile")
    else:
        print(f"    Mode: 256px tiles ({sat_format} q{quality})")

    for z in range(0, max_zoom + 1):
        # Calculate tile range at this zoom level
        if tile_size == 512:
            # For 512px tiles, we need source tiles at z+1 but store at z.
            # The output tile grid at zoom z covers the same area as the
            # 256px grid at zoom z, but each tile has 4x the source pixels.
            src_z = z + 1
            n = 2 ** src_z
        else:
            n = 2 ** z

        x_min = int(n * (minlon + 180) / 360)
        x_max = int(n * (maxlon + 180) / 360)
        lat_rad_min = math.radians(minlat)
        lat_rad_max = math.radians(maxlat)
        y_max = int(n * (1 - math.log(math.tan(lat_rad_min) + 1 / math.cos(lat_rad_min)) / math.pi) / 2)
        y_min = int(n * (1 - math.log(math.tan(lat_rad_max) + 1 / math.cos(lat_rad_max)) / math.pi) / 2)

        x_min = max(0, x_min)
        x_max = min(n - 1, x_max)
        y_min = max(0, y_min)
        y_max = min(n - 1, y_max)

        if tile_size == 512:
            # Convert source tile range to output tile range (halve coordinates)
            out_x_min = x_min // 2
            out_x_max = x_max // 2
            out_y_min = y_min // 2
            out_y_max = y_max // 2
            tile_count = (out_x_max - out_x_min + 1) * (out_y_max - out_y_min + 1)
            print(f"    z{z}: {tile_count} tiles ({out_x_max - out_x_min + 1}x{out_y_max - out_y_min + 1}) [512px, src z{src_z}]")
            process_fn = _process_tile_512
            tile_coords = [(z, x, y) for x in range(out_x_min, out_x_max + 1)
                           for y in range(out_y_min, out_y_max + 1)]
        else:
            tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)
            print(f"    z{z}: {tile_count} tiles ({x_max - x_min + 1}x{y_max - y_min + 1})")
            process_fn = _process_tile_256
            tile_coords = [(z, x, y) for x in range(x_min, x_max + 1)
                           for y in range(y_min, y_max + 1)]

        # Small zoom levels: process sequentially
        if tile_count <= 10:
            for coords in tile_coords:
                downloaded, jpeg_bytes, out_bytes = process_fn(*coords)
                if downloaded:
                    total_downloaded += 1
                    total_bytes_jpeg += jpeg_bytes
                    total_bytes_out += out_bytes
                else:
                    total_skipped += 1
            continue

        # Larger zoom levels: process in parallel
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(process_fn, *t): t for t in tile_coords}
            for future in as_completed(futures):
                downloaded, jpeg_bytes, out_bytes = future.result()
                if downloaded:
                    total_downloaded += 1
                    total_bytes_jpeg += jpeg_bytes
                    total_bytes_out += out_bytes
                else:
                    total_skipped += 1
                completed += 1
                if completed % 500 == 0:
                    print(f"\r    Processed {total_downloaded} tiles ({total_skipped} cached)...", end="", flush=True)

    print(f"\r    Produced {total_downloaded} satellite tiles ({total_skipped} cached)")
    if total_bytes_jpeg > 0:
        saved_mb = (total_bytes_jpeg - total_bytes_out) / (1024 * 1024)
        ratio = (1 - total_bytes_out / total_bytes_jpeg) * 100
        print(f"    {sat_format.upper()} compression saved {saved_mb:.1f} MB ({ratio:.0f}% vs JPEG source)")
    return total_downloaded + total_skipped


def stitch_satellite_image(satellite_dir, max_zoom, bbox_str, webp_quality=80):
    """Stitch max-zoom satellite tiles into a single image.

    Returns (image_path, coordinates) where coordinates is the MapLibre
    image source format: [[west,north],[east,north],[east,south],[west,south]].
    """
    import math

    from PIL import Image

    bbox = parse_bbox(bbox_str)
    minlon, minlat, maxlon, maxlat = bbox
    n = 2 ** max_zoom

    x_min = int(n * (minlon + 180) / 360)
    x_max = int(n * (maxlon + 180) / 360)
    lat_rad_min = math.radians(minlat)
    lat_rad_max = math.radians(maxlat)
    y_max = int(n * (1 - math.log(math.tan(lat_rad_min) + 1 / math.cos(lat_rad_min)) / math.pi) / 2)
    y_min = int(n * (1 - math.log(math.tan(lat_rad_max) + 1 / math.cos(lat_rad_max)) / math.pi) / 2)

    x_min = max(0, x_min)
    x_max = min(n - 1, x_max)
    y_min = max(0, y_min)
    y_max = min(n - 1, y_max)

    cols = x_max - x_min + 1
    rows = y_max - y_min + 1
    width = cols * 256
    height = rows * 256
    print(f"    Stitching {cols}x{rows} tiles ({width}x{height} px) from z{max_zoom}...")

    stitched = Image.new("RGB", (width, height))
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            tile_path = os.path.join(satellite_dir, str(max_zoom), str(x), f"{y}.webp")
            if os.path.exists(tile_path):
                tile_img = Image.open(tile_path)
                px = (x - x_min) * 256
                py = (y - y_min) * 256
                stitched.paste(tile_img, (px, py))

    output_path = os.path.join(satellite_dir, "stitched.webp")
    stitched.save(output_path, "WEBP", quality=webp_quality)
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"    Stitched image: {size_mb:.1f} MB")

    # Geographic bounds of the stitched image (tile edges, not bbox)
    west = x_min / n * 360 - 180
    east = (x_max + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y_min / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y_max + 1) / n))))

    # MapLibre image source coordinates: [lng, lat] for each corner
    coordinates = [
        [west, north],   # top-left
        [east, north],   # top-right
        [east, south],   # bottom-right
        [west, south],   # bottom-left
    ]

    return output_path, coordinates


def _generate_one_terrain_tile(args):
    """Generate a single terrain-RGB tile. Module-level for multiprocessing.

    Each process opens its own handle to the VRT/mosaic — GDAL reads only
    the pixels needed from the underlying GeoTIFFs."""
    mosaic_file, tile_x, tile_y, z, dest_dir_local, tb_west, tb_south, tb_east, tb_north = args
    import rasterio
    from rasterio.warp import reproject, Resampling, transform_bounds
    from rasterio.transform import from_bounds
    import numpy as np
    from PIL import Image

    tile_bounds_3857 = transform_bounds(
        "EPSG:4326", "EPSG:3857", tb_west, tb_south, tb_east, tb_north
    )
    tile_transform = from_bounds(*tile_bounds_3857, 256, 256)

    elevation = np.zeros((1, 256, 256), dtype=np.float32)
    with rasterio.open(mosaic_file) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=elevation,
            dst_transform=tile_transform,
            dst_crs="EPSG:3857",
            resampling=Resampling.cubic,
        )

    elev = elevation[0]
    elev = np.round(elev / 10.0) * 10.0  # quantize to 10m for ~74% compression savings
    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)

    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)

    img = Image.fromarray(np.stack([r, g, b], axis=-1))
    tile_dir_path = os.path.join(dest_dir_local, str(z), str(tile_x))
    os.makedirs(tile_dir_path, exist_ok=True)
    tile_path = os.path.join(tile_dir_path, f"{tile_y}.webp")
    img.save(tile_path, "WEBP", lossless=True)


def generate_terrain_tiles(bbox_str, dest_dir, max_zoom=12,
                           low_zoom_world_vrt=None):
    """Download Copernicus GLO-30 DEM and generate terrain-RGB tiles.

    Downloads 1-degree GeoTIFF tiles from AWS, mosaics them, then generates
    Mapbox terrain-RGB tiles as lossless WebP using rasterio + mercantile.
    Tiles are stored as {dest_dir}/{z}/{x}/{y}.webp.

    ``low_zoom_world_vrt`` (optional): if provided, z=0-7 tiles are
    generated from that DEM instead of the region-bbox mosaic. Prevents
    the bbox-edge stripe bug at low zooms where a tile's footprint
    extends past the region and zero-fills outside. z=8+ still use the
    regional mosaic (fine-grained detail, no stripe risk since each
    tile is small).
    """
    import math
    import io

    bbox = parse_bbox(bbox_str)
    minlon, minlat, maxlon, maxlat = bbox

    os.makedirs(dest_dir, exist_ok=True)
    # Always use the shared DEM sources directory (large raw files, ~547 GB total)
    dem_dir = os.path.join(SCRIPT_DIR, "terrain_cache", "dem_sources")
    os.makedirs(dem_dir, exist_ok=True)

    # Check if terrain generation is already complete for THIS SPECIFIC bbox.
    # The marker encodes the bbox so a Europe build can't fool a US build.
    import mercantile
    bbox_key = f"{minlon:.1f}_{minlat:.1f}_{maxlon:.1f}_{maxlat:.1f}"
    completed_marker = os.path.join(dest_dir, f"COMPLETED_z{max_zoom}_{bbox_key}")
    if os.path.isfile(completed_marker):
        total = sum(
            len([f for f in files if f.endswith(".webp")])
            for _, _, files in os.walk(dest_dir)
            if "dem_sources" not in _
        )
        print(f"    Using {total} cached terrain tiles (generation complete for {bbox_key})")
        return total

    # Fallback: sample z-max tiles at the CORNERS AND CENTER of this bbox
    # to check if they're cached. More robust than just first/last.
    z_max_tiles = list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=max_zoom))
    if z_max_tiles:
        # Sample corners + center of the bbox tile range
        n_tiles = len(z_max_tiles)
        sample_indices = [0, n_tiles//4, n_tiles//2, 3*n_tiles//4, n_tiles-1]
        sample = [z_max_tiles[i] for i in sample_indices if i < n_tiles]
        all_cached = all(
            os.path.isfile(os.path.join(dest_dir, str(max_zoom), str(t.x), f"{t.y}.webp"))
            for t in sample
        )
        if all_cached:
            total = sum(
                len([f for f in files if f.endswith(".webp")])
                for _, _, files in os.walk(dest_dir)
                if "dem_sources" not in _
            )
            print(f"    Using {total} cached terrain tiles")
            return total

    # Determine which 1-degree Copernicus tiles we need.
    # Include a 1-degree BUFFER around the bbox so that tiles at degree
    # boundaries get correct data from neighboring DEM cells.
    tif_paths = []
    for lat in range(math.floor(minlat) - 1, math.floor(maxlat) + 2):
        for lon in range(math.floor(minlon) - 1, math.floor(maxlon) + 2):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            abs_lat = abs(lat)
            abs_lon = abs(lon)
            url = COPERNICUS_DEM_URL.format(ns=ns, lat=abs_lat, ew=ew, lon=abs_lon)
            fname = f"dem_{ns}{abs_lat:02d}_{ew}{abs_lon:03d}.tif"
            fpath = os.path.join(dem_dir, fname)

            # Check for a "no data" marker (empty file left by a previous 404)
            nodata_marker = fpath + ".nodata"
            if os.path.exists(nodata_marker):
                continue

            if not os.path.exists(fpath) or os.path.getsize(fpath) < 1000:
                # Try GLO-30 first, fall back to GLO-90 for restricted regions
                # (Georgia, Armenia, Azerbaijan etc. that 404 on GLO-30).
                glo90_url = COPERNICUS_DEM_URL_GLO90.format(ns=ns, lat=abs_lat, ew=ew, lon=abs_lon)
                downloaded = False
                for try_url, label in [(url, "GLO-30"), (glo90_url, "GLO-90 fallback")]:
                    print(f"    Downloading {ns}{abs_lat:02d} {ew}{abs_lon:03d} ({label})...")
                    req = urllib.request.Request(try_url, headers={"User-Agent": "streetzim/1.0"})
                    try:
                        with urllib.request.urlopen(req, timeout=120) as resp:
                            with open(fpath, "wb") as f:
                                while True:
                                    chunk = resp.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    f.write(chunk)
                        size_mb = os.path.getsize(fpath) / (1024 * 1024)
                        print(f"      {size_mb:.1f} MB ({label})")
                        downloaded = True
                        break
                    except urllib.error.HTTPError as e:
                        if e.code == 404:
                            print(f"      404 on {label}, trying next source...")
                            continue
                        print(f"      Warning: failed to download from {label}: {e}")
                        break
                    except Exception as e:
                        print(f"      Warning: failed to download from {label}: {e}")
                        break
                if not downloaded:
                    # Both GLO-30 and GLO-90 failed — mark as genuinely nodata (ocean)
                    open(nodata_marker, "w").close()
                    continue
            else:
                size_mb = os.path.getsize(fpath) / (1024 * 1024)
                print(f"    Cached: {ns}{abs_lat:02d} {ew}{abs_lon:03d} ({size_mb:.1f} MB)")
            tif_paths.append(fpath)

    if not tif_paths:
        print("    No DEM tiles downloaded, skipping terrain")
        return 0

    # Build a VRT (Virtual Raster) instead of loading all DEMs into memory.
    # A VRT is a lightweight XML file that references source tiles on disk.
    # rasterio reads only the pixels needed for each terrain tile on demand.
    print("    Building VRT from DEM tiles...")
    import rasterio
    import mercantile

    # Use a UNIQUE VRT path per bbox to avoid race conditions when two
    # builds run in parallel and overwrite each other's VRT.
    mosaic_path = os.path.join(dem_dir, f"mosaic_{bbox_key}.vrt")
    try:
        # Use -input_file_list to avoid "Argument list too long" with 24K+ files
        import tempfile as _tmpfile
        with _tmpfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as flist:
            flist.write('\n'.join(tif_paths))
            flist_path = flist.name
        subprocess.run(
            ["gdalbuildvrt", "-overwrite", "-input_file_list", flist_path, mosaic_path],
            check=True, capture_output=True, text=True,
        )
        os.unlink(flist_path)
    except FileNotFoundError:
        # gdalbuildvrt not on PATH — fall back to in-memory merge
        print("    Warning: gdalbuildvrt not found, falling back to in-memory merge")
        from rasterio.merge import merge

        # Reuse cached mosaic if present — saves ~30 min validation + the
        # merge itself for subsequent builds against the same DEM set.
        mosaic_path = os.path.join(dem_dir, "mosaic_4326.tif")
        if os.path.isfile(mosaic_path):
            print(f"    Reusing cached mosaic: {mosaic_path} "
                  f"({os.path.getsize(mosaic_path)/1024/1024:.0f} MB)")
        else:
            # Pre-validate DEMs by reading full band — corrupt files crash merge()
            print(f"    Validating {len(tif_paths)} DEM tiles...")
            valid_paths = []
            for p in tif_paths:
                try:
                    with rasterio.open(p) as _ds:
                        _ds.read(1)
                    valid_paths.append(p)
                except Exception as e:
                    print(f"    Warning: skipping corrupt DEM {os.path.basename(p)}: {e}")
            if not valid_paths:
                print("    No valid DEM tiles, skipping terrain")
                return 0
            # Stream the merge directly to disk with dst_path + mem_limit so we
            # never materialize the full mosaic in memory. World-scale DEM at
            # GLO-30 is 612000x129600 pixels (~600 GB float32) which OOMs the
            # box; chunked streaming keeps RSS bounded by mem_limit (MB).
            print(f"    Merging {len(valid_paths)} validated DEM tiles "
                  f"-> {mosaic_path} (streaming)...")
            datasets = [rasterio.open(p) for p in valid_paths]
            try:
                merge(datasets, dst_path=mosaic_path, mem_limit=2048)
            finally:
                for ds in datasets:
                    ds.close()
            print(f"    Mosaic written: "
                  f"{os.path.getsize(mosaic_path)/1024/1024:.0f} MB")

    # Generate terrain-RGB tiles using multiprocessing.
    # Each process opens its own handle to the VRT file — GDAL reads only the
    # pixels needed per tile from the underlying GeoTIFFs. No shared state.
    # Uses a streaming generator so workers start immediately without building
    # a multi-million element list in memory (world z12 = 16.7M tiles).
    print(f"    Generating terrain-RGB tiles (z0-{max_zoom})...")
    count = 0
    cached = 0
    import multiprocessing

    num_workers = min(os.cpu_count() or 4, 16)  # cap at 16 to limit I/O contention

    for z in range(0, max_zoom + 1):
        # For z=0-7, prefer the world-coverage VRT if supplied — those
        # tiles span regions past the bbox, so a regional mosaic would
        # zero-fill outside and produce the bbox-edge stripe bug
        # (Iran 33°N, Butte MT, east-Iran 65°E). z=8+ stays on the
        # regional mosaic (small tiles, full DEM resolution, no stripe).
        vrt_for_z = (low_zoom_world_vrt
                     if (z <= 7 and low_zoom_world_vrt
                         and os.path.isfile(low_zoom_world_vrt))
                     else mosaic_path)

        # Streaming generator — yields args one at a time, skipping cached tiles
        def tile_arg_gen(zoom, _vrt=vrt_for_z):
            for tile in mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=zoom):
                # Skip already-cached tiles
                tile_path = os.path.join(dest_dir, str(zoom), str(tile.x), f"{tile.y}.webp")
                if os.path.isfile(tile_path):
                    continue
                b = mercantile.bounds(tile)
                yield (_vrt, tile.x, tile.y, zoom, dest_dir,
                       b.west, b.south, b.east, b.north)

        # Count total and cached for this zoom (estimate for large zooms)
        if z <= 8:
            all_tiles = list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z))
            total_at_z = len(all_tiles)
            cached_at_z = sum(1 for t in all_tiles
                              if os.path.isfile(os.path.join(dest_dir, str(z), str(t.x), f"{t.y}.webp")))
        else:
            # For large zoom levels, estimate count from 4x previous zoom
            import math
            n = 2 ** z
            x_min = int((minlon + 180) / 360 * n)
            x_max = int((maxlon + 180) / 360 * n)
            y_min = int((1 - math.log(math.tan(math.radians(maxlat)) + 1/math.cos(math.radians(maxlat))) / math.pi) / 2 * n)
            y_max = int((1 - math.log(math.tan(math.radians(max(minlat, -85))) + 1/math.cos(math.radians(max(minlat, -85)))) / math.pi) / 2 * n)
            total_at_z = (x_max - x_min + 1) * (y_max - y_min + 1)
            # Count cached from existing directory
            cached_at_z = sum(
                len([f for f in files if f.endswith(".webp")])
                for _, _, files in os.walk(os.path.join(dest_dir, str(z)))
            ) if os.path.isdir(os.path.join(dest_dir, str(z))) else 0

        need = total_at_z - cached_at_z
        if need <= 0:
            cached += cached_at_z
            print(f"      z{z}: {total_at_z} tiles (all cached)")
            continue

        print(f"      z{z}: {total_at_z} tiles ({cached_at_z} cached, {need} to generate)")
        z_count = 0

        if total_at_z <= 10:
            for args in tile_arg_gen(z):
                _generate_one_terrain_tile(args)
                z_count += 1
                count += 1
        else:
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(num_workers) as pool:
                for _ in pool.imap_unordered(_generate_one_terrain_tile,
                                              tile_arg_gen(z), chunksize=256):
                    z_count += 1
                    count += 1
                    if z_count % 5000 == 0:
                        print(f"\r      z{z}: {z_count}/{need} generated...", end="", flush=True)

        cached += cached_at_z
        print(f"\r      z{z}: {z_count} generated, {cached_at_z} cached          ")

    print(f"    Terrain complete: {count} generated, {cached} cached")
    # Write completion marker so future builds skip terrain entirely
    with open(completed_marker, "w") as f:
        f.write(f"{count + cached}\n")
    return count + cached


def search_detail_html(name, kind_label, lat, lon, map_hash, enrich=None):
    """HTML for a search-result detail page (`search/<slug>.html`).

    CTAs: "Directions to here" + "View on map" (no auto-redirect any
    more). The viewer parses `index.html#dest=lat,lon&label=…` on load
    and pops the routing panel open — see `applyHash` in
    `resources/viewer/index.html`.

    `enrich` is an optional dict sourced from Overture's places theme:
        {"ws": website, "p": phone, "soc": [social urls],
         "brand": brand name, "wd": wikidata Q-ID, "cat": category}
    Rendered as a compact contact block below the kind label when
    any field is present. Empty / missing fields are skipped so the
    page stays readable for plain OSM-only POIs.

    Note: the enrichment key for *website* is `ws`, not `w`. `w` is
    reserved for the Wikipedia tag ("en:Article_Title") that OSM POIs
    carry in the same record — colliding the two corrupts downstream
    consumers (mcpzim reads `rec["w"]` as a wiki title; feeding it a
    URL breaks article lookup).
    """
    safe_name = html_mod.escape(name)
    safe_kind = html_mod.escape(kind_label)
    label_q = urllib.parse.quote(name, safe="")
    dest_hash = f"dest={lat},{lon}&label={label_q}"

    enrich = enrich or {}
    contact_html = ""
    contact_parts = []
    if enrich.get("brand"):
        contact_parts.append(
            f'<p class="brand">{html_mod.escape(enrich["brand"])}</p>')
    links = []
    if enrich.get("ws"):
        w = enrich["ws"]
        w_show = html_mod.escape(w)
        w_attr = html_mod.escape(w, quote=True)
        links.append(
            f'<a href="{w_attr}" target="_blank" rel="noopener noreferrer">'
            f'🌐 {w_show}</a>')
    if enrich.get("p"):
        p = enrich["p"]
        p_attr = html_mod.escape(p.replace(" ", ""), quote=True)
        links.append(
            f'<a href="tel:{p_attr}">📞 {html_mod.escape(p)}</a>')
    for s in (enrich.get("soc") or [])[:3]:
        s_attr = html_mod.escape(s, quote=True)
        host = s.lower()
        if "facebook" in host:   g = "Facebook"
        elif "instagram" in host: g = "Instagram"
        elif "twitter" in host or "x.com" in host: g = "X / Twitter"
        elif "tiktok" in host:   g = "TikTok"
        else:                    g = "Social"
        links.append(
            f'<a href="{s_attr}" target="_blank" rel="noopener noreferrer">'
            f'{g}</a>')
    if enrich.get("wd"):
        wd = html_mod.escape(enrich["wd"], quote=True)
        links.append(
            f'<a href="https://www.wikidata.org/wiki/{wd}" '
            'target="_blank" rel="noopener noreferrer">Wikidata</a>')
    if links:
        contact_parts.append(
            '<ul class="contact">' +
            "".join(f'<li>{l}</li>' for l in links) +
            '</ul>')
    if contact_parts:
        contact_html = "".join(contact_parts)

    return (
        '<!DOCTYPE html><html><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{safe_name}</title>'
        '<style>'
        'body{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;'
        'margin:0;padding:24px;max-width:640px;color:#1a1a1a;'
        'background:#fafafa;line-height:1.45}'
        'h1{margin:0 0 4px;font-size:1.6rem}'
        'p.kind{margin:0 0 14px;color:#666;font-size:0.95rem}'
        'p.brand{margin:0 0 10px;color:#666;font-style:italic;font-size:0.95rem}'
        'ul.contact{list-style:none;padding:0;margin:0 0 18px;'
        'display:flex;flex-direction:column;gap:6px;font-size:0.95rem}'
        'ul.contact a{color:#0a7cff;text-decoration:none;word-break:break-all}'
        'ul.contact a:hover{text-decoration:underline}'
        'p.coords{margin:18px 0 0;color:#888;font-size:0.85rem;'
        'font-family:ui-monospace,Menlo,monospace}'
        '.cta{display:flex;flex-direction:column;gap:10px;margin-top:14px}'
        '.cta a{display:block;padding:12px 16px;border-radius:10px;'
        'text-decoration:none;font-weight:600;text-align:center;'
        'border:1px solid #d0d0d0;color:#1a1a1a;background:#fff}'
        '.cta a.primary{background:#0a7cff;color:#fff;border-color:#0a7cff}'
        '.cta a:active{transform:scale(0.99)}'
        '@media(prefers-color-scheme:dark){'
        'body{background:#111;color:#eee}p.kind,p.brand{color:#aaa}p.coords{color:#888}'
        '.cta a{background:#1c1c1c;border-color:#333;color:#eee}'
        '.cta a.primary{background:#0a7cff;color:#fff;border-color:#0a7cff}}'
        '</style>'
        '</head><body>'
        f'<h1>{safe_name}</h1>'
        f'<p class="kind">{safe_kind}</p>'
        f'{contact_html}'
        # Search detail pages live at `search/<slug>.html` inside the
        # ZIM. A bare `index.html#...` resolves to `search/index.html`
        # (which doesn't exist) — zimcheck flagged hundreds of these
        # as broken internal URLs and Kiwix's library validator
        # treats the whole ZIM as Fail. Use `../index.html` so the
        # link reaches the viewer at the ZIM root regardless of
        # how the host (Kiwix Desktop, kiwix-serve, our PWA's SW)
        # serves the path.
        '<div class="cta">'
        f'<a class="primary" href="../index.html#{dest_hash}">'
        'Directions to here</a>'
        f'<a href="../index.html#{map_hash}">View on map</a>'
        '</div>'
        f'<p class="coords">{lat:.5f}, {lon:.5f}</p>'
        '</body></html>'
    )


def download_osm_extract(geofabrik_path, dest):
    """Download an OSM PBF extract from Geofabrik (or planet.osm.org for planet)."""
    if geofabrik_path == "planet":
        url = "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf"
    else:
        url = f"{GEOFABRIK_BASE}/{geofabrik_path}-latest.osm.pbf"
    download_file(url, dest, f"OSM extract ({geofabrik_path})")


def extract_bbox_from_pbf(pbf_path, bbox, output_path):
    """Extract a bounding box from a PBF file using osmium."""
    print(f"  Extracting bbox {bbox} from PBF...")
    cmd = [
        "osmium", "extract",
        "--bbox", bbox,
        "--strategy", "complete_ways",
        "--overwrite",
        "-o", str(output_path),
        str(pbf_path),
    ]
    subprocess.run(cmd, check=True)
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"    Extracted: {size_mb:.1f} MB")


def generate_tiles(pbf_path, mbtiles_path, bbox=None, fast=False, store=None):
    """Generate vector tiles from OSM PBF using tilemaker."""
    print("  Generating vector tiles with tilemaker...")
    cmd = [
        "tilemaker",
        "--input", str(pbf_path),
        "--output", str(mbtiles_path),
        "--config", str(TILEMAKER_CONFIG),
        "--process", str(TILEMAKER_PROCESS),
        "--skip-integrity",
    ]
    if bbox:
        cmd.extend(["--bbox", bbox])
    if fast:
        cmd.append("--fast")
        print("    Using --fast mode (trades RAM for speed)")
    if store:
        cmd.extend(["--store", str(store)])
        print(f"    Using on-disk store: {store}")
    subprocess.run(cmd, check=True)
    size_mb = os.path.getsize(mbtiles_path) / (1024 * 1024)
    print(f"    Generated MBTiles: {size_mb:.1f} MB")


def get_mbtiles_info(mbtiles_path):
    """Get metadata and tile count from MBTiles without loading tiles."""
    conn = sqlite3.connect(str(mbtiles_path))
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT name, value FROM metadata")
        metadata = dict(cursor.fetchall())
    except sqlite3.OperationalError:
        metadata = {}
    cursor.execute("SELECT COUNT(*) FROM tiles")
    tile_count = cursor.fetchone()[0]
    conn.close()
    return metadata, tile_count


def iter_tiles_from_mbtiles(mbtiles_path, zoom_level=None, bbox=None, max_zoom=None):
    """Yield (z, x, y, data) tuples from MBTiles, streaming from SQLite.

    If zoom_level is specified, only yields tiles at that zoom.
    If max_zoom is specified (and zoom_level is not), yields tiles at zoom <= max_zoom.
    If bbox is specified as (minlon, minlat, maxlon, maxlat), only yields
    tiles that intersect the bounding box.
    Yields in (z, x, y) sorted order for deterministic ZIM insertion.
    """
    import math

    conn = sqlite3.connect(str(mbtiles_path))
    cursor = conn.cursor()

    # Whole-world bbox: drop the per-zoom column/row index lookups and use
    # the rowid-sequential scan path instead. World bbox at z13 has 67M
    # tiles; the index lookup forces a random heap fetch per tile_data BLOB
    # against a 113 GB MBTiles, which is ~1500x slower than scanning the
    # heap in rowid order (sqlite stores rows in zoom-major order from
    # tilemaker's insert pattern, so z<=max_zoom rows are contiguous in
    # the early part of the file).
    if bbox:
        _minlon, _minlat, _maxlon, _maxlat = bbox
        if (_minlon <= -179.0 and _maxlon >= 179.0
                and _minlat <= -84.0 and _maxlat >= 84.0):
            bbox = None

    if bbox:
        import mercantile
        minlon, minlat, maxlon, maxlat = bbox

        # Query per zoom level with SQL-level column/row filtering
        # This avoids reading 100+ GB of out-of-bbox tiles through Python
        zoom_min = 0
        if zoom_level is not None:
            zoom_min = zoom_level
            zoom_max = zoom_level
        elif max_zoom is not None:
            zoom_max = max_zoom
        else:
            zoom_max = 14

        for z in range(zoom_min, zoom_max + 1):
            # Get tile column/row bounds for this zoom
            tiles_in_bbox = list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z))
            if not tiles_in_bbox:
                continue
            min_col = min(t.x for t in tiles_in_bbox)
            max_col = max(t.x for t in tiles_in_bbox)
            # Convert XYZ y to TMS y for SQL filter
            n = 1 << z
            min_tms_row = min(n - 1 - t.y for t in tiles_in_bbox)
            max_tms_row = max(n - 1 - t.y for t in tiles_in_bbox)

            cursor.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data "
                "FROM tiles WHERE zoom_level = ? "
                "AND tile_column >= ? AND tile_column <= ? "
                "AND tile_row >= ? AND tile_row <= ? "
                "ORDER BY tile_column, tile_row",
                (z, min_col, max_col, min_tms_row, max_tms_row),
            )
            for zz, x, tms_y, data in cursor:
                y = n - 1 - tms_y
                yield zz, x, y, data
    else:
        if zoom_level is not None:
            cursor.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data "
                "FROM tiles WHERE zoom_level = ? ORDER BY zoom_level, tile_column, tile_row",
                (zoom_level,),
            )
        elif max_zoom is not None:
            # ORDER BY rowid drives a sequential heap scan rather than an
            # index-driven query that does random rowid lookups for each
            # tile_data BLOB. On a 113 GB world MBTiles backed by spinning
            # disks the difference is ~30 min vs ~22 hr.
            cursor.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data "
                "FROM tiles WHERE zoom_level <= ? ORDER BY rowid",
                (max_zoom,),
            )
        else:
            cursor.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data "
                "FROM tiles ORDER BY zoom_level, tile_column, tile_row"
            )
        for z, x, tms_y, data in cursor:
            y = (1 << z) - 1 - tms_y
            yield z, x, y, data
    conn.close()


def extract_tiles_from_mbtiles(mbtiles_path):
    """Extract individual tiles from an MBTiles file.

    Returns a dict of {(z, x, y): tile_data_bytes}.
    MBTiles uses TMS y-coordinate convention, so we flip to XYZ.
    Tiles in MBTiles are typically gzip-compressed already.
    """
    print("  Extracting tiles from MBTiles...")
    conn = sqlite3.connect(str(mbtiles_path))
    cursor = conn.cursor()

    # Get metadata
    try:
        cursor.execute("SELECT name, value FROM metadata")
        metadata = dict(cursor.fetchall())
        print(f"    Format: {metadata.get('format', 'unknown')}")
        print(f"    Name: {metadata.get('name', 'unknown')}")
    except sqlite3.OperationalError:
        metadata = {}

    # Extract tiles
    cursor.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles")
    tiles = {}
    count = 0
    for z, x, tms_y, data in cursor:
        # Convert TMS y to XYZ y
        y = (1 << z) - 1 - tms_y
        tiles[(z, x, y)] = data
        count += 1
        if count % 10000 == 0:
            print(f"\r    Extracted {count} tiles...", end="", flush=True)

    conn.close()
    print(f"\r    Extracted {count} total tiles")
    return tiles, metadata


def generate_sdf_font_glyphs():
    """Generate SDF font glyphs for MapLibre GL JS.

    MapLibre GL JS requires SDF (Signed Distance Field) font glyphs in
    protocol buffer format. Each range covers 256 Unicode codepoints.
    Downloads real SDF fonts from the openmaptiles font CDN.

    Downloads every BMP range the CDN serves so that labels across all
    European scripts render correctly — in particular the General
    Punctuation block (8192-8447, includes U+2013 en dash used in names
    like "Paris-Dakar") and Arabic (1536-1791), which are required for
    continental Europe builds. Ranges that 404 on the CDN are skipped;
    MapLibre falls back to local rendering for missing ranges.
    """
    print("  Downloading SDF font glyphs...")
    fonts = {}

    # MapLibre expects: fonts/{fontstack}/{start}-{end}.pbf
    # Use hyphenated names (no spaces) to avoid URL-encoding issues
    # across different Kiwix implementations (kiwix-serve, Kiwix JS PWA, etc.)
    #
    # Map our style font names → openmaptiles CDN font names
    font_map = {
        "OpenSansRegular": "Open Sans Regular",
        "OpenSansBold": "Open Sans Bold",
        "OpenSansItalic": "Open Sans Italic",
    }

    font_cdn = "https://fonts.openmaptiles.org"

    # Build the full list of (local_name, cdn_name, range_key) tasks so
    # we can parallelize the downloads.
    tasks = []
    for local_name, cdn_name in font_map.items():
        for start in range(0, 65536, 256):
            range_key = f"{start}-{start + 255}"
            tasks.append((local_name, cdn_name, range_key))

    def fetch_one(task):
        local_name, cdn_name, range_key = task
        cdn_encoded = cdn_name.replace(" ", "%20")
        url = f"{font_cdn}/{cdn_encoded}/{range_key}.pbf"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return (local_name, range_key, resp.read(), None)
        except urllib.error.HTTPError as e:
            # 404 means this range has no glyphs in this font — skip it.
            # MapLibre falls back to local rendering on 404.
            return (local_name, range_key, None, f"HTTP {e.code}")
        except Exception as e:
            return (local_name, range_key, None, str(e))

    from concurrent.futures import ThreadPoolExecutor, as_completed
    skipped = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(fetch_one, t) for t in tasks]
        done = 0
        for fut in as_completed(futures):
            local_name, range_key, data, err = fut.result()
            done += 1
            if data is not None:
                fonts[(local_name, range_key)] = data
            elif err and err.startswith("HTTP 404"):
                skipped += 1
            else:
                failed += 1
            if done % 100 == 0:
                print(f"\r    Downloaded {len(fonts)} ranges ({done}/{len(tasks)} checked, {skipped} empty, {failed} errors)...", end="", flush=True)

    print(f"\r    Downloaded {len(fonts)} font range files ({skipped} empty ranges skipped, {failed} errors)       ", flush=True)
    return fonts


def tile_to_lnglat(z, x, y, px, py, extent=4096):
    """Convert vector tile pixel coordinates to lng/lat.

    Args:
        z, x, y: Tile coordinates (XYZ scheme)
        px, py: Pixel coordinates within the tile (0..extent)
        extent: Tile extent (typically 4096)

    Returns:
        (longitude, latitude) tuple
    """
    import math
    n = 2.0 ** z
    lon = (x + px / extent) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (y + py / extent) / n)))
    lat = math.degrees(lat_rad)
    return lon, lat


def build_location_index(mbtiles_path):
    """Build a spatial index that maps (lat, lon) to "City, State".

    Prefer the `reverse_geocoder` package (built on GeoNames data, ships a
    ~30 MB city/admin1 dataset, KNN for fast lookup). It handles the nasty
    cases the OMT-place-layer-based fallback can't — federal districts
    (D.C.), cross-country proximity (Yokohama → Kanagawa, not Sakhalin),
    subnational boundaries (NYC → New York, not New Jersey) — because the
    GeoNames data has the right admin1 for every populated place.

    Falls back to the original MVT-nearest-point approach if the package
    isn't installed, so offline/stripped environments still get a best-
    effort label.
    """
    try:
        import reverse_geocoder as _rg
        # Country code → name lookup. GeoNames returns ISO 3166-1 alpha-2;
        # we prefer the full name for the last-resort fallback.
        _COUNTRY_NAMES = {
            "US": "United States", "JP": "Japan", "CA": "Canada", "GB": "United Kingdom",
            "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy",
            "MX": "Mexico", "BR": "Brazil", "AR": "Argentina", "CN": "China",
            "IN": "India", "RU": "Russia", "AU": "Australia", "NZ": "New Zealand",
            "KR": "South Korea", "KP": "North Korea", "VN": "Vietnam", "TH": "Thailand",
            "ID": "Indonesia", "PH": "Philippines", "MY": "Malaysia", "SG": "Singapore",
            "PL": "Poland", "NL": "Netherlands", "BE": "Belgium", "CH": "Switzerland",
            "AT": "Austria", "CZ": "Czechia", "SE": "Sweden", "NO": "Norway",
            "FI": "Finland", "DK": "Denmark", "IE": "Ireland", "PT": "Portugal",
            "GR": "Greece", "HU": "Hungary", "RO": "Romania", "BG": "Bulgaria",
            "UA": "Ukraine", "TR": "Turkey", "IL": "Israel", "IR": "Iran",
            "SA": "Saudi Arabia", "EG": "Egypt", "ZA": "South Africa", "NG": "Nigeria",
            "KE": "Kenya", "MA": "Morocco", "LV": "Latvia", "LT": "Lithuania",
            "EE": "Estonia", "HK": "Hong Kong", "TW": "Taiwan",
        }
        # Pre-load once — reverse_geocoder is lazy but has a noisy first-call
        # log ("Loading formatted geocoded file..."), so trigger it here.
        _ = _rg.search([(0.0, 0.0)], mode=1)

        def _compose(entry):
            """Produce 'City, State' (or 'City' when the city IS its own admin region)."""
            if not entry:
                return ""
            name = (entry.get("name") or "").strip()
            admin1 = (entry.get("admin1") or "").strip()
            cc = (entry.get("cc") or "").strip()
            if name and admin1:
                # Collapse redundant "Tokyo, Tokyo" / "Moscow, Moscow" /
                # "Mexico City, Mexico City". If admin1 is already contained
                # in name (e.g. name="Washington, D.C.", admin1="Washington, D.C.")
                # or equal to name, just use the name.
                if admin1 == name or admin1 in name:
                    return name
                return f"{name}, {admin1}"
            if name:
                # Fall back to country when admin1 missing
                country = _COUNTRY_NAMES.get(cc, cc)
                return f"{name}, {country}" if country else name
            return ""

        def lookup(lat, lon):
            results = _rg.search([(lat, lon)], mode=1)
            return _compose(results[0]) if results else ""

        print("    Location index: reverse_geocoder (GeoNames)")
        return lookup
    except ImportError:
        # Fall through to the MVT-place-layer-based fallback below.
        pass


    import mapbox_vector_tile
    import math

    places = []  # [(lat, lon, name, class)]

    conn = sqlite3.connect(str(mbtiles_path))
    for z in range(0, 9):
        rows = conn.execute(
            "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = ?",
            (z,),
        ).fetchall()
        for col, tms_row, data in rows:
            y = (1 << z) - 1 - tms_row
            tile_data = data
            if data[:2] == b"\x1f\x8b":
                try:
                    tile_data = gzip.decompress(data)
                except Exception:
                    continue
            try:
                decoded = mapbox_vector_tile.decode(tile_data, y_coord_down=True)
            except Exception:
                continue
            layer = decoded.get("place")
            if not layer:
                continue
            extent = layer.get("extent", 4096)
            for feat in layer.get("features", []):
                props = feat.get("properties", {})
                cls = props.get("class", "")
                if cls not in ("state", "country", "city"):
                    continue
                name = props.get("name:latin") or props.get("name", "")
                if not name:
                    continue
                geom = feat.get("geometry", {})
                coords = geom.get("coordinates")
                if not coords:
                    continue
                gtype = geom.get("type", "")
                try:
                    if gtype == "Point":
                        px, py = coords[0], coords[1]
                    else:
                        continue
                except (IndexError, TypeError):
                    continue
                n = 2.0 ** z
                lon = (col + px / extent) / n * 360.0 - 180.0
                lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (y + py / extent) / n)))
                lat = math.degrees(lat_rad)
                places.append((lat, lon, name, cls))
    conn.close()

    if not places:
        print("    No state/country places found for location index")
        return None

    # Separate by class
    states = [(lat, lon, name) for lat, lon, name, cls in places if cls == "state"]
    countries = [(lat, lon, name) for lat, lon, name, cls in places if cls == "country"]
    cities = [(lat, lon, name) for lat, lon, name, cls in places if cls == "city"]

    # Deduplicate by (coord, name) only — NOT by name alone. Many city names
    # collide across regions (there are ~11 Washingtons, ~50 Springfields, etc.);
    # dedup-by-name would drop all but the first occurrence, which would fool
    # the nearest-neighbor lookup into labeling Dupont Circle as "Silver Spring,
    # Maryland" just because the first Washington encountered happened to be
    # in a different state. Keep one entry per physical place (coord rounded
    # to ~11 m so near-duplicate tile entries across zoom levels collapse).
    def _dedup(items):
        seen = set()
        result = []
        for lat, lon, name in items:
            key = (name, round(lat, 4), round(lon, 4))
            if key not in seen:
                seen.add(key)
                result.append((lat, lon, name))
        return result

    states = _dedup(states)
    countries = _dedup(countries)
    cities = _dedup(cities)

    print(f"    Location index: {len(states)} states, {len(countries)} countries, {len(cities)} cities")

    # Grid-based spatial index for fast nearest-neighbor (no scipy needed).
    # Bucket places into 1-degree grid cells for O(1) average lookup.
    def _build_grid(items):
        grid = {}
        for lat, lon, name in items:
            key = (int(lat), int(lon))
            grid.setdefault(key, []).append((lat, lon, name))
        return grid

    def _nearest_grid(lat, lon, grid):
        best = None
        best_dist = float("inf")
        cell_lat, cell_lon = int(lat), int(lon)
        # Search 5x5 grid neighborhood (handles items near cell boundaries)
        for dlat in range(-2, 3):
            for dlon in range(-2, 3):
                for plat, plon, name in grid.get((cell_lat + dlat, cell_lon + dlon), []):
                    d = (plat - lat) ** 2 + (plon - lon) ** 2
                    if d < best_dist:
                        best_dist = d
                        best = name
        return best

    # Cities are dense (~40k worldwide) — grid indexing pays off. States and
    # countries are sparse (a few thousand each) and their label points are
    # often far from the feature's actual coverage (e.g. California's point
    # is in Madera County, 4° east of Palo Alto — outside a 5×5 cell grid),
    # so we scan them linearly.
    city_grid = _build_grid(cities) if cities else {}

    def _nearest_linear(lat, lon, items):
        best = None
        best_dist = float("inf")
        for plat, plon, name in items:
            dlat = plat - lat
            dlon = plon - lon
            d = dlat * dlat + dlon * dlon
            if d < best_dist:
                best_dist = d
                best = name
        return best

    # Country bounding boxes for the geographies we serve. One country may
    # contribute multiple rectangles — a single bbox per country captures
    # ocean gaps (e.g. Japan's single rectangle would sweep in Primorsky Krai
    # and Sakhalin because they fall in the Sea of Japan between Japan's
    # island chain). Each row is
    #   (min_lat, min_lon, max_lat, max_lon, country_name_as_in_OMT).
    # Proper fix is admin boundary polygons; this table is the pragmatic 95%.
    _COUNTRY_BBOXES = [
        # Japan — archipelago, needs three rectangles to skip the Sea of Japan.
        (30.0,  130.0,  41.6,  142.1,  "Japan"),   # Honshu + Kyushu + Shikoku
        (41.0,  139.5,  45.6,  146.0,  "Japan"),   # Hokkaido
        (24.0,  122.9,  30.0,  131.5,  "Japan"),   # Ryukyu (Okinawa)
        # Korea — peninsula
        (33.0,  125.0,  38.7,  131.9,  "South Korea"),
        (37.5,  124.0,  43.0,  130.7,  "North Korea"),
        # China — main landmass (Tibet on the south, Inner Mongolia top, etc.)
        (18.0,   73.0,  54.0,  135.0,  "China"),
        (22.1,  113.8,  22.6,  114.5,  "Hong Kong"),
        # Russia — main landmass excludes Japanese exclusion zones by lat-split
        (50.0,   19.0,  82.0,  180.0,  "Russia"),  # most of Russia
        (41.0,   19.0,  50.0,  102.0,  "Russia"),  # southwest Russia, skirts China
        (45.6,  131.5,  50.0,  180.0,  "Russia"),  # Far East mainland (Primorsky, Khabarovsk)
        (45.6,  141.5,  54.5,  146.0,  "Russia"),  # Sakhalin
        # North America
        (24.0, -125.0,  49.5,  -66.5,  "United States"),
        (49.0, -141.0,  72.0,  -52.0,  "Canada"),  # main landmass (south. Ontario overlaps US bbox; see note below)
        (14.5, -118.5,  33.0,  -86.5,  "Mexico"),
        # Europe
        (41.0,   -5.5,  51.5,    9.8,  "France"),
        (36.0,   -9.6,  44.0,    3.4,  "Spain"),
        (36.0,    6.5,  47.2,   18.6,  "Italy"),
        (47.2,    5.8,  55.1,   15.1,  "Germany"),
        (49.8,   -7.7,  55.9,    1.9,  "United Kingdom"),
        (51.5,    3.3,  53.8,    7.3,  "Netherlands"),
        (49.5,    2.5,  51.6,    6.4,  "Belgium"),
        (45.7,    5.9,  47.9,   10.6,  "Switzerland"),
        (46.4,    9.5,  49.1,   17.2,  "Austria"),
        (49.0,   14.0,  54.9,   24.2,  "Poland"),
        (55.3,   20.8,  58.1,   28.3,  "Latvia"),
        (57.5,   21.8,  59.8,   28.3,  "Estonia"),
        (53.9,   20.9,  56.5,   26.9,  "Lithuania"),
        # Asia additional
        (6.0,    68.0,  37.1,   97.5,  "India"),
        (23.5,   59.0,  38.0,   78.2,  "Iran"),
        (22.0,   34.0,  31.7,   35.9,  "Egypt"),
        (20.3,  102.0,  28.7,  109.5,  "Vietnam"),
    ]
    def _country_by_bbox(lat, lon):
        for mn_lat, mn_lon, mx_lat, mx_lon, cname in _COUNTRY_BBOXES:
            if mn_lat <= lat <= mx_lat and mn_lon <= lon <= mx_lon:
                return cname
        return None

    # Pre-classify each state to its country (bbox lookup first, nearest-
    # country fallback). Bucketing states per country means we only ever
    # consider in-country candidates at lookup — that's what prevents
    # Yokohama → Sakhalin Oblast even if Sakhalin's label point is closer.
    states_by_country = {}
    if states:
        for s_lat, s_lon, s_name in states:
            sc = _country_by_bbox(s_lat, s_lon)
            if not sc and countries:
                sc = _nearest_linear(s_lat, s_lon, countries)
            states_by_country.setdefault(sc, []).append((s_lat, s_lon, s_name))
    # Same treatment for cities — a point on the Russia/Ukraine border
    # should pick up in-country cities even if another city is closer across
    # the line. Grid lookup inside this dict keeps city lookups fast.
    cities_by_country_grid = {}
    if cities:
        raw = {}
        for c_lat, c_lon, c_name in cities:
            cc = _country_by_bbox(c_lat, c_lon)
            if not cc and countries:
                cc = _nearest_linear(c_lat, c_lon, countries)
            raw.setdefault(cc, []).append((c_lat, c_lon, c_name))
        cities_by_country_grid = {k: _build_grid(v) for k, v in raw.items()}

    # City-state / federal-district bindings: places where the MVT `place`
    # layer doesn't carry a matching state-class entry, so nearest-state
    # would otherwise fall back to a neighboring US state, Russian oblast,
    # etc. Keyed on the nearest-city name PLUS a bbox, so Silver Spring or
    # Arlington (whose nearest city is themselves, not Washington) don't get
    # mislabeled as D.C.
    #
    # `label` None means "this city IS its own admin region — suppress the
    # state part entirely" (output "Tokyo" instead of "Tokyo, Tokyo").
    _CITY_STATE_BINDINGS = {
        # (min_lat, min_lon, max_lat, max_lon, state_label)
        # DC is a federal district not in our state-class tiles.
        "Washington": (38.79, -77.13, 39.00, -76.90, "D.C."),
        # Each of these is a municipality / metro prefecture that is its own
        # admin region; OMT doesn't carry a matching state entry.
        "Tokyo":       (35.45, 138.95, 35.95, 139.95, None),
        "Beijing":     (39.40, 115.40, 41.10, 117.50, None),
        "Shanghai":    (30.60, 120.85, 31.90, 122.20, None),
        "Hong Kong":   (22.15, 113.80, 22.58, 114.45, None),
        "Delhi":       (28.40, 76.80, 28.90, 77.35, None),
    }

    def lookup(lat, lon):
        # Pick country first so we can filter city/state candidates to only
        # those whose label points are in the same country — that's what
        # prevents Yokohama → Sakhalin Oblast / Primorsky Krai and similar
        # cross-border bugs. Bbox table wins over nearest-country-label.
        country = _country_by_bbox(lat, lon)
        if not country and countries:
            country = _nearest_linear(lat, lon, countries)
        city_grid_local = cities_by_country_grid.get(country, city_grid)
        city = _nearest_grid(lat, lon, city_grid_local) if city_grid_local else None
        state = None
        state_suppressed = False
        if city in _CITY_STATE_BINDINGS:
            mn_lat, mn_lon, mx_lat, mx_lon, label = _CITY_STATE_BINDINGS[city]
            if mn_lat <= lat <= mx_lat and mn_lon <= lon <= mx_lon:
                if label is None:
                    state_suppressed = True
                else:
                    state = label
        if state is None and not state_suppressed:
            in_country_states = states_by_country.get(country, [])
            state = _nearest_linear(lat, lon, in_country_states) if in_country_states else None
        # Format: "City, State" when both are known (best disambiguation).
        # If state is missing or suppressed but we have city + country, use
        # "City, Country" — still more informative than country alone. Avoids
        # "Yokohama" collapsing to just "Japan" because no Japanese prefecture
        # is tagged class=state in OMT.
        if city and state:
            return f"{city}, {state}"
        elif city and state_suppressed:
            return city
        elif city and country:
            return f"{city}, {country}"
        elif city:
            return city
        elif state:
            return state
        elif country:
            return country
        return ""

    return lookup


def _process_tile_partition(args):
    """Worker: read a tile_column range from SQLite, extract and dedup search features.

    Writes deduplicated features to a temp file (JSON lines) to avoid sending
    huge lists through multiprocessing IPC pipes."""
    mbtiles_path, col_start, col_end, search_layers, output_file = args
    import mapbox_vector_tile
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(mbtiles_path))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT zoom_level, tile_column, tile_row, tile_data "
        "FROM tiles WHERE zoom_level = 14 AND tile_column >= ? AND tile_column < ?",
        (col_start, col_end),
    )

    seen = set()
    count = 0
    feat_count = 0
    out_f = open(output_file, "w")
    for z, x, tms_y, data in cursor:
        y = (1 << z) - 1 - tms_y
        tile_data = data
        if data[:2] == b"\x1f\x8b":
            try:
                tile_data = gzip.decompress(data)
            except Exception:
                count += 1
                continue

        try:
            decoded = mapbox_vector_tile.decode(tile_data, y_coord_down=True)
        except Exception:
            count += 1
            continue

        for layer_name, feature_type in search_layers.items():
            layer = decoded.get(layer_name)
            if not layer:
                continue
            extent = layer.get("extent", 4096)
            for feature in layer.get("features", []):
                props = feature.get("properties", {})
                name = props.get("name:latin") or props.get("name", "")
                if not name or len(name) < 2:
                    continue
                geom = feature.get("geometry", {})
                coords = geom.get("coordinates")
                if not coords:
                    continue
                geom_type = geom.get("type", "")
                try:
                    if geom_type == "Point":
                        px, py = coords[0], coords[1]
                    elif geom_type == "MultiPoint":
                        px = sum(c[0] for c in coords) / len(coords)
                        py = sum(c[1] for c in coords) / len(coords)
                    elif geom_type == "LineString":
                        mid = coords[len(coords) // 2]
                        px, py = mid[0], mid[1]
                    elif geom_type == "MultiLineString":
                        longest = max(coords, key=len)
                        mid = longest[len(longest) // 2]
                        px, py = mid[0], mid[1]
                    elif geom_type in ("Polygon", "MultiPolygon"):
                        ring = coords[0] if geom_type == "Polygon" else coords[0][0]
                        px = sum(c[0] for c in ring) / len(ring)
                        py = sum(c[1] for c in ring) / len(ring)
                    else:
                        continue
                except (IndexError, ZeroDivisionError, TypeError):
                    continue
                lon, lat = tile_to_lnglat(z, x, y, px, py, extent)
                subtype = props.get("class", "") or props.get("subclass", "")
                dedup_key = (name.lower(), feature_type, round(lat, 4), round(lon, 4))
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                json.dump({"name": name, "type": feature_type, "subtype": subtype,
                           "lat": lat, "lon": lon}, out_f, separators=(",", ":"))
                out_f.write("\n")
                feat_count += 1
        count += 1

    out_f.close()
    conn.close()
    return output_file, count, feat_count


def _process_tile_for_search(args):
    """Worker function for parallel search feature extraction."""
    import mapbox_vector_tile
    z, x, y, data, search_layers = args

    tile_data = data
    if data[:2] == b"\x1f\x8b":
        try:
            tile_data = gzip.decompress(data)
        except Exception:
            return []

    try:
        decoded = mapbox_vector_tile.decode(tile_data, y_coord_down=True)
    except Exception:
        return []

    results = []
    for layer_name, feature_type in search_layers.items():
        layer = decoded.get(layer_name)
        if not layer:
            continue

        extent = layer.get("extent", 4096)

        for feature in layer.get("features", []):
            props = feature.get("properties", {})
            name = props.get("name:latin") or props.get("name", "")
            if not name or len(name) < 2:
                continue

            geom = feature.get("geometry", {})
            coords = geom.get("coordinates")
            if not coords:
                continue

            geom_type = geom.get("type", "")
            try:
                if geom_type == "Point":
                    px, py = coords[0], coords[1]
                elif geom_type == "MultiPoint":
                    px = sum(c[0] for c in coords) / len(coords)
                    py = sum(c[1] for c in coords) / len(coords)
                elif geom_type == "LineString":
                    mid = coords[len(coords) // 2]
                    px, py = mid[0], mid[1]
                elif geom_type == "MultiLineString":
                    longest = max(coords, key=len)
                    mid = longest[len(longest) // 2]
                    px, py = mid[0], mid[1]
                elif geom_type in ("Polygon", "MultiPolygon"):
                    ring = coords[0] if geom_type == "Polygon" else coords[0][0]
                    px = sum(c[0] for c in ring) / len(ring)
                    py = sum(c[1] for c in ring) / len(ring)
                else:
                    continue
            except (IndexError, ZeroDivisionError, TypeError):
                continue

            lon, lat = tile_to_lnglat(z, x, y, px, py, extent)
            subtype = props.get("class", "") or props.get("subclass", "")

            results.append({
                "name": name,
                "type": feature_type,
                "subtype": subtype,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
            })

    return results


# Module-level helpers for multiprocessing location assignment
_place_grid = None


def _init_location_worker(grid_dict):
    global _place_grid
    _place_grid = grid_dict


def _assign_location_batch(batch):
    """Assign nearest place to a batch of features (module-level for pickling)."""
    results = []
    for f in batch:
        if f["type"] == "place":
            results.append(None)
            continue
        gx = int(f["lon"] * 2)
        gy = int(f["lat"] * 2)
        best_name = None
        best_dist = float("inf")
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                for p in _place_grid.get((gx + dx, gy + dy), []):
                    d = (p["lat"] - f["lat"]) ** 2 + (p["lon"] - f["lon"]) ** 2
                    if d < best_dist:
                        best_dist = d
                        best_name = p["name"]
        results.append(best_name)
    return results


def extract_addresses_pbf(pbf_path, output_path, bbox=None):
    """Extract addr:housenumber + addr:street features from OSM PBF.

    Appends address entries to the given JSONL output path in the same
    schema used by the rest of the search index (name/type/lat/lon).
    These feed the routing UI's typeahead so users can search by address.

    Returns count of address entries written.
    """
    print("  Extracting address features from OSM data...")
    if not shutil.which("osmium"):
        print("    Skipping: osmium CLI not found on PATH")
        return 0
    source_pbf = str(pbf_path)
    tmp = tempfile.mkdtemp(prefix="streetzim_addr_")
    try:
        if bbox:
            minlon, minlat, maxlon, maxlat = bbox
            bbox_pbf = os.path.join(tmp, "region.osm.pbf")
            subprocess.run([
                "osmium", "extract",
                "-b", f"{minlon},{minlat},{maxlon},{maxlat}",
                source_pbf, "-o", bbox_pbf, "--overwrite",
            ], check=True)
            source_pbf = bbox_pbf

        # osmium tags-filter keeps any element with addr:housenumber.
        # Covers address nodes and building ways/relations tagged directly.
        addr_pbf = os.path.join(tmp, "addresses.osm.pbf")
        subprocess.run([
            "osmium", "tags-filter", source_pbf,
            "addr:housenumber",
            "-o", addr_pbf, "--overwrite",
        ], check=True)

        addr_geojson = os.path.join(tmp, "addresses.geojsonseq")
        subprocess.run([
            "osmium", "export", addr_pbf,
            "-f", "geojsonseq",
            "-o", addr_geojson, "--overwrite",
        ], check=True)

        count = 0
        with open(addr_geojson, "r", encoding="utf-8") as fin, \
             open(output_path, "a", encoding="utf-8") as fout:
            for line in fin:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                try:
                    feat = json.loads(line)
                except Exception:
                    continue
                props = feat.get("properties") or {}
                num = (props.get("addr:housenumber") or "").strip()
                street = (props.get("addr:street") or "").strip()
                city = (props.get("addr:city") or "").strip()
                if not num or not street:
                    continue  # skip orphan addresses that can't be typed

                geom = feat.get("geometry") or {}
                gtype = geom.get("type")
                coords = geom.get("coordinates")
                if gtype == "Point" and coords:
                    lon, lat = coords[0], coords[1]
                elif gtype == "Polygon" and coords:
                    ring = coords[0]
                    if not ring:
                        continue
                    lon = sum(c[0] for c in ring) / len(ring)
                    lat = sum(c[1] for c in ring) / len(ring)
                else:
                    continue

                display = f"{num} {street}"
                if city:
                    display = f"{display}, {city}"
                entry = {
                    "name": display,
                    "type": "addr",
                    "subtype": "",
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                }
                fout.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))
                fout.write("\n")
                count += 1
                if count % 100000 == 0:
                    print(f"\r    Wrote {count} addresses...", end="", flush=True)
        print(f"\r    Wrote {count} address entries")
        return count
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# US street-suffix normalization map. The pass-2 matcher needs "1029 Ramona
# St" and "1029 Ramona Street" to hash to the same key. This dict is
# intentionally small and US-focused — full libpostal coverage would be
# overkill for v1 and pulls in a 2 GB libpostal dataset. For non-US
# regions the worst outcome is an extra Overture row slipping in
# alongside an equivalent OSM row, which degrades gracefully (dup at
# same coordinate) and can be tightened later per docs/overture-matching.md.
_STREET_ABBREV = {
    "st": "street", "str": "street",
    "ave": "avenue", "av": "avenue",
    "blvd": "boulevard", "bl": "boulevard",
    "rd": "road",
    "dr": "drive",
    "ln": "lane",
    "ct": "court",
    "pl": "place",
    "hwy": "highway",
    "pkwy": "parkway",
    "cir": "circle",
    "ter": "terrace",
    "ctr": "center",
    "sq": "square",
    "mt": "mount", "ft": "fort",
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
}

def _normalize_street(name):
    """Lowercase, strip punctuation, expand common US suffix abbreviations.

    Idempotent — running it twice is the same as running it once.
    """
    if not name:
        return ""
    import re as _re
    # Replace punctuation with spaces; strip accents via NFKD+combining.
    import unicodedata as _ud
    folded = "".join(
        c for c in _ud.normalize("NFKD", name.lower()) if not _ud.combining(c)
    )
    tokens = _re.findall(r"[a-z0-9]+", folded)
    return " ".join(_STREET_ABBREV.get(t, t) for t in tokens)


def merge_overture_addresses(overture_parquet, search_jsonl_path, bbox=None):
    """Append Overture-sourced address records to the search-feed JSONL.

    Two-pass conflation per docs/overture-matching.md:
      Pass 1 (deterministic) — skip any Overture row whose `sources[]`
        points to an OSM element ID we already extracted. The OSM
        record carries the same information and already participates in
        the routing graph, so keeping OSM as the authority is correct.
      Pass 2 (fuzzy) — for rows with no OSM provenance, match on rounded
        coord (~1m grid) with tie-break by matching (number, normalized
        street). The 1m coord grid catches OpenAddresses points that
        got mapped onto OSM-derived positions; normalized-street match
        collapses "RAMONA ST" / "Ramona Street" / "Ramona St." variants.

    Writes new Overture records into the same JSONL in the existing
    schema with `subtype="overture"` so downstream code and mcpzim can
    spot the provenance. Returns the count of rows added.
    """
    import duckdb  # local import — only needed when the flag is set
    print(f"  Merging Overture addresses from {overture_parquet}...")

    # ------------------------------------------------------------------
    # Build the OSM-side index from the existing JSONL. We scan only
    # `type == "addr"` entries because non-address records (cities,
    # POIs, ways) have fundamentally different identity.
    # ------------------------------------------------------------------
    osm_coord_index = set()   # {(lat_e5, lon_e5)}
    # attr_key was (number, normalized_street) but that collides across
    # cities — "1029 Ramona Street" in Ramona, CA (OSM) and "1029
    # RAMONA ST" in Palo Alto (Overture) hash to the same key, so the
    # Palo Alto Overture row gets dropped as a "spatial dup" 600 km
    # from any OSM neighbour. Include city to make the key
    # city-scoped; addresses with the same number+street in different
    # cities now both land. Cities are normalised the same way streets
    # are (lowercased, accent-stripped, single-spaced) so "PALO ALTO"
    # / "Palo Alto" / "palo  alto" all collapse to one value.
    osm_attr_index = set()    # {(number, normalized_street, normalized_city)}
    osm_count = 0
    with open(search_jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            if '"type":"addr"' not in line:
                # Fast path: ~98% of lines in the world feed aren't
                # addresses. Skipping the json.loads here saves minutes.
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "addr":
                continue
            lat = rec.get("lat"); lon = rec.get("lon")
            if lat is None or lon is None:
                continue
            # ~1 m grid — rounds to 5 decimal places in degrees.
            osm_coord_index.add((round(lat, 5), round(lon, 5)))
            name = rec.get("name") or ""
            # Existing OSM records serialize as "<num> <street>, <city>".
            # Split on the first space + comma to recover number/street/city.
            num = ""
            street = name
            city = ""
            if " " in name:
                num, _, rest = name.partition(" ")
                if "," in rest:
                    street, _, city = rest.partition(",")
                else:
                    street = rest
            else:
                if "," in name:
                    street, _, city = name.partition(",")
            street = street.strip()
            city = city.strip()
            if num and street:
                osm_attr_index.add((
                    num.strip(),
                    _normalize_street(street),
                    _normalize_street(city),
                ))
            osm_count += 1
    print(f"    Indexed {osm_count} existing OSM address records")

    # ------------------------------------------------------------------
    # Stream Overture rows via DuckDB Arrow batches. Materializing the
    # whole parquet OOMs a Mac for continent-scale bboxes; batch of
    # 2048 keeps working-set bounded.
    # ------------------------------------------------------------------
    con = duckdb.connect()
    # The parquet is already bbox-filtered by download_overture_data.py;
    # a second WHERE here would need a `bbox` struct the downloader doesn't
    # project. Instead, filter row-side in Python if the caller passes a
    # bbox — handles the edge case of reusing a larger-region parquet.
    sql = f"""
      SELECT number, street, postcode,
             address_levels, sources,
             ST_X(ST_GeomFromText(wkt)) AS lon,
             ST_Y(ST_GeomFromText(wkt)) AS lat
      FROM read_parquet('{overture_parquet}')
    """
    con.execute("INSTALL spatial; LOAD spatial;")
    reader = con.execute(sql).fetch_record_batch(2048)

    if bbox is not None:
        bbox_minlon, bbox_minlat, bbox_maxlon, bbox_maxlat = bbox
    else:
        bbox_minlon = bbox_minlat = -1e9
        bbox_maxlon = bbox_maxlat = 1e9

    pass1_skipped = 0      # dropped via OSM-source link
    pass2_skipped = 0      # dropped via coord/attr match
    added = 0              # net new records appended
    orphan_skipped = 0     # missing number or street
    # Distinct upstream datasets observed across rows that survived to
    # output. Drives overture-sources.json + the viewer's attribution
    # panel — OpenAddresses / LINZ NZ / Asiaq / NYC Open Data / etc.
    source_datasets = set()
    with open(search_jsonl_path, "a", encoding="utf-8") as fout:
        for batch in reader:
            for row in batch.to_pylist():
                num = (row.get("number") or "").strip()
                street_raw = (row.get("street") or "").strip()
                if not num or not street_raw:
                    orphan_skipped += 1
                    continue
                lat = row.get("lat"); lon = row.get("lon")
                if lat is None or lon is None:
                    orphan_skipped += 1
                    continue
                if not (bbox_minlat <= lat <= bbox_maxlat and
                        bbox_minlon <= lon <= bbox_maxlon):
                    continue

                # Pass 1: Overture-to-OSM provenance link. Today we
                # don't keep a set of imported OSM address node IDs —
                # extract_addresses_pbf doesn't expose them — so this
                # branch is a no-op for the address theme in v1. Left
                # in place so when we wire in the OSM-ID capture it
                # picks up automatically (docs/overture-matching.md §1).
                has_osm_source = False
                for src in (row.get("sources") or []):
                    if (src or {}).get("dataset") == "OpenStreetMap":
                        has_osm_source = True
                        break
                if has_osm_source:
                    pass1_skipped += 1
                    continue

                # Reconstruct a city label from address_levels when we
                # have it. For US addresses Overture writes
                # [{'value':'CA'},{'value':'PALO ALTO'}] — state first,
                # then city. For non-US layouts the last non-empty
                # value is usually the city, which is our best-effort
                # fallback. Computed before the dedup test because
                # attr_key now includes a normalised city.
                city = ""
                levels = row.get("address_levels") or []
                if len(levels) >= 2 and levels[-1]:
                    city = (levels[-1].get("value") or "").title()

                # Pass 2: fuzzy match against our OSM index. attr_key
                # now scopes by normalised city so two addresses with
                # the same number+street in different cities both
                # land — fixes the cross-city collision that dropped
                # "1029 Ramona St, Palo Alto" because OSM had "1029
                # Ramona Street" in Ramona (city), 600 km away.
                coord_key = (round(lat, 5), round(lon, 5))
                attr_key = (
                    num,
                    _normalize_street(street_raw),
                    _normalize_street(city),
                )
                if coord_key in osm_coord_index or attr_key in osm_attr_index:
                    pass2_skipped += 1
                    continue
                # Street is frequently uppercased in US OpenAddresses
                # feeds — title-case it so it renders nicely in search.
                street_display = street_raw.title() if street_raw.isupper() else street_raw
                display = f"{num} {street_display}"
                if city:
                    display = f"{display}, {city}"
                entry = {
                    "name": display,
                    "type": "addr",
                    "subtype": "overture",   # provenance marker
                    "lat": round(float(lat), 6),
                    "lon": round(float(lon), 6),
                }
                fout.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))
                fout.write("\n")
                added += 1
                for src in (row.get("sources") or []):
                    ds = (src or {}).get("dataset")
                    if ds:
                        source_datasets.add(ds)

    total_overture = pass1_skipped + pass2_skipped + added + orphan_skipped
    print(f"    Overture: {total_overture} rows scanned, "
          f"{pass1_skipped} skipped (OSM source link), "
          f"{pass2_skipped} skipped (spatial dup), "
          f"{orphan_skipped} orphan (missing num/street), "
          f"{added} added, "
          f"{len(source_datasets)} distinct upstream datasets")
    return {"added": added, "datasets": sorted(source_datasets)}


def merge_overture_places(overture_parquet, search_jsonl_path, bbox=None):
    """Enrich OSM POIs with Overture places' websites / phones / socials /
    categories / brand — and emit new POI records for places OSM doesn't
    know about.

    Two passes, mirroring merge_overture_addresses:

      Pass 1 (enrich): for each Overture row, look up an OSM POI in the
        search feed by rounded coord + normalized name. If found, add
        the Overture fields to that record in place. This is the main
        win — OSM's `subtype` is noisy (museums bucketed under `tourism`,
        hotels under `amenity`); Overture's `categories.primary` gives
        a clean label we can drive chips + popups off.
      Pass 2 (add-new): Overture rows with no OSM match become fresh
        `type: "poi"` records tagged `subtype` = Overture primary
        category and `source: "overture"`.

    Per-record extensions (kept terse to bound chunk sizes):
      cat        — Overture primary category ("museum", "hotel", …)
      w          — first website URL
      p          — first phone
      soc        — first 3 social URLs (array)
      brand      — brand primary name (string, often empty)
      wd         — brand wikidata Q-ID when present
      source     — "overture" if the record was freshly added by this pass

    Returns {"enriched": N, "added": M, "datasets": [...], "size_bytes": {...}}
    so the caller can log the size impact without re-stat'ing the jsonl.
    """
    import duckdb
    print(f"  Merging Overture places from {overture_parquet}...")

    size_before = os.path.getsize(search_jsonl_path)

    # Streaming refactor (was: load ALL records into memory). For
    # europe-scale the JSONL post-addresses-merge has ~120M lines;
    # the in-memory records[] peaked at >24 GB and OOM-killed the
    # process during continent rebuilds. New flow:
    #
    #   Pass A — scan JSONL once to collect POI keys (only POI type
    #     records contribute keys; bounded by POI count, ~10M for
    #     europe → ~1 GB, vs 24 GB for ALL records).
    #   Pass B — stream Overture parquet; for each row decide
    #     enrich-existing or new-POI, store decisions in two small
    #     in-memory tables keyed by (round(lat,4), round(lon,4),
    #     normalized_name).
    #   Pass C — re-stream JSONL → tmp file applying enrichments;
    #     append new-POI additions at end.
    #
    # No intermediate full-feature materialization. Memory bounded by
    # POI count + Overture row count, not total feature count.
    poi_keys = set()  # POI keys seen in source JSONL
    with open(search_jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            # Fast pre-filter: skip lines that aren't POIs without
            # parsing JSON. Saves minutes on continent-scale where
            # most of the feed is addresses + streets, not POIs.
            if '"type":"poi"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "poi":
                continue
            lat = rec.get("lat"); lon = rec.get("lon")
            nm = rec.get("name") or ""
            if lat is None or lon is None or not nm:
                continue
            poi_keys.add((round(lat, 4), round(lon, 4),
                          _normalize_street(nm)))
    print(f"    Indexed {len(poi_keys)} OSM POI keys (streaming, "
          f"bounded memory)")

    # Stream Overture places from parquet via arrow batches.
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    sql = f"""
      SELECT names, categories, phones, websites, socials, brand, sources,
             ST_X(ST_GeomFromText(wkt)) AS lon,
             ST_Y(ST_GeomFromText(wkt)) AS lat
      FROM read_parquet('{overture_parquet}')
    """
    reader = con.execute(sql).fetch_record_batch(2048)

    if bbox is not None:
        bbox_minlon, bbox_minlat, bbox_maxlon, bbox_maxlat = bbox
    else:
        bbox_minlon = bbox_minlat = -1e9
        bbox_maxlon = bbox_maxlat = 1e9

    enriched = 0
    added = 0
    unnamed = 0
    source_datasets = set()
    # Pass B accumulators (bounded by POI count + Overture row count,
    # not total feature count). enrichments stays under ~1-2 GB even
    # for europe; additions is the hot growth path.
    enrichments = {}   # key → extra-dict (applied to first matching POI)
    additions_path = search_jsonl_path + ".overture_additions"
    additions_count = 0
    with open(additions_path, "w", encoding="utf-8") as add_fh:
        for batch in reader:
            for row in batch.to_pylist():
                lat = row.get("lat"); lon = row.get("lon")
                if lat is None or lon is None:
                    continue
                if not (bbox_minlat <= lat <= bbox_maxlat and
                        bbox_minlon <= lon <= bbox_maxlon):
                    continue

                names = row.get("names") or {}
                name = (names.get("primary") or "").strip()
                if not name:
                    unnamed += 1
                    continue

                cats = row.get("categories") or {}
                primary = (cats.get("primary") or "").strip()

                phones = row.get("phones") or []
                websites = row.get("websites") or []
                socials = row.get("socials") or []
                brand = row.get("brand") or {}
                brand_names = (brand or {}).get("names") or {}
                brand_primary = (brand_names.get("primary") or "").strip() if brand_names else ""
                brand_wd = (brand or {}).get("wikidata") or None

                # Surface-area extensions. Keep empty fields out of the
                # record so downstream JSON stays tight.
                #
                # Website is `ws`, not `w`. `w` is reserved for the Wikipedia
                # tag (e.g. "en:HP_Garage") that OSM POIs carry in the same
                # record — mcpzim reads `rec["w"]` into `Place.wiki` and then
                # calls `articleByTitle` on it. Putting a URL in that slot
                # corrupts Wikipedia lookups across every downstream tool
                # (`nearby_stories`, `near_places(has_wiki=true)`, etc.). See
                # commit "Rename Overture website field to ws (fix w collision)".
                extra = {}
                if primary: extra["cat"] = primary
                if websites: extra["ws"] = websites[0]
                if phones: extra["p"] = phones[0]
                if socials: extra["soc"] = socials[:3]
                if brand_primary: extra["brand"] = brand_primary
                if brand_wd: extra["wd"] = brand_wd

                key = (round(lat, 4), round(lon, 4), _normalize_street(name))
                if key in poi_keys:
                    # Pass 1 enrich: queue the extra-dict by key. First
                    # Overture row wins per key (matches old "first match
                    # wins" semantics); duplicates are rare at this
                    # precision.
                    if key not in enrichments:
                        enrichments[key] = extra
                        enriched += 1
                else:
                    # Pass 2 add-new: skip uncategorized noise; stream
                    # additions to a sidecar file so we don't hold
                    # millions of dicts in memory.
                    if not primary:
                        continue
                    rec = {
                        "name": name,
                        "type": "poi",
                        "subtype": primary,
                        "lat": round(float(lat), 6),
                        "lon": round(float(lon), 6),
                        "source": "overture",
                        **extra,
                    }
                    add_fh.write(json.dumps(rec, separators=(",", ":"),
                                            ensure_ascii=False))
                    add_fh.write("\n")
                    additions_count += 1
                    added += 1

                for src in (row.get("sources") or []):
                    ds = (src or {}).get("dataset")
                    if ds:
                        source_datasets.add(ds)

    print(f"    Pass B done: {enriched} enrichments queued, "
          f"{added} additions staged at {additions_path}")

    # Pass C: stream-rewrite the JSONL applying enrichments inline,
    # then append the additions sidecar.
    tmp_path = search_jsonl_path + ".overture_tmp"
    applied = set()  # keys already enriched ("first match wins")
    with open(search_jsonl_path, "r", encoding="utf-8") as fin, \
         open(tmp_path, "w", encoding="utf-8") as out:
        for line in fin:
            if '"type":"poi"' not in line:
                # Fast path: not a POI, copy verbatim.
                out.write(line)
                continue
            try:
                rec = json.loads(line.rstrip("\n"))
            except Exception:
                out.write(line)
                continue
            if rec.get("type") != "poi":
                out.write(line)
                continue
            lat = rec.get("lat"); lon = rec.get("lon")
            nm = rec.get("name") or ""
            if lat is None or lon is None or not nm:
                out.write(line)
                continue
            key = (round(lat, 4), round(lon, 4), _normalize_street(nm))
            extra = enrichments.get(key)
            if not extra or key in applied:
                out.write(line)
                continue
            applied.add(key)
            for k, v in extra.items():
                if k not in rec:
                    rec[k] = v
            s_old = rec.get("subtype") or ""
            if extra.get("cat") and s_old in (
                    "", "tourism", "amenity", "shop",
                    "attraction", "leisure", "car",
                    "historic", "landuse"):
                rec["subtype"] = extra["cat"]
            out.write(json.dumps(rec, separators=(",", ":"),
                                 ensure_ascii=False))
            out.write("\n")
        # Append the additions sidecar (already JSONL formatted).
        if additions_count:
            with open(additions_path, "r", encoding="utf-8") as add_fh:
                shutil.copyfileobj(add_fh, out, length=8 * 1024 * 1024)
    os.replace(tmp_path, search_jsonl_path)
    try:
        os.unlink(additions_path)
    except OSError:
        pass

    size_after = os.path.getsize(search_jsonl_path)
    delta_mb = (size_after - size_before) / (1024 * 1024)
    print(f"    Overture places: {enriched} enriched, {added} added, "
          f"{unnamed} unnamed skipped, "
          f"{len(source_datasets)} upstream datasets; "
          f"jsonl {size_before/1024/1024:.1f} MB → "
          f"{size_after/1024/1024:.1f} MB (+{delta_mb:.1f} MB)")
    return {
        "enriched": enriched,
        "added": added,
        "datasets": sorted(source_datasets),
        "size_bytes": {"before": size_before, "after": size_after},
    }


def extract_wiki_tags_pbf(pbf_path, bbox=None):
    """Extract {wikipedia, wikidata} tags per OSM object with a name.

    Used to enrich the search index so offline agents can cross-link
    POI records to the Wikipedia ZIM (per the mcpzim contract doc).
    Returns a dict keyed by (normalized_name, quantized_lat, quantized_lon):
        { ("lincoln memorial", 3889018, -770358): {
              "wikipedia": "en:Lincoln_Memorial",
              "wikidata":  "Q162458",
          },
          ... }
    Coord quantization is round(lat*1e4) / round(lon*1e4) ≈ 11 m grid,
    which tolerates MVT-vs-PBF rounding without colliding unrelated POIs.
    """
    print("  Extracting wiki cross-ref tags from OSM data...")
    source_pbf = str(pbf_path)
    tmp = tempfile.mkdtemp(prefix="streetzim_wiki_")
    try:
        if bbox:
            minlon, minlat, maxlon, maxlat = bbox
            bbox_pbf = os.path.join(tmp, "region.osm.pbf")
            subprocess.run([
                "osmium", "extract",
                "-b", f"{minlon},{minlat},{maxlon},{maxlat}",
                source_pbf, "-o", bbox_pbf, "--overwrite",
            ], check=True)
            source_pbf = bbox_pbf

        # osmium-tags-filter: anything with wikipedia OR wikidata tag.
        wiki_pbf = os.path.join(tmp, "wiki.osm.pbf")
        subprocess.run([
            "osmium", "tags-filter", source_pbf,
            "wikipedia", "wikidata",
            "-o", wiki_pbf, "--overwrite",
        ], check=True)

        wiki_geojson = os.path.join(tmp, "wiki.geojsonseq")
        subprocess.run([
            "osmium", "export", wiki_pbf,
            "-f", "geojsonseq",
            "-o", wiki_geojson, "--overwrite",
        ], check=True)

        lookup = {}
        count = 0
        with open(wiki_geojson, "r", encoding="utf-8") as fin:
            for line in fin:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                try:
                    feat = json.loads(line)
                except Exception:
                    continue
                props = feat.get("properties") or {}
                name = (props.get("name") or props.get("name:latin") or "").strip()
                if not name:
                    continue
                wikipedia = (props.get("wikipedia") or "").strip()
                wikidata = (props.get("wikidata") or "").strip()
                if not wikipedia and not wikidata:
                    continue

                geom = feat.get("geometry") or {}
                gtype = geom.get("type")
                coords = geom.get("coordinates")
                if gtype == "Point" and coords:
                    lon, lat = coords[0], coords[1]
                elif gtype == "Polygon" and coords and coords[0]:
                    ring = coords[0]
                    lon = sum(c[0] for c in ring) / len(ring)
                    lat = sum(c[1] for c in ring) / len(ring)
                elif gtype == "LineString" and coords:
                    mid = coords[len(coords) // 2]
                    lon, lat = mid[0], mid[1]
                else:
                    continue

                key = (name.lower(), int(round(lat * 1e4)), int(round(lon * 1e4)))
                entry = {}
                if wikipedia:
                    entry["wikipedia"] = wikipedia
                if wikidata:
                    entry["wikidata"] = wikidata
                # If we already have an entry for this coord+name, prefer the one
                # with more fields (covers the case where a node and a way share
                # the same name but only one has both tags).
                existing = lookup.get(key)
                if existing is None or len(entry) > len(existing):
                    lookup[key] = entry
                    count += 1
                if count % 50000 == 0 and count:
                    print(f"\r    Indexed {count} wiki cross-refs...", end="", flush=True)
        print(f"\r    Indexed {len(lookup)} wiki cross-refs (from {count} raw)")
        return lookup
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def extract_routing_graph(pbf_path, output_dir, bbox=None, split_graph=False):
    """Extract road network from OSM PBF and build a compact routing graph.

    Streams through the (bbox-filtered) PBF with pyosmium in two passes:
      Pass 1 — collect highway-way node refs + endpoints to identify junctions
              (intersection/terminus nodes, the graph vertices).
      Pass 2 — re-scan ways, split each at junction nodes into edges, emit
              edges incrementally into arrays + a geom varint blob.

    The old implementation materialized all highway features in Python
    objects (~5 KB/feature), peaking at ~67 GB RAM for Japan and would
    need ~500 GB for Europe. Streaming + node-ref dedup + numpy/array.array
    storage keeps peak RAM well under 100 GB for any continent-scale bbox.

    Args:
        pbf_path: Source OSM PBF file
        output_dir: Where to write the bbox-filtered PBF (intermediate)
                    and the final routing-graph.bin.
        bbox: Optional (minlon, minlat, maxlon, maxlat) to bbox-filter first.
              Critical for regional builds from a planet PBF.
        split_graph: If True, emit SZRG v5 split layout — main ``routing-graph.bin``
                    holds everything routing needs (nodes + edges + name table);
                    companion ``routing-graph-geoms.bin`` (SZGM v1) holds the
                    polyline blob for lazy loading on route-draw. Frees iOS
                    Safari from allocating a multi-GB single buffer up-front.
                    Default False keeps the current v4 inline layout so Kiwix
                    Desktop + mcpzim stay on their supported contract.

    Returns (main_path, geoms_path_or_None). geoms_path is set only when
    ``split_graph=True``. Returns (None, None) if no highways found.
    """
    import math
    import array
    import numpy as np
    import struct

    try:
        import osmium
    except ImportError:
        raise RuntimeError("pyosmium is required for routing extraction "
                           "(pip install osmium)")

    print("  Extracting routing graph from OSM data...")

    # Step 0: Bbox-filter the PBF first so we never read ways outside the region.
    source_pbf = str(pbf_path)
    if bbox:
        minlon, minlat, maxlon, maxlat = bbox
        bbox_pbf = os.path.join(output_dir, "region.osm.pbf")
        print(f"    Extracting bbox {minlon},{minlat},{maxlon},{maxlat} from planet PBF...")
        subprocess.run([
            "osmium", "extract",
            "-b", f"{minlon},{minlat},{maxlon},{maxlat}",
            source_pbf, "-o", bbox_pbf, "--overwrite",
        ], check=True)
        size_mb = os.path.getsize(bbox_pbf) / (1024 * 1024)
        print(f"    Region PBF: {size_mb:.1f} MB")
        source_pbf = bbox_pbf

    # Highway classes excluded from routing (non-navigable)
    EXCLUDED = frozenset({
        "proposed", "construction", "raceway", "bus_guideway",
        "platform", "elevator", "razed", "abandoned",
    })
    # Speed estimates (km/h) by highway class for travel time
    SPEED = {
        "motorway": 100, "motorway_link": 60,
        "trunk": 80, "trunk_link": 50,
        "primary": 60, "primary_link": 40,
        "secondary": 50, "secondary_link": 35,
        "tertiary": 40, "tertiary_link": 30,
        "residential": 30, "living_street": 20,
        "unclassified": 40, "service": 20,
        "track": 15, "path": 5, "footway": 5,
        "cycleway": 15, "pedestrian": 5, "steps": 3,
    }
    DEFAULT_SPEED = 30

    # Road-class ordinal for the v4 routing-graph class_access u32
    # (bits 0..4). See docs/driving-mode-road-class-warnings.md for the
    # full bit layout. Unknown / missing classes fall through to 0.
    CLASS_ORDINAL = {
        "motorway": 1, "motorway_link": 2,
        "trunk": 3, "trunk_link": 4,
        "primary": 5, "primary_link": 6,
        "secondary": 7, "secondary_link": 8,
        "tertiary": 9, "tertiary_link": 10,
        "residential": 11, "living_street": 12,
        "unclassified": 13, "service": 14,
        "track": 15, "path": 16, "footway": 17,
        "cycleway": 18, "pedestrian": 19, "steps": 20,
    }

    # Pass 1: Walk every highway way, record node refs. Junctions = nodes
    # appearing in 2+ ways OR at way endpoints. Store interior refs in a
    # compact int64 array and endpoint refs in a set; after the pass, sort
    # the array to find the 2+ duplicates.
    print("    Pass 1: scanning highway ways for junction nodes...")

    class _Pass1(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.endpoints = set()
            self.interior_chunks = []   # list of numpy int64 arrays
            self._interior_buf = []
            self.way_count = 0
            self.hw_count = 0

        def way(self, w):
            self.way_count += 1
            hw = w.tags.get("highway")
            if not hw or hw in EXCLUDED:
                return
            refs = [n.ref for n in w.nodes]
            if len(refs) < 2:
                return
            self.endpoints.add(refs[0])
            self.endpoints.add(refs[-1])
            if len(refs) > 2:
                self._interior_buf.extend(refs[1:-1])
            self.hw_count += 1
            if self.hw_count % 200000 == 0:
                # Flush Python list into numpy (release Python-int overhead)
                if self._interior_buf:
                    self.interior_chunks.append(
                        np.fromiter(self._interior_buf, dtype=np.int64,
                                    count=len(self._interior_buf)))
                    self._interior_buf = []
                print(f"\r    Pass 1: {self.hw_count} highway ways...",
                      end="", flush=True)

        def finalize(self):
            if self._interior_buf:
                self.interior_chunks.append(
                    np.fromiter(self._interior_buf, dtype=np.int64,
                                count=len(self._interior_buf)))
                self._interior_buf = []

    p1 = _Pass1()
    p1.apply_file(source_pbf)
    p1.finalize()
    print(f"\r    Pass 1: scanned {p1.hw_count} highway ways "
          f"(of {p1.way_count} total)                    ")

    if p1.hw_count == 0:
        print("    Warning: no highway features found, skipping routing graph")
        return None

    # Find interior refs that appear in 2+ ways.
    if p1.interior_chunks:
        interior_arr = np.concatenate(p1.interior_chunks)
        p1.interior_chunks = []  # free
    else:
        interior_arr = np.empty(0, dtype=np.int64)
    interior_arr.sort()
    # A ref is a "count>=2 junction" if it appears adjacent to an equal ref
    # in the sorted array. Mark either side of each equal-pair.
    if len(interior_arr) > 1:
        dup = interior_arr[:-1] == interior_arr[1:]
        mask = np.concatenate([dup, [False]]) | np.concatenate([[False], dup])
        interior_junctions = np.unique(interior_arr[mask])
    else:
        interior_junctions = np.empty(0, dtype=np.int64)
    del interior_arr
    endpoint_arr = np.fromiter(p1.endpoints, dtype=np.int64, count=len(p1.endpoints))
    junction_arr = np.unique(np.concatenate([interior_junctions, endpoint_arr]))
    del interior_junctions, endpoint_arr
    p1.endpoints = None
    print(f"    Found {len(junction_arr)} junction nodes (graph vertices)")

    # Map junction ref -> graph index (0-based, sorted for determinism).
    # Dict lookup is hot in Pass 2 — Python dict is ~25 M lookups/s which is
    # fine for tens of millions of ways.
    ref_to_idx = {int(r): i for i, r in enumerate(junction_arr)}
    num_nodes = len(junction_arr)
    del junction_arr

    # Pass 2: stream ways again, this time with node locations. Split each
    # highway way at junctions and emit edges + geoms directly into arrays.
    print("    Pass 2: building edges + geometries...")

    R = 6371000.0
    def _hav(lat1, lon1, lat2, lon2):
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _zigzag32(n):
        return ((n << 1) ^ (n >> 31)) & 0xFFFFFFFF

    def _varint(v, out):
        while v >= 0x80:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        out.append(v & 0x7F)

    def _encode_geom(lons_e7, lats_e7, out):
        """Append a varint-encoded geom to `out`, return (start_byte, end_byte)."""
        start = len(out)
        out.extend(struct.pack('<ii', lons_e7[0], lats_e7[0]))
        prev_lon = lons_e7[0]
        prev_lat = lats_e7[0]
        for k in range(1, len(lons_e7)):
            _varint(_zigzag32(lons_e7[k] - prev_lon), out)
            _varint(_zigzag32(lats_e7[k] - prev_lat), out)
            prev_lon = lons_e7[k]
            prev_lat = lats_e7[k]
        return start, len(out)

    # Output buffers (using array.array for 4-byte primitives — much more
    # compact than Python lists of ints).
    # v3 edge layout (16 bytes/edge):
    #   target (u32), dist_speed (u32: dist_dm in low 24 bits + speed in high 8),
    #   geom_idx (u32 full; 0xFFFFFFFF = no geom), name_idx (u32)
    # v2 had geom_idx packed into only 24 bits which truncates at 16.78M geoms —
    # Japan has 19.87M, so ~16% of edges pointed at wrong geoms (Fukuoka-area
    # geometry grafted onto Kyoto-area edges etc.). v3 moves geom_idx to its
    # own full-width u32 field so continent-scale regions are correctly represented.
    edges_from = array.array('I')
    edges_to = array.array('I')
    edges_dist_speed = array.array('I')
    edges_geom = array.array('I')
    edges_name = array.array('I')
    # v4 class_access u32 per edge — see docs/driving-mode-road-class-warnings.md
    # for the bit layout. We populate:
    #   bits 0..4 : road-class ordinal
    #   bit 5     : foot=no
    #   bit 6     : bicycle=no
    #   bit 7     : oneway=yes
    #   bit 8     : junction=roundabout / circular / mini_roundabout
    # bits 9..31 stay reserved so future access/maneuver flags can slot in.
    edges_class_access = array.array('I')

    # Geom offsets are stored as uint32 byte offsets into the blob — v2 format
    # caps geom_blob at 2^32 bytes. For continent-scale extracts (Europe) the
    # naive blob can exceed 4 GB. When we detect we're close to the limit, we
    # stop growing the blob and fall back to geom_idx=-1 for subsequent edges
    # (they'll render as straight line-segments between their endpoint nodes).
    # That's a graceful degradation — routing still works, just with fewer
    # intermediate polyline points for very large regions.
    GEOM_BLOB_CAP = 0xFFFF0000  # leave ~64 KB headroom before 2^32

    # Node coordinates indexed by graph idx (populated lazily as we see them).
    node_coords = np.zeros((num_nodes, 2), dtype=np.int32)  # lat_e7, lon_e7

    # Geom dedup: hash geom bytes → geom index. Geom blob accumulates.
    geom_blob = bytearray()
    # geom_offsets[k] = byte offset of geom k's start; geom_offsets[k+1] = end.
    geom_offsets = array.array('I', [0])
    geom_map = {}

    # Name table — deduped street-name strings.
    name_table = [""]
    name_map = {"": 0}

    class _Pass2(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.hw_count = 0
            self.edge_count = 0

        def way(self, w):
            hw = w.tags.get("highway")
            if not hw or hw in EXCLUDED:
                return
            try:
                refs = []
                lats_e7 = []
                lons_e7 = []
                for n in w.nodes:
                    if not n.location.valid():
                        return
                    refs.append(n.ref)
                    lats_e7.append(int(round(n.location.lat * 1e7)))
                    lons_e7.append(int(round(n.location.lon * 1e7)))
            except osmium.InvalidLocationError:
                return
            if len(refs) < 2:
                return

            # One-way direction
            ow = w.tags.get("oneway", "")
            if ow in ("yes", "1", "true"):
                oneway = 1
            elif ow == "-1":
                oneway = -1
            else:
                oneway = 0
            speed = SPEED.get(hw, DEFAULT_SPEED)

            # v4 class_access u32 (see docs/driving-mode-road-class-warnings.md).
            # Packed once per way — every edge derived from this way shares the
            # same class / access / roundabout state.
            class_ord = CLASS_ORDINAL.get(hw, 0) & 0x1F
            access_bits = 0
            if w.tags.get("foot") == "no":    access_bits |= 0x20  # bit 5
            if w.tags.get("bicycle") == "no": access_bits |= 0x40  # bit 6
            if oneway == 1:                   access_bits |= 0x80  # bit 7
            # Roundabouts in OSM are implicitly oneway. Mark them in bit 8 so
            # the HUD can say "take roundabout" and render a curved arrow.
            # Includes mini_roundabout because the maneuver is the same from
            # a driver's perspective.
            junction = (w.tags.get("junction") or "").strip()
            if junction in ("roundabout", "circular", "mini_roundabout"):
                access_bits |= 0x100  # bit 8
            class_access = class_ord | access_bits

            # Name label (same logic as before: prefer name, fall back to ref)
            name = (w.tags.get("name") or "").strip()
            refT = (w.tags.get("ref") or "").strip()
            if name and refT:
                label = f"{name} ({refT})"
            else:
                label = name or refT
            name_idx = name_map.get(label)
            if name_idx is None:
                name_idx = len(name_table)
                name_table.append(label)
                name_map[label] = name_idx

            # Walk through refs, splitting at graph nodes (junctions).
            seg_start = 0
            n = len(refs)
            for i in range(1, n):
                if i != n - 1 and refs[i] not in ref_to_idx:
                    continue
                # Segment refs[seg_start:i+1] is between two graph nodes.
                a = seg_start
                b = i
                if b - a < 1:
                    seg_start = i
                    continue
                from_idx = ref_to_idx[refs[a]]
                to_idx = ref_to_idx[refs[b]]
                if from_idx != to_idx:
                    # Distance (haversine over all points in segment).
                    dist_m = 0.0
                    prev_lat = lats_e7[a] / 1e7
                    prev_lon = lons_e7[a] / 1e7
                    for j in range(a + 1, b + 1):
                        lat = lats_e7[j] / 1e7
                        lon = lons_e7[j] / 1e7
                        dist_m += _hav(prev_lat, prev_lon, lat, lon)
                        prev_lat = lat
                        prev_lon = lon
                    dist_dm = int(round(dist_m * 10))

                    # Cache endpoint coordinates.
                    if node_coords[from_idx, 0] == 0 and node_coords[from_idx, 1] == 0:
                        node_coords[from_idx, 0] = lats_e7[a]
                        node_coords[from_idx, 1] = lons_e7[a]
                    if node_coords[to_idx, 0] == 0 and node_coords[to_idx, 1] == 0:
                        node_coords[to_idx, 0] = lats_e7[b]
                        node_coords[to_idx, 1] = lons_e7[b]

                    # Geom: interior points only (endpoints are node vertices).
                    # Skip encoding when near the uint32 blob-size cap —
                    # downstream typed arrays use 4-byte offsets and must fit.
                    interior_len = b - a - 1
                    near_cap = len(geom_blob) >= GEOM_BLOB_CAP
                    if interior_len > 0 and not near_cap:
                        i_lons = lons_e7[a + 1:b]
                        i_lats = lats_e7[a + 1:b]
                        fstart, fend = _encode_geom(i_lons, i_lats, geom_blob)
                        key = bytes(geom_blob[fstart:fend])
                        existing_gi = geom_map.get(key)
                        if existing_gi is None:
                            fgi = len(geom_offsets) - 1
                            geom_offsets.append(fend)
                            geom_map[key] = fgi
                        else:
                            # Undo append: we already had this geom, trim blob.
                            del geom_blob[fstart:fend]
                            fgi = existing_gi
                    else:
                        fgi = -1

                    # Reverse geom (distinct encoding since deltas differ).
                    if oneway != 1 and interior_len > 0 and not near_cap:
                        r_lons = list(reversed(i_lons))
                        r_lats = list(reversed(i_lats))
                        rstart, rend = _encode_geom(r_lons, r_lats, geom_blob)
                        rkey = bytes(geom_blob[rstart:rend])
                        existing_rgi = geom_map.get(rkey)
                        if existing_rgi is None:
                            rgi = len(geom_offsets) - 1
                            geom_offsets.append(rend)
                            geom_map[rkey] = rgi
                        else:
                            del geom_blob[rstart:rend]
                            rgi = existing_rgi
                    elif oneway != 1:
                        rgi = -1

                    # dist_dm truncates at 24 bits = 1677 km; real road edges
                    # don't come close, but clamp for safety.
                    dist_dm_packed = min(dist_dm, 0xFFFFFF)
                    dist_speed = ((speed & 0xFF) << 24) | dist_dm_packed
                    if oneway != -1:
                        edges_from.append(from_idx)
                        edges_to.append(to_idx)
                        edges_dist_speed.append(dist_speed)
                        edges_geom.append(0xFFFFFFFF if fgi < 0 else fgi)
                        edges_name.append(name_idx)
                        edges_class_access.append(class_access)
                        self.edge_count += 1
                    if oneway != 1:
                        edges_from.append(to_idx)
                        edges_to.append(from_idx)
                        edges_dist_speed.append(dist_speed)
                        edges_geom.append(0xFFFFFFFF if rgi < 0 else rgi)
                        edges_name.append(name_idx)
                        edges_class_access.append(class_access)
                        self.edge_count += 1

                seg_start = i

            self.hw_count += 1
            if self.hw_count % 200000 == 0:
                print(f"\r    Pass 2: {self.hw_count} ways, "
                      f"{self.edge_count} edges, "
                      f"{len(geom_offsets) - 1} geoms, "
                      f"{len(geom_blob) // (1024 * 1024)} MB geom blob...",
                      end="", flush=True)

    p2 = _Pass2()
    # File-backed sparse node location store on a fast (NVMe) volume.
    # We iterated through several map types:
    #   - default sparse_mem_array — OOM'd US Pass 2 three runs in a row.
    #   - dense_file_array on /storage HDD — OOM-safe but each node
    #     lookup was a random HDD seek (US Pass 2 didn't finish 200k of
    #     53M ways in 1.5 h).
    #   - dense_mmap_array — anonymous mmap committed ~96 GB virtual for
    #     planet-scale node ids, OOM-killed Europe Pass 2.
    #   - sparse_mem_map — hash-based; OOM-killed Europe Pass 2 too
    #     (~50 GB peak with libosmium overhead + Pass 1 state).
    # sparse_file_array is sorted (id, lon, lat) triples on disk —
    # ~16 GB for Europe's ~1B touched nodes, sequential writes during
    # indexing, mostly cached lookups during way iteration. Putting it
    # on /data (NVMe SSD, 370 GB free) makes random reads fast enough.
    # /data is the project's reserved fast-scratch volume (separate from
    # /storage HDD and the 79 GB / root); cleaned up at end of pass.
    NODE_LOC_DIR = os.environ.get("STREETZIM_NODE_LOC_DIR", "/data")
    if not os.path.isdir(NODE_LOC_DIR) or not os.access(NODE_LOC_DIR, os.W_OK):
        NODE_LOC_DIR = output_dir
    node_loc_path = os.path.join(NODE_LOC_DIR, "streetzim_node_locations.bin")
    if os.path.exists(node_loc_path):
        os.remove(node_loc_path)
    loc_handler = osmium.NodeLocationsForWays(
        osmium.index.create_map(f"sparse_file_array,{node_loc_path}"))
    loc_handler.ignore_errors()
    osmium.apply(source_pbf, loc_handler, p2)
    # Drop loc_handler (and its libosmium index) BEFORE the post-Pass-2
    # numpy work, so the kernel can release the ~60 GB sparse_file_array
    # mmap. Without this, the file pages squat in RssFile even after
    # os.remove(), starving the argsort/fancy-indexing ops that follow
    # of cache and forcing them to thrash through swap.
    del loc_handler
    try:
        os.remove(node_loc_path)
    except OSError:
        pass
    print(f"\r    Pass 2: {p2.hw_count} ways, {p2.edge_count} edges, "
          f"{len(geom_offsets) - 1} geoms, "
          f"{len(geom_blob) / (1024 * 1024):.1f} MB geom blob          ")

    # Sort edges by from-node so adj_offsets is just a cumulative-count array.
    num_edges = len(edges_from)
    num_geoms = len(geom_offsets) - 1
    num_names = len(name_table)

    edges_from_np = np.frombuffer(edges_from, dtype=np.uint32)
    sort_order = np.argsort(edges_from_np, kind='stable')
    # Build final edges array in v4 layout (u32 stride = 5):
    #   (target, dist_speed, geom_idx, name_idx, class_access)
    # dist_speed  = (speed << 24) | dist_dm24
    # geom_idx    full u32; 0xFFFFFFFF = "no geometry"
    # class_access bit layout per docs/driving-mode-road-class-warnings.md
    edges_arr = np.empty((num_edges, 5), dtype='<u4')
    edges_arr[:, 0] = np.frombuffer(edges_to, dtype=np.uint32)[sort_order]
    edges_arr[:, 1] = np.frombuffer(edges_dist_speed, dtype=np.uint32)[sort_order]
    edges_arr[:, 2] = np.frombuffer(edges_geom, dtype=np.uint32)[sort_order]
    edges_arr[:, 3] = np.frombuffer(edges_name, dtype=np.uint32)[sort_order]
    edges_arr[:, 4] = np.frombuffer(edges_class_access, dtype=np.uint32)[sort_order]
    edges_from_sorted = edges_from_np[sort_order]
    del edges_from, edges_to, edges_dist_speed, edges_geom, edges_name, edges_class_access
    del edges_from_np, sort_order

    adj_offsets = np.zeros(num_nodes + 1, dtype='<u4')
    # Cumulative count of edges by from-node.
    if num_edges > 0:
        np.add.at(adj_offsets, edges_from_sorted.astype(np.int64) + 1, 1)
    np.cumsum(adj_offsets, out=adj_offsets)
    del edges_from_sorted

    # Nodes array in (lat_e7, lon_e7) layout. node_coords is already shaped (N, 2).
    nodes_arr = node_coords.astype('<i4', copy=False)

    # Geom offsets as numpy uint32; include the closing offset.
    geom_offsets_np = np.frombuffer(geom_offsets, dtype=np.uint32).astype('<u4', copy=False)

    # Pad geom blob to 4-byte alignment (else the following Uint32Array view
    # of name_offsets lands at a non-aligned offset and the browser throws
    # RangeError — cost us hours with Baltics; keep this).
    while len(geom_blob) % 4 != 0:
        geom_blob.append(0)
    geom_bytes_total = len(geom_blob)

    # Name table → UTF-8 blob + byte-offset index.
    name_blobs = [n.encode("utf-8") for n in name_table]
    names_bytes = sum(len(b) for b in name_blobs)
    name_offsets = np.empty(num_names + 1, dtype='<u4')
    cur = 0
    for i, b in enumerate(name_blobs):
        name_offsets[i] = cur
        cur += len(b)
    name_offsets[num_names] = cur

    # Serialize. Two layouts:
    #   * v4 inline (default) — everything in one graph.bin. Back-compat
    #     with Kiwix Desktop + mcpzim; matches docs/mcpzim-contract.md.
    #   * v5 split (--split-graph) — geoms hoisted into a companion file
    #     so the PWA can defer their GB-scale allocation until a route is
    #     actually drawn. main layout keeps the same header plus
    #     nodes/adj/edges/names. class_access bit layout unchanged.
    output_path = os.path.join(output_dir, "routing-graph.bin")
    geoms_path = None

    if not split_graph:
        # v4 inline — byte-identical to pre-split builds.
        with open(output_path, "wb") as f:
            f.write(b"SZRG")
            np.array([4, num_nodes, num_edges, num_geoms, geom_bytes_total,
                      num_names, names_bytes], dtype='<u4').tofile(f)
            nodes_arr.tofile(f)
            adj_offsets.tofile(f)
            edges_arr.tofile(f)
            geom_offsets_np.tofile(f)
            f.write(bytes(geom_blob))
            name_offsets.tofile(f)
            for b in name_blobs:
                f.write(b)
    else:
        # v5 split main file. Header sets geomBytes=0 so old parsers that
        # ignore the version field still notice "no geoms here." Readers
        # that understand v5 look for routing-graph-geoms.bin beside it.
        with open(output_path, "wb") as f:
            f.write(b"SZRG")
            np.array([5, num_nodes, num_edges, num_geoms, 0,
                      num_names, names_bytes], dtype='<u4').tofile(f)
            nodes_arr.tofile(f)
            adj_offsets.tofile(f)
            edges_arr.tofile(f)
            name_offsets.tofile(f)
            for b in name_blobs:
                f.write(b)
        # Companion geoms file — SZGM magic so the viewer can't accidentally
        # mis-interpret this as a graph buffer.
        geoms_path = os.path.join(output_dir, "routing-graph-geoms.bin")
        with open(geoms_path, "wb") as gf:
            gf.write(b"SZGM")
            np.array([1, num_geoms, geom_bytes_total], dtype='<u4').tofile(gf)
            geom_offsets_np.tofile(gf)
            gf.write(bytes(geom_blob))

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    # Class_access diagnostics — helps verify the writer populated flags
    # for regions that are expected to have lots of roundabouts or ramps.
    class_access_col = edges_arr[:, 4]
    num_round = int(((class_access_col >> 8) & 1).sum())
    num_link = int(np.isin((class_access_col & 0x1F), [2, 4, 6, 8, 10]).sum())
    fmt_note = "v5 split" if split_graph else "v4 inline"
    geoms_note = ""
    if geoms_path:
        geoms_mb = os.path.getsize(geoms_path) / (1024 * 1024)
        geoms_note = f", companion {geoms_mb:.1f} MB"
    print(f"    Routing graph ({fmt_note}): {size_mb:.1f} MB{geoms_note} "
          f"({num_nodes} nodes, {num_edges} edges, {num_geoms} geoms, "
          f"{geom_bytes_total / (1024*1024):.1f} MB geom blob, "
          f"{num_names} names, {names_bytes / 1024:.0f} KB name text, "
          f"{num_round} roundabout + {num_link} link edges)")
    return output_path, geoms_path


def chunk_graph_file(src_path: str, chunk_size_bytes: int,
                     out_prefix: str = "routing-graph-chunk") -> tuple[list[str], dict]:
    """Split ``src_path`` into N files of ``chunk_size_bytes`` each.

    Returns (chunk_paths, manifest_dict). The manifest mirrors the shape
    the viewer's ``loadChunkedGraph()`` expects:

        {"schema": 1, "total_bytes": <size>,
         "chunks": [{"path": "...", "bytes": N}, ...]}

    A dedicated manifest entry (rather than inferring from file listing)
    keeps the ordering deterministic for the reader. If concatenation of
    the chunks doesn't byte-match the source, the loader rejects them —
    that saves a lot of pain tracking down torn uploads.
    """
    import hashlib
    if chunk_size_bytes <= 0:
        raise ValueError("chunk_size_bytes must be positive")
    src_size = os.path.getsize(src_path)
    chunk_paths: list[str] = []
    entries: list[dict] = []
    src_dir = os.path.dirname(src_path) or "."

    with open(src_path, "rb") as src:
        idx = 0
        while True:
            chunk = src.read(chunk_size_bytes)
            if not chunk:
                break
            fname = f"{out_prefix}-{idx:04d}.bin"
            out_path = os.path.join(src_dir, fname)
            with open(out_path, "wb") as fh:
                fh.write(chunk)
            chunk_paths.append(out_path)
            entries.append({"path": fname, "bytes": len(chunk)})
            idx += 1

    # Sanity sha — the reader verifies this so torn uploads fail loud.
    h = hashlib.sha256()
    with open(src_path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)

    manifest = {
        "schema": 1,
        "total_bytes": src_size,
        "sha256": h.hexdigest(),
        "chunks": entries,
    }
    return chunk_paths, manifest


def extract_searchable_features(tiles=None, mbtiles_path=None, output_dir=None):
    """Extract named features from z14 vector tiles for search indexing.

    Decodes the highest-zoom tiles and extracts features with names from
    the place, poi, transportation_name, water_name, park, mountain_peak,
    and aerodrome_label layers.

    Can operate in two modes:
    - tiles=dict: legacy mode, filters z14 from in-memory dict
    - mbtiles_path=str: streaming mode, reads z14 directly from SQLite

    If output_dir is set, writes features to a JSONL file on disk and returns
    the file path (freeing the in-memory list). Otherwise returns a list of dicts.
    """
    import mapbox_vector_tile

    print("  Extracting searchable features from tiles...")

    # Layers that contain searchable named features
    search_layers = {
        "place": "place",
        "poi": "poi",
        "transportation_name": "street",
        "water_name": "water",
        "waterway": "water",
        "park": "park",
        "mountain_peak": "peak",
        "aerodrome_label": "airport",
        "building": "building",
        "landuse": "area",
    }

    if mbtiles_path:
        # Streaming mode: each worker reads its own partition from SQLite
        conn = sqlite3.connect(str(mbtiles_path))
        total_z14 = conn.execute(
            "SELECT COUNT(*) FROM tiles WHERE zoom_level = 14"
        ).fetchone()[0]
        if total_z14 == 0:
            conn.close()
            print("    No z14 tiles found in mbtiles")
            if output_dir:
                features_path = os.path.join(output_dir, "search_features.jsonl")
                open(features_path, "w").close()
                return features_path
            return []

        # Balanced partitioning: query tile counts per column and split evenly
        print("    Querying tile distribution for balanced partitioning...")
        col_counts = conn.execute(
            "SELECT tile_column, COUNT(*) FROM tiles WHERE zoom_level = 14 "
            "GROUP BY tile_column ORDER BY tile_column"
        ).fetchall()
        conn.close()

        import multiprocessing
        import os as _os
        import tempfile
        num_workers = min(_os.cpu_count() or 4, len(col_counts))
        # Use 4x more partitions than workers for dynamic load balancing —
        # dense urban partitions take longer per tile, so small partitions let
        # idle workers pick up the next chunk instead of waiting on one straggler.
        num_partitions = min(num_workers * 4, len(col_counts))
        print(f"    Processing {total_z14} z14 tiles across {len(col_counts)} columns "
              f"with {num_workers} workers, {num_partitions} partitions...")

        # Split columns into partitions with roughly equal tile counts
        tiles_per_partition = total_z14 / num_partitions
        partitions = []
        tmp_dir = tempfile.mkdtemp(prefix="streetzim_search_")
        current_start = col_counts[0][0]
        current_count = 0
        part_idx = 0

        for col, cnt in col_counts:
            current_count += cnt
            if current_count >= tiles_per_partition and part_idx < num_partitions - 1:
                tmp_file = os.path.join(tmp_dir, f"features_{part_idx}.jsonl")
                partitions.append((mbtiles_path, current_start, col + 1, search_layers, tmp_file))
                part_idx += 1
                current_start = col + 1
                current_count = 0

        # Last partition gets the rest
        if part_idx < num_partitions:
            tmp_file = os.path.join(tmp_dir, f"features_{part_idx}.jsonl")
            last_col = col_counts[-1][0]
            partitions.append((mbtiles_path, current_start, last_col + 1, search_layers, tmp_file))

        processed = 0
        total_features = 0
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(num_workers) as pool:
            for output_file, batch_count, batch_feats in pool.imap_unordered(
                _process_tile_partition, partitions
            ):
                processed += batch_count
                total_features += batch_feats
                print(f"\r    Processed {processed}/{total_z14} tiles, {total_features} features (pre-dedup)...", end="", flush=True)

        print()

        # Stream features from temp JSONL files for cross-worker dedup
        print(f"    Cross-worker deduplication from {len(partitions)} temp files...")
        features = []
        seen_global = set()
        for part_args in partitions:
            tmp_file = part_args[4]
            if not os.path.exists(tmp_file):
                continue
            with open(tmp_file, "r") as f:
                for line in f:
                    feat = json.loads(line)
                    dedup_key = (feat["name"].lower(), feat["type"],
                                 round(feat["lat"], 4), round(feat["lon"], 4))
                    if dedup_key not in seen_global:
                        seen_global.add(dedup_key)
                        features.append(feat)
            os.unlink(tmp_file)
        del seen_global

        # Clean up temp dir
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

        print(f"    {len(features)} unique features after cross-worker dedup")
    else:
        # Legacy mode: filter from in-memory dict
        z14_tiles = {(z, x, y): data for (z, x, y), data in tiles.items() if z == 14}
        if not z14_tiles:
            max_z = max(z for z, x, y in tiles.keys())
            z14_tiles = {(z, x, y): data for (z, x, y), data in tiles.items() if z == max_z}
            print(f"    No z14 tiles found, using z{max_z}")

        features = []
        import multiprocessing
        import os as _os
        num_workers = _os.cpu_count() or 4
        total_tiles = len(z14_tiles)
        print(f"    Processing {total_tiles} z14 tiles with {num_workers} workers...")

        tile_iter = (
            (z, x, y, data, search_layers)
            for (z, x, y), data in z14_tiles.items()
        )
        chunk_size = max(1, total_tiles // (num_workers * 4))
        processed = 0

        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(num_workers) as pool:
            for batch_features in pool.imap_unordered(
                _process_tile_for_search,
                tile_iter,
                chunksize=chunk_size,
            ):
                features.extend(batch_features)
                processed += 1
                if processed % 5000 == 0:
                    print(f"\r    Processed {processed}/{total_tiles} tiles, {len(features)} features so far...", end="", flush=True)

        if processed > 5000:
            print()  # Newline after progress

    # Ensure multiprocessing cleanup before libzim
    import gc
    gc.collect()

    # Deduplicate across tiles (only needed for legacy path; mbtiles path dedups inline)
    if not mbtiles_path:
        seen = set()
        deduped = []
        for f in features:
            dedup_key = (f["name"].lower(), f["type"], round(f["lat"], 4), round(f["lon"], 4))
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            deduped.append(f)
        features = deduped

    # Assign location context (nearest city/town) to each feature
    print("    Assigning location context to features...")
    places = [f for f in features if f["type"] == "place"]
    if places:
        # Build a coarse spatial grid of places for fast nearest-neighbor lookup
        # Grid cells are ~0.5 degrees (~50km)
        from collections import defaultdict
        place_grid = defaultdict(list)
        for p in places:
            gx = int(p["lon"] * 2)
            gy = int(p["lat"] * 2)
            place_grid[(gx, gy)].append(p)

        # Convert to regular dict for pickling (multiprocessing)
        place_grid_dict = dict(place_grid)

        # For small feature sets, run directly; for large ones, use multiprocessing
        if len(features) > 100_000:
            from concurrent.futures import ProcessPoolExecutor
            batch_size = max(10_000, len(features) // (os.cpu_count() or 4))
            batches = [features[i:i + batch_size] for i in range(0, len(features), batch_size)]
            num_workers = min(os.cpu_count() or 4, len(batches))

            assigned = 0
            with ProcessPoolExecutor(
                max_workers=num_workers,
                initializer=_init_location_worker,
                initargs=(place_grid_dict,),
            ) as pool:
                for batch_idx, locs in enumerate(pool.map(_assign_location_batch, batches)):
                    start_idx = batch_idx * batch_size
                    for j, loc in enumerate(locs):
                        if loc:
                            features[start_idx + j]["location"] = loc
                            assigned += 1
        else:
            # For small sets, set the global directly and run in-process
            global _place_grid
            _place_grid = place_grid_dict
            assigned = 0
            locs = _assign_location_batch(features)
            for j, loc in enumerate(locs):
                if loc:
                    features[j]["location"] = loc
                    assigned += 1

        print(f"    Assigned location to {assigned}/{len(features)} features")

    # Sort by type priority then name
    type_order = {"place": 0, "airport": 1, "peak": 2, "park": 3, "water": 4, "poi": 5, "street": 6}
    features.sort(key=lambda f: (type_order.get(f["type"], 99), f["name"]))

    print(f"    Extracted {len(features)} searchable features")
    type_counts = {}
    for f in features:
        type_counts[f["type"]] = type_counts.get(f["type"], 0) + 1
    for t, c in sorted(type_counts.items()):
        print(f"      {t}: {c}")

    if output_dir:
        features_path = os.path.join(output_dir, "search_features.jsonl")
        with open(features_path, "w") as fout:
            for feat in features:
                fout.write(json.dumps(feat, separators=(",", ":")) + "\n")
        count = len(features)
        del features
        import gc; gc.collect()
        size_mb = os.path.getsize(features_path) / (1024 * 1024)
        print(f"    Wrote {count} features to disk ({size_mb:.0f} MB)")
        return features_path

    return features


def download_maplibre(dest_dir):
    """Download MapLibre GL JS files for embedding in the ZIM."""
    print("  Downloading MapLibre GL JS...")
    js_url = f"{MAPLIBRE_CDN}/maplibre-gl.js"
    css_url = f"{MAPLIBRE_CDN}/maplibre-gl.css"

    js_path = os.path.join(dest_dir, "maplibre-gl.js")
    css_path = os.path.join(dest_dir, "maplibre-gl.css")

    download_file(js_url, js_path, "maplibre-gl.js")
    download_file(css_url, css_path, "maplibre-gl.css")

    return js_path, css_path


def _sub_bucket_for_name(name: str, n_buckets: int) -> int:
    """FNV-1a 32-bit hash of the UTF-8 bytes of `name`, mod n_buckets.

    Used when ``--split-hot-search-chunks-mb`` fans out an oversized
    prefix chunk into ``{prefix}-{hex}`` sub-files. MUST match:
      * ``cloud/repackage_zim._sub_bucket_for_name``
      * viewer ``subBucketFor`` in resources/viewer/index.html
      * Swift ``Geocoder.subBucketFor`` in mcpzim/MCPZimKit
    Any disagreement silently drops records from query results.
    """
    h = 0x811C9DC5  # FNV offset basis (32-bit)
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h % n_buckets


def _split_big_search_chunk(prefix: str, records: list, n_buckets: int = 16
                            ) -> list[tuple[str, bytes]]:
    """Fan out `records` into up to `n_buckets` sub-chunks based on
    FNV-1a hash of record['n']. Returns [(sub_prefix, json_bytes), …]
    — same on-disk format the repackage writer + JS/Swift readers
    expect. Empty buckets are omitted (not emitted)."""
    import json as _json
    buckets: list[list] = [[] for _ in range(n_buckets)]
    for rec in records:
        name = rec.get("n", "") or ""
        buckets[_sub_bucket_for_name(name, n_buckets)].append(rec)
    hex_width = len(format(n_buckets - 1, "x"))
    out = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        sub_prefix = f"{prefix}-{format(i, f'0{hex_width}x')}"
        sub_bytes = _json.dumps(bucket, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
        out.append((sub_prefix, sub_bytes))
    return out


def create_zim(
    output_path,
    tiles,
    tile_metadata,
    fonts,
    maplibre_js_path,
    maplibre_css_path,
    viewer_html_path,
    map_config,
    name,
    mbtiles_path=None,
    tile_count=None,
    description="Offline OpenStreetMap",
    cluster_size=2048 * 1024,
    search_features=None,
    search_features_path=None,
    satellite_dir=None,
    satellite_max_zoom=None,
    satellite_format="webp",
    terrain_dir=None,
    terrain_max_zoom=None,
    zim_workers=None,
    bbox=None,
    wikidata_data=None,
    routing_graph_path=None,
    routing_graph_geoms_path=None,
    routing_graph_chunk_mb=0,
    wiki_cross_refs=None,
    address_count=0,
    overture_sources=None,
    overture_themes=None,
    split_hot_search_chunks_mb=0,
    split_find_chips=False,
    zim_builder="python",
    max_zoom=None,
):
    """Create a ZIM file containing the map viewer and all tiles."""
    from libzim.writer import Creator as LibzimCreator, Item, StringProvider, FileProvider
    from libzim.writer import Hint
    if zim_builder == "rust":
        from cloud.manifest_writer import ManifestCreator
        Creator = lambda p: ManifestCreator(  # noqa: E731 — small adapter
            p, verbose=True
        )
    else:
        Creator = LibzimCreator

    print(f"  Creating ZIM file: {output_path}")
    print(f"    Name: {name}")
    print(f"    Tiles: {tile_count if tiles is None else len(tiles)}")
    print(f"    Fonts: {len(fonts)}")

    class MapItem(Item):
        """A single item (file) in the ZIM archive."""
        def __init__(self, path, title, mimetype, content, is_front=False, compress=True):
            super().__init__()
            self._path = path
            self._title = title
            self._mimetype = mimetype
            self._is_front = is_front
            self._compress = compress
            # Normalize content to bytes
            if isinstance(content, (str, Path)) and os.path.isfile(str(content)):
                self._file_path = str(content)
                self._data = None
            else:
                self._file_path = None
                self._data = content if isinstance(content, bytes) else str(content).encode("utf-8")

        def get_path(self):
            return self._path

        def get_title(self):
            return self._title

        def get_mimetype(self):
            return self._mimetype

        def get_contentprovider(self):
            if self._file_path:
                return FileProvider(self._file_path)
            return StringProvider(self._data)

        def get_hints(self):
            return {Hint.FRONT_ARTICLE: self._is_front, Hint.COMPRESS: self._compress}

    # Create ZIM file
    # config_indexing and set_mainpath must be called BEFORE __enter__
    creator = Creator(str(output_path))
    creator.config_indexing(True, "en")
    creator.config_clustersize(cluster_size)
    # Use 2 compression workers for large builds to avoid libzim's
    # spin-lock death spiral. With many workers + ZSTD level 22, all
    # workers busy-wait in queue.h pushToQueue()/popFromQueue() and
    # the build stalls permanently. 2 workers avoids contention while
    # still allowing the main thread to fill the queue ahead.
    num_workers = zim_workers or min(os.cpu_count() or 4, 20)
    print(f"    ZIM compression workers: {num_workers} (tiles: {tile_count if tiles is None else len(tiles)})", flush=True)
    creator.config_nbworkers(num_workers)
    creator.set_mainpath("index.html")
    with creator:

        # Add metadata — Name and Illustration are required by Kiwix to register the ZIM
        zim_name = name.lower().replace(" ", "_").replace(",", "").replace(".", "")
        creator.add_metadata("Title", name)
        creator.add_metadata("Description", description)
        creator.add_metadata("Language", "eng")
        creator.add_metadata("Publisher", "create_osm_zim")
        creator.add_metadata("Creator", "OpenStreetMap contributors")
        import time as _time
        creator.add_metadata("Date", _time.strftime("%Y-%m-%d"))
        creator.add_metadata("Tags", "maps;osm;offline;_pictures:yes;_ftindex:yes")
        creator.add_metadata("Name", f"osm_{zim_name}")
        creator.add_metadata("Flavour", "maxi")
        creator.add_metadata("Scraper", "streetzim/1.0")
        license_parts = [
            "Map data: ODbL (OpenStreetMap)",
            "Tile schema: CC-BY 4.0 (OpenMapTiles)",
            "Satellite imagery: CC BY-NC-SA 4.0 (Sentinel-2 cloudless by EOX)",
            "Elevation: Copernicus GLO-30 DEM © DLR/Airbus, provided under COPERNICUS by EU and ESA",
            "Place info: CC0 (Wikidata) / CC BY-SA 3.0 (Wikipedia)",
        ]
        if overture_sources:
            # Overture's addresses theme ships mixed per-source licenses
            # (CC0/CC-BY-4.0/OGL-UK/etc.). We point to the dataset credits
            # embedded in the ZIM as overture-sources.json rather than
            # enumerating every upstream feed inline.
            license_parts.append(
                "Address enrichment: Overture Maps Foundation "
                "(overturemaps.org) — dataset credits in overture-sources.json"
            )
        license_parts.append("Tool code: MIT")
        creator.add_metadata("License", "; ".join(license_parts))

        # Add 48x48 illustration (required by Kiwix to show in library)
        # Generate a simple map icon as PNG
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (48, 48), (37, 99, 235, 255))
            draw = ImageDraw.Draw(img)
            # Simple globe/map icon
            draw.ellipse([8, 8, 40, 40], outline=(255, 255, 255, 200), width=2)
            draw.line([24, 8, 24, 40], fill=(255, 255, 255, 120), width=1)
            draw.line([8, 24, 40, 24], fill=(255, 255, 255, 120), width=1)
            draw.arc([4, 8, 44, 40], 0, 360, fill=(255, 255, 255, 80), width=1)
            import io
            buf = io.BytesIO()
            img.save(buf, "PNG")
            creator.add_illustration(48, buf.getvalue())
        except ImportError:
            pass  # PIL not available, skip illustration

        # Add the viewer HTML (main page)
        print("    Adding viewer HTML...")
        creator.add_item(MapItem(
            "index.html", name, "text/html",
            open(str(viewer_html_path)).read().encode("utf-8"),
            is_front=True,
        ))

        # Find-places mini-app (`places.html`). LLM-free: searches the
        # in-ZIM `search-data/` + `category-index/` files client-side
        # and links each result through the viewer's `dest=` hash so
        # the user lands in the routing panel with the destination
        # pre-filled. Same single file works in Kiwix and the Firebase
        # PWA shell — see HOW_TO_BUILD-style notes in the file itself.
        places_path = VIEWER_DIR / "places.html"
        if places_path.exists():
            print("    Adding places.html (find-places mini-app)...")
            creator.add_item(MapItem(
                "places.html", "Find places", "text/html",
                open(str(places_path)).read().encode("utf-8"),
                is_front=False,
            ))

        # Add MapLibre GL JS
        print("    Adding MapLibre GL JS...")
        creator.add_item(MapItem(
            "maplibre-gl.js", "MapLibre GL JS", "application/javascript",
            maplibre_js_path,
        ))
        creator.add_item(MapItem(
            "maplibre-gl.css", "MapLibre GL CSS", "text/css",
            maplibre_css_path,
        ))

        # Add map config
        config_json = json.dumps(map_config, indent=2)
        creator.add_item(MapItem(
            "map-config.json", "Map Config", "application/json",
            config_json.encode("utf-8"),
        ))

        # Watchdog thread: monitors progress and dumps all thread stacks on stall
        import threading, sys, traceback
        _watchdog_tile_count = [0]  # mutable container for thread access
        _watchdog_stop = threading.Event()

        def _watchdog():
            last_count = 0
            stall_seconds = 0
            while not _watchdog_stop.is_set():
                _watchdog_stop.wait(10)  # check every 10 seconds
                current = _watchdog_tile_count[0]
                if current == last_count and current > 0:
                    stall_seconds += 10
                    if stall_seconds >= 30:
                        # Stall detected — dump everything
                        print(f"\n\n=== WATCHDOG: No progress for {stall_seconds}s (stuck at tile {current}) ===", flush=True)
                        try:
                            tmp_path = str(output_path) + ".tmp"
                            if os.path.exists(tmp_path):
                                print(f"    File size: {os.path.getsize(tmp_path) / 1e9:.2f} GB", flush=True)
                            else:
                                print(f"    File size: {os.path.getsize(str(output_path)) / 1e9:.2f} GB", flush=True)
                        except OSError:
                            print(f"    File not yet created", flush=True)
                        import resource
                        mem_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**3)
                        print(f"    RSS: {mem_gb:.1f} GB", flush=True)
                        print(f"    Threads: {threading.active_count()}", flush=True)
                        # Dump all thread stacks
                        frames = sys._current_frames()
                        for tid, frame in frames.items():
                            tname = "unknown"
                            for t in threading.enumerate():
                                if t.ident == tid:
                                    tname = t.name
                                    break
                            print(f"\n--- Thread {tid} ({tname}) ---", flush=True)
                            traceback.print_stack(frame)
                            sys.stdout.flush()
                        print(f"=== END WATCHDOG DUMP ===\n", flush=True)
                        stall_seconds = 0  # reset so we dump again if still stuck
                else:
                    stall_seconds = 0
                last_count = current

        watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        watchdog_thread.start()

        # Add vector tiles — decompress in parallel for speed
        import time
        import itertools
        from concurrent.futures import ThreadPoolExecutor

        def decompress_tile(item):
            z, x, y, data = item
            if data[:2] == b"\x1f\x8b":  # gzip magic bytes
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            return z, x, y, data

        # Stream tiles from mbtiles or use in-memory dict
        if mbtiles_path:
            total_tiles = tile_count or 0
            tile_source = iter_tiles_from_mbtiles(mbtiles_path, bbox=bbox, max_zoom=max_zoom)
        else:
            total_tiles = len(tiles)
            tile_source = iter([(z, x, y, data) for (z, x, y), data in sorted(tiles.items())])

        print(f"    Adding {total_tiles} vector tiles...", flush=True)
        tiles_added = 0
        # Tilemaker emits a 0-byte PBF for every tile coord that has no
        # features in its bbox (deep ocean / desert / pure-empty). Adding
        # those wastes a libzim entry per tile (~50 B each) and floods
        # zimcheck's "Empty article" report (3k–191k per region as of
        # 2026-04-25). MapLibre treats 404 and "0-byte tile" the same —
        # nothing to render — so we drop them at write time. Real-content
        # near-empty tiles (e.g. 55-byte ocean-only with a water/ocean
        # layer) ARE kept; they paint the right ocean color when MapLibre
        # styles them.
        tiles_skipped_empty = 0
        tile_start = time.time()
        batch_start = time.time()
        batch_size = 1000
        # Adaptive backpressure: if a batch of add_item() calls slows down,
        # sleep briefly to let libzim's compression workers drain the queue.
        # This prevents the spin-lock death spiral in libzim's queue.h where
        # the main thread and all workers busy-wait with microsleep().
        backpressure_sleep = 0.0
        while True:
            batch = list(itertools.islice(tile_source, batch_size))
            if not batch:
                break
            decompress_start = time.time()
            with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
                results = list(pool.map(decompress_tile, batch))
            decompress_time = time.time() - decompress_start

            add_start = time.time()
            for i, (z, x, y, tile_data) in enumerate(results):
                # See note above: 0-byte tiles are MVT placeholders for
                # bbox cells with no features. Drop them — MapLibre
                # rendering is unaffected, ZIM entries dedup, zimcheck
                # "Empty article" count goes to 0.
                if not tile_data:
                    tiles_skipped_empty += 1
                    continue
                item_start = time.time()
                creator.add_item(MapItem(
                    f"tiles/{z}/{x}/{y}.pbf", f"Tile {z}/{x}/{y}",
                    "application/x-protobuf",
                    tile_data,
                ))
                item_elapsed = time.time() - item_start
                tiles_added += 1
                _watchdog_tile_count[0] = tiles_added
                # Per-item backpressure: if a single add_item() took over
                # 100ms, the queue is full — sleep to let workers drain.
                # This prevents the spin-lock stall where add_item blocks
                # forever inside libzim's C++ queue.
                if item_elapsed > 0.1:
                    time.sleep(min(item_elapsed * 2, 2.0))
            add_time = time.time() - add_start

            # Batch-level backpressure: if overall rate is slow, add
            # sleep between batches too.
            batch_rate = batch_size / add_time if add_time > 0 else float("inf")
            if batch_rate < 5000 and total_tiles > 100_000:
                backpressure_sleep = min(backpressure_sleep + 0.05, 1.0)
                time.sleep(backpressure_sleep)
            elif batch_rate > 15000:
                backpressure_sleep = max(backpressure_sleep - 0.01, 0.0)

            batch_start = time.time()

            if tiles_added % 2000 == 0:
                elapsed = time.time() - tile_start
                rate = tiles_added / elapsed if elapsed > 0 else 0
                remaining = (total_tiles - tiles_added) / rate if rate > 0 else 0
                import resource
                mem_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**3)
                bp_str = f" bp={backpressure_sleep*1000:.0f}ms" if backpressure_sleep > 0 else ""
                print(f"\r    Added {tiles_added}/{total_tiles} tiles ({rate:.0f}/s, ~{remaining/60:.0f}m left, {mem_gb:.1f}GB RSS{bp_str})...", end="", flush=True)

        elapsed = time.time() - tile_start
        rate_str = f"{tiles_added/elapsed:.0f}/s" if elapsed > 0 else "instant"
        skip_str = (f" (skipped {tiles_skipped_empty} empty)"
                    if tiles_skipped_empty else "")
        print(f"\r    Added {tiles_added} tiles in {elapsed:.0f}s ({rate_str}){skip_str}                ", flush=True)
        _watchdog_stop.set()  # stop watchdog after tiles

        # Build bbox tile filter if bbox is provided (shared cache may have tiles from other areas)
        def _tile_in_bbox(z, x, y, bbox_coords):
            """Check if tile (z,x,y) overlaps with bbox. Uses mercantile for accuracy."""
            import mercantile
            tile_bounds = mercantile.bounds(mercantile.Tile(x, y, z))
            minlon, minlat, maxlon, maxlat = bbox_coords
            return not (tile_bounds.east < minlon or tile_bounds.west > maxlon or
                        tile_bounds.north < minlat or tile_bounds.south > maxlat)

        def _add_raster_tiles(source_dir, zim_prefix, max_zoom, label, ext="webp", mimetype="image/webp"):
            """Walk a tile cache dir and add tiles to ZIM, filtering by bbox."""
            count = 0
            skipped = 0
            suffix = f".{ext}"
            strip_len = len(suffix)
            for z in range(0, max_zoom + 1):
                z_dir = os.path.join(source_dir, str(z))
                if not os.path.isdir(z_dir):
                    continue
                for x_name in sorted(os.listdir(z_dir)):
                    x_dir = os.path.join(z_dir, x_name)
                    if not os.path.isdir(x_dir):
                        continue
                    try:
                        x = int(x_name)
                    except ValueError:
                        continue
                    for fname in os.listdir(x_dir):
                        if not fname.endswith(suffix):
                            continue
                        try:
                            y = int(fname[:-strip_len])
                        except ValueError:
                            continue
                        if bbox and not _tile_in_bbox(z, x, y, bbox):
                            skipped += 1
                            continue
                        fpath = os.path.join(x_dir, fname)
                        zim_path = f"{zim_prefix}/{z}/{x_name}/{fname}"
                        creator.add_item(MapItem(
                            zim_path, f"{label} {z}/{x_name}/{fname}",
                            mimetype,
                            fpath,
                            compress=False,
                        ))
                        count += 1
                        if count % 2000 == 0:
                            print(f"\r    Added {count} {label.lower()} tiles...", end="", flush=True)
            print(f"\r    Added {count} {label.lower()} tiles" +
                  (f" (skipped {skipped} outside bbox)" if skipped else ""))
            return count

        # Add satellite tiles if provided
        if satellite_dir and os.path.isdir(satellite_dir):
            sat_ext = satellite_format  # "webp" or "avif"
            sat_mime = "image/avif" if sat_ext == "avif" else "image/webp"
            max_sz = satellite_max_zoom if satellite_max_zoom is not None else 99
            _add_raster_tiles(satellite_dir, "satellite", max_sz, "Satellite",
                              ext=sat_ext, mimetype=sat_mime)

        # Add terrain tiles if provided
        if terrain_dir and os.path.isdir(terrain_dir):
            max_tz = terrain_max_zoom if terrain_max_zoom is not None else 99
            _add_raster_tiles(terrain_dir, "terrain", max_tz, "Terrain")

        # Add font glyphs
        print(f"    Adding {len(fonts)} font glyph ranges...")
        for (font_name, range_key), data in fonts.items():
            # font_name has no spaces (e.g. "OpenSansRegular") to avoid
            # URL-encoding issues across Kiwix implementations
            path = f"fonts/{font_name}/{range_key}.pbf"
            creator.add_item(MapItem(
                path, f"Font {font_name} {range_key}",
                "application/x-protobuf",
                data,
            ))

        # Add Wikidata info — filter to Q-IDs present in the bbox tiles
        # Skip filtering for world bbox (all Q-IDs are relevant)
        if wikidata_data:
            is_world_bbox = bbox and abs(bbox[0] - (-180)) < 1 and abs(bbox[2] - 180) < 1 and abs(bbox[1] - (-85)) < 2 and abs(bbox[3] - 85) < 2
            if bbox and mbtiles_path and not is_world_bbox:
                print(f"    Scanning tiles for Wikidata Q-IDs in bbox...")
                import mapbox_vector_tile as _mvt
                bbox_qids = set()
                for z, x, y, data in iter_tiles_from_mbtiles(mbtiles_path, zoom_level=14, bbox=bbox):
                    tile_data = data
                    if data[:2] == b"\x1f\x8b":
                        try:
                            tile_data = gzip.decompress(data)
                        except Exception:
                            continue
                    try:
                        decoded = _mvt.decode(tile_data, y_coord_down=True)
                    except Exception:
                        continue
                    for layer in decoded.values():
                        for feat in layer.get("features", []):
                            qid = (feat.get("properties") or {}).get("wikidata", "")
                            if qid and qid.startswith("Q"):
                                bbox_qids.add(qid)
                filtered = {qid: data for qid, data in wikidata_data.items() if qid in bbox_qids}
                print(f"    Filtered Wikidata: {len(filtered)} entries in bbox (from {len(wikidata_data)} total)")
                wikidata_data = filtered

            print(f"    Adding Wikidata info for {len(wikidata_data)} features...")
            from collections import defaultdict as _dd
            wd_chunks = _dd(dict)
            for qid, data in wikidata_data.items():
                # Bucket by first 2 chars of Q-ID number for chunked loading
                num = qid[1:]  # strip 'Q'
                prefix = num[:2] if len(num) >= 2 else num.ljust(2, "0")
                wd_chunks[prefix][qid] = data

            # Write manifest
            wd_manifest = {
                "total": len(wikidata_data),
                "chunks": {k: len(v) for k, v in sorted(wd_chunks.items())},
            }
            creator.add_item(MapItem(
                "wikidata/manifest.json", "Wikidata Manifest", "application/json",
                json.dumps(wd_manifest, separators=(",", ":")).encode("utf-8"),
            ))

            # Write each chunk
            for prefix, chunk_entries in sorted(wd_chunks.items()):
                chunk_json = json.dumps(chunk_entries, separators=(",", ":"),
                                        ensure_ascii=False)
                creator.add_item(MapItem(
                    f"wikidata/{prefix}.json",
                    f"Wikidata chunk {prefix}",
                    "application/json",
                    chunk_json.encode("utf-8"),
                ))

            total_bytes = sum(
                len(json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode())
                for v in wd_chunks.values()
            )
            print(f"    Added {len(wd_chunks)} Wikidata chunks ({total_bytes / 1024:.0f} KB)")

        # Add routing graph data.
        # Large regions produce multi-hundred-MB / multi-GB graph.bin
        # files (Japan = 1.8 GB, Europe/US ≥ 3 GB). libzim's default
        # ZSTD clustering puts the whole file in one giant compressed
        # cluster, which our in-browser PWA's pure-JS `fzstd` port
        # cannot decompress in a single shot — it throws "invalid zstd
        # data" around ~500 MB. Set COMPRESS=0 so the file lands in
        # its own uncompressed cluster; the cluster header becomes
        # type-1 (raw) and `zim-reader.js` bypasses fzstd entirely.
        # SZRG is a tight binary format already (~10–15% ZSTD gain),
        # so the ZIM grows by only that much in return for PWA-
        # parseable routing.
        if routing_graph_path and os.path.isfile(routing_graph_path):
            size_mb = os.path.getsize(routing_graph_path) / (1024 * 1024)
            if routing_graph_chunk_mb and routing_graph_chunk_mb > 0:
                # Byte-range chunk the primary graph file into N entries
                # so libzim puts each in its own cluster. fzstd's ~500 MB
                # ceiling is the actual blocker for Japan-size ZIMs; this
                # side-steps it without touching the SZRG format.
                # Always emit the monolithic graph.bin — Kiwix iOS
                # (and Desktop, and mcpzim) read it natively via
                # libzim's cluster decompression, independent of the
                # PWA fzstd path. Skipping this broke iOS on Iran
                # 2026-04-24. The chunks below are for PWA fzstd
                # only; both coexist cheaply.
                compress_graph = size_mb < 200
                creator.add_item(MapItem(
                    "routing-data/graph.bin",
                    "Routing Graph",
                    "application/octet-stream",
                    routing_graph_path,
                    compress=compress_graph,
                ))
                print(f"    Adding routing graph ({size_mb:.1f} MB, "
                      f"{'compressed' if compress_graph else 'raw'}) "
                      f"+ {routing_graph_chunk_mb} MB chunks for PWA...")
                # NOTE: out_prefix is what the manifest records. The
                # reader joins it with the manifest's *directory* inside
                # the ZIM, so keep this in lock-step with the ZIM entry
                # names below ("graph-chunk-NNNN.bin" under routing-data/).
                chunk_paths, manifest = chunk_graph_file(
                    routing_graph_path,
                    routing_graph_chunk_mb * 1024 * 1024,
                    out_prefix="graph-chunk",
                )
                # Manifest first — the reader checks it to learn chunk order.
                creator.add_item(MapItem(
                    "routing-data/graph-chunk-manifest.json",
                    "Routing Graph Manifest",
                    "application/json",
                    json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
                    compress=True,
                ))
                for i, cp in enumerate(chunk_paths):
                    cp_mb = os.path.getsize(cp) / (1024 * 1024)
                    compress_chunk = cp_mb < 200
                    creator.add_item(MapItem(
                        f"routing-data/graph-chunk-{i:04d}.bin",
                        f"Routing Graph Chunk {i}",
                        "application/octet-stream",
                        cp,
                        compress=compress_chunk,
                    ))
                print(f"    Wrote monolithic graph.bin + "
                      f"{len(chunk_paths)} chunks + manifest")
            else:
                # Cap where compression helps more than it hurts: below
                # ~200 MB fzstd handles it fine in one shot, so keep it
                # compressed. Above, skip compression for PWA compat.
                compress_graph = size_mb < 200
                compress_note = "compressed" if compress_graph else "raw (PWA-compat)"
                print(f"    Adding routing graph ({size_mb:.1f} MB, {compress_note})...")
                creator.add_item(MapItem(
                    "routing-data/graph.bin",
                    "Routing Graph",
                    "application/octet-stream",
                    routing_graph_path,
                    compress=compress_graph,
                ))
            # v5 companion — only emitted when --split-graph was passed.
            # Compressed is fine since the viewer lazy-loads it on route
            # render, not startup; fzstd has to decompress only when a
            # route is drawn, which is an easy allocation window compared
            # to the original "everything at page load" pattern.
            if (routing_graph_geoms_path
                    and os.path.isfile(routing_graph_geoms_path)):
                geoms_mb = os.path.getsize(routing_graph_geoms_path) / (1024 * 1024)
                if routing_graph_chunk_mb and routing_graph_chunk_mb > 0:
                    # Same reason we chunk graph.bin — the geoms companion
                    # is typically 30–50% of total graph size, so it also
                    # busts fzstd's per-cluster cap on continents. Chunk it.
                    print(f"    Adding geoms companion chunked "
                          f"({geoms_mb:.1f} MB → {routing_graph_chunk_mb} MB chunks)...")
                    # Same lock-step naming constraint as the main graph
                    # chunks — manifest path must match the ZIM entry
                    # relative to routing-data/.
                    chunk_paths, manifest = chunk_graph_file(
                        routing_graph_geoms_path,
                        routing_graph_chunk_mb * 1024 * 1024,
                        out_prefix="graph-geoms-chunk",
                    )
                    creator.add_item(MapItem(
                        "routing-data/graph-geoms-chunk-manifest.json",
                        "Routing Geoms Manifest",
                        "application/json",
                        json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
                        compress=True,
                    ))
                    for i, cp in enumerate(chunk_paths):
                        cp_mb = os.path.getsize(cp) / (1024 * 1024)
                        creator.add_item(MapItem(
                            f"routing-data/graph-geoms-chunk-{i:04d}.bin",
                            f"Routing Geoms Chunk {i}",
                            "application/octet-stream",
                            cp,
                            compress=cp_mb < 200,
                        ))
                else:
                    compress_geoms = geoms_mb < 200
                    print(f"    Adding routing geoms companion "
                          f"({geoms_mb:.1f} MB, "
                          f"{'compressed' if compress_geoms else 'raw (PWA-compat)'})...")
                    creator.add_item(MapItem(
                        "routing-data/graph-geoms.bin",
                        "Routing Graph Geoms",
                        "application/octet-stream",
                        routing_graph_geoms_path,
                        compress=compress_geoms,
                    ))

        # Build location index for search feature enrichment
        loc_lookup = None
        if mbtiles_path:
            print("    Building location index for search results...")
            loc_lookup = build_location_index(mbtiles_path)

        # Add search features — stream from disk if path provided, else use in-memory list
        if search_features_path and os.path.isfile(search_features_path) and os.path.getsize(search_features_path) > 0:
            import tempfile
            chunk_tmp = tempfile.mkdtemp(prefix="streetzim_chunks_")
            xapian_types = {"place", "airport", "park", "peak", "water"}

            # Pass 1: stream JSONL -> per-prefix chunk files + xapian file
            chunk_counts = {}
            chunk_fds = {}  # prefix -> open file handle
            xapian_path = os.path.join(chunk_tmp, "_xapian.jsonl")
            total_features = 0
            xapian_count = 0

            # Normalize (lowercase + ASCII-fold) so search matches across
            # accented / diacritic variants: "Café" ↔ "cafe", "São" ↔ "sao".
            import unicodedata
            def _norm(s):
                s = unicodedata.normalize("NFKD", s)
                s = "".join(c for c in s if not unicodedata.combining(c))
                return s.lower()

            def _prefix_key(word):
                """Chunk key for a word's first char.

                Latin-leading names get the same 2-char ASCII-alnum
                prefix as before (``"to"`` for Tokyo in rōmaji, ``"_p"``
                for "_private"). Non-ASCII first chars are bucketed by
                their single Unicode codepoint in lowercase hex
                (``"u6771"`` for 東, ``"u43f"`` for п) — previously every
                CJK/Cyrillic/Arabic/Thai record collapsed into one
                ``__.json`` chunk, 350 MB on Japan, 230 MB on Iran,
                crashing Kiwix Desktop on "find". Now each distinct
                leading codepoint gets its own bucket.

                Callers (JS viewer ``keyFor``, Swift ``normalizePrefix``)
                implement the same rule — if this changes, update them
                in lockstep or lookups desync.
                """
                pw = _norm(word).replace(" ", "_")
                if not pw:
                    return "__"
                c0 = pw[0]
                # Non-ASCII → codepoint hex bucket
                if not c0.isascii():
                    return "u" + format(ord(c0), "x")
                # ASCII path mirrors the original rule: first char is
                # alnum or '_' (kept), anything else becomes '_'.
                def _ascii_norm(ch: str) -> str:
                    return ch if ch.isalnum() or ch == "_" else "_"
                k0 = _ascii_norm(c0)
                if len(pw) >= 2:
                    c1 = pw[1]
                    # If the 2nd char is non-ASCII, collapse it to ``_`` —
                    # the bucket is keyed by c0 alone in that case.
                    k1 = _ascii_norm(c1) if c1.isascii() else "_"
                else:
                    k1 = "_"
                return k0 + k1

            # Word splitter: any run of non-alnum (unicode-aware) ends a word.
            # Gives us each term in the name so "Washington National Cathedral"
            # gets indexed under each of "wa", "na", "ca" (not just "wa").
            # Without this, typing "cathedral" in a search box will miss it
            # because the query prefix is "ca" but the entry lives under "wa".
            import re as _re
            _word_re = _re.compile(r"[^\W_]+", _re.UNICODE)

            def _prefixes_for(name):
                """Set of 2-char prefix keys this name should be indexed under."""
                keys = set()
                # First-2-of-whole-name (keeps backwards-compat for callers
                # that computed it the old way: "45 Broadway" → "45").
                keys.add(_prefix_key(name[:2]))
                # Plus one key per word — this is what unlocks substring search.
                for m in _word_re.findall(name):
                    if len(m) >= 2:
                        keys.add(_prefix_key(m))
                return keys

            # Per-type counts for streetzim-meta.json, plus a parallel set of
            # chunk files keyed by OSM top-level `type` (category-index).
            # Category-index is a cheap O(1)-per-query alternative to
            # near_places scanning every search-data chunk (mcpzim does that
            # linearly today; see STREETZIM_CONSUMPTION.md).
            type_counts = {}
            wiki_fields_added = 0
            cat_chunk_fds = {}
            cat_chunk_counts = {}
            cat_dir = os.path.join(chunk_tmp, "categories")
            os.makedirs(cat_dir, exist_ok=True)
            def _cat_slug(t):
                s = "".join(c if c.isascii() and (c.isalnum() or c == "_") else "_" for c in t.lower())
                return s[:40] or "_"

            print("    Streaming search features from disk...", flush=True)
            with open(xapian_path, "w") as xf:
                with open(search_features_path, "r") as sf:
                    for line in sf:
                        feat = json.loads(line)
                        total_features += 1
                        t = feat.get("type", "")
                        type_counts[t] = type_counts.get(t, 0) + 1

                        # Enrich with location (state, country) if missing
                        if loc_lookup and not feat.get("location"):
                            feat["location"] = loc_lookup(feat["lat"], feat["lon"])

                        # Enrich with wiki cross-refs if this POI has matching
                        # (name, coord) in the OSM-tag lookup built from the PBF.
                        wiki = None
                        if wiki_cross_refs:
                            wiki_key = (
                                feat["name"].lower(),
                                int(round(feat["lat"] * 1e4)),
                                int(round(feat["lon"] * 1e4)),
                            )
                            wiki = wiki_cross_refs.get(wiki_key)
                            if wiki:
                                wiki_fields_added += 1

                        # Canonical record shape consumed by mcpzim:
                        #   n, t (type), s (subtype), a (lat), o (lon), l (location)
                        # Optional additions (safe to forward through their parser):
                        #   w  = wikipedia tag value(s)  (OSM format, e.g. "en:Lincoln_Memorial")
                        #   q  = wikidata Q-ID
                        #   Overture-places enrichment (set by merge_overture_places;
                        #   empty on non-POI rows): ws = website, p = phone, soc = socials,
                        #   brand = brand primary name, wd = brand Wikidata Q-ID,
                        #   cat = normalized category, source = "overture" for Pass-2 adds.
                        rec = {"n": feat["name"], "t": t, "s": feat.get("subtype", ""),
                               "a": feat["lat"], "o": feat["lon"], "l": feat.get("location", "")}
                        for ov_key in ("ws", "p", "soc", "brand", "wd", "cat", "source"):
                            v = feat.get(ov_key)
                            if v:
                                rec[ov_key] = v
                        if wiki:
                            if wiki.get("wikipedia"):
                                rec["w"] = wiki["wikipedia"]
                            if wiki.get("wikidata"):
                                rec["q"] = wiki["wikidata"]
                        entry = json.dumps(rec, separators=(",", ":")) + "\n"

                        # Write abbreviated entry to per-prefix chunk file(s).
                        # Index under each word's prefix — duplicates entries
                        # across 1–4 chunks (avg ~2×) but enables substring
                        # hits like "cathedral" → "Washington National Cathedral".
                        for prefix in _prefixes_for(feat["name"]):
                            if prefix not in chunk_fds:
                                chunk_fds[prefix] = open(
                                    os.path.join(chunk_tmp, f"{prefix}.jsonl"), "w")
                                chunk_counts[prefix] = 0
                            chunk_fds[prefix].write(entry)
                            chunk_counts[prefix] += 1

                        # Also write to the category-index (one file per type).
                        # Same record shape so downstream consumers stay trivial.
                        if t:
                            cat_slug = _cat_slug(t)
                            if cat_slug not in cat_chunk_fds:
                                cat_chunk_fds[cat_slug] = open(
                                    os.path.join(cat_dir, f"{cat_slug}.jsonl"), "w")
                                cat_chunk_counts[cat_slug] = 0
                            cat_chunk_fds[cat_slug].write(entry)
                            cat_chunk_counts[cat_slug] += 1

                        # Collect xapian-eligible features separately
                        if feat["type"] in xapian_types:
                            xf.write(line)
                            xapian_count += 1

                        if total_features % 500_000 == 0:
                            print(f"\r    Bucketed {total_features} features into {len(chunk_counts)} chunks...", end="", flush=True)
            for fd in cat_chunk_fds.values():
                fd.close()
            del cat_chunk_fds
            if wiki_fields_added:
                print(f"    Enriched {wiki_fields_added} entries with wiki cross-refs")

            # Close all chunk file handles
            for fd in chunk_fds.values():
                fd.close()
            del chunk_fds

            print(f"\r    Bucketed {total_features} features into {len(chunk_counts)} chunks, {xapian_count} xapian entries", flush=True)

            # Pass 2: read each chunk file, serialize, and emit. When
            # `split_hot_search_chunks_mb` > 0, fan out any chunk whose
            # JSON exceeds that threshold into 16 FNV-1a sub-buckets
            # (`{prefix}-{0..f}.json`). The manifest then records the
            # fan-out in ``sub_chunks`` so clients (viewer, Swift) know
            # which queries to spread across sub-files.
            #
            # Accumulate manifest mutations during the emission loop
            # (instead of writing the manifest up-front) so split vs
            # passthrough decisions are reflected in the final manifest.
            hot_split_bytes = (split_hot_search_chunks_mb * 1024 * 1024
                               if split_hot_search_chunks_mb > 0 else None)
            hot_split_N = 16
            manifest_chunks: dict[str, int] = {}
            manifest_sub_chunks: dict[str, list[str]] = {}
            split_total = 0

            chunks_added = 0
            for prefix in sorted(chunk_counts):
                chunk_path = os.path.join(chunk_tmp, f"{prefix}.jsonl")
                entries = []
                with open(chunk_path, "r") as cf:
                    for cline in cf:
                        entries.append(json.loads(cline))
                os.unlink(chunk_path)

                chunk_bytes = json.dumps(entries, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8")
                if hot_split_bytes and len(chunk_bytes) > hot_split_bytes:
                    # Oversized — fan out. Also handle multi-level
                    # recursion: if a sub-bucket is STILL too big after
                    # the first split (high-cardinality hotspots like
                    # Texas "st" splitting into "st-b" at 37 MB), split
                    # that sub-bucket too. Max depth 2 in practice.
                    sub_chunks = _split_big_search_chunk(prefix, entries, hot_split_N)
                    sub_prefix_list = []
                    for sub_prefix, sub_bytes in sub_chunks:
                        if len(sub_bytes) > hot_split_bytes:
                            # Recursive split.
                            sub_records = json.loads(sub_bytes.decode("utf-8"))
                            sub2 = _split_big_search_chunk(sub_prefix, sub_records, hot_split_N)
                            sub2_prefixes = []
                            for sp2, sb2 in sub2:
                                creator.add_item(MapItem(
                                    f"search-data/{sp2}.json",
                                    f"Search chunk {sp2}",
                                    "application/json",
                                    sb2,
                                ))
                                recs2 = json.loads(sb2.decode("utf-8"))
                                manifest_chunks[sp2] = len(recs2)
                                sub2_prefixes.append(sp2)
                                split_total += 1
                            manifest_sub_chunks[sub_prefix] = sub2_prefixes
                            # The parent sub_prefix itself is NOT an
                            # entry — only its sub-sub-chunks are.
                        else:
                            creator.add_item(MapItem(
                                f"search-data/{sub_prefix}.json",
                                f"Search chunk {sub_prefix}",
                                "application/json",
                                sub_bytes,
                            ))
                            recs = json.loads(sub_bytes.decode("utf-8"))
                            manifest_chunks[sub_prefix] = len(recs)
                            sub_prefix_list.append(sub_prefix)
                            split_total += 1
                    manifest_sub_chunks[prefix] = sub_prefix_list
                else:
                    creator.add_item(MapItem(
                        f"search-data/{prefix}.json",
                        f"Search chunk {prefix}",
                        "application/json",
                        chunk_bytes,
                    ))
                    manifest_chunks[prefix] = len(entries)
                chunks_added += 1
                if chunks_added % 100 == 0:
                    print(f"\r    Added {chunks_added}/{len(chunk_counts)} search chunks...", end="", flush=True)

            # Emit the manifest AFTER the emission loop so it reflects
            # every split decision.
            manifest_dict: dict = {"total": total_features,
                                   "chunks": manifest_chunks}
            if manifest_sub_chunks:
                manifest_dict["sub_chunks"] = manifest_sub_chunks
            creator.add_item(MapItem(
                "search-data/manifest.json", "Search Manifest",
                "application/json",
                json.dumps(manifest_dict, separators=(",", ":")).encode("utf-8"),
            ))

            if hot_split_bytes:
                print(f"\r    Added {chunks_added} chunks; "
                      f"{len(manifest_sub_chunks)} hot prefix(es) split → "
                      f"{split_total} sub-chunks "
                      f"({total_features} features)          ",
                      flush=True)
            else:
                print(f"\r    Added {chunks_added} search chunks "
                      f"({total_features} features)          ",
                      flush=True)

            # Pass 2b: category-index files (optional, mirrors search-data
            # chunks but keyed by OSM top-level `type`). Lets consumers answer
            # "all museums in this region" with one file read instead of a
            # linear scan. Same canonical record shape as search-data chunks.
            if cat_chunk_counts:
                cat_total_records = 0
                records_by_cat: dict[str, list] = {}
                for cat_slug in sorted(cat_chunk_counts):
                    cat_path = os.path.join(cat_dir, f"{cat_slug}.jsonl")
                    entries = []
                    with open(cat_path, "r") as cf:
                        for cline in cf:
                            entries.append(json.loads(cline))
                    os.unlink(cat_path)
                    chunk_json = json.dumps(entries, separators=(",", ":"))
                    creator.add_item(MapItem(
                        f"category-index/{cat_slug}.json",
                        f"Category index {cat_slug}",
                        "application/json",
                        chunk_json.encode("utf-8"),
                    ))
                    cat_total_records += len(entries)
                    if split_find_chips and cat_slug in ("poi", "park"):
                        records_by_cat[cat_slug] = entries
                cat_manifest = {k: cat_chunk_counts[k] for k in sorted(cat_chunk_counts)}
                manifest_payload = {"total": cat_total_records,
                                    "categories": cat_manifest}
                if split_find_chips and records_by_cat:
                    from cloud.chip_rules import CHIP_RULES, split_records_by_chip
                    by_chip = split_records_by_chip(records_by_cat)
                    chips_manifest: dict = {}
                    for chip in CHIP_RULES:
                        recs = by_chip.get(chip.id, [])
                        blob = json.dumps(recs, separators=(",", ":"))
                        blob_bytes = blob.encode("utf-8")
                        creator.add_item(MapItem(
                            f"category-index/chip-{chip.id}.json",
                            f"Find chip {chip.label}",
                            "application/json",
                            blob_bytes,
                        ))
                        chips_manifest[chip.id] = {
                            "label": chip.label,
                            "count": len(recs),
                            "bytes": len(blob_bytes),
                        }
                    manifest_payload["chips"] = chips_manifest
                    print(f"    Added {len(chips_manifest)} chip files "
                          f"({sum(c['count'] for c in chips_manifest.values())} records)")
                creator.add_item(MapItem(
                    "category-index/manifest.json",
                    "Category Index Manifest",
                    "application/json",
                    json.dumps(manifest_payload, separators=(",", ":")).encode("utf-8"),
                ))
                print(f"    Added category-index: "
                      f"{len(cat_chunk_counts)} categories, {cat_total_records} records")

            # streetzim-meta.json — ZIM-level summary for offline LLM agents.
            # Shape matches the mcpzim consumption contract (see
            # docs/STREETZIM_CONSUMPTION.md) so they can expose a `zim_info`
            # tool without inferring capabilities from filenames.
            routing_stats = {}
            if routing_graph_path and os.path.isfile(routing_graph_path):
                try:
                    import struct as _struct
                    with open(routing_graph_path, "rb") as _rf:
                        _magic = _rf.read(4)
                        _hdr = _struct.unpack("<7I", _rf.read(28))
                        if _magic == b"SZRG":
                            routing_stats = {
                                "version": int(_hdr[0]),
                                "nodes": int(_hdr[1]),
                                "edges": int(_hdr[2]),
                                "geoms": int(_hdr[3]),
                            }
                except Exception:
                    pass

            meta = {
                "name": map_config.get("name", name),
                "buildDate": _time.strftime("%Y-%m-%d"),
                "hasRouting": bool(routing_graph_path),
                "hasSatellite": bool(map_config.get("hasSatellite")),
                "hasTerrain": bool(map_config.get("hasTerrain")),
                "hasWikidata": bool(map_config.get("hasWikidata")),
                "hasOvertureAddresses": bool(map_config.get("hasOvertureAddresses")),
                "hasAddresses": address_count > 0,
                "counts": {
                    "total": total_features,
                    "addresses": int(address_count),
                    "byType": type_counts,
                    "wikiCrossRefs": int(wiki_fields_added),
                    "wikidataEntries": int(len(wikidata_data) if wikidata_data else 0),
                },
            }
            if bbox:
                meta["bbox"] = list(bbox)  # [minLon, minLat, maxLon, maxLat]
            if routing_stats:
                meta["routingGraph"] = routing_stats
            meta["wikipediaLang"] = "en"  # we emit OSM-raw `<lang>:<Title>`; en is the dominant edition we reference
            creator.add_item(MapItem(
                "streetzim-meta.json", "StreetZim Meta", "application/json",
                json.dumps(meta, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8"),
            ))
            print(f"    Added streetzim-meta.json (name={meta['name']}, "
                  f"types={len(type_counts)}, addresses={address_count})")

            # Overture dataset credits. Written when --overture-addresses
            # was used so the viewer's Sources panel (and the ZIM-level
            # License metadata) can point readers at the actual upstream
            # feeds the address enrichment came from — OpenAddresses
            # contributors, national/regional registers, etc.
            if overture_sources:
                themes = overture_themes or ["addresses"]
                theme_label = " + ".join(themes)
                themes_phrase = (
                    "Address data is derived from the Overture addresses theme"
                    if themes == ["addresses"] else
                    "Place info (POIs, websites, phones, socials, brand,"
                    " categories) is derived from the Overture places theme"
                    if themes == ["places"] else
                    "Address + place info (POIs, websites, phones, socials,"
                    " brand, categories) are derived from the Overture"
                    " addresses + places themes"
                )
                overture_doc = {
                    "release": "2026-04-15.0",
                    "themes": themes,
                    "attribution": (
                        "© OpenStreetMap contributors and Overture Maps "
                        "Foundation (overturemaps.org). "
                        f"{themes_phrase}; "
                        "credits for each underlying dataset follow."
                    ),
                    "datasets": list(overture_sources),
                    "canonicalCredits": "https://docs.overturemaps.org/attribution/",
                }
                creator.add_item(MapItem(
                    "overture-sources.json", "Overture Dataset Credits",
                    "application/json",
                    json.dumps(overture_doc, separators=(",", ":"),
                               ensure_ascii=False).encode("utf-8"),
                ))
                print(f"    Added overture-sources.json "
                      f"({len(overture_sources)} upstream datasets)")

            # Pass 3: stream xapian file -> HTML redirect pages
            print(f"    Adding {xapian_count} Xapian search pages (of {total_features} total)...", flush=True)
            xapian_start = time.time()
            i = 0
            with open(xapian_path, "r") as xf:
                for line in xf:
                    feat = json.loads(line)
                    slug = feat["name"].lower()
                    slug = "".join(c if c.isalnum() or c in "-_ " else "" for c in slug)
                    slug = slug.strip().replace(" ", "-")[:80]
                    slug = f"{slug}-{i}"

                    zoom = {"place": 14, "airport": 14, "peak": 15, "park": 15,
                            "water": 14, "poi": 17, "street": 16}.get(feat["type"], 15)
                    map_hash = f"map={zoom}/{feat['lat']}/{feat['lon']}"
                    # Prefer Overture's normalized category for display
                    # when present (falls back to OMT subtype / OSM type).
                    kind_raw = feat.get("cat") or feat.get("subtype") or feat["type"]
                    label = kind_raw.replace("_", " ").title()
                    enrich = {k: feat[k] for k in ("ws", "p", "soc", "brand", "wd")
                              if feat.get(k)}
                    page_html = search_detail_html(
                        feat["name"], label,
                        feat["lat"], feat["lon"], map_hash, enrich=enrich,
                    )
                    creator.add_item(MapItem(
                        f"search/{slug}.html",
                        feat["name"],
                        "text/html",
                        page_html.encode("utf-8"),
                        is_front=False,
                    ))

                    i += 1
                    if i % 2000 == 0:
                        elapsed = time.time() - xapian_start
                        rate = i / elapsed if elapsed > 0 else 0
                        remaining = (xapian_count - i) / rate if rate > 0 else 0
                        print(f"\r    Added {i}/{xapian_count} search pages ({rate:.0f}/s, ~{remaining/60:.0f}m left)...", end="", flush=True)

            os.unlink(xapian_path)
            print(f"\r    Added {i} search pages in {time.time() - xapian_start:.0f}s                ", flush=True)

            # Clean up chunk temp dir
            try:
                os.rmdir(chunk_tmp)
            except OSError:
                pass

        elif search_features:
            print(f"    Adding {len(search_features)} search entries...")

            # Enrich with location if available
            if loc_lookup:
                for f in search_features:
                    if not f.get("location"):
                        f["location"] = loc_lookup(f["lat"], f["lon"])

            # Build chunked search index for scalable on-demand loading.
            from collections import defaultdict
            chunks = defaultdict(list)
            for f in search_features:
                prefix = f["name"].lower()[:2].replace(" ", "_")
                prefix = "".join(c if c.isalnum() or c == "_" else "_" for c in prefix)
                if not prefix:
                    prefix = "__"
                prefix = prefix[:2].ljust(2, "_")
                chunks[prefix].append(
                    {"n": f["name"], "t": f["type"], "s": f.get("subtype", ""),
                     "a": f["lat"], "o": f["lon"], "l": f.get("location", "")}
                )

            manifest = {k: len(v) for k, v in sorted(chunks.items())}
            total_features = sum(manifest.values())
            creator.add_item(MapItem(
                "search-data/manifest.json", "Search Manifest", "application/json",
                json.dumps({"total": total_features, "chunks": manifest},
                           separators=(",", ":")).encode("utf-8"),
            ))

            for prefix, entries in sorted(chunks.items()):
                chunk_json = json.dumps(entries, separators=(",", ":"))
                creator.add_item(MapItem(
                    f"search-data/{prefix}.json",
                    f"Search chunk {prefix}",
                    "application/json",
                    chunk_json.encode("utf-8"),
                ))

            print(f"    Added {len(chunks)} search chunks ({total_features} features)")

            xapian_types = {"place", "airport", "park", "peak", "water"}
            xapian_features = [f for f in search_features if f["type"] in xapian_types]
            print(f"    Adding {len(xapian_features)} Xapian search pages (of {len(search_features)} total)...", flush=True)

            xapian_start = time.time()
            for i, feat in enumerate(xapian_features):
                slug = feat["name"].lower()
                slug = "".join(c if c.isalnum() or c in "-_ " else "" for c in slug)
                slug = slug.strip().replace(" ", "-")[:80]
                slug = f"{slug}-{i}"

                zoom = {"place": 14, "airport": 14, "peak": 15, "park": 15,
                        "water": 14, "poi": 17, "street": 16}.get(feat["type"], 15)
                map_hash = f"map={zoom}/{feat['lat']}/{feat['lon']}"
                kind_raw = feat.get("cat") or feat.get("subtype") or feat["type"]
                label = kind_raw.replace("_", " ").title()
                enrich = {k: feat[k] for k in ("ws", "p", "soc", "brand", "wd")
                          if feat.get(k)}
                page_html = search_detail_html(
                    feat["name"], label,
                    feat["lat"], feat["lon"], map_hash, enrich=enrich,
                )
                creator.add_item(MapItem(
                    f"search/{slug}.html",
                    feat["name"],
                    "text/html",
                    page_html.encode("utf-8"),
                    is_front=False,
                ))

                if (i + 1) % 2000 == 0:
                    elapsed = time.time() - xapian_start
                    rate = (i + 1) / elapsed if elapsed > 0 else 0
                    remaining = (len(xapian_features) - i - 1) / rate if rate > 0 else 0
                    print(f"\r    Added {i + 1}/{len(xapian_features)} search pages ({rate:.0f}/s, ~{remaining/60:.0f}m left)...", end="", flush=True)

            print(f"\r    Added {len(xapian_features)} search pages in {time.time() - xapian_start:.0f}s                ", flush=True)

        print("    Finalizing ZIM (ZSTD compression + Xapian indexing)...", flush=True)
        finalize_start = time.time()

    finalize_elapsed = time.time() - finalize_start
    print(f"    Finalized in {finalize_elapsed:.0f}s", flush=True)
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"    ZIM file created: {size_mb:.1f} MB")


def parse_bbox(bbox_str):
    """Parse a bbox string 'minlon,minlat,maxlon,maxlat' into a list of floats."""
    parts = [float(x.strip()) for x in bbox_str.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Invalid bbox format: {bbox_str}. Expected: minlon,minlat,maxlon,maxlat")
    return parts


def get_center_and_zoom(bbox):
    """Calculate center point and initial zoom from a bounding box."""
    minlon, minlat, maxlon, maxlat = bbox
    center_lon = (minlon + maxlon) / 2
    center_lat = (minlat + maxlat) / 2

    # Rough zoom level based on extent
    lon_extent = maxlon - minlon
    lat_extent = maxlat - minlat
    extent = max(lon_extent, lat_extent)
    if extent > 50:
        zoom = 4
    elif extent > 10:
        zoom = 6
    elif extent > 5:
        zoom = 7
    elif extent > 2:
        zoom = 8
    elif extent > 1:
        zoom = 9
    elif extent > 0.5:
        zoom = 10
    elif extent > 0.2:
        zoom = 11
    elif extent > 0.1:
        zoom = 12
    else:
        zoom = 13

    return [center_lon, center_lat], zoom


# Well-known areas with their Geofabrik paths and bounding boxes
KNOWN_AREAS = {
    "dc": {
        "geofabrik": "north-america/us/district-of-columbia",
        "bbox": "-77.12,38.79,-76.91,38.99",
        "name": "Washington, D.C.",
    },
    "district-of-columbia": {
        "geofabrik": "north-america/us/district-of-columbia",
        "bbox": "-77.12,38.79,-76.91,38.99",
        "name": "Washington, D.C.",
    },
    "austin": {
        "geofabrik": "north-america/us/texas",
        "bbox": "-97.95,30.10,-97.55,30.50",
        "name": "Austin, TX",
    },
    "san-francisco": {
        "geofabrik": "north-america/us/california",
        "bbox": "-122.52,37.70,-122.36,37.82",
        "name": "San Francisco, CA",
    },
    "manhattan": {
        "geofabrik": "north-america/us/new-york",
        "bbox": "-74.03,40.70,-73.91,40.88",
        "name": "Manhattan, NY",
    },
    "portland": {
        "geofabrik": "north-america/us/oregon",
        "bbox": "-122.84,45.43,-122.47,45.60",
        "name": "Portland, OR",
    },
    "liechtenstein": {
        "geofabrik": "europe/liechtenstein",
        "bbox": "9.47,47.04,9.64,47.27",
        "name": "Liechtenstein",
    },
    "monaco": {
        "geofabrik": "europe/monaco",
        "bbox": "7.40,43.72,7.44,43.76",
        "name": "Monaco",
    },
    "california": {
        "geofabrik": "north-america/us/california",
        "bbox": "-124.48,32.53,-114.13,42.01",
        "name": "California",
    },
    "colorado": {
        "geofabrik": "north-america/us/colorado",
        "bbox": "-109.06,36.99,-102.04,41.00",
        "name": "Colorado",
    },
    "virginia": {
        "geofabrik": "north-america/us/virginia",
        "bbox": "-83.68,36.54,-75.17,39.47",
        "name": "Virginia",
    },
    "iran": {
        "geofabrik": "asia/iran",
        "bbox": "44.0,25.0,63.5,39.8",
        "name": "Iran",
    },
    "united-states": {
        "geofabrik": "north-america/us",
        "bbox": "-125.0,24.4,-66.9,49.4",
        "name": "United States",
    },
    "us": {
        "geofabrik": "north-america/us",
        "bbox": "-125.0,24.4,-66.9,49.4",
        "name": "United States",
    },
}


def main():
    parser = argparse.ArgumentParser(
        description="Create a ZIM file with offline OpenStreetMap viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use a well-known area (downloads automatically)
  python3 create_osm_zim.py --area dc

  # Specify Geofabrik path for a state/country
  python3 create_osm_zim.py --geofabrik europe/liechtenstein --name "Liechtenstein"

  # Use custom bbox with a Geofabrik region
  python3 create_osm_zim.py --geofabrik north-america/us/texas \\
      --bbox "-97.95,30.10,-97.55,30.50" --name "Austin, TX"

  # Use a local PBF file
  python3 create_osm_zim.py --pbf mydata.osm.pbf --name "My Area" \\
      --bbox "-97.9,30.1,-97.5,30.5"

Known areas: """ + ", ".join(sorted(KNOWN_AREAS.keys())),
    )

    parser.add_argument(
        "--zim-builder",
        choices=["python", "rust"],
        default="python",
        help=(
            "ZIM emit backend. 'python' (default) uses libzim/python-libzim "
            "as before. 'rust' shells out to streetzim-pack (zimru-backed); "
            "supports per-item compress flags so routing-graph chunks land "
            "in raw clusters even when tiles/HTML stay zstd."
        ),
    )
    parser.add_argument("--area", help="Well-known area name (see list above)")
    parser.add_argument("--geofabrik", help="Geofabrik download path (e.g., europe/liechtenstein)")
    parser.add_argument("--pbf", help="Path to local OSM PBF file")
    parser.add_argument("--bbox", help="Bounding box: minlon,minlat,maxlon,maxlat")
    parser.add_argument("--name", help="Name for the map (shown in Kiwix)")
    parser.add_argument("--output", "-o", help="Output ZIM file path")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary files")
    parser.add_argument("--max-zoom", type=int, default=14, help="Maximum zoom level (default: 14)")
    parser.add_argument("--cluster-size", type=int, default=2048,
                        help="ZIM cluster size in KiB (default: 2048 = 2 MiB)")
    parser.add_argument("--fast", action="store_true",
                        help="Trade RAM for speed in tilemaker (needs 32+ GB RAM)")
    parser.add_argument("--store", metavar="PATH",
                        help="Path for tilemaker on-disk temp storage (reduces RAM usage)")
    parser.add_argument("--mbtiles", metavar="PATH",
                        help="Skip tilemaker and use existing MBTiles file")
    parser.add_argument("--satellite", action="store_true",
                        help="Include Sentinel-2 Cloudless satellite imagery tiles")
    parser.add_argument("--satellite-zoom", type=int, default=None,
                        help="Max zoom for satellite tiles (default: same as --max-zoom)")
    parser.add_argument("--satellite-download-zoom", type=int, default=None,
                        help="Max zoom to DOWNLOAD new satellite tiles (default: same as --satellite-zoom). "
                             "Cached tiles above this zoom are still included in the ZIM.")
    parser.add_argument("--satellite-format", choices=["webp", "avif"], default="avif",
                        help="Satellite tile image format (default: avif)")
    parser.add_argument("--satellite-quality", type=int, default=None,
                        help="Satellite tile compression quality (default: 40 for avif, 65 for webp)")
    parser.add_argument("--satellite-tile-size", type=int, choices=[256, 512], default=256,
                        help="Satellite tile pixel size (default: 256; 512 stitches 4 source tiles)")
    parser.add_argument("--terrain", action="store_true",
                        help="Include Copernicus GLO-30 terrain tiles for 3D/hillshade")
    parser.add_argument("--terrain-zoom", type=int, default=12,
                        help="Max zoom for terrain tiles (default: 12)")
    parser.add_argument("--terrain-dir", metavar="PATH", default=None,
                        help="Directory for terrain tile cache (default: terrain_cache/)")
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of ZIM compression workers (default: CPU_count/2)")
    parser.add_argument("--wikidata", action="store_true",
                        help="Include Wikidata info (population, description, etc.) for places/POIs")
    parser.add_argument("--wikidata-cache", metavar="PATH", default=None,
                        help="Wikidata cache directory (default: wikidata_cache/)")
    parser.add_argument("--wikidata-no-extracts", action="store_true",
                        help="Skip Wikipedia text extracts (smaller cache, faster)")
    parser.add_argument("--search-cache", metavar="PATH", default=None,
                        help="Use pre-built search features JSONL instead of extracting from tiles. "
                             "If bbox is set, features are filtered to the bounding box.")
    parser.add_argument("--skip-address-extract", action="store_true",
                        help="Skip extract_addresses_pbf and merge_overture_{addresses,places}. "
                             "Use when --search-cache already contains the address records and "
                             "overture enrichment from a prior run that crashed in a later phase.")
    parser.add_argument("--routing", action="store_true",
                        help="Include offline routing graph for turn-by-turn directions")
    parser.add_argument("--split-graph", action="store_true",
                        help="Emit SZRG v5 split routing graph (main graph.bin "
                             "+ companion graph-geoms.bin) so the PWA can "
                             "defer geom loading. Opt-in: default stays on "
                             "v4 inline for Kiwix Desktop / mcpzim compat.")
    parser.add_argument("--chunk-graph-mb", type=int, default=0, metavar="N",
                        help="Split the routing graph file(s) into N-MB chunks "
                             "when packaging (each chunk becomes its own ZIM "
                             "entry). Intended for continent-scale ZIMs whose "
                             "graph.bin would land in a single libzim cluster "
                             "> 500 MB — the PWA's fzstd port chokes there. "
                             "Default 0 = no chunking. 200 is a safe starting "
                             "value; it keeps each cluster well under the limit.")
    parser.add_argument("--split-hot-search-chunks-mb", type=int, default=0,
                        metavar="N",
                        help="Fan out any search-data chunk whose JSON "
                             "exceeds N MB into 16 FNV-1a-hashed sub-"
                             "buckets (`{prefix}-{0..f}.json`). The "
                             "manifest gains `sub_chunks` so clients "
                             "know to spread queries across sub-files. "
                             "Essential for region-heavy prefixes like "
                             "Japan's u5927 (大) at 514 MB; 10 is the "
                             "target to keep each chunk fetch fast on "
                             "iOS Safari. Default 0 = off.")
    parser.add_argument("--low-zoom-world-vrt", metavar="PATH", default=None,
                        help="Use a world-coverage DEM VRT (e.g. "
                             "terrain_cache/dem_sources/world_dem_32k.tif) "
                             "for z=0-7 terrain tiles instead of the "
                             "region-bbox VRT. Prevents the bbox-edge "
                             "stripe bug where z=0-7 tiles that extend "
                             "past the bbox get zero-fill outside the "
                             "region. z=8+ still use the regional VRT "
                             "(fine-grained, no stripe risk). Default "
                             "None = regional VRT everywhere (matches "
                             "pre-2026-04-24 behavior).")
    parser.add_argument("--overture-addresses", metavar="PARQUET",
                        help="Merge Overture Maps address records from a parquet extract. "
                             "Use download_overture_data.py to produce the parquet first. "
                             "Dedups against the OSM address pass; see docs/overture-matching.md.")
    parser.add_argument("--overture-places", metavar="PARQUET",
                        help="Enrich OSM POIs (and add new ones) from Overture Maps "
                             "places theme: websites, phones, socials, brand, and normalized "
                             "categories (museum/hotel/…) instead of OMT's noisy class buckets. "
                             "Run download_overture_data.py places --out …parquet first.")
    parser.add_argument("--split-find-chips", action="store_true",
                        help="Pre-slice category-index/{poi,park}.json by Find-page "
                             "chip at build time. Emits one `category-index/chip-{id}.json` "
                             "per chip plus chip entries in the manifest. places.html "
                             "loads only the chosen chip file (~MB) instead of the "
                             "full poi.json (up to 1 GB on Japan), which OOM'd Chrome "
                             "and iOS WebViews. Source of truth: cloud/chip_rules.py.")

    args = parser.parse_args()

    # Resolve area configuration
    geofabrik_path = args.geofabrik
    bbox_str = args.bbox.strip() if args.bbox else args.bbox
    name = args.name
    pbf_path = args.pbf

    if args.area:
        area_key = args.area.lower().replace(" ", "-")
        if area_key not in KNOWN_AREAS:
            print(f"Unknown area: {args.area}")
            print(f"Known areas: {', '.join(sorted(KNOWN_AREAS.keys()))}")
            sys.exit(1)
        area = KNOWN_AREAS[area_key]
        geofabrik_path = geofabrik_path or area["geofabrik"]
        bbox_str = bbox_str or area.get("bbox")
        name = name or area["name"]

    if not pbf_path and not geofabrik_path and not args.mbtiles:
        print("Error: Must specify --area, --geofabrik, --pbf, or --mbtiles")
        parser.print_help()
        sys.exit(1)

    if not name:
        name = args.area or args.geofabrik or "OpenStreetMap"

    # Set output path — dated by default (e.g. osm-europe-2026-04.zim)
    import time as _time
    safe_name = name.lower().replace(" ", "-").replace(",", "").replace(".", "")
    date_suffix = _time.strftime("%Y-%m-%d")
    output_path = args.output or f"osm-{safe_name}-{date_suffix}.zim"

    # Satellite options
    include_satellite = args.satellite
    satellite_max_zoom = args.satellite_zoom or args.max_zoom
    satellite_download_zoom = args.satellite_download_zoom or satellite_max_zoom
    satellite_format = args.satellite_format
    satellite_quality = args.satellite_quality
    satellite_tile_size = args.satellite_tile_size
    if satellite_quality is None:
        satellite_quality = 40 if satellite_format == "avif" else 65

    # Terrain options
    include_terrain = args.terrain
    terrain_max_zoom = args.terrain_zoom

    # Wikidata options
    include_wikidata = args.wikidata
    wikidata_cache_dir = args.wikidata_cache

    # Routing options
    include_routing = args.routing

    total_steps = 6 + (1 if include_satellite else 0) + (1 if include_terrain else 0) + (1 if include_wikidata else 0) + (1 if include_routing else 0)

    print(f"=== Creating Offline OSM ZIM: {name} ===")
    if include_satellite:
        sat_desc = f"{satellite_format} q{satellite_quality} {satellite_tile_size}px"
        print(f"  Including Sentinel-2 satellite imagery (z0-{satellite_max_zoom}, {sat_desc})")
    if include_terrain:
        print(f"  Including Copernicus GLO-30 terrain (z0-{terrain_max_zoom})")
    if include_wikidata:
        print(f"  Including Wikidata info for places and POIs")
    if include_routing:
        print(f"  Including offline routing graph")
    print()
    log_viewer_freshness()

    # Create temp directory
    tmpdir = tempfile.mkdtemp(prefix="osm_zim_")
    try:
        if args.mbtiles:
            # Skip OSM download and tilemaker — reuse existing MBTiles
            print(f"[1/{total_steps}] Skipping OSM data (using existing MBTiles)...")
            print()
            print(f"[2/{total_steps}] Reusing existing MBTiles...")
            mbtiles_path = args.mbtiles
            print(f"  Using: {mbtiles_path} ({os.path.getsize(mbtiles_path) / 1e9:.1f} GB)")
        else:
            # Step 1: Get OSM data
            print(f"[1/{total_steps}] Acquiring OSM data...")
            if pbf_path:
                source_pbf = pbf_path
            else:
                source_pbf = os.path.join(tmpdir, "source.osm.pbf")
                download_osm_extract(geofabrik_path, source_pbf)

            # Step 2: Extract bbox if needed
            if bbox_str and not args.area:
                work_pbf = os.path.join(tmpdir, "area.osm.pbf")
                extract_bbox_from_pbf(source_pbf, bbox_str, work_pbf)
            elif bbox_str and args.area and geofabrik_path != KNOWN_AREAS.get(args.area.lower().replace(" ", "-"), {}).get("geofabrik"):
                work_pbf = os.path.join(tmpdir, "area.osm.pbf")
                extract_bbox_from_pbf(source_pbf, bbox_str, work_pbf)
            else:
                work_pbf = source_pbf

            # Step 3: Generate vector tiles
            print()
            print(f"[2/{total_steps}] Generating vector tiles...")
            mbtiles_path = os.path.join(tmpdir, "tiles.mbtiles")
            generate_tiles(work_pbf, mbtiles_path, bbox=bbox_str,
                           fast=args.fast, store=args.store)

        # Step 4: Extract tiles from MBTiles
        print()
        print(f"[3/{total_steps}] Processing tiles...")

        # For large mbtiles (>5 GB), use streaming to avoid OOM
        mbtiles_size_gb = os.path.getsize(mbtiles_path) / (1024**3)
        use_streaming = mbtiles_size_gb > 5.0
        if use_streaming:
            tile_metadata, total_tile_count = get_mbtiles_info(mbtiles_path)
            tiles = None  # Don't load into memory
            print(f"  Streaming mode: {total_tile_count:,} tiles ({mbtiles_size_gb:.1f} GB)")
            print(f"    Format: {tile_metadata.get('format', 'unknown')}")
            print(f"    Name: {tile_metadata.get('name', 'unknown')}")
        else:
            tiles, tile_metadata = extract_tiles_from_mbtiles(mbtiles_path)
            total_tile_count = len(tiles)

        # Generate font glyphs
        fonts = generate_sdf_font_glyphs()

        # Step 5: Extract search features from tiles (or use cached)
        print()
        print(f"[4/{total_steps}] Building search index...")
        if args.search_cache:
            search_cache_path = args.search_cache
            if not os.path.isfile(search_cache_path):
                print(f"    Error: search cache not found: {search_cache_path}")
                sys.exit(1)
            cache_size = os.path.getsize(search_cache_path) / (1024 * 1024)
            print(f"    Using cached search features: {search_cache_path} ({cache_size:.0f} MB)")
            bbox = parse_bbox(bbox_str) if bbox_str else None
            if bbox:
                # Filter cached features to bbox
                minlon, minlat, maxlon, maxlat = bbox
                filtered_path = os.path.join(tmpdir, "search_features.jsonl")
                total = 0
                kept = 0
                with open(search_cache_path, "r") as fin, open(filtered_path, "w") as fout:
                    for line in fin:
                        total += 1
                        feat = json.loads(line)
                        lat, lon = feat["lat"], feat["lon"]
                        if minlat <= lat <= maxlat and minlon <= lon <= maxlon:
                            fout.write(line)
                            kept += 1
                        if total % 5_000_000 == 0:
                            print(f"\r    Filtered {total} features, kept {kept}...", end="", flush=True)
                print(f"\r    Filtered {kept}/{total} features within bbox          ", flush=True)
                search_features = filtered_path
            else:
                # No bbox — use the whole cache, copy to tmpdir
                import shutil
                filtered_path = os.path.join(tmpdir, "search_features.jsonl")
                shutil.copy2(search_cache_path, filtered_path)
                print(f"    Using all features (no bbox filter)")
                search_features = filtered_path
        elif use_streaming:
            search_features = extract_searchable_features(mbtiles_path=mbtiles_path, output_dir=tmpdir)
        else:
            search_features = extract_searchable_features(tiles=tiles, output_dir=tmpdir)

        # Append street addresses (addr:housenumber + addr:street) so users can
        # type "45 Brīvības gatve" in the routing UI. Requires a PBF — the MVT
        # tiles don't carry addr:* tags. Skipped silently when PBF is missing.
        address_count = 0
        wiki_cross_refs = None
        overture_sources = None
        overture_themes = None
        if isinstance(search_features, str) and os.path.isfile(search_features):
            addr_pbf = locals().get('work_pbf') or pbf_path or args.pbf
            if addr_pbf:
                addr_bbox = parse_bbox(bbox_str) if bbox_str else None
                if args.skip_address_extract:
                    print("    [--skip-address-extract] reusing cached addresses + overture enrichment")
                else:
                    address_count = extract_addresses_pbf(
                        addr_pbf, search_features, bbox=addr_bbox) or 0
                # Overture address enrichment — runs after OSM extraction so
                # the dedup index is populated. Only adds rows the OSM pass
                # didn't cover (the 1029-block gaps on Ramona St and friends).
                # Propagates the upstream-dataset list into overture_sources
                # (written into the ZIM + surfaced in the viewer's Sources
                # panel) so attribution credits every underlying feed. We
                # merge both address + places themes when provided, and
                # union their `datasets` so the ZIM credits every upstream
                # feed we touched.
                overture_themes = []
                overture_datasets = set()
                if args.overture_addresses and not args.skip_address_extract:
                    try:
                        merge_result = merge_overture_addresses(
                            args.overture_addresses, search_features,
                            bbox=addr_bbox)
                        address_count += merge_result.get("added", 0) or 0
                        overture_datasets.update(merge_result.get("datasets") or [])
                        overture_themes.append("addresses")
                    except Exception as _e:
                        print(f"    Warning: Overture addresses merge failed: {_e}")
                if args.overture_places and not args.skip_address_extract:
                    try:
                        places_result = merge_overture_places(
                            args.overture_places, search_features,
                            bbox=addr_bbox)
                        overture_datasets.update(places_result.get("datasets") or [])
                        overture_themes.append("places")
                    except Exception as _e:
                        print(f"    Warning: Overture places merge failed: {_e}")
                if overture_datasets:
                    overture_sources = sorted(overture_datasets)
                # Same PBF feeds the wiki-tag lookup so the chunker can enrich
                # POI records with wikipedia/wikidata for offline cross-ref.
                try:
                    wiki_cross_refs = extract_wiki_tags_pbf(addr_pbf, bbox=addr_bbox)
                except Exception as _e:
                    print(f"    Warning: wiki cross-ref extraction failed: {_e}")
                    wiki_cross_refs = None

        # Build Wikidata cache if requested
        wikidata_data = None
        if include_wikidata:
            step_wd = 5
            print()
            print(f"[{step_wd}/{total_steps}] Building Wikidata info cache...")
            from wikidata_cache import build_cache as wd_build_cache, load_cache_for_zim

            # Determine PBF path for Q-ID extraction (PBF preferred — has wikidata tags)
            wd_pbf = locals().get('work_pbf') or pbf_path or args.pbf
            if not wd_pbf:
                wd_mbtiles = mbtiles_path
            else:
                wd_mbtiles = None

            wd_cache_path = wd_build_cache(
                pbf_path=wd_pbf,
                mbtiles_path=wd_mbtiles,
                cache_dir=wikidata_cache_dir,
                skip_extracts=args.wikidata_no_extracts,
            )
            wikidata_data = load_cache_for_zim(wd_cache_path)
            if wikidata_data:
                print(f"    Loaded {len(wikidata_data)} Wikidata entries for ZIM")
            else:
                print("    No Wikidata entries available")

        # Extract routing graph if requested
        routing_graph_path = None
        routing_graph_geoms_path = None
        if include_routing:
            step_rt = 5 + (1 if include_wikidata else 0)
            print()
            print(f"[{step_rt}/{total_steps}] Extracting routing graph...")
            rt_pbf = locals().get('work_pbf') or pbf_path or args.pbf
            if not rt_pbf:
                print("    Warning: no PBF file available, skipping routing graph")
                print("    (routing requires a PBF file — not available with --mbtiles only)")
            else:
                rt_bbox = parse_bbox(bbox_str) if bbox_str else None
                routing_graph_path, routing_graph_geoms_path = extract_routing_graph(
                    rt_pbf, tmpdir, bbox=rt_bbox,
                    split_graph=bool(getattr(args, 'split_graph', False)),
                )

        # Download satellite tiles and generate terrain tiles
        # These are independent (satellite=I/O-bound, terrain=CPU-bound) so run in parallel
        satellite_dir = None
        terrain_dir = None
        sat_future = None
        terrain_future = None

        if include_satellite and bbox_str:
            # Use format/size-specific cache dir to avoid mixing tile formats
            sat_cache_suffix = f"_{satellite_format}_{satellite_tile_size}"
            satellite_dir = os.path.join(SCRIPT_DIR, f"satellite_cache{sat_cache_suffix}")
        if include_terrain and bbox_str:
            terrain_dir = args.terrain_dir or os.path.join(SCRIPT_DIR, "terrain_cache")

        if include_satellite and include_terrain and bbox_str:
            from concurrent.futures import ThreadPoolExecutor as StepPool
            print()
            print(f"[5/{total_steps}] Downloading satellite tiles + generating terrain tiles (parallel)...")

            with StepPool(max_workers=2) as step_pool:
                sat_future = step_pool.submit(
                    download_satellite_tiles, bbox_str, satellite_dir, satellite_download_zoom,
                    sat_format=satellite_format, sat_quality=satellite_quality,
                    tile_size=satellite_tile_size)
                terrain_future = step_pool.submit(
                    generate_terrain_tiles, bbox_str, terrain_dir, terrain_max_zoom,
                    low_zoom_world_vrt=getattr(args, "low_zoom_world_vrt", None))
                # Wait for both — exceptions will be raised on .result()
                terrain_future.result()
                print("    Terrain generation complete (satellite download continuing...)")
                sat_future.result()
                print("    Satellite download complete")
        else:
            if include_satellite:
                print()
                print(f"[5/{total_steps}] Downloading satellite tiles...")
                if not bbox_str:
                    print("    Warning: no bbox specified, skipping satellite tiles")
                else:
                    download_satellite_tiles(bbox_str, satellite_dir, max_zoom=satellite_download_zoom,
                                             sat_format=satellite_format, sat_quality=satellite_quality,
                                             tile_size=satellite_tile_size)

            if include_terrain:
                step_terrain = 5 + (1 if include_satellite else 0)
                print()
                print(f"[{step_terrain}/{total_steps}] Generating terrain tiles...")
                if not bbox_str:
                    print("    Warning: no bbox specified, skipping terrain tiles")
                else:
                    generate_terrain_tiles(bbox_str, terrain_dir,
                        max_zoom=terrain_max_zoom,
                        low_zoom_world_vrt=getattr(args, "low_zoom_world_vrt", None))

        # Verify terrain completeness — regen missing tiles AND fix boundary
        # seam tiles before packaging. Boundary tiles (straddling 1-degree DEM
        # cell edges) may have partial zero data if generated from a VRT that
        # didn't include all neighboring cells.
        if include_terrain and bbox_str and terrain_dir:
            import mercantile
            import math as _math
            bbox_parsed = parse_bbox(bbox_str)
            # Use buffered VRT for verification — bbox + 1 degree on each side
            dem_dir_v = os.path.join(terrain_dir, "dem_sources")
            _bbox_key_v = f"{bbox_parsed[0]:.1f}_{bbox_parsed[1]:.1f}_{bbox_parsed[2]:.1f}_{bbox_parsed[3]:.1f}"
            vrt_path = os.path.join(dem_dir_v, f"verify_{_bbox_key_v}.vrt")
            all_tifs_v = []
            for _lat in range(_math.floor(bbox_parsed[1]) - 1, _math.floor(bbox_parsed[3]) + 2):
                for _lon in range(_math.floor(bbox_parsed[0]) - 1, _math.floor(bbox_parsed[2]) + 2):
                    _ns = "N" if _lat >= 0 else "S"
                    _ew = "E" if _lon >= 0 else "W"
                    _p = os.path.join(dem_dir_v, f"dem_{_ns}{abs(_lat):02d}_{_ew}{abs(_lon):03d}.tif")
                    if os.path.isfile(_p) and os.path.getsize(_p) > 1000:
                        all_tifs_v.append(_p)
            if all_tifs_v:
                import tempfile as _tmpfile
                with _tmpfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as flist:
                    flist.write('\n'.join(all_tifs_v))
                    flist_path = flist.name
                try:
                    subprocess.run(
                        ["gdalbuildvrt", "-overwrite", "-input_file_list", flist_path, vrt_path],
                        check=True, capture_output=True, text=True,
                    )
                except FileNotFoundError:
                    # gdalbuildvrt not on PATH — skip the verification VRT.
                    # The terrain pyramid generation step already produced the
                    # tiles; verification is a defensive boundary-seam fixer
                    # that runs only when the VRT can be (re)built.
                    print(f"    Skipping terrain verification: gdalbuildvrt not found on PATH")
                os.unlink(flist_path)

            if os.path.isfile(vrt_path):
                print("    Verifying terrain tiles (missing + boundary seams)...")
                repair_tiles = []
                for z in range(0, terrain_max_zoom + 1):
                    for t in mercantile.tiles(*bbox_parsed, zooms=z):
                        tile_path = os.path.join(terrain_dir, str(z), str(t.x), f"{t.y}.webp")
                        bounds = mercantile.bounds(t)
                        needs_regen = False
                        if not os.path.isfile(tile_path):
                            needs_regen = True
                        elif os.path.getsize(tile_path) < 500:
                            # 44-byte WebPs are a known failure mode: when an
                            # earlier build's VRT didn't include the DEM for
                            # this tile's area, lossless WebP compressed the
                            # all-zeros fill down to ~44 bytes. Treat any tiny
                            # tile as broken and regenerate from the full VRT.
                            needs_regen = True
                        elif z >= 10:
                            # Check if tile straddles a 1-degree boundary
                            crosses_lon = _math.floor(bounds.west) != _math.floor(bounds.east)
                            crosses_lat = _math.floor(bounds.south) != _math.floor(bounds.north)
                            if crosses_lon or crosses_lat:
                                needs_regen = True
                        if needs_regen:
                            repair_tiles.append(
                                (vrt_path, t.x, t.y, z, terrain_dir,
                                 bounds.west, bounds.south, bounds.east, bounds.north)
                            )
                if repair_tiles:
                    print(f"    Repairing {len(repair_tiles)} tiles (missing + boundary)...")
                    from multiprocessing import Pool as _Pool
                    with _Pool(min(4, os.cpu_count() or 4)) as pool:
                        pool.map(_generate_one_terrain_tile, repair_tiles)
                    print(f"    Repaired {len(repair_tiles)} terrain tiles")
                else:
                    print("    Terrain complete — no gaps or boundary issues")

                # Strict post-repair audit: any tile at z>=10 that is both (a)
                # under the blank-size threshold AND (b) decodes to near-zero
                # elevation AND (c) sits over a DEM cell that IS on land (not
                # a .nodata marker) is the VRT-race bug — we have real DEM
                # data for this area but the tile says "0 m". Fail loudly
                # rather than ship a ZIM with visible stripes of missing
                # terrain.
                #
                # The size filter alone isn't enough: a 44-byte tile can be a
                # legit flat Colorado plateau at 2300 m (10 m quantization
                # collapses a ±5 m variation into a single RGB). Elevation
                # filter alone isn't enough either: genuine ocean tiles are
                # also near-zero. The combination is the signal.
                from PIL import Image as _PILImage
                import numpy as _np
                def _center_elev(path):
                    try:
                        im = _PILImage.open(path)
                        px = im.convert("RGB").load()
                        r, g, b = px[128, 128][:3]
                        return -10000.0 + ((r * 65536 + g * 256 + b) * 0.1)
                    except Exception:
                        return None

                def _tile_nonzero_fraction(path):
                    """Fraction of 256x256 pixels with elev != 0 m.
                    Cheap signal: a uniform-zero tile (the VRT-race
                    blank) is 0.0; a real terrain tile with a small
                    sea/lake patch (e.g. Caspian shoreline at z=12,
                    97 % at -10 m + 3 % at 0 m) is ≥ 0.97. Center-
                    pixel sampling alone caught the wrong cases —
                    Europe 2026-04-26 flagged 2 tiles whose center
                    happened to land in the small 0 m patch."""
                    try:
                        im = _np.array(_PILImage.open(path).convert("RGB"))
                        encoded = (im[:, :, 0].astype(_np.uint32) << 16) | \
                                  (im[:, :, 1].astype(_np.uint32) << 8) | \
                                  im[:, :, 2].astype(_np.uint32)
                        zero_code = int((10000.0 / 0.1))  # encoded value for 0 m
                        nonzero = (encoded != zero_code).sum()
                        return float(nonzero) / encoded.size
                    except Exception:
                        return 1.0  # on read error, don't flag — fall through

                # The real bug we're guarding against: tile decodes to ~0 m
                # (all-zeros output from a VRT-race artifact) but the VRT
                # itself would report real elevation at that location. If
                # the TILE and the VRT agree (both 0, or both 20 m plateau,
                # etc.) the tile is correct no matter how small its file size
                # — 10 m elevation quantization can collapse any ±5 m region
                # into a single RGB byte that compresses to 44 bytes.
                import rasterio as _rio
                _vrt_handle = _rio.open(vrt_path)
                try:
                    _vrt_sample = _vrt_handle.sample

                    def _vrt_land_fraction(bnds):
                        """Fraction of 3x3 sample points with real elevation (>5 m).
                        Returns a value in [0, 1]. 1.0 means all 9 are land, 0.0 ocean."""
                        pts = []
                        for fl in (0.25, 0.5, 0.75):
                            for fla in (0.25, 0.5, 0.75):
                                pts.append((bnds.west + (bnds.east - bnds.west) * fl,
                                            bnds.south + (bnds.north - bnds.south) * fla))
                        hits = 0
                        total = 0
                        for v in _vrt_sample(pts, indexes=1):
                            total += 1
                            if v and len(v) and abs(float(v[0])) > 5:
                                hits += 1
                        return hits / total if total else 0.0

                    # Previously: audited only z ≥ 10 (see
                    # ``project_terrain_blank_tile_bug.md``) on the grounds
                    # that z < 10 tiles extend outside the bbox+1° buffer
                    # and "cannot be fully regenerated from the buffered
                    # VRT". In practice that carve-out let interior z8-z9
                    # blanks slip through — Iran 2026-04-23 shipped with
                    # 9,433 blank tiles, of which ~80 at z8-z9 were
                    # user-visible as a horizontal stripe. The land-
                    # fraction check (≥ 6/9 of VRT sample points on
                    # land) is zoom-independent — it already treats
                    # genuinely-ocean low-zoom tiles as OK. So extend the
                    # audit all the way to z=0. Legitimate-partial cases
                    # stay exempt; VRT-race blanks over real land fail.
                    still_broken = []
                    for z in range(0, terrain_max_zoom + 1):
                        for t in mercantile.tiles(*bbox_parsed, zooms=z):
                            tile_path = os.path.join(terrain_dir, str(z), str(t.x),
                                                     f"{t.y}.webp")
                            if not os.path.isfile(tile_path):
                                continue
                            if os.path.getsize(tile_path) >= 500:
                                continue
                            tile_elev = _center_elev(tile_path)
                            if tile_elev is None:
                                continue
                            # Whole-tile sanity check first: if MOST of the
                            # 256x256 pixels are non-zero, the tile is fine
                            # regardless of what the center pixel says. The
                            # center-pixel-only check let two false positives
                            # through Europe 2026-04-26 (Caspian shoreline +
                            # Pechora lowland — both 70-97 % real terrain
                            # but the center pixel landed in a small 0 m
                            # patch). Threshold of 5 % nonzero matches the
                            # known-broken signature: VRT-race blanks are
                            # uniform 0 m (0 % nonzero), legit tiles even at
                            # the lowest land elevations have at least some
                            # spatial variation.
                            nonzero_frac = _tile_nonzero_fraction(tile_path)
                            if nonzero_frac > 0.05:
                                continue
                            bnds = mercantile.bounds(t)
                            # Broken iff tile says near-zero AND majority of VRT
                            # samples have real elevation. A single land sample
                            # among 9 (e.g. a tiny island in Bass Strait) isn't
                            # enough — the tile is >80% ocean and writing 0 m
                            # is correct. Threshold = 6/9 (~67% land).
                            if abs(tile_elev) < 10 and _vrt_land_fraction(bnds) >= 6/9:
                                still_broken.append((z, t.x, t.y, tile_path))
                finally:
                    _vrt_handle.close()
                if still_broken:
                    # Narrow escape hatch: when Copernicus GLO-30 has
                    # genuine gaps (e.g. high Arctic ≥75°N where DEM
                    # tiles are sparse on Banks Island, Sverdrup
                    # Islands), an operator can set
                    # `TERRAIN_BLANK_TOLERATE=N` to allow up to N
                    # still-blank tiles through. Default 0 keeps the
                    # hard fail. This is intentionally NOT a flag —
                    # we don't want it to leak into routine builds.
                    tolerate = int(os.environ.get("TERRAIN_BLANK_TOLERATE", "0") or 0)
                    sample = still_broken[:5]
                    sample_str = "\n  ".join(
                        f"z={z} x={x} y={y} ({p})" for z, x, y, p in sample)
                    if len(still_broken) <= tolerate:
                        print(f"    [WARN] {len(still_broken)} blank tile(s) past "
                              f"repair, within TERRAIN_BLANK_TOLERATE={tolerate}. "
                              f"Sample:\n  {sample_str}\n    Continuing.")
                    else:
                        raise RuntimeError(
                            f"Terrain build unhealthy: {len(still_broken)} tiles still "
                            f"under 500 bytes after repair pass. Sample:\n  " +
                            sample_str +
                            "\nLikely missing DEM sources for these tiles' bbox. "
                            "Download the needed Copernicus DEMs, delete the broken "
                            "tiles and rerun, or set TERRAIN_BLANK_TOLERATE=N to "
                            f"accept up to N gaps (currently {tolerate}). Aborting."
                        )
                else:
                    print("    Terrain audit passed — no blank tiles in bbox")

        # NOTE: No size-threshold satellite audit — legitimate deep-ocean
        # Sentinel-2 imagery compresses to ~300-500 bytes (dark near-black RGB).
        # A stricter content-based check (pure uniform RGB → broken) could be
        # added later, but tile-size alone is not a valid signal for satellite.

        # Download MapLibre GL JS
        step_maplibre = total_steps - 1
        print()
        print(f"[{step_maplibre}/{total_steps}] Downloading MapLibre GL JS...")
        maplibre_dir = os.path.join(tmpdir, "maplibre")
        os.makedirs(maplibre_dir, exist_ok=True)
        maplibre_js, maplibre_css = download_maplibre(maplibre_dir)

        # Create ZIM
        step_zim = total_steps
        print()
        print(f"[{step_zim}/{total_steps}] Building ZIM file...")

        # Build map config
        bbox = parse_bbox(bbox_str) if bbox_str else None
        if bbox:
            center, zoom = get_center_and_zoom(bbox)
        else:
            center = [0, 0]
            zoom = 2

        import time as _time
        map_config = {
            "name": name,
            "center": center,
            "zoom": zoom,
            "minZoom": 0,
            "maxZoom": args.max_zoom,
            "buildDate": _time.strftime("%Y/%m"),
        }
        if bbox:
            map_config["bounds"] = bbox
        if satellite_dir and os.path.isdir(str(satellite_dir)):
            map_config["hasSatellite"] = True
            map_config["satelliteMaxZoom"] = satellite_max_zoom
            map_config["satelliteFormat"] = satellite_format
            map_config["satelliteTileSize"] = satellite_tile_size
        if terrain_dir and os.path.isdir(str(terrain_dir)):
            map_config["hasTerrain"] = True
            map_config["terrainMaxZoom"] = terrain_max_zoom
        if wikidata_data:
            map_config["hasWikidata"] = True
        if routing_graph_path:
            map_config["hasRouting"] = True
        if overture_sources:
            # Surface the flag so the viewer's Sources panel can show the
            # Overture attribution section. The concrete dataset list is
            # shipped as overture-sources.json at the ZIM root (below).
            map_config["hasOvertureAddresses"] = True

        create_zim(
            output_path=output_path,
            tiles=tiles,
            tile_metadata=tile_metadata,
            fonts=fonts,
            maplibre_js_path=maplibre_js,
            maplibre_css_path=maplibre_css,
            viewer_html_path=str(VIEWER_DIR / "index.html"),
            map_config=map_config,
            name=f"OSM - {name}",
            description=f"Offline OpenStreetMap for {name}. Vector tiles rendered client-side.",
            cluster_size=args.cluster_size * 1024,
            search_features_path=search_features if isinstance(search_features, str) else None,
            search_features=search_features if not isinstance(search_features, str) else None,
            satellite_dir=satellite_dir,
            satellite_max_zoom=satellite_max_zoom,
            satellite_format=satellite_format,
            terrain_dir=terrain_dir,
            terrain_max_zoom=terrain_max_zoom,
            zim_workers=args.workers,
            mbtiles_path=mbtiles_path if use_streaming else None,
            tile_count=total_tile_count if use_streaming else None,
            bbox=parse_bbox(bbox_str) if bbox_str else None,
            wikidata_data=wikidata_data,
            routing_graph_path=routing_graph_path,
            routing_graph_geoms_path=routing_graph_geoms_path,
            routing_graph_chunk_mb=int(getattr(args, 'chunk_graph_mb', 0) or 0),
            split_hot_search_chunks_mb=int(getattr(args, 'split_hot_search_chunks_mb', 0) or 0),
            split_find_chips=bool(getattr(args, 'split_find_chips', False)),
            wiki_cross_refs=wiki_cross_refs,
            overture_sources=overture_sources,
            overture_themes=overture_themes,
            address_count=address_count,
            zim_builder=getattr(args, "zim_builder", "python"),
            max_zoom=args.max_zoom,
        )

        print()
        print("=" * 60)
        print(f"SUCCESS! Created: {output_path}")
        print(f"  Size: {os.path.getsize(output_path) / (1024 * 1024):.1f} MB")
        print(f"  Tiles: {total_tile_count}")
        print(f"  Area: {name}")
        print()
        print("To use:")
        print("  1. Transfer the .zim file to your device")
        print("  2. Open it in the Kiwix app (iOS, Android, desktop)")
        print("  3. The map renders vector tiles client-side in MapLibre GL JS")
        print()
        print("Size savings vs raster tiles:")
        if bbox:
            # Rough estimate: raster tiles at z0-18 for this bbox
            lon_extent = bbox[2] - bbox[0]
            lat_extent = bbox[3] - bbox[1]
            # Very rough: ~500 tiles per sq degree at z14, 16x more per zoom after
            area_deg = lon_extent * lat_extent
            raster_est = area_deg * 500 * 16 * 16 * 20 / 1024  # rough KB estimate for z14-18
            zim_size = os.path.getsize(output_path) / 1024
            if raster_est > 0:
                ratio = raster_est / zim_size
                print(f"  This ZIM: {zim_size / 1024:.1f} MB")
                print(f"  Estimated raster z0-18: ~{raster_est / 1024:.0f} MB")
                print(f"  Savings: ~{ratio:.0f}x smaller")
        print("=" * 60)

    finally:
        if not args.keep_temp:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"\nTemp files kept at: {tmpdir}")


if __name__ == "__main__":
    main()
