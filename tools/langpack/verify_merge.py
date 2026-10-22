#!/usr/bin/env python3
"""Verify an N-pack merged ZIM against its BASE and PACKs.

    verify_merge.py MERGED.zim BASE.zim PACK... --lang L [--region R] [--json OUT]

Every check prints PASS/FAIL with its discriminator. Exit 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
import uuid as uuidlib
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from libzim.reader import Archive  # noqa: E402
from libzim.search import Query, Searcher  # noqa: E402
from libzim.suggestion import SuggestionSearcher  # noqa: E402

from tools.langpack.merge_zim import (ISO3, NO_U64, base_lineage, blob_of, derive_uuid,  # noqa: E402
                       open_index, open_pack,
                       parse_counter, strip_mime, utf16_units)
from tools.langpack.zimfmt import ZimReader  # noqa: E402

C, M, W, X = (ord(c) for c in "CMWX")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def sha(b) -> str:
    return hashlib.sha256(bytes(b)).hexdigest()[:16]


def sig(a: Archive, path: str):
    e = a.get_entry_by_path(path)
    if e.is_redirect:
        return ("R", e.title, e.get_redirect_entry().path)
    it = e.get_item()
    return ("I", e.title, it.mimetype, sha(it.content))


def fronts(z: ZimReader):
    v1 = z.v1_listing()
    return [z.dirent_at(i).key for i in z.iter_u32_blob(*v1)] if v1 else []


def range_hash(path, a, b):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        fh.seek(a)
        left = b - a
        while left:
            buf = fh.read(min(8 << 20, left))
            h.update(buf)
            left -= len(buf)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("merged")
    ap.add_argument("base")
    ap.add_argument("packs", nargs="*")
    ap.add_argument("--lang", required=True)
    ap.add_argument("--region")
    ap.add_argument("--region-langs", default="")
    ap.add_argument("--language-meta", choices=("list", "map"), default="list")
    ap.add_argument("--title")
    ap.add_argument("--queries", default="Vaduz,Schaan,Malbun")
    ap.add_argument("--json")
    ap.add_argument("--index", help="the search pack whose indexes the merged file must carry")
    a = ap.parse_args()
    zm, zb = ZimReader(a.merged), ZimReader(a.base)
    am, ab = Archive(a.merged), Archive(a.base)
    packs = sorted((open_pack(p) for p in a.packs), key=lambda p: p.prefix)
    aps = [Archive(Path(p.path)) for p in packs]
    ix = open_index(a.index) if a.index else None

    check("Archive.check() (MD5 + integrity)", am.check(), f"md5={am.checksum}")
    mmeta = {k.encode(): bytes(am.get_metadata(k)) for k in ("Name", "Title", "Language", "Flavour", "Description")
             if k in am.metadata_keys}
    mcfg_bytes = bytes(am.get_entry_by_path("map-config.json").get_item().content)
    want_uuid = uuidlib.UUID(bytes=derive_uuid(zb.header.uuid, [p.uuid for p in packs] + ([ix.uuid] if ix else []),
                                               a.lang, mmeta, mcfg_bytes))
    check("UUID derived (inputs + generated map-config/metadata, S3) and fresh",
          am.uuid == want_uuid and am.uuid not in [ab.uuid] + [x.uuid for x in aps], f"{am.uuid}")
    check("header: no v0 title list", zm.header.title_ptr_pos == NO_U64)
    lin = base_lineage(a.base, zb.header.uuid, check_md5=True)
    check("every pack.json base_uuid is this base or a recorded parent (S4)",
          all(p.base_uuid in lin for p in packs), f"packs={[(p.lang, p.base_uuid[:8]) for p in packs]} lineage={[u[:8] for u in lin]}")

    # base bytes verbatim at the same offsets
    cs = min(zb.cluster_offsets())
    last = zb.dirent_at(zb.header.entry_count - 1)
    d1 = last.offset + last.raw_len
    check("base bytes [first cluster, end of dirents) verbatim at the same offsets",
          range_hash(a.merged, cs, d1) == range_hash(a.base, cs, d1), f"[{cs}, {d1})")

    # key set
    mkeys = [d.key for d in zm.iter_dirents()]
    check("merged keys strictly sorted", all(x < y for x, y in itertools.pairwise(mkeys)), f"n={len(mkeys)}")
    bkeys = {d.key for d in zb.iter_dirents()}
    pkeys = {}
    for k, p in enumerate(packs):
        for d in p.z.iter_dirents():
            if d.ns == C:
                pkeys[d.key] = k
    gen_new = {(M, b"Flavour"), (M, b"Name"), (M, b"Title"), (M, b"Language")} - bkeys
    expected = bkeys | set(pkeys) | gen_new | ({(X, b"fulltext/xapian"), (X, b"title/xapian")} if ix else set())
    check("key set == base + pack C entries (+ generated metadata, + the search pack's indexes)", set(mkeys) == expected,
          f"missing={len(expected - set(mkeys))} extra={sorted(set(mkeys) - expected)[:3]}")
    check("no pack M/X/W entry leaked", not any(k in pkeys for k in mkeys if k[0] != C))

    # base C entries
    bad = []
    n = 0
    for d in zb.iter_dirents():
        if d.ns != C or d.path == b"map-config.json":
            continue
        n += 1
        p = d.path.decode()
        if sig(am, p) != sig(ab, p):
            bad.append(p)
    check("base C entries identical (title, mime, content, redirect target)", not bad, f"checked={n} bad={bad[:3]}")

    # map-config.json
    bcfg = json.loads(bytes(ab.get_entry_by_path("map-config.json").get_item().content))
    mcfg = json.loads(bytes(am.get_entry_by_path("map-config.json").get_item().content))
    rank = {x: i for i, x in enumerate([a.lang] + [x for x in a.region_langs.split(",") if x])}
    visitor = sorted(packs, key=lambda p: (rank.get(p.lang, len(rank)), p.prefix))
    want = dict(bcfg, wikiPacks=[{"lang": p.lang, "prefix": p.prefix.decode(), "articles": p.articles}
                                 for p in visitor], language=a.lang,
                baseLanguage=bcfg.get("language") or "en")
    check("map-config.json = base + wikiPacks (visitor's order) + language + baseLanguage", mcfg == want,
          f"wikiPacks={[w['lang'] for w in mcfg.get('wikiPacks', [])]} language={mcfg.get('language')} "
          f"baseLanguage={mcfg.get('baseLanguage')}")

    # pack entries
    bad, n = [], 0
    for k, p in enumerate(packs):
        for d in p.z.iter_dirents():
            if d.ns != C:
                continue
            n += 1
            ps = d.path.decode()
            if sig(am, ps) != sig(aps[k], ps):
                bad.append(ps)
            if d.is_redirect:
                tgt = am.get_entry_by_path(ps).get_redirect_entry().path
                if not tgt.startswith(p.prefix.decode()):
                    bad.append(ps + " (redirect leaves its pack)")
    check("pack C entries identical (title, mime, content, redirect target in own pack)", not bad,
          f"checked={n} bad={bad[:3]}")
    for k, p in enumerate(packs):
        for f in ("pack.json", "wiki-geo-index.json", "wikidata-text.json"):
            path = p.prefix.decode() + f
            if aps[k].has_entry_by_path(path):
                same = bytes(am.get_entry_by_path(path).get_item().content) == \
                    bytes(aps[k].get_entry_by_path(path).get_item().content)
                if not same:
                    check(f"{path} readable and identical", False)
    check("every pack's pack.json / geo-index / wikidata-text readable", True, f"packs={[p.lang for p in packs]}")

    # metadata
    region = a.region
    if region is None:
        nm = bytes(ab.get_metadata("Name")).decode()
        region = nm.split("_", 2)[2] if nm.count("_") >= 2 else "region"
    langs = [a.lang] + ([p.lang for p in visitor if p.lang != a.lang] if a.language_meta == "list" else [])
    meta = {k: bytes(am.get_metadata(k)).decode() for k in am.metadata_keys if not k.startswith("Illustration")}
    title = meta.get("Title", "")
    ok = (meta.get("Name") == f"osm_{a.lang}_{region}" and meta.get("Language") == ",".join(ISO3[x] for x in langs)
          and utf16_units(title) <= 30 and (a.title is None or title == a.title)
          and all(x in meta.get("Flavour", "") for x in (p.lang for p in packs)))
    check("metadata: Name, Language (639-3 list), Title <= 30 UTF-16, Flavour has the wiki set", ok,
          f"Name={meta.get('Name')} Language={meta.get('Language')} Title={title!r} Flavour={meta.get('Flavour')}")
    desc = meta.get("Description", "")
    check("Description <= 80 characters", len(desc) <= 80, f"{len(desc)}: {desc!r}")
    gen = {"Name", "Title", "Language", "Flavour", "Counter", "Description"}
    other_bad = [k for k in am.metadata_keys if k not in gen and k in ab.metadata_keys
                 and bytes(am.get_metadata(k)) != bytes(ab.get_metadata(k))]
    check("other metadata (incl. illustration) == base", not other_bad, f"bad={other_bad}")

    # counter
    recount, seen = {}, set()
    for d in zm.iter_dirents():
        if d.ns == C and not d.is_redirect and (d.cluster, d.blob) not in seen:
            seen.add((d.cluster, d.blob))
            km = strip_mime(zm.mimes[d.mime])
            recount[km] = recount.get(km, 0) + 1
    stored = parse_counter(bytes(am.get_metadata("Counter")))
    diff = {k.decode(): (stored.get(k), recount.get(k)) for k in set(stored) | set(recount)
            if stored.get(k) != recount.get(k)}
    # the base's own Counter may already disagree with a recount (aliases); the merge must not add to that
    bstored = parse_counter(bytes(ab.get_metadata("Counter")))
    brecount, bseen = {}, set()
    for d in zb.iter_dirents():
        if d.ns == C and not d.is_redirect and (d.cluster, d.blob) not in bseen:
            bseen.add((d.cluster, d.blob))
            km = strip_mime(zb.mimes[d.mime])
            brecount[km] = brecount.get(km, 0) + 1
    bdiff = {k.decode(): (bstored.get(k), brecount.get(k)) for k in set(bstored) | set(brecount)
             if bstored.get(k) != brecount.get(k)}
    check("M/Counter == recount (or carries exactly the base's own discrepancy)", diff == bdiff,
          f"diff={diff} base_diff={bdiff}")

    # v1 listing
    v1 = zm.v1_listing()
    v1_i = zm.find(X, b"listing/titleOrdered/v1")
    assert v1 is not None and v1_i is not None, "the merged file has no v1 listing"
    v1_cluster = zm.dirent_at(v1_i).cluster
    assert v1_cluster is not None
    idx = list(zm.iter_u32_blob(*v1))
    tk = [(zm.dirent_at(i).ns, zm.dirent_at(i).title) for i in idx]
    check("v1 listing sorted by (ns, title), uncompressed", all(x <= y for x, y in itertools.pairwise(tk))
          and zm.cluster_info(v1_cluster) & 0x0F in (0, 1),
          f"n={len(idx)}")
    want_f = set(fronts(zb)) | {k for p in packs for k in fronts(p.z)}
    got_f = {zm.dirent_at(i).key for i in idx}
    check("v1 == base fronts + every pack's fronts", got_f == want_f and am.article_count == len(want_f),
          f"got={len(got_f)} want={len(want_f)} article_count={am.article_count}")
    bad = []
    for p in packs:
        for key in fronts(p.z):
            ki = p.z.find(*key)
            assert ki is not None
            t = p.z.dirent_at(ki).title.decode()
            try:
                if am.get_entry_by_title(t).title != t:
                    bad.append(t)
            except KeyError:
                bad.append(t)
    check("get_entry_by_title finds every pack front title", not bad, f"bad={bad[:3]}")

    # main page, redirects, Xapian
    me, be = am.main_entry, ab.main_entry
    check("main page == base's", (me.get_redirect_entry().path if me.is_redirect else me.path) ==
          (be.get_redirect_entry().path if be.is_redirect else be.path) and zm.dirent_at(zm.header.main_page).key == (W, b"mainPage"))
    nred = badr = 0
    for d in zm.iter_dirents():
        if d.ns == C and d.is_redirect:
            nred += 1
            try:
                am.get_entry_by_path(d.path.decode()).get_redirect_entry().get_item()
            except Exception:  # noqa: BLE001
                badr += 1
    check("every C redirect resolves", badr == 0, f"redirects={nred}")
    if ix is None:
        xok = all(blob_of(zm, (X, n)) == blob_of(zb, (X, n)) for n in (b"fulltext/xapian", b"title/xapian"))
        check("base Xapian databases kept (byte-identical)", xok and am.has_title_index == ab.has_title_index,
              f"title={am.has_title_index} fulltext={am.has_fulltext_index}")
    else:
        xok = all(blob_of(zm, (X, n)) == blob_of(ix.z, (X, n)) for n in (b"fulltext/xapian", b"title/xapian"))
        check("the search pack's Xapian databases replace the base's (byte-identical)",
              xok and am.has_title_index and am.has_fulltext_index,
              f"title={am.has_title_index} fulltext={am.has_fulltext_index} pack={os.path.basename(a.index)}")
        lin = base_lineage(a.base, zb.header.uuid, check_md5=True)
        check("the search pack was built for this base", ix.base_uuid in lin, f"{ix.base_uuid[:8]}")

    ss = SuggestionSearcher(am)
    for q in a.queries.split(","):
        print(f"INFO  suggest {q!r}: {list(ss.suggest(q).getResults(0, 5))}")
        if am.has_fulltext_index:
            print(f"INFO  search {q!r}: {list(Searcher(am).search(Query().set_query(q)).getResults(0, 5))}")
    nf = sum(1 for _, ok, _ in results if not ok)
    print(f"SUMMARY {len(results) - nf}/{len(results)} PASS")
    if a.json:
        json.dump([{"check": c, "ok": o, "detail": d} for c, o, d in results], open(a.json, "w"), indent=1)
    return 1 if nf else 0


if __name__ == "__main__":
    sys.exit(main())
