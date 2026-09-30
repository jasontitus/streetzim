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

### Non-English `wikipedia=` tags (2026-09-30)

Outside English-speaking countries most OSM `wikipedia=` tags name an
article in the local language (`nl:Utrecht (stad)`). These used to be
kept as they were and looked up, title as-is, on English Wikipedia: in the
Netherlands full build 13,019 of 16,848 titles were missing. Such a tag is
now resolved through its `wikidata=` Q-ID like an untagged one: the
English sitelink replaces it (`wikipedia_osm` keeps the OSM tag).

Which tags are English (`is_english_title`): `en:` or no language prefix.
A language prefix is 2-3 letters before the first colon, in any case
(`NL:Foo` is Dutch, `EN:Foo` English), the rule the bundler
(`cloud/wiki_articles._strip_lang`), the geo-index (`zim_writer`) and the
viewer (`_wikiTagTitle`) use to turn a tag into a title; so "Foo: a bar"
reads as language `foo` everywhere, and is resolved through its Q-ID.
Longer codes (`simple:`, `nds-nl:`, `zh-yue:`, `be-tarask:`) are rare; they
are not English either (resolved through the Q-ID), but nothing strips
them, so one left unresolved is looked up whole and misses, as before.

When Wikidata answers that the item has no English article, the tag stays
and is flagged `wikipedia_no_en`. Its English namesake is usually absent,
and when present usually another subject; it is bundled only when English
Wikipedia has it as a **redirect to an article bundled for this map**
(`bundle_wiki_articles(redirect_only=...)`), written as a ZIM redirect
`wiki-article/<the redirect's title>` -> the bundled article (no second
copy, so no duplicate full-text hit; the geo-index lists it). A redirect
is an editor's alias (`Aalten (dorp)` -> `Aalten`, `De Bilt (dorp)` ->
`De Bilt`), but not always one for this place: `Pannenberg` redirects to
Wolfhart Pannenberg, a theologian, and `VVAC` to the Verde Valley
Archaeology Center. Requiring the target to be an article of another place
in the map (the municipality, the city) keeps the first kind. The target
is matched by the page each bundled title opens, so an article bundled
under an alias of its own (`AEGON` -> Aegon; 451 of the Netherlands' 3,829
bundled titles are such aliases) counts. Offline the source ZIM's redirect
entries tell; online, `action=query&redirects=1` for 50 titles a request,
no text (about 84 requests for the Netherlands' ~4,170 such titles, once:
each answer is cached as `<sha1>.redirect`; an article fetch records the
page it opened there too, and bundled titles fetched before that are looked
up the same way, only when a target is not matched directly).

The flag goes with the tag: the same tag on an object without a Q-ID is
bundled by the same rule, not as-is (the geo-index and the viewer go by
title, so its namesake would otherwise show for the flagged object too).

"Wikidata could not answer" (a 5xx, a stopped run, an offline map's gap)
is never "no English article": such a tag is looked up as before. Nor is an
id Wikidata refused on its own (cached as `#refused`, not asked again).

Administrative areas take their tags from their own boundary relation.
Those tags join the cross-ref lookup keyed by relation
(`admin_areas.add_admin_wiki_refs`, before titles are resolved), so they
are resolved and flagged like the rest and their articles are bundled for
them. They used to be read off the records at write time, unresolved, and
bundled only when a place node happened to carry the same tag: 96 Dutch
areas lost their article when non-English tags stopped being looked up as
they were.

Measured on 500 random Dutch-tagged places of the Netherlands map (with a
Q-ID; 7,076 of its 7,361 non-English links have one):

```
English article per Wikidata:            204  (41%; 203 in the 2026-02 enwiki ZIM,
                                               absent: "Spui (tram station)")
no English article:                      295  (59%)
old lookup (Dutch title in enwiki):      140 found: 116 the same article,
                                               11 a redirect to it,
                                               13 a different one (~5 a
                                               reasonable parent article)
no English article, namesake in enwiki:  10: 6 redirects (4 right, 2 wrong),
                                               4 articles of their own
```

So the English articles found go from ~127 to 204. The 59% with no English
article could only be covered by bundling the local-language Wikipedia (not
done); the redirect rule adds back the few English aliases.

## Resolution sources

1. **Wikidata Action API (default).** `wbgetentities?props=sitelinks&
   sitefilter=enwiki`, 50 ids/request, results cached to disk (hits *and*
   known-misses, so rebuilds never re-query). ~`distinct_q / 50` requests
   (≈178 for CA), sent serially with `maxlag=5` and a 0.1 s gap after each
   response, at most 120 a minute (Wikimedia asks for serial API requests
   rather than a fixed rate; the gap widens only on 429, maxlag or a 5xx
   with `Retry-After`), honouring `Retry-After`
   (`cloud/wikimedia_http.py`). A 429/5xx/maxlag/network failure caches
   nothing; a maxlag (replication lag) or ratelimited answer is retried
   until the step's wait budget (`STREETZIM_WIKI_WAIT_BUDGET`, 15 min) is
   spent, not five times: the two builds before this change stopped at
   Q-ID 0 on a lag episode. A 400/401/403/404/405/410 stops the run at
   once; a batch refused with an `error` body is halved until the bad id
   is found (wherever it sits in the batch), and that id alone is cached
   as `#refused`: not asked again, and not taken as having no article.
   Three ids in a row refused on their own, with nothing answered between
   them, mean the API refuses everything and stop the run. The build host resolves Q-IDs this way
   (`ops/build-region-fast.sh` passes no `--wikidata-title-map`): pacing
   costs about what the old fixed 0.1 s sleep did, and a title cache that
   holds malformed ids (from a batch an older build had refused) has its
   `""` entries dropped once and asked again, logged with the count.
   Public data only; the User-Agent names the project's public issue
   tracker (`STREETZIM_WIKI_CONTACT` adds an operator address at run time).
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

The flag is **off by default** in `create_osm_zim.py` so stock builds stay
hermetic/offline unless explicitly opted in. The `streetzim` command turns
it on, with `--bundle-wiki-articles`, when Wikipedia is on (`--profile full`
or `--wikipedia`; `profile_feature_args` in `streetzim/cli.py`).

## Web viewer (dual-use)

The enriched `w` field is the standard, shared OSM-Wikipedia field — not an
mcpzim-only path — so the in-ZIM web viewer surfaces it too, including
the titles backfilled here:

- **Find-page cards** (`places.html`) render a 📖 link to the public site,
  `https://<lang>.wikipedia.org/wiki/<Title>`, whenever a record has `w`.
- **Place detail panel** (`resources/viewer/index.html`, source
  `src/index/210-place-detail-sheet.js`) shows a 📖 **Wikipedia** button
  when the ZIM bundles Wikipedia articles (`--bundle-wiki-articles`,
  below) and a title is known, and opens it inside the ZIM.
  `_wikiArticlePath` (`src/index/100-wiki-bridge-and-viewport.js`) returns
  a path only when `wiki-geo-index.json` exists; the title is the record's
  `w`, else the geo index's Q-ID map, else the PWA's `wiki-qid-titles.json`
  (drive PWA only). It does not check that this particular article is in
  the ZIM. Most hosts (incl. kiwix-serve) **can't deep-link across ZIMs**, so
  there is no link to a separate Wikipedia ZIM (the old `wikipediaBase`
  setting is gone).

This keeps the design honest: one shared field, two consumers (mcpzim's
`articleByTitle` and the web viewer), zero parallel blobs.

## Bundling full articles offline (`--bundle-wiki-articles`)

Linking (above) needs a Wikipedia ZIM present. But **kiwix-serve can't
deep-link from one ZIM into another**, so for a truly self-contained
offline streetzim, `--bundle-wiki-articles` stores the article *in* the
ZIM at `wiki-article/<Title>` for every linkable title (the `w` set + any
`--resolve-wikidata-titles` backfill). mcpzim's `articleByTitle` resolves
that path, and its narration cleaner (`ArticleSections.stripHTML`) de-
noises it for Kokoro TTS (see mcpzim BundledArticleTests /
ArticleSpeechCleanupTests).

Each article is trimmed at build time to a compact reader page
(`cloud/wiki_articles.py:clean_article_html`): scripts/styles/tables/
infoboxes/figures/nav/reference-lists/edit-links and the IPA/coordinate
clutter removed, links unwrapped to text, attributes stripped, with a
CC BY-SA source-link footer. Real articles trim ~8× (Camarillo Ranch
House 98 KB → 12 KB; Nevada 630 KB → 67 KB). Measured size: ~0.2-1% of
the California ZIM for the linkable set.

Sources + caching:

- **Local Wikipedia ZIM** (`--wiki-articles-source <enwiki.zim>`) — offline,
  fast, no crawl. Use a FULL enwiki ZIM; a `top`/subset misses long-tail
  POIs.
- **Wikipedia API** (default) — cached to `--wiki-articles-cache`, else
  `wiki_articles_cache/` under `$STREETZIM_CACHE_DIR`, the checkout, or (an
  installed package) the user cache directory (`streetzim/paths.py`
  `cache_root`, like `wikidata_cache/`; gitignored). Hits (`<sha1>.html`) and
  definitive misses (`<sha1>.miss`: no such page, no text, with the reason)
  are cached, and so is whether a title with no English article of its own
  is a redirect (`<sha1>.redirect`), so **a rebuild never re-crawls**. Requests are serial with a
  0.1 s gap after each response, at most 120 a minute and 5 s after an
  answer over 1 s (`polite_pacer`), and honour `Retry-After`; a 429, 5xx,
  network failure, unexpected body, or a 400/404/410 without a
  `MediaWiki-API-Error: missingtitle` header is never cached. A 401/403/404,
  25 unanswered titles in a row, or a spent wait budget
  (`STREETZIM_WIKI_WAIT_BUDGET`, 15 min for this step) stops the requests; cached
  articles are still bundled. The build ends with a WARNING counting the
  unfetched articles (`STREETZIM_REQUIRE_WIKI=1` makes it fail). An empty
  `<sha1>.html` is the old miss marker, which a 429 could also leave; each
  is re-checked once, at most `STREETZIM_WIKI_RECHECK_MAX` (1000) per build.

```sh
# Offline (fast) — read articles from a local enwiki ZIM:
python create_osm_zim.py ... --resolve-wikidata-titles \
    --bundle-wiki-articles --wiki-articles-source ~/zim/wikipedia_en_all.zim

# Online (cached) — fetch + cache, so the next rebuild is free:
python create_osm_zim.py ... --resolve-wikidata-titles --bundle-wiki-articles
```

Off by default. Pairs with `--resolve-wikidata-titles` for the widest set,
but works alone (bundles just the OSM-tagged `w` titles).

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
  hook right after `extract_wiki_tags_pbf` (`streetzim/addresses.py`); the
  `wsrc` provenance field is written with the search records
  (`streetzim/zim_writer.py`).
- `tests/test_wikidata_titles.py` — unit tests (network mocked): batching,
  cache hit/miss persistence, offline map, and the augment contract.
