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
as libzim index data (front articles in the title index, every HTML page in
the full-text one). Its X/title/xapian and X/fulltext/xapian replace the
base's in the merged file (merge_zim.py --index); its other entries are not
carried over. Kiwix then finds the base's pages and the packs' articles
alike, with libzim's own matching (case, accents, words inside titles).

The base's pages' text is read once per base and cached
(<cache>/index-text/<base uuid>.jsonl.gz). OUT.json beside the pack records
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
from streetzim.languages import iso639_3  # noqa: E402
from tools.langpack.gatelib import md5_file  # noqa: E402

FORMAT = "streetzim-search-pack/1"
_DROP = re.compile(r"<(script|style|head|nav)\b.*?</\1\s*>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
MAX_TEXT = 200_000          # characters of a page's text (a long article's index stays bounded)


def fold(text: str) -> str:
    """Without accents (NFD, combining marks out): libzim's reader takes the
    accents off every query, and python-libzim's writer keeps them in the
    full-text terms of index data ("αθηνάς"), so a folded query ("αθηνας")
    would never match. The base's own index is folded the same way."""
    return unicodedata.normalize("NFC", "".join(
        ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch)))


def page_text(raw: bytes) -> str:
    """A page's readable text, as libzim's HTML indexer sees it: no scripts,
    styles, head or navigation; tags out, entities decoded, accents folded."""
    h = raw.decode("utf-8", "replace")
    h = _DROP.sub(" ", h)
    t = html.unescape(_TAG.sub(" ", h))
    return fold(_WS.sub(" ", t).strip()[:MAX_TEXT])


def pages(path: str):
    """(path, title, front, text) of every HTML page of a ZIM (redirects out)."""
    from libzim.reader import Archive

    from tools.langpack.zimfmt import ZimReader
    z = ZimReader(path)
    v1 = z.v1_listing()
    fronts = set(z.iter_u32_blob(*v1)) if v1 else set()
    a = Archive(Path(path))
    html_mimes = {i for i, m in enumerate(z.mimes) if m.split(b";")[0].strip() == b"text/html"}
    for d in z.iter_dirents():
        if d.ns != ord("C") or d.is_redirect or d.mime not in html_mimes:
            continue
        item = a._get_entry_by_id(d.index).get_item()
        yield d.path.decode("utf-8", "replace"), d.title.decode("utf-8", "replace"), d.index in fronts, \
            page_text(bytes(item.content))


def base_pages(base: str, cache: Path | None, log) -> list[tuple[str, str, bool, str]]:
    from libzim.reader import Archive
    uuid = str(Archive(Path(base)).uuid)
    cf = cache / "index-text" / f"{uuid}.jsonl.gz" if cache else None
    if cf and cf.exists():
        with gzip.open(cf, "rt", encoding="utf-8") as fh:
            rows = [tuple(json.loads(x)) for x in fh]
        log(f"  base pages: {len(rows)} (cached {cf.name})")
        return rows  # type: ignore[return-value]
    t0 = time.time()
    rows = list(pages(base))
    if cf:
        cf.parent.mkdir(parents=True, exist_ok=True)
        tmp = cf.with_name(cf.name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, cf)
    log(f"  base pages: {len(rows)} read in {time.time() - t0:.0f}s")
    return rows


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
            return Data(self._t, self._x)

    t0 = time.time()
    rows = base_pages(base, cache, log)
    pack_rows: dict[str, int] = {}
    for p in packs:
        n0 = len(rows)
        rows.extend(pages(p))
        pack_rows[os.path.basename(p)] = len(rows) - n0
    b = Archive(Path(base))
    tmp = out + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    with Creator(Path(tmp)).config_indexing(True, iso639_3(lang)) as c:
        for k, v in (("Name", f"osm_{region}_search-{lang}"), ("Title", f"{region} search ({lang})"[:30]),
                     ("Language", iso639_3(lang)),
                     ("Description", f"Search indexes for a StreetZim map of {region}"[:80]),
                     ("Creator", "OpenStreetMap, Wikipedia"), ("Publisher", "StreetZim"),
                     ("Date", datetime.date.today().isoformat())):
            c.add_metadata(k, v)
        try:
            c.add_illustration(48, bytes(b.get_illustration_item(48).content))
        except Exception:  # noqa: BLE001 - a base without one
            pass
        for path, title, front, text in rows:
            c.add_item(Page(path, title, front, text))
    os.replace(tmp, out)
    side = {"format": FORMAT, "base_uuid": str(b.uuid).replace("-", ""),
            "lang": lang, "region": region, "packs": {os.path.basename(p): md5_file(p) for p in packs},
            "pages": len(rows), "pages_per_pack": pack_rows, "built": datetime.datetime.now().isoformat(timespec="seconds"),
            "seconds": round(time.time() - t0, 1), "bytes": os.path.getsize(out)}
    with open(out + ".json", "w") as fh:
        json.dump(side, fh, ensure_ascii=False, indent=1)
    side["gate"] = gate(out, base, packs)
    return side


def gate(out: str, base: str, packs: list[str]) -> dict:
    """The search pack's own check, written to OUT.gate.json: libzim opens it,
    it has both indexes, and a page of the base and of every pack is found by
    title suggestion and by full text."""
    from libzim.reader import Archive
    from libzim.search import Query, Searcher
    from libzim.suggestion import SuggestionSearcher

    from tools.langpack.zimfmt import ZimReader
    problems: list[str] = []
    a = Archive(Path(out))
    if not (a.has_title_index and a.has_fulltext_index):
        problems.append(f"indexes: title={a.has_title_index} fulltext={a.has_fulltext_index}")
    found = {}
    for src in [base, *packs]:
        z = ZimReader(src)
        v1 = z.v1_listing()
        sample = None
        for i in (z.iter_u32_blob(*v1) if v1 else []):
            d = z.dirent_at(i)
            t = d.title.decode("utf-8", "replace")
            if not d.is_redirect and len(t) >= 4:
                sample = (d.path.decode("utf-8", "replace"), t)
                break
        if sample is None:
            continue
        p, t = sample
        sug = list(SuggestionSearcher(a).suggest(t).getResults(0, 20)) if a.has_title_index else []
        ful = list(Searcher(a).search(Query().set_query(t)).getResults(0, 50)) if a.has_fulltext_index else []
        found[os.path.basename(src)] = {"title": t, "suggest": p in sug, "fulltext": p in ful}
        if p not in sug:
            problems.append(f"{os.path.basename(src)}: suggestion for {t!r} misses {p}")
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
