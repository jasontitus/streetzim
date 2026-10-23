#!/usr/bin/env python3
"""Build the search packs a region's menu needs (index_pack.py).

    search_packs.py BASE.zim PACKS_DIR --region R --langs de,el,... --region-langs el
        [--cache DIR] [--date YYYY-MM-DD] [--jobs N] [--work-dir DIR]

Every menu entry L carries the packs recipe2.menu_packs gives it, and its
search pack is indexed in L itself (recipe2.search_lang: libzim's stemmer,
stopwords and the query side's language are L's), so entries share a search
pack only with the same packs and the same language. For each entry without
a search pack built for exactly these packs, this base and this language
(recipe2.find_search_pack), this writes
PACKS_DIR/osm_<R>_search-<langs>[.<L>]_<date>_<first 8 hex of the base's UUID>.zim
with its sidecar and gate (".<L>" unless the set's one language beyond the
region's is L: search-de-fr-it-ja-rm is ja's, search-de-fr-it-rm.de de's).
An entry with no pack needs none (the base's own indexes find everything in
it). Until 2026-10-23 entries with the same packs shared one pack indexed in
the set's own language, else the base's: Switzerland's de, fr, it, rm and en
versions all had English stemming; such a pack still serves the version
whose language it is indexed in. Prints one JSON line per pack, in the
menu's order; exit 1 if a search pack fails its gate.

--jobs N builds N packs at a time, each in a process of its own; the base's
pages are read once first (index_pack.py's index-text cache, with --cache).
--work-dir DIR (the NVMe scratch): each pack is built and gated there and
then moved into PACKS_DIR, its sidecars first, each through a temporary name
in PACKS_DIR, so a reader sees a pack there only with its gate beside it.
"""
from __future__ import annotations

import argparse
import datetime
import json
import multiprocessing
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack import index_pack  # noqa: E402
from tools.langpack.merge_zim import KEY_MAPCFG, ZimReader, blob_of, open_pack  # noqa: E402
from tools.langpack.recipe2 import available_packs, find_search_pack, menu_packs, search_lang  # noqa: E402


def _into(src: str, dst: str) -> None:
    """src moved to dst through a temporary name beside dst (dst's folder may be another disk)."""
    tmp = os.path.join(os.path.dirname(dst), f".{os.path.basename(dst)}.{os.getpid()}.part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    os.remove(src)


def build_one(base: str, files: list[str], lang: str, region: str, out: str, cache: str | None,
              work: str | None) -> dict:
    """index_pack.build, in DIR when given, then moved into out's folder (the
    pack last: its gate and sidecar are there before it)."""
    if not work:
        return index_pack.build(base, files, lang, region, out, Path(cache) if cache else None)
    os.makedirs(work, exist_ok=True)
    w = os.path.join(work, os.path.basename(out))
    side = index_pack.build(base, files, lang, region, w, Path(cache) if cache else None)
    for ext in (".json", ".gate.json", ""):
        _into(w + ext, out + ext)
    return side


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("base")
    ap.add_argument("packs_dir")
    ap.add_argument("--region", required=True)
    ap.add_argument("--langs", required=True)
    ap.add_argument("--region-langs", default="")
    ap.add_argument("--cache", default=None)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--work-dir", default=None)
    a = ap.parse_args(argv)
    rl = [x for x in a.region_langs.split(",") if x]
    z = ZimReader(a.base)
    cfg = json.loads(blob_of(z, KEY_MAPCFG) or b"{}")
    base_lang = cfg.get("language") or "en"
    base_wiki = base_lang if cfg.get("hasWikiArticles") else None
    avail = available_packs(a.packs_dir, a.base, a.region)
    sets: dict[tuple[tuple[str, ...], str], list[str]] = {}       # (pack files, index language) -> entries
    for L in [x for x in a.langs.split(",") if x]:
        packs = menu_packs(rl, base_wiki, L, avail)
        if packs:
            sets.setdefault((tuple(sorted(avail[x] for x in packs)), search_lang(L)), []).append(L)
    u8 = z.header.uuid.hex()[:8]
    rows: list[dict] = []
    todo: list[tuple] = []                          # (row, build_one's arguments)
    for (files, lang), entries in sets.items():
        have = find_search_pack(a.packs_dir, a.base, list(files), lang)
        langs = sorted(open_pack(f).lang for f in files)
        row: dict = {"packs": langs, "entries": entries}
        rows.append(row)
        if have:
            row["search_pack"] = os.path.basename(have)
            row["built"] = False
        else:
            own = [x for x in langs if x not in rl]
            tag = "" if own == [lang] else f".{lang}"
            out = os.path.join(a.packs_dir, f"osm_{a.region}_search-{'-'.join(langs)}{tag}_{a.date}_{u8}.zim")
            row.update(search_pack=os.path.basename(out), built=True, index_lang=lang)
            todo.append((row, (a.base, list(files), lang, a.region, out, a.cache, a.work_dir)))
    args = {id(row): x for row, x in todo}
    futs = {}
    ex = None
    side_by_side = len(todo) > 1 and a.jobs > 1
    print(f"search packs: {len(todo)} to build, {min(a.jobs, len(todo)) if side_by_side else 1} at a time"
          + (f", in {a.work_dir}" if a.work_dir else ""), file=sys.stderr, flush=True)
    if side_by_side:
        if a.cache:                                 # the base's pages read once, for every build
            for _ in index_pack.base_entries(a.base, Path(a.cache), lambda m: print(m, file=sys.stderr, flush=True)):
                pass
        ex = ProcessPoolExecutor(max_workers=min(a.jobs, len(todo)), mp_context=multiprocessing.get_context("spawn"))
        futs = {k: ex.submit(build_one, *x) for k, x in args.items()}
    bad = 0
    try:
        for row in rows:                            # in the menu's order, each as soon as it is built
            if id(row) in args:
                side = futs[id(row)].result() if ex else build_one(*args[id(row)])
                row.update(bytes=side["bytes"], seconds=side["seconds"], gate_ok=side["gate"]["ok"],
                           problems=side["gate"]["problems"])
                bad += not side["gate"]["ok"]
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        if ex:
            ex.shutdown(cancel_futures=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
