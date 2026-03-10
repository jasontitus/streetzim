#!/usr/bin/env python3
"""
create_osm_zim_leaflet.py - Create a ZIM file with offline Leaflet map using raster tiles.

This is the raster-tile counterpart to create_osm_zim.py (vector tiles + MapLibre).
It renders OSM vector tiles to PNG raster images and packages them with Leaflet.js
into a ZIM file for offline use in Kiwix.

Comparison:
  - Vector (create_osm_zim.py): Small files, client-side rendering via MapLibre GL JS
  - Raster (this script): Larger files, simple rendering via Leaflet, wider compatibility

Pipeline:
  1. Download OSM data from Geofabrik
  2. Generate vector tiles with tilemaker (same as vector approach)
  3. Render vector tiles to PNG raster images using Python (Pillow + mapbox-vector-tile)
  4. Download Leaflet.js
  5. Package everything into a ZIM file

Usage:
    python3 create_osm_zim_leaflet.py --area monaco
    python3 create_osm_zim_leaflet.py --area dc
"""

import argparse
import gzip
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter
import mapbox_vector_tile

SCRIPT_DIR = Path(__file__).parent.resolve()
RESOURCES_DIR = SCRIPT_DIR / "resources"
TILEMAKER_CONFIG = RESOURCES_DIR / "tilemaker" / "config-openmaptiles.json"
TILEMAKER_PROCESS = RESOURCES_DIR / "tilemaker" / "process-openmaptiles.lua"
VIEWER_DIR = RESOURCES_DIR / "viewer-leaflet"

GEOFABRIK_BASE = "https://download.geofabrik.de"

LEAFLET_VERSION = "1.9.4"
LEAFLET_CDN = f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist"

# Tile size in pixels
TILE_SIZE = 512  # 2x resolution for retina/HiDPI displays

# ── Color scheme (OSM-like) ──────────────────────────────────────────────────

COLORS = {
    "background": (242, 239, 233),
    # Landcover / landuse
    "grass": (205, 235, 176),
    "forest": (173, 209, 158),
    "wood": (173, 209, 158),
    "farmland": (237, 240, 214),
    "residential": (224, 222, 222),
    "commercial": (238, 207, 207),
    "industrial": (235, 219, 232),
    "park": (200, 230, 180),
    "cemetery": (170, 203, 175),
    # Water
    "water": (170, 211, 223),
    "river": (170, 211, 223),
    "waterway": (170, 211, 223),
    # Buildings
    "building": (217, 208, 201),
    "building_outline": (195, 185, 177),
    # Roads
    "motorway": (233, 144, 160),
    "trunk": (249, 178, 156),
    "primary": (252, 214, 164),
    "secondary": (246, 250, 187),
    "tertiary": (255, 255, 255),
    "minor": (255, 255, 255),
    "path": (200, 200, 200),
    "rail": (180, 180, 180),
    # Boundaries
    "boundary": (190, 140, 190),
}

# Font paths for label rendering
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Cache loaded fonts at various sizes
_font_cache = {}


def get_font(bold=False, size=11):
    """Get a cached PIL font at the requested size, scaled for tile resolution."""
    scaled_size = int(size * SCALE)
    key = (bold, scaled_size)
    if key not in _font_cache:
        path = FONT_BOLD if bold else FONT_REGULAR
        try:
            _font_cache[key] = ImageFont.truetype(path, scaled_size)
        except (IOError, OSError):
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


# Scale factor: tile pixels / 256 (=1 for 256px tiles, =2 for 512px retina)
SCALE = TILE_SIZE / 256

ROAD_WIDTHS = {
    "motorway": 3.0 * SCALE,
    "trunk": 2.5 * SCALE,
    "primary": 2.0 * SCALE,
    "secondary": 1.5 * SCALE,
    "tertiary": 1.2 * SCALE,
    "minor": 0.8 * SCALE,
    "service": 0.5 * SCALE,
    "path": 0.5 * SCALE,
    "rail": 1.0 * SCALE,
}


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
                    chunk = resp.read(1024 * 1024)
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


def download_osm_extract(geofabrik_path, dest):
    """Download an OSM PBF extract from Geofabrik."""
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


def generate_tiles(pbf_path, mbtiles_path, bbox=None):
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
    subprocess.run(cmd, check=True)
    size_mb = os.path.getsize(mbtiles_path) / (1024 * 1024)
    print(f"    Generated MBTiles: {size_mb:.1f} MB")


def extract_tiles_from_mbtiles(mbtiles_path):
    """Extract individual vector tiles from an MBTiles file.

    Returns a dict of {(z, x, y): tile_data_bytes}.
    """
    print("  Extracting tiles from MBTiles...")
    conn = sqlite3.connect(str(mbtiles_path))
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT name, value FROM metadata")
        metadata = dict(cursor.fetchall())
    except sqlite3.OperationalError:
        metadata = {}

    cursor.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles")
    tiles = {}
    count = 0
    for z, x, tms_y, data in cursor:
        y = (1 << z) - 1 - tms_y
        tiles[(z, x, y)] = data
        count += 1
        if count % 10000 == 0:
            print(f"\r    Extracted {count} tiles...", end="", flush=True)

    conn.close()
    print(f"\r    Extracted {count} total tiles")
    return tiles, metadata


# ── Raster tile rendering ────────────────────────────────────────────────────

def decode_vector_tile(data):
    """Decode a vector tile from PBF format, handling gzip."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    try:
        return mapbox_vector_tile.decode(data)
    except Exception:
        return {}


def project_coords(geom_coords, extent, tile_size=TILE_SIZE):
    """Scale geometry coordinates from tile extent to pixel coordinates."""
    scale = tile_size / extent
    if not geom_coords:
        return []
    # Handle nested coordinate lists (polygons have rings)
    if isinstance(geom_coords[0], (list, tuple)) and isinstance(geom_coords[0][0], (list, tuple)):
        return [[(c[0] * scale, c[1] * scale) for c in ring] for ring in geom_coords]
    return [(c[0] * scale, c[1] * scale) for c in geom_coords]


def get_road_class(properties):
    """Map transportation feature properties to a road class."""
    cls = properties.get("class", "")
    if cls in ("motorway", "motorway_link"):
        return "motorway"
    if cls in ("trunk", "trunk_link"):
        return "trunk"
    if cls in ("primary", "primary_link"):
        return "primary"
    if cls in ("secondary", "secondary_link"):
        return "secondary"
    if cls in ("tertiary", "tertiary_link"):
        return "tertiary"
    if cls in ("minor", "unclassified", "residential"):
        return "minor"
    if cls in ("service", "track"):
        return "service"
    if cls in ("path", "footway", "cycleway", "bridleway", "steps"):
        return "path"
    if cls == "rail":
        return "rail"
    return "minor"


def render_tile_to_png(decoded_tile, zoom):
    """Render a decoded vector tile to a PNG image."""
    img = Image.new("RGB", (TILE_SIZE, TILE_SIZE), COLORS["background"])
    draw = ImageDraw.Draw(img)

    # Render layers in order: landcover → water → buildings → roads → boundaries

    # 1. Landcover / landuse
    for layer_name in ("landcover", "landuse"):
        layer = decoded_tile.get(layer_name)
        if not layer:
            continue
        extent = layer.get("extent", 4096)
        for feat in layer.get("features", []):
            geom = feat.get("geometry", {})
            props = feat.get("properties", {})
            cls = props.get("class", props.get("subclass", ""))
            gtype = geom.get("type", "")

            color = COLORS.get(cls, COLORS.get("grass"))
            if cls in ("forest", "wood"):
                color = COLORS["forest"]
            elif cls in ("farmland", "farm"):
                color = COLORS["farmland"]
            elif cls in ("residential",):
                color = COLORS["residential"]
            elif cls in ("commercial", "retail"):
                color = COLORS["commercial"]
            elif cls in ("industrial",):
                color = COLORS["industrial"]
            elif cls in ("park", "garden"):
                color = COLORS["park"]
            elif cls in ("cemetery",):
                color = COLORS["cemetery"]

            if gtype in ("Polygon", "MultiPolygon"):
                coords_list = geom.get("coordinates", [])
                if gtype == "Polygon":
                    coords_list = [coords_list]
                for poly_coords in coords_list:
                    projected = project_coords(poly_coords, extent)
                    if projected:
                        # Draw outer ring (first ring)
                        ring = projected[0] if isinstance(projected[0][0], tuple) else projected
                        if len(ring) >= 3:
                            draw.polygon(ring, fill=color)

    # 2. Water
    for layer_name in ("water", "waterway"):
        layer = decoded_tile.get(layer_name)
        if not layer:
            continue
        extent = layer.get("extent", 4096)
        for feat in layer.get("features", []):
            geom = feat.get("geometry", {})
            gtype = geom.get("type", "")

            if gtype in ("Polygon", "MultiPolygon"):
                coords_list = geom.get("coordinates", [])
                if gtype == "Polygon":
                    coords_list = [coords_list]
                for poly_coords in coords_list:
                    projected = project_coords(poly_coords, extent)
                    if projected:
                        ring = projected[0] if isinstance(projected[0][0], tuple) else projected
                        if len(ring) >= 3:
                            draw.polygon(ring, fill=COLORS["water"])
            elif gtype in ("LineString", "MultiLineString"):
                coords_list = geom.get("coordinates", [])
                if gtype == "LineString":
                    coords_list = [coords_list]
                for line_coords in coords_list:
                    projected = project_coords(line_coords, extent)
                    if projected and len(projected) >= 2:
                        width = max(1, int(1.5 * (zoom / 14.0)))
                        draw.line(projected, fill=COLORS["water"], width=width)

    # 3. Buildings (only at higher zoom)
    if zoom >= 13:
        layer = decoded_tile.get("building")
        if layer:
            extent = layer.get("extent", 4096)
            for feat in layer.get("features", []):
                geom = feat.get("geometry", {})
                gtype = geom.get("type", "")
                if gtype in ("Polygon", "MultiPolygon"):
                    coords_list = geom.get("coordinates", [])
                    if gtype == "Polygon":
                        coords_list = [coords_list]
                    for poly_coords in coords_list:
                        projected = project_coords(poly_coords, extent)
                        if projected:
                            ring = projected[0] if isinstance(projected[0][0], tuple) else projected
                            if len(ring) >= 3:
                                draw.polygon(ring, fill=COLORS["building"], outline=COLORS["building_outline"])

    # 4. Roads (transportation layer)
    layer = decoded_tile.get("transportation")
    if layer:
        extent = layer.get("extent", 4096)
        # Sort by road class so major roads draw on top
        road_order = {"path": 0, "service": 1, "minor": 2, "tertiary": 3,
                       "secondary": 4, "primary": 5, "trunk": 6, "motorway": 7, "rail": 8}
        features = sorted(layer.get("features", []),
                          key=lambda f: road_order.get(get_road_class(f.get("properties", {})), 0))

        for feat in features:
            geom = feat.get("geometry", {})
            props = feat.get("properties", {})
            gtype = geom.get("type", "")
            road_class = get_road_class(props)

            if gtype not in ("LineString", "MultiLineString"):
                continue

            coords_list = geom.get("coordinates", [])
            if gtype == "LineString":
                coords_list = [coords_list]

            base_width = ROAD_WIDTHS.get(road_class, 0.5)
            width = max(1, int(base_width * (zoom / 10.0)))
            color = COLORS.get(road_class, COLORS["minor"])

            if road_class == "rail":
                color = COLORS["rail"]

            for line_coords in coords_list:
                projected = project_coords(line_coords, extent)
                if projected and len(projected) >= 2:
                    # Draw casing (outline) for major roads
                    if road_class in ("motorway", "trunk", "primary") and width > 1:
                        draw.line(projected, fill=(150, 150, 150), width=width + 2)
                    draw.line(projected, fill=color, width=width)

    # 5. Boundaries
    layer = decoded_tile.get("boundary")
    if layer:
        extent = layer.get("extent", 4096)
        for feat in layer.get("features", []):
            geom = feat.get("geometry", {})
            gtype = geom.get("type", "")
            if gtype in ("LineString", "MultiLineString"):
                coords_list = geom.get("coordinates", [])
                if gtype == "LineString":
                    coords_list = [coords_list]
                for line_coords in coords_list:
                    projected = project_coords(line_coords, extent)
                    if projected and len(projected) >= 2:
                        draw.line(projected, fill=COLORS["boundary"], width=max(1, int(SCALE)))

    # 6. Labels — place names, road names, water names
    _render_labels(img, draw, decoded_tile, zoom)

    return img


def _render_labels(img, draw, decoded_tile, zoom):
    """Render text labels for places, roads, and water features.

    To avoid label repetition across tile boundaries, we only render
    labels whose anchor point falls within the inner portion of the
    tile (with margin). Features near tile edges are skipped — the
    neighboring tile will render them instead.
    """
    labels_drawn = []  # Track label bounding boxes to avoid overlap

    # Margin: only render labels whose anchor is within this inner area.
    # This prevents the same label from appearing on adjacent tiles.
    # Use a margin that's ~15% of tile size on each side.
    margin = int(TILE_SIZE * 0.15)
    inner_min = margin
    inner_max = TILE_SIZE - margin

    def _in_tile_center(px, py):
        """Check if a point is within the inner area of the tile."""
        return inner_min <= px <= inner_max and inner_min <= py <= inner_max

    def _get_name(props):
        """Extract best label text from properties."""
        return props.get("name:latin") or props.get("name") or ""

    def _draw_label(x, y, text, font, color, halo_color=None):
        """Draw a text label with optional halo, avoiding overlap."""
        if not text:
            return
        bbox = font.getbbox(text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        lx, ly = int(x - tw / 2), int(y - th / 2)

        # Check for overlap with existing labels (with padding)
        pad = int(4 * SCALE)
        new_rect = (lx - pad, ly - pad, lx + tw + pad, ly + th + pad)
        for existing in labels_drawn:
            if (new_rect[0] < existing[2] and new_rect[2] > existing[0] and
                    new_rect[1] < existing[3] and new_rect[3] > existing[1]):
                return  # Skip — overlaps
        labels_drawn.append(new_rect)

        # Draw halo (outline) by drawing text offset in 8 directions
        if halo_color:
            for dx, dy in [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]:
                draw.text((lx + dx, ly + dy), text, fill=halo_color, font=font)
        draw.text((lx, ly), text, fill=color, font=font)

    # Place labels (cities, towns, villages)
    layer = decoded_tile.get("place")
    if layer:
        extent = layer.get("extent", 4096)
        scale = TILE_SIZE / extent

        # Sort by rank/class for priority (cities first)
        class_order = {"city": 0, "town": 1, "village": 2, "suburb": 3,
                       "quarter": 4, "neighbourhood": 5, "hamlet": 6}
        features = sorted(
            layer.get("features", []),
            key=lambda f: class_order.get(f.get("properties", {}).get("class", ""), 99)
        )

        for feat in features:
            props = feat.get("properties", {})
            geom = feat.get("geometry", {})
            name = _get_name(props)
            cls = props.get("class", "")
            gtype = geom.get("type", "")
            if not name or gtype != "Point":
                continue

            coords = geom.get("coordinates", [])
            if not coords:
                continue
            px, py = coords[0] * scale, coords[1] * scale

            # Only render if anchor is in the inner tile area
            # (exception: cities/towns are important enough to always show)
            if cls not in ("city", "town") and not _in_tile_center(px, py):
                continue

            # Size and visibility by class and zoom
            if cls == "city" and zoom >= 5:
                font = get_font(bold=True, size=min(14, 8 + zoom - 5))
                _draw_label(px, py, name, font, (51, 51, 68), (255, 255, 255))
            elif cls == "town" and zoom >= 8:
                font = get_font(bold=True, size=min(12, 8 + zoom - 8))
                _draw_label(px, py, name, font, (68, 68, 68), (255, 255, 255))
            elif cls == "village" and zoom >= 10:
                font = get_font(bold=False, size=min(11, 8 + zoom - 10))
                _draw_label(px, py, name, font, (85, 85, 85), (255, 255, 255))
            elif cls in ("suburb", "quarter", "neighbourhood") and zoom >= 12:
                font = get_font(bold=False, size=10)
                _draw_label(px, py, name.upper(), font, (102, 102, 102), (255, 255, 255))

    # Road labels (transportation_name layer)
    if zoom >= 12:
        layer = decoded_tile.get("transportation_name")
        if layer:
            extent = layer.get("extent", 4096)
            scale = TILE_SIZE / extent
            # Deduplicate: only label each road name once per tile
            seen_road_names = set()
            for feat in layer.get("features", []):
                props = feat.get("properties", {})
                geom = feat.get("geometry", {})
                name = _get_name(props)
                if not name or name in seen_road_names:
                    continue
                gtype = geom.get("type", "")
                if gtype not in ("LineString", "MultiLineString"):
                    continue

                coords_list = geom.get("coordinates", [])
                if gtype == "LineString":
                    coords_list = [coords_list]
                for line_coords in coords_list:
                    if len(line_coords) < 2:
                        continue
                    # Place label at midpoint of the line
                    mid = len(line_coords) // 2
                    px = line_coords[mid][0] * scale
                    py = line_coords[mid][1] * scale
                    # Only render if midpoint is in inner tile area
                    if not _in_tile_center(px, py):
                        continue
                    font = get_font(bold=False, size=9)
                    _draw_label(px, py, name, font, (85, 85, 85), (255, 255, 255))
                    seen_road_names.add(name)
                    break  # One label per road name per tile

    # Water labels
    if zoom >= 10:
        layer = decoded_tile.get("water_name")
        if layer:
            extent = layer.get("extent", 4096)
            scale = TILE_SIZE / extent
            for feat in layer.get("features", []):
                props = feat.get("properties", {})
                geom = feat.get("geometry", {})
                name = _get_name(props)
                if not name or geom.get("type") != "Point":
                    continue
                coords = geom.get("coordinates", [])
                if not coords:
                    continue
                px, py = coords[0] * scale, coords[1] * scale
                if not _in_tile_center(px, py):
                    continue
                font = get_font(bold=False, size=10)
                _draw_label(px, py, name, font, (93, 128, 180), (255, 255, 255))


def render_all_tiles(vector_tiles, output_dir):
    """Render all vector tiles to PNG raster tiles.

    Returns count of rendered tiles and total bytes.
    """
    print("  Rendering raster tiles from vector tiles...")
    count = 0
    total_bytes = 0

    for (z, x, y), data in sorted(vector_tiles.items()):
        decoded = decode_vector_tile(data)
        if not decoded:
            continue

        img = render_tile_to_png(decoded, z)

        # Save PNG
        tile_dir = os.path.join(output_dir, str(z), str(x))
        os.makedirs(tile_dir, exist_ok=True)
        tile_path = os.path.join(tile_dir, f"{y}.png")
        img.save(tile_path, "PNG", optimize=True)

        total_bytes += os.path.getsize(tile_path)
        count += 1
        if count % 1000 == 0:
            print(f"\r    Rendered {count} tiles ({total_bytes / (1024*1024):.1f} MB)...",
                  end="", flush=True)

    print(f"\r    Rendered {count} tiles ({total_bytes / (1024*1024):.1f} MB total)")
    return count, total_bytes


# ── Leaflet download ─────────────────────────────────────────────────────────

def download_leaflet(dest_dir):
    """Download Leaflet JS files for embedding in the ZIM."""
    print("  Downloading Leaflet.js...")
    js_url = f"{LEAFLET_CDN}/leaflet.js"
    css_url = f"{LEAFLET_CDN}/leaflet.css"

    js_path = os.path.join(dest_dir, "leaflet.js")
    css_path = os.path.join(dest_dir, "leaflet.css")

    download_file(js_url, js_path, "leaflet.js")
    download_file(css_url, css_path, "leaflet.css")

    # Also download marker icons (Leaflet needs them)
    images_dir = os.path.join(dest_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    for img_name in ["marker-icon.png", "marker-icon-2x.png", "marker-shadow.png"]:
        img_url = f"{LEAFLET_CDN}/images/{img_name}"
        img_path = os.path.join(images_dir, img_name)
        try:
            download_file(img_url, img_path, img_name)
        except Exception:
            print(f"    Warning: Could not download {img_name}")

    return js_path, css_path


# ── ZIM creation ─────────────────────────────────────────────────────────────

def create_zim(
    output_path,
    raster_tile_dir,
    leaflet_js_path,
    leaflet_css_path,
    leaflet_images_dir,
    viewer_html_path,
    map_config,
    name,
    description="Offline OpenStreetMap (Raster)",
):
    """Create a ZIM file containing the Leaflet map viewer and raster tiles."""
    from libzim.writer import Creator, Item, StringProvider, FileProvider, Hint

    print(f"  Creating ZIM file: {output_path}")

    class MapItem(Item):
        def __init__(self, path, title, mimetype, content, is_front=False, compress=True):
            super().__init__()
            self._path = path
            self._title = title
            self._mimetype = mimetype
            self._is_front = is_front
            self._compress = compress
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

    creator = Creator(str(output_path))
    creator.config_indexing(True, "en")
    creator.set_mainpath("index.html")
    with creator:
        # Metadata
        creator.add_metadata("Title", name)
        creator.add_metadata("Description", description)
        creator.add_metadata("Language", "eng")
        creator.add_metadata("Publisher", "create_osm_zim_leaflet")
        creator.add_metadata("Creator", "OpenStreetMap contributors")
        creator.add_metadata("Date", "2026-03-10")
        creator.add_metadata("Tags", "maps;osm;offline;leaflet;raster")

        # Viewer HTML
        print("    Adding viewer HTML...")
        creator.add_item(MapItem(
            "index.html", name, "text/html",
            open(str(viewer_html_path)).read().encode("utf-8"),
            is_front=True,
        ))

        # Leaflet JS/CSS
        print("    Adding Leaflet.js...")
        creator.add_item(MapItem(
            "leaflet.js", "Leaflet JS", "application/javascript",
            leaflet_js_path,
        ))
        creator.add_item(MapItem(
            "leaflet.css", "Leaflet CSS", "text/css",
            leaflet_css_path,
        ))

        # Leaflet marker images
        if os.path.isdir(leaflet_images_dir):
            for img_name in os.listdir(leaflet_images_dir):
                img_path = os.path.join(leaflet_images_dir, img_name)
                if os.path.isfile(img_path):
                    creator.add_item(MapItem(
                        f"images/{img_name}", img_name, "image/png",
                        img_path,
                    ))

        # Map config
        config_json = json.dumps(map_config, indent=2)
        creator.add_item(MapItem(
            "map-config.json", "Map Config", "application/json",
            config_json.encode("utf-8"),
        ))

        # Raster tiles
        tile_count = 0
        for root, dirs, files in os.walk(raster_tile_dir):
            for fname in files:
                if not fname.endswith(".png"):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, raster_tile_dir)
                zim_path = f"tiles/{rel}"
                creator.add_item(MapItem(
                    zim_path, f"Tile {rel}",
                    "image/png",
                    fpath,
                ))
                tile_count += 1
                if tile_count % 5000 == 0:
                    print(f"\r    Added {tile_count} tiles...", end="", flush=True)

        print(f"\r    Added {tile_count} raster tiles")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"    ZIM file created: {size_mb:.1f} MB")


# ── Shared utilities ─────────────────────────────────────────────────────────

def parse_bbox(bbox_str):
    parts = [float(x.strip()) for x in bbox_str.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Invalid bbox: {bbox_str}")
    return parts


def get_center_and_zoom(bbox):
    minlon, minlat, maxlon, maxlat = bbox
    center_lon = (minlon + maxlon) / 2
    center_lat = (minlat + maxlat) / 2
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
    "monaco": {
        "geofabrik": "europe/monaco",
        "bbox": "7.40,43.72,7.44,43.76",
        "name": "Monaco",
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
}


def main():
    parser = argparse.ArgumentParser(
        description="Create a ZIM file with offline Leaflet map (raster tiles)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 create_osm_zim_leaflet.py --area monaco
  python3 create_osm_zim_leaflet.py --area dc
  python3 create_osm_zim_leaflet.py --geofabrik europe/liechtenstein --name "Liechtenstein"

Known areas: """ + ", ".join(sorted(KNOWN_AREAS.keys())),
    )

    parser.add_argument("--area", help="Well-known area name")
    parser.add_argument("--geofabrik", help="Geofabrik download path")
    parser.add_argument("--pbf", help="Path to local OSM PBF file")
    parser.add_argument("--bbox", help="Bounding box: minlon,minlat,maxlon,maxlat")
    parser.add_argument("--name", help="Name for the map")
    parser.add_argument("--output", "-o", help="Output ZIM file path")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary files")
    parser.add_argument("--max-zoom", type=int, default=14, help="Maximum zoom level (default: 14)")

    args = parser.parse_args()

    geofabrik_path = args.geofabrik
    bbox_str = args.bbox
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

    if not pbf_path and not geofabrik_path:
        print("Error: Must specify --area, --geofabrik, or --pbf")
        parser.print_help()
        sys.exit(1)

    if not name:
        name = args.area or args.geofabrik or "OpenStreetMap"

    safe_name = name.lower().replace(" ", "-").replace(",", "").replace(".", "")
    output_path = args.output or f"osm-{safe_name}-leaflet.zim"

    print(f"=== Creating Offline OSM ZIM (Leaflet/Raster): {name} ===")
    print()

    tmpdir = tempfile.mkdtemp(prefix="osm_zim_leaflet_")
    try:
        # Step 1: Get OSM data
        print("[1/6] Acquiring OSM data...")
        if pbf_path:
            source_pbf = pbf_path
        else:
            source_pbf = os.path.join(tmpdir, "source.osm.pbf")
            download_osm_extract(geofabrik_path, source_pbf)

        # Step 2: Extract bbox if needed
        if bbox_str and not args.area:
            work_pbf = os.path.join(tmpdir, "area.osm.pbf")
            extract_bbox_from_pbf(source_pbf, bbox_str, work_pbf)
        else:
            work_pbf = source_pbf

        # Step 3: Generate vector tiles (intermediate step)
        print()
        print("[2/6] Generating vector tiles (intermediate)...")
        mbtiles_path = os.path.join(tmpdir, "tiles.mbtiles")
        generate_tiles(work_pbf, mbtiles_path, bbox=bbox_str)

        # Step 4: Extract and render to raster
        print()
        print("[3/6] Extracting vector tiles...")
        vector_tiles, tile_metadata = extract_tiles_from_mbtiles(mbtiles_path)

        print()
        print("[4/6] Rendering raster tiles...")
        raster_dir = os.path.join(tmpdir, "raster_tiles")
        os.makedirs(raster_dir, exist_ok=True)
        tile_count, tile_bytes = render_all_tiles(vector_tiles, raster_dir)

        # Step 5: Download Leaflet
        print()
        print("[5/6] Downloading Leaflet.js...")
        leaflet_dir = os.path.join(tmpdir, "leaflet")
        os.makedirs(leaflet_dir, exist_ok=True)
        leaflet_js, leaflet_css = download_leaflet(leaflet_dir)
        leaflet_images = os.path.join(leaflet_dir, "images")

        # Step 6: Create ZIM
        print()
        print("[6/6] Building ZIM file...")

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

        create_zim(
            output_path=output_path,
            raster_tile_dir=raster_dir,
            leaflet_js_path=leaflet_js,
            leaflet_css_path=leaflet_css,
            leaflet_images_dir=leaflet_images,
            viewer_html_path=str(VIEWER_DIR / "index.html"),
            map_config=map_config,
            name=f"OSM - {name} (Leaflet)",
            description=f"Offline OpenStreetMap for {name}. Pre-rendered raster tiles with Leaflet.",
        )

        print()
        print("=" * 60)
        zim_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"SUCCESS! Created: {output_path}")
        print(f"  ZIM Size: {zim_size_mb:.1f} MB")
        print(f"  Tiles: {tile_count}")
        print(f"  Raw raster size: {tile_bytes / (1024*1024):.1f} MB")
        print(f"  Area: {name}")
        print()
        print("Comparison tip:")
        print(f"  Vector (MapLibre): python3 create_osm_zim.py --area {args.area or 'monaco'}")
        print(f"  Raster (Leaflet):  python3 create_osm_zim_leaflet.py --area {args.area or 'monaco'}")
        print("=" * 60)

    finally:
        if not args.keep_temp:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"\nTemp files kept at: {tmpdir}")


if __name__ == "__main__":
    main()
