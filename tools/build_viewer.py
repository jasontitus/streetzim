#!/usr/bin/env python3
"""Build resources/viewer/index.html from its parts in resources/viewer/src/index/.

The viewer ships as ONE index.html: published ZIMs have it in a fixed-size
slot that cloud/patch_viewer_inplace.py overwrites, and a ZIM can never gain
new files (docs/viewer-slots.md). So the parts are joined back into exactly
that file, in file-name order. Nothing is added or changed in between.

    python tools/build_viewer.py            # rebuild index.html from the parts
    python tools/build_viewer.py --check    # fail if index.html != parts (CI)

Edit the parts, not index.html, then rebuild, run
scripts/sync-drive-viewer.sh, and commit all three. Parts are fragments: a
part may open a function that a later part closes (initRouting spans
500-610), so they are not meant to be valid JavaScript on their own.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARTS = ROOT / "resources" / "viewer" / "src" / "index"
TARGET = ROOT / "resources" / "viewer" / "index.html"


PART_NAME = re.compile(r"^\d{3}-[\w-]+\.(html|js)$")


def parts() -> list[Path]:
    """The parts, in order. Anything else in the directory (a README, an
    editor backup, a .orig/.rej) is an error rather than being spliced into
    the shipped viewer; hidden files are ignored."""
    found = sorted(p for p in PARTS.iterdir() if not p.name.startswith("."))
    stray = [p.name for p in found if not PART_NAME.match(p.name)]
    if stray:
        raise SystemExit(f"not a viewer part (want NNN-name.html|js): {stray} in {PARTS}")
    if not found:
        raise SystemExit(f"no parts in {PARTS}")
    return found


def build() -> bytes:
    return b"".join(p.read_bytes() for p in parts())


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if index.html differs from the joined parts")
    args = ap.parse_args()
    data = build()
    if args.check:
        if TARGET.read_bytes() != data:
            print(f"{TARGET.relative_to(ROOT)} does not match {PARTS.relative_to(ROOT)}/*.\n"
                  "Edit the parts, then run: python tools/build_viewer.py", file=sys.stderr)
            return 1
        print(f"ok: {TARGET.relative_to(ROOT)} matches its {len(parts())} parts")
        return 0
    TARGET.write_bytes(data)
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(data):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
