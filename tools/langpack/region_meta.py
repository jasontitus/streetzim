#!/usr/bin/env python3
"""What ship_packs.sh builds for a base: languages, packs and titles.

    region_meta.py BASE.zim --wiki-src DIR [--core ar,de,...] [--cache DIR]

Prints one JSON object:

  {"region_title": map-config "name",
   "base_lang": the base's language (map-config "language", else en),
   "base_wiki": the language of the base's own Wikipedia articles, or null,
   "region_langs": map-config regionLanguages (most spoken first) that have a
            Wikipedia ZIM; a map of any tier but country, local and us-region
            (--tier, or the region's tier in cloud/regions.tsv; a region not
            in it counts as capped) keeps at most --region-packs-max (3)
            besides the base's own, a country map all,
   "langs": the menu: CORE + region_langs (+ the base's language), each with
            a Wikipedia ZIM in --wiki-src or needing none (the base's own),
   "pack_langs": the packs to build: every language a menu entry needs
            (recipe2.menu_packs: the entry's, the region's, English) minus
            the base's own Wikipedia,
   "titles": {L: the merged file's Title}, the region's name in L where the
            pack builder's Wikidata cache has it (the base's country record,
            category-index/admin.json, when it names the region), else the
            base's Title; at most 30 UTF-16 units,
   "dropped": [{"lang", "why"}]: languages left out, and why}

CORE defaults to the core set (CORE_LANGS below). A region language
without a Wikipedia ZIM is dropped from region_langs, since every menu
entry would need its pack.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack.merge_zim import KEY_MAPCFG, blob_of  # noqa: E402
from tools.langpack.recipe2 import menu_packs  # noqa: E402
from tools.langpack.zimfmt import ZimReader  # noqa: E402

# The core set: every map language a published map is offered in
# (docs/language-pack-pipeline.md). The tiles' languages plus English.
CORE_LANGS = ("ar", "de", "el", "en", "es", "fa", "fr", "he", "hi", "id", "it", "ja",
              "ko", "nl", "pl", "pt", "ru", "th", "tr", "uk", "vi", "zh")
TITLE_MAX_UTF16 = 30
# Every menu entry carries a pack for each of the region's languages, so a
# continent or a multi-country map (Europe lists 38, by speakers) keeps its
# most spoken few: the first REGION_PACKS_MAX with a Wikipedia ZIM, the
# base's own not counted. A country map keeps all its languages
# (Switzerland: de, fr, it, rm in every entry).
REGION_PACKS_MAX = 3
# The cap applies to every tier but these (continent, multi-country, and a
# region cloud/regions.tsv does not know: review S-4, it fails closed).
UNCAPPED_TIERS = ("country", "local", "us-region")
REGISTRY = os.path.join(ROOT, "cloud", "regions.tsv")


def registry_tier(region: str | None, path: str = REGISTRY) -> str | None:
    """The region's tier in cloud/regions.tsv (continent, multi-country,
    country, us-region, local), or None for a region not in it."""
    if not region:
        return None
    try:
        for line in open(path, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if not line.startswith("#") and len(f) > 3 and f[0] == region:
                return f[3]
    except OSError:
        return None
    return None


def utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def cut16(s: str, n: int) -> str:
    """The longest prefix of s within n UTF-16 units (a character outside the
    BMP counts 2; merge_zim refuses a Title over 30)."""
    out = ""
    for ch in s:
        if utf16(out + ch) > n:
            break
        out += ch
    return out


def has_wiki(src: str, lang: str) -> bool:
    from streetzim.wiki_pack import pick_source
    try:
        pick_source(src, lang)
        return True
    except (FileNotFoundError, OSError):
        return False


def region_qid(z: ZimReader, name: str) -> str | None:
    """The Q-ID of the base's admin record named like the region (a country
    map's country), else None."""
    try:
        admin = json.loads(blob_of(z, (ord("C"), b"category-index/admin.json")) or b"[]")
    except ValueError:
        return None
    for r in admin if isinstance(admin, list) else []:
        if isinstance(r, dict) and r.get("n") == name and r.get("q") and r.get("s") in ("country", "state", "region"):
            return str(r["q"])
    return None


def label_in(cache: Path | None, lang: str, qid: str | None) -> str:
    if not cache or not qid:
        return ""
    p = cache / "wikidata-text" / f"{lang}.jsonl"
    found = ""
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if qid in line:
                    try:
                        q, label, _desc = json.loads(line)
                    except ValueError:
                        continue
                    if q == qid and label:
                        found = label
    except OSError:
        return ""
    return found


def meta(base: str, wiki_src: str, core: list[str], cache: Path | None, region_max: int | None = REGION_PACKS_MAX,
         tier: str | None = None) -> dict:
    """`region_max` applies to every tier but UNCAPPED_TIERS (None: never),
    an unknown one (None) included."""
    if tier in UNCAPPED_TIERS:
        region_max = None
    z = ZimReader(base)
    cfg = json.loads(blob_of(z, KEY_MAPCFG) or b"{}")
    name = str(cfg.get("name") or Path(base).stem)
    base_lang = str(cfg.get("language") or "en")
    base_wiki = base_lang if cfg.get("hasWikiArticles") else None
    dropped: list[dict] = []
    region_langs = []
    for lg in cfg.get("regionLanguages") or []:
        if not isinstance(lg, str):
            continue
        if not (lg == base_wiki or has_wiki(wiki_src, lg)):
            dropped.append({"lang": lg, "why": "region language with no Wikipedia ZIM in WIKI_SRC"})
        elif region_max is not None and len([x for x in region_langs if x != base_wiki]) >= region_max \
                and lg != base_wiki:
            dropped.append({"lang": lg, "why": f"region language beyond the {region_max} most spoken "
                                               "(every menu entry carries the region's packs)"})
        else:
            region_langs.append(lg)
    langs = []
    for lg in list(dict.fromkeys([*core, *region_langs, base_lang])):
        if lg == base_wiki or has_wiki(wiki_src, lg):
            langs.append(lg)
        else:
            dropped.append({"lang": lg, "why": "no Wikipedia ZIM in WIKI_SRC"})
    avail = {lg: "" for lg in langs if lg != base_wiki}
    if "en" != base_wiki and has_wiki(wiki_src, "en"):
        avail["en"] = ""
    need: set[str] = set()
    for L in langs:
        need.update(menu_packs(region_langs, base_wiki, L, avail))
    try:
        base_title = blob_of(z, (ord("M"), b"Title"))
    except Exception:  # noqa: BLE001 - a base without M/Title
        base_title = None
    fallback = (base_title or b"").decode("utf-8", "replace") or name
    qid = region_qid(z, name)
    titles = {}
    for L in langs:
        t = label_in(cache, L, qid) or fallback
        titles[L] = t if utf16(t) <= TITLE_MAX_UTF16 else cut16(fallback, TITLE_MAX_UTF16)
    return {"region_title": name, "base_lang": base_lang, "base_wiki": base_wiki,
            "region_langs": region_langs, "langs": langs, "pack_langs": sorted(need),
            "region_qid": qid, "titles": titles, "dropped": dropped}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("base")
    ap.add_argument("--wiki-src", required=True)
    ap.add_argument("--core", default=",".join(CORE_LANGS))
    ap.add_argument("--cache", default=None, help="the pack builder's cache (region names in each language)")
    ap.add_argument("--region-packs-max", type=int, default=REGION_PACKS_MAX,
                    help=f"a map of any tier but country, local and us-region keeps its first N languages with a Wikipedia ZIM "
                         f"(default {REGION_PACKS_MAX})")
    ap.add_argument("--region", help="the region id: its tier (cloud/regions.tsv) decides the cap")
    ap.add_argument("--tier", help="the tier, instead of looking it up")
    a = ap.parse_args(argv)
    core = [x.strip() for x in a.core.split(",") if x.strip()]
    tier = a.tier or registry_tier(a.region)
    out = meta(a.base, a.wiki_src, core, Path(a.cache) if a.cache else None, a.region_packs_max, tier)
    out["tier"] = tier
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
