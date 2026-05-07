# Setting up a new region

Quick recipe for adding a region that doesn't yet have an OSM PBF or MBTiles
under `world-data/regions/`. Mirrors what `build-region.sh` expects so a fresh
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

`build-region.sh` uses the path you provide as `${ID}.osm.pbf` for every
phase: address extract, overture merges (parquet only — no PBF read here),
wiki cross-ref extract, wikidata Q-ID scan, and routing extract's bbox
osmium-extract. Every PBF-touching phase pays the 91-GB-vs-3-GB cost on each
scan, so a single bad symlink multiplies into 10+ hours of waste across one
build, and that compounds for every region on the queue.

```sh
# 1. Extract the regional PBF — do this BEFORE launching build-region.sh
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
`overture_cache/`. Filename pattern is what `build-region.sh` looks for:

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
the Overture downloader **must** be reused at build time — `build-region.sh`
takes BBOX as its second arg.

## Build

Once the four inputs are in place, the canonical command is:

```sh
setsid nohup bash build-region.sh "$ID" "$BBOX" "$DISPLAY_NAME" \
    > "${ID}-build.out" 2>&1 < /dev/null &
```

The wrapper handles: create_osm_zim → mv → repackage (spatial cells layout +
chip split) → validate. After the wrapper exits, smoke-test routing/search/
find on the resulting ZIM (per the in-house policy) before invoking
`cloud/upload_validated.sh`.
