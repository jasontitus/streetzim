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

Satellite imagery is off by default and opt-in (--satellite). The default
source is freely licensed (CC BY 4.0); a non-commercial one needs
--satellite-accept-noncommercial and makes a ZIM labelled as restricted
(Flavour, Tags, LongDescription, License, viewer credits). See
docs/zimfarm.md, "Satellite imagery".
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

# Keep the imports above stdlib-only: --dl must reach STREETZIM_CACHE_DIR
# before streetzim.common is first imported (it reads it at import time).
# streetzim.area is stdlib-only.

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:     # also runnable as `python streetzim/cli.py`
    sys.path.insert(0, str(REPO_ROOT))
from streetzim import area  # noqa: E402  (after the path fix above)
from streetzim.paths import RESOURCES_DIR, missing_runtime_files  # noqa: E402
from streetzim import satellite_sources  # noqa: E402
GEOFABRIK_POLY = re.compile(r"^https?://download\.geofabrik\.de/(.+)\.poly$")
USER_AGENT = "streetzim (https://github.com/jasontitus/streetzim)"

# Extra keys for offliner-definition.json, by flag. `offliner: False` keeps a
# developer flag out of the Zimfarm definition. Everything else is derived
# from the parser (tools/offliner_definition.py). "choices": "KNOWN_AREAS"
# is filled in from create_osm_zim.KNOWN_AREAS when the file is generated.
ZIMFARM: dict[str, dict[str, Any]] = {
    "name": {"title": "ZIM name",
             "pattern": r"^([a-z0-9\-\.]+_)([a-z\-]+_)([a-z0-9\-\.]+)$"},
    "title": {"title": "ZIM title", "minGraphemes": 1, "maxGraphemes": 30},
    "description": {"title": "ZIM description", "minGraphemes": 1, "maxGraphemes": 80},
    "long_description": {"title": "ZIM long description", "minGraphemes": 1,
                         "maxGraphemes": 4000},
    "publisher": {"isPublisher": True},
    "file_name": {"title": "ZIM filename"},
    "tags": {"title": "ZIM tags"},
    "illustration_url": {"title": "Illustration URL", "type": "url"},
    "area": {"title": "Preset area", "type": "string-enum", "choices": "KNOWN_AREAS"},
    "include_poly": {"title": "Include poly"},
    "bbox": {"title": "Bounding box",
             "pattern": r"^-?[0-9.]+,-?[0-9.]+,-?[0-9.]+,-?[0-9.]+$"},
    "pbf_url": {"title": "OSM extract URL", "type": "url"},
    "no_routing": {"title": "No routing",
                   "description": "Leave out offline routing (on by default)"},
    "wikidata": {"title": "Wikidata"},
    "terrain": {"title": "Terrain"},
    "kiwix_poi_pages": {"title": "POIs in Kiwix search"},
    "default_view": {"title": "Default view"},
    "output": {"pattern": r"^/output$"},
    "stats_filename": {"pattern": r"^/output/task_progress\.json$"},
    "zim_workers": {"title": "ZIM workers", "min": 1},
    "max_zoom": {"min": 0, "max": 14},
    "satellite": {"title": "Satellite imagery"},
    "satellite_source": {"title": "Satellite source", "type": "string-enum",
                         "choices": sorted(satellite_sources.SOURCES)},
    "satellite_accept_noncommercial": {"title": "Accept non-commercial imagery"},
    "satellite_max_zoom": {"title": "Satellite max zoom"},
    "tmp": {"offliner": False},
    "dl": {"offliner": False},
    "shapefiles": {"offliner": False},
    "mbtiles": {"offliner": False},
    "overwrite": {"offliner": False},
    "keep_temp": {"offliner": False},
}
# Zimfarm checks these when a recipe is saved (its check_exclusive_fields).
MODEL_VALIDATORS = [{"name": "check_exclusive_fields",
                     "fields": ["area", "include_poly", "bbox"]}]
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
    p.add_argument("--file-name",
                   help="ZIM file name, without .zim; {name}, {period} (YYYY-MM) "
                        "and {flavour} are replaced. Default: {name}_{period}, "
                        "or {name}_{flavour}_{period} with --satellite")
    p.add_argument("--tags", help="Semicolon (;) delimited list of tags to add to the ZIM")
    p.add_argument("--illustration-url",
                   help="URL (or path) of a PNG, JPEG, WebP or SVG (SVG needs "
                        "zimscraperlib, as in the Docker image) used for the ZIM "
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
    src.add_argument("--bbox", help="minlon,minlat,maxlon,maxlat. For an area across "
                                    "the antimeridian, minlon > maxlon (Fiji: "
                                    "172.8,-23.2,-176.5,-11.2)")
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
    feat.add_argument("--kiwix-poi-pages", action="store_true",
                      help="Also list every named POI in Kiwix's own search, "
                           "not only "
                           "places, parks, peaks, water and airports. Adds "
                           "about 440 B per POI (+16%% on Luxembourg). Default: off")
    feat.add_argument("--max-zoom", type=int, choices=range(0, 15), metavar="{0..14}",
                      help="Maximum zoom of the vector tiles. Default: 14")
    feat.add_argument("--default-view",
                      help="Initial map view as latitude,longitude[,zoom]")
    feat.add_argument("--zim-workers", type=int,
                      help="Compression threads for libzim. Default: the CPU "
                           "count, at most 20")
    feat.add_argument("--keep-temp", action="store_true", help=argparse.SUPPRESS)
    add_satellite_flags(p)
    return p


# ---------------------------------------------------------------- satellite


def add_satellite_flags(p: argparse.ArgumentParser) -> None:
    free = satellite_sources.OPENZIM_DEFAULT
    sat = p.add_argument_group(
        "Satellite imagery (off by default)",
        "EOX Sentinel-2 cloudless mosaics. Each year has its own licence; "
        "see docs/zimfarm.md, 'Satellite imagery'.")
    sat.add_argument("--satellite", action="store_true",
                     help="Add a satellite imagery layer (a Satellite button in the "
                          f"viewer). Off by default. The source defaults to {free}, "
                          "CC BY 4.0; the ZIM's Flavour becomes 'satellite'. "
                          "--satellite-source and --satellite-max-zoom also turn it on")
    sat.add_argument("--satellite-source", choices=sorted(satellite_sources.SOURCES),
                     help=f"Satellite imagery source (implies --satellite). {free}: EOX Sentinel-2 "
                          "cloudless 2016, CC BY 4.0, free for any use with "
                          "attribution (softer, bluer, some seams). "
                          "s2cloudless-2021: EOX Sentinel-2 cloudless 2021, "
                          "CC BY-NC-SA 4.0, NON-COMMERCIAL use only (sharper); "
                          "needs --satellite-accept-noncommercial and labels the "
                          "ZIM as restricted (Flavour 'satellite-nc', tag "
                          f"'non-commercial'). Default: {free}")
    sat.add_argument("--satellite-accept-noncommercial", action="store_true",
                     help="Required with a non-commercial --satellite-source "
                          "(s2cloudless-2021), refused without satellite imagery: "
                          "confirms that this ZIM may be used and redistributed "
                          "for non-commercial purposes only")
    sat.add_argument("--satellite-max-zoom", type=int, choices=range(0, 15),
                     metavar="{0..14}",
                     help="Maximum zoom of the satellite tiles (implies "
                          "--satellite; the viewer over-zooms past it). Default: "
                          "--max-zoom, and at most 13 for areas centred 45 degrees "
                          "or more from the equator")


LONG_DESCRIPTION_MAX = 4000      # zimscraperlib; streetzim/zim_metadata.py


def satellite_source(args: argparse.Namespace) -> satellite_sources.SatelliteSource | None:
    """The satellite source the flags ask for (None: no satellite), after
    checking them. ValueError when they are inconsistent, or a
    non-commercial source is not acknowledged."""
    if not (args.satellite or args.satellite_source or args.satellite_max_zoom is not None):
        if args.satellite_accept_noncommercial:
            raise ValueError("--satellite-accept-noncommercial needs --satellite-source")
        return None
    src = satellite_sources.get(args.satellite_source or satellite_sources.OPENZIM_DEFAULT)
    if src.noncommercial and not args.satellite_accept_noncommercial:
        raise ValueError(
            f"--satellite-source {src.key} is {src.license}: non-commercial use "
            "only, so the ZIM could not be used or passed on commercially. Add "
            "--satellite-accept-noncommercial to build it as a restricted variant "
            f"(Flavour {satellite_sources.FLAVOUR_NONCOMMERCIAL}), or use "
            f"{satellite_sources.OPENZIM_DEFAULT} ("
            f"{satellite_sources.get(satellite_sources.OPENZIM_DEFAULT).license})")
    return src


def apply_satellite(args: argparse.Namespace) -> None:
    """Resolve --file-name and, with --satellite, label the ZIM: Flavour,
    Tags and (for a non-commercial source) a LongDescription note. Run after
    {name}/{period} are filled and before the metadata is validated."""
    src = satellite_source(args)
    args.flavour = satellite_sources.flavour(src) if src else None
    if args.file_name is None:
        args.file_name = "{name}_{flavour}_{period}" if src else "{name}_{period}"
    args.file_name = args.file_name.replace("{flavour}", args.flavour or "maxi")
    if not src:
        return
    args.tags = ";".join(([args.tags] if args.tags else []) + satellite_sources.tags(src))
    if src.noncommercial:
        # The note always fits: the text before it is cut to leave room
        # (code points, which are never fewer than graphemes).
        note = satellite_sources.restricted_note(src)
        text = args.long_description or args.description
        room = LONG_DESCRIPTION_MAX - len(note) - 2
        if len(text) > room:
            text = text[:room - 1].rstrip() + "\u2026"
        args.long_description = f"{text}\n\n{note}"


def satellite_argv(args: argparse.Namespace) -> list[str]:
    """create_osm_zim arguments for the satellite layer and the flavour."""
    src = satellite_source(args)
    if not src:
        return []
    argv = ["--satellite", f"--satellite-source={src.key}",
            f"--flavour={satellite_sources.flavour(src)}"]
    if args.satellite_max_zoom is not None:
        argv += ["--satellite-zoom", str(args.satellite_max_zoom)]
    return argv


# ---------------------------------------------------------------- helpers


def period(today: datetime.date | None = None) -> str:
    return (today or datetime.date.today()).strftime("%Y-%m")


def fill(value: str, name: str, today: datetime.date | None = None) -> str:
    """maps2zim's placeholders: {name} and {period} (YYYY-MM)."""
    return value.replace("{name}", name).replace("{period}", period(today))


def zim_filename(pattern: str, name: str, today: datetime.date | None = None) -> str:
    fn = fill(pattern, name, today)
    if not fn or "/" in fn or os.sep in fn or fn in (".", ".."):
        raise ValueError(f"--file-name {pattern!r} does not give a plain file name")
    return fn if fn.endswith(".zim") else fn + ".zim"


BBox = tuple[float, float, float, float]
_Group = tuple[BBox, list[BBox], float]      # a box, its member boxes, land area


def check_bbox(b: BBox, what: str) -> BBox:
    """The box, unwrapped (streetzim/area.py). An area across the
    antimeridian is given with minlon > maxlon (170,-20,-175,-10) or with
    maxlon past 180 (170,-20,185,-10); both mean the same box."""
    minlon, minlat, maxlon, maxlat = b
    wrapped = -180 <= minlon <= 180 and -180 <= maxlon <= 180 and minlon != maxlon
    unwrapped = -180 <= minlon < 180 < maxlon < minlon + 360
    if not ((wrapped or unwrapped) and -90 <= minlat < maxlat <= 90):
        raise ValueError(f"{what}: {b} is not minlon,minlat,maxlon,maxlat "
                         "with min < max in range (minlon > maxlon, or maxlon "
                         "past 180, for an area across the antimeridian)")
    nb = area.normalize(b)
    if nb[2] - nb[0] > 180:
        # Areas are bounding boxes: one this wide is a band round the world
        # (or, across the antimeridian, most likely swapped minlon/maxlon).
        hint = (" (are minlon and maxlon swapped?)" if area.crosses(nb) else
                "; build the parts separately")
        raise ValueError(f"{what}: {b} is {nb[2] - nb[0]:.0f}° wide; an area is "
                         f"at most 180° wide{hint}")
    return nb


def parse_bbox_arg(value: str) -> BBox:
    parts = value.split(",")
    if len(parts) != 4:
        raise ValueError(f"--bbox {value!r}: need minlon,minlat,maxlon,maxlat")
    a, b, c, d = (float(x) for x in parts)
    return check_bbox((a, b, c, d), "--bbox")


def _unwrap_ring(lons: list[float]) -> list[float]:
    """A ring's longitudes, unwrapped when it crosses the antimeridian:
    followed the short way from point to point, as a ring drawn across ±180
    (179.9 then -179.9) means. Unchanged otherwise."""
    if max(lons) - min(lons) <= 180:
        return lons
    run = [lons[0]]
    for x in lons[1:]:
        run.append(x - 360 * round((x - run[-1]) / 360))
    if max(run) - min(run) <= 0 or abs(run[-1] - run[0]) >= 180:
        # Followed point to point, the ring goes all the way round (a
        # whole-world or polar ring): there is no box to cut.
        raise ValueError("a .poly ring goes all the way round the world; an "
                         "area is at most 180° wide — use a smaller polygon "
                         "or --bbox")
    return run


def _canon(b: BBox) -> BBox:
    """West back in [-180, 180), keeping the width (see streetzim/area.py)."""
    w, s, e, n = b
    k = 360.0 if w < -180 else -360.0 if w >= 180 else 0.0
    return (w + k, s, e + k, n) if k else b


def poly_parts(text: str) -> list[BBox]:
    """Bounding box of each outer ring of an Osmosis .poly file (holes,
    marked '!', ignored)."""
    return [b for b, _ in _poly_rings(text)]


def _ring_area(lons: list[float], lats: list[float]) -> float:
    """Shoelace area in degrees², scaled by cos(latitude): a size to compare
    parts by, not a measurement."""
    import math
    a = sum(lons[k] * lats[k + 1] - lons[k + 1] * lats[k] for k in range(len(lons) - 1))
    a += lons[-1] * lats[0] - lons[0] * lats[-1]
    return abs(a) / 2 * math.cos(math.radians((min(lats) + max(lats)) / 2))


def _poly_rings(text: str) -> list[tuple[BBox, float]]:
    parts: list[tuple[BBox, float]] = []
    lons: list[float] = []
    lats: list[float] = []
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
            if lons:
                lons = _unwrap_ring(lons)
                parts.append((_canon((min(lons), min(lats), max(lons), max(lats))),
                              _ring_area(lons, lats)))
            depth, hole, lons, lats = 0, False, [], []
            continue
        if depth == 0:
            depth, hole = 1, ln.startswith("!")
            continue
        if not hole:
            x, y = ln.split()[:2]
            lons.append(float(x))
            lats.append(float(y))
    if not parts:
        raise ValueError("no coordinates in .poly file")
    return parts


def parse_poly(text: str) -> BBox:
    """Bounding box of an Osmosis .poly file (holes ignored)."""
    return _union(poly_parts(text))


def _union(boxes: list[BBox]) -> BBox:
    """The narrowest box holding all of `boxes`, which may be the one across
    the antimeridian (Fiji's parts at 177E and 179W)."""
    u = boxes[0]
    for b in boxes[1:]:
        u = min(((min(u[0], b[0] + k), min(u[1], b[1]), max(u[2], b[2] + k), max(u[3], b[3]))
                 for k in (0.0, -360.0, 360.0)),
                key=lambda c: c[2] - c[0])        # ties: no shift, as before
        u = _canon(u)
    return u


def _area(b: BBox) -> float:
    import math
    return (b[2] - b[0]) * (b[3] - b[1]) * math.cos(math.radians((b[1] + b[3]) / 2))


# Two groups of polygon parts share one box when they are less than
# MERGE_GAP degrees apart (coastal islands), or when that box is at most
# MERGE_WASTE times the size of the two boxes it replaces.
MERGE_GAP = 1.0
MERGE_WASTE = 3.0


def _gap(a: BBox, b: BBox) -> float:
    """Degrees between two boxes, the short way round in longitude."""
    lon = min(max(0.0, a[0] - (b[2] + k), (b[0] + k) - a[2]) for k in (0.0, -360.0, 360.0))
    return max(lon, a[1] - b[3], b[1] - a[3])


def area_bbox(rings: list[tuple[BBox, float]]) -> tuple[BBox, list[BBox]]:
    """The box to build for a polygon's rings (each a box and its area), and
    the ring boxes it leaves out.

    Areas are bounding boxes, so parts far apart (the Netherlands and its
    Caribbean islands, 70 degrees west) cannot share one: the box would be
    mostly ocean, millions of tiles. Parts are grouped while they are near
    each other or a group's box stays close to the size of its members
    (Spain keeps the Balearics and the Canaries); the group with the most
    land is built, and the others are returned so they can be named.
    """
    groups: list[_Group] = [(b, [b], a) for b, a in rings]
    while len(groups) > 1:
        best: tuple[float, int, int, BBox] | None = None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                u = _union([groups[i][0], groups[j][0]])
                waste = _area(u) / max(_area(groups[i][0]) + _area(groups[j][0]), 1e-12)
                if _gap(groups[i][0], groups[j][0]) < MERGE_GAP:
                    waste = 0.0
                if best is None or waste < best[0]:
                    best = (waste, i, j, u)
        assert best is not None
        waste, i, j, u = best
        if waste > MERGE_WASTE:
            break
        merged: _Group = (u, groups[i][1] + groups[j][1], groups[i][2] + groups[j][2])
        groups = [g for k, g in enumerate(groups) if k not in (i, j)] + [merged]
    groups.sort(key=lambda g: g[2], reverse=True)
    return groups[0][0], [p for g in groups[1:] for p in g[1]]


def parse_default_view(value: str) -> tuple[float, float, float | None]:
    parts = value.split(",")
    if len(parts) not in (2, 3):
        raise ValueError("--default-view needs latitude,longitude[,zoom]")
    lat, lon = float(parts[0]), float(parts[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"--default-view {value!r} is not latitude,longitude")
    return lat, lon, (float(parts[2]) if len(parts) == 3 else None)


def _source_stamp(url: str) -> dict[str, str] | None:
    """What identifies the current version of `url` (HEAD for http(s), size
    and mtime for file://); None when it can't be checked (offline)."""
    if url.startswith("file://"):
        st = os.stat(url[len("file://"):])
        return {"size": str(st.st_size), "mtime": str(int(st.st_mtime))}
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            h = r.headers
            return {k: h.get(k, "") for k in ("ETag", "Last-Modified", "Content-Length")}
    except OSError:
        return None


def fetch(url: str, dest: Path) -> Path:
    """Download into --dl, reusing a previous download only while the source
    is unchanged (same ETag/Last-Modified/size; for file://, size and mtime).
    Geofabrik's -latest files change daily, so an old copy is refreshed."""
    meta = dest.with_name(dest.name + ".source.json")
    stamp = _source_stamp(url)
    if dest.exists() and dest.stat().st_size > 0 and meta.exists():
        old = json.loads(meta.read_text())
        if stamp is None:
            print(f"  Reusing {dest} (could not check {url} for updates)")
            return dest
        if old == stamp:
            print(f"  Reusing {dest} (unchanged upstream)")
            return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    print(f"  Downloading {url}")
    from streetzim import scraperlib
    if scraperlib.AVAILABLE and not url.startswith("file://"):
        scraperlib.download(url, user_agent=USER_AGENT, dest=part)   # retries
    else:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    os.replace(part, dest)
    if stamp is not None:
        meta.write_text(json.dumps(stamp))
    else:
        meta.unlink(missing_ok=True)
    return dest


def _name_of_url(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[-1])


# ---------------------------------------------------------------- main


def plan(args: argparse.Namespace, dl: Path, *, illustration: Path | None = None
         ) -> tuple[list[str], dict[str, str | None]]:
    """Resolve the area and inputs, returning create_osm_zim arguments.

    Downloads the .poly and .pbf into `dl`. Raises ValueError on bad flags.
    Text flags are expected with {name}/{period} already filled in.
    """
    from create_osm_zim import KNOWN_AREAS  # after STREETZIM_CACHE_DIR is set

    sources = [bool(args.area), bool(args.include_poly), bool(args.bbox)]
    if sum(sources) != 1:
        raise ValueError("give exactly one of --area, --include-poly, --bbox")
    geofabrik: str | None = None
    if args.area:
        key = args.area.lower().replace(" ", "-")
        if key not in KNOWN_AREAS:
            raise ValueError(f"unknown --area {args.area!r}; known: "
                             + ", ".join(sorted(KNOWN_AREAS)))
        area = KNOWN_AREAS[key]
        geofabrik, bbox = area["geofabrik"], area["bbox"]
    elif args.include_poly:
        urls = [u.strip() for u in args.include_poly.split(",") if u.strip()]
        rings: list[tuple[BBox, float]] = []
        for u in urls:
            rings += _poly_rings(fetch(u, dl / "poly" / _name_of_url(u)).read_text())
        box, left_out = area_bbox(rings)
        if left_out:
            print(f"  WARNING: --include-poly has parts too far apart for one box; "
                  f"building {box} and leaving out {len(left_out)} part(s): "
                  + "; ".join(",".join(f"{v:.2f}" for v in b) for b in left_out)
                  + ". Build those with --bbox.", flush=True)
        bbox = ",".join(f"{v:.6f}" for v in check_bbox(box, "--include-poly"))
        m = GEOFABRIK_POLY.match(urls[0])
        if len(urls) == 1 and m:
            geofabrik = m.group(1)
    else:
        parse_bbox_arg(args.bbox)
        bbox = args.bbox

    pbf_url: str | None = args.pbf_url or (
        f"https://download.geofabrik.de/{geofabrik}-latest.osm.pbf" if geofabrik else None)
    if not pbf_url and not args.mbtiles:
        raise ValueError("no OSM extract for this area: give --pbf-url "
                         "(or --mbtiles, which builds without routing)")
    if not pbf_url and args.routing:
        raise ValueError("--routing needs an OSM extract: give --pbf-url, or --no-routing")

    # --flag=value throughout: a value may start with "-" (a western
    # longitude, a title), which argparse would otherwise read as a flag.
    argv = [f"--bbox={bbox}", f"--name={args.title}", f"--zim-name={args.name}",
            f"--title={args.title}", f"--description={args.description}",
            f"--creator={args.creator}", f"--publisher={args.publisher}",
            f"--scraper=streetzim v{version()}", "--split-find-chips"]
    if pbf_url:
        argv += ["--pbf", str(fetch(pbf_url, dl / "osm" / _name_of_url(pbf_url)))]
    if args.mbtiles:
        argv += ["--mbtiles", str(Path(args.mbtiles).resolve())]
    if args.long_description:
        argv += [f"--long-description={args.long_description}"]
    if args.tags:
        argv += [f"--tags={args.tags}"]
    if illustration:
        argv += ["--illustration", str(illustration)]
    if args.stats_filename:
        argv += ["--stats-filename", str(Path(args.stats_filename).resolve())]
    if args.routing:
        argv += ["--routing", "--spatial-chunk-scale", "10"]
    if args.wikidata:
        argv += ["--wikidata"]
    if args.terrain:
        argv += ["--terrain"]
    if args.kiwix_poi_pages:
        argv += ["--kiwix-poi-pages"]
    if args.max_zoom is not None:
        argv += ["--max-zoom", str(args.max_zoom)]
    if args.zim_workers:
        argv += ["--workers", str(args.zim_workers)]
    if args.default_view:
        lat, lon, zoom = parse_default_view(args.default_view)
        argv += [f"--map-center={lon},{lat}"]
        if zoom is not None:
            argv += ["--map-zoom", str(round(zoom))]
    if args.debug or args.keep_temp:
        argv += ["--keep-temp"]
    argv += satellite_argv(args)
    return argv, {"bbox": bbox, "pbf_url": pbf_url}


def ensure_shapefiles(folder: Path) -> Path:
    from streetzim.tiles import required_shapefiles   # the tilemaker config's list
    if all((folder / s).exists() for s in required_shapefiles()):
        return folder
    print(f"  Fetching coastline and Natural Earth shapefiles into {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["bash", str(RESOURCES_DIR / "tilemaker" / "fetch-shapefiles.sh"),
                    str(folder)], check=True)
    return folder


def _error(msg: object) -> int:
    print(f"streetzim: error: {msg}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    missing = missing_runtime_files()
    if missing:
        return _error(f"missing from {RESOURCES_DIR}: {', '.join(missing)}; "
                      "reinstall streetzim, or run it from a checkout")
    out_dir = Path(args.output).resolve()
    tmp = Path(args.tmp or (Path(tempfile.gettempdir()) / "streetzim")).resolve()
    tmp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(tmp)
    # Downloads and caches (OSM extract, DEM, Wikidata) go to --dl, else
    # <tmp>/dl as in maps2zim -- never to the output folder, whatever
    # STREETZIM_CACHE_DIR the environment (e.g. the Docker image) sets.
    dl = Path(args.dl or (tmp / "dl")).resolve()
    os.environ["STREETZIM_CACHE_DIR"] = str(dl / "cache")

    # {name} and {period} in the text flags, as maps2zim does.
    for key in ("title", "description", "long_description", "tags"):
        if getattr(args, key):
            setattr(args, key, fill(getattr(args, key), args.name))

    # Everything that can be checked cheaply is checked before downloading.
    illustration: Path | None = None
    try:
        apply_satellite(args)
        from streetzim.zim_metadata import build_overrides, load_illustration
        build_overrides(name=args.name, title=args.title, description=args.description,
                        long_description=args.long_description, creator=args.creator,
                        publisher=args.publisher, tags=args.tags, flavour=args.flavour)
        final = out_dir / zim_filename(args.file_name, args.name)
        if args.default_view:
            parse_default_view(args.default_view)
        if args.bbox:
            parse_bbox_arg(args.bbox)
        if args.illustration_url:
            illustration = tmp / "illustration-48.png"
            illustration.write_bytes(load_illustration(args.illustration_url))
    except (ValueError, OSError) as e:
        return _error(e)
    out_dir.mkdir(parents=True, exist_ok=True)
    if final.exists() and not args.overwrite:
        return _error(f"{final} exists (use --overwrite)")
    # The build writes <final>.tmp. Check that name, not the final one: the
    # check creates and deletes the file it is given.
    building = final.with_name(final.name + ".tmp")
    try:
        from streetzim import scraperlib
        if scraperlib.AVAILABLE:
            scraperlib.check_output(out_dir, building.name)
        else:
            with tempfile.NamedTemporaryFile(dir=out_dir):
                pass
    except OSError as e:
        return _error(f"cannot write to {out_dir}: {e}")
    if args.stats_filename:
        # Before the downloads, which can take a while on big regions.
        from streetzim.progress import StatsFile
        StatsFile(Path(args.stats_filename).resolve()).write(0, 1)

    try:
        build_args, _ = plan(args, dl, illustration=illustration)
    except ValueError as e:
        return _error(e)

    # Build next to the target and rename at the end, so a failed or
    # interrupted run never leaves (or replaces) a .zim in the output folder.
    # (libzim itself writes <path>.tmp and renames it when it finishes.)
    for stale in (building, building.with_name(building.name + ".tmp")):
        stale.unlink(missing_ok=True)       # left by an interrupted run
    build_args += ["-o", str(building)]
    cwd = os.getcwd()
    if not args.mbtiles:
        # tilemaker reads the shapefiles relative to the working directory.
        os.chdir(ensure_shapefiles(Path(args.shapefiles or (dl / "shapefiles")).resolve()))
    try:
        import create_osm_zim
        # The builder module itself is not typed (pyright basic mode).
        create_osm_zim.main(build_args)  # pyright: ignore[reportUnknownMemberType]
    finally:
        os.chdir(cwd)
    os.replace(building, final)
    print(f"streetzim: wrote {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
