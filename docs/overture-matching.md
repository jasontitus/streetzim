# Conflating Overture Maps with OpenStreetMap (2026)

A reference note for anyone adding Overture-derived enrichment to the StreetZim
pipeline. Covers the `addresses` theme (alpha, 2026-04-15.0) and the `places`
theme (GA). Scope: how to merge Overture rows into our existing OSM-backed
`search_cache/world.jsonl` without inventing duplicates.

## 1. GERS vs. OSM IDs: the plan of record

Overture's Global Entity Reference System (GERS) issues 128-bit UUID v4
identifiers, stable across releases, for every feature. GERS is a deliberate
alternative to OSM `@id`s, not a wrapper around them: Overture was explicit
that OSM IDs are unsuitable as a cross-release FK because they churn whenever a
way is split, merged, or retagged. GERS IDs therefore do **not** encode any
OSM `@id` and never will — OSM provenance is always surfaced indirectly.

Two mechanisms give you the OSM back-pointer:

- **`sources[]` array on each row.** For OSM-derived rows,
  `sources[].dataset == "OpenStreetMap"` and `sources[].record_id` follows the
  `n<id>` / `w<id>` / `r<id>` convention (node/way/relation). This is the only
  per-row direct FK.
- **Bridge files.** Published alongside every Overture release at
  `docs.overturemaps.org/bridge-files/`, these are GERS-ID → source-ID lookup
  tables for the datasets Overture ingests. Use them for bulk joins rather
  than row-by-row iteration.

GERS guarantees ID stability for a real-world entity, not geometry stability —
a shop that moves 20 m across the street keeps its GERS ID even though its
`lon/lat` changes. Plan accordingly when caching.

## 2. Two-pass matching pattern

Every mature conflation toolchain — Hootenanny, `mapsme/osm_conflate`, the
JOSM Conflation plugin, HOT's `osm-merge` — uses the same shape:

1. **Pass 1 — deterministic ID join.** If `sources[].dataset == "OpenStreetMap"`
   on the Overture side matches an `n<id>` / `w<id>` / `r<id>` already in our
   `search_cache/world.jsonl`, merge attributes and stop. No spatial logic.
   Hootenanny calls this the "exact-match stage" of its conflation matrix; it
   runs before any geometry scoring.
2. **Pass 2 — probabilistic spatial + attribute match.** For every Overture
   row left unmatched, compute a weighted score over (distance, name
   similarity, category compatibility) and accept above a threshold.
   Hootenanny produces a 3-valued verdict (match / review / miss);
   `osm_conflate` emits a single best candidate per profile.

Community work on Overture specifically — Drew Breunig's DuckDB +
Ollama pipeline, Mikel Maron's vibe-coded OSM/Overture conflator, TomTom's
Transportation work — all follow the same two-pass shape. The "consensus
algorithm" is: deterministic ID join first, cascading fuzzy conditions second,
with rules ordered from strictest to loosest.

## 3. Concrete pass-2 thresholds

**Addresses** (point-to-point):

- Distance: **≤ 10 m** for urban, **≤ 25 m** for rural/unparsed. Overture's
  own buildings conflation uses **IoU ≥ 0.5** on polygons; addresses are
  points, so fall back to Euclidean after reprojection to a local metric CRS.
- House-number: **exact equality** after stripping whitespace and letter
  suffixes (`12A` → `12`, retain as tiebreaker).
- Street name: **Jaro-Winkler ≥ 0.89** after libpostal expansion (see §4).
  Breunig's experiment found 0.89 was the sweet spot for name matching; 0.83
  with an address-match conditional pushed recall from 55 % to 68 %.

**Places / POIs**:

- Distance: **≤ 50 m** default, tightened to **≤ 25 m** inside dense urban
  cells. Hootenanny's default POI search radius is **500 m** but that's
  intentionally loose for review-queue use; for unsupervised merge, keep it
  tight.
- Name similarity: **token-set ratio ≥ 85** (rapidfuzz) or Jaro-Winkler
  ≥ 0.89, same as addresses.
- Category compatibility: require Overture's `categories.primary` to map to a
  compatible OSM `amenity` / `shop` / `tourism` tag. Maintain an explicit
  crosswalk table; do **not** trust raw category-string equality.

## 4. Street-name normalization

Pure regex + a USPS abbreviation dict handles ~80 % of US cases ("St." →
"Street", "Ave" → "Avenue", "N" → "North"). For everything else, libpostal
earns its keep.

Recommended pipeline, in order:

1. `pypostal.expand.expand_address(s)` — returns a list of normalized
   variants. Intersect variant sets across the two sides and declare a match
   if the intersection is non-empty.
2. For US-only flows, `usaddress.tag()` to structure, then a USPS C1
   suffix-abbreviation dict to canonicalize `StreetNamePostType`. `datamade/
   usaddress` is production-quality; pair it with rapidfuzz on the
   `StreetName` component only.
3. Only compare fields of the same semantic type. Never run fuzzy matching on
   the raw full-address string — it conflates house-number and city noise
   with street-name signal.

Libpostal is worth the C dependency once you cross a national border:
international address variants (Straße vs. Str., ulitsa vs. ул.) are where
regex dies.

## 5. 1:many and many:1 matches

The textbook answer is the Hungarian algorithm
(`scipy.optimize.linear_sum_assignment`) on a cost matrix of
`1 - score(osm_i, overture_j)`, solved globally per tile or per H3 cell.
Papadakis et al. (VLDB 2023) showed Hungarian dominates greedy on Clean-Clean
ER benchmarks by 5–15 points of F1.

In practice, most Overture-community pipelines use **greedy-by-score**
(sort all candidate pairs descending, accept top pair, remove both rows,
repeat) because it's O(n log n) instead of O(n³) and the quality gap is
small when thresholds are strict. Hootenanny uses greedy inside a tile and
defers ties to a human review queue.

Recommended for us: greedy-by-score within a 200 m H3-r9 cell, falling back
to Hungarian only when a cell has > 20 candidates on either side. When
Overture has obvious duplicates (two Foursquare-sourced rows for the same
business), dedupe the Overture side **before** matching — the best-score-wins
rule is symmetric only when each side is already clean.

## 6. v1 implementation notes (what we actually shipped 2026-04-22)

`merge_overture_addresses()` and `merge_overture_places()` are now live
in `create_osm_zim.py`. v1 is intentionally simpler than the full
checklist below — practical defaults first, tighten later as real-world
failures surface:

- Pass 1 uses only `sources[].dataset == "OpenStreetMap"` as the
  deterministic hook. `record_id` back-lookup against our OSM index is
  a no-op today because `extract_addresses_pbf` doesn't persist OSM
  element IDs. Places merge Pass 1 therefore runs almost exclusively
  through Pass 2.
- Pass 2 uses a rounded-coord key `(round(lat, 5), round(lon, 5))` for
  addresses (≈1 m grid) and `(round(lat, 4), round(lon, 4))` for POIs
  (≈10 m grid) plus normalized-name equality. No libpostal — in-house
  `_normalize_street()` handles US suffix + direction abbreviations.
  Good enough for the US/EU rows we've tested; expect tuning as
  non-US regions land.
- 1:many resolution is greedy-first-hit inside each coord bucket. No
  Hungarian yet; the v1 buckets are small enough (often size 1).
- Category authority — for POIs, Overture's `categories.primary` wins
  over OMT's noisy `class` when the OSM `subtype` was a generic bucket
  (`tourism`, `amenity`, `shop`, `attraction`, `leisure`, …). This is
  the "categories matter" win: **museums tagged `tourism` in OMT now
  expose `subtype: "museum"` in the search feed**, and the
  `places.html` chips / pin-popup label treat `r.cat` as canonical.
- Enrichment fields land on existing OSM POIs without overwriting any
  field the OSM row already had (no regressions).
- Fresh Overture POIs get emitted with `source: "overture"` so mcpzim
  and the viewer can surface provenance.

**Search record extensions** (all optional, additive):

| field    | from                                 | typical size |
|----------|--------------------------------------|-------------:|
| `cat`    | `categories.primary`                 |  ~15 bytes   |
| `ws`     | `websites[0]`                        |  ~50 bytes   |
| `p`      | `phones[0]`                          |  ~15 bytes   |
| `soc`    | `socials[:3]` (array)                |  ~120 bytes  |
| `brand`  | `brand.names.primary`                |  ~20 bytes   |
| `wd`     | `brand.wikidata`                     |   ~8 bytes   |
| `source` | "overture" when Pass 2 added it      |  ~15 bytes   |

`ws` (not `w`) is deliberate: `w` is the Wikipedia tag key
(`"en:Article_Title"`) on OSM POIs elsewhere in the same record, and
the `w` / website collision silently corrupted mcpzim's article lookup
(`Place.wiki` would receive a URL, then `articleByTitle` would fail).

Silicon Valley (bbox `-122.6,37.2,-121.7,37.9`, 2026-04-22) results:
- 4,988 OSM POIs enriched
- 224,141 new POIs added from Overture
- JSONL: 222 MB → 275.8 MB (+53.8 MB raw)
- ZIM: 194.9 MB → 221.0 MB (+26.1 MB, +13.4%)
- Effective ZSTD ratio on the delta: ~2×

**Known v1 gaps (deferred):**

- `address_levels[]` from Overture isn't used to seed `location` —
  `build_location_index` still runs `reverse_geocoder` on the coord.
- `addresses[]` on place rows is ignored. When a place row is the only
  signal of a building's address, we miss a chance to backfill the
  addresses feed.
- Pass 1 for address rows is dormant since we don't keep OSM element
  IDs in the address pipeline; fixing that is a one-liner when we care.

## 7. Full-fat engineer checklist (for the next iteration)

1. Download Overture `addresses` + `places` theme for the target bbox.
2. Reproject both sides to a local metric CRS (UTM zone or Web Mercator for
   rough work).
3. **Pass 1:** iterate Overture rows, scan `sources[]` for
   `dataset == "OpenStreetMap"`, look up `record_id` in the OSM index keyed
   by `n|w|r<id>`. Merge on hit; mark source row consumed.
4. Build an R-tree / H3 spatial index of the remaining OSM rows keyed by
   `(lat, lon)` from `search_cache/world.jsonl`.
5. Normalize street names on both sides via libpostal `expand_address` +
   USPS dict.
6. **Pass 2:** for each unconsumed Overture row, pull candidates within 10 m
   (address) or 50 m (place), score by weighted sum
   (0.5·name + 0.3·distance + 0.2·category), keep those above threshold.
7. Resolve 1:many with greedy-by-score inside each H3-r9 cell; escalate to
   Hungarian when cell candidate count > 20.
8. For matched rows, enrich our schema — populate `name` / `type` / `subtype`
   / `location` from Overture where the OSM row is missing the field. Never
   overwrite a non-empty OSM value; log the conflict instead.
9. For unmatched Overture rows (no OSM partner), emit as new records with
   `type="overture_only"` and keep the GERS ID as `id`.
10. Write GERS ID and OSM `@id` back onto the merged record so re-runs are
    idempotent.
11. Emit a match-stats report (pass-1 %, pass-2 %, unmatched Overture,
    unmatched OSM) and fail the build if pass-2 recall drops > 10 % from the
    previous release — a canary for thresholds drifting.
12. Cache the result keyed by `(overture_release, osm_pbf_date)` so the
    expensive pass-2 doesn't rerun on every incremental build.

## References

- Overture GERS concept — https://docs.overturemaps.org/gers/
- Using the GERS system (blog) — https://docs.overturemaps.org/blog/2025/06/25/getting-started-gers/
- Overture 2026-04-15.0 release notes — https://docs.overturemaps.org/blog/2026/04/15/release-notes/
- Overture Addresses guide — https://docs.overturemaps.org/guides/addresses/
- Overture Places guide — https://docs.overturemaps.org/guides/places/
- Hootenanny — https://github.com/ngageoint/hootenanny
- mapsme `osm_conflate` — https://github.com/mapsme/osm_conflate
- OSM Conflation wiki — https://wiki.openstreetmap.org/wiki/Conflation
- Drew Breunig, "Conflating Overture POIs with DuckDB, Ollama, and More" — https://www.dbreunig.com/2024/09/27/conflating-overture-points-of-interests-with-duckdb-ollama-and-more.html
- libpostal — https://github.com/openvenues/libpostal ; pypostal — https://github.com/openvenues/pypostal
- datamade `usaddress` — https://github.com/datamade/usaddress
- Papadakis et al., "Bipartite Graph Matching Algorithms for Clean-Clean Entity Resolution" (EDBT 2022) — http://disi.unitn.it/~pavel/OM/articles/Papadakis_EDBT22.pdf
- `scipy.optimize.linear_sum_assignment` — https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.linear_sum_assignment.html
