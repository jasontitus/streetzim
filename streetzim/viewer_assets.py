"""The third-party files the viewer ships, pinned by SHA-256.

resources/viewer-assets.lock.json records, and tools/pin_viewer_assets.py
writes (docs/viewer-supply-chain.md):

- MapLibre GL JS: vendored in resources/vendor/maplibre-gl/ from the npm
  tarball (checked against the registry's sha512 when pinned); the build
  checks each file's SHA-256 before packing it and never fetches it.
- The SDF glyph ranges (fonts/<stack>/<start>-<end>.pbf): fetched from the
  openmaptiles font CDN, each checked against its SHA-256, and kept in a
  content-addressed cache so later and offline builds need no network.

A file that does not match its hash stops the build (IntegrityError); there
is no switch to ship it anyway. A range that cannot be downloaded at all is a
DownloadError, which STREETZIM_ALLOW_FONT_ERRORS=1 can waive
(streetzim/tiles.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

from streetzim.paths import LOCK_NAME, REPO_ROOT, RESOURCES_DIR

ROOT = REPO_ROOT
LOCK = RESOURCES_DIR / LOCK_NAME
VENDOR = RESOURCES_DIR / "vendor"
# Filled when the Docker image is built (tools/pin_viewer_assets.py
# --prefetch); read-only at run time, searched after the cache.
BAKED = ROOT / "viewer-assets"
PIN_HELP = "see docs/viewer-supply-chain.md (tools/pin_viewer_assets.py)"
USER_AGENT = "streetzim/1.0"


class IntegrityError(Exception):
    """A pinned file's content is not what the lock file says."""


class DownloadError(Exception):
    """A pinned file could not be fetched (network, HTTP error)."""


class FontRange(NamedTuple):
    stack: str          # our name, as in the style and the ZIM path
    range_key: str      # "0-255"
    url: str
    sha256: str | None  # None: the CDN had no file for it when pinned


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_lock(path: Path = LOCK) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        lock: dict[str, Any] = json.load(f)
    return lock


def maplibre_version(lock: dict[str, Any] | None = None) -> str:
    return str((lock or load_lock())["maplibre-gl"]["version"])


def vendored_maplibre(lock: dict[str, Any] | None = None,
                      vendor: Path = VENDOR) -> dict[str, Path]:
    """The vendored MapLibre files by name, each checked against the lock."""
    entry = (lock or load_lock())["maplibre-gl"]
    out: dict[str, Path] = {}
    for name, want in entry["files"].items():
        path = vendor / "maplibre-gl" / name
        if not path.is_file():
            raise IntegrityError(f"{path} is missing; {PIN_HELP}")
        got = sha256_hex(path.read_bytes())
        if got != want:
            raise IntegrityError(
                f"{path}: sha256 {got}, lock file says {want} "
                f"(maplibre-gl {entry['version']}); {PIN_HELP}")
        out[name] = path
    return out


def font_ranges(lock: dict[str, Any] | None = None) -> list[FontRange]:
    fonts = (lock or load_lock())["fonts"]
    base = fonts["base_url"].rstrip("/")
    out: list[FontRange] = []
    for stack, cdn_name in fonts["fontstacks"].items():
        encoded = urllib.parse.quote(cdn_name)
        for range_key, digest in fonts["ranges"][stack].items():
            out.append(FontRange(stack, range_key, f"{base}/{encoded}/{range_key}.pbf", digest))
    return out


def cache_dirs() -> list[Path]:
    """Where verified files are looked for, first one written to:
    $STREETZIM_CACHE_DIR/viewer-assets (the `streetzim` command sets it to
    <--dl>/cache), else <repo>/viewer-assets, then the image's baked copy."""
    first = Path(os.environ.get("STREETZIM_CACHE_DIR") or ROOT) / "viewer-assets"
    return [first] if first == BAKED else [first, BAKED]


def cache_path(root: Path, digest: str) -> Path:
    return root / "sha256" / digest[:2] / digest


def cached(digest: str) -> bytes | None:
    """A cached copy whose content still has this hash (a corrupt one is ignored)."""
    for root in cache_dirs():
        p = cache_path(root, digest)
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if sha256_hex(data) == digest:
            return data
    return None


def store(digest: str, data: bytes, root: Path | None = None) -> None:
    """Add verified bytes to the cache; a read-only cache is not an error."""
    p = cache_path(root or cache_dirs()[0], digest)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # A unique temporary name: identical ranges share a digest and are
        # stored by several download threads at once.
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{digest[:8]}.")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o644)  # mkstemp's 0600 would hide a baked cache from --user
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        pass


def fetch(url: str, *, attempts: int = 5, timeout: float = 30) -> bytes:
    """GET url, retrying transient errors; a 404 is not retried."""
    err = "no attempt"
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise DownloadError(f"{url}: HTTP 404") from None
            err = f"HTTP {e.code}"
        except Exception as e:  # timeouts, resets, DNS: all worth a retry
            err = str(e) or type(e).__name__
        if attempt + 1 < attempts:
            time.sleep(min(2 ** attempt, 10))
    raise DownloadError(f"{url}: {err} (after {attempts} attempts)")


def fetch_verified(url: str, digest: str, *, attempts: int = 5) -> bytes:
    """The file with this SHA-256: from the cache, else downloaded, checked
    and cached. Raises IntegrityError when the download has other content."""
    data = cached(digest)
    if data is not None:
        return data
    data = fetch(url, attempts=attempts)
    got = sha256_hex(data)
    if got != digest:
        raise IntegrityError(f"{url}: sha256 {got}, lock file says {digest}; {PIN_HELP}")
    store(digest, data)
    return data
