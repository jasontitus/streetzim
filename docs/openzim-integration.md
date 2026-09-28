# Bringing StreetZim features into openzim/maps

openZIM plans to keep [openzim/maps](https://github.com/openzim/maps)
(`maps2zim`) as its scraper and add StreetZim's features to it
([review](https://github.com/openzim/maps/blob/763a9bea611e2a64da90146636ce821c5e40d253/Streetzim%20vs%20Maps.md),
[our response](openzim-review-response.md)). This page is a concrete plan
for doing that. It is based on maps2zim at `707fc44` (2026-09) and StreetZim
at this commit. Everything here uses libzim / zimscraperlib and the standard
openZIM tools.

## The two pipelines side by side

| step | maps2zim | StreetZim |
|---|---|---|
| vector tiles | downloads OpenFreeMap's OpenMapTiles MBTiles (`planet` or `monaco`); cuts by `.poly` (shapely `TileFilter`) | tilemaker on a Geofabrik PBF, **or `--mbtiles` any OpenMapTiles MBTiles, OpenFreeMap included** |
| ZIM paths for tiles | `tiles/{z}/{x}/{y}.pbf`, identical tiles written once plus ZIM aliases | `tiles/{z}/{x}/{y}.pbf` (the same paths); empty tiles dropped |
| ZIM writer | zimscraperlib `Creator` (libzim) | python-libzim `Creator` (libzim) |
| search | Kiwix title index over GeoNames ADM1–4 `search/<label>` redirect pages; full-text off | in-map search over sharded JSON (`search-data/`) of every named place, POI, street, peak, park and water feature, plus Kiwix full-text over detail pages |
| categories | — | Find chips over `category-index/` |
| routing | — | graph from the PBF, loaded in cells by a Web Worker |
| viewer | Vite + ES modules, `content/config.json` | single-file viewer, `map-config.json` |
| QA | ruff, pyright strict, pytest, daily Monaco build + `zimcheck` | ruff (narrow), pytest + Node tests, Monaco build + validator + `zimcheck` + browser test through `kiwix-serve` |

## What we verified

StreetZim's search and chip pipeline runs **unchanged on the exact tiles
maps2zim downloads**. We fetched OpenFreeMap's Monaco MBTiles
(`scripts/fetch-openfreemap-mbtiles.py`, the same lookup as maps2zim's
`_fetch_mbtiles`) and built with
`create_osm_zim.py --mbtiles <ofm> --bbox … --split-find-chips`:

- **Search**: 1,397 searchable records (922 POIs, 458 streets, 11 places, and
  water, park and peak names).
- **Chips**: all 10 Find chips populated (food 172, shops 129, museums 88, …).
  The chip rules already match the OpenMapTiles `class`/`subclass` values
  OpenFreeMap uses.
- **Checks**: `cloud/validate_zim.py` with `zimcheck` 3.8 passes, and the
  in-ZIM viewer passes the headless-browser smoke test **through
  `kiwix-serve`**.

CI repeats this on every push (job `openfreemap-tiles`). So the data side of
search and categories needs no new data source in maps2zim, only code that
reads the MBTiles it already has.

## Options

1. **Port the code into maps2zim** (recommended). MIT code may be
   included in GPL-3.0 maps2zim; keep the MIT notice in each ported file.
   The ported code follows maps2zim's conventions: pydantic models, `Context`,
   zimscraperlib `add_item_for`, pyright strict, and tests in their tree. The
   shared contract is the data formats ([search-records.md](search-records.md),
   [formats.md](formats.md)), plus fixture tests on both sides.
2. **A small shared library** (for example `streetzim-core` on PyPI) with the
   pure modules. This makes sense only if routing ends up co-maintained;
   otherwise it gives openZIM a dependency it doesn't control.
3. **StreetZim as a post-processor** on a maps2zim ZIM. We advise against
   it: a ZIM can't be appended to, so this means a full repack, and it hides
   the features from Zimfarm.

## Proposed PR sequence for maps2zim

Each PR stands on its own, ships behind a flag where it adds content, and
keeps the Monaco daily build green. Efforts are rough, for someone familiar
with both codebases.

| # | PR | touches in maps2zim | source in StreetZim | effort |
|---|---|---|---|---|
| 1 | **Tile fetch retry + concurrency cap** (Kiwix service-worker drops) | new `zimui/zimtile.js`, registered with `maplibre.addProtocol`; `transformRequest` prefixes Tile/Glyphs URLs | `zimtile` protocol in `resources/viewer/index.html`; `docs/zim-packaging-gotchas.md` | 0.5–1 d |
| 2 | **Populated places in search** | `_parse_geonames`: accept `PPL`, `PPLA*`, `PPLC` with a zoom per code | — | 0.5–1 d |
| 3 | **Search records from the MBTiles** | new `search_records.py`; `Processor._write_search_data` after `_write_tilejson`; filter with `TileFilter`; new dep `mapbox-vector-tile` | `extract_searchable_features` (z14 decode, one point per feature, dedup), [search-records.md](search-records.md) | 2–3 d |
| 4 | **Search box in zimui** | `zimui/search.js`: normalize + prefix, fetch manifest and chunk, rank, fly to | the viewer's `search-shards` block; `tests/search_shards_js.test.mjs` pattern for JS/Python parity | 2–3 d |
| 5 | **Hot-prefix sharding** (continents) | port `search_shards.py` + reader | `cloud/search_shards.py` (stdlib only), `tests/test_search_shards.py` | 1–2 d |
| 6 | **Category chips / Find panel** | port `chip_rules.py`, emit `content/chip-rules.json` from `rules_as_json()` (no inline copies needed in maps2zim); port `chip_shards.py` (numpy) | `cloud/chip_rules.py`, `cloud/chip_shards.py`, their tests | 3–4 d |
| 7 | **PBF download + clip** (prerequisite for routing) | `--routing` derives the Geofabrik `-latest.osm.pbf` from the `.poly` URL, or takes `--osm-pbf-url`; clip with `osmium extract -p` | — | 1–2 d |
| 8 | **Routing graph** | `maps2zim/routing/`; emit `routing-data/*`, storing entries ≥ 200 MB uncompressed | `extract_routing_graph`, `tests/szrg_spatial.py`, `tests/szrg_reader.py`; spec in [formats.md](formats.md); differential tests `tests/szrg_astar.py`, `tests/test_route_identity.py` | 4–6 d |
| 9 | **Routing UI** | ES-module worker + directions panel; browser test through `kiwix-serve` | `resources/viewer/routing-worker.js`, `docs/routing.md` (includes the iOS memory limits) | 4–6 d |
| later | Wikidata (reliable only from the PBF: OpenFreeMap tiles carry no `wikidata`), terrain (GDAL, large) | | `wikidata_cache.py`, `generate_terrain_tiles` | 3–5 d each |

PRs 1–2 are small and independent. PRs 3–4 give maps2zim real search. PRs
5–6 scale it and add categories. Routing (7–9) is a project of its own.

Resource notes for Zimfarm:
- Search extraction over a regional cut is cheap.
- Routing needs the regional PBF and a few GB of RAM for a country; the US
  peaked around 12 GB building cells.
- Planet-scale routing is not a goal.

## What we are doing on the StreetZim side

Done:
- `cloud/chip_rules.py` `rules_as_json()`: the chip rules as JSON, for a
  build-time `chip-rules.json`. The viewer parity test uses it.
- [search-records.md](search-records.md): the record and manifest contract.
- [formats.md](formats.md): the routing formats, byte by byte.
- `scripts/fetch-openfreemap-mbtiles.py` and the `openfreemap-tiles` CI job,
  which prove the OpenFreeMap path.

Next, to make each port close to a copy:
1. Move `extract_searchable_features` and its workers out of
   `create_osm_zim.py` into an importable module, with `bbox`/`contains`,
   `tmp_dir` and logger parameters and no module-level side effects. GNU
   `sort` becomes optional.
2. Merge street records that repeat per tile (Monaco: 458 records, 295
   distinct names), and index native-script names alongside `name:latin`.
3. Move the search-data/category emitters out of `create_zim` into a
   function that returns `(path, bytes)` pairs. Move
   `_split_records_recursive` into `search_shards.py`.
4. Move the routing writer out of `tests/` into a `routing/` package.
5. Type-annotate the pure modules (`search_shards`, `chip_rules`,
   `chip_shards`, routing) and check them with pyright, since maps2zim runs
   pyright strict.
6. Publish a small fixture corpus (MBTiles in, records and shards out) that
   both repositories test against.

## Small things we noticed in maps2zim

We checked each of these against `707fc44`. We are happy to send PRs:

- `processor.py` replaces `<title>Vite App</title>`, but `zimui/index.html`
  now has `<title>Maps2ZIM</title>`, so the ZIM's page title is never set.
- `entrypoint.py` keeps only truthy argument values
  (`{k: v … if value}`), so `--max-zoom 0` and any future `--no-…` switch
  are silently ignored.
- `offliner-definition.json`: the `area` pattern `^planet|monaco$` parses
  as `(^planet)|(monaco$)`, and `geonames_region` isn't exposed to Zimfarm.
- `zimui/main.js` rejects `#…&zoom=` above 14 while the map allows 18.
