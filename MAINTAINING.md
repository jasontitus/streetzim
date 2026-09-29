# Maintaining StreetZim

For whoever picks this up next. [README.md](README.md) covers building a ZIM;
this file covers how the repository is organised, how to change it safely,
how production releases are made, and what is known to be wrong.
[docs/openzim-review-response.md](docs/openzim-review-response.md) records
how the 2026-09 openZIM review of this codebase was addressed, and
[docs/openzim-integration.md](docs/openzim-integration.md) is the plan for
bringing StreetZim features into openzim/maps.
[docs/adoption-plan.md](docs/adoption-plan.md) tracks what it would take for
openZIM to adopt StreetZim directly.

## 1. What is core and what is operations

The repository mixes three kinds of code. Only the first is needed to build a
ZIM.

**Core: builds a ZIM from OSM data.** Portable, tested, run by CI.

- `create_osm_zim.py`: the builder's CLI (`build_parser()`, and `main()`
  calling one function per build phase). The steps it
  runs live in the `streetzim/` package: `common.py` (the phase-timing `print`,
  `PHASE_TIMER`, repo paths, URLs, `download_file`, `parse_bbox`), `tiles.py`
  (tilemaker, MBTiles readers, fonts, MapLibre), `satellite.py`, `terrain.py`,
  `addresses.py` (PBF addresses, Overture merges, wiki tags),
  `search_extract.py`, `routing/build.py` (graph extraction + chunking) and
  `zim_writer.py` (`create_zim`). `create_osm_zim.py` re-exports every name
  those modules define, so `import create_osm_zim` callers keep working. At
  import time it also needs `cloud/viewer_slots.py` and
  `cloud/search_shards.py`; later it lazily imports `cloud/{manifest_writer,
  wiki_articles, chip_shards, chip_rules, wikidata_titles}.py`,
  `wikidata_cache.py`, and `streetzim.routing` (graph formats, spatial cells,
  reference routers). `tests/szrg_*.py` are aliases of those modules, kept so
  old imports work.
- `resources/viewer/`: the viewer baked into every ZIM. `resources/tilemaker/`: the tile profile.
- `cloud/validate_zim.py`: the release gate. `cloud/repackage_zim.py` / `cloud/patch_viewer_inplace.py` (libzim): rewrite published ZIMs. (The accelerator variant, `swap_viewer_rust.py`, is in `ops/cloud/`.)
- **The ZIM writer is libzim** (python-libzim), which is the default. `zimcheck`
  and `kiwix-serve` are the reference checker and reader. CI uses only these.

**Optional accelerators.** Only the continent-scale production builds use
them; see §3. `rust/streetzim-pack` (`--zim-builder rust`) and
`--xapian builder` are faster replacements for libzim's writer and indexer.
They depend on the author's `zimru` and `xapianbuilder` checkouts placed next
to this repo. They make the same ZIM, faster. You can ignore them unless you
are building continents.

**Operations: the author's hosting.** Works only on the production host and
with the author's accounts. It lives in [`ops/`](ops/README.md), with a
symlink at each old path the host runs, so the host is unaffected; `web/`,
`preview-proxy/` and the files the host edits in place are still at their
old paths ([`ops/in-place.txt`](ops/in-place.txt)) and move with the second
stage, a separate repository. `tools/check_boundary.py` (CI) keeps the
builder from depending on any of it.

- Build host layout: everything runs from `/storage/streetzim` (a checkout
  with `world-data/`, caches, `venv-linux/`, `wiki-src/`). Most wrappers `cd`
  there and call each other by absolute path.
- archive.org items `streetzim-<id>` (collection `opensource_media`), the
  Firebase project `streetzim` (`web/`, `.firebaserc`), a Cloudflare Worker
  (`preview-proxy/`), and an older GCS bucket and GCP VMs (`cloud/*vm*`,
  `cloud/upload-caches.sh`).

**One-offs.** Dated scripts, queue lists (`*.list`, `viewer-refresh.tsv`),
`ops/tmp/`, `ops/STATUS-*.md`. They record what was done. Most can be retired; see
[ops/docs/scripts.md](ops/docs/scripts.md) for the full inventory and plan.

## 2. Day-to-day development

```bash
ruff check .                       # syntax errors + pyflakes (ruff.toml)
python tools/pyright_gate.py       # type check: strict modules clean, no new findings
python tools/offliner_definition.py --check   # Zimfarm definition matches the flags
python -m pytest tests -q          # ~30 s; tests needing big local ZIMs skip themselves
for t in tests/chip_rules_js.test.mjs tests/chip_shards_js.test.mjs \
         tests/search_shards_js.test.mjs tests/zim_reader_js.test.mjs \
         tests/test_zim_http_source.mjs; do node "$t"; done
```

CI (`.github/workflows/ci.yml`) runs all of that, then builds Monaco end to
end, validates it (including `zimcheck`) and loads the in-ZIM viewer in
headless Chrome. It also runs weekly, to catch upstream drift.

**Python versions.** The builder runs on 3.12 (the production host) and 3.14
(the Docker image and openZIM's scrapers). On 3.14 `requirements.txt` also
installs zimscraperlib, and the `streetzim` command uses it
(`streetzim/scraperlib.py`); on 3.12 `streetzim/zim_metadata.py` applies its
own copy of the same metadata rules. CI runs the unit tests on both, and
`tests/test_scraperlib_parity.py` fails on 3.14 if the copy and zimscraperlib
disagree. Keep the builder itself 3.12-compatible until the production host
moves.

Rules that keep published ZIMs working:

- **Formats are frozen.** A change to any binary or JSON layout gets a new
  version number, a reader that still accepts the old one, and an update to
  [docs/formats.md](docs/formats.md). The `/drive/` PWA serves the *current*
  viewer to *every* ZIM a user opens, so readers can't drop old versions
  until no published ZIM needs them (the retirement table is in formats.md).
- **Viewer edits.** `index.html` is built from the parts in
  `resources/viewer/src/index/` (head, styles, markup, then one file per
  feature: search, find, wiki, the routing formats, A*, the worker bridge,
  driving mode…). Edit a part, then run `python tools/build_viewer.py`. CI
  fails if `index.html` and the parts differ. `places.html` and
  `routing-worker.js` are edited directly. `npm run lint:viewer` runs
  ESLint over all three (CI does too). MapLibre is vendored and the font
  glyphs are pinned by hash; bumping either is in
  [docs/viewer-supply-chain.md](docs/viewer-supply-chain.md). Then run
  `scripts/sync-drive-viewer.sh` to refresh the PWA copy in
  `web/drive/viewer/` (the tests compare the shared blocks). Published ZIMs
  get viewer updates by in-place slot patching (`docs/viewer-slots.md`,
  `ops/docs/viewer-rollout.md`). That can replace `index.html`, `places.html` and
  `routing-worker.js`, but **cannot add a new file to a ZIM**. So a viewer
  change must not depend on a new ZIM entry unless it degrades gracefully
  when the entry is missing. Keep each file inside its slot (1 MiB / 256 KiB /
  128 KiB).
- **Find chips.** `cloud/chip_rules.py` is the source of truth. The viewer
  keeps two inline copies, and `tests/chip_rules_js.test.mjs` fails if either
  drifts.
- **Search sharding.** `cloud/search_shards.py` and the viewer's
  `search-shards` block are covered the same way by
  `tests/search_shards_js.test.mjs`.
- **Routing changes.** Run the route-identity suite
  (`tests/run_identity_suite.sh`, `tests/test_route_identity.py`) against
  real ZIMs; see `docs/routing.md`.

### Environment variables

| variable | effect |
|---|---|
| `STREETZIM_REQUIRE_SHAPEFILES=1` | fail the build if the coastline / Natural Earth shapefiles are missing (otherwise a warning) |
| `STREETZIM_REQUIRE_ZIMCHECK=1` | `validate_zim.py` fails when `zimcheck` is not installed (otherwise skipped) |
| `STREETZIM_SKIP_ZIMCHECK=1` | skip zimcheck in the validator |
| `ZIMRU_ZIMCHECK` | optional: a faster drop-in `zimcheck` for very large ZIMs; the standard `zimcheck` is used otherwise |
| `STREETZIM_NODE_LOC_DIR` | fast scratch volume for the routing node-location store (default `/data`, falling back to the output directory) |
| `STREETZIM_PACK_BIN`, `XAPIANBUILDER_BIN` | optional accelerators only (see §1) |
| `ZSTD_CLEVEL` | ZIM compression level (production uses 22) |
| `STREETZIM_MERGE_STREETS=0` | keep one search record per tile for streets instead of merging the pieces (docs/search-records.md). Merging is the default since merge #19, so regions built before it have more street records |
| `STREETZIM_ALLOW_FONT_ERRORS=1` | ship even if some font ranges failed to download (e.g. during a CDN outage); by default the build stops after 5 attempts per range. A range whose bytes do not match its pinned sha256 always stops the build ([docs/viewer-supply-chain.md](docs/viewer-supply-chain.md)) |
| `PYTHON` | interpreter the Node tests shell out to |

## 3. How production releases are made

On the build host, from `/storage/streetzim`. The details are in
`ops/docs/remote-rebuild.md`, `ops/docs/new-region-setup.md` and
`ops/docs/rebuild-2026-09-plan.md`; this is the map.

1. **Inputs.** A planet PBF (`download-planet.sh`), regional extracts
   (`extract-region-pbfs.sh`), world vector tiles (`build-world-tiles.sh`,
   tilemaker in Docker), regional MBTiles and search JSONL, Overture parquets
   (`download_overture_data.py`), a local English Wikipedia ZIM, plus the
   terrain, satellite and Wikidata caches.
2. **Registry.** `cloud/regions.tsv` has one row per region (id, name, bbox,
   tier, smoke-test points). `cloud/region-variants.tsv` defines derived
   variants such as `switzerland-light` (no satellite, z13).
3. **Build one region.** `build-region-fast.sh <id> <bbox> <name>` is the
   canonical wrapper. It builds with in-build spatial routing cells,
   Wikipedia articles and Overture, and no LLM bundle. On this host it also
   turns on the two optional accelerators (§1) for speed. The same
   `create_osm_zim.py` command without `--zim-builder rust --xapian builder`
   produces an equivalent ZIM with plain libzim.
4. **Gate and ship.** `ship-region.sh <id>` builds, then runs its gates
   (terrain, `validate_zim.py`, route checks, search + Find smoke tests,
   `cloud/pwa_smoke_test.mjs`), then uploads with
   `cloud/upload_validated.sh`, **the only upload path**. The Kiwix UI gate
   `cloud/kiwix_viewer_gate.sh` is run by the viewer-rollout scripts.
   `build-refresh-queue.sh` does the same for the whole registry.
5. **Viewer-only updates** go out with `cloud/rollout_viewer_patch.sh`
   (in-place slot patch, gates, upload). There is no rebuild.
6. **Catalogue and torrents.** `web/generate.py` produces `streetzim.web.app`,
   and `cloud/deploy_pwa.sh` runs a gated Firebase deploy.
   `cloud/generate_all_torrents.py` builds the torrents.

Handover checklist for the accounts: archive.org (`ia` credentials; items
`streetzim-*`), Firebase project `streetzim`, the Cloudflare account that owns
the `streetzim-preview-proxy` worker, the GCP project `streetzim`, and this
GitHub repository.

## 4. Known debt

In rough priority order. The items marked **bug** were found during the
2026-09 maintainability review and are not yet fixed, because they touch
scripts that may be running on the production host (see
[ops/docs/scripts.md](ops/docs/scripts.md) for how to change those safely).

- **Script sprawl.** 42 live shell scripts (73 before Phase 1 moved the dead
  ones to `attic/`); about 23 are needed. The remaining phases are in
  ops/docs/scripts.md.
- **Shared gate code is copy-pasted, or `sed`-extracted at runtime from
  `retrofit-chips-queue.sh`** by six other scripts. It should become a sourced
  `scripts/lib/`; ops/docs/scripts.md gives the order that avoids breaking the
  extractors.
- **bug:** `build-refresh-queue.sh` and `ship-region.sh` upload without the
  `.retrofit-upload.lock` every other uploader takes. `build-refresh-queue.sh`
  also records `upload_validated.sh` exit 6 ("listing pending") as a failure.
- **bug:** `ship-region.sh` picks a random port in 8810–8889, which overlaps
  ports fixed elsewhere (8811, 8821, 8831/8832, …).
- **bug:** `ship-region.sh` does not understand variants
  (`ship-region.sh switzerland-light` exits looking for
  `regions/switzerland-light.osm.pbf`).
- **bug:** `tmp/upload-recorder.sh` watches `retrofit-chips-queue.log`, but the
  queue writes `retrofit-chips.log`.
- **trap:** `cloud/regions.tsv` still has `switzerland-nosat*` rows with no
  matching `region-variants.tsv` rows. A full registry run would build them as
  full satellite z14 ZIMs under "light" names.
- **Large units.** `create_osm_zim.py`'s `main()` and `create_zim` are now
  sequences of phase functions (`build_parser` plus `_openzim_options` ...
  `_print_summary` in `create_osm_zim.py`; the phases in
  `streetzim/zim_writer.py`), none of them over ~290 lines. The phases pass
  state as keyword arguments and tuple returns, so a new option usually
  means a parameter on the phase that reads it. The largest functions left
  are `extract_routing_graph` (`streetzim/routing/build.py`, ~650 lines) and
  the published-ZIM tools `repackage` and `swap_viewer_rust` in `cloud/`.
  `index.html` is edited as parts in `resources/viewer/src/index/`.
  `places.html` (2.5k) and `routing-worker.js` (1.7k) are unsplit.
- **Legacy format readers.** The builder writes only current formats
  (SZRG v4, SZCI v3 + SZRC v2); the v5 writer and the SZCI v2 upgrader are
  retired. The readers for SZRG v2/v3/v5, SZCI v1/v2 and SZRC v1 stay while
  published ZIMs carry them; formats.md lists every branch to delete and
  when.
- **`streetzim-meta.json` `routingGraph.version`** reports the intermediate
  SZRG version even when the ZIM ships SZCI v3 cells. `map-config.json` has no
  routing-format field.
- **Production leans on the optional accelerators.** `build-region-fast.sh`
  and `retrofit-chips-queue.sh` (via `cloud/swap_viewer_rust.py`) use
  them. Both have libzim equivalents: the default writer, and
  `cloud/repackage_zim.py` / `cloud/patch_viewer_inplace.py`. CI covers
  those equivalents. A maintainer without the accelerators can run the same
  wrappers without the two flags. Only the largest regions get slower.
- **Docs that point at files that don't exist:** `docs/mcpzim-contract.md`
  and `docs/STREETZIM_CONSUMPTION.md` are cited from `create_osm_zim.py`.
- **Satellite licence.** The EOX 2021 layer is CC BY-NC-SA and ships in most
  published ZIMs; see the README's licence section.
