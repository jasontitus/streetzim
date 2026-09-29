"""Where the builder's data files are: in a checkout, or in an installed wheel.

In a checkout (and an editable install) they are the repository's
``resources/`` folder. A wheel carries the files the build reads at run time
as the ``streetzim.resources`` package: pyproject.toml maps ``resources/`` to
it and lists them under package-data. RUNTIME_FILES is that list, and the
``streetzim`` command checks it before building.

It also says where download caches go when STREETZIM_CACHE_DIR is unset
(cache_root), and which cloud/ modules the builder imports (CLOUD_MODULES,
the only ones a wheel ships).

Standard library only: streetzim/cli.py imports this before --dl has set
STREETZIM_CACHE_DIR.
"""
from __future__ import annotations

import os
from pathlib import Path

# The directory holding create_osm_zim.py: the checkout, or site-packages.
REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGED_RESOURCES = Path(__file__).resolve().parent / "resources"
LOCK_NAME = "viewer-assets.lock.json"

# Relative to RESOURCES_DIR; keep in step with [tool.setuptools.package-data]
# in pyproject.toml (tests/test_packaging.py checks the two agree).
RUNTIME_FILES = (
    LOCK_NAME,
    "tilemaker/config-openmaptiles.json",
    "tilemaker/process-openmaptiles.lua",
    "tilemaker/fetch-shapefiles.sh",
    "viewer/index.html",
    "viewer/places.html",
    "viewer/routing-worker.js",
    "vendor/maplibre-gl/maplibre-gl.js",
    "vendor/maplibre-gl/maplibre-gl.css",
    "vendor/maplibre-gl/LICENSE.txt",
    "vendor/mapbox-gl-rtl-text/mapbox-gl-rtl-text.js",
    "vendor/mapbox-gl-rtl-text/LICENSE.md",
)


# The cloud/ modules the builder imports (streetzim/, create_osm_zim.py,
# wikidata_cache.py and these, transitively). A wheel ships only these:
# MANIFEST.in keeps the rest of cloud/, operations code, out of the sdist the
# wheel is built from. tests/test_packaging.py checks the list against an
# import scan; tools/check_wheel_install.py against the installed files.
CLOUD_MODULES = (
    "chip_rules",
    "chip_shards",
    "manifest_writer",
    "search_shards",
    "viewer_slots",
    "wiki_articles",
    "wikidata_titles",
)


def find_resources() -> Path:
    """The installed copy when there is one, else the checkout's resources/."""
    if (PACKAGED_RESOURCES / LOCK_NAME).is_file():
        return PACKAGED_RESOURCES
    return REPO_ROOT / "resources"


RESOURCES_DIR = find_resources()


def missing_runtime_files(root: Path | None = None) -> list[str]:
    """The RUNTIME_FILES not present under root (default RESOURCES_DIR)."""
    base = RESOURCES_DIR if root is None else root
    return [name for name in RUNTIME_FILES if not (base / name).is_file()]


def installed() -> bool:
    """True when running from an installed wheel, not a checkout or an
    editable install."""
    return RESOURCES_DIR == PACKAGED_RESOURCES


def cache_root() -> Path:
    """Where download caches (satellite, DEM, Wikidata, Wikipedia, font
    glyphs) go: $STREETZIM_CACHE_DIR; else the checkout, as always; else,
    installed, $XDG_CACHE_HOME/streetzim or ~/.cache/streetzim, never
    site-packages. The `streetzim` command always sets the variable
    (<--dl>/cache)."""
    env = os.environ.get("STREETZIM_CACHE_DIR")
    if env:
        return Path(env)
    if not installed():
        return REPO_ROOT
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "streetzim"
