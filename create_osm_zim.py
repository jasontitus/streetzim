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
import gzip
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
RESOURCES_DIR = SCRIPT_DIR / "resources"
TILEMAKER_CONFIG = RESOURCES_DIR / "tilemaker" / "config-openmaptiles.json"
TILEMAKER_PROCESS = RESOURCES_DIR / "tilemaker" / "process-openmaptiles.lua"
VIEWER_DIR = RESOURCES_DIR / "viewer"

# Geofabrik base URL for downloading OSM extracts
GEOFABRIK_BASE = "https://download.geofabrik.de"

# Sentinel-2 Cloudless satellite tile service (EOX, CC BY 4.0 for 2016 vintage)
SATELLITE_TILE_URL = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/default/g/{z}/{y}/{x}.jpg"

# Copernicus GLO-30 DEM tile URL (public S3, no auth)
COPERNICUS_DEM_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)

# MapLibre GL JS version to bundle
MAPLIBRE_VERSION = "5.20.0"
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


def download_satellite_tiles(bbox_str, dest_dir, max_zoom=14, webp_quality=65):
    """Download Sentinel-2 Cloudless satellite tiles for a bounding box.

    Downloads JPEG tiles from the EOX Sentinel-2 Cloudless WMTS service,
    converts them to WebP for better compression, and stores them as
    {dest_dir}/{z}/{x}/{y}.webp.

    Returns the number of tiles downloaded.
    """
    import io
    import math
    import time
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from PIL import Image

    bbox = parse_bbox(bbox_str)
    minlon, minlat, maxlon, maxlat = bbox

    os.makedirs(dest_dir, exist_ok=True)
    total_downloaded = 0
    total_skipped = 0
    total_bytes_saved = 0
    lock = threading.Lock()

    def _download_tile(z, x, y):
        """Download and convert a single tile. Returns (downloaded, bytes_saved)."""
        tile_dir = os.path.join(dest_dir, str(z), str(x))
        tile_path = os.path.join(tile_dir, f"{y}.webp")

        if os.path.exists(tile_path) and os.path.getsize(tile_path) > 0:
            return (False, 0)

        os.makedirs(tile_dir, exist_ok=True)
        url = SATELLITE_TILE_URL.format(z=z, x=x, y=y)

        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    jpg_data = resp.read()
                img = Image.open(io.BytesIO(jpg_data))
                img.save(tile_path, "WEBP", quality=webp_quality)
                saved = len(jpg_data) - os.path.getsize(tile_path)
                return (True, saved)
            except Exception as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                else:
                    print(f"\n    Warning: failed to download z{z}/{x}/{y}: {e}")
        return (False, 0)

    max_workers = min(32, (os.cpu_count() or 4) * 4)

    for z in range(0, max_zoom + 1):
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

        tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)
        print(f"    z{z}: {tile_count} tiles ({x_max - x_min + 1}x{y_max - y_min + 1})")

        # Small zoom levels: download sequentially (few tiles)
        if tile_count <= 10:
            for x in range(x_min, x_max + 1):
                for y in range(y_min, y_max + 1):
                    downloaded, saved = _download_tile(z, x, y)
                    if downloaded:
                        total_downloaded += 1
                        total_bytes_saved += saved
                    else:
                        total_skipped += 1
            continue

        # Larger zoom levels: download in parallel
        tiles = [(z, x, y) for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1)]
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_download_tile, *t): t for t in tiles}
            for future in as_completed(futures):
                downloaded, saved = future.result()
                if downloaded:
                    total_downloaded += 1
                    total_bytes_saved += saved
                else:
                    total_skipped += 1
                completed += 1
                if completed % 500 == 0:
                    print(f"\r    Downloaded {total_downloaded} tiles ({total_skipped} cached)...", end="", flush=True)

    saved_mb = total_bytes_saved / (1024 * 1024)
    print(f"\r    Downloaded {total_downloaded} satellite tiles ({total_skipped} cached)")
    print(f"    WebP compression saved {saved_mb:.1f} MB vs JPEG source")
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


def generate_terrain_tiles(bbox_str, dest_dir, max_zoom=12):
    """Download Copernicus GLO-30 DEM and generate terrain-RGB tiles.

    Downloads 1-degree GeoTIFF tiles from AWS, mosaics them, then generates
    Mapbox terrain-RGB tiles as lossless WebP using rasterio + mercantile.
    Tiles are stored as {dest_dir}/{z}/{x}/{y}.webp.
    """
    import math
    import io

    bbox = parse_bbox(bbox_str)
    minlon, minlat, maxlon, maxlat = bbox

    os.makedirs(dest_dir, exist_ok=True)
    dem_dir = os.path.join(dest_dir, "dem_sources")
    os.makedirs(dem_dir, exist_ok=True)

    # Check if tiles already generated for THIS bbox by sampling a few z-max tiles
    import mercantile
    z_max_tiles = list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=max_zoom))
    if z_max_tiles:
        sample = z_max_tiles[:5] + z_max_tiles[-5:]
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

    # Determine which 1-degree Copernicus tiles we need
    tif_paths = []
    for lat in range(math.floor(minlat), math.floor(maxlat) + 1):
        for lon in range(math.floor(minlon), math.floor(maxlon) + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            abs_lat = abs(lat)
            abs_lon = abs(lon)
            url = COPERNICUS_DEM_URL.format(ns=ns, lat=abs_lat, ew=ew, lon=abs_lon)
            fname = f"dem_{ns}{abs_lat:02d}_{ew}{abs_lon:03d}.tif"
            fpath = os.path.join(dem_dir, fname)

            if not os.path.exists(fpath) or os.path.getsize(fpath) < 1000:
                print(f"    Downloading {ns}{abs_lat:02d} {ew}{abs_lon:03d}...")
                req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        with open(fpath, "wb") as f:
                            while True:
                                chunk = resp.read(1024 * 1024)
                                if not chunk:
                                    break
                                f.write(chunk)
                    size_mb = os.path.getsize(fpath) / (1024 * 1024)
                    print(f"      {size_mb:.1f} MB")
                except Exception as e:
                    print(f"      Warning: failed to download: {e}")
                    continue
            else:
                size_mb = os.path.getsize(fpath) / (1024 * 1024)
                print(f"    Cached: {ns}{abs_lat:02d} {ew}{abs_lon:03d} ({size_mb:.1f} MB)")
            tif_paths.append(fpath)

    if not tif_paths:
        print("    No DEM tiles downloaded, skipping terrain")
        return 0

    # Mosaic source DEMs using rasterio
    print("    Mosaicing DEM tiles...")
    import rasterio
    from rasterio.merge import merge
    from rasterio.warp import reproject, Resampling, transform_bounds
    from rasterio.transform import from_bounds
    import mercantile
    import numpy as np
    from PIL import Image

    datasets = [rasterio.open(p) for p in tif_paths]
    mosaic, mosaic_transform = merge(datasets)
    mosaic_crs = datasets[0].crs
    mosaic_meta = datasets[0].meta.copy()
    for ds in datasets:
        ds.close()

    mosaic_meta.update({
        "height": mosaic.shape[1],
        "width": mosaic.shape[2],
        "transform": mosaic_transform,
        "count": 1,
    })

    # Write mosaic to temp file
    mosaic_path = os.path.join(dem_dir, "mosaic_4326.tif")
    with rasterio.open(mosaic_path, "w", **mosaic_meta) as dst:
        dst.write(mosaic[0], 1)
    del mosaic  # free memory

    # Generate terrain-RGB tiles directly with rasterio + mercantile
    # Each thread opens its own file handle to avoid loading the full raster into memory.
    # For large areas (e.g. Iran = 20°x15° = ~14 GB at 30m), loading all into RAM OOMs.
    print(f"    Generating terrain-RGB tiles (z0-{max_zoom})...")
    count = 0

    def _generate_one_terrain_tile(mosaic_file, tile_x, tile_y, z, dest_dir_local,
                                    tb_west, tb_south, tb_east, tb_north):
        """Generate a single terrain-RGB tile. Opens its own file handle."""
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

    for z in range(0, max_zoom + 1):
        tiles_at_z = list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z))
        if not tiles_at_z:
            continue

        # Use ThreadPoolExecutor — rasterio/PIL release the GIL for the heavy C work.
        # Each thread opens its own file handle for windowed reads (low memory).
        if len(tiles_at_z) <= 10:
            for tile in tiles_at_z:
                tb = mercantile.bounds(tile)
                _generate_one_terrain_tile(
                    mosaic_path, tile.x, tile.y, z, dest_dir,
                    tb.west, tb.south, tb.east, tb.north,
                )
                count += 1
        else:
            from concurrent.futures import ThreadPoolExecutor as TerrainPool
            num_workers = min(os.cpu_count() or 4, len(tiles_at_z))
            with TerrainPool(max_workers=num_workers) as pool:
                futs = []
                for tile in tiles_at_z:
                    tb = mercantile.bounds(tile)
                    futs.append(pool.submit(
                        _generate_one_terrain_tile,
                        mosaic_path, tile.x, tile.y, z, dest_dir,
                        tb.west, tb.south, tb.east, tb.north,
                    ))
                for f in futs:
                    f.result()
                    count += 1

        print(f"      z{z}: {len(tiles_at_z)} tiles")

    print(f"    Generated {count} terrain tiles")
    return count


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


def iter_tiles_from_mbtiles(mbtiles_path, zoom_level=None):
    """Yield (z, x, y, data) tuples from MBTiles, streaming from SQLite.

    If zoom_level is specified, only yields tiles at that zoom.
    Yields in (z, x, y) sorted order for deterministic ZIM insertion.
    """
    conn = sqlite3.connect(str(mbtiles_path))
    cursor = conn.cursor()
    if zoom_level is not None:
        cursor.execute(
            "SELECT zoom_level, tile_column, tile_row, tile_data "
            "FROM tiles WHERE zoom_level = ? ORDER BY zoom_level, tile_column, tile_row",
            (zoom_level,),
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
    """Generate minimal SDF font glyphs for MapLibre GL JS.

    MapLibre GL JS requires SDF (Signed Distance Field) font glyphs in
    protocol buffer format. Each range covers 256 Unicode codepoints.
    Downloads real SDF fonts from the openmaptiles font CDN.
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

    for local_name, cdn_name in font_map.items():
        # Download ranges covering Latin + common characters (0-1279)
        for start in range(0, 65536, 256):
            end = start + 255
            range_key = f"{start}-{end}"

            cdn_encoded = cdn_name.replace(" ", "%20")
            url = f"{font_cdn}/{cdn_encoded}/{range_key}.pbf"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
                resp = urllib.request.urlopen(req)
                pbf_data = resp.read()
                fonts[(local_name, range_key)] = pbf_data
            except Exception as e:
                # Generate empty stub as fallback
                fonts[(local_name, range_key)] = _encode_font_pbf(local_name, range_key)

            # Only need ranges with actual glyphs (Latin + common)
            if start >= 1024:
                break

    print(f"    Downloaded {len(fonts)} font range files")
    return fonts


def _encode_font_pbf(name, range_str):
    """Encode a minimal protobuf for a font glyph range.

    This creates a valid but empty fontstack protobuf that MapLibre can parse
    without errors (it just won't have bitmap data for the glyphs).
    """
    # Protobuf wire format:
    # field 1 (fontstack message):
    #   field 1 (name): string
    #   field 2 (range): string

    def encode_varint(value):
        result = b""
        while value > 0x7F:
            result += bytes([(value & 0x7F) | 0x80])
            value >>= 7
        result += bytes([value])
        return result

    def encode_string_field(field_num, s):
        tag = (field_num << 3) | 2  # wire type 2 = length-delimited
        encoded = s.encode("utf-8")
        return encode_varint(tag) + encode_varint(len(encoded)) + encoded

    # Build inner fontstack message
    inner = encode_string_field(1, name)  # name
    inner += encode_string_field(2, range_str)  # range

    # Wrap in outer stacks field (field 1, wire type 2)
    outer = encode_varint((1 << 3) | 2) + encode_varint(len(inner)) + inner
    return outer


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


def _process_tile_partition(args):
    """Worker: read a tile_column range from SQLite and extract search features."""
    mbtiles_path, col_start, col_end, search_layers = args
    import mapbox_vector_tile
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(mbtiles_path))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT zoom_level, tile_column, tile_row, tile_data "
        "FROM tiles WHERE zoom_level = 14 AND tile_column >= ? AND tile_column < ?",
        (col_start, col_end),
    )

    results = []
    count = 0
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
                results.append({
                    "name": name,
                    "type": feature_type,
                    "subtype": subtype,
                    "lat": lat,
                    "lon": lon,
                })
        count += 1

    conn.close()
    return results, count


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


def extract_searchable_features(tiles=None, mbtiles_path=None):
    """Extract named features from z14 vector tiles for search indexing.

    Decodes the highest-zoom tiles and extracts features with names from
    the place, poi, transportation_name, water_name, park, mountain_peak,
    and aerodrome_label layers.

    Can operate in two modes:
    - tiles=dict: legacy mode, filters z14 from in-memory dict
    - mbtiles_path=str: streaming mode, reads z14 directly from SQLite

    Returns a list of dicts: [{"name": str, "type": str, "lat": float, "lon": float}, ...]
    """
    import mapbox_vector_tile

    print("  Extracting searchable features from tiles...")

    # Layers that contain searchable named features
    search_layers = {
        "place": "place",
        "poi": "poi",
        "transportation_name": "street",
        "water_name": "water",
        "park": "park",
        "mountain_peak": "peak",
        "aerodrome_label": "airport",
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
            return []
        # Get tile_column range for partitioning
        row = conn.execute(
            "SELECT MIN(tile_column), MAX(tile_column) FROM tiles WHERE zoom_level = 14"
        ).fetchone()
        col_min, col_max = row[0], row[1] + 1  # exclusive end
        conn.close()

        import multiprocessing
        import os as _os
        num_workers = _os.cpu_count() or 4
        print(f"    Processing {total_z14} z14 tiles with {num_workers} workers (partitioned reads)...")

        # Partition tile_column range across workers
        col_range = col_max - col_min
        partition_size = max(1, col_range // num_workers)
        partitions = []
        for i in range(num_workers):
            c_start = col_min + i * partition_size
            c_end = col_min + (i + 1) * partition_size if i < num_workers - 1 else col_max
            partitions.append((mbtiles_path, c_start, c_end, search_layers))

        features = []
        processed = 0
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(num_workers) as pool:
            for batch_features, batch_count in pool.imap_unordered(
                _process_tile_partition, partitions
            ):
                features.extend(batch_features)
                processed += batch_count
                print(f"\r    Processed {processed}/{total_z14} tiles, {len(features)} features so far...", end="", flush=True)

        print()
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

    # Deduplicate across tiles
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
    satellite_dir=None,
    satellite_max_zoom=None,
    terrain_dir=None,
    terrain_max_zoom=None,
    zim_workers=None,
):
    """Create a ZIM file containing the map viewer and all tiles."""
    from libzim.writer import Creator, Item, StringProvider, FileProvider
    from libzim.writer import Hint

    print(f"  Creating ZIM file: {output_path}")
    print(f"    Name: {name}")
    print(f"    Tiles: {len(tiles)}")
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
    # Use half of available cores for compression workers. Combined with
    # adaptive backpressure in the tile insertion loop, this prevents
    # libzim's queue spin-locks from causing stalls on large builds.
    num_workers = zim_workers or max(2, (os.cpu_count() or 4) // 2)
    print(f"    ZIM compression workers: {num_workers} (tiles: {len(tiles)})", flush=True)
    creator.config_nbworkers(num_workers)
    creator.set_mainpath("index.html")
    with creator:

        # Add metadata
        creator.add_metadata("Title", name)
        creator.add_metadata("Description", description)
        creator.add_metadata("Language", "eng")
        creator.add_metadata("Publisher", "create_osm_zim")
        creator.add_metadata("Creator", "OpenStreetMap contributors")
        creator.add_metadata("Date", "2026-03-10")
        creator.add_metadata("Tags", "maps;osm;offline")

        # Add the viewer HTML (main page)
        print("    Adding viewer HTML...")
        creator.add_item(MapItem(
            "index.html", name, "text/html",
            open(str(viewer_html_path)).read().encode("utf-8"),
            is_front=True,
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
            tile_source = iter_tiles_from_mbtiles(mbtiles_path)
        else:
            total_tiles = len(tiles)
            tile_source = iter([(z, x, y, data) for (z, x, y), data in sorted(tiles.items())])

        print(f"    Adding {total_tiles} vector tiles...", flush=True)
        tiles_added = 0
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
            for z, x, y, tile_data in results:
                creator.add_item(MapItem(
                    f"tiles/{z}/{x}/{y}.pbf", f"Tile {z}/{x}/{y}",
                    "application/x-protobuf",
                    tile_data,
                ))
                tiles_added += 1
                _watchdog_tile_count[0] = tiles_added
            add_time = time.time() - add_start

            # Adaptive backpressure: measure add_item throughput per batch.
            # If insertion rate drops below threshold, compression workers
            # can't keep up — sleep to let them drain the queue.
            batch_rate = batch_size / add_time if add_time > 0 else float("inf")
            if batch_rate < 5000 and total_tiles > 100_000:
                # Queue is backing up — increase sleep
                backpressure_sleep = min(backpressure_sleep + 0.02, 0.2)
                time.sleep(backpressure_sleep)
            elif batch_rate > 15000:
                # Queue is draining fine — reduce sleep
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
        print(f"\r    Added {tiles_added} tiles in {elapsed:.0f}s ({rate_str})                ", flush=True)
        _watchdog_stop.set()  # stop watchdog after tiles

        # Add satellite tiles if provided
        if satellite_dir and os.path.isdir(satellite_dir):
            sat_count = 0
            max_sz = satellite_max_zoom if satellite_max_zoom is not None else 99
            for z in range(0, max_sz + 1):
                z_dir = os.path.join(satellite_dir, str(z))
                if not os.path.isdir(z_dir):
                    continue
                for root, dirs, files in os.walk(z_dir):
                    for fname in files:
                        if not fname.endswith(".webp"):
                            continue
                        fpath = os.path.join(root, fname)
                        rel = os.path.relpath(fpath, satellite_dir)
                        zim_path = f"satellite/{rel}"
                        creator.add_item(MapItem(
                            zim_path, f"Satellite {rel}",
                            "image/webp",
                            fpath,
                            compress=False,
                        ))
                        sat_count += 1
                        if sat_count % 2000 == 0:
                            print(f"\r    Added {sat_count} satellite tiles...", end="", flush=True)
            print(f"\r    Added {sat_count} satellite tiles")

        # Add terrain tiles if provided
        if terrain_dir and os.path.isdir(terrain_dir):
            ter_count = 0
            max_tz = terrain_max_zoom if terrain_max_zoom is not None else 99
            for z in range(0, max_tz + 1):
                z_dir = os.path.join(terrain_dir, str(z))
                if not os.path.isdir(z_dir):
                    continue
                for root, dirs, files in os.walk(z_dir):
                    for fname in files:
                        if not fname.endswith(".webp"):
                            continue
                        fpath = os.path.join(root, fname)
                        rel = os.path.relpath(fpath, terrain_dir)
                        zim_path = f"terrain/{rel}"
                        creator.add_item(MapItem(
                            zim_path, f"Terrain {rel}",
                            "image/webp",
                            fpath,
                            compress=False,
                        ))
                        ter_count += 1
                        if ter_count % 2000 == 0:
                            print(f"\r    Added {ter_count} terrain tiles...", end="", flush=True)
            print(f"\r    Added {ter_count} terrain tiles")

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

        # Add search features
        if search_features:
            print(f"    Adding {len(search_features)} search entries...")

            # Build chunked search index for scalable on-demand loading.
            # Features are grouped by 2-character lowercase prefix of name.
            # The viewer fetches only the chunk matching the user's query,
            # so RAM usage stays bounded even for world-scale datasets.
            from collections import defaultdict
            chunks = defaultdict(list)
            for f in search_features:
                # Use first 2 chars of lowercased name as chunk key
                prefix = f["name"].lower()[:2].replace(" ", "_")
                # Normalize non-ascii to keep filenames safe
                prefix = "".join(c if c.isalnum() or c == "_" else "_" for c in prefix)
                if not prefix:
                    prefix = "__"
                prefix = prefix[:2].ljust(2, "_")
                chunks[prefix].append(
                    {"n": f["name"], "t": f["type"], "s": f.get("subtype", ""),
                     "a": f["lat"], "o": f["lon"], "l": f.get("location", "")}
                )

            # Add chunk manifest (list of available prefixes with counts)
            manifest = {k: len(v) for k, v in sorted(chunks.items())}
            total_features = sum(manifest.values())
            creator.add_item(MapItem(
                "search-data/manifest.json", "Search Manifest", "application/json",
                json.dumps({"total": total_features, "chunks": manifest},
                           separators=(",", ":")).encode("utf-8"),
            ))

            # Add each chunk as a separate JSON file
            for prefix, entries in sorted(chunks.items()):
                chunk_json = json.dumps(entries, separators=(",", ":"))
                creator.add_item(MapItem(
                    f"search-data/{prefix}.json",
                    f"Search chunk {prefix}",
                    "application/json",
                    chunk_json.encode("utf-8"),
                ))

            print(f"    Added {len(chunks)} search chunks ({total_features} features)")

            # Add individual HTML redirect pages for Kiwix's native Xapian
            # full-text search. Only include important features (places, airports,
            # parks, peaks, water) to keep the ZIM manageable. Streets and POIs
            # are still searchable via the JS chunked search but don't get
            # individual pages (there can be millions of them).
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
                label = feat.get("subtype", feat["type"]).replace("_", " ").title()

                html = (
                    f'<!DOCTYPE html><html><head>'
                    f'<meta charset="utf-8">'
                    f'<meta http-equiv="refresh" content="0;url=index.html#{map_hash}">'
                    f'<title>{feat["name"]}</title>'
                    f'</head><body>'
                    f'<h1>{feat["name"]}</h1>'
                    f'<p>{label}</p>'
                    f'<p><a href="index.html#{map_hash}">View on map</a></p>'
                    f'</body></html>'
                )
                creator.add_item(MapItem(
                    f"search/{slug}.html",
                    feat["name"],
                    "text/html",
                    html.encode("utf-8"),
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
    parser.add_argument("--terrain", action="store_true",
                        help="Include Copernicus GLO-30 terrain tiles for 3D/hillshade")
    parser.add_argument("--terrain-zoom", type=int, default=12,
                        help="Max zoom for terrain tiles (default: 12)")
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of ZIM compression workers (default: CPU_count/2)")

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

    # Set output path
    safe_name = name.lower().replace(" ", "-").replace(",", "").replace(".", "")
    output_path = args.output or f"osm-{safe_name}.zim"

    # Satellite options
    include_satellite = args.satellite
    satellite_max_zoom = args.satellite_zoom or args.max_zoom

    # Terrain options
    include_terrain = args.terrain
    terrain_max_zoom = args.terrain_zoom

    total_steps = 6 + (1 if include_satellite else 0) + (1 if include_terrain else 0)

    print(f"=== Creating Offline OSM ZIM: {name} ===")
    if include_satellite:
        print(f"  Including Sentinel-2 satellite imagery (z0-{satellite_max_zoom})")
    if include_terrain:
        print(f"  Including Copernicus GLO-30 terrain (z0-{terrain_max_zoom})")
    print()

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

        # Step 5: Extract search features from tiles
        print()
        print(f"[4/{total_steps}] Building search index...")
        if use_streaming:
            search_features = extract_searchable_features(mbtiles_path=mbtiles_path)
        else:
            search_features = extract_searchable_features(tiles=tiles)

        # Download satellite tiles and generate terrain tiles
        # These are independent (satellite=I/O-bound, terrain=CPU-bound) so run in parallel
        satellite_dir = None
        terrain_dir = None
        sat_future = None
        terrain_future = None

        if include_satellite and bbox_str:
            satellite_dir = os.path.join(SCRIPT_DIR, "satellite_cache")
        if include_terrain and bbox_str:
            terrain_dir = os.path.join(SCRIPT_DIR, "terrain_cache")

        if include_satellite and include_terrain and bbox_str:
            from concurrent.futures import ThreadPoolExecutor as StepPool
            print()
            print(f"[5/{total_steps}] Downloading satellite tiles + generating terrain tiles (parallel)...")

            with StepPool(max_workers=2) as step_pool:
                sat_future = step_pool.submit(
                    download_satellite_tiles, bbox_str, satellite_dir, satellite_max_zoom)
                terrain_future = step_pool.submit(
                    generate_terrain_tiles, bbox_str, terrain_dir, terrain_max_zoom)
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
                    download_satellite_tiles(bbox_str, satellite_dir, max_zoom=satellite_max_zoom)

            if include_terrain:
                step_terrain = 5 + (1 if include_satellite else 0)
                print()
                print(f"[{step_terrain}/{total_steps}] Generating terrain tiles...")
                if not bbox_str:
                    print("    Warning: no bbox specified, skipping terrain tiles")
                else:
                    generate_terrain_tiles(bbox_str, terrain_dir, max_zoom=terrain_max_zoom)

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

        map_config = {
            "name": name,
            "center": center,
            "zoom": zoom,
            "minZoom": 0,
            "maxZoom": args.max_zoom,
        }
        if bbox:
            map_config["bounds"] = bbox
        if satellite_dir and os.path.isdir(str(satellite_dir)):
            map_config["hasSatellite"] = True
            map_config["satelliteMaxZoom"] = satellite_max_zoom
        if terrain_dir and os.path.isdir(str(terrain_dir)):
            map_config["hasTerrain"] = True
            map_config["terrainMaxZoom"] = terrain_max_zoom

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
            search_features=search_features,
            satellite_dir=satellite_dir,
            satellite_max_zoom=satellite_max_zoom,
            terrain_dir=terrain_dir,
            terrain_max_zoom=terrain_max_zoom,
            zim_workers=args.workers,
            mbtiles_path=mbtiles_path if use_streaming else None,
            tile_count=total_tile_count if use_streaming else None,
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
