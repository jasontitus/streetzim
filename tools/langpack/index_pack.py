#!/usr/bin/env python3
"""Search pack: Kiwix's search indexes for one merged file's entries.

    index_pack.py BASE.zim [PACK.zim ...] --lang L --region R -o OUT.zim [--cache DIR]

Kiwix searches a ZIM through libzim: title suggestions from X/title/xapian
(or, without it, a case-sensitive prefix walk of the title listing) and full
text from X/fulltext/xapian. A merged file that kept the base's indexes
found none of the packs' articles (search_gate.py: 0 of 8 on Greece el).

A search pack is a small ZIM written by libzim itself (python-libzim, so the
indexes are exactly what libzim reads): one empty item per page of the
merged file, at the merged file's path, with the page's title and its text
as libzim index data (every HTML page in the full-text index; in the title
index, every page of the base and the packs' front articles), and every
redirect of the base and every front redirect of the packs at its own path ("Δήμος Παλλήνης" -> the Pallini page: Kiwix suggests
places by their other names). A published base flags none of its pages or
redirects front (its builder's own title index holds them all regardless),
so the flags cannot say which of the base's go in: until 2026-10-29 none
did, and Kiwix found none of the map's places or the base's own Wikipedia
in a merged file (Bahrain's English version: "Manama", 0 suggestions). Its X/title/xapian and X/fulltext/xapian replace the
base's in the merged file (merge_zim.py --index); its other entries are not
carried over. Kiwix then finds the base's pages and the packs' articles
alike, with libzim's own matching (case, accents, words inside titles).

Pages and redirects stream from the base and the packs into libzim's
writer; nothing holds every page's text. The base's are read once per base
and cached (<cache>/index-text/<base uuid>.v<TEXT_VERSION>.jsonl.gz; a cache
of another version is not read). OUT.json beside the pack records
what it was built from (base UUID, pack files and MD5s, language), and
OUT.gate.json its own check (libzim opens it, both indexes, a suggestion
and a full-text query find a page of each source); recipe2.py's menu takes
a search pack only with a passing gate built from exactly the entry's packs.
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from streetzim.languages import pack_iso639_3  # noqa: E402
from tools.langpack.gatelib import md5_file  # noqa: E402
from tools.langpack.merge_zim import peak_rss_mb  # noqa: E402

FORMAT = "streetzim-search-pack/1"
# The index-text cache's rows: 2 = libzim's folding (every Unicode mark, and
# lowercase) and the front redirects; 3 = every page of the base in the title
# index, and all its redirects. A new folding, row kind or rule is a new
# version; a search pack of another version is not reused
# (recipe2.find_search_pack).
TEXT_VERSION = 3
_DROP = re.compile(r"<(script|style|head|nav)\b.*?</\1\s*>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
MAX_TEXT = 200_000          # characters of a page's text (a long article's index stays bounded)


def fold(text: str) -> str:
    """libzim's zim::removeAccents (src/tools.cpp: the ICU transliterator
    "Lower; NFD; [:M:] remove; NFC"): lowercase, then every Unicode mark out
    (category M: the Thai and Devanagari vowel signs too, not only the
    combining accents). libzim folds every query so, and the index data of
    its own HTML indexer (defaultIndexData.h); custom index data, as a
    search pack's, it indexes as given, so the pack folds it here: Greek
    full text found nothing before ("Παρθενώνας"), Thai and Hindi words
    nothing until category M ("สวัสดี", "दिल्ली")."""
    return unicodedata.normalize("NFC", "".join(
        ch for ch in unicodedata.normalize("NFD", text.lower()) if not unicodedata.category(ch).startswith("M")))


def page_text(raw: bytes) -> str:
    """A page's readable text, as libzim's HTML indexer sees it: no scripts,
    styles, head or navigation; tags out, entities decoded, accents folded."""
    h = raw.decode("utf-8", "replace")
    h = _DROP.sub(" ", h)
    t = html.unescape(_TAG.sub(" ", h))
    return fold(_WS.sub(" ", t).strip()[:MAX_TEXT])


def front_bitmap(z) -> bytearray:
    """Bit i set: entry i is a front article (in X/listing/titleOrdered/v1).
    A bitmap, not a set: a continent's millions of entries in a few MB."""
    bits = bytearray((z.header.entry_count + 7) // 8)
    v1 = z.v1_listing()
    for i in (z.iter_u32_blob(*v1) if v1 else []):
        bits[i >> 3] |= 1 << (i & 7)
    return bits


def is_front(bits: bytearray, i: int) -> bool:
    return bool(bits[i >> 3] >> (i & 7) & 1)


def html_mimes(z) -> set[int]:
    return {i for i, m in enumerate(z.mimes) if m.split(b";")[0].strip() == b"text/html"}


def final_target(z, d, hm: set[int], hops: int = 8):
    """The page a redirect leads to (through up to `hops` redirects), or None
    when that is not an HTML page under C (the search pack has only those)."""
    for _ in range(hops):
        if d.target is None:
            return None
        d = z.dirent_at(d.target)
        if not d.is_redirect:
            return d if d.ns == ord("C") and d.mime in hm else None
    return None


def entries(path: str, base: bool = False):
    """Rows for the search pack, streamed in path order: ("p", path, title,
    front, text) for every HTML page of a ZIM, ("r", path, title, target)
    for every front redirect that leads to one. Of a base (`base`): every
    page front, and every redirect."""
    from libzim.reader import Archive

    from tools.langpack.zimfmt import ZimReader
    z = ZimReader(path)
    fronts = front_bitmap(z)
    a = Archive(Path(path))
    hm = html_mimes(z)
    for d in z.iter_dirents():
        if d.ns != ord("C"):
            continue
        if d.is_redirect:
            if (base or is_front(fronts, d.index)) and (t := final_target(z, d, hm)) is not None:
                yield "r", d.path.decode("utf-8", "replace"), d.title.decode("utf-8", "replace"), \
                    t.path.decode("utf-8", "replace")
            continue
        if d.mime not in hm:
            continue
        item = a._get_entry_by_id(d.index).get_item()
        yield "p", d.path.decode("utf-8", "replace"), d.title.decode("utf-8", "replace"), base or is_front(fronts, d.index), \
            page_text(bytes(item.content))


def base_entries(base: str, cache: Path | None, log):
    """entries(base), from the index-text cache when it has them; else read
    and written to the cache as they stream (kept only once complete)."""
    from libzim.reader import Archive
    uuid = str(Archive(Path(base)).uuid)
    cf = cache / "index-text" / f"{uuid}.v{TEXT_VERSION}.jsonl.gz" if cache else None
    if cf and cf.exists():
        log(f"  base pages: cached {cf.name}")
        with gzip.open(cf, "rt", encoding="utf-8") as fh:
            for x in fh:
                yield tuple(json.loads(x))
        return
    t0 = time.time()
    if not cf:
        yield from entries(base, base=True)
        return
    cf.parent.mkdir(parents=True, exist_ok=True)
    tmp = cf.with_name(cf.name + f".{os.getpid()}.tmp")
    n = 0
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for r in entries(base, base=True):
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                n += 1
                yield r
        os.replace(tmp, cf)
        log(f"  base pages and redirects: {n} read in {time.time() - t0:.0f}s")
    finally:
        if tmp.exists():
            tmp.unlink()


def build(base: str, packs: list[str], lang: str, region: str, out: str, cache: Path | None = None,
          log=lambda m: print(m, file=sys.stderr, flush=True)) -> dict:
    from libzim.reader import Archive
    from libzim.writer import Creator, Hint, IndexData, Item, StringProvider

    class Data(IndexData):
        def __init__(self, title: str, text: str):
            super().__init__()
            self._t, self._x = title, text

        def has_indexdata(self):
            return True

        def get_title(self):
            return self._t

        def get_content(self):
            return self._x

        def get_keywords(self):
            return ""

        def get_wordcount(self):
            return len(self._x.split())

        def get_geoposition(self):
            return None

    class Page(Item):
        def __init__(self, path: str, title: str, front: bool, text: str):
            super().__init__()
            self._p, self._t, self._f, self._x = path, title, front, text

        def get_path(self):
            return self._p

        def get_title(self):
            return self._t

        def get_mimetype(self):
            return "text/html"

        def get_contentprovider(self):
            return StringProvider("")

        def get_hints(self):
            return {Hint.FRONT_ARTICLE: self._f, Hint.COMPRESS: True}

        def get_indexdata(self):
            # folded as libzim folds its own index data; the title index
            # (the item's title) libzim folds itself
            return Data(fold(self._t), self._x)

    t0 = time.time()
    counts: dict[str, dict[str, int]] = {}

    def add(c, src: str, rows) -> None:
        n = counts.setdefault(src, {"pages": 0, "redirects": 0})
        for r in rows:
            if r[0] == "p":
                c.add_item(Page(*r[1:]))
                n["pages"] += 1
            else:
                c.add_redirection(r[1], r[2], r[3], {Hint.FRONT_ARTICLE: True})
                n["redirects"] += 1
    b = Archive(Path(base))
    tmp = out + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    with Creator(Path(tmp)).config_indexing(True, pack_iso639_3(lang)) as c:
        for k, v in (("Name", f"osm_{region}_search-{lang}"), ("Title", f"{region} search ({lang})"[:30]),
                     ("Language", pack_iso639_3(lang)),
                     ("Description", f"Search indexes for a StreetZim map of {region}"[:80]),
                     ("Creator", "OpenStreetMap, Wikipedia"), ("Publisher", "StreetZim"),
                     ("Date", datetime.date.today().isoformat())):
            c.add_metadata(k, v)
        try:
            c.add_illustration(48, bytes(b.get_illustration_item(48).content))
        except Exception:  # noqa: BLE001 - a base without one
            pass
        add(c, "base", base_entries(base, cache, log))
        for p in packs:
            add(c, os.path.basename(p), entries(p))
    os.replace(tmp, out)
    side = {"format": FORMAT, "text_version": TEXT_VERSION, "base_uuid": str(b.uuid).replace("-", ""),
            "lang": lang, "region": region, "packs": {os.path.basename(p): md5_file(p) for p in packs},
            "pages": sum(x["pages"] for x in counts.values()),
            "redirects": sum(x["redirects"] for x in counts.values()),
            "pages_per_pack": {k: v["pages"] for k, v in counts.items() if k != "base"}, "per_source": counts,
            "built": datetime.datetime.now().isoformat(timespec="seconds"),
            "seconds": round(time.time() - t0, 1), "bytes": os.path.getsize(out), "peak_rss_mb": peak_rss_mb()}
    with open(out + ".json", "w") as fh:
        json.dump(side, fh, ensure_ascii=False, indent=1)
    side["gate"] = gate(out, base, packs)
    return side


def gate(out: str, base: str, packs: list[str]) -> dict:
    """The search pack's own check, written to OUT.gate.json: libzim opens it,
    it has both indexes, a page of the base and of every pack is found by
    title suggestion (a pack's by full text too), and so is a redirect of the
    base by its own title. The base's samples come from all its pages (a
    place's page first) and redirects, not its front listing, which a
    published base leaves empty: a gate that sampled from it checked nothing
    of the base."""
    from libzim.reader import Archive
    from libzim.search import Query, Searcher
    from libzim.suggestion import SuggestionSearcher

    from tools.langpack.zimfmt import ZimReader
    problems: list[str] = []
    a = Archive(Path(out))
    if not (a.has_title_index and a.has_fulltext_index):
        problems.append(f"indexes: title={a.has_title_index} fulltext={a.has_fulltext_index}")

    def first(z, redirect: bool):
        v1 = z.v1_listing()
        for i in (z.iter_u32_blob(*v1) if v1 else []):
            d = z.dirent_at(i)
            t = d.title.decode("utf-8", "replace")
            if d.ns == ord("C") and d.is_redirect == redirect and len(t) >= 4:
                return d.path.decode("utf-8", "replace"), t
        return None

    def base_sample(z, redirect: bool):
        hm, best = html_mimes(z), None
        for d in z.iter_dirents():
            p, t = d.path.decode("utf-8", "replace"), d.title.decode("utf-8", "replace")
            if d.ns != ord("C") or d.is_redirect != redirect or len(t) < 4:
                continue
            if redirect:
                if final_target(z, d, hm) is not None:
                    return p, t
            elif d.mime in hm:
                if p.startswith("search/"):
                    return p, t
                best = best or (p, t)
        return best

    found = {}
    for src in [base, *packs]:
        z = ZimReader(src)
        samples = (("page", base_sample(z, False)), ("redirect", base_sample(z, True))) if src == base \
            else (("page", first(z, False)), ("redirect", None))
        for kind, sample in samples:
            if sample is None:
                continue
            p, t = sample
            sug = list(SuggestionSearcher(a).suggest(t).getResults(0, 20)) if a.has_title_index else []
            ful = list(Searcher(a).search(Query().set_query(t)).getResults(0, 50)) if a.has_fulltext_index else []
            key = os.path.basename(src) + ("" if kind == "page" else " (redirect)")
            found[key] = {"title": t, "suggest": p in sug, "fulltext": p in ful}
            if p not in sug:
                problems.append(f"{key}: suggestion for {t!r} misses {p}")
            if src != base and p not in ful:
                problems.append(f"{key}: full text for {t!r} misses {p}")
    g = {"ok": not problems, "problems": problems, "found": found, "pack_md5": md5_file(out),
         "kind": "search"}
    with open(out + ".gate.json", "w") as fh:
        json.dump(g, fh, ensure_ascii=False, indent=1)
    return g


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("base")
    ap.add_argument("packs", nargs="*")
    ap.add_argument("--lang", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--cache", default=None)
    a = ap.parse_args(argv)
    side = build(a.base, a.packs, a.lang, a.region, a.out, Path(a.cache) if a.cache else None)
    print(json.dumps({k: v for k, v in side.items() if k != "gate"} | {"gate_ok": side["gate"]["ok"],
                                                                         "gate_problems": side["gate"]["problems"]},
                     ensure_ascii=False))
    return 0 if side["gate"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
