# Setting up a new region

Quick recipe for adding a region that doesn't yet have an OSM PBF or MBTiles
under `world-data/regions/`. Mirrors what `build-region-fast.sh` expects so a fresh
region drops into the existing build pipeline without code changes.

## Prereqs

- `world-data/planet-2026-MM-DD.osm.pbf` (the canonical planet OSM dump)
- `world-data/world-tiles-v2.mbtiles` (the planet MBTiles, ~120 GB, indexed
  per-tile so bbox filtering is fast)
- `search_cache/world.jsonl` (global searchable feature dump)
- A Linux box with `osmium-tool` and the `venv-linux` Python env

## Critical: extract the regional PBF — do NOT symlink the planet PBF

`extract_qids_from_pbf` in `wikidata_cache.py` walks the **entire** PBF without
bbox filtering (`handler.apply_file(pbf_path)` — there's no bbox-aware variant
of pyosmium's `SimpleHandler` for this scan). With a planet symlink that means
9+ hours of HDD random-IO on the global PBF. With a real regional extract
(typically <5 GB) it finishes in ~5 minutes.

The build wrapper uses the path you provide as `${ID}.osm.pbf` for every
phase: address extract, overture merges (parquet only — no PBF read here),
wiki cross-ref extract, wikidata Q-ID scan, and routing extract's bbox
osmium-extract. Every PBF-touching phase pays the 91-GB-vs-3-GB cost on each
scan, so a single bad symlink multiplies into 10+ hours of waste across one
build, and that compounds for every region on the queue.

```sh
# 1. Extract the regional PBF — do this BEFORE launching build-region-fast.sh
cd /storage/streetzim
osmium extract \
    -b "$BBOX" \
    world-data/planet-2026-03-10.osm.pbf \
    -o world-data/regions/${ID}.osm.pbf \
    --overwrite
```

For multiple new regions queue them sequentially in one `bash -c` so they
share the planet PBF page cache:

```sh
setsid nohup bash -c '
for spec in "argentina:-73.5,-55.5,-53.5,-21.5" \
            "south-america:-82.0,-56.0,-32.0,13.5" \
            "indian-subcontinent:60.0,5.0,98.0,38.0"; do
  ID="${spec%%:*}"; BBOX="${spec##*:}"
  osmium extract -b "$BBOX" world-data/planet-2026-03-10.osm.pbf \
    -o "world-data/regions/${ID}.osm.pbf" --overwrite
done
' > extract-queue.log 2>&1 < /dev/null &
```

Each extract takes ~10–30 min depending on the region's bbox area.

## MBTiles + search-cache: symlinks are fine

Unlike the PBF, both of these are **already bbox-aware** at read time:

- `world-tiles-v2.mbtiles` is a SQLite file — `--bbox` causes
  `create_osm_zim.py` to query only the relevant tile rows.
- `world.jsonl` is bbox-filtered in a single linear scan in `[4/10] Building
  search index` — that's a sequential read, not a random-IO scan.

So symlinks save disk and don't hurt:

```sh
cd /storage/streetzim/world-data/regions/
ln -sf /storage/streetzim/world-data/world-tiles-v2.mbtiles ${ID}.mbtiles
ln -sf /storage/streetzim/search_cache/world.jsonl          ${ID}.search.jsonl
```

## Overture parquets

Use `download_overture_data.py` for both themes. Cache lives in
`overture_cache/`. Filename pattern is what `build-region-fast.sh` looks for
(with `OVERTURE_RELEASE` set to match):

```sh
venv-linux/bin/python3 download_overture_data.py addresses \
    --bbox="$BBOX" \
    --out overture_cache/addresses-${ID}-2026-04-15.0.parquet

venv-linux/bin/python3 download_overture_data.py places \
    --bbox="$BBOX" \
    --out overture_cache/places-${ID}-2026-04-15.0.parquet
```

addresses + places can run in parallel (DuckDB+S3, separate row groups). Some
regions have very sparse address coverage in Overture (e.g. India's addresses
parquet came back ~0 rows / 479 bytes — that's expected; the build will fall
back to OSM-derived addresses and still emit a valid ZIM, just without the
Overture address overlay).

## Bbox

Use Geofabrik bboxes as a starting point — they're usually a tight fit
around the country/region without ocean overhang. Sanity-check by counting
expected POIs in the parquet (`places` row count should be in the millions
for a continent, hundreds of thousands for a country). The bbox you pass to
the Overture downloader **must** be reused at build time — `build-region-fast.sh`
takes BBOX as its second arg.

## Build

Once the four inputs are in place, the canonical command is
**`build-region-fast.sh`** — the wrapper every shipped region is built with
(it is what `build-refresh-queue.sh` calls):

```sh
setsid nohup env OVERTURE_RELEASE=2026-08-19.0 WIKI_IMAGES=all \
    bash build-region-fast.sh "$ID" "$BBOX" "$DISPLAY_NAME" \
    > "${ID}-build.out" 2>&1 < /dev/null &
```

- `OVERTURE_RELEASE` **must** match the release in your parquet filenames. The
  wrapper defaults to `2026-04-15.0`; if that file is absent it silently drops
  `--overture-places` (`[ -f "$PLACES" ] && ARGS+=…`) and the build ships
  without Overture — the same failure that cost switzerland-light 748,654
  place records.
- `WIKI_IMAGES`: `lead` for continent-tier regions, `all` otherwise (the rule
  in `build-refresh-queue.sh`).
- If the region's `.mbtiles` is a symlink to the 107 GB world tile file, pass
  `STAGE_MBTILES_NVME=0`, or the wrapper copies the whole file onto the
  shared NVMe for every build.
- The wrapper exits **0 on "ALREADY EXISTS"** when today's output is present,
  and exits 0 even when its own `validate_zim` fails. Callers must check for a
  fresh output file and gate on `validate_zim` themselves.

### Do not use `build-region.sh`

It is an older wrapper missing 12 flags the production one passes — no
`--bundle-wiki-articles` / `--resolve-wikidata-titles`, no `--url-cache`,
the Python writer instead of `--zim-builder=rust`. On 2026-09-22 four
European country builds made with it (benelux, greece, carpathians, balkans,
3+ hours each) had **no Wikipedia layer at all**; the in-ZIM Kiwix gate
caught it. This page previously named it as the canonical command.

After the build, before `cloud/upload_validated.sh`, run the gates in
`.queue-europe-countries.sh`: `validate_zim`, the archive marker check
(viewer fixes, viewer slots, `wiki-geo-index.json`), overlap, the device
matrix, the **render gate** (`tmp/map-health.mjs`, ≥100 rendered features —
the only gate that fails a blank map; the device matrix and Kiwix gate both
pass one), and `cloud/kiwix_viewer_gate.sh`.
