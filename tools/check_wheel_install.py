"""Check that an installed streetzim (a wheel, not the checkout) finds its files.

    python -m venv /tmp/v && /tmp/v/bin/pip install dist/streetzim-*.whl
    cd /tmp && /tmp/v/bin/python /path/to/tools/check_wheel_install.py

Run it with the installed interpreter from outside the checkout. It needs no
network: it imports the package, checks that every file the build reads at
run time (streetzim/paths.py RUNTIME_FILES) resolves inside the installed
package, verifies the vendored MapLibre and RTL plugin against their pinned
hashes, and reads the tilemaker config, the lock file's font list and
cloud/regions.tsv the way a build does. CI's `wheel` job runs it
(docs/packaging.md).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def check() -> list[str]:
    import create_osm_zim
    from streetzim import paths, tiles, viewer_assets
    from streetzim import zim_writer

    problems: list[str] = []
    here = Path(__file__).resolve().parent.parent
    for mod in (paths, create_osm_zim):
        where = Path(str(mod.__file__)).resolve()
        if where.is_relative_to(here):
            problems.append(f"{mod.__name__} imported from the checkout ({where}), "
                            "not the installed package")
    if paths.RESOURCES_DIR != paths.PACKAGED_RESOURCES:
        problems.append(f"resources resolve to {paths.RESOURCES_DIR}, "
                        f"not the installed {paths.PACKAGED_RESOURCES}")
    problems += [f"missing: {paths.RESOURCES_DIR / name}"
                 for name in paths.missing_runtime_files()]
    if problems:
        return problems

    viewer_assets.vendored_maplibre()          # raises IntegrityError on a mismatch
    if not viewer_assets.font_ranges():
        problems.append("the lock file lists no font ranges")
    if not tiles.required_shapefiles():
        problems.append("the tilemaker config names no shapefiles")
    rtl = zim_writer.RTL_TEXT_PLUGIN_PATH
    if hashlib.sha256(rtl.read_bytes()).hexdigest() != zim_writer.RTL_TEXT_PLUGIN_SHA256:
        problems.append(f"{rtl}: hash does not match the pinned one")
    regions = Path(str(create_osm_zim.__file__)).parent / "cloud" / "regions.tsv"
    if not regions.is_file():
        problems.append(f"missing: {regions}")
    return problems


def main() -> int:
    problems = check()
    for p in problems:
        print(f"check_wheel_install: {p}", file=sys.stderr)
    if not problems:
        from streetzim import paths
        print(f"check_wheel_install: ok ({len(paths.RUNTIME_FILES)} files "
              f"under {paths.RESOURCES_DIR})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
