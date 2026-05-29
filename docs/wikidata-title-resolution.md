# Wikidata → Wikipedia-title resolution (cross-ZIM linking)

## Problem

streetzim search-index records carry two Wikipedia cross-ref fields:

| field | source | example | mcpzim use |
| --- | --- | --- | --- |
| `w` | OSM `wikipedia=` tag | `en:Lincoln_Memorial` | `ZimService.articleByTitle` links it to a Wikipedia ZIM **by title** |
| `q` | OSM `wikidata=` tag | `Q162458` | carried, but **not resolvable** to an article without a Q-ID→title map |

mcpzim links a POI to the full article only when `w` is present. A Q-ID
alone can't be turned into an article — there's no Q-ID→title map on the
record, in the streetzim, or in a stock Wikipedia ZIM. So every
`wikidata`-only feature is dark to the cross-ref path.

### Measured gap (`osm-california-2026-05-09.zim`)

Full scan of all 78,955,300 search-data records:

```
records with a usable wikipedia title (w):     7,538   -> 2,260 distinct articles
records with wikidata-only (no w):           126,795
  ├─ POI wikidata (q), no title:              42,772   -> 8,863 distinct Q-IDs
  └─ brand-only wikidata (wd):                84,023   ->   628 distinct Q-IDs
```

So ~94% of wiki-signal records carry only a Q-ID, and only **2,260
distinct articles** are reachable today.

## Approach

At build time, resolve each distinct POI Q-ID (`q`) to its English
Wikipedia title via its Wikidata **sitelink**, and fill `w` from it:

```
entry["wikipedia"]     = "en:" + Title_With_Underscores
entry["wikipedia_src"] = "wd"        # provenance: derived, not OSM-tagged
```

The chunker already writes `entry["wikipedia"]` into `rec["w"]`, so mcpzim
links these with **zero app-side change**. A Wikidata enwiki sitelink is,
by construction, the exact title of a live English Wikipedia article — so
this honours mcpzim's "exact-match only, no fuzzy name matching" contract
(`NearPlacesWikiEnrichmentTests`), unlike name-based guessing.

The new `wsrc:"wd"` field on the record records provenance so consumers
(and eval) can tell OSM-tagged links from wikidata-derived ones.

### Why fill `w` (vs. a new field or an in-ZIM Q-index)

- mcpzim already resolves `w` titles via an exact-path `read()` (with a
  redirect/`searchTitles` fallback). Filling `w` needs **no mcpzim change**
  — existing app builds benefit immediately.
- Per-record cost is tiny: one short title string on the resolved subset
  (~3 k distinct titles on CA), negligible in a multi-GB ZIM.
- Alternative considered — ship a `wikidata-index/` Q-ID→title file in the
  ZIM and add a Q-ID fallback in `articleByTitle`. Rejected for v1: more
  moving parts on both sides for no extra reach (the WP ZIM is keyed by
  title anyway). Kept as a future option if title bloat ever matters at
  planet scale.

## Measured lift

Resolving all 8,863 distinct POI Q-IDs against the Wikidata API:

```
resolved (have an enwiki sitelink):  3,199   (36.1%)
no enwiki article:                   5,664   (63.9%)
```

- **Directly-linkable distinct articles: 2,260 → ~5,459 (~2.4×).**
- Upgrades a large share of the 42,772 POI-`q` records.
- Validation: a strided sample of the resolved titles was checked against
  live `en.wikipedia.org` — **50/50 are real articles**. (Resolution is
  correct by construction; the check just confirms titling/encoding.)
- The 64% misses are genuine: Wikidata items for minor streets, creeks,
  and peaks (often GNIS/geonames imports) that have **no** English
  Wikipedia article — there is nothing to link to.

Brand wikidata (`wd`, 84,023 records / 628 distinct chains) is **out of
scope for v1**: it resolves to the brand's article ("Starbucks"), not the
specific place, so it's a different feature (a `--resolve-brand-wikidata`
opt-in could add it later — high record coverage, ~hundreds of articles).

## Resolution sources

1. **Wikidata Action API (default).** `wbgetentities?props=sitelinks&
   sitefilter=enwiki`, 50 ids/request, results cached to disk (hits *and*
   known-misses, so rebuilds never re-query). ~`distinct_q / 50` requests
   (≈178 for CA). Public data only; User-Agent identifies the project by
   its public repo URL — no personal contact info.
2. **Offline `Q-ID<TAB>Title` map (air-gapped builds).** Pass
   `--wikidata-title-map`. Build one from the enwiki `page` +
   `page_props` (`pp_propname='wikibase_item'`) SQL dumps, or a tool like
   `wikimapper`. The build never touches the network in this mode.

## Usage

```sh
# Online (cached across rebuilds):
python create_osm_zim.py ... --resolve-wikidata-titles \
    --wikidata-title-cache build/wd_titles.json

# Offline:
python create_osm_zim.py ... --resolve-wikidata-titles \
    --wikidata-title-map data/qid_enwiki_titles.tsv

# Measure the lift on an already-built ZIM (no rebuild):
python -m cloud.wikidata_titles --measure osm-california-2026-05-09.zim \
    --cache build/wd_titles.json
```

The flag is **off by default** so stock builds stay hermetic/offline
unless explicitly opted in.

## Edge cases

- **No enwiki sitelink** → left as wikidata-only; behaviour unchanged.
- **Redirects / title drift** → mcpzim's `searchTitles` fallback catches
  most; a stale title degrades to a clean "not found", never a wrong hit.
- **API errors / rate limiting** → retry with backoff; unresolved Q-IDs
  stay wikidata-only (partial resolution degrades gracefully).
- **Build hermeticity** → API mode adds a network dependency; use
  `--wikidata-title-map` for reproducible/air-gapped builds.

## Implementation

- `cloud/wikidata_titles.py` — `resolve_qids()`, `augment_wiki_cross_refs()`,
  and a `--measure` CLI that reproduces the lift analysis on any ZIM.
- `create_osm_zim.py` — the `--resolve-wikidata-titles` flag + a one-call
  hook right after `extract_wiki_tags_pbf`, plus the `wsrc` provenance
  field on emitted records.
- `tests/test_wikidata_titles.py` — unit tests (network mocked): batching,
  cache hit/miss persistence, offline map, and the augment contract.
