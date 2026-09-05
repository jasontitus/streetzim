"""Resolve Wikidata Q-IDs -> English Wikipedia article titles.

Why: streetzim search-index records carry two cross-ref fields — `w`
(the OSM ``wikipedia=`` tag, already a title like ``en:Lincoln_Memorial``)
and `q` (the OSM ``wikidata=`` Q-ID). mcpzim's ``ZimService.articleByTitle``
links `w` straight to a Wikipedia ZIM by title. But the *majority* of
wiki-tagged features carry only `q` (no ``wikipedia=`` tag), and a Q-ID
can't be turned into an article without a Q-ID->title map.

This module builds that map (from the public Wikidata API, or an offline
dump) and fills in `w` from `q`, so those records become linkable with
NO mcpzim change. A Wikidata enwiki *sitelink* is, by construction, the
exact title of a live English Wikipedia article, so the resolved title
honours mcpzim's "exact-match only, no fuzzy name matching" contract.

Measured lift (osm-california-2026-05-09.zim): 8,863 distinct POI Q-IDs
without a title; 3,199 (36%) have an enwiki article; resolving them lifts
the directly-gettable distinct-article count ~2.4x (2,260 -> ~5,459).
The other 64% are Wikidata items (minor streets/creeks/peaks, often GNIS
imports) with no enwiki article — nothing to link to. See
``docs/wikidata-title-resolution.md``.

Privacy: public data only (Q-IDs + article titles). The User-Agent
identifies the project by its PUBLIC repo URL — never personal contact
info — per the repo's outbound-HTTP convention.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable, Optional

USER_AGENT = "streetzim-wikidata/1.0 (+https://github.com/jasontitus/streetzim)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
ENWIKI = "enwiki"
BATCH = 50  # wbgetentities accepts up to 50 ids per request


def _api_batch(qids: list[str], *, api: str = WIKIDATA_API,
               user_agent: str = USER_AGENT, retries: int = 4) -> dict:
    """One ``wbgetentities`` call for <=50 Q-IDs; returns parsed JSON.

    Retries 429/503/transport errors with exponential backoff.
    """
    params = urllib.parse.urlencode({
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "props": "sitelinks",
        "sitefilter": ENWIKI,
        "format": "json",
    })
    req = urllib.request.Request(f"{api}?{params}",
                                 headers={"User-Agent": user_agent})
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            last = e
            if (e.code == 429 or 500 <= e.code < 600) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    if last:
        raise last
    return {}


def _titles_from_response(data: dict, qids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    entities = data.get("entities", {}) or {}
    for q in qids:
        sitelinks = (entities.get(q, {}) or {}).get("sitelinks", {}) or {}
        title = (sitelinks.get(ENWIKI) or {}).get("title")
        if title:
            out[q] = title
    return out


def _load_tsv(path: str) -> dict[str, str]:
    """Load an offline ``Q-ID<TAB>Title`` map (for air-gapped builds)."""
    m: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0].startswith("Q") and parts[1]:
                m[parts[0]] = parts[1]
    return m


def resolve_qids(
    qids: Iterable[str],
    *,
    cache_path: Optional[str] = None,
    offline_map=None,
    user_agent: str = USER_AGENT,
    sleep: float = 0.1,
    progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, str]:
    """Return ``{qid: enwiki_title}`` for Q-IDs with an English sitelink.

    Q-IDs with no enwiki article are simply absent from the result.

    offline_map: path to a ``Q-ID<TAB>Title`` TSV, or a pre-loaded dict —
        resolves entirely offline, no network.
    cache_path: JSON file persisting resolutions across rebuilds. Both
        hits and known-misses (stored as "") are cached so repeated builds
        never re-query the same Q-ID.
    """
    want = sorted({q for q in qids if q and q.startswith("Q")})
    if not want:
        return {}

    if offline_map is not None:
        m = offline_map if isinstance(offline_map, dict) else _load_tsv(offline_map)
        return {q: m[q] for q in want if m.get(q)}

    cache: dict[str, str] = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except (ValueError, OSError):
            cache = {}

    todo = [q for q in want if q not in cache]

    def _flush():
        if not cache_path:
            return
        tmp = cache_path + ".part"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, cache_path)
        except OSError:
            pass

    # Resolve incrementally. A Europe-sized region is thousands of
    # wbgetentities calls; one 502 at batch N used to raise straight
    # through, the cache was never written, and the build carried on
    # with ZERO backfilled titles (about 60% of the linkable set) and no
    # gate to notice — and the next rebuild repeated every request.
    # Now: 5xx retried, the cache flushed every 50 batches and on any
    # failure, and a failure returns what was resolved so far, loudly.
    failed_at = None
    for n, i in enumerate(range(0, len(todo), BATCH)):
        batch = todo[i:i + BATCH]
        try:
            hits = _titles_from_response(_api_batch(batch, user_agent=user_agent), batch)
        except Exception as exc:  # noqa: BLE001 — network/API; keep what we have
            failed_at = (i, exc)
            _flush()
            break
        for q in batch:
            cache[q] = hits.get(q, "")  # "" == known to have no enwiki article
        if n % 50 == 49:
            _flush()
        if progress:
            progress(min(i + BATCH, len(todo)), len(todo))
        if sleep and i + BATCH < len(todo):
            time.sleep(sleep)

    if cache_path and todo:
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, cache_path)

    if failed_at is not None:
        i, exc = failed_at
        resolved = sum(1 for q in want if cache.get(q))
        print(f"    WARNING: Wikidata title resolution stopped at Q-ID {i}/{len(todo)} "
              f"({type(exc).__name__}: {exc}); continuing with the {resolved} titles "
              f"resolved so far — the rest will be retried on the next build",
              file=sys.stderr, flush=True)

    return {q: cache[q] for q in want if cache.get(q)}


def augment_wiki_cross_refs(
    wiki_cross_refs: Optional[dict],
    *,
    cache_path: Optional[str] = None,
    offline_map=None,
    log: Callable[[str], None] = print,
) -> dict:
    """In-place: fill `wikipedia` from `wikidata` on cross-ref entries.

    ``wiki_cross_refs`` is the ``extract_wiki_tags_pbf`` lookup:
    ``{key: {"wikipedia"?: "en:...", "wikidata"?: "Q..."}}``. For every
    entry that has ``wikidata`` but no ``wikipedia``, resolve the Q-ID and
    set::

        entry["wikipedia"]     = "en:" + Title_With_Underscores
        entry["wikipedia_src"] = "wd"        # provenance: derived, not OSM-tagged

    The downstream chunker then writes these into ``rec["w"]`` exactly as
    it does for OSM-tagged titles — so mcpzim links them with no change.

    Returns stats: ``{distinct_qids, resolved, entries_upgraded}``.
    """
    empty = {"distinct_qids": 0, "resolved": 0, "entries_upgraded": 0}
    if not wiki_cross_refs:
        return empty

    pending: dict[str, list] = {}
    for entry in wiki_cross_refs.values():
        if entry.get("wikipedia"):
            continue
        q = entry.get("wikidata")
        if q:
            pending.setdefault(q, []).append(entry)
    if not pending:
        return empty

    log(f"    wikidata->title: resolving {len(pending)} distinct Q-IDs"
        + (" (offline map)" if offline_map is not None else " via Wikidata API")
        + "...")
    titles = resolve_qids(pending.keys(), cache_path=cache_path,
                          offline_map=offline_map)

    upgraded = 0
    for q, entries in pending.items():
        title = titles.get(q)
        if not title:
            continue
        tag = "en:" + title.replace(" ", "_")
        for entry in entries:
            entry["wikipedia"] = tag
            entry["wikipedia_src"] = "wd"
            upgraded += 1

    stats = {"distinct_qids": len(pending), "resolved": len(titles),
             "entries_upgraded": upgraded}
    log(f"    wikidata->title: {stats['resolved']}/{stats['distinct_qids']} "
        f"Q-IDs resolved, {upgraded} cross-ref entries upgraded")
    return stats


# --------------------------------------------------------------------------
# Measurement CLI: reproduce the link-lift analysis on a built ZIM.
#   python -m cloud.wikidata_titles --measure path/to/osm-*.zim [--sample N]
# --------------------------------------------------------------------------
def _measure(zim_path: str, sample: int = 0, cache_path: Optional[str] = None) -> None:
    from libzim.reader import Archive  # lazy: only needed for measurement

    a = Archive(zim_path)

    def raw(p):
        return bytes(a.get_entry_by_path(p).get_item().content)

    manifest = json.loads(raw("search-data/manifest.json"))
    prefixes = sorted(manifest.get("chunks", {}).keys())

    distinct_q: set[str] = set()
    q_records = 0
    have_w = 0
    by_type: dict[str, int] = {}
    for pref in prefixes:
        try:
            b = raw(f"search-data/{pref}.json")
        except Exception:
            continue
        if b'"q":' not in b and b'"w":' not in b:
            continue
        for r in json.loads(b):
            if r.get("w"):
                have_w += 1
                continue
            q = r.get("q")
            if q:
                q_records += 1
                distinct_q.add(q)
                by_type[r.get("t", "?")] = by_type.get(r.get("t", "?"), 0) + 1

    qids = sorted(distinct_q)
    if sample and len(qids) > sample:
        stride = len(qids) / sample
        qids = [qids[int(i * stride)] for i in range(sample)]

    print(f"records already linkable by title (w):  {have_w:,}")
    print(f"POI-wikidata records w/o title (q):      {q_records:,}")
    print(f"  distinct POI Q-IDs:                    {len(distinct_q):,}")
    print(f"resolving {len(qids):,} Q-IDs...", flush=True)

    done = [0]

    def prog(d, t):
        if d - done[0] >= 1000 or d == t:
            done[0] = d
            print(f"    ... {d}/{t}", flush=True)

    titles = resolve_qids(qids, cache_path=cache_path, progress=prog)
    hit = len(titles) / max(1, len(qids))
    print(f"\nresolved (enwiki sitelink): {len(titles):,}  ({100*hit:.1f}% hit rate)")
    print(f"estimated linkable distinct articles added: ~{int(len(distinct_q)*hit):,}")
    print("\nPOI-q records by feature type:")
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"   {t:12s} {c:,}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Wikidata Q-ID -> enwiki title resolver")
    ap.add_argument("--measure", metavar="ZIM",
                    help="measure link-lift on a built streetzim ZIM")
    ap.add_argument("--sample", type=int, default=0,
                    help="resolve a strided sample of N distinct Q-IDs (0 = all)")
    ap.add_argument("--cache", help="JSON cache path for resolutions")
    args = ap.parse_args()
    if args.measure:
        _measure(args.measure, sample=args.sample, cache_path=args.cache)
    else:
        ap.error("nothing to do; pass --measure ZIM")
