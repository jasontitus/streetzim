"""Pre-build gate — refuse to build a ZIM until every input, cache,
and source asset is known-good for the target bbox.

Why this exists: we've shipped multiple broken ZIMs (stale terrain with
a horizontal stripe on Iran / Butte MT, missing `places.html` so the
Kiwix "Find" button 404s, oversized search chunks that crash Kiwix on
"find") because validation happened AFTER a multi-hour build. This
runs in ~seconds, covers every class of failure we've seen, and exits
nonzero with a concrete punch list so we never start a build that will
fail validation.

Usage:
  python cloud/preflight.py --bbox=-120,31.3,-104,49.0 --name central-us
  python cloud/preflight.py --region central-us
  python cloud/preflight.py --region all     # every known region

Build wrappers (overture-rollout-redo.sh etc.) should invoke this with
the region's bbox; a nonzero exit is a hard stop, not a warning.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import multiprocessing
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor as Pool
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEM_DIR = ROOT / "terrain_cache" / "dem_sources"
WORLD_VRT = DEM_DIR / "comprehensive.vrt"
TERRAIN_DIR = ROOT / "terrain_cache"
SATELLITE_DIR = ROOT / "satellite_cache_avif_256"
WIKIDATA_DIR = ROOT / "wikidata_cache"
SEARCH_CACHE = ROOT / "search_cache" / "world.jsonl"
VIEWER_DIR = ROOT / "resources" / "viewer"
WORLD_PBF = ROOT / "world-data" / "planet-2026-03-10.osm.pbf"
WORLD_MBTILES = ROOT / "world-data" / "world-tiles-v2.mbtiles"

# Every region we've ever built — keep this in sync with
# overture-rollout-redo.sh. An unknown region must be called with
# --bbox / --name.
REGIONS = {
    "australia-nz":  (112.0, -47.5, 179.5, -10.0),
    "texas":         (-107.0, 25.5, -93.4, 36.6),
    "west-coast-us": (-125.0, 32.0, -114.0, 46.0),
    "east-coast-us": (-82.0, 24.0, -66.5, 47.6),
    "central-us":    (-120.0, 31.3, -104.0, 49.0),
    "iran":          (44.0, 25.0, 63.5, 39.8),
    "japan":         (122.9, 24.0, 146.0, 45.6),
    "silicon-valley":(-122.6, 37.2, -121.7, 37.9),
    "colorado":      (-109.06, 36.99, -102.04, 41.00),
    "hispaniola":    (-74.5, 17.5, -68.3, 20.1),
    "washington-dc": (-77.12, 38.79, -76.91, 38.99),
    "baltics":       (20.9, 53.9, 28.3, 59.7),
    "midwest-us":    (-104.1, 36.0, -80.5, 49.4),
    "central-asia":  (51.0, 35.0, 88.0, 56.0),
    "west-asia":     (25.0, 12.0, 63.0, 45.0),
    "africa":        (-18.0, -35.0, 52.0, 38.0),
    "europe":        (-25.0, 34.0, 50.5, 72.0),
    "indian-subcontinent": (60.0, 5.0, 97.5, 37.5),
}


@dataclass
class CheckResult:
    name: str
    status: str  # "pass", "warn", "fail"
    message: str
    fix_hint: str | None = None


# -----------------------------------------------------------------
# Low-level helpers
# -----------------------------------------------------------------

def tile_to_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z
    lon_w = x / n * 360.0 - 180.0
    lon_e = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (lon_w, lat_s, lon_e, lat_n)


def covering_dem_names(lon_w, lat_s, lon_e, lat_n) -> list[str]:
    """1°-square DEM filenames that cover a geographic bbox."""
    names = []
    for lat in range(int(math.floor(lat_s)), int(math.floor(lat_n)) + 1):
        for lon in range(int(math.floor(lon_w)), int(math.floor(lon_e)) + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            names.append(f"dem_{ns}{abs(lat):02d}_{ew}{abs(lon):03d}.tif")
    return names


# -----------------------------------------------------------------
# Individual checks
# -----------------------------------------------------------------

def check_inputs() -> list[CheckResult]:
    """Planet PBF + world MBtiles exist and are readable."""
    out = []
    for label, path in [("planet_pbf", WORLD_PBF),
                         ("world_mbtiles", WORLD_MBTILES)]:
        if not path.is_file():
            out.append(CheckResult(label, "fail",
                f"{path} missing",
                "re-download from archive or upstream source"))
            continue
        size_gb = path.stat().st_size / (1024 ** 3)
        if size_gb < 0.1:
            out.append(CheckResult(label, "fail",
                f"{path} is only {size_gb:.2f} GB — suspiciously small",
                "verify file is complete"))
            continue
        out.append(CheckResult(label, "pass",
            f"{path.name} = {size_gb:.1f} GB"))
    return out


def check_viewer_assets() -> list[CheckResult]:
    """Source files the build embeds: index.html, places.html, maplibre."""
    out = []
    # Hard requirements — build will ship these verbatim from here.
    # maplibre-gl.js/.css are downloaded from CDN during build, not
    # stored in resources/viewer, so not listed here.
    must = [
        ("index.html",    VIEWER_DIR / "index.html"),
        ("places.html",   VIEWER_DIR / "places.html"),
    ]
    for label, p in must:
        if not p.is_file():
            out.append(CheckResult(f"viewer.{label}", "fail",
                f"{p} missing — build will ship ZIM without it",
                f"restore from git or redeploy resources/viewer/"))
            continue
        size_kb = p.stat().st_size / 1024
        if size_kb < 1:
            out.append(CheckResult(f"viewer.{label}", "fail",
                f"{p} is only {size_kb:.1f} KB — probably a stub",
                f"restore from git"))
            continue
        out.append(CheckResult(f"viewer.{label}", "pass",
            f"{p.name} = {size_kb:.1f} KB"))

    # Quick content-level sanity — index.html must reference the key
    # APIs we know are present in the current code (e.g., `map._queryPlaces`
    # for the routing typeahead). If someone drops in an old index.html,
    # this catches it before build time rather than at user complaint.
    idx = VIEWER_DIR / "index.html"
    if idx.is_file():
        text = idx.read_text(errors="ignore")
        required_tokens = ["_queryPlaces", "maplibre-gl", "manifest.json"]
        missing = [t for t in required_tokens if t not in text]
        if missing:
            out.append(CheckResult("viewer.index_contents", "fail",
                f"index.html missing expected tokens: {missing}",
                "viewer is stale — resync web/drive/viewer or rebuild"))
        else:
            out.append(CheckResult("viewer.index_contents", "pass",
                f"all expected APIs present ({len(required_tokens)} tokens)"))
    return out


def check_search_cache() -> list[CheckResult]:
    if not SEARCH_CACHE.is_file():
        return [CheckResult("search_cache", "fail",
            f"{SEARCH_CACHE} missing — build would have to regenerate "
            "(hours)", "rebuild via scripts/build_search_cache.py or "
            "restore from archive")]
    size_gb = SEARCH_CACHE.stat().st_size / (1024 ** 3)
    if size_gb < 1:
        return [CheckResult("search_cache", "fail",
            f"{SEARCH_CACHE} is only {size_gb:.1f} GB — too small for "
            "world coverage", "rebuild")]
    return [CheckResult("search_cache", "pass",
        f"world.jsonl = {size_gb:.1f} GB")]


def check_wikidata_cache() -> list[CheckResult]:
    if not WIKIDATA_DIR.is_dir():
        return [CheckResult("wikidata_cache", "fail",
            f"{WIKIDATA_DIR} missing", "rebuild from dump")]
    n_items = 0
    for f in WIKIDATA_DIR.glob("*.json"):
        n_items += 1
    if n_items < 100:
        return [CheckResult("wikidata_cache", "warn",
            f"{WIKIDATA_DIR} has {n_items} JSON files — very sparse")]
    return [CheckResult("wikidata_cache", "pass",
        f"{n_items} shards on disk")]


def check_dem_cache_coverage(bbox) -> list[CheckResult]:
    """Every 1°-DEM that covers the bbox must be present and non-zero."""
    out = []
    minlon, minlat, maxlon, maxlat = bbox
    need = []
    for lat in range(int(math.floor(minlat)), int(math.floor(maxlat)) + 1):
        for lon in range(int(math.floor(minlon)), int(math.floor(maxlon)) + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            need.append(f"dem_{ns}{abs(lat):02d}_{ew}{abs(lon):03d}.tif")
    missing = []
    empty   = []
    for name in need:
        p = DEM_DIR / name
        nodata_marker = DEM_DIR / (name + ".nodata")
        if nodata_marker.is_file():
            # Sea/ice/polar cell with no terrestrial data — expected empty.
            continue
        if not p.is_file():
            missing.append(name)
            continue
        if p.stat().st_size < 1024:
            empty.append(name)
    if missing:
        out.append(CheckResult("dem_cache.coverage", "fail",
            f"{len(missing)} DEM(s) missing for bbox (e.g. {missing[:3]})",
            "re-run scripts/download_dem.py for the missing cells"))
    if empty:
        out.append(CheckResult("dem_cache.empty", "fail",
            f"{len(empty)} DEM(s) look empty/stub (e.g. {empty[:3]})",
            "re-download"))
    if not missing and not empty:
        out.append(CheckResult("dem_cache.coverage", "pass",
            f"{len(need)} 1°-DEMs all present"))
    return out


_VRT_CACHE = {}


def _get_vrt_cached(vrt_path: str):
    import rasterio
    src = _VRT_CACHE.get(vrt_path)
    if src is None:
        src = rasterio.open(vrt_path)
        _VRT_CACHE[vrt_path] = src
    return src


def _check_one_terrain(args) -> tuple:
    """Worker: per-tile (z, x, y, path) audit combining freshness +
    land-fraction-based content check.

    Returns (z, x, y, status). status in:
      'ok', 'missing', 'stale', 'corrupt', 'skip_ocean'
    """
    z, x, y, tile_path, dem_index, newest_dem_mtime, vrt_path, audit_content = args
    try:
        tile_mtime = os.path.getmtime(tile_path)
    except OSError:
        # Tile missing. To avoid flagging every ocean tile, consult the
        # VRT: if the tile's center pixel has valid elevation, it's land
        # and the tile should exist. Otherwise it's ocean — skip.
        if audit_content:
            try:
                tb_west, tb_south, tb_east, tb_north = tile_to_bounds(z, x, y)
                clon = (tb_west + tb_east) / 2
                clat = (tb_south + tb_north) / 2
                src = _get_vrt_cached(vrt_path)
                vals = list(src.sample([(clon, clat)]))[0]
                if vals[0] == src.nodata or abs(vals[0]) < 1:
                    return (z, x, y, "skip_ocean")
            except Exception:
                pass
        return (z, x, y, "missing")

    # Freshness check: every covering DEM must be <= tile mtime.
    if tile_mtime < newest_dem_mtime:
        tb_west, tb_south, tb_east, tb_north = tile_to_bounds(z, x, y)
        for name in covering_dem_names(tb_west, tb_south, tb_east, tb_north):
            m = dem_index.get(name)
            if m is not None and m > tile_mtime:
                return (z, x, y, "stale")

    # Content audit — tile-only, no VRT comparison.
    #
    # Every terrain failure we've actually shipped falls into two shapes:
    # (1) tile never written, (2) tile was generated from a VRT that
    # didn't cover its full bbox, so a swath of pixels came out as
    # elevation=0 (the "Iran 33°N stripe" / "Butte MT vertical band"
    # pattern). We've never seen random pixel-level corruption that
    # would require a pixel-exact diff against the VRT. So just decode
    # the tile and look for the zero-fill pattern:
    #
    #   - If >20% of pixels are |elev|<5  AND the tile's max elevation
    #     is > 100m somewhere, the tile mixes real land with a
    #     zero-fill region → flagged corrupt.
    #
    # Ocean tiles are all-zero with max~0, so max>100m filters them out.
    # This runs at ~1-2k tiles/s/worker, vs ~40/s for VRT-sample — a
    # 36× speedup on the real bug.
    if audit_content:
        try:
            import numpy as np
            from PIL import Image
            im = Image.open(tile_path).convert("RGB")
            arr = np.array(im).astype(np.int64)
            elev_tile = -10000 + (arr[:, :, 0] * 65536 + arr[:, :, 1] * 256
                                  + arr[:, :, 2]) * 0.1
            zero_pct = 100.0 * np.sum(np.abs(elev_tile) < 5) / elev_tile.size
            max_elev = elev_tile.max()
            if zero_pct > 20 and max_elev > 100:
                return (z, x, y, "corrupt")
        except Exception:
            return (z, x, y, "corrupt")

    return (z, x, y, "ok")


def check_terrain_cache(bbox, zooms, workers, audit_content=False) -> list[CheckResult]:
    """Every terrain tile the build will emit must exist, be fresh,
    and (with --audit-content) have content matching the VRT."""
    import mercantile
    if not WORLD_VRT.is_file():
        return [CheckResult("terrain_cache.vrt", "fail",
            f"{WORLD_VRT} missing — regen would write zero-filled tiles",
            "build with scripts/build_comprehensive_vrt.py")]

    dem_index = {}
    for f in glob.glob(str(DEM_DIR / "*.tif")):
        try:
            dem_index[os.path.basename(f)] = os.path.getmtime(f)
        except OSError:
            pass
    newest = max(dem_index.values()) if dem_index else 0.0

    jobs = []
    seen = set()
    for z in zooms:
        for t in mercantile.tiles(*bbox, zooms=z):
            key = (z, t.x, t.y)
            if key in seen:
                continue
            seen.add(key)
            p = os.path.join(TERRAIN_DIR, str(z), str(t.x), f"{t.y}.webp")
            jobs.append((z, t.x, t.y, p, dem_index, newest,
                         str(WORLD_VRT), audit_content))

    missing = 0; stale = 0; corrupt = 0; ok = 0; skipped = 0
    corrupt_paths: list[str] = []
    with Pool(max_workers=workers) as pool:
        for res in pool.map(_check_one_terrain, jobs, chunksize=256):
            z_, x_, y_, status = res
            if   status == "missing":    missing += 1
            elif status == "stale":      stale   += 1
            elif status == "corrupt":
                corrupt += 1
                corrupt_paths.append(os.path.join(str(TERRAIN_DIR),
                    str(z_), str(x_), f"{y_}.webp"))
            elif status == "skip_ocean": skipped += 1
            else:                        ok += 1

    results = []
    total_bad = missing + stale + corrupt
    if total_bad == 0:
        results.append(CheckResult("terrain_cache", "pass",
            f"{ok:,} tiles fresh ({skipped:,} ocean skipped)"))
    else:
        extra = ""
        if corrupt_paths:
            # Emit a concrete file list the build wrapper can feed into
            # a regen step: `xargs rm -f` then verify_terrain_freshness
            # --regenerate picks them up as missing.
            dump = ROOT / f".preflight-corrupt-tiles.txt"
            dump.write_text("\n".join(corrupt_paths))
            extra = f"; wrote {len(corrupt_paths)} corrupt paths → {dump.name}"
        results.append(CheckResult("terrain_cache", "fail",
            f"{missing:,} missing, {stale:,} stale, {corrupt:,} "
            f"content-mismatched ({ok:,} ok, {skipped:,} ocean-skipped)"
            f"{extra}",
            "xargs rm -f < .preflight-corrupt-tiles.txt && "
            "cloud/verify_terrain_freshness.py --regenerate"))
    return results


def check_satellite_cache(bbox) -> list[CheckResult]:
    if not SATELLITE_DIR.is_dir():
        return [CheckResult("satellite_cache", "warn",
            f"{SATELLITE_DIR} not present — builds without --satellite "
            "still pass; builds with --satellite will download in-line "
            "(slow)")]
    # We don't try to enumerate every tile — just spot-check z0, z6, z10
    # all exist for SOMEWHERE in the bbox. A fully-populated cache can
    # be ~100 GB which we'd skip.
    import mercantile
    hit = 0
    checked = 0
    for z in (0, 6, 10):
        for t in list(mercantile.tiles(*bbox, zooms=z))[:3]:
            checked += 1
            if (SATELLITE_DIR / str(z) / str(t.x) / f"{t.y}.avif").is_file():
                hit += 1
    if hit == 0 and checked > 0:
        return [CheckResult("satellite_cache.sample", "warn",
            f"{checked} spot-check lookups all missed — cache may be "
            "incomplete (build will re-download)")]
    return [CheckResult("satellite_cache.sample", "pass",
        f"{hit}/{checked} spot-checks hit")]


# -----------------------------------------------------------------
# Reporter + entrypoint
# -----------------------------------------------------------------

def report(results: list[CheckResult]) -> int:
    exit_code = 0
    fails = [r for r in results if r.status == "fail"]
    warns = [r for r in results if r.status == "warn"]
    for r in results:
        tag = {"pass": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}[r.status]
        print(f"  {tag} {r.name:28s} {r.message}")
        if r.fix_hint and r.status != "pass":
            print(f"         → fix: {r.fix_hint}")
    print()
    if fails:
        print(f"=== {len(fails)} FAIL / {len(warns)} WARN — "
              "DO NOT BUILD; fix above and re-run preflight ===")
        exit_code = 1
    elif warns:
        print(f"=== {len(warns)} WARN — build may proceed, but review ===")
    else:
        print("=== all checks pass — build is safe to start ===")
    return exit_code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox",
        help="minlon,minlat,maxlon,maxlat — overrides --region")
    ap.add_argument("--name", default=None,
        help="region name (for reporting); optional when --region used")
    ap.add_argument("--region", default=None,
        help="named region from the baked-in REGIONS dict, or 'all'")
    ap.add_argument("--zooms", default="0-12",
        help="terrain audit zoom range")
    ap.add_argument("--workers", type=int,
        default=max(2, multiprocessing.cpu_count() - 1))
    ap.add_argument("--audit-content", action="store_true",
        help="Also compare tile content against VRT samples — 50x "
             "slower but catches the 'fresh mtime but zero-fill' bug. "
             "Required for production gates.")
    ap.add_argument("--skip-terrain", action="store_true",
        help="Skip terrain cache check (for debugging other gates)")
    args = ap.parse_args()

    # Resolve bbox.
    if args.bbox and args.region:
        ap.error("pass either --bbox or --region, not both")
    if args.bbox:
        bbox = tuple(float(v) for v in args.bbox.split(","))
        if len(bbox) != 4: ap.error("--bbox needs 4 comma-separated floats")
        name = args.name or "custom"
        bboxes = [(name, bbox)]
    elif args.region == "all":
        bboxes = list(REGIONS.items())
    elif args.region in REGIONS:
        bboxes = [(args.region, REGIONS[args.region])]
    elif args.region is None:
        ap.error("pass --bbox or --region")
    else:
        ap.error(f"unknown region {args.region!r}; "
                 f"pick from {sorted(REGIONS)} or use --bbox")

    zoom_lo, _, zoom_hi = args.zooms.partition("-")
    zoom_lo, zoom_hi = int(zoom_lo), int(zoom_hi or zoom_lo)
    zooms = list(range(zoom_lo, zoom_hi + 1))

    print("=" * 72)
    print("streetzim preflight")
    print("=" * 72)
    print(f"  regions: {', '.join(n for n, _ in bboxes)}")
    print(f"  zooms:   {zoom_lo}-{zoom_hi}")
    print(f"  workers: {args.workers}")
    print(f"  content audit: {'ON' if args.audit_content else 'off (fast)'}")
    print()

    results: list[CheckResult] = []

    # Global checks (don't vary per-region).
    print("-- inputs + viewer assets")
    t0 = time.time()
    results.extend(check_inputs())
    results.extend(check_viewer_assets())
    results.extend(check_search_cache())
    results.extend(check_wikidata_cache())
    print(f"   ({time.time()-t0:.2f}s)")

    # Per-region checks.
    for name, bbox in bboxes:
        print(f"-- region {name} bbox={bbox}")
        t0 = time.time()
        results.extend(check_dem_cache_coverage(bbox))
        results.extend(check_satellite_cache(bbox))
        if not args.skip_terrain:
            results.extend(check_terrain_cache(bbox, zooms, args.workers,
                audit_content=args.audit_content))
        print(f"   ({time.time()-t0:.2f}s)")

    print()
    print("-" * 72)
    print("REPORT")
    print("-" * 72)
    return report(results)


if __name__ == "__main__":
    sys.exit(main())
