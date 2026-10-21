#!/usr/bin/env python3
"""Kiwix search gate: does a merged file's search find its pack articles?

    search_gate.py MERGED.zim BASE.zim --lang L [--n 8]

Kiwix's search box asks libzim for title suggestions (Kiwix desktop and the
apps; kiwix-serve /suggest) and full-text results (kiwix-serve /search).
This samples N article titles of the ~w/<L>/ pack that no base entry
carries (spread evenly over the pack's articles) and asks the merged file
both ways, as python-libzim does: a title counts as found when one of the
first 10 results is an entry under ~w/<L>/. PASS when every sampled title
is found by the suggestion search. L is the map language, or (an entry
whose language has no pack, e.g. English on an English base) the first
pack in the file.

Prints one JSON line: {"lang", "titles", "suggest_found", "fulltext_found",
"rows": [{"title", "suggest", "fulltext", "suggest_top", "fulltext_top"}],
"title_index", "fulltext_index", "pass"}. Exit 0 = PASS, 1 = FAIL, 2 = no
pack article to sample.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def pack_langs(a) -> list[str]:
    out: list[str] = []
    for i in range(a.entry_count):
        p = a._get_entry_by_id(i).path
        if p.startswith("~w/"):
            lg = p.split("/")[1]
            if lg not in out:
                out.append(lg)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("merged")
    ap.add_argument("base")
    ap.add_argument("--lang", required=True)
    ap.add_argument("--n", type=int, default=8)
    a = ap.parse_args(argv)
    from libzim.reader import Archive
    from libzim.search import Query, Searcher
    from libzim.suggestion import SuggestionSearcher
    m, b = Archive(Path(a.merged)), Archive(Path(a.base))
    langs = pack_langs(m)
    lang = a.lang if a.lang in langs else (langs[0] if langs else a.lang)
    pre = f"~w/{lang}/wiki-article/"
    titles = []
    for i in range(m.entry_count):
        e = m._get_entry_by_id(i)
        if e.path.startswith(pre) and not e.is_redirect and e.title:
            try:
                in_base = b.has_entry_by_title(e.title)
            except Exception:  # noqa: BLE001 - treat a lookup error as "maybe in the base"
                in_base = True
            if not in_base:
                titles.append(e.title)
    out: dict = {"lang": lang, "merged": Path(a.merged).name, "pack_only_titles": len(titles),
                 "title_index": bool(m.has_title_index), "fulltext_index": bool(m.has_fulltext_index)}
    if not titles:
        out.update({"titles": 0, "pass": False, "why": f"no pack-only article under {pre}"})
        print(json.dumps(out, ensure_ascii=False))
        return 2
    step = max(1, len(titles) // max(1, a.n))
    sample = titles[::step][:a.n]
    ss, se = SuggestionSearcher(m), Searcher(m)
    rows = []
    for t in sample:
        sug = list(ss.suggest(t).getResults(0, 10))
        try:
            ful = list(se.search(Query().set_query(t)).getResults(0, 10)) if m.has_fulltext_index else []
        except Exception:  # noqa: BLE001 - no usable full-text index
            ful = []
        rows.append({"title": t, "suggest": any(p.startswith(f"~w/{lang}/") for p in sug),
                     "fulltext": any(p.startswith(f"~w/{lang}/") for p in ful),
                     "suggest_top": sug[:3], "fulltext_top": ful[:3]})
    out.update({"titles": len(rows), "suggest_found": sum(r["suggest"] for r in rows),
                "fulltext_found": sum(r["fulltext"] for r in rows), "rows": rows})
    out["pass"] = out["suggest_found"] == len(rows)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
