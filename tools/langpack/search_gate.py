#!/usr/bin/env python3
"""Kiwix search gate: does a merged file's search find its pack articles,
and everything the base's own search finds?

    search_gate.py MERGED.zim BASE.zim --lang L [--n 8] [--base-n 12]

Kiwix's search box asks libzim for title suggestions (Kiwix desktop and the
apps; kiwix-serve /suggest) and full-text results (kiwix-serve /search).

Packs: N article titles of the ~w/<L>/ pack that no base entry carries
(spread evenly over the pack's articles), asked both ways as python-libzim
does: found when one of the first 10 suggestions, and one of the first 25
full-text results (kiwix-serve's first page), is an entry under ~w/<L>/.
L is the map language, or (an entry whose language has no pack, e.g.
English on an English base) the first pack in the file.

Base parity: BASE_N front pages and BASE_N front redirects of the base
("Δήμος Παλλήνης" -> a place page), a fixed sample spread over the base;
each one the base's own suggestions find in their first 10 must be among
the merged file's first 20 (the merged index holds more entries).

PASS when every sampled pack title is found both ways and parity holds. A
file with no pack passes (its indexes are the base's). Prints one JSON
line: {"lang", "titles", "suggest_found", "fulltext_found", "rows",
"base": {"pages", "redirects", "base_found", "merged_found", "missed"},
"title_index", "fulltext_index", "pass"}. Exit 0 = PASS, 1 = FAIL, 2 = packs
but no pack-only article to sample.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SUGGEST_TOP, FULLTEXT_TOP, MERGED_TOP = 10, 25, 20


def pack_langs(a) -> list[str]:
    out: list[str] = []
    for i in range(a.entry_count):
        p = a._get_entry_by_id(i).path
        if p.startswith("~w/"):
            lg = p.split("/")[1]
            if lg not in out:
                out.append(lg)
    return out


def base_sample(base: str, n: int) -> dict[str, list[tuple[str, str]]]:
    """n front pages and n front redirects (path, title) of the base under C,
    a fixed reservoir sample over one pass of its dirents."""
    from tools.langpack.index_pack import front_bitmap, html_mimes, is_front
    from tools.langpack.zimfmt import ZimReader
    z = ZimReader(base)
    fronts = front_bitmap(z)
    hm = html_mimes(z)
    rnd = random.Random(0)
    out: dict[str, list[tuple[str, str]]] = {"pages": [], "redirects": []}
    seen = {"pages": 0, "redirects": 0}
    for d in z.iter_dirents():
        if d.ns != ord("C") or not is_front(fronts, d.index) or not d.title_raw:
            continue
        kind = "redirects" if d.is_redirect else "pages" if d.mime in hm else None
        if kind is None:
            continue
        seen[kind] += 1
        row = (d.path.decode("utf-8", "replace"), d.title.decode("utf-8", "replace"))
        if len(out[kind]) < n:
            out[kind].append(row)
        elif (j := rnd.randrange(seen[kind])) < n:
            out[kind][j] = row
    return out


def parity(m, b, sample: dict[str, list[tuple[str, str]]]) -> dict:
    """Every sampled base entry the base's suggestions find (first 10), the
    merged file's must find too (first 20)."""
    from libzim.suggestion import SuggestionSearcher
    sb, sm = SuggestionSearcher(b), SuggestionSearcher(m)
    res = {"pages": len(sample["pages"]), "redirects": len(sample["redirects"]),
           "base_found": 0, "merged_found": 0, "missed": []}
    for kind in ("pages", "redirects"):
        for path, title in sample[kind]:
            if path not in list(sb.suggest(title).getResults(0, SUGGEST_TOP)):
                continue
            res["base_found"] += 1
            top = list(sm.suggest(title).getResults(0, MERGED_TOP))
            if path in top:
                res["merged_found"] += 1
            else:
                res["missed"].append({"kind": kind[:-1], "title": title, "path": path, "merged_top": top[:3]})
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("merged")
    ap.add_argument("base")
    ap.add_argument("--lang", required=True)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--base-n", type=int, default=12, help="base pages, and base redirects, for the parity check")
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
    if not langs:
        out.update({"titles": 0, "pass": True, "why": "no pack in this file: nothing for Kiwix to miss"})
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not titles:
        out.update({"titles": 0, "pass": False, "why": f"no pack-only article under {pre}"})
        print(json.dumps(out, ensure_ascii=False))
        return 2
    step = max(1, len(titles) // max(1, a.n))
    sample = titles[::step][:a.n]
    ss, se = SuggestionSearcher(m), Searcher(m)
    rows = []
    for t in sample:
        sug = list(ss.suggest(t).getResults(0, SUGGEST_TOP))
        try:
            ful = list(se.search(Query().set_query(t)).getResults(0, FULLTEXT_TOP)) if m.has_fulltext_index else []
        except Exception:  # noqa: BLE001 - no usable full-text index
            ful = []
        rows.append({"title": t, "suggest": any(p.startswith(f"~w/{lang}/") for p in sug),
                     "fulltext": any(p.startswith(f"~w/{lang}/") for p in ful),
                     "suggest_top": sug[:3], "fulltext_top": ful[:3]})
    out.update({"titles": len(rows), "suggest_found": sum(r["suggest"] for r in rows),
                "fulltext_found": sum(r["fulltext"] for r in rows), "rows": rows})
    out["base"] = parity(m, b, base_sample(a.base, a.base_n))
    out["pass"] = (out["suggest_found"] == out["fulltext_found"] == len(rows)
                   and out["base"]["merged_found"] == out["base"]["base_found"])
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
