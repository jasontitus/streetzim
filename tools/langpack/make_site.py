#!/usr/bin/env python3
"""Builder-page files for one region (web/lab/build/, builder.js schema).

    make_site.py BASE.zim --packs-dir DIR --region california --region-title California
        --region-langs en --langs de,th,zh,es,ja,ko,fr,en --titles JSON
        --url-template '{proxy}/item/{file}' --base-download URL -o OUT_RECIPES_DIR

URLs may start "{proxy}": the builder page puts its range proxy there
(web/drive/preview-config.js), so the proxy is set in one place.

Writes OUT/<L>.json (streetzim-recipe/2, absolute pack URLs) for every map
language and OUT/index.json:

  {"regions": [{"id", "title",
                "base": {"file", "size", "md5" (the whole file's, as archive.org lists it), "download"},
                "probe": {"url", "size"},
                "menu": [{"lang", "label", "recipe": "recipes/<L>.json", "size",
                          "download_bytes", "name", "file_name", "md5",
                          "packs": [{"lang", "label", "articles", "bytes", "in_base"?}]}]}]}

download_bytes is what the page really fetches: every 16 MiB chunk of a pack
that a segment touches (the worker reads and checks whole chunks).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack.merge_zim import KEY_MAPCFG, ZimReader, blob_of, open_pack, peak_rss_mb  # noqa: E402
from tools.langpack.recipe2 import build_recipe, find_search_pack, menu_packs, search_lang, wanted_packs  # noqa: E402
from tools.langpack import gatelib  # noqa: E402
from tools.langpack.gatelib import entry_verdict_ok, pack_gate_ok  # noqa: E402


# Each language's own name (the builder page adds the visitor's name for it).
NATIVE = {"ar": "العربية", "de": "Deutsch", "el": "Ελληνικά", "en": "English", "es": "Español",
          "fa": "فارسی", "fr": "Français", "he": "עברית", "hi": "हिन्दी", "hy": "Հայերեն",
          "id": "Bahasa Indonesia", "it": "Italiano", "ja": "日本語", "ko": "한국어",
          "nl": "Nederlands", "pl": "Polski", "pt": "Português", "rm": "Rumantsch", "ru": "Русский", "sv": "Svenska",
          "th": "ไทย", "tr": "Türkçe", "uk": "Українська", "vi": "Tiếng Việt", "zh": "中文"}


# Per entry (plan review M-3): what a visitor downloads besides the base
# (the packs' chunks the recipe touches, the search pack's included), and the
# recipe itself, which the page parses whole.
ENTRY_BUDGET = int(os.environ.get("LANGPACK_ENTRY_BUDGET") or 2 << 30)
RECIPE_BUDGET = int(os.environ.get("LANGPACK_RECIPE_BUDGET") or 32 << 20)


def over_budget(recipe_bytes: int, download_bytes: int, entry_budget: int = ENTRY_BUDGET,
                recipe_budget: int = RECIPE_BUDGET) -> str | None:
    """Why an entry is over the per-entry budget, or None."""
    if download_bytes > entry_budget:
        return (f"over the per-entry budget: {download_bytes / 1e6:.0f} MB of packs to download "
                f"> {entry_budget / 1e6:.0f} MB")
    if recipe_bytes > recipe_budget:
        return f"over the per-entry budget: a {recipe_bytes / 1e6:.1f} MB recipe > {recipe_budget / 1e6:.0f} MB"
    return None


def touched_bytes(recipe: dict) -> dict[str, int]:
    out = {}
    for lang, p in recipe["packs"].items():
        C, size = p["chunk"], p["size"]
        ks = set()
        for s in recipe["segments"]:
            if s["src"] == "pack" and s["pack"] == lang:
                ks.update(range(s["off"] // C, (s["off"] + s["len"] - 1) // C + 1))
        out[lang] = sum(min((k + 1) * C, size) - k * C for k in ks)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--packs-dir", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--region-title", required=True)
    ap.add_argument("--region-langs", required=True)
    ap.add_argument("--langs", required=True)
    ap.add_argument("--titles", default="{}")
    ap.add_argument("--url-template", required=True)
    ap.add_argument("--base-download", required=True)
    ap.add_argument("--url-override", action="append", default=[],
                    help="LANG=TEMPLATE: a pack served from another item (repeatable)")
    ap.add_argument("--base-url", help="the base through the range proxy (region.base.url, one-click mode)")
    ap.add_argument("--language-meta", choices=("list", "map"), default="list")
    ap.add_argument("--recipe-prefix", default="recipes/")
    ap.add_argument("--entry-budget", type=int, default=ENTRY_BUDGET,
                    help="most bytes of packs one entry may download (LANGPACK_ENTRY_BUDGET; 2 GiB)")
    ap.add_argument("--recipe-budget", type=int, default=RECIPE_BUDGET,
                    help="largest recipe JSON (LANGPACK_RECIPE_BUDGET; 32 MiB)")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    titles = json.loads(a.titles)
    rl = [x for x in a.region_langs.split(",") if x]
    zb = ZimReader(a.base)
    cfg = json.loads(blob_of(zb, KEY_MAPCFG) or b"{}")
    base_wiki = (cfg.get("language") or "en") if cfg.get("hasWikiArticles") else None
    base_articles = sum(1 for d in zb.iter_dirents()
                        if d.ns == ord("C") and d.path.startswith(b"wiki-article/") and not d.is_redirect)
    avail, refused = {}, []
    for f in sorted(os.listdir(a.packs_dir)):
        if f.endswith(".zim") and f.startswith(f"osm_{a.region}_wiki-"):
            ok, why = pack_gate_ok(os.path.join(a.packs_dir, f))      # review S1
            if not ok:
                refused.append({"pack": f, "why": why})
                continue
            p = open_pack(os.path.join(a.packs_dir, f))
            avail[p.lang] = p
    menu, rows = [], []
    overrides = dict(o.split("=", 1) for o in a.url_override)
    for L in a.langs.split(","):
        packs = menu_packs(rl, base_wiki, L, {k: v.path for k, v in avail.items()})
        recipe, plan, st = build_recipe(a.base, [avail[x].path for x in packs], L, url_template=a.url_template,
                                        url_overrides=overrides,
                                        region=a.region, title=titles.get(L), region_langs=rl,
                                        language_meta=a.language_meta,
                                        index_path=find_search_pack(a.packs_dir, a.base,
                                                                    [avail[x].path for x in packs], search_lang(L)))
        missing = sorted(wanted_packs(rl, base_wiki, L) - set(packs))
        ok, why = entry_verdict_ok(a.base, L, recipe)                  # review S2
        if missing:
            ok, why = False, f"missing packs {missing}"
        if not ok:
            refused.append({"lang": L, "why": why})
            print(json.dumps({"L": L, "REFUSED": why}), flush=True)
            stale = os.path.join(a.out, f"{L}.json")
            if os.path.exists(stale):
                os.remove(stale)
            continue
        path = os.path.join(a.out, f"{L}.json")
        with open(path, "w") as fh:
            json.dump(recipe, fh, ensure_ascii=False)
        tb = touched_bytes(recipe)
        why = over_budget(os.path.getsize(path), sum(tb.values()), a.entry_budget, a.recipe_budget)
        if why:
            refused.append({"lang": L, "why": why})
            print(json.dumps({"L": L, "REFUSED": why}), flush=True)
            os.remove(path)
            continue
        wiki_order = [w["lang"] for w in json.loads(plan.layout["mapcfg"])["wikiPacks"]]
        plist = [{"lang": x, "label": f"{NATIVE.get(x, x)} Wikipedia", "articles": avail[x].articles,
                  "bytes": tb[x]} for x in wiki_order]
        if base_wiki:
            plist.append({"lang": base_wiki, "label": f"{NATIVE.get(base_wiki, base_wiki)} Wikipedia",
                          "articles": base_articles, "bytes": 0, "in_base": True})
        menu.append({"lang": L, "label": NATIVE.get(L, L), "recipe": f"{a.recipe_prefix}{L}.json",
                     "size": recipe["size"], "download_bytes": sum(tb.values()), "name": recipe["name"],
                     # the recipe file's size: the page shows how much of it has come
                     "recipe_bytes": os.path.getsize(path),
                     "file_name": recipe["file_name"], "md5": recipe["md5"], "packs": plist,
                     "gate_verdict": os.path.relpath(gatelib.verdict_path(a.base, L), gatelib.work_dir())})
        rows.append({"L": L, "packs": packs, "size": recipe["size"], "download_bytes": sum(tb.values()),
                     "segments": st["segments"], "inline_bytes": st["inline_bytes"],
                     "recipe_json_bytes": os.path.getsize(path), "plan_s": st["plan_s"], "md5": recipe["md5"]})
        print(json.dumps(rows[-1]), flush=True)
    # The speed probe reads the biggest pack the menu uses: only those are
    # uploaded (ship_packs.sh), and a refused entry's pack may not be.
    used = {x for row in rows for x in row["packs"]} or set(avail)
    if not used:
        print(f"make_site.py: no pack in {a.packs_dir} passed its gate for {a.region}: "
              f"nothing to offer (refused: {json.dumps(refused, ensure_ascii=False)})",
              file=sys.stderr, flush=True)
        sys.exit(1)
    biggest = max((avail[x] for x in used), key=lambda p: os.path.getsize(p.path))
    index = {"format": "streetzim-build-index/1", "generated": datetime.date.today().isoformat(),
             "regions": [{"id": a.region, "title": a.region_title,
                          "base": {"file": os.path.basename(os.path.realpath(a.base)),
                                   "size": os.path.getsize(a.base), "md5": gatelib.md5_file(a.base),
                                   "download": a.base_download},
                          "probe": {"url": (overrides.get(biggest.lang) or a.url_template).format(
                                        file=os.path.basename(biggest.path), proxy="{proxy}"),
                                    "size": os.path.getsize(biggest.path)},
                          "menu": menu}]}
    if a.base_url:
        index["regions"][0]["base"]["url"] = a.base_url
    with open(os.path.join(a.out, "index.json"), "w") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "region.json"), "w") as fh:   # the region object alone
        json.dump(index["regions"][0], fh, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "refused.json"), "w") as fh:
        json.dump(refused, fh, ensure_ascii=False, indent=1)
    print(json.dumps({"index": os.path.join(a.out, "index.json"), "menu": [m["lang"] for m in menu],
                      "refused": refused, "peak_rss_MB": round(peak_rss_mb(), 1)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
