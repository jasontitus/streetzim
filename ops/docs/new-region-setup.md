# Setting up a new region

Quick recipe for adding a region that doesn't yet have an OSM PBF or MBTiles
under `world-data/regions/`. Mirrors what `build-region-fast.sh` expects so a fresh
region drops into the existing build pipeline without code changes.

## Prereqs

- `world-data/planet-2026-MM-DD.osm.pbf` (the canonical planet OSM dump;
  this round's is `planet-2026-08-31.osm.pbf`)
- `world-data/world-tiles-v3.mbtiles` (the planet MBTiles built from that
  planet, 107 GiB, indexed per-tile so bbox filtering is fast)
- `search_cache/world-2026-08-31.jsonl` (global searchable feature dump,
  extracted from those tiles)
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
    world-data/planet-2026-08-31.osm.pbf \
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
  osmium extract -b "$BBOX" world-data/planet-2026-08-31.osm.pbf \
    -o "world-data/regions/${ID}.osm.pbf" --overwrite
done
' > extract-queue.log 2>&1 < /dev/null &
```

Each extract takes ~10–30 min depending on the region's bbox area. For
regions that have a row in `cloud/regions.tsv`, `./extract-region-pbfs.sh
--only <id>` does the same in one planet pass and writes the `.bbox`
sidecar (below).

### Regions across the antimeridian (alaska)

A region that crosses ±180° is written in `cloud/regions.tsv` with
minlon > maxlon: alaska is `172.0,51.0,-130.0,72.0` (the Aleutians west to
Attu, 172.4E, through Anchorage). `osmium extract -b` cannot take such a
box, so cut it as one box each side of 180 with a two-ring .poly:

```sh
./venv-linux/bin/python3 -m streetzim.area poly "$BBOX" "world-data/regions/${ID}.poly"
osmium extract -p "world-data/regions/${ID}.poly" world-data/planet-2026-08-31.osm.pbf \
    -o "world-data/regions/${ID}.osm.pbf" --overwrite
```

The queue does this by itself: `build-refresh-queue.sh`,
`cloud/rebuild_old_regions.sh` (a .poly next to the PBF) and
`extract-region-pbfs.sh` (a two-part `multipolygon` in its osmium config)
switch only for such a row; every other row runs the same `-b` command as
before. `derive-region-mbtiles.py`, `derive-region-search.py`,
`download_overture_data.py` and `cloud/check_terrain_coverage.py` take both
sides too, and `create_osm_zim.py` accepts the bbox as written
(docs/formats.md, "Areas across the antimeridian"). The legacy cloud-VM
scripts (`cloud/build_region.sh`, `cloud/preflight.py`,
`cloud/verify_terrain_freshness.py`, `cloud/fix_terrain_seams.py`,
`verify_tile_cache.py`) still assume minlon < maxlon; do not run them on
such a region.

Never hand such a bbox to `osmium extract -b`: osmium 1.16 accepts
minlon > maxlon, exits 0, and extracts the complement (the band round the
other side of the world). `ops/region-bbox.sh` (`bbox_osmium_area`) is the
one place that builds the extract arguments; if the .poly cannot be
written the row fails.

**Bbox sidecars.** Region files are named by id, not bbox, so each producer
writes `<file>.bbox` with the registry bbox it was cut for (the queue,
`rebuild_old_regions.sh`, `extract-region-pbfs.sh`, `ship-region.sh` for
PBFs and Overture parquets; `derive-region-{mbtiles,search}.py` for the
slices). A file whose sidecar names another bbox is stale: re-extracted,
re-downloaded, or (a slice) parked as `.stale-<date>` in favour of the world
file. A file without a sidecar is trusted for a normal row and gets one;
for a row across the antimeridian it is treated as stale with a loud
`WARNING … no .bbox sidecar` in the log. After changing a row's bbox, run
the one-time steps in [alaska-antimeridian-runbook.md](alaska-antimeridian-runbook.md)
(written for alaska; the same four commands with another id):

```sh
./extract-region-pbfs.sh --only alaska --force
rm -f overture_cache/addresses-alaska-*.parquet overture_cache/places-alaska-*.parquet
./derive-region-mbtiles.py --only alaska --src "$WORLD_MBTILES"
./derive-region-search.py  --only alaska --src "$WORLD_SEARCH"
```

## MBTiles + search-cache: symlinks work, slices are faster

Unlike the PBF, both of these are **already bbox-aware** at read time:

- `world-tiles-v3.mbtiles` is a SQLite file — `--bbox` causes
  `create_osm_zim.py` to query only the relevant tile rows.
- `world-2026-08-31.jsonl` is bbox-filtered in a single linear scan in `[4/10] Building
  search index` — that's a sequential read, not a random-IO scan.

So symlinks to the world files give a correct build and save disk:

```sh
cd /storage/streetzim/world-data/regions/
ln -sf /storage/streetzim/world-data/world-tiles-v3.mbtiles   ${ID}.mbtiles
ln -sf /storage/streetzim/search_cache/world-2026-08-31.jsonl ${ID}.search.jsonl
```

They are slow on the HDD, though: the tile-add phase reads the mbtiles
randomly, which against the world file caps at ~120 tiles/s, against
1,600+/s for a regional slice the page cache holds.
`derive-region-mbtiles.py` and `derive-region-search.py` cut those
per-region slices in one sequential scan each; see
[alaska-antimeridian-runbook.md](alaska-antimeridian-runbook.md) step 4.

## Overture parquets

Use `download_overture_data.py` for both themes. Cache lives in
`overture_cache/`. Filename pattern is what `build-region-fast.sh` looks for
(with `OVERTURE_RELEASE` set to match). Use one release for both files. To
stay on the current round's release (what the ops wrappers default to), set
`REL=2026-08-19.0`. For the newest complete release instead (the
downloader's own default, `--release latest`: STAC's "latest", walking back
past a release still being uploaded; it moves when Overture publishes),
resolve it once:

```sh
REL=$(venv-linux/bin/python3 download_overture_data.py addresses places --print-release)

venv-linux/bin/python3 download_overture_data.py addresses \
    --bbox="$BBOX" --release "$REL" --transport s3 \
    --out overture_cache/addresses-${ID}-${REL}.parquet

venv-linux/bin/python3 download_overture_data.py places \
    --bbox="$BBOX" --release "$REL" --transport s3 \
    --out overture_cache/places-${ID}-${REL}.parquet
```

`--transport s3` is what the host has always used and what the ops wrappers
pass by default (`--transport "${OVERTURE_TRANSPORT:-s3}"`). The downloader's
own default is `https`: it lists the files anonymously and reads them over
HTTPS (only those whose STAC bbox meets yours), which also works behind a
proxy that breaks s3:// signing, but is untested on the host; set
`OVERTURE_TRANSPORT=https` for a wrapper to opt in. Release resolution
(`--print-release`) uses the STAC catalog either way.

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
(it is what `build-refresh-queue.sh`, `ship-region.sh` and
`cloud/rebuild_old_regions.sh` call):

```sh
setsid nohup env OVERTURE_RELEASE="$REL" WIKI_IMAGES=all \
    bash build-region-fast.sh "$ID" "$BBOX" "$DISPLAY_NAME" \
    > "${ID}-build.out" 2>&1 < /dev/null &
```

- `OVERTURE_RELEASE` **must** match the release in your parquet filenames, and
  `build-region-fast.sh` refuses to start without it (it used to default to
  `2026-04-15.0`). If the named file is absent it silently drops
  `--overture-places` (`[ -f "$PLACES" ] && ARGS+=…`) and the build ships
  without Overture — the same failure that cost switzerland-light 748,654
  place records.
- The wrappers (`build-refresh-queue.sh`, `ship-region.sh`,
  `cloud/build_region.sh`) default to the pinned round release, 2026-08-19.0;
  `OVERTURE_RELEASE=latest` opts in to the newest complete release, resolved
  once at start. The queue records its release in `queue-refresh.release`,
  and `--continue` refuses a different one unless `OVERTURE_RELEASE` names a
  release explicitly.
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

After the build, gate the ZIM before `cloud/upload_validated.sh`. The three
drivers do not run the same gates:

- `./ship-region.sh <id>` and `build-refresh-queue.sh` run terrain coverage
  (`cloud/check_terrain_coverage.py`), `validate_zim`, live routing
  (`cloud/route_cli.py`, A*), search + the Find chip record count, and the
  browser smoke (`cloud/pwa_smoke_test.mjs`; the queue treats a browser
  failure as soft by default). They upload only if the gates pass.
- `cloud/rebuild_old_regions.sh` runs `validate_zim` (inline, after the
  build), its `markers()` check (viewer fixes, viewer slots,
  `wiki-geo-index.json`) and its `gate()`: overlap, the device matrix
  (layout and chip tap only), the **render gate** (`tmp/map-health.mjs`,
  ≥100 rendered features — the only gate that fails a blank map; the
  device matrix and Kiwix gate both pass one) and
  `cloud/kiwix_viewer_gate.sh` (which runs a search). It does **not** run
  terrain coverage, live routing or the Find record count.

For a hand build of a region with a row in `cloud/regions.tsv`, run
`./ship-region.sh <id> --no-upload`: it builds and runs the first list's
gates without uploading.
