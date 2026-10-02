"""OSM extract download, tilemaker, MBTiles readers, SDF fonts and the
vendored MapLibre (moved verbatim from create_osm_zim.py, which re-exports
these names)."""
import json
import os
import shutil
import signal
import sqlite3
import subprocess

from streetzim import area
# The builder's flushing, phase-timing print (see streetzim/common.py).
from streetzim.common import (
    print,
    parse_bbox,
    TILEMAKER_CONFIG,
    TILEMAKER_PROCESS,
    GEOFABRIK_BASE,
    download_file,
)
from streetzim import viewer_assets
from streetzim.cpus import build_cpus


def download_osm_extract(geofabrik_path, dest):
    """Download an OSM PBF extract from Geofabrik (or planet.osm.org for planet)."""
    if geofabrik_path == "planet":
        url = "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf"
    else:
        url = f"{GEOFABRIK_BASE}/{geofabrik_path}-latest.osm.pbf"
    download_file(url, dest, f"OSM extract ({geofabrik_path})")


def extract_bbox_from_pbf(pbf_path, bbox, output_path):
    """Extract a bounding box from a PBF file using osmium.

    A box across the antimeridian is cut as two boxes, one each side
    (streetzim/area.py)."""
    print(f"  Extracting bbox {bbox} from PBF...")
    workdir = os.path.dirname(os.path.abspath(str(output_path)))
    cmd = [
        "osmium", "extract",
        *area.osmium_extract_args(parse_bbox(bbox), workdir, bbox_arg=bbox,
                                  flag="--bbox"),
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
    def tilemaker(input_pbf, bbox, merge=False):
        cmd = [
            "tilemaker",
            "--input", str(input_pbf),
            "--output", str(mbtiles_path),
            "--config", str(TILEMAKER_CONFIG),
            "--process", str(TILEMAKER_PROCESS),
            "--skip-integrity",
            # Without it tilemaker starts a thread per core it sees, and its
            # memory grows with them (streetzim/cpus.py).
            "--threads", str(build_cpus()),
        ]
        if bbox:
            cmd.extend(["--bbox", bbox])
        if merge:
            cmd.append("--merge")
        if fast:
            cmd.append("--fast")
            print("    Using --fast mode (trades RAM for speed)")
        if store:
            os.makedirs(store, exist_ok=True)
            cmd.extend(["--store", str(store)])
            print(f"    Using on-disk store: {store}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            # tilemaker maps its store files into memory: a full disk is a
            # SIGBUS on a page it cannot write, not an error message.
            if store and e.returncode == -signal.SIGBUS:
                try:
                    free = f"{shutil.disk_usage(store).free / 1e9:.1f} GB free"
                except OSError:
                    free = "free space unknown"
                print(f"    ERROR: tilemaker died of SIGBUS: its on-disk store ran out "
                      f"of disk in {store} ({free})", flush=True)
            raise

    parts = area.split(parse_bbox(bbox)) if bbox else []
    if len(parts) < 2:
        tilemaker(pbf_path, bbox)
    else:
        # Across the antimeridian. tilemaker clips to one box in [-180, 180]
        # (and one spanning the world would fill it with ocean tiles), so
        # each side is its own run over that side's data, the second
        # merged into the first MBTiles. Only z0 covers both sides;
        # --merge combines its layers.
        for i, part in enumerate(parts):
            side = area.to_str(part)
            side_pbf = f"{mbtiles_path}.side{i}.osm.pbf"
            print(f"    Side {i + 1} of the antimeridian: {side}")
            subprocess.run(["osmium", "extract", "--bbox", side,
                            "--strategy", "complete_ways", "--overwrite",
                            "-o", side_pbf, str(pbf_path)], check=True)
            try:
                tilemaker(side_pbf, side, merge=i > 0)
            finally:
                os.remove(side_pbf)
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
        return sum((c1 - c0 + 1) * (r1 - r0 + 1)
                   for z in range(zoom_min, zoom_max + 1)
                   for c0, c1, r0, r1 in _bbox_tile_ranges(bbox, z))

    # Whole-world path: the caller already has get_mbtiles_info()'s COUNT(*),
    # which for a world build IS the right denominator. Return 0 so it falls
    # back to that rather than paying a second scan -- "WHERE zoom_level <= 14"
    # walks all 345 M index entries on the 113 GB world file, minutes of IO
    # bought for a progress number.
    return 0


def _merge_ranges(ranges):
    """Inclusive integer ranges, sorted, with overlapping ones joined."""
    out = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _bbox_tile_ranges(bbox, zoom):
    """Column and TMS-row rectangles selected by mercantile.tiles, without
    enumerating their tiles. East/south edges use mercantile's epsilon so
    bounds of one tile select that tile alone. Across 180, z0 appears once.
    """
    import mercantile

    columns = []
    min_row = max_row = 0
    for west, south, east, north in area.split(area.normalize(bbox)):
        west, south = max(-180.0, west), max(-85.051129, south)
        east, north = min(180.0, east), min(85.051129, north)
        ul = mercantile.tile(west, north, zoom)
        lr = mercantile.tile(east - mercantile.LL_EPSILON,
                             south + mercantile.LL_EPSILON, zoom)
        if ul.x > lr.x or ul.y > lr.y:
            continue
        columns.append((ul.x, lr.x))
        min_row, max_row = (1 << zoom) - 1 - lr.y, (1 << zoom) - 1 - ul.y
    return [(lo, hi, min_row, max_row) for lo, hi in _merge_ranges(columns)]


def _tile_scan_order(conn):
    """Use the sequential scan only for a rowid-backed tiles table.

    Normalized MBTiles exposes a view, and cuts may use WITHOUT ROWID:
    neither has a physical rowid to order by. Older SQLite also lets view
    rowids read as NULL, which would sort the entire joined tile payload.
    """
    kind = conn.execute("SELECT type FROM sqlite_master WHERE name = 'tiles'").fetchone()
    if kind and kind[0] == "table":
        try:
            conn.execute("SELECT rowid FROM tiles LIMIT 0")
        except sqlite3.OperationalError:
            pass
        else:
            return "rowid"
    return "zoom_level, tile_column, tile_row"


def iter_tiles_from_mbtiles(mbtiles_path, zoom_level=None, bbox=None, max_zoom=None):
    """Yield (z, x, y, data) tuples from MBTiles, streaming from SQLite.

    If zoom_level is specified, only yields tiles at that zoom.
    If max_zoom is specified (and zoom_level is not), yields tiles at zoom <= max_zoom.
    If bbox is specified as (minlon, minlat, maxlon, maxlat), only yields
    tiles that intersect the bounding box.
    Yields in (z, x, TMS row) sorted order on the regional path.
    """
    conn = sqlite3.connect(str(mbtiles_path))
    try:
        cursor = conn.cursor()

        # On whole-world inputs a sequential heap scan avoids random BLOB
        # lookups. Keep this fast path for large tilemaker databases.
        if bbox:
            minlon, minlat, maxlon, maxlat = bbox
            if (minlon <= -179.0 and maxlon >= 179.0
                    and minlat <= -84.0 and maxlat >= 84.0):
                bbox = None

        if bbox:
            zoom_min = zoom_level if zoom_level is not None else 0
            zoom_max = (zoom_level if zoom_level is not None
                        else max_zoom if max_zoom is not None else 14)
            for z in range(zoom_min, zoom_max + 1):
                n = 1 << z
                for min_col, max_col, min_tms_row, max_tms_row in _bbox_tile_ranges(bbox, z):
                    cursor.execute(
                        "SELECT zoom_level, tile_column, tile_row, tile_data "
                        "FROM tiles WHERE zoom_level = ? "
                        "AND tile_column >= ? AND tile_column <= ? "
                        "AND tile_row >= ? AND tile_row <= ? "
                        "ORDER BY tile_column, tile_row",
                        (z, min_col, max_col, min_tms_row, max_tms_row),
                    )
                    for zz, x, tms_y, data in cursor:
                        yield zz, x, n - 1 - tms_y, data
        else:
            if zoom_level is not None:
                cursor.execute(
                    "SELECT zoom_level, tile_column, tile_row, tile_data "
                    "FROM tiles WHERE zoom_level = ? ORDER BY zoom_level, tile_column, tile_row",
                    (zoom_level,),
                )
            elif max_zoom is not None:
                # Sequential reads matter on continent-sized tilemaker files.
                cursor.execute(
                    "SELECT zoom_level, tile_column, tile_row, tile_data "
                    "FROM tiles WHERE zoom_level <= ? ORDER BY " + _tile_scan_order(conn),
                    (max_zoom,),
                )
            else:
                cursor.execute(
                    "SELECT zoom_level, tile_column, tile_row, tile_data "
                    "FROM tiles ORDER BY zoom_level, tile_column, tile_row"
                )
            for z, x, tms_y, data in cursor:
                yield z, x, (1 << z) - 1 - tms_y, data
    finally:
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


FALLBACK_SCAN_MAX_BYTES = 200_000_000


def fallback_scripts_in_tiles(tiles, lock=None):
    """The fallback scripts (lock ``fonts.fallback.scripts``) that occur in
    the labels of ``tiles`` ({(z, x, y): tile bytes}), for
    generate_sdf_font_glyphs.

    None (all of them, about 0.6 MB in the ZIM) when the tiles are not in
    memory (a streamed, continent-sized build) or are more than
    FALLBACK_SCAN_MAX_BYTES: in a ZIM that large the glyphs cost under 0.3%,
    less than the scan's time is worth. Measured 15-18 MB/s of stored tiles
    (tilemaker tiles of Fiji, 366k tiles; OpenMapTiles-schema Monaco x100,
    whose name:xx translations match nearly every tile), so seconds below
    the cap (about 13 s)."""
    import time
    from streetzim import glyph_fallback
    fallback = viewer_assets.font_fallback(lock)
    if fallback is None:
        return set()
    if tiles is None:
        return None
    total = sum(len(t) for t in tiles.values())
    if total > FALLBACK_SCAN_MAX_BYTES:
        print(f"  Label scripts: not scanned ({total / 1e6:,.0f} MB of tiles); "
              f"shipping every fallback script")
        return None
    t0 = time.monotonic()
    stats = {}
    found = glyph_fallback.scripts_in_tiles(tiles.values(), fallback.scripts, stats)
    unreadable = stats.get("unreadable", 0)
    print(f"  Label scripts needing fallback glyphs: {', '.join(sorted(found)) or 'none'} "
          f"({len(tiles):,} tiles scanned in {time.monotonic() - t0:.1f}s"
          + (f"; {unreadable:,} unreadable tiles skipped" if unreadable else "") + ")")
    return found


def generate_sdf_font_glyphs(lock=None, scripts=None):
    """SDF font glyphs for MapLibre GL JS, as pinned in the lock file.

    MapLibre needs SDF (Signed Distance Field) glyphs in protocol-buffer
    form, one file per 256 codepoints: fonts/{fontstack}/{start}-{end}.pbf.
    Every BMP range of each fontstack comes from the openmaptiles font CDN
    (e.g. General Punctuation 8192-8447 for the en dash in
    "Paris-Dakar"), each checked
    against its SHA-256 in resources/viewer-assets.lock.json and cached
    (streetzim/viewer_assets.py). A range the lock records as absent is
    skipped; MapLibre falls back to local rendering for it.

    Open Sans has no glyphs for some scripts (Arabic, Hebrew, ...). For
    those in ``scripts`` (names from the lock's ``fonts.fallback.scripts``;
    None: all of them), the pinned Noto Sans glyphs are merged into the
    ranges that hold them (streetzim/glyph_fallback.py); every other range
    is shipped exactly as pinned.

    A range whose content does not match its hash stops the build, always.
    One that cannot be downloaded after 5 attempts stops it too, unless
    STREETZIM_ALLOW_FONT_ERRORS=1 (a fallback range that could not be
    fetched then leaves its Open Sans range as it was).
    """
    from streetzim import glyph_fallback
    lock = lock or viewer_assets.load_lock()
    print("  Downloading SDF font glyphs (pinned, verified)...")
    # Our fontstack names have no spaces (URL-encoding differs between
    # Kiwix implementations); the lock maps them to the CDN's names.
    tasks = viewer_assets.font_ranges(lock)
    fallback = viewer_assets.font_fallback(lock)
    merge_stacks, blocks, merge_ranges, wanted = {}, [], [], []
    if fallback is not None:
        wanted = sorted(fallback.scripts if scripts is None
                        else set(scripts) & set(fallback.scripts))
        blocks = [b for name in wanted for b in fallback.scripts[name]]
        merge_ranges = glyph_fallback.ranges_for(blocks)
        stacks = {fr.stack for fr in tasks}
        merge_stacks = {s: f for s, f in fallback.for_stack.items() if s in stacks}
        fb_stacks = set(merge_stacks.values())
        tasks = tasks + [fr for fr in fallback.ranges
                         if fr.stack in fb_stacks and fr.range_key in merge_ranges]
        if wanted:
            print(f"    Fallback glyphs for: {', '.join(wanted)}")
    fonts, fb_fonts = _fetch_font_ranges(tasks, fb_stacks=set(merge_stacks.values()))
    merged = 0
    pinned = {(fr.stack, fr.range_key) for fr in fallback.ranges if fr.sha256} if fallback else set()
    lost = []  # (stack, range, scripts) left without glyphs by a waived download error
    for stack, fb_stack in merge_stacks.items():
        name = lock["fonts"]["fontstacks"].get(stack, stack)
        for r in merge_ranges:
            fb_data = fb_fonts.get((fb_stack, r))
            if fb_data is None:  # absent from the fallback, or waived download error
                if fallback is not None and (fb_stack, r) in pinned:
                    lo, hi = (int(x) for x in r.split("-"))
                    lost.append((stack, r, sorted(
                        s for s in wanted
                        if any(a <= hi and lo <= b for a, b in fallback.scripts[s]))))
                continue
            old = fonts.get((stack, r))
            new = glyph_fallback.merge_range(old, fb_data, blocks, name=name, range_key=r)
            if new is not old:
                fonts[(stack, r)] = new
                merged += 1
    for stack, r, lost_scripts in lost:
        # Only reachable under STREETZIM_ALLOW_FONT_ERRORS=1 (otherwise the
        # failed download already stopped the build).
        print(f"    WARNING: {stack} lost glyphs in {r} for {', '.join(lost_scripts)} "
              f"({merge_stacks[stack]} {r} failed to download); characters of that "
              f"range draw blank in that style")
    if merged:
        print(f"    Merged fallback glyphs into {merged} ranges")
        licence = viewer_assets.fallback_licence(lock)
        if licence is not None:
            # The OFL travels with the glyphs: fonts/NotoSans/OFL.txt
            fonts[(FALLBACK_LICENCE_DIR, "OFL.txt")] = licence
    return fonts


FALLBACK_LICENCE_DIR = "NotoSans"


def _fetch_font_ranges(tasks, *, fb_stacks=()):
    """Download (or read from the cache) and verify each range: (primary
    ranges, fallback ranges), both keyed by (stack, range)."""
    fonts = {}
    fb_fonts = {}

    def fetch_one(fr):
        if fr.sha256 is None:
            return (fr, None, "absent")
        try:
            return (fr, viewer_assets.fetch_verified(fr.url, fr.sha256), None)
        except viewer_assets.IntegrityError as e:
            return (fr, None, e)
        except viewer_assets.DownloadError as e:
            return (fr, None, str(e))

    from concurrent.futures import ThreadPoolExecutor, as_completed
    skipped = 0
    failed = 0
    mismatched = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(fetch_one, t) for t in tasks]
        done = 0
        for fut in as_completed(futures):
            fr, data, err = fut.result()
            done += 1
            if data is not None:
                (fb_fonts if fr.stack in fb_stacks else fonts)[(fr.stack, fr.range_key)] = data
            elif err == "absent":
                skipped += 1
            elif isinstance(err, viewer_assets.IntegrityError):
                mismatched.append(str(err))
            else:
                failed += 1
            if done % 100 == 0:
                print(f"\r    Got {len(fonts)} ranges ({done}/{len(tasks)} checked, {skipped} empty, {failed} errors)...", end="", flush=True)

    print(f"\r    Got {len(fonts)} verified font range files ({skipped} empty ranges skipped, {failed} errors)       ", flush=True)
    if mismatched:
        # Never waived: content that is not what was reviewed and pinned.
        raise SystemExit(f"{len(mismatched)} font glyph range(s) do not match their pinned "
                         f"sha256, e.g. {sorted(mismatched)[0]}")
    if failed and os.environ.get("STREETZIM_ALLOW_FONT_ERRORS") != "1":
        # Fail rather than ship a map whose labels in some scripts never render.
        # STREETZIM_ALLOW_FONT_ERRORS=1 ships anyway (e.g. during a CDN outage).
        raise SystemExit(f"{failed} font glyph range(s) failed to download after retries; "
                         f"not building a ZIM with missing glyphs")
    return fonts, fb_fonts


def vendored_maplibre():
    """MapLibre GL JS and CSS from resources/vendor/maplibre-gl/, after
    checking them against the lock file (nothing is downloaded)."""
    try:
        files = viewer_assets.vendored_maplibre()
    except viewer_assets.IntegrityError as e:
        raise SystemExit(str(e)) from None
    print(f"  MapLibre GL JS {viewer_assets.maplibre_version()} (vendored, sha256 verified)")
    return str(files["maplibre-gl.js"]), str(files["maplibre-gl.css"])
