#!/usr/bin/env python3
"""Build the search packs a region's menu needs (index_pack.py).

    search_packs.py BASE.zim PACKS_DIR --region R --langs de,el,... --region-langs el
        [--cache DIR] [--date YYYY-MM-DD]

Every menu entry L carries the packs recipe2.menu_packs gives it; entries
with the same packs (on Greece: el and en both carry el alone) share one
search pack. For each such pack set without a search pack built for exactly
these packs and this base (recipe2.find_search_pack), this writes
PACKS_DIR/osm_<R>_search-<langs>_<date>_<first 8 hex of the base's UUID>.zim
with its sidecar and gate. An entry with no pack needs none (the base's own
indexes find everything in it).

The index language (libzim's stemmer) is the set's language that is not one
of the region's (the visitor's), else the base's. Prints one JSON line per
pack set; exit 1 if a search pack fails its gate.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack import index_pack  # noqa: E402
from tools.langpack.merge_zim import KEY_MAPCFG, ZimReader, blob_of, open_pack  # noqa: E402
from tools.langpack.recipe2 import available_packs, find_search_pack, menu_packs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("base")
    ap.add_argument("packs_dir")
    ap.add_argument("--region", required=True)
    ap.add_argument("--langs", required=True)
    ap.add_argument("--region-langs", default="")
    ap.add_argument("--cache", default=None)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    a = ap.parse_args(argv)
    rl = [x for x in a.region_langs.split(",") if x]
    z = ZimReader(a.base)
    cfg = json.loads(blob_of(z, KEY_MAPCFG) or b"{}")
    base_lang = cfg.get("language") or "en"
    base_wiki = base_lang if cfg.get("hasWikiArticles") else None
    avail = available_packs(a.packs_dir, a.base, a.region)
    sets: dict[tuple[str, ...], list[str]] = {}
    for L in [x for x in a.langs.split(",") if x]:
        packs = menu_packs(rl, base_wiki, L, avail)
        if packs:
            sets.setdefault(tuple(sorted(avail[x] for x in packs)), []).append(L)
    u8 = z.header.uuid.hex()[:8]
    bad = 0
    for files, entries in sets.items():
        have = find_search_pack(a.packs_dir, a.base, list(files))
        langs = sorted(open_pack(f).lang for f in files)
        row: dict = {"packs": langs, "entries": entries}
        if have:
            row["search_pack"] = os.path.basename(have)
            row["built"] = False
        else:
            own = [x for x in langs if x not in rl]
            lang = own[0] if len(own) == 1 else base_lang
            out = os.path.join(a.packs_dir, f"osm_{a.region}_search-{'-'.join(langs)}_{a.date}_{u8}.zim")
            side = index_pack.build(a.base, list(files), lang, a.region, out, Path(a.cache) if a.cache else None)
            row.update(search_pack=os.path.basename(out), built=True, index_lang=lang, bytes=side["bytes"],
                       seconds=side["seconds"], gate_ok=side["gate"]["ok"], problems=side["gate"]["problems"])
            bad += not side["gate"]["ok"]
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
