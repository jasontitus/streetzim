#!/usr/bin/env python3
"""Verify tile cache completeness for a bbox.

For a given bounding box, enumerate all expected tiles at each zoom level
and verify they exist on disk with a reasonable minimum size. Supports both
the satellite_cache_avif_256/ and terrain_cache/ caches. Terrain mode skips
ocean tiles (where there's no DEM data, so no tile is expected).

Usage:
    # Satellite (default)
    python3 verify_satellite_cache.py --region united-states
    python3 verify_satellite_cache.py --region europe --delete-stubs
    python3 verify_satellite_cache.py --region africa --list-missing > africa-missing.txt

    # Terrain
    python3 verify_satellite_cache.py --type terrain --region united-states
    python3 verify_satellite_cache.py --type terrain --region europe --delete-stubs

    # Custom bbox
    python3 verify_satellite_cache.py --bbox="-125.0,24.4,-66.9,49.4" --max-zoom 14
"""
import argparse
import math
import multiprocessing
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SATELLITE_CACHE = os.path.join(SCRIPT_DIR, "satellite_cache_avif_256")
TERRAIN_CACHE   = os.path.join(SCRIPT_DIR, "terrain_cache")
DEM_SOURCES     = os.path.join(TERRAIN_CACHE, "dem_sources")
MIN_TILE_SIZE = 200  # bytes; anything smaller is almost certainly a broken stub

# Pre-defined region bboxes (keep in sync with cloud/launch-build-vm.sh)
REGIONS = {
    "united-states":     "-125.0,24.4,-66.9,49.4",
    "midwest-us":        "-104.1,36.0,-80.5,49.4",
    "washington-dc":     "-77.12,38.79,-76.91,38.99",
    "california":        "-124.48,32.53,-114.13,42.01",
    "colorado":          "-109.06,36.99,-102.04,41.00",
    "europe":            "-25.0,34.5,45.0,72.0",
    "west-asia":         "25.0,12.0,62.5,42.0",
    "iran":              "44.0,25.0,63.5,39.8",
    "indian-subcontinent": "60.0,5.0,97.5,37.0",
    "africa":            "-18.0,-35.0,52.0,38.0",
    "asia":              "25.0,-12.0,180.0,82.0",
    "south-america":     "-82.0,-56.0,-34.0,13.0",
    "oceania":           "110.0,-50.0,180.0,0.0",
    "japan":             "122.9,24.0,146.0,45.6",
    "hispaniola":        "-74.5,17.5,-68.3,20.1",
}


def parse_bbox(s):
    """Parse 'west,south,east,north'."""
    parts = [float(p) for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bad bbox: {s}")
    return tuple(parts)


def tile_range(bbox, z):
    """Return (x_min, x_max, y_min, y_max) tile indices at zoom z for bbox."""
    w, s, e, n = bbox
    n_tiles = 2 ** z
    x_min = max(0, int(n_tiles * (w + 180) / 360))
    x_max = min(n_tiles - 1, int(n_tiles * (e + 180) / 360))
    lat_rad_s = math.radians(s)
    lat_rad_n = math.radians(n)
    y_max = min(n_tiles - 1, int(n_tiles * (1 - math.log(math.tan(lat_rad_s) + 1 / math.cos(lat_rad_s)) / math.pi) / 2))
    y_min = max(0, int(n_tiles * (1 - math.log(math.tan(lat_rad_n) + 1 / math.cos(lat_rad_n)) / math.pi) / 2))
    return x_min, x_max, y_min, y_max


def load_land_cells():
    """Build dict of (floor(lat), floor(lon)) -> DEM file path for each cell."""
    cells = {}
    if not os.path.isdir(DEM_SOURCES):
        return cells
    for f in os.listdir(DEM_SOURCES):
        if not f.startswith("dem_") or not f.endswith(".tif"):
            continue
        path = os.path.join(DEM_SOURCES, f)
        if os.path.getsize(path) < 1000:
            continue
        parts = f.replace("dem_", "").replace(".tif", "").split("_")
        if len(parts) != 2:
            continue
        try:
            lat = int(parts[0][1:]) * (1 if parts[0][0] == "N" else -1)
            lon = int(parts[1][1:]) * (1 if parts[1][0] == "E" else -1)
            cells[(lat, lon)] = path
        except ValueError:
            continue
    return cells


def tile_bounds(z, x, y):
    """Return (lon_west, lat_south, lon_east, lat_north) of a tile."""
    n = 2 ** z
    lon_w = x / n * 360 - 180
    lon_e = (x + 1) / n * 360 - 180
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lon_w, lat_s, lon_e, lat_n


def tile_has_land(z, x, y, land_cells, dem_cache, min_elev=1):
    """True if the tile overlaps any land (nonzero elevation).

    Reads actual DEM data for the tile's bounds. Uses `dem_cache` dict to
    keep rasterio handles open between calls (keyed by DEM path).
    """
    import rasterio
    from rasterio.windows import from_bounds
    lon_w, lat_s, lon_e, lat_n = tile_bounds(z, x, y)

    # 1-degree cells that this tile overlaps
    lat_cell_min = int(math.floor(lat_s))
    lat_cell_max = int(math.floor(lat_n))
    lon_cell_min = int(math.floor(lon_w))
    lon_cell_max = int(math.floor(lon_e))

    # Fast reject: if no overlapping cell has a DEM, it's ocean
    any_cell = False
    for cell_lat in range(lat_cell_min, lat_cell_max + 1):
        for cell_lon in range(lon_cell_min, lon_cell_max + 1):
            if (cell_lat, cell_lon) in land_cells:
                any_cell = True
                break
        if any_cell:
            break
    if not any_cell:
        return False

    # Accurate sample: read DEM window for each overlapping cell
    for cell_lat in range(lat_cell_min, lat_cell_max + 1):
        for cell_lon in range(lon_cell_min, lon_cell_max + 1):
            key = (cell_lat, cell_lon)
            if key not in land_cells:
                continue
            dem_path = land_cells[key]
            ds = dem_cache.get(dem_path)
            if ds is None:
                try:
                    ds = rasterio.open(dem_path)
                    dem_cache[dem_path] = ds
                except Exception:
                    continue
            try:
                window = from_bounds(lon_w, lat_s, lon_e, lat_n, ds.transform)
                data = ds.read(1, window=window, boundless=True, fill_value=0)
                if data.size and (data >= min_elev).any():
                    return True
            except Exception:
                continue
    return False


def check_zoom(args):
    """Scan all tiles at one zoom level. Worker for multiprocessing.

    In terrain mode with `accurate=True`, reads the DEM per tile to decide
    whether a tile should exist. In `accurate=False` (satellite mode or
    skipped land check), just enumerates all tiles in the bbox.
    """
    cache_dir, ext, z, bbox, min_size, land_cells, accurate = args
    x_min, x_max, y_min, y_max = tile_range(bbox, z)
    expected = 0
    present = 0
    missing = []
    stubs   = []
    skipped_ocean = 0
    dem_cache = {}  # per-worker rasterio handle cache
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            if land_cells is not None:
                if accurate:
                    if not tile_has_land(z, x, y, land_cells, dem_cache):
                        skipped_ocean += 1
                        continue
                else:
                    # Fast coarse check — cell-only
                    n = 2 ** z
                    lon = (x + 0.5) / n * 360 - 180
                    lat_c = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
                    if (math.floor(lat_c), math.floor(lon)) not in land_cells:
                        skipped_ocean += 1
                        continue
            expected += 1
            path = os.path.join(cache_dir, str(z), str(x), f"{y}.{ext}")
            try:
                size = os.path.getsize(path)
            except (FileNotFoundError, NotADirectoryError):
                missing.append((z, x, y))
                continue
            if size < min_size:
                stubs.append((z, x, y, size))
            else:
                present += 1
    return (z, expected, present, missing, stubs, skipped_ocean)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bbox", help="west,south,east,north")
    parser.add_argument("--region", help=f"preset: {', '.join(sorted(REGIONS))}")
    parser.add_argument("--type", choices=["satellite", "terrain"], default="satellite",
                        help="Which cache to verify (default: satellite)")
    parser.add_argument("--cache", default=None, help="Override cache dir")
    parser.add_argument("--ext", default=None, help="tile extension (default: avif for sat, webp for terrain)")
    parser.add_argument("--min-zoom", type=int, default=0)
    parser.add_argument("--max-zoom", type=int, default=None,
                        help="Max zoom (default: 14 for sat, 12 for terrain)")
    parser.add_argument("--min-size", type=int, default=MIN_TILE_SIZE,
                        help=f"bytes — anything smaller is a stub (default: {MIN_TILE_SIZE})")
    parser.add_argument("--delete-stubs", action="store_true",
                        help="Delete undersized stub tiles so they regenerate on next build")
    parser.add_argument("--list-missing", action="store_true",
                        help="Print missing tile paths to stdout (one per line)")
    parser.add_argument("--accurate", action="store_true",
                        help="Terrain mode: read actual DEM data per tile to decide if a tile "
                             "should exist. Much slower but avoids false-positive 'missing' "
                             "tiles for coastal areas where the cell-based mask is too coarse.")
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args()

    if args.region:
        if args.region not in REGIONS:
            print(f"Unknown region: {args.region}")
            print(f"Known: {', '.join(sorted(REGIONS))}")
            sys.exit(1)
        bbox_str = REGIONS[args.region]
    elif args.bbox:
        bbox_str = args.bbox
    else:
        print("Error: must specify --bbox or --region")
        sys.exit(1)

    # Apply type-specific defaults
    if args.type == "satellite":
        cache_dir = args.cache or SATELLITE_CACHE
        ext = args.ext or "avif"
        max_zoom = args.max_zoom if args.max_zoom is not None else 14
        land_cells = None  # don't filter — satellite tiles cover ocean too
    else:  # terrain
        cache_dir = args.cache or TERRAIN_CACHE
        ext = args.ext or "webp"
        max_zoom = args.max_zoom if args.max_zoom is not None else 12
        land_cells = load_land_cells()
        if not land_cells:
            print(f"Warning: no DEM sources in {DEM_SOURCES} — cannot determine land vs ocean")
        else:
            print(f"Using {len(land_cells):,} DEM cells as land mask (ocean tiles will be skipped)")

    bbox = parse_bbox(bbox_str)
    if not os.path.isdir(cache_dir):
        print(f"Error: cache dir not found: {cache_dir}")
        sys.exit(1)

    print(f"Scanning {cache_dir} [{args.type}] for bbox {bbox} z{args.min_zoom}-{max_zoom}")
    print()

    work = [(cache_dir, ext, z, bbox, args.min_size, land_cells, args.accurate)
            for z in range(args.min_zoom, max_zoom + 1)]
    workers = args.workers or min(len(work), (os.cpu_count() or 4))
    with multiprocessing.Pool(workers) as pool:
        results = pool.map(check_zoom, work)

    total_expected = 0
    total_present = 0
    total_missing = []
    total_stubs = []
    total_skipped = 0

    # Header
    print(f"{'Zoom':>5}  {'Expected':>12}  {'Present':>12}  {'Missing':>12}  {'Stubs':>9}  {'%':>6}")
    print(f"{'-'*5}  {'-'*12}  {'-'*12}  {'-'*12}  {'-'*9}  {'-'*6}")

    for z, expected, present, missing, stubs, skipped_ocean in results:
        pct = present / expected * 100 if expected else 100.0
        print(f"{z:>5}  {expected:>12,}  {present:>12,}  {len(missing):>12,}  {len(stubs):>9,}  {pct:>5.1f}%")
        total_expected += expected
        total_present += present
        total_missing.extend(missing)
        total_stubs.extend(stubs)
        total_skipped += skipped_ocean

    print(f"{'-'*5}  {'-'*12}  {'-'*12}  {'-'*12}  {'-'*9}  {'-'*6}")
    pct = total_present / total_expected * 100 if total_expected else 100.0
    print(f"{'TOTAL':>5}  {total_expected:>12,}  {total_present:>12,}  {len(total_missing):>12,}  {len(total_stubs):>9,}  {pct:>5.1f}%")
    if total_skipped:
        print(f"(Skipped {total_skipped:,} ocean tiles with no DEM data)")
    print()

    if args.list_missing:
        for z, x, y in total_missing:
            print(f"{z}/{x}/{y}.{ext}")

    if args.delete_stubs and total_stubs:
        print(f"Deleting {len(total_stubs)} stub tiles...")
        for z, x, y, size in total_stubs:
            path = os.path.join(cache_dir, str(z), str(x), f"{y}.{ext}")
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        print(f"Deleted {len(total_stubs)} tiles. They will be regenerated on next build.")


if __name__ == "__main__":
    main()
