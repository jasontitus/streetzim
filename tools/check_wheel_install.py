"""Check that an installed streetzim (a wheel, not the checkout) finds its files.

    python -m venv /tmp/v && /tmp/v/bin/pip install dist/streetzim-*.whl
    cd /tmp && /tmp/v/bin/python /path/to/tools/check_wheel_install.py

Run it with the installed interpreter from outside the checkout. It needs no
network: it imports the package, checks that every file the build reads at
run time (streetzim/paths.py RUNTIME_FILES) resolves inside the installed
package and that cloud/ holds exactly CLOUD_MODULES (no operations code),
verifies the vendored MapLibre and RTL plugin against their pinned hashes,
and reads the tilemaker config, the lock file's font list and
cloud/regions.tsv the way a build does. CI's `wheel` job runs it
(docs/packaging.md).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def check() -> list[str]:
    try:
        import create_osm_zim
        from streetzim import paths, tiles, viewer_assets, zim_writer
    except ImportError as e:
        return [f"streetzim is not installed in this interpreter ({e})"]

    problems: list[str] = []
    here = Path(__file__).resolve().parent.parent
    # The checkout's own copies, not "anything under the checkout": the venv
    # may live inside it.
    for mod, rel in ((paths, "streetzim/paths.py"), (create_osm_zim, "create_osm_zim.py")):
        where = Path(str(mod.__file__)).resolve()
        if where == here / rel:
            problems.append(f"{mod.__name__} imported from the checkout ({where}), "
                            "not the installed package")
    if not paths.installed():
        problems.append(f"resources resolve to {paths.RESOURCES_DIR}, "
                        f"not the installed {paths.PACKAGED_RESOURCES}")
    problems += [f"missing: {paths.RESOURCES_DIR / name}"
                 for name in paths.missing_runtime_files()]
    cloud_dir = Path(str(create_osm_zim.__file__)).parent / "cloud"
    shipped = {p.stem for p in cloud_dir.glob("*.py")}
    if shipped != set(paths.CLOUD_MODULES):
        problems.append(f"cloud/ has {sorted(shipped)}, expected exactly "
                        f"{sorted(paths.CLOUD_MODULES)} (streetzim/paths.py)")
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
    regions = cloud_dir / "regions.tsv"
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
              f"under {paths.RESOURCES_DIR}; cloud/ = {len(paths.CLOUD_MODULES)} modules)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
