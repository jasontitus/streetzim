#!/usr/bin/env python3
"""The builder page's region list from its published regions.

    site_index.py RECIPES_DIR

RECIPES_DIR is web/build/recipes/: one folder per region, each holding
make_site.py's region.json (and its recipes). Writes RECIPES_DIR/index.json,
{"format": "streetzim-build-index/1", "generated", "regions": [...]}, the
regions sorted by title; a folder without a readable region.json, or whose
region id is not the folder's name, is skipped and named on stderr. Exit 1
when no region is left (the page would list nothing).

Each region gets "photo_search": whether its base map has the sign reader
(search with a photo: map-config.json's "ocr"), read from the base file in
$STREETZIM_MAPS (default: the repository's root, where the build host keeps
the maps); a base not there keeps what the last index said.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile


def photo_search(maps: str, base_file: str) -> bool | None:
    """Whether the map file `base_file` in `maps` has the sign reader; None
    when it is not there or cannot be read."""
    path = os.path.join(maps, os.path.basename(base_file or ""))
    if not base_file or not os.path.isfile(path):
        return None
    try:
        from libzim.reader import Archive
        from pathlib import Path
        cfg = json.loads(bytes(Archive(Path(path)).get_entry_by_path("map-config.json").get_item().content))
    except Exception:                                   # noqa: BLE001  (no libzim, a damaged file)
        return None
    return bool(cfg.get("ocr"))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    d = argv[0]
    maps = os.environ.get("STREETZIM_MAPS") or os.path.abspath(os.path.join(d, "..", "..", ".."))
    try:
        before = {r["id"]: r.get("photo_search") for r in
                  json.load(open(os.path.join(d, "index.json"), encoding="utf-8"))["regions"]}
    except (OSError, ValueError, KeyError, TypeError):
        before = {}
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
        ps = photo_search(maps, (r.get("base") or {}).get("file", ""))
        if ps is None:
            ps = before.get(name)
        if ps is not None:
            r["photo_search"] = bool(ps)
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
