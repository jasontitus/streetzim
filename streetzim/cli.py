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

--profile picks the feature set (full, the default, or basic; see PROFILES
and docs/zimfarm.md); a feature flag given explicitly overrides it.

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
import contextlib
import datetime
import http.client
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Keep the imports above stdlib-only: --dl must reach STREETZIM_CACHE_DIR
# before streetzim.common is first imported (it reads it at import time).
# streetzim.area is stdlib-only.

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:     # also runnable as `python streetzim/cli.py`
    sys.path.insert(0, str(REPO_ROOT))
from streetzim import area, cpus, download  # noqa: E402  (after the path fix above)
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
    "mbtiles_url": {"title": "MBTiles URL", "type": "url"},
    "no_routing": {"title": "No routing",
                   "description": "Leave out offline routing (on by default)"},
    "wikidata": {"title": "Wikidata"},
    "terrain": {"title": "Terrain"},
    "kiwix_poi_pages": {"title": "POIs in Kiwix search"},
    "default_view": {"title": "Default view"},
    "output": {"pattern": r"^/output$"},
    "stats_filename": {"pattern": r"^/output/task_progress\.json$"},
    "zim_workers": {"title": "ZIM workers", "min": 1},
    "cpus": {"title": "CPU cores", "min": 1},
    "zim_builder": {"offliner": False},
    "xapian": {"offliner": False},
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

# ---------------------------------------------------------------- profiles
# --profile sets the content features; any of them given on the command line
# (--wikidata, --wikidata=off, --no-wikidata, ...) wins over the profile.
#   full:  what StreetZim's own builds ship that openZIM can ship too
#          (docs/zimfarm.md, "Feature parity"). Satellite is in no profile:
#          it is opt-in (the 2021 source is non-commercial).
#   basic: nothing fetched besides the OSM extract and the shapefiles.
# Each feature is one on/off flag (a string-enum on Zimfarm; unset: the
# profile decides), so a recipe cannot say both. --terrain and
# --kiwix-poi-pages are defined as plain flags in the "Content" group below;
# add_profile_arguments (_adopt) replaces each with an on/off flag.
PROFILES: dict[str, dict[str, bool]] = {
    "full": {"wikidata": True, "wikipedia": True, "overture": True, "terrain": True,
             "kiwix_poi_pages": True},
    "basic": {"wikidata": False, "wikipedia": False, "overture": False, "terrain": False,
              "kiwix_poi_pages": False},
}
DEFAULT_PROFILE = "full"
FEATURE_NAMES = {"wikidata": "Wikidata", "wikipedia": "Wikipedia articles",
                 "overture": "Overture Maps", "terrain": "terrain",
                 "kiwix_poi_pages": "POIs in Kiwix search"}
# Joins the profile only when it can also be turned off where it is defined
# (--terrain/--no-terrain).
NEEDS_OFF_SWITCH = {"terrain"}
ON_OFF = ("on", "off")
WIKIPEDIA_IMAGES = ("none", "lead", "all")
OVERTURE_THEMES = ("addresses", "places")
ZIMFARM.update({
    # Required on Zimfarm (the command line defaults to full), so every
    # recipe states its profile next to the resources it is given.
    "profile": {"title": "Profile", "required": True},
    "wikipedia": {"title": "Wikipedia articles"},
    "wikipedia_zim_url": {"title": "Wikipedia ZIM URL", "type": "url"},
    "wikipedia_images": {"title": "Wikipedia images"},
    "overture": {"title": "Overture Maps"},
    "overture_release": {"title": "Overture release",
                         "pattern": r"^(latest|[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+)$"},
})


def version() -> str:
    from streetzim.__about__ import __version__
    return __version__



def positive_int(text: str) -> int:
    """argparse type for a count of at least 1, so a bad value fails before
    any download."""
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {n}")
    return n

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="streetzim", allow_abbrev=False,
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
                     help="Use this OpenMapTiles MBTiles file (e.g. OpenFreeMap) "
                          "instead of generating tiles with tilemaker")
    src.add_argument("--mbtiles-url",
                     help="URL of an OpenMapTiles MBTiles (e.g. OpenFreeMap's) to "
                          "use instead of generating tiles with tilemaker: "
                          "downloaded into the download folder (resumed if "
                          "interrupted, reused while unchanged upstream), or "
                          "a file:// URL used in place. Tiles outside the "
                          "area are left out. See docs/zimfarm.md")
    src.add_argument("--shapefiles",
                     help="Folder with coastline/ and landcover/ for tilemaker. "
                          "Default: <dl>/shapefiles, fetched when missing")

    feat = p.add_argument_group("Content")
    feat.add_argument("--routing", action=argparse.BooleanOptionalAction, default=True,
                      help="Offline routing (driving, walking, cycling). Default: on")
    feat.add_argument("--wikidata", action="store_true",
                      help="Wikidata place details (needs network access to Wikidata)")
    feat.add_argument("--terrain", action=argparse.BooleanOptionalAction, default=True,
                      help="Hillshade and 3D terrain from the Copernicus DEM "
                           "(downloads DEM tiles for the area)")
    feat.add_argument("--kiwix-poi-pages", action="store_true",
                      help="Also list every named POI in Kiwix's own search, "
                           "not only "
                           "places, parks, peaks, water and airports. Adds "
                           "about 440 B per POI (+16%% on Luxembourg). Default: off")
    feat.add_argument("--max-zoom", type=int, choices=range(0, 15), metavar="{0..14}",
                      help="Maximum zoom of the vector tiles. Default: 14")
    feat.add_argument("--default-view",
                      help="Initial map view as latitude,longitude[,zoom]")
    feat.add_argument("--zim-workers", type=positive_int,
                      help="Compression threads for libzim. Default: --cpus, "
                           "else the usable cores within any CPU quota, at most 20")
    feat.add_argument("--zim-builder", choices=["python", "manifest"], default="python",
                      help="ZIM writer: python uses libzim (default); manifest uses "
                           "the Python packer with custom compression and raw namespaces")
    feat.add_argument("--xapian", choices=["libzim", "builder", "none"],
                      help="Native search index: default libzim for the libzim writer, "
                           "none for manifest; builder uses the external xapianbuilder")
    feat.add_argument("--cpus", type=positive_int,
                      help="CPU cores the build uses at once (tilemaker, "
                           "search, terrain, compression). Set it to the task's "
                           "CPUs: a CPU share hides no cores, and each core "
                           "costs memory. Default: the usable cores, at most "
                           "the container's CPU quota and one per "
                           f"{cpus.GIB_PER_CPU} GiB of its memory limit")
    feat.add_argument("--keep-temp", action="store_true", help=argparse.SUPPRESS)
    add_profile_arguments(p)
    add_satellite_flags(p)
    return p


def _profile_default_text(dest: str) -> str:
    on = [name for name, feats in PROFILES.items() if feats.get(dest)]
    off = [name for name, feats in PROFILES.items() if dest in feats and not feats[dest]]
    return (f"Default: on with --profile {', '.join(on)}, off with "
            f"{', '.join(off)}")


class OnOff(argparse.Action):
    """A profile feature: `--x` or `--x=on`, `--x=off` or `--no-x`. Stores
    True/False; left None when not given, for the profile to decide. Saying
    both on and off is an error."""

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: Any, option_string: str | None = None) -> None:
        value = (values or self.const) == "on"
        before = getattr(namespace, self.dest, None)
        if before is not None and before != value:
            parser.error(f"{option_string} contradicts an earlier "
                         f"--{self.dest.replace('_', '-')} flag")
        setattr(namespace, self.dest, value)


def _add_feature(group: Any, dest: str, help: str) -> None:   # an argument group
    name = "--" + dest.replace("_", "-")
    group.add_argument(name, dest=dest, action=OnOff, nargs="?", const="on",
                       choices=ON_OFF, default=None, help=help)
    group.add_argument("--no-" + name[2:], dest=dest, action=OnOff, nargs=0,
                       const="off", default=None, help=f"The same as {name}=off")


def _drop(p: argparse.ArgumentParser, action: argparse.Action) -> None:
    p._remove_action(action)
    for g in p._action_groups:
        if action in g._group_actions:
            g._group_actions.remove(action)
    for o in action.option_strings:
        p._option_string_actions.pop(o, None)


def _adopt(p: argparse.ArgumentParser, dest: str) -> str | None:
    """Replace the boolean flag(s) another part of the parser defines for a
    profile feature by nothing, returning their help text; None when there
    is no such flag (yet), or it cannot be turned off (NEEDS_OFF_SWITCH)."""
    olds = [a for a in p._actions if a.dest == dest and a.nargs == 0
            and not isinstance(a, OnOff)]
    if not olds:
        return None
    if dest in NEEDS_OFF_SWITCH and not any(
            o.startswith("--no-") for a in olds for o in a.option_strings):
        return None
    text = next((a.help for a in olds if a.help and a.help != argparse.SUPPRESS), "") or ""
    for a in olds:
        _drop(p, a)
    # Its own "Default: ..." gives way to the profile's.
    return re.sub(r"[.\s]*Default: [^.]*\.?\s*$", "", text)


def add_profile_arguments(p: argparse.ArgumentParser) -> None:
    """--profile and the features it sets (see PROFILES)."""
    prof = p.add_argument_group(
        "Profile", "Each feature is on, off, or (unset) as --profile says: "
                   "--x or --x=on, --x=off or --no-x")
    profile = prof.add_argument("--profile", choices=list(PROFILES))
    helps: dict[str, str] = {
        "wikipedia": "English Wikipedia articles for the places that have one, "
                     "stored in the ZIM (CC BY-SA). Text from the Wikipedia API, "
                     "or text and images from --wikipedia-zim-url",
        "overture": "Overture Maps addresses and place details (websites, "
                    "phones, brands, categories), read from Overture's public "
                    "S3 bucket over HTTPS",
    }
    for dest in PROFILES[DEFAULT_PROFILE]:
        text = helps.get(dest) or _adopt(p, dest)
        if text is not None:
            _add_feature(prof, dest, f"{text.rstrip('.')}. {_profile_default_text(dest)}")
    prof.add_argument("--wikipedia-zim-url",
                      help="Kiwix Wikipedia ZIM (download.kiwix.org) to read the "
                           "articles and their images from instead of the API. "
                           "Downloaded for every task: a full English one is 50 to "
                           "120 GB. Default: none (the API, text only)")
    prof.add_argument("--wikipedia-images", choices=WIKIPEDIA_IMAGES, default="all",
                      help="With --wikipedia-zim-url: the articles' images to store "
                           "(lead: the first picture). Default: all")
    prof.add_argument("--overture-release", default="latest",
                      help="Overture release, e.g. 2026-09-23.1. Default: latest "
                           "(the newest complete release)")
    sets = {name: [FEATURE_NAMES.get(d, d) for d, on in feats.items()
                   if on and d in profile_features(p)]
            for name, feats in PROFILES.items()}
    profile.help = ("Feature set: "
                    + "; ".join(f"{name}: {', '.join(on) or 'none of them'}"
                                for name, on in sets.items())
                    + f" (routing is on in both). Default: {DEFAULT_PROFILE}")


def profile_features(parser: argparse.ArgumentParser) -> set[str]:
    """The profile features this parser has an on/off flag for."""
    return {a.dest for a in parser._actions if isinstance(a, OnOff)}


def apply_profile(args: argparse.Namespace,
                  parser: argparse.ArgumentParser | None = None) -> argparse.Namespace:
    """Fill the features the command line leaves unset from --profile.
    Raises ValueError on flags that cannot go together."""
    parser = parser or build_parser()
    args.profile = args.profile or DEFAULT_PROFILE
    features = profile_features(parser)
    for dest, value in PROFILES[args.profile].items():
        if dest in features and getattr(args, dest, None) is None:
            setattr(args, dest, value)
    if args.wikipedia_zim_url and not args.wikipedia:
        raise ValueError("--wikipedia-zim-url needs --wikipedia (off with "
                         f"--profile {args.profile})")
    return args


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.zim_workers is not None and args.zim_workers < 1:
            raise ValueError("--zim-workers must be greater than zero")
        if args.zim_builder == "manifest" and args.xapian == "libzim":
            raise ValueError("--zim-builder=manifest requires --xapian=builder or --xapian=none")
        if args.xapian == "builder" and args.zim_builder != "manifest":
            raise ValueError("--xapian=builder requires --zim-builder=manifest")
        return apply_profile(args, parser)
    except ValueError as e:
        parser.error(str(e))


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


def _head(url: str) -> dict[str, str] | None:
    """The response headers for `url` (HEAD, or a one-byte GET when HEAD is
    refused); None when it fails (offline)."""
    return download.head(url, USER_AGENT)


def _source_stamp(url: str, head: dict[str, str] | None = None) -> dict[str, str] | None:
    """What identifies the current version of `url` (HEAD for http(s), size
    and mtime for file://); None when it can't be checked (offline)."""
    if url.startswith("file://"):
        from urllib.parse import urlsplit
        st = os.stat(urllib.request.url2pathname(urlsplit(url).path))
        return {"size": str(st.st_size), "mtime_ns": str(st.st_mtime_ns)}
    return download.stamp_of(head if head is not None else _head(url))


def fetch_resumable(url: str, dest: Path, *,
                    check_head: Callable[[bytes], None] | None = None,
                    trust_preseeded: bool = True) -> Path:
    """A large download into --dl: resumed, reused, checked and locked as
    streetzim/download.py describes."""
    return download.fetch_resumable(url, dest, user_agent=USER_AGENT, check_head=check_head,
                                   trust_preseeded=trust_preseeded)


def fetch(url: str, dest: Path) -> Path:
    """Download into --dl with per-file locking and complete-body checks.

    HTTP inputs use the resumable downloader, shared with MBTiles. Local
    file URLs retain size/mtime reuse and are copied atomically under the
    same lock so parallel builds cannot share an in-progress .part file.
    """
    if not url.startswith("file://"):
        return fetch_resumable(url, dest, trust_preseeded=False)
    meta = dest.with_name(dest.name + ".source.json")
    part = dest.with_name(dest.name + ".part")
    with download.file_lock(dest):
        stamp = _source_stamp(url)
        if (dest.exists() and stamp is not None
                and dest.stat().st_size == int(stamp["size"])
                and download.read_download_metadata(meta) == stamp):
            print(f"  Reusing {dest} (unchanged source)")
            return dest
        print(f"  Downloading {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            if (stamp is None or part.stat().st_size != int(stamp["size"])
                    or _source_stamp(url) != stamp):
                raise OSError(f"{url}: local source changed or was cut short during the copy")
            os.replace(part, dest)
            download.write_download_metadata(meta, stamp)
        finally:
            part.unlink(missing_ok=True)
    return dest


def _check_mbtiles_head(head: bytes) -> None:
    from streetzim import mbtiles
    if not mbtiles.looks_like_sqlite(head):
        raise ValueError("--mbtiles-url is not an MBTiles file (not SQLite)")


def record_url(url: str) -> str:
    """The URL as the ZIM records it: no user name, password, query or
    fragment (which may carry a token); for file://, the file name only
    (a path on the build host means nothing to a reader)."""
    from urllib.parse import unquote, urlsplit, urlunsplit
    u = urlsplit(url)
    if u.scheme == "file":
        return unquote(u.path).rsplit("/", 1)[-1]
    netloc = u.hostname or ""
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if u.port:
        netloc += f":{u.port}"
    return urlunsplit((u.scheme, netloc, u.path, "", ""))


def mbtiles_source(args: argparse.Namespace, dl: Path) -> tuple[Path, str | None] | None:
    """The MBTiles to build from and the URL to record for it: --mbtiles as
    given, a file:// --mbtiles-url in place, or an http(s) one downloaded
    into <dl>/mbtiles. ValueError if it is missing or not an MBTiles."""
    if args.mbtiles:
        path = Path(args.mbtiles).resolve()
        if not path.is_file():
            raise ValueError(f"--mbtiles {args.mbtiles}: no such file")
        return path, None
    url: str | None = args.mbtiles_url
    if not url:
        return None
    if url.startswith("file://"):
        from urllib.parse import unquote, urlparse
        path = Path(unquote(urlparse(url).path))
        if not path.is_file():
            raise ValueError(f"--mbtiles-url {record_url(url)}: no such file")
        return path, record_url(url)
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"--mbtiles-url {record_url(url)!r}: need an http(s):// or "
                         "file:// URL (or --mbtiles for a local path)")
    try:
        return fetch_resumable(url, dl / "mbtiles" / _name_of_url(url),
                               check_head=_check_mbtiles_head), record_url(url)
    except (OSError, http.client.HTTPException) as e:
        raise ValueError(f"--mbtiles-url {record_url(url)}: {e}") from e


def prepare_mbtiles(path: Path, bbox: BBox | None, work: Path,
                    max_zoom: int | None = None) -> tuple[Path, dict[str, str]]:
    """Check the MBTiles and log what it is; with an area, cut it to the
    tiles touching the area's box into `work` (streetzim/mbtiles.py). The
    cut goes to z14 whatever --max-zoom says: the builder caps the tiles
    it stores, and still reads z14 for search. Without a box every tile is
    kept. `max_zoom` is accepted for callers and ignored."""
    from streetzim import mbtiles
    del max_zoom
    meta = mbtiles.check(path)
    print(f"  MBTiles: {mbtiles.describe(meta)}", flush=True)
    if bbox is None:
        return path, meta
    import time
    t0 = time.monotonic()
    work.mkdir(parents=True, exist_ok=True)
    out = work / "area.mbtiles"
    counts = mbtiles.cut(path, out, bbox, max_zoom=mbtiles.MAX_ZOOM)
    print(f"  Cut to the area: {sum(counts.values()):,} tiles "
          f"({', '.join(f'z{z} {n:,}' for z, n in counts.items() if n)}) "
          f"in {time.monotonic() - t0:.1f}s, {out.stat().st_size / 1e6:,.1f} MB", flush=True)
    return out, meta


def _name_of_url(url: str) -> str:
    return download.name_of_url(url)


# ---------------------------------------------------------------- main


def plan(args: argparse.Namespace, dl: Path, *, illustration: Path | None = None,
         work: Path | None = None) -> tuple[list[str], dict[str, str | None]]:
    """Resolve the area and inputs, returning create_osm_zim arguments.

    Downloads the .poly, .pbf and MBTiles into `dl`, and cuts an MBTiles
    larger than the area into `work` (default: a new temporary folder).
    Raises ValueError on bad flags.
    Text flags are expected with {name}/{period} already filled in.
    """
    from create_osm_zim import KNOWN_AREAS  # after STREETZIM_CACHE_DIR is set

    sources = [bool(args.area), bool(args.include_poly), bool(args.bbox)]
    if sum(sources) != 1:
        raise ValueError("give exactly one of --area, --include-poly, --bbox")
    if args.mbtiles and args.mbtiles_url:
        raise ValueError("give --mbtiles or --mbtiles-url, not both")
    has_tiles = bool(args.mbtiles or args.mbtiles_url)
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
    if not pbf_url and not has_tiles:
        raise ValueError("no OSM extract for this area: give --pbf-url "
                         "(or --mbtiles / --mbtiles-url, which build without routing)")
    if not pbf_url and args.routing:
        raise ValueError("--routing needs an OSM extract: give --pbf-url, or --no-routing")

    # The MBTiles before the extract: a bad one fails before that download.
    tiles_argv: list[str] = []
    cut: str | None = None
    source = mbtiles_source(args, dl)
    if source is not None:
        work = work or Path(tempfile.mkdtemp(prefix="streetzim-mbtiles-"))
        from streetzim.area import normalize    # (`area` is the preset here)
        box = normalize([float(v) for v in bbox.split(",")])
        path, _ = prepare_mbtiles(source[0], box, work)
        if path != source[0]:
            cut = str(path)
        tiles_argv = ["--mbtiles", str(path), "--record-tile-source"]
        if source[1]:
            tiles_argv.append(f"--tile-source-url={source[1]}")
    try:
        argv = _builder_argv(args, bbox, pbf_url, dl, illustration) + tiles_argv
    except BaseException:
        drop_cut(cut)                           # a failed extract download, or SIGTERM
        raise
    return argv, {"bbox": bbox, "pbf_url": pbf_url, "mbtiles_cut": cut}


def drop_cut(cut: str | None) -> None:
    """Remove the cut MBTiles (and its folder when that is left empty)."""
    if cut:
        Path(cut).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            Path(cut).parent.rmdir()


def _builder_argv(args: argparse.Namespace, bbox: str, pbf_url: str | None, dl: Path,
                  illustration: Path | None) -> list[str]:
    """create_osm_zim's arguments, but for the tiles; downloads the extract."""
    # --flag=value throughout: a value may start with "-" (a western
    # longitude, a title), which argparse would otherwise read as a flag.
    argv = [f"--bbox={bbox}", f"--name={args.title}", f"--zim-name={args.name}",
            f"--title={args.title}", f"--description={args.description}",
            f"--creator={args.creator}", f"--publisher={args.publisher}",
            f"--scraper=streetzim v{version()}", "--split-find-chips"]
    if pbf_url:
        argv += ["--pbf", str(fetch(pbf_url, dl / "osm" / _name_of_url(pbf_url)))]
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
    if args.zim_workers is not None:
        argv += ["--workers", str(args.zim_workers)]
    if args.cpus is not None:
        argv += ["--cpus", str(args.cpus)]
    if args.zim_builder == "manifest":
        argv += ["--zim-builder", "manifest", "--xapian", args.xapian or "none"]
    elif args.xapian is not None:
        argv += ["--xapian", args.xapian]
    if args.default_view:
        lat, lon, zoom = parse_default_view(args.default_view)
        argv += [f"--map-center={lon},{lat}"]
        if zoom is not None:
            argv += ["--map-zoom", str(round(zoom))]
    if args.debug or args.keep_temp:
        argv += ["--keep-temp"]
    argv += LAYOUT_ARGS + profile_feature_args(args, dl, bbox) + satellite_argv(args)
    return argv


# What StreetZim's own builds (ops/build-region-fast.sh) pass whatever the
# content: search-data chunks over 10 MB split in 16 (slow to fetch on iOS
# otherwise), and no category-index/{addr,poi,street}.json, the bulk
# records only an external LLM tool read (the Find page uses the chip files).
LAYOUT_ARGS = ["--split-hot-search-chunks-mb", "10", "--no-llm-bundle"]


def profile_feature_args(args: argparse.Namespace, dl: Path, bbox: str) -> list[str]:
    """create_osm_zim arguments for the features --profile sets (other than
    Wikidata and terrain, which plan() passes), fetching their inputs into
    `dl`. Overture data or a Wikipedia ZIM that cannot be fetched fails the
    build rather than shipping without it; the Wikimedia APIs are
    best-effort in the builder (an article still rate limited after its
    retries is left out)."""
    out: list[str] = []
    if getattr(args, "wikipedia", None):
        (dl / "wikipedia").mkdir(parents=True, exist_ok=True)   # the builder's caches
        out += ["--resolve-wikidata-titles",
                "--wikidata-title-cache", str(dl / "wikipedia" / "qid_titles.json"),
                "--bundle-wiki-articles",
                "--wiki-articles-cache", str(dl / "wikipedia" / "articles")]
        if getattr(args, "wikipedia_zim_url", None):
            url = args.wikipedia_zim_url
            src = fetch(url, dl / "wikipedia" / _name_of_url(url))
            out += ["--wiki-articles-source", str(src),
                    "--wiki-images", args.wikipedia_images, "--wiki-image-max-kb", "128"]
    if getattr(args, "overture", None):
        for theme, path in fetch_overture(bbox, args.overture_release, dl).items():
            out += [f"--overture-{theme}", str(path)]
    return out


def fetch_overture(bbox: str, release: str, dl: Path) -> dict[str, Path]:
    """Overture's addresses and places for the box, as parquet files in
    `dl`, from one release (`latest` is resolved once, for both themes)."""
    import hashlib

    import duckdb

    import download_overture_data as ov  # repository root, or the wheel's module
    try:
        # The script is not typed (`themes` has no annotation).
        resolved: str = ov.resolve_release(  # pyright: ignore[reportUnknownMemberType]
            release, list(OVERTURE_THEMES))
        key = hashlib.sha1(bbox.encode()).hexdigest()[:12]
        out: dict[str, Path] = {}
        for theme in OVERTURE_THEMES:
            dest = dl / "overture" / f"{theme}-{resolved}-{key}.parquet"
            if not dest.exists():
                # download_overture reuses any file at its path: write
                # elsewhere so an interrupted run leaves nothing to reuse.
                part = dest.with_name(dest.name + ".part")
                part.parent.mkdir(parents=True, exist_ok=True)
                part.unlink(missing_ok=True)
                ov.download_overture(theme, bbox, resolved, str(part))
                os.replace(part, dest)
            out[theme] = dest
        return out
    except SystemExit as e:        # the script's way of reporting a failure
        raise ValueError(f"Overture Maps: {e.code}") from e
    except (duckdb.Error, OSError) as e:
        raise ValueError(f"Overture Maps: could not fetch the {bbox} extract "
                         f"({type(e).__name__}: {e}); --no-overture builds "
                         "without it") from e


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


# What a build killed before its cleanup ran (OOM killer, SIGKILL) leaves
# behind: its staging archive next to the output (and libzim's <staging>.tmp)
# and its workspace in --tmp. Their names carry the owner's PID, and later
# builds remove those whose owner is gone and that nothing has touched for
# STALE_AFTER. The age also covers folders shared between machines or PID
# namespaces, where a live owner's PID can look unused. Leftovers kept on
# purpose (--debug, --keep-temp, an archive that could not be published)
# have a <name>.keep marker and are never removed.
STALE_AFTER = 6 * 3600
STAGING_NAME = re.compile(r"^\..+\.zim\.(\d+)\.[a-z0-9_]{8}\.building(?:\.tmp)?$")
WORKSPACE_NAME = re.compile(r"^streetzim-build-(\d+)-[a-z0-9_]{8}$")
KEEP_SUFFIX = ".keep"


def _pid_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return True                     # (kill(0) would signal our process group)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:                     # EPERM: it exists, owned by another user
        return True
    return True


def _newest_mtime(path: Path) -> float:
    """The latest modification time of `path` and, for a folder, of
    everything in it (symbolic links are not followed)."""
    newest = path.lstat().st_mtime
    if path.is_dir() and not path.is_symlink():
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                with contextlib.suppress(OSError):
                    newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
    return newest


def keep_marker(path: Path) -> Path:
    """The marker that protects a staging archive (and its .tmp) or a
    workspace from sweep_stale()."""
    name = path.name.removesuffix(".tmp") if path.name.endswith(".building.tmp") else path.name
    return path.with_name(name + KEEP_SUFFIX)


def mark_kept(path: Path) -> None:
    with contextlib.suppress(OSError):
        keep_marker(path).touch()


def sweep_stale(folder: Path, pattern: re.Pattern[str], *, folders: bool,
                now: float | None = None) -> list[Path]:
    """Remove the entries of `folder` named by `pattern` (folders when
    `folders`, else files) whose owning process is gone and that are older
    than STALE_AFTER; returns what was removed."""
    import time
    now = time.time() if now is None else now
    removed: list[Path] = []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return removed
    for entry in entries:
        m = pattern.match(entry.name)
        if not m or entry.is_symlink() or entry.is_dir() != folders:
            continue
        try:
            if (_pid_alive(int(m.group(1))) or keep_marker(entry).exists()
                    or now - _newest_mtime(entry) < STALE_AFTER):
                continue
            if folders:
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            continue
        print(f"streetzim: removed {entry}, left by an interrupted build "
              f"(process {m.group(1)})", flush=True)
        removed.append(entry)
    return removed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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

    # The workspace belongs to this invocation, including its illustration
    # and cut MBTiles. Other builds may share --tmp and --dl safely.
    sweep_stale(tmp, WORKSPACE_NAME, folders=True)
    work = Path(tempfile.mkdtemp(prefix=f"streetzim-build-{os.getpid()}-", dir=tmp))
    previous = _exit_on_sigterm()
    try:
        return _run_build(args, dl, out_dir, work)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if args.debug or args.keep_temp:
            mark_kept(work)
        else:
            shutil.rmtree(work, ignore_errors=True)


def _run_build(args: argparse.Namespace, dl: Path, out_dir: Path, work: Path) -> int:
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
            illustration = work / "illustration-48.png"
            illustration.write_bytes(load_illustration(args.illustration_url))
    except (ValueError, OSError) as e:
        return _error(e)
    out_dir.mkdir(parents=True, exist_ok=True)
    sweep_stale(out_dir, STAGING_NAME, folders=False)
    if final.exists() and not args.overwrite:
        return _error(f"{final} exists (use --overwrite)")
    try:
        # Staging lives on the output filesystem for atomic publication, and
        # its unique name prevents concurrent builds from deleting each
        # other's archive (previously both used <final>.tmp). The PID lets
        # sweep_stale() tell when an interrupted build's staging is orphaned.
        with tempfile.NamedTemporaryFile(prefix=f".{final.name}.{os.getpid()}.",
                                         suffix=".building",
                                         dir=out_dir) as probe:
            building = Path(probe.name)
        from streetzim import scraperlib
        if scraperlib.AVAILABLE:
            scraperlib.check_output(out_dir, building.name)
    except OSError as e:
        return _error(f"cannot write to {out_dir}: {e}")
    keep = False
    try:
        if args.stats_filename:
            from streetzim.progress import StatsFile
            StatsFile(Path(args.stats_filename).resolve()).write(0, 1)
        return _build(args, dl, illustration, work, building, final)
    except _Unpublished as e:
        # The archive is complete: never delete it because the last step
        # failed. It is marked so that sweep_stale() leaves it too.
        keep = True
        mark_kept(building)
        return _error(f"could not move the finished archive to {final} ({e.cause}); "
                      f"it is at {building}")
    finally:
        staged = (building, building.with_name(building.name + ".tmp"))
        if args.debug or args.keep_temp:
            if any(p.exists() for p in staged):
                mark_kept(building)
        elif not keep:
            for p in staged:
                p.unlink(missing_ok=True)


def _exit_on_sigterm() -> Any:
    """Make SIGTERM raise SystemExit, so cleanup code runs; returns the
    handler it replaced (None off the main thread, where it cannot be set)."""
    import threading
    if threading.current_thread() is not threading.main_thread():
        return None

    def stop(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)
    return signal.signal(signal.SIGTERM, stop)


def _build(args: argparse.Namespace, dl: Path, illustration: Path | None, work: Path,
           building: Path, final: Path) -> int:
    try:
        build_args, _ = plan(args, dl, illustration=illustration, work=work)
    except (ValueError, OSError, sqlite3.Error, http.client.HTTPException) as e:
        return _error(e)

    # Build next to the target and rename at the end, so a failed or
    # interrupted run never leaves (or replaces) a .zim in the output folder.
    # (libzim itself writes <path>.tmp and renames it when it finishes.)
    for stale in (building, building.with_name(building.name + ".tmp")):
        stale.unlink(missing_ok=True)       # left by an interrupted run
    build_args += ["-o", str(building)]
    cwd = os.getcwd()
    if not (args.mbtiles or args.mbtiles_url):
        # tilemaker reads the shapefiles relative to the working directory.
        os.chdir(ensure_shapefiles(Path(args.shapefiles or (dl / "shapefiles")).resolve()))
    from streetzim import source_report
    source_report.reset()
    try:
        import create_osm_zim
        # The builder module itself is not typed (pyright basic mode).
        create_osm_zim.main(build_args)  # pyright: ignore[reportUnknownMemberType]
    finally:
        os.chdir(cwd)
    print(f"streetzim: {source_report.summary()}")
    try:
        publish(building, final, overwrite=args.overwrite)
    except FileExistsError:
        return _error(f"{final} exists (use --overwrite)")
    except OSError as e:
        raise _Unpublished(e) from e
    print(f"streetzim: wrote {final}")
    return 0


class _Unpublished(Exception):
    """The archive was built but could not be moved into place."""

    def __init__(self, cause: OSError) -> None:
        super().__init__(str(cause))
        self.cause = cause


def publish(building: Path, final: Path, *, overwrite: bool) -> None:
    """Move the finished archive `building` to `final` (same folder).
    Without `overwrite`, raises FileExistsError when `final` appeared while
    building; any other OSError leaves `building` where it is."""
    if overwrite:
        os.replace(building, final)
        return
    # A hard link publishes a completed same-filesystem file atomically and
    # refuses to replace another invocation's successful output.
    try:
        os.link(building, final)
    except FileExistsError:
        raise
    except OSError:
        # Some filesystems have no hard links (EPERM on FAT/exFAT and some
        # network or FUSE mounts, ENOTSUP, EMLINK). A rename cannot refuse
        # an existing destination, so check for one just before.
        if os.path.lexists(final):
            raise FileExistsError(f"{final} exists") from None
        os.replace(building, final)
        return
    with contextlib.suppress(OSError):  # published: the extra name is harmless
        building.unlink()


if __name__ == "__main__":
    sys.exit(main())
