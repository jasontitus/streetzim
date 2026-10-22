#!/usr/bin/env python3
"""Is this map file the base of language recipes published on /build/?

    published_base.py ZIM [--web WEB_DIR] [--md5 HEX]

Reads WEB_DIR/build/recipes/*/region.json (default: this checkout's web/).
Without --md5: exit 3 when any published region's base is a file of this
name (an in-place viewer patch of it would break those recipes and their
language torrents), else 0. With --md5 (the file's MD5 as it is now): exit
3 only when a published region names this file with ANOTHER md5 (its bytes
changed since the recipes were made), else 0. Prints the region ids, one
per line, on exit 3. Exit 2 on a usage error. Used by
ops/cloud/upload_validated.sh and ops/cloud/rollout_viewer_patch.sh to
enforce the freeze on in-place patches of bases with published recipes
(user decision 2026-10-21; plan review B2/H-2) unless ship_packs.sh runs
right after (LANGPACK_RESHIP_AFTER_PATCH=1).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def published(zim: str, web: str, md5: str | None = None) -> list[str]:
    name = os.path.basename(zim)
    rdir = os.path.join(web, "build", "recipes")
    out = []
    if not os.path.isdir(rdir):
        return out
    for region in sorted(os.listdir(rdir)):
        p = os.path.join(rdir, region, "region.json")
        try:
            base = json.load(open(p, encoding="utf-8"))["base"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if base.get("file") != name:
            continue
        if md5 is not None and base.get("md5") == md5:
            continue
        out.append(region)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zim")
    ap.add_argument("--web", default=os.path.join(ROOT, "web"))
    ap.add_argument("--md5")
    a = ap.parse_args(argv)
    if a.md5 is not None and not (len(a.md5) == 32 and all(c in "0123456789abcdef" for c in a.md5)):
        print(f"published_base: --md5 {a.md5!r} is not an MD5", file=sys.stderr)
        return 2
    regions = published(a.zim, a.web, a.md5)
    for r in regions:
        print(r)
    return 3 if regions else 0


if __name__ == "__main__":
    sys.exit(main())
