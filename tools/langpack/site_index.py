#!/usr/bin/env python3
"""The builder page's region list from its published regions.

    site_index.py RECIPES_DIR

RECIPES_DIR is web/build/recipes/: one folder per region, each holding
make_site.py's region.json (and its recipes). Writes RECIPES_DIR/index.json,
{"format": "streetzim-build-index/1", "generated", "regions": [...]}, the
regions sorted by title; a folder without a readable region.json, or whose
region id is not the folder's name, is skipped and named on stderr. Exit 1
when no region is left (the page would list nothing).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    d = argv[0]
    regions = []
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name, "region.json")
        if not os.path.isdir(os.path.join(d, name)):
            continue
        try:
            r = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"site_index: {name}: no readable region.json ({e})", file=sys.stderr)
            continue
        if not isinstance(r, dict) or r.get("id") != name or not r.get("menu"):
            print(f"site_index: {name}: region.json is not region {name!r} with a menu; skipped", file=sys.stderr)
            continue
        regions.append(r)
    if not regions:
        print("site_index: no region to list", file=sys.stderr)
        return 1
    regions.sort(key=lambda r: (str(r.get("title", "")).casefold(), r["id"]))
    out = {"format": "streetzim-build-index/1", "generated": datetime.date.today().isoformat(), "regions": regions}
    fd, tmp = tempfile.mkstemp(dir=d, prefix="index.json.", suffix=".tmp")   # a name of its own
    os.fchmod(fd, 0o644)                  # a site file: mkstemp's 0600 otherwise
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(d, "index.json"))
    print(json.dumps({"regions": [r["id"] for r in regions]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
