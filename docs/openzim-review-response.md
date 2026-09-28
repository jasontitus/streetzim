# Response to "openzim/maps vs. streetzim"

The openZIM team compared the two tools in
[`Streetzim vs Maps.md`](https://github.com/openzim/maps/blob/763a9bea611e2a64da90146636ce821c5e40d253/Streetzim%20vs%20Maps.md),
reviewing streetzim at `abdb891`. It recommends that openZIM keep
openzim/maps as its scraper and port streetzim's features into it one at a
time ("Option A").

**We agree with that recommendation.** Nothing below argues for Option B.
This note covers three things:

1. what we changed in streetzim in response to each weakness the review names;
2. a few places where the review's reading of the code needs a correction;
3. how we can help with the port.

All changes are on the branch that adds this file. Every claim here was
checked against the code, and the end-to-end claims were checked against a
real build.

## 1. The review's weaknesses, one by one

| review point | status | what changed |
|---|---|---|
| **No CI in the repo** | fixed | `.github/workflows/ci.yml` runs on every push and weekly. It runs a lint gate, the pytest suite (about 290 tests) and the Node viewer tests. A second job builds a **real Monaco ZIM** from Geofabrik, using tilemaker v3 and the production routing layout, then runs `cloud/validate_zim.py` together with **`zimcheck` 3.8**. Last, it loads the viewer in headless Chrome, **served straight out of the ZIM**, and checks that the map renders, the chip rail works and the Find page returns places. This is the same shape as openzim/maps' daily Monaco + `zimcheck` job. |
| **Reproducibility: hardcoded local paths** | fixed for the core, documented for ops | The ZIM builder and the modules it imports had no machine-specific paths, apart from test harness defaults. Those now honour `$PYTHON` and fall back to `python3`. `validate_zim.py`'s optional Rust zimcheck path is now overridable. The machine paths that remain (`/storage/streetzim`, `/Users/...`) are in the author's operations scripts. [MAINTAINING.md](../MAINTAINING.md) §1 separates core from operations. |
| **Reproducibility: undocumented prerequisites** | fixed | Building from a clean checkout turned up four problems:<br>• The README's `apt install tilemaker` gives 2.4, which cannot run our Lua profile.<br>• Nothing downloaded the coastline and Natural Earth shapefiles, and without them tilemaker silently produces a ZIM with no ocean.<br>• Ubuntu's `zimcheck` 3.2 cannot read ZIMs written by current libzim.<br>• The in-ZIM browser smoke test depended on a server script that had never been committed.<br>All four are fixed: the README has build steps, `scripts/fetch-shapefiles.sh` fetches the shapefiles, the builder warns on missing shapefiles (`STREETZIM_REQUIRE_SHAPEFILES=1` makes it fatal), `cloud/serve_zim_entries.py` is now committed, and the README pins a zimcheck version. CI runs exactly the README steps. |
| **Stale docs** | fixed | The README was rewritten. The claims about no coastline, placeholder fonts, pre-built ZIMs in the repo and the patched-libzim step are gone. The licence table now includes Overture, Natural Earth and GLO-90, and Wikipedia is correctly CC BY-SA 4.0 (the viewer attribution was fixed too). A byte-level format spec, [docs/formats.md](formats.md), replaces a stale SZRG v3 description. |
| **Category chip rules duplicated in three places** | fixed | See the correction in §2. The viewer's two copies are now marked blocks, and `tests/chip_rules_js.test.mjs` checks them field by field against `cloud/chip_rules.py`. It also checks that the client-side filter and the Python matcher agree on a shared corpus, so any drift now fails CI. Writing this test also turned up a real bug: `repackage_zim --split-find-chips` dropped the pre-merge "restaurants"/"cafes" chips, which could leave a retrofitted region with no food chip. That is fixed, with a regression test. |
| **Custom binary formats, several versions** | documented and frozen, retirement planned | [docs/formats.md](formats.md) specifies SZRG v2–v5, SZGM, SZCI v1–v3, SZRC v1–v2, the chunk manifest and every ZIM path, all derived from the writer code. It has a version-support table saying which reader branches can be removed and when. The main rule is that a layout change means a new version plus a reader that still accepts the old one. That rule matters because the `/drive/` PWA serves the current viewer to every ZIM a user opens. |
| **Code shape: monolithic builder and viewer** | not split yet, on purpose | Splitting an 8k-line builder or an 11k-line viewer with no end-to-end test would have been reckless. The new Monaco CI job is that test. MAINTAINING.md names the seams for a split: the viewer's BEGIN/END blocks, and moving the routing writer out of `tests/`. It also names the constraint: published ZIMs are patched in place and cannot gain new files, so the viewer must still ship as the same three files. |
| **Many one-off scripts (`tmp/`, queues, rollouts)** | inventoried, reduction planned | [docs/scripts.md](scripts.md) classifies all 72 shell scripts. About 23 are live; the rest are retired Mac/AWS/GCP tooling or dated one-offs. It gives a phased plan to get down to those ~23 plus a shared gate library. The plan is careful because the production host runs these scripts by absolute path, some of them concurrently. The first retirement batch is waiting on the maintainer's sign-off. |
| **Heavy builds** | documented | This is inherent to continent-scale routing and search. City builds run on a laptop in minutes (CI builds Monaco on a standard runner); the README and MAINTAINING.md say where the large-memory paths are. |
| **Outside openZIM scope (hosting)** | agreed, separated | This tooling is streetzim's own distribution (archive.org, torrents, the PWA) and is not part of building a ZIM. MAINTAINING.md separates it and lists the accounts a new maintainer would need. |
| **Satellite layer is non-commercial** | agreed, made prominent | `--satellite` was always opt-in in the builder. The README now states plainly that the production wrappers enable it, so most published StreetZim ZIMs contain CC BY-NC-SA imagery. `cloud/region-variants.tsv` already builds satellite-free variants. We agree with the review's item 10: don't port it with the 2021 EOX layer. |
| **Depends on personal forks (`zimru`, `xapianbuilder`)** | verified optional | The default path is stock libzim end to end: building (python-libzim), validating (`zimcheck` 3.8), serving (`kiwix-serve` 3.8.2), and the tools that rewrite published ZIMs (`repackage_zim.py`, `patch_viewer_inplace.py`). CI runs all of these on every push. `tests/test_libzim_contract.py` pins the libzim behaviour that in-place viewer patching relies on (`Hint.COMPRESS=False` stores bytes verbatim; the trailing MD5). This work turned up and fixed one libzim-path bug: `repackage_zim.py` re-added every entry as non-front, so libzim wrote no title index and Kiwix lost its search suggestion. zimru is needed only for `--zim-builder rust`, `--xapian builder` and `cloud/swap_viewer_rust.py`, which exist for speed and memory on continent-scale builds. |
| **Bus factor** | improved | MAINTAINING.md covers the development rules, the release flow, environment variables, the account handover and a prioritised debt list. CI makes the tree safe for someone other than the author to change. |

## 2. Corrections to the review

These don't change its recommendation. They matter to whoever does the port.

- **Chip rules.** The build side already had one source of truth,
  `cloud/chip_rules.py`: `create_osm_zim.py` and `repackage_zim.py` import it.
  The duplication was in the viewer: `places.html` `CATEGORIES` (the full
  rules, needed for ZIMs built without per-chip files) and `index.html`
  `EXPLORE_CHIPS` (ids and labels). We kept those as inline literals rather
  than a JSON file the viewer fetches, because in-place viewer updates cannot
  add a new entry to a published ZIM. For openzim/maps, which has no such
  constraint, the review's suggestion of a build-time JSON is the better
  design.
- **"The viewer baked into each ZIM must understand whichever version was
  shipped."** That is true in Kiwix. But the `/drive/` PWA serves the
  *current* viewer to *any* ZIM, so its reader must understand every version
  still in circulation. That is why formats.md has a retirement table instead
  of dropping legacy readers.

## 3. Helping with Option A

The review's roadmap maps onto streetzim code as follows. All of it is
MIT-licensed, so it can be reused in GPL-3.0 openzim/maps with attribution.

| roadmap item | where to look in streetzim |
|---|---|
| 3. Retry/concurrency wrapper for tile fetches | the `zimtile://` protocol in `resources/viewer/index.html` (`maplibregl.addProtocol('zimtile', …)`) and the Kiwix quirks it works around in `docs/zim-packaging-gotchas.md` |
| 4. POI/street search from vector tiles, sharded | `extract_searchable_features` in `create_osm_zim.py`, `cloud/search_shards.py` plus the viewer's `search-shards` block (with `tests/search_shards_js.test.mjs`), and `docs/search-prefix-locality.md` |
| 5. Category chips | `cloud/chip_rules.py` (export `CHIP_RULES` as the build-time JSON), `cloud/chip_shards.py` for geographic sharding of large categories, and `docs/find-chip-shards.md` |
| 7. Terrain | `generate_terrain_tiles` in `create_osm_zim.py` (Copernicus GLO-30 with GLO-90 fallback, terrain-RGB WebP) and `docs/elevation-compression-analysis.md` |
| 8. Routing | `extract_routing_graph` (graph builder), `tests/szrg_spatial.py` (cell writer), `resources/viewer/routing-worker.js`, the byte spec in `docs/formats.md`, the algorithm and iOS memory notes in `docs/routing.md`, and the reference router plus differential tests (`tests/szrg_astar.py`, `tests/test_route_identity.py`) |
| 9. Wikidata / Wikipedia | `wikidata_cache.py`, `cloud/wikidata_titles.py` and `cloud/wiki_articles.py`, which already reads articles from a local Wikipedia ZIM, i.e. the cross-ZIM idea in the review |

We're happy to review ports, especially routing and search sharding, and to
upstream the Kiwix reader findings in `docs/zim-packaging-gotchas.md` to the
reader projects, as §6.4 of the review suggests.
