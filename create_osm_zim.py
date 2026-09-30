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

# Some of these are unused here since the split into streetzim/; they stay
# because callers reach them through this module (tests patch
# create_osm_zim.urllib.request / create_osm_zim.time).
import argparse
import datetime
import glob
import gzip
import html as html_mod
import json
import itertools
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
# Viewer slots: pad the three viewer files to fixed UNCOMPRESSED slots so
# cloud/patch_viewer_inplace.py can replace them later without a re-pack.
# Single source of truth for the layout is cloud/viewer_slots.py, shared
# with cloud/swap_viewer_rust.py -- they previously built the header
# independently, which would have drifted.
from cloud.viewer_slots import pad_to_slot as _pad_to_slot
from streetzim import cpus as _cpus
from streetzim.cpus import build_cpus
# Search-feature extraction lives in streetzim/search_extract.py so it can be
# used without the builder (openzim/maps integration, cloud/ tools). Names
# are re-exported here for existing callers.
from streetzim.search_extract import (  # noqa: F401
    _annotate_lines_batch,
    _assign_location_batch,
    _finish_features_streaming,
    _init_location_worker,
    _process_tile_for_search,
    _process_tile_partition,
    build_location_index,
    extract_searchable_features,
    tile_to_lnglat,
)

# The builder's pieces live in streetzim/ (see streetzim/common.py and
# friends); every top-level name that used to be defined here is
# re-exported so `import create_osm_zim` callers (cloud/, tests/) and
# the PHASE_TIMER lookup in cloud/manifest_writer.py keep working.
from streetzim.common import (  # noqa: F401
    CACHE_DIR,
    _builtin_print,
    _PhaseTimer,
    _fmt_phase_dur,
    _phase_row,
    PHASE_TIMER,
    _HTTP_OK_RE,
    _PHASE_RE,
    print,
    SCRIPT_DIR,
    RESOURCES_DIR,
    TILEMAKER_CONFIG,
    TILEMAKER_PROCESS,
    VIEWER_DIR,
    REPO_ROOT,
    log_viewer_freshness,
    GEOFABRIK_BASE,
    SATELLITE_TILE_URL,
    COPERNICUS_DEM_URL,
    COPERNICUS_DEM_URL_GLO90,
    download_file,
    _SEARCH_COORD_DP,
    parse_bbox,
    _re_phase,
)
from streetzim import area as _area
from streetzim.routing.build import (  # noqa: F401
    extract_routing_graph,
    chunk_graph_file,
)
from streetzim.tiles import (  # noqa: F401
    download_osm_extract,
    extract_bbox_from_pbf,
    generate_tiles,
    get_mbtiles_info,
    estimate_tile_total,
    iter_tiles_from_mbtiles,
    extract_tiles_from_mbtiles,
    generate_sdf_font_glyphs,
    fallback_scripts_in_tiles,
    vendored_maplibre,
)
from streetzim.satellite import (  # noqa: F401
    download_satellite_tiles,
    satellite_cache_dirs,
    stitch_satellite_image,
)
from streetzim import satellite_sources
from streetzim import terrain as _terrain
from streetzim.terrain import (  # noqa: F401
    _DEM_HANDLES,
    _generate_one_terrain_tile,
    _terrain_vrt_for_zoom,
    _DEM_MIN_TIF_BYTES,
    _TIFF_MAGICS,
    _TERRAIN_MIN_REUSE_BYTES,
    _TERRAIN_POISON_FROM,
    _TERRAIN_POISON_UNTIL,
    _build_dem_vrt,
    _dem_tif_is_usable,
    _bbox_tile_total,
    _terrain_tile_usable,
    generate_terrain_tiles,
)
from streetzim.addresses import (  # noqa: F401
    extract_addresses_pbf,
    _STREET_ABBREV,
    _normalize_street,
    _sql_string_literal,
    _haversine_m,
    _sample_overture_themes_in_cache,
    _load_url_cache,
    _url_dead_statuses,
    _is_url_dead,
    merge_overture_addresses,
    merge_overture_places,
    extract_wiki_tags_pbf,
)
from streetzim.overture import overture_release
from streetzim.zim_writer import (  # noqa: F401
    search_detail_html,
    _split_big_search_chunk,
    _resolve_xapianbuilder_binary,
    _streetzim_to_xapianbuilder_jsonl,
    _build_xapian_via_xapianbuilder,
    create_zim,
    _sub_bucket_for_name,
)


def registry_anchor(bbox, registry_path=None):
    """The hand-picked city for the region with this exact bbox, if any.

    Preferred over the place median because a median is dragged to whatever
    is densest inside the bbox, which is not always the region: pacific-
    islands spans 130E-180 and so includes part of Australia, whose places
    outnumber the islands' -- its median landed in inland Queensland, 15 deg
    from the bbox centre and not on any Pacific island. cloud/regions.tsv
    column 5 is a real city inside the region, chosen by hand.

    Matched on the bbox, because create_zim() is given a bbox and a display
    name but not the region id.
    """
    registry_path = registry_path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "cloud", "regions.tsv")
    if not bbox or not os.path.isfile(registry_path):
        return None
    try:
        with open(registry_path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                c = line.rstrip("\n").split("\t")
                if len(c) < 5:
                    continue
                try:
                    rb = [float(x) for x in c[2].split(",")]
                    lat, lon = (float(x) for x in c[4].split(","))
                except ValueError:
                    continue
                if len(rb) == 4 and _area.crosses(bbox) and rb[0] > rb[2]:
                    # A row across the antimeridian is written minlon >
                    # maxlon; the build has it unwrapped (streetzim/area.py).
                    rb = list(_area.normalize(rb))
                if len(rb) != 4 or any(abs(a - b) > 1e-6 for a, b in zip(rb, bbox)):
                    continue
                if not (rb[1] <= lat <= rb[3] and _area.contains_lon(rb, lon)):
                    return None          # anchor outside its own bbox: ignore
                # c[1] is the region NAME. Do NOT label with c[6]: that is
                # the full-text search term, which for united-states is
                # "Chicago" while c[5] is San Francisco -- a label naming a
                # city the point is not at is how wrong evidence gets made.
                return [round(lon, 5), round(lat, 5)], (c[1] if len(c) > 1 else c[0])
    except OSError:
        return None
    return None


def center_from_places(search_features_path, bbox, sample_limit=400_000):
    """Opening centre = where this region's places actually are.

    get_center_and_zoom() returns the geometric centre of the bbox, which for
    several regions is nothing at all: hawaii's is open Pacific ~500 km west
    of Kauai, north-africa's is open Sahara, nordics' is the Gulf of Bothnia.
    Reported 2026-09-25 as ZIMs "opening to random fairly empty areas".

    Two viewer-side fixes were tried and reverted -- fitBounds is undone by
    maxBounds, and a rescue that hunts for a city cannot find one when no
    tiles containing places are loaded. Doing it at build time needs no
    runtime machinery and no per-region data: the search-feature dump is
    already on disk at this point, one JSON object per place.

    MEDIAN, not mean: a mean is dragged into the sea by a few outlying
    islands, which is the failure being fixed. The median lands where the
    mass of places is -- Oahu for hawaii. Falls back to the bbox centre if
    the dump is missing or unreadable, so a build never fails over this.
    """
    import json as _json
    if not search_features_path or not os.path.isfile(search_features_path):
        return None
    lats, lons = [], []
    try:
        with open(search_features_path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= sample_limit:
                    break
                try:
                    rec = _json.loads(line)
                except ValueError:
                    continue
                lat, lon = rec.get("lat"), rec.get("lon")
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    if bbox and not _area.contains(bbox, lon, lat):
                        continue
                    lats.append(lat)
                    # In the bbox's frame, so the median of an area across
                    # the antimeridian is not pulled to the far side.
                    lons.append(_area.unwrap_lon(bbox, lon) if bbox else lon)
    except OSError:
        return None
    if len(lats) < 50:
        return None
    lats.sort()
    lons.sort()
    mid = len(lats) // 2
    return [round(_area.wrap_lon(lons[mid]), 5), round(lats[mid], 5)]


def get_center_and_zoom(bbox):
    """Calculate center point and initial zoom from a bounding box."""
    minlon, minlat, maxlon, maxlat = bbox
    center_lon = _area.wrap_lon((minlon + maxlon) / 2)   # past 180 across it
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
    # Monaco runs 7.409-7.440 E, 43.725-43.752 N. The old box
    # (7.40,43.72,7.44,43.76) ended on its eastern border and 500 m into
    # the sea, so the sea stopped in a straight line at Larvotto and off
    # Fontvieille, and a desktop window could not fit all of Monaco. This
    # one adds about 1.5 km of sea to the south and east and the edge of
    # the neighbouring towns. The Geofabrik extract stops near the border
    # (7.409-7.449, 43.723-43.752), so French land beyond it has only the
    # ways that cross it; the sea comes from the coastline shapefile.
    "monaco": {
        "geofabrik": "europe/monaco",
        "bbox": "7.39,43.715,7.46,43.765",
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


def _existing_file(path):
    """argparse type: a path that is a file. --low-zoom-world-vrt must not
    silently fall back to the fresh-machine terrain layout when missing."""
    if not os.path.isfile(path):
        raise argparse.ArgumentTypeError(f"{path} is not a file")
    return path


def build_parser():
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
    parser.add_argument("--bbox", help="Bounding box: minlon,minlat,maxlon,maxlat "
                        "(minlon > maxlon for an area across the antimeridian)")
    parser.add_argument("--map-center", metavar="LON,LAT",
                        help="Override initial map center. Default = bbox "
                             "centroid, which lands in empty water for "
                             "regions like Hawaii whose bbox includes the "
                             "uninhabited NW Hawaiian Islands. Format: "
                             "'-157.5,20.7'.")
    parser.add_argument("--map-zoom", type=int, metavar="Z",
                        help="Override initial map zoom (default = derived "
                             "from bbox extent).")
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
    parser.add_argument("--record-tile-source", action="store_true",
                        help="With --mbtiles: record the MBTiles' name, version and "
                             "OSM date in map-config.json (tileSource) and License")
    parser.add_argument("--tile-source-url", metavar="URL",
                        help="With --record-tile-source: the URL the MBTiles came from")
    parser.add_argument("--satellite", action="store_true",
                        help="Include Sentinel-2 Cloudless satellite imagery tiles")
    parser.add_argument("--satellite-source", choices=sorted(satellite_sources.SOURCES),
                        default=satellite_sources.BUILDER_DEFAULT,
                        help="Which EOX mosaic: s2cloudless-2016 is CC BY 4.0; "
                             "s2cloudless-2021 is CC BY-NC-SA 4.0, non-commercial "
                             "use only (default: %(default)s). The License metadata "
                             "and the viewer's credits follow the choice")
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
                        help="Number of ZIM compression workers (default: --cpus, at most 20)")
    parser.add_argument("--cpus", type=int, default=None, metavar="N",
                        help="CPU cores the build uses at once: tilemaker threads, "
                             "search and terrain processes, compression threads. "
                             "Default: the usable cores, capped by the container's "
                             "CPU quota and by its memory limit at "
                             f"{_cpus.GIB_PER_CPU} GiB per core (streetzim/cpus.py)")
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
    # Retired: the SZRG v5 split writer (never used in production). Kept as
    # a flag only to fail clearly instead of "unrecognized arguments".
    parser.add_argument("--split-graph", action="store_true", help=argparse.SUPPRESS)
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
                        type=_existing_file,
                        help="Use a world-coverage DEM VRT (e.g. "
                             "terrain_cache/dem_sources/world_dem_32k.tif) "
                             "for z=0-7 terrain tiles instead of the "
                             "region-bbox VRT. Prevents the bbox-edge "
                             "stripe bug where z=0-7 tiles that extend "
                             "past the bbox get zero-fill outside the "
                             "region. z=8+ still use the regional VRT "
                             "(fine-grained, no stripe risk). Must be a "
                             "file. Default None = the fresh-machine layout "
                             "(streetzim/terrain.py: terrain from the lowest "
                             "zoom the viewer can show, low zooms from GLO-90).")
    parser.add_argument("--overture-addresses", metavar="PARQUET",
                        help="Merge Overture Maps address records from a parquet extract. "
                             "Use download_overture_data.py to produce the parquet first. "
                             "Dedups against the OSM address pass; see docs/overture-matching.md.")
    parser.add_argument("--overture-places", metavar="PARQUET",
                        help="Enrich OSM POIs (and add new ones) from Overture Maps "
                             "places theme: websites, phones, socials, brand, and normalized "
                             "categories (museum/hotel/…) instead of OMT's noisy class buckets. "
                             "Run download_overture_data.py places --out …parquet first.")
    parser.add_argument("--url-cache", metavar="PATH", default=None,
                        help="Path to url_validation_cache.json (produced by "
                             "cloud/validate_overture_urls.py). When set, the "
                             "Overture-places merge consults the cache: rows whose "
                             "`ws` URL is dead (4xx/5xx/DNS/timeout/parked) are "
                             "dropped under drop-record policy (Pass 2 add-new), "
                             "or have their `ws` scrubbed under scrub-only policy "
                             "(Pass 1 enrich path always scrubs — never drops an "
                             "OSM POI based on a dead Overture URL).")
    parser.add_argument("--url-cache-policy",
                        choices=("drop-record", "scrub-only"),
                        default="drop-record",
                        help="How to handle Overture rows with dead `ws` URLs. "
                             "'drop-record' (default) — skip the whole Overture-added "
                             "POI when its website is dead, on the theory that a "
                             "dead site usually means a dead business. "
                             "'scrub-only' — keep the record but strip the dead `ws` "
                             "field. Pass 1 enrich (OSM POI getting Overture extras) "
                             "always scrubs regardless of policy.")
    parser.add_argument("--split-find-chips", action="store_true",
                        help="Pre-slice category-index/{poi,park}.json by Find-page "
                             "chip at build time. Emits one `category-index/chip-{id}.json` "
                             "per chip plus chip entries in the manifest. places.html "
                             "loads only the chosen chip file (~MB) instead of the "
                             "full poi.json (up to 1 GB on Japan), which OOM'd Chrome "
                             "and iOS WebViews. Source of truth: cloud/chip_rules.py.")
    parser.add_argument("--xapian",
                        choices=("libzim", "builder", "none"),
                        default="libzim",
                        help="How to produce the X/fulltext/xapian and "
                             "X/title/xapian indexes. "
                             "'libzim' (default) — emit search/<slug>.html "
                             "stubs and let libzim's auto-indexer build the "
                             "Xapian DBs at finalize. The 2026-05 baseline "
                             "behaviour. "
                             "'builder' — skip the HTML stubs entirely; "
                             "stream the search-feature JSONL through the "
                             "external `xapianbuilder` helper "
                             "(../xapianbuilder/target/release/xapianbuilder) "
                             "to produce the glass DBs on disk, then add "
                             "them to the ZIM at namespace 'X' with "
                             "compress=false. Requires --zim-builder=rust "
                             "(libzim's Creator can't accept items at the "
                             "X namespace). Saves 2-6h on continent-scale "
                             "ZIMs and 13-15 GB on Europe. "
                             "'none' — skip Xapian entirely; users search "
                             "via the in-ZIM places.html (which uses the "
                             "JSON search-data chunks). Saves another "
                             "1-2h of libzim finalize time. Kiwix's "
                             "native search bar degrades to title-prefix.")
    parser.add_argument("--kiwix-poi-pages", action="store_true",
                        help="Also give every named POI a Kiwix page, so "
                             "Kiwix's own search finds shops, stops and "
                             "sights, not "
                             "only places, parks, peaks, water and airports. "
                             "About 440 B per POI: +16%% on Luxembourg. Off by "
                             "default (docs/zimfarm.md).")
    parser.add_argument("--xapianbuilder-bin", metavar="PATH", default=None,
                        help="Path to the xapianbuilder binary. Defaults to "
                             "$XAPIANBUILDER_BIN, then "
                             "../xapianbuilder/target/release/xapianbuilder, "
                             "then ../xapianbuilder/target/debug/xapianbuilder.")
    parser.add_argument("--no-llm-bundle", action="store_true",
                        help="Skip writing category-index/{addr,poi,street}.json "
                             "(the LLM bundle). These files are hundreds of MB "
                             "to multi-GB on continent regions; the post-build "
                             "`cloud/repackage_zim.py` strips them by default. "
                             "Set this flag on direct create_osm_zim builds to "
                             "match the shipped output without an extra repack "
                             "pass. The chip-*.json files (Find page) are still "
                             "derived from poi+park records — they survive the "
                             "drop.")
    parser.add_argument("--resolve-wikidata-titles", action="store_true",
                        help="For search-index records that carry an OSM "
                             "`wikidata=` Q-ID but no `wikipedia=` tag, resolve "
                             "the Q-ID to its English Wikipedia title and fill "
                             "`w` so mcpzim can cross-link them to a Wikipedia "
                             "ZIM by title (no mcpzim change needed). Uses the "
                             "public Wikidata API by default; pass "
                             "--wikidata-title-map for an offline build. Lifts "
                             "the directly-linkable distinct-article count ~2.4x "
                             "on California. See docs/wikidata-title-resolution.md.")
    parser.add_argument("--wikidata-title-cache", metavar="JSON",
                        help="JSON cache for Q-ID->title resolutions; reused "
                             "across rebuilds so the Wikidata API is hit once.")
    parser.add_argument("--wikidata-title-map", metavar="TSV",
                        help="Offline `Q-ID<TAB>Title` map; when set, "
                             "--resolve-wikidata-titles uses it instead of the "
                             "network (air-gapped builds).")
    parser.add_argument("--bundle-wiki-articles", action="store_true",
                        help="Store full Wikipedia article pages at "
                             "wiki-article/<Title> for every linkable POI (the "
                             "`w` set + any --resolve-wikidata-titles backfill), "
                             "trimmed to a compact reader page. Lets offline "
                             "clients open + narrate articles without a separate "
                             "Wikipedia ZIM (kiwix can't deep-link across ZIMs). "
                             "~0.2-1%% size on California. Cached so rebuilds "
                             "don't re-crawl. See docs/wikidata-title-resolution.md.")
    parser.add_argument("--wiki-articles-cache", metavar="DIR", default=None,
                        help="Disk cache for fetched article HTML (default: "
                             "wiki_articles_cache/). Reused across rebuilds.")
    parser.add_argument("--wiki-images", choices=("none", "lead", "all"), default="none",
                        help="With --bundle-wiki-articles and an offline "
                             "--wiki-articles-source: also bundle each article's "
                             "images (Kiwix maxi thumbnails, stored once at "
                             "wiki-image/<sha1>.<ext>). 'lead' = the infobox/first "
                             "picture only (~12 KB/article on California), 'all' = "
                             "every non-icon image (~107 KB/article, median 3).")
    parser.add_argument("--wiki-image-max-kb", type=int, default=128,
                        help="Skip any single bundled image larger than this.")
    parser.add_argument("--wiki-images-per-article", type=int, default=12,
                        help="With --wiki-images all: cap per article (list "
                             "pages reach 80+; the median place has 3).")
    parser.add_argument("--wiki-articles-source", metavar="ZIM", default=None,
                        help="Local Wikipedia ZIM to read articles from "
                             "(offline, fast, no crawl). Omit to fetch from the "
                             "public Wikipedia API. Use a FULL enwiki ZIM for "
                             "coverage; a 'top'/subset misses long-tail POIs.")
    parser.add_argument("--spatial-chunk-scale", type=int, default=0, metavar="N",
                        help="Convert the monolithic routing graph into the "
                             "spatial SZCI/SZRC layout in-build (N = cells per "
                             "degree; 1 = 1° cells, 10 = 0.1° cells). When "
                             "set, the routing graph emits as "
                             "routing-data/graph-cells-index.bin + per-cell "
                             "graph-cell-NNNNN.bin files with cell-local node "
                             "coordinates. Replaces the post-"
                             "build `cloud/repackage_zim.py --spatial-chunk-scale N` "
                             "pass: same output bytes, but the work runs while "
                             "create_osm_zim already has the graph in memory, "
                             "saving a full unpack-repack of the ZIM. "
                             "Default 0 = monolithic graph.bin (legacy).")

    meta = parser.add_argument_group(
        "ZIM metadata (openZIM conventions)",
        "Each overrides the builder's default; lengths follow openZIM's rules "
        "and are checked before the build starts. `streetzim` (streetzim/cli.py) "
        "is the openZIM-style front end that sets these.")
    meta.add_argument("--zim-name", metavar="NAME",
                      help="ZIM Name metadata (book identity in Kiwix). "
                           "Default: osm_<name>")
    meta.add_argument("--title", help="Title metadata (max 30 characters). "
                                      "Default: 'OSM - <name>'")
    meta.add_argument("--description",
                      help="Description metadata (max 80 characters)")
    meta.add_argument("--long-description",
                      help="LongDescription metadata (max 4000 characters)")
    meta.add_argument("--creator", help="Creator metadata. "
                                        "Default: OpenStreetMap contributors")
    meta.add_argument("--publisher", help="Publisher metadata. Default: create_osm_zim")
    meta.add_argument("--tags", help="Extra tags, semicolon-delimited; added "
                                     "after the builder's own")
    meta.add_argument("--illustration", metavar="PATH_OR_URL",
                      help="Image for the 48x48 ZIM illustration (PNG, JPEG, "
                           "WebP, or SVG where zimscraperlib is installed; cropped "
                           "to fill). Default: a generated map icon")
    meta.add_argument("--scraper", help=argparse.SUPPRESS)
    meta.add_argument("--flavour", help="Flavour metadata. Default: maxi")
    meta.add_argument("--stats-filename", metavar="PATH",
                      help="Write Zimfarm progress JSON ({\"done\": N, "
                           "\"total\": M}) here as the build moves through "
                           "its phases")
    return parser


def _openzim_options(*, args):
    """Retired flags, and the openZIM metadata flags validated before the build."""
    if args.split_graph:
        raise SystemExit("Error: --split-graph (SZRG v5) was retired; large "
                         "regions use --spatial-chunk-scale N instead. See "
                         "docs/formats.md, 'Version support and retirement'.")

    # Validate the openZIM metadata now, not after a multi-hour build.
    zim_metadata = zim_illustration = None
    try:
        satellite_sources.check_flavour(
            args.flavour,
            satellite_sources.get(args.satellite_source) if args.satellite else None)
    except ValueError as e:
        raise SystemExit(f"Error: {e}") from None
    if any(getattr(args, k) is not None for k in (
            "zim_name", "title", "description", "long_description", "creator",
            "publisher", "tags", "scraper", "flavour")):
        from streetzim.zim_metadata import build_overrides
        try:
            zim_metadata = build_overrides(
                name=args.zim_name, title=args.title,
                description=args.description,
                long_description=args.long_description, creator=args.creator,
                publisher=args.publisher, tags=args.tags, scraper=args.scraper,
                flavour=args.flavour)
        except ValueError as e:
            raise SystemExit(f"Error: {e}") from None
    if args.illustration:
        from streetzim.zim_metadata import load_illustration
        try:
            zim_illustration = load_illustration(args.illustration)
        except (OSError, ValueError) as e:
            raise SystemExit(f"Error: illustration {args.illustration!r}: {e}") from None
    stats = None
    if args.stats_filename:
        from streetzim.progress import StatsFile
        stats = StatsFile(args.stats_filename).attach()
    return stats, zim_illustration, zim_metadata


def _resolve_area(*, args, parser):
    """The area (preset, Geofabrik path, PBF, bbox), map name and output path."""
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

    if bbox_str:
        # One spelling from here on for a box crossing the antimeridian:
        # unwrapped, east past 180 (streetzim/area.py). Others unchanged.
        _bb = parse_bbox(bbox_str)
        if _area.crosses(_bb):
            bbox_str = _area.to_str(_bb)

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
    return bbox_str, geofabrik_path, name, output_path, pbf_path


def _layer_options(*, args, bbox_str):
    """Satellite, terrain, Wikidata and routing options."""
    # Satellite options
    include_satellite = args.satellite
    satellite_max_zoom = (args.satellite_zoom if args.satellite_zoom is not None
                          else args.max_zoom)
    # Latitude-aware satellite cap.
    #
    # The imagery is Sentinel-2 Cloudless, natively 10 m/pixel. A 256 px tile
    # at z14 is 156543*cos(lat)/2^14 m/pixel: 9.6 m at the equator (about the
    # source resolution), 7.8 m at Casablanca, 5.2 m at Riga. So above the
    # tropics z14 is not extra detail, it is interpolation -- and it costs
    # 63% of the satellite payload, which is 18-24% of a ZIM and does not
    # compress (AVIF already).
    #
    # Measured at Riga: a z14 tile against its z13 parent upscaled is
    # PSNR 29.4 dB, and part of that gap is AVIF noise rather than detail.
    # Clients overzoom the deepest level automatically, so capping at z13
    # softens the deepest view rather than removing the layer.
    #
    # Cut at 45 deg on the bbox CENTRE, not its nearest edge. Testing the
    # nearest edge capped only 5 small regions: russia (41-82N) and canada
    # both keep z14 off a southern edge at ~41 deg although nearly all their
    # area is far north. The centre is a fair proxy for where a region's
    # tiles actually are, and it selects europe, russia, canada, alaska,
    # ukraine, iceland, the baltics and switzerland -- 137 GB of the 424 GB
    # being rebuilt. An explicit --satellite-zoom always wins.
    # NB: `bbox` is not parsed until much later in main(); use bbox_str, which
    # is final by this point (args, then the area lookup above).
    if (include_satellite and args.satellite_zoom is None and bbox_str
            and satellite_max_zoom is not None and satellite_max_zoom > 13):
        try:
            _bb = parse_bbox(bbox_str)
            _lat_ctr = abs((float(_bb[1]) + float(_bb[3])) / 2.0)
            if _lat_ctr >= 45.0:
                print(f"    satellite: capping z{satellite_max_zoom} -> z13 "
                      f"(centred at {_lat_ctr:.0f} deg; Sentinel-2 is 10 m/px, "
                      f"so z14 there is upsampled)", flush=True)
                satellite_max_zoom = 13
        except Exception as _e:   # never fail a build over a progress nicety
            print(f"    satellite: latitude cap skipped ({_e})", flush=True)
    satellite_download_zoom = (args.satellite_download_zoom
                               if args.satellite_download_zoom is not None
                               else satellite_max_zoom)
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
    return include_routing, include_satellite, include_terrain, include_wikidata, satellite_download_zoom, satellite_format, satellite_max_zoom, satellite_quality, satellite_tile_size, terrain_max_zoom, total_steps, wikidata_cache_dir


def _acquire_tiles(*, args, bbox_str, geofabrik_path, pbf_path, tmpdir, total_steps):
    """Steps 1-2: the OSM extract (downloaded, or cut to the bbox) and its vector tiles, or an existing MBTiles."""
    work_pbf = None
    # True when work_pbf is our own cut of the extract to bbox_str: the
    # address, wiki-tag and routing steps then skip cutting it again (each
    # osmium extract took 3.7 GB even for Luxembourg).
    work_pbf_cut = False
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
            work_pbf_cut = True
        elif bbox_str and args.area and geofabrik_path != KNOWN_AREAS.get(args.area.lower().replace(" ", "-"), {}).get("geofabrik"):
            work_pbf = os.path.join(tmpdir, "area.osm.pbf")
            extract_bbox_from_pbf(source_pbf, bbox_str, work_pbf)
            work_pbf_cut = True
        else:
            work_pbf = source_pbf

        # Step 3: Generate vector tiles
        print()
        print(f"[2/{total_steps}] Generating vector tiles...")
        mbtiles_path = os.path.join(tmpdir, "tiles.mbtiles")
        generate_tiles(work_pbf, mbtiles_path, bbox=bbox_str,
                       fast=args.fast, store=args.store)
    return mbtiles_path, work_pbf, work_pbf_cut


def _process_tiles(*, args, mbtiles_path, total_steps):
    """Step 3: read the tiles (streamed when the MBTiles is large) and fetch the font glyphs."""
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
        tiles, tile_metadata = extract_tiles_from_mbtiles(
            mbtiles_path, max_zoom=args.max_zoom)
        total_tile_count = len(tiles)

    # Generate font glyphs (with fallback glyphs for the scripts the labels use)
    fonts = generate_sdf_font_glyphs(scripts=fallback_scripts_in_tiles(tiles))
    return fonts, tile_metadata, tiles, total_tile_count, use_streaming


def _build_search(
        *, args, bbox_str, mbtiles_path, pbf_path, tiles, tmpdir, total_steps,
        use_streaming, work_pbf, work_pbf_cut=False):
    """Step 4: search features (from a cache or the tiles), addresses, Wikipedia cross-refs and Overture enrichment."""
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
            with open(search_cache_path) as fin, open(filtered_path, "w") as fout:
                for line in fin:
                    total += 1
                    feat = json.loads(line)
                    lat, lon = feat["lat"], feat["lon"]
                    if minlat <= lat <= maxlat and _area.contains_lon(bbox, lon):
                        fout.write(line)
                        kept += 1
                    if total % 5_000_000 == 0:
                        print(f"\r    Filtered {total} features, kept {kept}...", end="", flush=True)
            print(f"\r    Filtered {kept}/{total} features within bbox          ", flush=True)
            search_features = filtered_path
        else:
            # No bbox — use the whole cache, copy to tmpdir.
            # NOTE: do NOT `import shutil` here. shutil is already
            # imported at module level (line 36); a local import binds
            # the name as a local for the WHOLE of main(), so when this
            # branch does not run, main()'s `finally: shutil.rmtree(...)`
            # raises UnboundLocalError and the process exits non-zero
            # AFTER a completely successful build. build-refresh-queue.sh
            # reads that rc as BUILD FAILED and discards the region
            # (caught 2026-09-18 on switzerland-nosat; the ZIM was valid).
            filtered_path = os.path.join(tmpdir, "search_features.jsonl")
            shutil.copy2(search_cache_path, filtered_path)
            print("    Using all features (no bbox filter)")
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
        addr_pbf = work_pbf or pbf_path or args.pbf
        if addr_pbf:
            addr_bbox = parse_bbox(bbox_str) if bbox_str else None
            addr_precut = bool(work_pbf_cut and addr_pbf == work_pbf)
            if args.skip_address_extract:
                print("    [--skip-address-extract] reusing cached addresses + overture enrichment")
            else:
                address_count = extract_addresses_pbf(
                    addr_pbf, search_features, bbox=addr_bbox, precut=addr_precut) or 0
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
                    from streetzim.source_report import note
                    note("Overture addresses",
                         f"{merge_result.get('scanned', '?')} rows "
                         f"({merge_result.get('added', 0)} added)")
                    overture_datasets.update(merge_result.get("datasets") or [])
                    overture_themes.append("addresses")
                except Exception as _e:
                    # The theme was explicitly requested; shipping a
                    # ZIM without it (and with hasOvertureAddresses
                    # false, so the validator skips the check) is a
                    # silent regression. Fail the build.
                    raise SystemExit(
                        f"Overture addresses merge failed: {_e} — "
                        f"fix the input or drop --overture-addresses") from _e
            if args.overture_places and not args.skip_address_extract:
                try:
                    _url_cache = _load_url_cache(args.url_cache)
                    if args.url_cache:
                        print(f"  URL cache: {len(_url_cache)} entries from "
                              f"{args.url_cache} "
                              f"(policy={args.url_cache_policy})",
                              flush=True)
                    places_result = merge_overture_places(
                        args.overture_places, search_features,
                        bbox=addr_bbox,
                        url_cache=_url_cache,
                        url_cache_policy=args.url_cache_policy)
                    overture_datasets.update(places_result.get("datasets") or [])
                    overture_themes.append("places")
                    from streetzim.source_report import note
                    note("Overture places",
                         f"{places_result.get('enriched', 0)} enriched, "
                         f"{places_result.get('added', 0)} added")
                except Exception as _e:
                    raise SystemExit(
                        f"Overture places merge failed: {_e} — "
                        f"fix the input or drop --overture-places") from _e
            if overture_datasets:
                overture_sources = sorted(overture_datasets)
            elif args.skip_address_extract:
                # Salvage rebuild: ``merge_overture_*`` was skipped, so
                # neither overture_themes nor overture_sources got
                # populated. But the cached search jsonl was generated
                # by a prior build that DID merge overture, and those
                # records (tagged ``"source":"overture"``) are now in
                # the search index of this ZIM. Without an
                # overture-sources.json entry, the static link in
                # ``index.html`` points at a missing file — zimcheck
                # flags it as a broken internal link, validate_zim.py
                # rejects the ZIM, and uploads abort. The runtime
                # conditional in the viewer also stays off (because
                # streetzim-meta:hasOvertureAddresses is False), so
                # users searching for Overture POIs see them but the
                # viewer's Sources panel doesn't credit Overture —
                # an attribution bug as well as a validation bug.
                # Sample the cache for overture markers; if found,
                # emit a stub credits doc that points users at the
                # canonical Overture credits URL for the upstream
                # dataset list (which the salvage cache doesn't
                # retain).
                sampled_themes = _sample_overture_themes_in_cache(
                    search_features)
                if sampled_themes:
                    overture_themes = sampled_themes
                    overture_sources = ["__salvage_inherited__"]
                    print(f"    [--skip-address-extract] overture content "
                          f"detected in cache (themes={sampled_themes}); "
                          f"will emit stub overture-sources.json", flush=True)
            # Same PBF feeds the wiki-tag lookup so the chunker can enrich
            # POI records with wikipedia/wikidata for offline cross-ref.
            try:
                wiki_cross_refs = extract_wiki_tags_pbf(addr_pbf, bbox=addr_bbox,
                                                        precut=addr_precut)
            except Exception as _e:
                print(f"    Warning: wiki cross-ref extraction failed: {_e}")
                wiki_cross_refs = None
            # Optionally backfill `wikipedia` from `wikidata` so records
            # that carry only a Q-ID become title-linkable to a Wikipedia
            # ZIM (the chunker writes the filled title into rec["w"]).
            if getattr(args, "resolve_wikidata_titles", False) and wiki_cross_refs:
                try:
                    from cloud.wikidata_titles import augment_wiki_cross_refs
                    _t = augment_wiki_cross_refs(
                        wiki_cross_refs,
                        cache_path=getattr(args, "wikidata_title_cache", None),
                        offline_map=getattr(args, "wikidata_title_map", None),
                    ) or {}
                    from streetzim.source_report import note
                    note("Wikipedia titles", f"{_t.get('resolved', '?')}/"
                         f"{_t.get('distinct_qids', '?')} Q-IDs resolved")
                except Exception as _e:
                    print(f"    Warning: wikidata->title resolution failed: {_e}")
    return address_count, overture_sources, overture_themes, search_features, wiki_cross_refs


def _build_wikidata(
        *, args, include_wikidata, mbtiles_path, pbf_path, total_steps,
        wikidata_cache_dir, work_pbf):
    """Wikidata place details for the Q-IDs in the extract."""
    # Build Wikidata cache if requested
    wikidata_data = None
    if include_wikidata:
        step_wd = 5
        print()
        print(f"[{step_wd}/{total_steps}] Building Wikidata info cache...")
        from wikidata_cache import build_cache as wd_build_cache, load_cache_for_zim

        # Determine PBF path for Q-ID extraction (PBF preferred — has wikidata tags)
        wd_pbf = work_pbf or pbf_path or args.pbf
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
        from streetzim.source_report import note
        note("Wikidata", f"{len(wikidata_data or {})} entries")
    return wikidata_data


def _build_routing(
        *, args, bbox_str, include_routing, include_wikidata, pbf_path, tmpdir,
        total_steps, work_pbf, work_pbf_cut=False):
    """The routing graph (SZRG v4) from the extract."""
    # Extract routing graph if requested
    routing_graph_path = None
    if include_routing:
        step_rt = 5 + (1 if include_wikidata else 0)
        print()
        print(f"[{step_rt}/{total_steps}] Extracting routing graph...")
        rt_pbf = work_pbf or pbf_path or args.pbf
        if not rt_pbf:
            print("    Warning: no PBF file available, skipping routing graph")
            print("    (routing requires a PBF file — not available with --mbtiles only)")
        else:
            rt_bbox = parse_bbox(bbox_str) if bbox_str else None
            routing_graph_path = extract_routing_graph(
                rt_pbf, tmpdir, bbox=rt_bbox,
                precut=bool(work_pbf_cut and rt_pbf == work_pbf))
    return routing_graph_path


def _satellite_and_terrain(
        *, args, bbox_str, include_routing, include_satellite, include_terrain,
        include_wikidata, satellite_download_zoom, satellite_format, satellite_quality,
        satellite_tile_size, terrain_max_zoom, total_steps):
    """Satellite and terrain tiles, in parallel when both are requested."""
    # Download satellite tiles and generate terrain tiles
    # These are independent (satellite=I/O-bound, terrain=CPU-bound) so run in parallel
    satellite_dir = None
    terrain_dir = None
    sat_future = None
    terrain_future = None

    if include_satellite and bbox_str:
        # A cache per source, format and size, so none of them is mixed.
        satellite_dir = satellite_cache_dirs(
            args.satellite_source, satellite_format, satellite_tile_size)[1]
    if include_terrain and bbox_str:
        terrain_dir = args.terrain_dir or os.path.join(CACHE_DIR, "terrain_cache")

    # Satellite and terrain come after the Wikidata and routing phases.
    step_sat = 5 + (1 if include_wikidata else 0) + (1 if include_routing else 0)
    if include_satellite and include_terrain and bbox_str:
        from concurrent.futures import ThreadPoolExecutor as StepPool
        print()
        print(f"[{step_sat}/{total_steps}] Downloading satellite tiles + generating terrain tiles (parallel)...")

        with StepPool(max_workers=2) as step_pool:
            sat_future = step_pool.submit(
                download_satellite_tiles, bbox_str, satellite_dir, satellite_download_zoom,
                sat_format=satellite_format, sat_quality=satellite_quality,
                tile_size=satellite_tile_size, source=args.satellite_source)
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
            print(f"[{step_sat}/{total_steps}] Downloading satellite tiles...")
            if not bbox_str:
                print("    Warning: no bbox specified, skipping satellite tiles")
            else:
                download_satellite_tiles(bbox_str, satellite_dir, max_zoom=satellite_download_zoom,
                                         sat_format=satellite_format, sat_quality=satellite_quality,
                                         tile_size=satellite_tile_size,
                                         source=args.satellite_source)

        if include_terrain:
            step_terrain = step_sat + (1 if include_satellite else 0)
            print()
            print(f"[{step_terrain}/{total_steps}] Generating terrain tiles...")
            if not bbox_str:
                print("    Warning: no bbox specified, skipping terrain tiles")
            else:
                generate_terrain_tiles(bbox_str, terrain_dir,
                    max_zoom=terrain_max_zoom,
                    low_zoom_world_vrt=getattr(args, "low_zoom_world_vrt", None))
    return satellite_dir, terrain_dir


def _verify_terrain(*, args, bbox_str, include_terrain, terrain_dir, terrain_max_zoom,
                    terrain_min_zoom=None):
    """Terrain audit: regenerate missing and seam tiles, fail on remaining gaps."""
    # Verify terrain completeness — regen missing tiles AND fix boundary
    # seam tiles before packaging. Boundary tiles (straddling 1-degree DEM
    # cell edges) may have partial zero data if generated from a VRT that
    # didn't include all neighboring cells.
    world_vrt = getattr(args, "low_zoom_world_vrt", None)
    if include_terrain and bbox_str and terrain_min_zoom is None:
        # From the whole area, as generate_terrain_tiles does.
        terrain_min_zoom = _terrain.terrain_min_zoom(
            parse_bbox(bbox_str), terrain_max_zoom, world_vrt)
    if include_terrain and bbox_str and terrain_dir and _area.crosses(parse_bbox(bbox_str)):
        # Across the antimeridian: each side, as it was generated.
        for part in _area.split(parse_bbox(bbox_str)):
            _verify_terrain(args=args, bbox_str=_area.to_str(part),
                            include_terrain=include_terrain, terrain_dir=terrain_dir,
                            terrain_max_zoom=terrain_max_zoom,
                            terrain_min_zoom=terrain_min_zoom)
        return
    if include_terrain and bbox_str and terrain_dir:
        plan = _terrain.TerrainPlan(parse_bbox(bbox_str), terrain_max_zoom,
                                    terrain_min_zoom, world_vrt)
        if plan.fresh:
            # No world DEM (every `streetzim` build): each tile is checked
            # against the mosaic it was made from (streetzim/terrain.py).
            _terrain.audit_terrain(plan, terrain_dir)
            return
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
                if _dem_tif_is_usable(_p):
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
                # gdalbuildvrt is absent on this host, so this used to skip
                # verification entirely — the seam repair and the tiny-tile
                # safety net below have been dead here for months, which is
                # the same silent-skip that hid the Hispaniola mosaic bug.
                # Build the VRT ourselves instead.
                print("    gdalbuildvrt not found; building verification VRT directly")
                _build_dem_vrt(all_tifs_v, vrt_path)
            finally:
                os.unlink(flist_path)

        if os.path.isfile(vrt_path):
            print("    Verifying terrain tiles (missing + boundary seams)...")
            repair_tiles = []
            for z in range(0, terrain_max_zoom + 1):
                for t in mercantile.tiles(*bbox_parsed, zooms=z):
                    tile_path = os.path.join(terrain_dir, str(z), str(t.x), f"{t.y}.webp")
                    bounds = mercantile.bounds(t)
                    needs_regen = False
                    if not _terrain_tile_usable(tile_path):
                        # Same rule as the generator. A bare size test here
                        # condemned every legitimate 44-byte ocean tile and
                        # so "repaired" all of Hawaii on every build.
                        needs_regen = True
                    elif z >= 10:
                        # Check if tile straddles a 1-degree boundary
                        crosses_lon = _math.floor(bounds.west) != _math.floor(bounds.east)
                        crosses_lat = _math.floor(bounds.south) != _math.floor(bounds.north)
                        if crosses_lon or crosses_lat:
                            needs_regen = True
                    if needs_regen:
                        # Low zooms must come from the world VRT when
                        # one was given: the bbox+1° verify VRT is
                        # zero outside its extent, which painted the
                        # "65°E stripe" onto z0-9 tiles that reach far
                        # beyond the region.
                        repair_src = _terrain_vrt_for_zoom(
                            z, vrt_path,
                            low_zoom_world_vrt=getattr(args, "low_zoom_world_vrt", None))
                        repair_tiles.append(
                            (repair_src, t.x, t.y, z, terrain_dir,
                             bounds.west, bounds.south, bounds.east, bounds.north)
                        )
            if repair_tiles:
                print(f"    Repairing {len(repair_tiles)} tiles (missing + boundary)...")
                # spawn, not fork: _generate_one_terrain_tile keeps its
                # DEM dataset open in _DEM_HANDLES, and the parent has its
                # own open (low zooms are rasterised in-process). Forking
                # would hand four children the SAME GDAL dataset and its
                # shared file offset, so they would race on seeks and read
                # each other's blocks.
                import multiprocessing as _mp
                _ctx = _mp.get_context("spawn")
                with _ctx.Pool(min(4, build_cpus())) as pool:
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
                    zero_code = int(10000.0 / 0.1)  # encoded value for 0 m
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


def _verified_maplibre(*, total_steps):
    """MapLibre GL JS and CSS: the vendored copy, checked against the lock
    file (nothing is downloaded)."""
    step_maplibre = total_steps - 1
    print()
    print(f"[{step_maplibre}/{total_steps}] Checking MapLibre GL JS...")
    maplibre_js, maplibre_css = vendored_maplibre()
    return maplibre_css, maplibre_js


def _build_map_config(
        *, args, bbox_str, name, overture_sources, routing_graph_path, satellite_dir,
        satellite_format, satellite_max_zoom, satellite_tile_size, search_features,
        terrain_dir, terrain_max_zoom, total_steps, wiki_cross_refs, wikidata_data):
    """The ZIM-file step header and map-config.json: centre, zoom, bounds and layer flags."""
    # Create ZIM
    step_zim = total_steps
    print()
    print(f"[{step_zim}/{total_steps}] Building ZIM file...")

    # Build map config
    bbox = parse_bbox(bbox_str) if bbox_str else None
    if bbox:
        center, zoom = get_center_and_zoom(bbox)
        # Prefer the median place position over the bbox centre; see
        # center_from_places(). Zoom stays extent-based.
        _anchor = registry_anchor(bbox)
        _place_center = None if _anchor else center_from_places(
            search_features, bbox)
        if _anchor or _place_center:
            _new = _anchor[0] if _anchor else _place_center
            _src = f"registry anchor ({_anchor[1]})" if _anchor else "place median"
            _dist = math.hypot(_new[0] - center[0], _new[1] - center[1])
            print(f"    opening centre: {_new} from {_src} "
                  f"(bbox centre was {[round(c, 5) for c in center]}, "
                  f"{_dist:.1f} deg away)", flush=True)
            center = _new
    else:
        center = [0, 0]
        zoom = 2
    if args.map_center:
        try:
            lon, lat = (float(x) for x in args.map_center.split(","))
            center = [lon, lat]
        except (ValueError, TypeError) as e:
            raise SystemExit(
                f"--map-center {args.map_center!r} must be 'LON,LAT': {e}"
            ) from None
    if args.map_zoom is not None:
        zoom = args.map_zoom

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
        # Which mosaic, its licence and the credit EOX requires.
        map_config.update(satellite_sources.map_config(
            satellite_sources.get(args.satellite_source)))
    if terrain_dir and os.path.isdir(str(terrain_dir)):
        map_config["hasTerrain"] = True
        map_config["terrainMaxZoom"] = terrain_max_zoom
        if bbox:
            # Without a world DEM, terrain starts at the lowest zoom the
            # viewer can show (streetzim/terrain.py); the viewer's DEM source
            # and the packer start there too. 0 (the production layout) is
            # left out, so those map-configs are unchanged.
            _tmin = _terrain.terrain_min_zoom(
                bbox, terrain_max_zoom, getattr(args, "low_zoom_world_vrt", None))
            if _tmin:
                map_config["terrainMinZoom"] = _tmin
    if wikidata_data:
        map_config["hasWikidata"] = True
    # hasWikiArticles is set by create_zim, once it knows whether any
    # article was actually stored.
    if routing_graph_path:
        map_config["hasRouting"] = True
    if overture_sources:
        # Surface the flag so the viewer's Sources panel can show the
        # Overture attribution section. The concrete dataset list is
        # shipped as overture-sources.json at the ZIM root (below).
        map_config["hasOvertureAddresses"] = True
    return bbox, map_config


def _overture_releases(args, overture_themes):
    """{theme: release} of the Overture parquets merged, for
    overture-sources.json (read from the parquet, or its file name)."""
    paths = {"addresses": getattr(args, "overture_addresses", None),
             "places": getattr(args, "overture_places", None)}
    return {t: overture_release(paths.get(t)) for t in (overture_themes or [])}


def _write_zim(
        *, address_count, args, bbox_str, fonts, map_config, maplibre_css, maplibre_js,
        mbtiles_path, name, output_path, overture_sources, overture_themes,
        routing_graph_path, satellite_dir, satellite_format, satellite_max_zoom,
        search_features, terrain_dir, terrain_max_zoom, tile_metadata, tiles, tmpdir,
        total_tile_count, use_streaming, wiki_cross_refs, wikidata_data,
        zim_illustration, zim_metadata):
    """Merge street pieces, then write the ZIM; remove a partial file on failure."""
    if isinstance(search_features, str) and os.path.isfile(search_features):
        # After every filter and merge that rewrites the file (bbox cut of
        # a search cache, addresses, Overture), so a street is merged
        # from the pieces inside this region only.
        from streetzim.search_extract import merge_streets_in_file
        merge_streets_in_file(search_features)
    _out_before = os.path.exists(output_path)
    try:
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
        routing_graph_chunk_mb=int(getattr(args, 'chunk_graph_mb', 0) or 0),
        split_hot_search_chunks_mb=int(getattr(args, 'split_hot_search_chunks_mb', 0) or 0),
        split_find_chips=bool(getattr(args, 'split_find_chips', False)),
        wiki_cross_refs=wiki_cross_refs,
        overture_sources=overture_sources,
        overture_themes=overture_themes,
        overture_release=_overture_releases(args, overture_themes),
        address_count=address_count,
        zim_builder=getattr(args, "zim_builder", "python"),
        max_zoom=args.max_zoom,
        xapian_mode=getattr(args, "xapian", "libzim"),
        kiwix_poi_pages=bool(getattr(args, "kiwix_poi_pages", False)),
        xapianbuilder_bin=getattr(args, "xapianbuilder_bin", None),
        xapian_workdir=tmpdir,
        no_llm_bundle=bool(getattr(args, "no_llm_bundle", False)),
        spatial_chunk_scale=int(getattr(args, "spatial_chunk_scale", 0) or 0),
        bundle_wiki_articles=bool(getattr(args, "bundle_wiki_articles", False)),
        wiki_articles_cache=getattr(args, "wiki_articles_cache", None),
        wiki_articles_source=getattr(args, "wiki_articles_source", None),
        wiki_images=getattr(args, "wiki_images", "none"),
        wiki_image_max_kb=getattr(args, "wiki_image_max_kb", 128),
        wiki_images_per_article=getattr(args, "wiki_images_per_article", 12),
        metadata=zim_metadata,
        illustration=zim_illustration,
        )
    except BaseException:
        # libzim's Creator.__exit__ finalises on exception, so an
        # aborted build (corrupt tile, OOM, Ctrl-C) used to leave a
        # truncated-but-readable ZIM at the output path — exactly
        # where the queue scripts look for a finished build.
        if not _out_before and os.path.exists(output_path):
            try:
                os.unlink(output_path)
                print(f"    removed partial output {output_path}", flush=True)
            except OSError:
                pass
        raise


def _print_summary(*, bbox, name, output_path, stats, total_tile_count):
    """Progress file, phase timing table and the closing summary."""
    if stats:
        stats.finish()
    # Stop the phase timer (no further phases will be printed
    # after this) and emit the summary table for post-mortem.
    PHASE_TIMER.stop()
    summary = PHASE_TIMER.summary()
    if summary:
        print()
        print("=== Phase timing ===")
        print(summary)

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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cpus is not None and args.cpus < 1:
        parser.error("--cpus must be at least 1")
    _cpus.set_build_cpus(args.cpus)
    stats, zim_illustration, zim_metadata = _openzim_options(args=args)

    bbox_str, geofabrik_path, name, output_path, pbf_path = _resolve_area(
        args=args, parser=parser)

    (include_routing, include_satellite, include_terrain, include_wikidata,
     satellite_download_zoom, satellite_format, satellite_max_zoom, satellite_quality,
     satellite_tile_size, terrain_max_zoom, total_steps, wikidata_cache_dir) = _layer_options(
        args=args, bbox_str=bbox_str)

    if stats:
        stats.write(0, total_steps)
    print(f"=== Creating Offline OSM ZIM: {name} ===")
    print(f"  CPU cores: {build_cpus()} ("
          + ("--cpus" if args.cpus is not None else _cpus.detect()[1]) + ")")
    if include_satellite:
        sat_desc = f"{satellite_format} q{satellite_quality} {satellite_tile_size}px"
        _src = satellite_sources.get(args.satellite_source)
        print(f"  Including Sentinel-2 satellite imagery (z0-{satellite_max_zoom}, {sat_desc}); "
              f"{_src.key}, {_src.license}")
        if _src.noncommercial:
            print("  " + "!" * 72)
            print(f"  !! NON-COMMERCIAL: {_src.key} imagery is {_src.license}. This ZIM may")
            print("  !! only be used and redistributed for non-commercial purposes.")
            print("  " + "!" * 72)
    if include_terrain:
        _tmin = (_terrain.terrain_min_zoom(parse_bbox(bbox_str), terrain_max_zoom,
                                           getattr(args, "low_zoom_world_vrt", None))
                 if bbox_str else 0)
        print(f"  Including Copernicus DEM terrain (z{_tmin}-{terrain_max_zoom})")
    if include_wikidata:
        print("  Including Wikidata info for places and POIs")
    if include_routing:
        print("  Including offline routing graph")
    print()
    log_viewer_freshness()

    # Create temp directory
    tmpdir = tempfile.mkdtemp(prefix="osm_zim_")
    try:
        mbtiles_path, work_pbf, work_pbf_cut = _acquire_tiles(
            args=args, bbox_str=bbox_str, geofabrik_path=geofabrik_path,
            pbf_path=pbf_path, tmpdir=tmpdir, total_steps=total_steps)

        (fonts, tile_metadata, tiles, total_tile_count, use_streaming) = _process_tiles(
            args=args, mbtiles_path=mbtiles_path, total_steps=total_steps)

        (address_count, overture_sources, overture_themes, search_features,
         wiki_cross_refs) = _build_search(
            args=args, bbox_str=bbox_str, mbtiles_path=mbtiles_path, pbf_path=pbf_path,
            tiles=tiles, tmpdir=tmpdir, total_steps=total_steps,
            use_streaming=use_streaming, work_pbf=work_pbf, work_pbf_cut=work_pbf_cut)

        wikidata_data = _build_wikidata(
            args=args, include_wikidata=include_wikidata, mbtiles_path=mbtiles_path,
            pbf_path=pbf_path, total_steps=total_steps,
            wikidata_cache_dir=wikidata_cache_dir, work_pbf=work_pbf)

        routing_graph_path = _build_routing(
            args=args, bbox_str=bbox_str, include_routing=include_routing,
            include_wikidata=include_wikidata, pbf_path=pbf_path, tmpdir=tmpdir,
            total_steps=total_steps, work_pbf=work_pbf, work_pbf_cut=work_pbf_cut)

        satellite_dir, terrain_dir = _satellite_and_terrain(
            args=args, bbox_str=bbox_str, include_routing=include_routing,
            include_satellite=include_satellite, include_terrain=include_terrain,
            include_wikidata=include_wikidata,
            satellite_download_zoom=satellite_download_zoom,
            satellite_format=satellite_format, satellite_quality=satellite_quality,
            satellite_tile_size=satellite_tile_size, terrain_max_zoom=terrain_max_zoom,
            total_steps=total_steps)

        _verify_terrain(
            args=args, bbox_str=bbox_str, include_terrain=include_terrain,
            terrain_dir=terrain_dir, terrain_max_zoom=terrain_max_zoom)

        # NOTE: No size-threshold satellite audit — legitimate deep-ocean
        # Sentinel-2 imagery compresses to ~300-500 bytes (dark near-black RGB).
        # A stricter content-based check (pure uniform RGB → broken) could be
        # added later, but tile-size alone is not a valid signal for satellite.

        maplibre_css, maplibre_js = _verified_maplibre(
            total_steps=total_steps)

        bbox, map_config = _build_map_config(
            args=args, bbox_str=bbox_str, name=name, overture_sources=overture_sources,
            routing_graph_path=routing_graph_path, satellite_dir=satellite_dir,
            satellite_format=satellite_format, satellite_max_zoom=satellite_max_zoom,
            satellite_tile_size=satellite_tile_size, search_features=search_features,
            terrain_dir=terrain_dir, terrain_max_zoom=terrain_max_zoom,
            total_steps=total_steps, wiki_cross_refs=wiki_cross_refs,
            wikidata_data=wikidata_data)
        if args.mbtiles and args.record_tile_source:
            from streetzim import mbtiles as _mbt
            map_config["tileSource"] = _mbt.source_record(
                tile_metadata, args.tile_source_url)

        _write_zim(
            address_count=address_count, args=args, bbox_str=bbox_str, fonts=fonts,
            map_config=map_config, maplibre_css=maplibre_css, maplibre_js=maplibre_js,
            mbtiles_path=mbtiles_path, name=name, output_path=output_path,
            overture_sources=overture_sources, overture_themes=overture_themes,
            routing_graph_path=routing_graph_path, satellite_dir=satellite_dir,
            satellite_format=satellite_format, satellite_max_zoom=satellite_max_zoom,
            search_features=search_features, terrain_dir=terrain_dir,
            terrain_max_zoom=terrain_max_zoom, tile_metadata=tile_metadata,
            tiles=tiles, tmpdir=tmpdir, total_tile_count=total_tile_count,
            use_streaming=use_streaming, wiki_cross_refs=wiki_cross_refs,
            wikidata_data=wikidata_data, zim_illustration=zim_illustration,
            zim_metadata=zim_metadata)

        _print_summary(
            bbox=bbox, name=name, output_path=output_path, stats=stats,
            total_tile_count=total_tile_count)

    finally:
        if stats:
            stats.detach()
        if not args.keep_temp:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"\nTemp files kept at: {tmpdir}")


if __name__ == "__main__":
    main()
