"""`streetzim`: the openZIM-style command line for the builder.

It follows maps2zim's conventions, so Zimfarm can run it the same way:
--name/--title/--description (required), --creator, --publisher, --tags,
--illustration-url, --output (a folder), --file-name "{name}_{period}",
--stats-filename, --overwrite, --tmp, --dl, --default-view, --max-zoom,
--include-poly, --area. Flag names match maps2zim's wherever both have the
flag.

It does no building itself: it validates the flags (before any download),
fetches the inputs into --dl, and runs create_osm_zim.main() with the
matching arguments. Production scripts keep calling create_osm_zim.py
directly; both paths produce the same ZIM for the same inputs.

    streetzim --name osm_en_monaco --title Monaco \\
        --description "Offline map of Monaco with search and routing" \\
        --area monaco --output /output

The satellite layer (CC BY-NC-SA) is deliberately not offered here.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Keep the imports above stdlib-only: --dl must reach STREETZIM_CACHE_DIR
# before streetzim.common is first imported (it reads it at import time).

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:     # also runnable as `python streetzim/cli.py`
    sys.path.insert(0, str(REPO_ROOT))
GEOFABRIK_POLY = re.compile(r"^https?://download\.geofabrik\.de/(.+)\.poly$")
USER_AGENT = "streetzim (https://github.com/jasontitus/streetzim)"

# Extra keys for offliner-definition.json, by flag. `offliner: False` keeps a
# developer flag out of the Zimfarm definition. Everything else is derived
# from the parser (tools/offliner_definition.py).
ZIMFARM = {
    "name": {"title": "ZIM name",
             "pattern": r"^([a-z0-9\-\.]+_)([a-z\-]+_)([a-z0-9\-\.]+)$"},
    "title": {"title": "ZIM title", "minGraphemes": 1, "maxGraphemes": 30},
    "description": {"title": "ZIM description", "minGraphemes": 1, "maxGraphemes": 80},
    "long_description": {"title": "ZIM long description", "minGraphemes": 1,
                         "maxGraphemes": 4000},
    "publisher": {"isPublisher": True},
    "file_name": {"title": "ZIM filename"},
    "tags": {"title": "ZIM tags"},
    "illustration_url": {"title": "Illustration URL"},
    "area": {"title": "Preset area"},
    "include_poly": {"title": "Include poly"},
    "bbox": {"title": "Bounding box",
             "pattern": r"^-?[0-9.]+,-?[0-9.]+,-?[0-9.]+,-?[0-9.]+$"},
    "pbf_url": {"title": "OSM extract URL"},
    "no_routing": {"title": "No routing",
                   "description": "Leave out offline routing (on by default)"},
    "wikidata": {"title": "Wikidata"},
    "terrain": {"title": "Terrain"},
    "default_view": {"title": "Default view"},
    "output": {"pattern": r"^/output$"},
    "stats_filename": {"pattern": r"^/output/task_progress\.json$"},
    "zim_workers": {"title": "ZIM workers", "min": 1},
    "max_zoom": {"min": 0, "max": 14},
    "tmp": {"offliner": False},
    "dl": {"offliner": False},
    "shapefiles": {"offliner": False},
    "mbtiles": {"offliner": False},
    "overwrite": {"offliner": False},
    "keep_temp": {"offliner": False},
}
# Flag -> ZIM metadata, for the definition's zimMetadata list.
ZIM_METADATA_FLAGS = {"Name": "name", "Title": "title", "Description": "description",
                      "LongDescription": "long_description", "Creator": "creator",
                      "Publisher": "publisher", "Illustration": "illustration_url"}


def version() -> str:
    from streetzim.__about__ import __version__
    return __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="streetzim",
        description="Make a ZIM of an offline OpenStreetMap map with search, "
                    "category browsing and routing.")
    p.add_argument("--name", required=True, help="Name of the ZIM")
    p.add_argument("--title", required=True,
                   help="Title of the ZIM (at most 30 characters)")
    p.add_argument("--description", required=True,
                   help="Description of the ZIM (at most 80 characters)")
    p.add_argument("--long-description",
                   help="Long description of the ZIM (at most 4000 characters)")
    p.add_argument("--creator", default="OpenStreetMap contributors",
                   help="Name of content creator. Default: OpenStreetMap contributors")
    p.add_argument("--publisher", default="openZIM", help="Publisher name. Default: openZIM")
    p.add_argument("--file-name", default="{name}_{period}",
                   help="ZIM file name, without .zim; {name} and {period} (YYYY-MM) "
                        "are replaced. Default: {name}_{period}")
    p.add_argument("--tags", help="Semicolon (;) delimited list of tags to add to the ZIM")
    p.add_argument("--illustration-url",
                   help="URL (or path) of a PNG, JPEG or WebP used for the ZIM "
                        "illustration. Default: a generated map icon")
    p.add_argument("--output", default=os.environ.get("STREETZIM_OUTPUT", "output"),
                   help="Output folder for the ZIM. Default: ./output")
    p.add_argument("--stats-filename", help="Path to store the progress JSON file to")
    p.add_argument("--overwrite", action="store_true",
                   help="Do not fail if the ZIM already exists, overwrite it")
    p.add_argument("--tmp", help="Folder for temporary files")
    p.add_argument("--dl", help="Folder for downloads and caches (OSM extracts, "
                                "DEM, Wikidata), reusable across runs")
    p.add_argument("--debug", action="store_true",
                   help="Keep temporary files (the build log is always verbose)")
    p.add_argument("--version", action="version", version=f"%(prog)s {version()}")

    src = p.add_argument_group("Area (one of --area, --include-poly, --bbox)")
    src.add_argument("--area", help="A preset area from create_osm_zim.py's "
                                    "KNOWN_AREAS, e.g. monaco")
    src.add_argument("--include-poly",
                     help="URL of a .poly file bounding the area. A Geofabrik "
                          "URL also selects its OSM extract; otherwise give "
                          "--pbf-url")
    src.add_argument("--bbox", help="minlon,minlat,maxlon,maxlat")
    src.add_argument("--pbf-url",
                     help="OSM extract (.osm.pbf) to build from. Default: the "
                          "Geofabrik extract of --area / --include-poly")
    src.add_argument("--mbtiles",
                     help="Use this OpenMapTiles MBTiles (e.g. OpenFreeMap) "
                          "instead of generating tiles with tilemaker")
    src.add_argument("--shapefiles",
                     help="Folder with coastline/ and landcover/ for tilemaker. "
                          "Default: <dl>/shapefiles, fetched when missing")

    feat = p.add_argument_group("Content")
    feat.add_argument("--routing", action=argparse.BooleanOptionalAction, default=True,
                      help="Offline routing (driving, walking, cycling). Default: on")
    feat.add_argument("--wikidata", action="store_true",
                      help="Wikidata place details (needs network access to Wikidata)")
    feat.add_argument("--terrain", action="store_true",
                      help="Hillshade from Copernicus DEM (downloads DEM tiles)")
    feat.add_argument("--max-zoom", type=int, choices=range(0, 15), metavar="{0..14}",
                      help="Maximum zoom of the vector tiles. Default: 14")
    feat.add_argument("--default-view",
                      help="Initial map view as latitude,longitude[,zoom]")
    feat.add_argument("--zim-workers", type=int,
                      help="Compression threads for libzim. Default: CPU count")
    feat.add_argument("--keep-temp", action="store_true", help=argparse.SUPPRESS)
    return p


# ---------------------------------------------------------------- helpers


def zim_filename(pattern: str, name: str, today: datetime.date | None = None) -> str:
    period = (today or datetime.date.today()).strftime("%Y-%m")
    fn = pattern.replace("{name}", name).replace("{period}", period)
    if not fn or "/" in fn or os.sep in fn or fn in (".", ".."):
        raise ValueError(f"--file-name {pattern!r} does not give a plain file name")
    return fn if fn.endswith(".zim") else fn + ".zim"


def parse_poly(text: str) -> tuple[float, float, float, float]:
    """Bounding box of an Osmosis .poly file (holes, marked '!', ignored)."""
    lons, lats = [], []
    lines = [ln.strip() for ln in text.splitlines()]
    i, depth, hole = 1, 0, False            # line 0 is the file's name
    while i < len(lines):
        ln = lines[i]
        i += 1
        if not ln:
            continue
        if ln == "END":
            if depth == 0:
                break
            depth, hole = 0, False
            continue
        if depth == 0:
            depth, hole = 1, ln.startswith("!")
            continue
        if not hole:
            x, y = ln.split()[:2]
            lons.append(float(x))
            lats.append(float(y))
    if not lons:
        raise ValueError("no coordinates in .poly file")
    return min(lons), min(lats), max(lons), max(lats)


def parse_default_view(value: str) -> tuple[float, float, float | None]:
    parts = value.split(",")
    if len(parts) not in (2, 3):
        raise ValueError("--default-view needs latitude,longitude[,zoom]")
    lat, lon = float(parts[0]), float(parts[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"--default-view {value!r} is not latitude,longitude")
    return lat, lon, (float(parts[2]) if len(parts) == 3 else None)


def fetch(url: str, dest: Path) -> Path:
    """Download once into --dl; reuse it on later runs."""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  Reusing {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    print(f"  Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    os.replace(part, dest)
    return dest


def _name_of_url(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[-1])


# ---------------------------------------------------------------- main


def plan(args: argparse.Namespace, dl: Path) -> tuple[list[str], dict]:
    """Resolve the area and inputs, returning create_osm_zim arguments.

    Downloads the .poly and .pbf into `dl`. Raises ValueError on bad flags.
    """
    from create_osm_zim import KNOWN_AREAS  # after STREETZIM_CACHE_DIR is set

    sources = [bool(args.area), bool(args.include_poly), bool(args.bbox)]
    if sum(sources) != 1:
        raise ValueError("give exactly one of --area, --include-poly, --bbox")
    geofabrik = None
    map_name = args.title
    if args.area:
        key = args.area.lower().replace(" ", "-")
        if key not in KNOWN_AREAS:
            raise ValueError(f"unknown --area {args.area!r}; known: "
                             + ", ".join(sorted(KNOWN_AREAS)))
        area = KNOWN_AREAS[key]
        geofabrik, bbox = area["geofabrik"], area["bbox"]
    elif args.include_poly:
        urls = [u.strip() for u in args.include_poly.split(",") if u.strip()]
        boxes = []
        for u in urls:
            text = fetch(u, dl / "poly" / _name_of_url(u)).read_text()
            boxes.append(parse_poly(text))
        bbox = ",".join(f"{v:.6f}" for v in (
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)))
        m = GEOFABRIK_POLY.match(urls[0])
        if len(urls) == 1 and m:
            geofabrik = m.group(1)
    else:
        bbox = args.bbox
        from streetzim.common import parse_bbox
        parse_bbox(bbox)

    pbf_url = args.pbf_url or (
        f"https://download.geofabrik.de/{geofabrik}-latest.osm.pbf" if geofabrik else None)
    if not pbf_url and not args.mbtiles:
        raise ValueError("no OSM extract for this area: give --pbf-url "
                         "(or --mbtiles, which builds without routing)")
    if not pbf_url and args.routing:
        raise ValueError("--routing needs an OSM extract: give --pbf-url, or --no-routing")

    argv = ["--bbox", bbox, "--name", map_name, "--zim-name", args.name,
            "--title", args.title, "--description", args.description,
            "--creator", args.creator, "--publisher", args.publisher,
            "--scraper", f"streetzim v{version()}", "--split-find-chips"]
    if pbf_url:
        argv += ["--pbf", str(fetch(pbf_url, dl / "osm" / _name_of_url(pbf_url)))]
    if args.mbtiles:
        argv += ["--mbtiles", args.mbtiles]
    if args.long_description:
        argv += ["--long-description", args.long_description]
    if args.tags:
        argv += ["--tags", args.tags]
    if args.illustration_url:
        argv += ["--illustration", args.illustration_url]
    if args.stats_filename:
        argv += ["--stats-filename", str(Path(args.stats_filename).resolve())]
    if args.routing:
        argv += ["--routing", "--spatial-chunk-scale", "10"]
    if args.wikidata:
        argv += ["--wikidata"]
    if args.terrain:
        argv += ["--terrain"]
    if args.max_zoom is not None:
        argv += ["--max-zoom", str(args.max_zoom)]
    if args.zim_workers:
        argv += ["--workers", str(args.zim_workers)]
    if args.default_view:
        lat, lon, zoom = parse_default_view(args.default_view)
        argv += ["--map-center", f"{lon},{lat}"]
        if zoom is not None:
            argv += ["--map-zoom", str(round(zoom))]
    if args.debug or args.keep_temp:
        argv += ["--keep-temp"]
    return argv, {"bbox": bbox, "pbf_url": pbf_url}


def ensure_shapefiles(folder: Path) -> Path:
    from streetzim.tiles import required_shapefiles   # the tilemaker config's list
    if all((folder / s).exists() for s in required_shapefiles()):
        return folder
    print(f"  Fetching coastline and Natural Earth shapefiles into {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["bash", str(REPO_ROOT / "scripts" / "fetch-shapefiles.sh"), str(folder)],
                   check=True)
    return folder


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (REPO_ROOT / "resources" / "viewer" / "index.html").exists():
        print("streetzim: error: resources/ not found next to the code; install "
              "from a checkout (pip install -e .) or use the Docker image",
              file=sys.stderr)
        return 2
    out_dir = Path(args.output).resolve()
    tmp = Path(args.tmp or (Path(tempfile.gettempdir()) / "streetzim")).resolve()
    tmp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(tmp)
    # Downloads and caches: --dl, else <tmp>/dl (as maps2zim does). An
    # explicit --dl wins over an inherited STREETZIM_CACHE_DIR.
    dl = Path(args.dl or (tmp / "dl")).resolve()
    if args.dl or not os.environ.get("STREETZIM_CACHE_DIR"):
        os.environ["STREETZIM_CACHE_DIR"] = str(dl / "cache")

    # Everything that can be checked cheaply is checked before downloading.
    try:
        from streetzim.zim_metadata import build_overrides
        build_overrides(name=args.name, title=args.title, description=args.description,
                        long_description=args.long_description, creator=args.creator,
                        publisher=args.publisher, tags=args.tags)
        final = out_dir / zim_filename(args.file_name, args.name)
        if args.default_view:
            parse_default_view(args.default_view)
    except ValueError as e:
        print(f"streetzim: error: {e}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    if final.exists() and not args.overwrite:
        print(f"streetzim: error: {final} exists (use --overwrite)", file=sys.stderr)
        return 2

    try:
        build_args, info = plan(args, dl)
    except ValueError as e:
        print(f"streetzim: error: {e}", file=sys.stderr)
        return 2

    # Build next to the target and rename at the end, so a failed or
    # interrupted run never leaves (or replaces) a .zim in the output folder.
    building = final.with_name(final.name + ".tmp")
    building.unlink(missing_ok=True)   # left by an interrupted run
    build_args += ["-o", str(building)]
    cwd = os.getcwd()
    if not args.mbtiles:
        # tilemaker reads the shapefiles relative to the working directory.
        os.chdir(ensure_shapefiles(Path(args.shapefiles or (dl / "shapefiles")).resolve()))
    try:
        import create_osm_zim
        create_osm_zim.main(build_args)
    finally:
        os.chdir(cwd)
    os.replace(building, final)
    print(f"streetzim: wrote {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
