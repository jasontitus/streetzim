#!/usr/bin/env python3
"""Pin, check and prefetch the viewer's third-party files
(resources/viewer-assets.lock.json; docs/viewer-supply-chain.md).

    python tools/pin_viewer_assets.py --check              # offline; CI runs it
    python tools/pin_viewer_assets.py --maplibre 5.24.0    # vendor another MapLibre
    python tools/pin_viewer_assets.py --rtl-text 0.3.0     # vendor the RTL text plugin
    python tools/pin_viewer_assets.py --fonts              # re-pin the glyph ranges
    python tools/pin_viewer_assets.py --prefetch DIR       # fill a cache (Docker image)

--maplibre downloads the npm tarball, checks it against the sha512 the npm
registry publishes for that version, and writes dist/maplibre-gl.js,
dist/maplibre-gl.css and LICENSE.txt to resources/vendor/maplibre-gl/ with
their SHA-256s in the lock file. --rtl-text does the same for
@mapbox/mapbox-gl-rtl-text (dist/mapbox-gl-rtl-text.js and LICENSE.md, to
resources/vendor/mapbox-gl-rtl-text/). --fonts downloads every range of every
fontstack from the font CDN and records its SHA-256 (null for a range the
CDN answers 404), printing how many changed, and does the same for the
fallback Noto Sans ranges (``fonts.fallback``: the ranges holding the
Unicode blocks of its scripts, from a commit-pinned URL) and the hash of its
vendored licence. A download that is not a glyph range (an HTML error page)
stops it. Review the diff and commit both. --prefetch downloads every pinned range, checks it, and stores it in
DIR as the builder's cache (streetzim/viewer_assets.py) expects, so builds
that use DIR need no network for fonts.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import tarfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim import viewer_assets as va  # noqa: E402
from streetzim.glyph_fallback import is_glyph_pbf  # noqa: E402

NPM_REGISTRY = "https://registry.npmjs.org"
# lock-file key -> (npm package, licence, {tarball member: vendored file name})
NPM_PACKAGES: dict[str, tuple[str, str, dict[str, str]]] = {
    "maplibre-gl": ("maplibre-gl", "BSD-3-Clause (LICENSE.txt)", {
        "package/dist/maplibre-gl.js": "maplibre-gl.js",
        "package/dist/maplibre-gl.css": "maplibre-gl.css",
        "package/LICENSE.txt": "LICENSE.txt",
    }),
    va.RTL_TEXT_PLUGIN: ("@mapbox/mapbox-gl-rtl-text",
                         "BSD-2-Clause, ICU under the Unicode licence (LICENSE.md)", {
        "package/dist/mapbox-gl-rtl-text.js": "mapbox-gl-rtl-text.js",
        "package/LICENSE.md": "LICENSE.md",
    }),
}
DEFAULT_FONTS: dict[str, Any] = {
    "base_url": "https://fonts.openmaptiles.org",
    "licence": "Open Sans, as served by openmaptiles/fonts; see "
               "https://github.com/openmaptiles/fonts",
    # our name (style + ZIM path, no spaces) -> CDN fontstack name
    "fontstacks": {
        "OpenSansRegular": "Open Sans Regular",
        "OpenSansBold": "Open Sans Bold",
        "OpenSansItalic": "Open Sans Italic",
    },
}
RANGES = [f"{s}-{s + 255}" for s in range(0, 65536, 256)]
# Noto Sans glyphs for the scripts Open Sans has none for, merged into the
# Open Sans ranges at build time (streetzim/glyph_fallback.py). The URL names
# a git commit, so its files cannot change under the pins.
FALLBACK_COMMIT = "028c18f713baecad011301ff7a69acc39bcc2ae7"
DEFAULT_FALLBACK: dict[str, Any] = {
    "base_url": ("https://raw.githubusercontent.com/protomaps/basemaps-assets/"
                 f"{FALLBACK_COMMIT}/fonts"),
    "licence": "Noto Sans, SIL Open Font License 1.1 (Copyright 2022 The Noto Project "
               "Authors); glyphs made by maplibre/font-maker, as published by "
               "https://github.com/protomaps/basemaps-assets",
    "licence_file": "resources/vendor/noto-sans/OFL.txt",
    # our name -> the source's fontstack name
    "fontstacks": {
        "NotoSansRegular": "Noto Sans Regular",
        "NotoSansMedium": "Noto Sans Medium",
    },
    # which fallback each Open Sans stack gets (Noto Sans Italic has none of
    # these scripts; Medium is the source's boldest weight)
    "for": {
        "OpenSansRegular": "NotoSansRegular",
        "OpenSansBold": "NotoSansMedium",
        "OpenSansItalic": "NotoSansRegular",
    },
    # script -> Unicode blocks merged when the map's labels use it. Scripts
    # that need shaping MapLibre does not do (Devanagari, Bengali, ...) are
    # left out: their glyphs would render in the wrong order and shape.
    "scripts": {
        "Arabic": ["0600-06FF", "FB50-FBFF", "FE70-FEFF"],
        "Armenian": ["0530-058F"],
        "Georgian": ["10A0-10FF"],
        "Hebrew": ["0590-05FF", "FB1D-FB4F"],
        "Lao": ["0E80-0EFF"],
        "Thai": ["0E00-0E7F"],
    },
}


def fallback_ranges(fb: dict[str, Any]) -> list[str]:
    from streetzim.glyph_fallback import parse_blocks, ranges_for
    return ranges_for([b for blocks in fb["scripts"].values() for b in parse_blocks(blocks)])


def write_lock(lock: dict[str, Any], path: Path = va.LOCK) -> None:
    lock["_comment"] = ("Written by tools/pin_viewer_assets.py; see "
                        "docs/viewer-supply-chain.md. Do not edit by hand.")
    ordered = {"_comment": lock["_comment"]}
    ordered.update((k, v) for k, v in lock.items() if k != "_comment")
    path.write_text(json.dumps(ordered, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def read_lock_or_empty() -> dict[str, Any]:
    return va.load_lock() if va.LOCK.exists() else {}


def check_npm_integrity(data: bytes, integrity: str) -> None:
    algo, _, b64 = integrity.partition("-")
    if algo != "sha512":
        raise SystemExit(f"unexpected npm integrity algorithm: {integrity}")
    got = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if got != b64:
        raise SystemExit(f"npm tarball sha512 {got} != registry's {b64}")


def pin_npm(name: str, version: str) -> None:
    """Vendor package ``name`` (a key of NPM_PACKAGES) at ``version`` from
    its npm tarball, checked against the registry's sha512."""
    package, licence, members = NPM_PACKAGES[name]
    meta = json.loads(va.fetch(f"{NPM_REGISTRY}/{package}/{version}"))
    tarball_url: str = meta["dist"]["tarball"]
    integrity: str = meta["dist"]["integrity"]
    tgz = va.fetch(tarball_url)
    check_npm_integrity(tgz, integrity)
    out_dir = va.VENDOR / name
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
        for member, fname in members.items():
            f = tar.extractfile(member)
            if f is None:
                raise SystemExit(f"{member} not in {tarball_url}")
            data = f.read()
            (out_dir / fname).write_bytes(data)
            files[fname] = va.sha256_hex(data)
            print(f"  {fname}: {len(data):,} bytes, sha256 {files[fname]}")
    lock = read_lock_or_empty()
    lock[name] = {
        "version": version,
        "licence": licence,
        "tarball": tarball_url,
        "integrity": integrity,
        "files": files,
    }
    write_lock(lock)
    print(f"vendored {package} {version} in {out_dir.relative_to(ROOT)}")


def pin_maplibre(version: str) -> None:
    pin_npm("maplibre-gl", version)
    print("run scripts/sync-drive-viewer.sh for the website copy")


def _pin_ranges(base_url: str, fontstacks: dict[str, str], ranges: list[str],
                old: dict[str, Any]) -> tuple[dict[str, dict[str, str | None]], int, int]:
    """Download each range of each stack: ({stack: {range: sha256 or None}},
    how many differ from ``old``, how many are absent)."""
    tasks = [(stack, src, r) for stack, src in fontstacks.items() for r in ranges]

    def one(task: tuple[str, str, str]) -> tuple[str, str, str | None]:
        stack, src, r = task
        url = f"{base_url.rstrip('/')}/{urllib.parse.quote(src)}/{r}.pbf"
        try:
            data = va.fetch(url)
        except va.DownloadError as e:
            if str(e).endswith("HTTP 404"):
                return stack, r, None
            raise SystemExit(f"not pinning fonts: {e}") from None
        if not is_glyph_pbf(data):
            # e.g. the font CDN answers an unknown fontstack with an HTML page
            raise SystemExit(f"not pinning fonts: {url} is not a glyph range "
                             f"({len(data)} bytes starting {data[:16]!r})")
        return stack, r, va.sha256_hex(data)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(one, tasks))
    out: dict[str, dict[str, str | None]] = {}
    changed = 0
    for stack, r, digest in results:
        out.setdefault(stack, {})[r] = digest
        if old.get(stack, {}).get(r, "?") != digest:
            changed += 1
    return out, changed, sum(1 for *_, d in results if d is None)


def pin_fonts() -> None:
    lock = read_lock_or_empty()
    old = lock.get("fonts", {})
    fonts: dict[str, Any] = {k: old.get(k, v) for k, v in DEFAULT_FONTS.items()}
    fonts["ranges"], changed, empty = _pin_ranges(
        fonts["base_url"], fonts["fontstacks"], RANGES, old.get("ranges", {}))
    n = len(fonts["fontstacks"]) * len(RANGES)
    print(f"pinned {n} glyph ranges ({empty} absent on the CDN); "
          f"{changed} differ from the previous lock file")

    old_fb = old.get("fallback", {})
    fb: dict[str, Any] = {k: old_fb.get(k, v) for k, v in DEFAULT_FALLBACK.items()}
    fb_ranges = fallback_ranges(fb)
    fb["ranges"], changed, empty = _pin_ranges(
        fb["base_url"], fb["fontstacks"], fb_ranges, old_fb.get("ranges", {}))
    fb["licence_sha256"] = va.sha256_hex((ROOT / fb["licence_file"]).read_bytes())
    fonts["fallback"] = fb
    lock["fonts"] = fonts
    write_lock(lock)
    print(f"pinned {len(fb['fontstacks']) * len(fb_ranges)} fallback glyph ranges "
          f"({empty} absent); {changed} differ from the previous lock file")


def all_ranges(lock: dict[str, Any] | None = None) -> list[va.FontRange]:
    """The pinned glyph ranges and the fallback ranges."""
    fb = va.font_fallback(lock)
    return va.font_ranges(lock) + (fb.ranges if fb else [])


def prefetch(dest: Path) -> None:
    ranges = [fr for fr in all_ranges() if fr.sha256]

    def one(fr: va.FontRange) -> None:
        assert fr.sha256
        path = va.cache_path(dest, fr.sha256)
        if path.is_file() and va.sha256_hex(path.read_bytes()) == fr.sha256:
            return
        data = va.fetch(fr.url)
        got = va.sha256_hex(data)
        if got != fr.sha256:
            raise va.IntegrityError(f"{fr.url}: sha256 {got}, lock file says {fr.sha256}")
        va.store(fr.sha256, data, root=dest)
        if not path.is_file():
            raise SystemExit(f"could not write {path}")

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(one, ranges))
    except (va.IntegrityError, va.DownloadError) as e:
        raise SystemExit(f"prefetch failed: {e}") from None
    print(f"ok: {len(ranges)} verified glyph ranges in {dest}")


def check() -> None:
    lock = va.load_lock()
    for name in NPM_PACKAGES:
        va.vendored(name, lock)
    ranges = va.font_ranges(lock)
    stacks = lock["fonts"]["fontstacks"]
    for stack in stacks:
        if list(lock["fonts"]["ranges"].get(stack, {})) != RANGES:
            raise SystemExit(f"lock file: fontstack {stack} does not list all {len(RANGES)} ranges")
    fb = lock["fonts"].get("fallback")
    if fb:
        want = fallback_ranges(fb)
        for stack in fb["fontstacks"]:
            if list(fb["ranges"].get(stack, {})) != want:
                raise SystemExit(f"lock file: fallback fontstack {stack} does not list "
                                 f"the ranges of its scripts ({want})")
        for stack, fb_stack in fb["for"].items():
            if stack not in stacks or fb_stack not in fb["fontstacks"]:
                raise SystemExit(f"lock file: fallback {stack} -> {fb_stack} names an unknown fontstack")
        va.fallback_licence(lock)
    fb_count = len(all_ranges(lock)) - len(ranges)
    ranges = all_ranges(lock)
    bad = [fr for fr in ranges if fr.sha256 is not None and
           (len(fr.sha256) != 64 or any(c not in "0123456789abcdef" for c in fr.sha256))]
    if bad:
        raise SystemExit(f"lock file: malformed sha256 for {bad[0].stack}/{bad[0].range_key}")
    print(f"ok: maplibre-gl {lock['maplibre-gl']['version']} and "
          f"{va.RTL_TEXT_PLUGIN} {lock[va.RTL_TEXT_PLUGIN]['version']} match the lock file; "
          f"{len(ranges) - fb_count} glyph ranges and {fb_count} fallback ranges pinned")


def main() -> int:
    ap = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="verify vendored files (offline)")
    g.add_argument("--maplibre", metavar="VERSION", help="vendor this MapLibre GL JS version")
    g.add_argument("--rtl-text", metavar="VERSION",
                   help="vendor this @mapbox/mapbox-gl-rtl-text version")
    g.add_argument("--fonts", action="store_true", help="re-pin every glyph range from the CDN")
    g.add_argument("--prefetch", metavar="DIR", type=Path,
                   help="download the pinned glyph ranges into this cache directory")
    args = ap.parse_args()
    try:
        if args.check:
            check()
        elif args.maplibre:
            pin_maplibre(args.maplibre)
        elif args.rtl_text:
            pin_npm(va.RTL_TEXT_PLUGIN, args.rtl_text)
        elif args.fonts:
            pin_fonts()
        else:
            prefetch(args.prefetch)
    except va.IntegrityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
