"""OSM extract download, tilemaker, MBTiles readers, SDF fonts and the
MapLibre download (moved verbatim from create_osm_zim.py, which re-exports
these names)."""
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

# The builder's flushing, phase-timing print (see streetzim/common.py).
from streetzim.common import (
    print,
    TILEMAKER_CONFIG,
    TILEMAKER_PROCESS,
    GEOFABRIK_BASE,
    MAPLIBRE_CDN,
    download_file,
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


def required_shapefiles():
    """Shapefiles the tilemaker config reads, relative to the build directory."""
    with open(TILEMAKER_CONFIG) as f:
        return sorted({layer["source"] for layer in json.load(f)["layers"].values()
                       if "source" in layer})


def generate_tiles(pbf_path, mbtiles_path, bbox=None, fast=False, store=None):
    """Generate vector tiles from OSM PBF using tilemaker."""
    print("  Generating vector tiles with tilemaker...")
    # tilemaker opens the config's shapefiles relative to the current
    # directory and only prints "Unable to open" when one is missing, so a
    # build run from the wrong directory silently loses the ocean (and with
    # it, often the z0 tile). Fetch them with scripts/fetch-shapefiles.sh.
    shp = required_shapefiles()
    missing = [p for p in shp if not os.path.exists(p)]
    if missing:
        print(f"    WARNING: {len(missing)} shapefile(s) not found under "
              f"{os.getcwd()}: {', '.join(missing)}. Oceans / Natural Earth "
              "layers will be missing. Run scripts/fetch-shapefiles.sh from "
              "the directory you build in.")
        if os.environ.get("STREETZIM_REQUIRE_SHAPEFILES") == "1":
            raise SystemExit("STREETZIM_REQUIRE_SHAPEFILES=1 and shapefiles are missing")
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


def estimate_tile_total(mbtiles_path, zoom_level=None, bbox=None, max_zoom=None):
    """Upper bound on what iter_tiles_from_mbtiles() will yield, for progress.

    total_tiles used to be get_mbtiles_info()'s COUNT(*) over the WHOLE
    MBTiles. Every region streams from the shared 345 M-tile world file, so
    the progress line read "Added 26000/345534297 tiles (~24081m left)" on a
    3-hour build -- a denominator three orders of magnitude too large and an
    ETA of 16 days. Mirror the iterator's own bounds instead: for a bbox it
    is the per-zoom tile rectangle the SQL WHERE clause selects (arithmetic,
    no DB read). The whole-world path returns 0 so the caller keeps the
    MBTiles COUNT(*) it already has. The bbox figure is an upper bound -- a sparse MBTiles holds
    fewer rows than the rectangle -- so the ETA errs long, never short.
    """
    import mercantile

    # Same whole-world short-circuit the iterator applies before using bbox.
    if bbox:
        _minlon, _minlat, _maxlon, _maxlat = bbox
        if (_minlon <= -179.0 and _maxlon >= 179.0
                and _minlat <= -84.0 and _maxlat >= 84.0):
            bbox = None

    if zoom_level is not None:
        zoom_min = zoom_max = zoom_level
    else:
        zoom_min, zoom_max = 0, (14 if max_zoom is None else max_zoom)

    if bbox:
        minlon, minlat, maxlon, maxlat = bbox
        # Web Mercator cuts off near +-85.0511; mercantile.tile() raises
        # outside it, and several regions (nordics reaches 71N, and a
        # whole-world bbox that dodges the short-circuit reaches 90) would
        # otherwise crash the build for the sake of a progress number.
        lat_lo = max(minlat, -85.0)
        lat_hi = min(maxlat, 85.0)
        if lat_lo > lat_hi:
            return 0
        total = 0
        for z in range(zoom_min, zoom_max + 1):
            ul = mercantile.tile(minlon, lat_hi, z)
            lr = mercantile.tile(maxlon, lat_lo, z)
            nx = lr.x - ul.x + 1
            ny = lr.y - ul.y + 1
            if nx <= 0 or ny <= 0:
                # Antimeridian-crossing bbox. Do NOT `continue`: z0 always
                # yields nx=ny=1, so the function would return 1, and the
                # caller's `estimate_tile_total(...) or tile_count` treats 1
                # as a real answer -- total_tiles=1 then breaks the progress
                # line and disarms the backpressure guard. Bail so the caller
                # falls back to the MBTiles COUNT(*).
                return 0
            total += nx * ny
        return total

    # Whole-world path: the caller already has get_mbtiles_info()'s COUNT(*),
    # which for a world build IS the right denominator. Return 0 so it falls
    # back to that rather than paying a second scan -- "WHERE zoom_level <= 14"
    # walks all 345 M index entries on the 113 GB world file, minutes of IO
    # bought for a progress number.
    return 0


def iter_tiles_from_mbtiles(mbtiles_path, zoom_level=None, bbox=None, max_zoom=None):
    """Yield (z, x, y, data) tuples from MBTiles, streaming from SQLite.

    If zoom_level is specified, only yields tiles at that zoom.
    If max_zoom is specified (and zoom_level is not), yields tiles at zoom <= max_zoom.
    If bbox is specified as (minlon, minlat, maxlon, maxlat), only yields
    tiles that intersect the bounding box.
    Yields in (z, x, y) sorted order for deterministic ZIM insertion.
    """

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


def extract_tiles_from_mbtiles(mbtiles_path, max_zoom=None):
    """Extract individual tiles from an MBTiles file.

    Returns a dict of {(z, x, y): tile_data_bytes}.
    MBTiles uses TMS y-coordinate convention, so we flip to XYZ.
    Tiles in MBTiles are typically gzip-compressed already.

    max_zoom caps the zoom levels loaded. Without it, `--max-zoom` was
    silently ignored for every region whose mbtiles is <= 5 GB: create_zim
    only receives mbtiles_path when use_streaming is true (size > 5 GB), so
    small regions take the in-memory `tiles` dict path, which never consulted
    max_zoom. The streaming path (iter_tiles_from_mbtiles) filtered correctly,
    so the flag worked on continents and did nothing on countries. Caught
    2026-09-18: a switzerland `--max-zoom 13` build shipped all 44,520 z14
    tiles and came out byte-identical to the unrestricted build.

    Note for callers: when no --search-cache is given, search features are
    extracted from this dict (extract_searchable_features(tiles=...)), which
    reads z14 for POIs. Capping the zoom therefore also thins the search index
    unless a prebuilt search cache is supplied.
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

    # Extract tiles. Filter in SQL, not in Python: a z14 region is ~75% z14
    # tiles by count, and loading then discarding them wastes the memory this
    # non-streaming path exists to bound.
    if max_zoom is not None:
        cursor.execute(
            "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
            "WHERE zoom_level <= ?",
            (max_zoom,),
        )
    else:
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
        err = None
        # Transient errors (timeouts, resets, 5xx) are retried: a range lost
        # here ships as missing glyphs and those labels never render. Seen
        # in 2 of 6 CI-sized builds: 1-9 of 768 ranges silently dropped.
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return (local_name, range_key, resp.read(), None)
            except urllib.error.HTTPError as e:
                # 404 means this range has no glyphs in this font — skip it.
                # MapLibre falls back to local rendering on 404.
                if e.code == 404:
                    return (local_name, range_key, None, "HTTP 404")
                err = f"HTTP {e.code}"
            except Exception as e:
                err = str(e)
            time.sleep(min(2 ** attempt, 10))
        return (local_name, range_key, None, err)

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
    if failed and os.environ.get("STREETZIM_ALLOW_FONT_ERRORS") != "1":
        # Fail rather than ship a map whose labels in some scripts never render.
        # STREETZIM_ALLOW_FONT_ERRORS=1 ships anyway (e.g. during a CDN outage).
        raise SystemExit(f"{failed} font glyph range(s) failed to download after retries; "
                         f"not building a ZIM with missing glyphs")
    return fonts


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
