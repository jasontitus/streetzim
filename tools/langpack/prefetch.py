#!/usr/bin/env python3
"""Fetch everything build_packs.sh's offline builds read from the network.

    prefetch.py BASE.zim --langs ar,de,el,... [--cache DIR] [--text-rounds N]

build_packs.sh runs the pack builder with --offline under a one-hour
timeout per language, so the network work happens here first, with no
timeout of its own (the caller sets one), into the builder's --cache:

- each language's page_props, page and redirect SQL dumps
  (dumps.wikimedia.org), and the Q-ID -> title map made from them;
- the base's Q-IDs (its category files and geo-index; cached by UUID);
- wikidata-text: labels and descriptions of those Q-IDs in every
  language asked plus the matrix's (wiki_pack.TEXT_LANGS), one
  wbgetentities request for all of them. A throttled run keeps what it
  got (JSON lines) and the next round asks only for the rest; up to
  --text-rounds rounds, a minute apart.

Prints one JSON line per language, then a summary line, which lists the
languages whose title map failed ("failed") and those whose wikidata-text
is incomplete ("text_incomplete"). Exit 0 when every language has its
title map and complete wikidata-text, 1 otherwise (the packs can still be
built: an incomplete wikidata-text is said in the pack's stats), 2 on bad
arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from streetzim import wiki_pack  # noqa: E402
from streetzim.languages import pack_iso639_3  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("base")
    ap.add_argument("--langs", required=True, help="comma-separated Wikipedia languages")
    ap.add_argument("--cache", default=None, help="the builder's --cache "
                    "(default: <cache root>/wiki_pack_cache, as the builder)")
    ap.add_argument("--text-rounds", type=int, default=3)
    a = ap.parse_args(argv)
    langs = [x.strip().lower() for x in a.langs.split(",") if x.strip()]
    try:
        for lg in langs:
            pack_iso639_3(lg)
    except ValueError as e:
        print(f"prefetch: {e}", file=sys.stderr)
        return 2
    if a.cache:
        cache = Path(a.cache)
    else:
        from streetzim.paths import cache_root
        cache = cache_root() / "wiki_pack_cache"
    log = wiki_pack.log_stderr
    base = wiki_pack.read_base(a.base, cache, log)
    qids = base.qids
    # every pack language's labels in one pass: the default list lacks a
    # region language such as rm, whose turn then asked Wikidata again for
    # every Q-ID (Switzerland: 1,360 more requests, ~22 min)
    text_langs = list(dict.fromkeys([*wiki_pack.TEXT_LANGS, *langs]))
    ok = True
    failed: list[str] = []
    incomplete: list[str] = []
    for lg in langs:
        t0 = time.time()
        row: dict[str, object] = {"lang": lg}
        try:
            wiki_pack.build_title_map(lg, cache, log=log)
            wiki_pack.fetch_dump(lg, "redirect", cache, log=log)
            row["titles"] = len(wiki_pack.load_titles(lg, qids, cache, log=log))
        except Exception as e:  # noqa: BLE001 - reported per language, others go on
            row["titles_error"] = f"{type(e).__name__}: {e}"[:300]
            failed.append(lg)
            ok = False
        complete = False
        for r in range(max(1, a.text_rounds)):
            _, st = wiki_pack.wikidata_text(qids, lg, cache, fetch=wiki_pack.api_fetcher(log), langs=text_langs, log=log)
            complete = bool(st["complete"]) and not st["missing"]
            row["text"] = {k: st.get(k) for k in ("qids", "asked", "requests", "missing", "complete")}
            if complete:
                break
            if r + 1 < a.text_rounds:
                time.sleep(60)
        ok = ok and complete
        if not complete:
            incomplete.append(lg)
        row["seconds"] = round(time.time() - t0, 1)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    print(json.dumps({"base": os.path.basename(a.base), "base_uuid": base.uuid, "qids": len(qids),
                      "langs": langs, "ok": ok, "failed": failed, "text_incomplete": incomplete}),
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
