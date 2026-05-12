# Remote-box rebuild runbook

For the 128 GB / 14 TB remote box (Europe-located, with the full
satellite tile cache + world build currently in progress, which means
planet PBF + world MBTiles + terrain cache are already there).

**Default path on this box: FULL REBUILD.** A reroll preserves any
trailing data issues from the source ZIM (the 12 broken terrain
tiles in California's source were caught by the validator only
because the audit got tighter today; older shipped regions might
have missed similar bugs). Starting from the planet PBF guarantees
no inherited regressions and produces fresh OSM data + fresh
Overture enrichment.

Use REROLL only as an exception:
- When you need a quick fix (e.g. a viewer-only bug) and a full
  rebuild can't fit in the schedule.
- When the source ZIM is known-good and you only need new viewer
  HTML / search-link rewrite / chip-sub-bucketing.

The reroll commands are at the END of this doc. The full-rebuild
flow is the body.

---

## URGENT — Hawaii map opens on empty ocean (2026-05-12)

User feedback: opening `osm-hawaii-*.zim` in Kiwix Desktop "shows no
map." Root cause: Hawaii's bbox `[-178.5, 18.5, -154.5, 28.5]` includes
the uninhabited NW Hawaiian Islands, so the geometric centroid is
`(-166.5, 23.5)` — mid-Pacific between Midway and Kauai. At z6 the
user sees only open ocean. The vector tiles, satellite, and terrain
are all present (728 K tiles, validated); the bug is purely the
initial-view default in `map-config.json`.

### Fix (build flag)

`create_osm_zim.py` now accepts `--map-center "LON,LAT"` and
`--map-zoom Z` overrides. For Hawaii, build with:

    --map-center "-157.5,20.7" --map-zoom 7

That centers the initial view between Oahu and the Big Island, with
all main islands visible at z7. NWHI atolls remain in the ZIM and pan
into view if the user moves the map west.

### Action on the remote box

Full rebuild of Hawaii with the override above. The same data caches
apply — only the map-config.json output changes.

`cloud/repackage_zim.py` also got the same flag pair, but the python
repackage path can't write libzim's X namespace, so the rebuilt xapian
ends up empty (the in-viewer Find still works via search-data/*.json,
but Kiwix Desktop's top-bar search returns no hits). A full rebuild
via `create_osm_zim.py` is the only way to get both the map-center fix
**and** a working fulltext index.

---

## URGENT — chip-sort regression baked into every shipped ZIM (2026-05-11)

Commit `db560e1` fixes a Find-page chip-sort bug in
`resources/viewer/places.html`. **The PWA at streetzim.web.app/drive/
is already fixed** (Firebase served the new viewer; SW refresh delivers
it on next page load). **Kiwix-native readers — Kiwix Desktop, iOS,
Android, anything that opens a `.zim` directly — see the buggy viewer
baked into every ZIM uploaded before this commit.** Every region's
chip-sort can produce wildly wrong "first result" distances when the
user pins an origin via the Near input.

### What the bug does

`places.html` auto-ticks "Limit to map area" on first render when it
finds a stashed viewport from `index.html`. `pickNearResult` then sets
`state.origin` to the typed city but **doesn't update the viewport
filter**. `applyResults` filters records to the (stale) viewport
BEFORE sort-by-distance — so typing "Cancún" + clicking the Gas chip
on CAC produced "Pemex 941 km in the Cayman Islands" as the nearest
gas station, because the default viewport excluded every Mexican
record. The auto-fallback ("if filter yields zero, expand") doesn't
fire when the viewport has *some* matches.

Verified shapes:

| Region | First Gas result before fix | After fix |
|---|---|---|
| central-america-caribbean | 941 km from Cancún | **26 m** (Pemex) |
| iran                      | 103 km from Tehran | **1 m** |
| silicon-valley            | 8.4 km from SF     | **71 m** |
| washington-dc             | 15 m from Washington | 15 m (unchanged — origin already on top) |

**Workaround for Kiwix users on existing ZIMs**: untick "Limit to map
area" before clicking the chip, or pan the map to the typed city
before searching. Other Find paths (typing a query, GPS-pinned origin,
direct chip without Near) are unaffected.

### ZIMs needing rebuild

All 18 ZIMs uploaded this rebuild cycle are pre-fix:

**Locally rebuilt today (8 — `osm-*-2026-05-10.zim` or `-2026-05-11.zim`):**
washington-dc, silicon-valley, colorado, baltics, hispaniola, iran,
texas, central-america-caribbean

**Remote-box rebuilt this cycle (10 — `osm-*-2026-05-11.zim`):**
russia (in progress, no ZIM up yet), iceland, korea-mongolia, caucasus,
turkey, south-korea, hawaii, nyc-metro, chicago-metro, greater-la

Plus every pre-existing region not rebuilt this cycle (~20 more) also
has the buggy viewer baked in, but those are old enough that they'll
get cleaned up in the natural rebuild cadence.

### Action on the remote box

The remote box should rebuild all 18 fresh-this-cycle regions to clear
the regression for Kiwix-native users. The build pipeline already pulls
the latest `resources/viewer/places.html` into every ZIM (no special
flag needed — just `git pull` before each build).

The build host can use a reroll path (preserves OSM+Overture data,
re-emits the viewer asset) instead of a full rebuild to save time —
the bug is purely in the JS, no upstream data needs to change. See
"Path A — REROLL" at the bottom of this doc for the syntax; passing
`--rebuild-viewer-only` (if implemented) or just rerunning the chunked
copy-with-new-resources flow is faster than a full PBF parse.

If reroll isn't viable for some regions, full rebuilds in any order
also fix it — just longer.

### Side effect on the smoke harness

Commit `51ee80d` bumped `NEAR_THRESHOLD_KM` from 50 → 250 km, with the
comment "URL-liveness filter is dropping POIs near major cities". That
reasoning was wrong — the URL filter wasn't the cause; the
viewport-filter bug was. Now that the real bug is fixed, the threshold
could safely return to 50 km for any future regression to surface
immediately. Left at 250 for now; revisit when the smoke is next
touched.

---

## Pending changes to apply before the next rebuild

These are repo-wide changes that should land in the same PR as the
next regional rebuild — they're not in `main` yet because the PWA
viewer can't ship them without rebuilt chip files (see each entry
for migration notes).

- **Merge Restaurants + Cafés into a single Food & Drink chip.**
  See [`docs/in-zim-apps.md` § "Queued for next rebuild"](in-zim-apps.md#queued-for-next-rebuild--merge-restaurants--cafés).
  Touches `cloud/chip_rules.py`, `resources/viewer/places.html`,
  `resources/viewer/index.html`, `cloud/validate_zim.py`. Also fixes
  the `ice_cream` / `ice_cream_parlor` cross-bucket bug.
- **Recursive search-data split: bucket by next-character, not hash.**
  Hot prefixes (e.g. `sa`) get fanned into 256 FNV-hash-bucketed
  sub-chunks (`sa-0-0`…`sa-f-f`). The viewer can't pre-filter by them,
  so typing "san" downloads ~384 MB of JSON and crashes iOS Safari.
  Switch to a char-prefix split (`sa-n` for "san…", `sa-c` for
  "sacramento…") so the viewer can target one chunk per typed
  character. Touches `_split_records_recursive` in
  `cloud/repackage_zim.py` and the in-build copy in
  `create_osm_zim.py`. Existing viewer mitigation
  (`streamFilterChunks` in `resources/viewer/index.html`) avoids the
  OOM crash but is slow until the build emits char-bucketed chunks.
- **Smaller routing cells: `--spatial-chunk-scale 10` (0.1° cells).**
  Today California ships with `--spatial-chunk-scale 1` → 113 cells,
  each ~50 MB on disk. A *single* cold cell-fetch on iPhone over the
  SW range-read into a 3.3 GB ZIM blob takes ~1.7 s — measured on a
  1 km route where 0 cells were missed but the in-flight prewarm
  fetch took the full 1.7 s before A* could proceed.
  Switching to scale 10 produces ~11 k tiny ~500 KB cells per region.
  Single-cell short routes drop from ~1.7 s to ~150-300 ms cold;
  long routes stay roughly even (more cells but each fetches faster
  and parallelism caps at 6). Touches the `--spatial-chunk-scale`
  CLI default in `cloud/build_region.sh` and `docs/remote-rebuild.md`'s
  Path B canonical command (already documents `=10` in the wrapper
  script at the bottom of the doc; just promote it to the primary
  build flow). NB: the cells-index header grows roughly linearly in
  cell count; we've shipped scale-10 builds before (Europe), so this
  is well-tested.
- **Street addresses on POI search records.**
  POI records today carry `{n, t, s, l, ws, p, soc, cat}` (name,
  type, subtype, location-label, website, phone, socials, category)
  but no street address. The OSM tags `addr:housenumber` and
  `addr:street` are dropped at extraction. Surface them as a new
  field (suggest `f` for "full address" — short JSON keys keep the
  per-region search-data size from ballooning) on POI records during
  extraction. Then carousel cards and the place-detail bottom-sheet
  in `resources/viewer/index.html` can render the address line below
  the existing category/location subhead. Touches `create_osm_zim.py`
  POI emission (and possibly the Overture merge path that overwrites
  some fields). When the field is present in records, the viewer can
  start rendering it without further coordination — old viewers just
  ignore the unrecognised field.
- **Filter records against the URL liveness cache.**
  Many Overture POI websites are dead — DNS failures, 4xx/5xx,
  redirects to domain-squatter parking pages (sedo / hugedomains /
  godaddy parked / etc.). Field reports of broken links + parking
  pages prompted us to add `cloud/validate_overture_urls.py` (the
  async crawler) and `cloud/url_cache_filter.py` (build-side
  consumer).

  **Cache status (2026-05-10):** the first full crawl is complete.
  1,292,172 URLs sampled from
  `osm-california-2026-05-09.zim` — 833,060 alive (64.5%), 459,112
  dead (35.5%). The 333 MiB `url_validation_cache.json` is on GCS
  at `gs://streetzim-cache/url_validation_cache.json`. The build
  VM already pulls it at startup
  (`cloud/build-vm-startup.sh`, optional — absent file does not
  fail the build).

  Build-side wiring is still TODO:
    1. ~~Run `cloud/validate_overture_urls.py` to populate
       `url_validation_cache.json`.~~ (DONE, see above.)
    2. In `create_osm_zim.py`'s POI emission step, import
       `from cloud.url_cache_filter import load_cache,
       decide_record_action, scrub_record_url` and consult the
       cache per record. Default policy: `drop-record` for POIs
       whose website is dead (per user: "a dead website may likely
       mean a dead business so we might want to drop it
       entirely"). Records without a `ws` field are always kept.
    3. The cache is reused across builds; only URLs older than
       `--max-age-days` (default 30) get rechecked, so subsequent
       runs are cheap. New URLs from a fresh Overture parquet
       still need a first crawl pass.
    4. To refresh the cache mid-rebuild cycle: locally re-run
       `cloud/validate_overture_urls.py --zim <newest-region>.zim`,
       then `bash cloud/upload_url_cache.sh` to push to GCS.

## Prereqs (one-time)

```sh
# 1. Clone repo
git clone https://github.com/jasontitus/streetzim.git
cd streetzim

# 2. Python venv. The build needs python 3.12 + the patched libzim
#    (vanilla pip install libzim won't include the streetzim
#    patches — see patches/README.md if a fresh build is needed).
python3.12 -m venv venv312
source venv312/bin/activate
pip install -r requirements.txt
# IMPORTANT: drop the patched libzim in. From the local Mac:
#   tar czf /tmp/libzim-patched.tgz \
#     ~/experiments/python-libzim/libzim
# scp it to the remote, then:
#   tar xzf /tmp/libzim-patched.tgz -C ~/experiments/

# 3. Internet Archive CLI auth (only needed for upload).
ia configure
# enter your archive.org email + password.

# 4. Verify zimru is built (the validator uses it for big regions):
ls ~/experiments/zimru/target/release/zimcheck
# If absent:
git clone https://github.com/jasontitus/zimru.git ~/experiments/zimru
(cd ~/experiments/zimru && cargo build --release)
```

## Path A (DE-EMPHASIZED) — REROLL a shipped archive.org ZIM

Use this only as an exception when a fresh full rebuild can't fit
the schedule. A reroll keeps any bugs latent in the source ZIM.

```sh
# Region IDs match the streetzim-{id} archive item names.
# Big-storage regions go here (45 GB europe, 20 GB united-states,
# 17 GB africa). Run from the repo root with venv activated.

ID=europe                                           # change per run
SRC="osm-${ID}-source.zim"
# Filename to fetch — pick the latest dated .zim from
# https://archive.org/details/streetzim-${ID}/
ARCH_FILE=osm-europe-2026-04-17.zim                 # update per region

# 1. Download (one-time per session; the script keeps the file).
curl -fL "https://archive.org/download/streetzim-${ID}/${ARCH_FILE}" \
    -o "$SRC"

# 2. Roll with full mobile-safety flags. Most are auto-detected by
#    the reroll wrapper; passing them explicitly is fine and
#    idempotent. --refresh-terrain-tiles requires a populated
#    terrain_cache/ — if missing, omit; the validator will catch
#    edge-stripe regressions later.
TODAY="$(date +%Y-%m-%d)c"
OUT="osm-${ID}-${TODAY}.zim"
TMP="osm-${ID}-${TODAY}-reroll.zim"

./venv312/bin/python3 cloud/repackage_zim.py "$SRC" "$TMP" \
    --split-find-chips \
    --spatial-chunk-scale 10 \
    --split-hot-search-chunks-mb 10 \
    --chip-split-threshold-mb 10

# --split-find-chips:           re-derive Find chips from poi.json/park.json
#                               (idempotent if source already has chips, but
#                                triggers chip-sub-bucketing for the
#                                threshold-fat ones)
# --spatial-chunk-scale 10:     convert monolithic graph.bin to 0.1° spatial cells
#                               (skipped automatically when the source is
#                                already spatial — the script detects)
# --split-hot-search-chunks-mb 10: sub-bucket search-data/*.json > 10 MB
# --chip-split-threshold-mb 10: sub-bucket category-index/chip-*.json > 10 MB

# 3. Validate. Hard-fails on real errors; warnings are OK to ship.
./venv312/bin/python3 cloud/validate_zim.py "$TMP"

# 4. Upload to archive.org. Validates again, uploads, stamps metadata,
#    rotates old dated versions, regenerates the public site.
cp "$TMP" "$OUT"
bash cloud/upload_validated.sh "$ID" "$OUT"

# 5. Cleanup local source if disk-tight.
rm -f "$SRC" "$TMP"
```

### Big regions to reroll on the remote box (high disk):

| ID                | Source size | Reason for remote run                 |
|-------------------|-------------|---------------------------------------|
| `europe`          | 44.8 GB     | too big to keep + roll on a 7 TB Mac at 94 % full |
| `united-states`   | 19.6 GB     | same                                  |
| `africa`          | 17.5 GB     | same                                  |
| `midwest-us`      | 4.8 GB      | optional — fits anywhere              |

Smaller regions (baltics 1.2 GB, colorado 0.8 GB, hispaniola 0.2 GB,
washington-dc 0.2 GB, california 3.0 GB) are fine on the local Mac.

### Chip-sub-bucket retrofit (already on -c but with > 100 MB chips)

These regions already have today's viewer + spatial routing + LLM
drop, but their largest chip file is too big for iOS heap. Re-roll
from the canonical local source so `--split-find-chips` can
sub-bucket them. Use a `-d` suffix (don't overwrite -c).

```sh
TODAY="$(date +%Y-%m-%d)d"

# Pick one:
#   japan        biggest chip 156.8 MB (restaurants)
#   east-coast-us biggest chip 148.6 MB (shops)
#   canada       biggest chip 130.7 MB (shops)
#   west-asia    biggest chip 127.1 MB (shops)
ID=japan
SRC=osm-japan-chips-v2.zim    # source name from cloud/reroll_viewer.sh
# (If this isn't local, fetch the latest -c from archive — its
# poi.json was dropped, so use the older non-c source archive.)

OUT="osm-${ID}-${TODAY}.zim"
TMP="osm-${ID}-${TODAY}-reroll.zim"

./venv312/bin/python3 cloud/repackage_zim.py "$SRC" "$TMP" \
    --split-find-chips \
    --split-hot-search-chunks-mb 10 \
    --chip-split-threshold-mb 10
./venv312/bin/python3 cloud/validate_zim.py "$TMP"
cp "$TMP" "$OUT"
bash cloud/upload_validated.sh "$ID" "$OUT"
```

---

## Path B (PRIMARY) — FULL REBUILD from planet PBF

This is the canonical path on the remote box. Produces fresh data
from the planet PBF + Overture + Wikidata, with all of today's
mobile-safety flags.

### Prereq: planet data on the remote box

The Europe box already has the heavy assets (planet PBF + world
MBTiles + terrain DEM cache + full satellite tile cache) from the
world-build run. **Do not re-download.** Just confirm they're at the
expected paths:

```sh
# Verify the world build's working files are present.
ls -lh world-data/{planet*.osm.pbf,world-tiles*.mbtiles}
ls -d terrain_cache/dem_sources/ satellite_cache_avif_256/

# Pull just the per-region Overture parquets you need (small,
# 1-3 GB each, regional bbox-clipped). Skip if running a region
# the world build hasn't touched.
#   scp local-mac:~/experiments/streetzim/overture_cache/{addresses,places}-${ID}-2026-04-15.0.parquet \
#       remote-box:~/experiments/streetzim/overture_cache/
```

If the world build is still running, **don't kick off concurrent
regional builds** until it finishes — they share the same MBTiles +
terrain cache + tilemaker store and will block each other on
filesystem locks. The world build is the implicit Phase 0; queue
regional builds after it.

### Build a region

```sh
ID=europe
BBOX="-25.0,34.0,50.5,72.0"           # west,south,east,north
NAME=Europe                            # human-readable region name

# This is the canonical command for a regional build (2026-05-08+).
# All Tier-1 work (spatial routing layout, LLM-bundle drop, recursive
# search-chunk split) happens inside create_osm_zim.py — there is no
# longer a post-build `cloud/repackage_zim.py` pass. Xapian indexes
# are produced by the standalone `xapianbuilder` helper rather than
# libzim's auto-indexer (40-60× faster on continent corpora). The
# ZIM emit goes through zimru (`--zim-builder=rust`) for parallel
# rayon-backed compression.
ZSTD_CLEVEL=22 ./venv312/bin/python3 create_osm_zim.py \
    --mbtiles world-data/world-tiles-v2.mbtiles \
    --pbf world-data/planet.osm.pbf \
    --bbox="$BBOX" \
    --name "$NAME" \
    --satellite --satellite-download-zoom 12 \
    --terrain \
    --wikidata \
    --routing \
    --search-cache search_cache/world.jsonl \
    --overture-addresses overture_cache/addresses-${ID}-2026-04-15.0.parquet \
    --overture-places overture_cache/places-${ID}-2026-04-15.0.parquet \
    --split-hot-search-chunks-mb 10 \
    --split-find-chips \
    --no-llm-bundle \
    --spatial-chunk-scale 1 \
    --xapian builder \
    --zim-builder rust \
    --low-zoom-world-vrt terrain_cache/dem_sources/world_dem_32k.tif \
    --output osm-${ID}.zim \
    --keep-temp \
    2>&1 | tee ${ID}-build.log
```

Flag reference:
- `--no-llm-bundle` — skip writing `category-index/{addr,poi,street}.json`.
  These are hundreds of MB to multi-GB on continent regions; the old
  post-build repack used to drop them. Chip emission still derives
  from poi+park records.
- `--spatial-chunk-scale N` — emit the routing graph as native
  SZCI/SZRC layout in-build (replaces the post-build
  `cloud/repackage_zim.py --spatial-chunk-scale N` step). N=1 (1°
  cells, ~110 cells for California) matches today's shipping
  convention; N=10 (0.1° cells) for ZIMs where the graph would be
  huge per-cell otherwise.
- `--xapian builder` — produce Xapian fulltext + title indexes via
  the external `../xapianbuilder/` helper (parallel, seconds rather
  than hours). Requires `--zim-builder=rust`. See
  `docs/zim-builder-rust.md` § "Xapian on the rust path".
- `--zim-builder rust` — emit via zimru / streetzim-pack. Required
  for `--xapian=builder`. Per-cluster zstd `windowLog` is pinned to
  `ceil(log2(cluster_size))` after zimru `64e76c7` — fzstd-friendly
  in browser. Don't ship continent-scale rust ZIMs against an older
  zimru checkout; see `docs/zim-packaging-gotchas.md` Gotcha #6.
- `--keep-temp` — important. Builds can fail at the ZIM-pack step
  and `--keep-temp` lets you resume without redoing the 4-hour
  routing extraction.
- `ZSTD_CLEVEL=22` — production default. ManifestCreator now reads
  this and forwards to zimru explicitly. Logged in the build summary
  as `zim-pack: zstd level` so you can spot a level mismatch.
- `TERRAIN_BLANK_TOLERATE=5` — set if the build aborts on the safety
  check from a few legit-low-elevation tiles (Caspian shoreline
  type cases). See memory `project_terrain_blank_tile_bug.md`.

After build (no repack pass any more):

```sh
TODAY="$(date +%Y-%m-%d)"
OUT="osm-${ID}-${TODAY}.zim"
mv "osm-${ID}.zim" "$OUT"
./venv312/bin/python3 cloud/validate_zim.py "$OUT"
bash cloud/upload_validated.sh "$ID" "$OUT"
```

The build summary printed at end-of-run breaks down phase + sub-phase
wall-clock so before/after measurements are concrete (memory:
`feedback_instrument_what_you_optimize.md`).

### Suggested order (small → large, lets you bail without losing big work)

| Order | ID              | Bbox                           | Est runtime |
|-------|-----------------|--------------------------------|-------------|
| 1     | washington-dc   | -77.2,38.7,-76.9,39.0          | ~10 min     |
| 2     | hispaniola      | -75.0,17.0,-67.0,21.0          | ~15 min     |
| 3     | colorado        | -109.1,36.9,-102.0,41.0        | ~20 min     |
| 4     | baltics         | 19.0,53.0,28.5,60.0            | ~30 min     |
| 5     | california      | -125.0,32.0,-114.0,42.0        | ~30 min     |
| 6     | midwest-us      | -97.5,36.0,-80.0,49.4          | ~1 h        |
| 7     | africa          | -18.0,-35.0,52.0,38.0          | ~3 h        |
| 8     | united-states   | -125.0,24.5,-66.9,49.4         | ~3 h        |
| 9     | europe          | -25.0,34.0,50.5,72.0           | ~4 h        |

California is in the list because today's local reroll caught a
pre-existing low-zoom terrain edge-stripe bug in the source ZIM
(tile 3/1/2.webp had 59-pixel and 131-pixel zero columns). The
local Mac doesn't have the world DEM cache to render those edges
correctly; the remote box does. The local reroll shipped with
TERRAIN_STRIPE_TOLERATE=20 as a stop-gap so iOS Kiwix users get the
spatial-routing fix today; replace with a full rebuild when the
remote queue catches up.

Total ≈ 12 h serially; less if the world build's caches are warm.
Run them sequentially — `create_osm_zim.py` saturates 8+ cores per
region during the routing extraction pass and concurrent regions
will fight for memory + the same tilemaker store dir.

### Wrapper script for the queue

After Phase 0 (the world build) finishes, run one of these per
region. Each takes its own log so you can resume from where it
crashed:

```sh
for row in \
  "washington-dc -77.2,38.7,-76.9,39.0 'Washington, D.C.'" \
  "hispaniola    -75.0,17.0,-67.0,21.0 'Hispaniola'"       \
  "colorado      -109.1,36.9,-102.0,41.0 'Colorado'"       \
  "baltics       19.0,53.0,28.5,60.0 'Baltics'"            \
  "midwest-us    -97.5,36.0,-80.0,49.4 'Midwest US'"       \
  "africa        -18.0,-35.0,52.0,38.0 'Africa'"           \
  "united-states -125.0,24.5,-66.9,49.4 'United States'"   \
  "europe        -25.0,34.0,50.5,72.0 'Europe'"            \
; do
  read -r id bbox name <<< "$row"
  log="${id}-rebuild-$(date +%Y%m%d).log"
  if [ -s "osm-${id}-$(date +%Y-%m-%d).zim" ]; then
      echo "skip $id (already built today)"; continue
  fi
  echo "=== $id @ $(date '+%H:%M:%S') ==="
  ./venv312/bin/python3 create_osm_zim.py \
      --mbtiles world-data/world-tiles-v2.mbtiles \
      --pbf world-data/planet.osm.pbf \
      --bbox="$bbox" --name "$name" \
      --satellite --satellite-download-zoom 12 \
      --terrain --wikidata --routing \
      --search-cache search_cache/world.jsonl \
      --overture-addresses overture_cache/addresses-${id}-2026-04-15.0.parquet \
      --overture-places overture_cache/places-${id}-2026-04-15.0.parquet \
      --chunk-graph-mb 200 \
      --split-hot-search-chunks-mb 10 \
      --split-find-chips \
      --low-zoom-world-vrt terrain_cache/dem_sources/world_dem_32k.tif \
      --output "osm-${id}.zim" \
      --keep-temp 2>&1 | tee "$log" || { echo "FAIL $id"; continue; }
  TODAY=$(date +%Y-%m-%d)
  OUT="osm-${id}-${TODAY}.zim"
  mv "osm-${id}.zim" "$OUT"
  ./venv312/bin/python3 cloud/repackage_zim.py "$OUT" "${OUT}.tmp" \
      --spatial-chunk-scale 10 \
      --split-find-chips --chip-split-threshold-mb 10 \
      --split-hot-search-chunks-mb 10 \
      >> "$log" 2>&1
  mv "${OUT}.tmp" "$OUT"
  ./venv312/bin/python3 cloud/validate_zim.py "$OUT" >> "$log" 2>&1
  bash cloud/upload_validated.sh "$id" "$OUT" >> "$log" 2>&1
done
```

---

## Region IDs / archive items

| ID              | Item                       | Last shipped     | Bbox                           |
|-----------------|----------------------------|------------------|--------------------------------|
| africa          | streetzim-africa           | 2026-04-17       | -18.0,-35.0,52.0,38.0          |
| australia-nz    | streetzim-australia-nz     | 2026-04-26c      | 110.0,-50.0,180.0,-9.0         |
| baltics         | streetzim-baltics          | 2026-04-22       | 19.0,53.0,28.5,60.0            |
| california      | streetzim-california       | 2026-04-22       | -125.0,32.0,-114.0,42.0        |
| canada          | streetzim-canada           | 2026-04-26c      | -141.0,41.0,-52.0,84.0         |
| central-asia    | streetzim-central-asia     | 2026-04-26c      | 35.0,30.0,80.0,55.0            |
| central-us      | streetzim-central-us       | 2026-04-26c      | -114.0,30.0,-94.0,49.0         |
| colorado        | streetzim-colorado         | 2026-04-22       | -109.1,36.9,-102.0,41.0        |
| east-coast-us   | streetzim-east-coast-us    | 2026-04-26c      | -84.0,24.5,-66.9,49.4          |
| egypt           | streetzim-egypt            | 2026-04-26c      | 24.0,21.0,38.0,33.0            |
| europe          | streetzim-europe           | 2026-04-17       | -25.0,34.0,50.5,72.0           |
| hispaniola      | streetzim-hispaniola       | 2026-04-22       | -75.0,17.0,-67.0,21.0          |
| iran            | streetzim-iran             | 2026-04-26c      | 44.0,25.0,63.5,39.8            |
| japan           | streetzim-japan            | 2026-04-26c      | 122.5,24.0,153.0,46.0          |
| midwest-us      | streetzim-midwest-us       | 2026-04-15       | -97.5,36.0,-80.0,49.4          |
| silicon-valley  | streetzim-silicon-valley   | 2026-04-26c      | -123.5,36.5,-121.0,38.5        |
| texas           | streetzim-texas            | 2026-04-26c      | -107.0,25.5,-93.0,37.0         |
| united-states   | streetzim-united-states    | 2026-04-13       | -125.0,24.5,-66.9,49.4         |
| washington-dc   | streetzim-washington-dc    | 2026-04-20       | -77.2,38.7,-76.9,39.0          |
| west-asia       | streetzim-west-asia        | 2026-04-26c      | 26.0,12.0,75.0,42.0            |
| west-coast-us   | streetzim-west-coast-us    | 2026-04-26c      | -125.0,32.0,-114.0,49.0        |

---

## Confirming a fresh roll on the live site

After upload, archive.org's metadata API takes 5–60 min to surface
the new file in the search index. The site's `web/generate.py` re-runs
during `upload_validated.sh`; the listing it sees may not include
the just-uploaded file yet. Re-run from any host:

```sh
./venv312/bin/python3 web/generate.py --deploy
```

Or wait for the next regular reroll which calls it.

### What happens when archive.org's metadata API is flaky

`fetch_item_details` now retries 3× with 1s/2s/4s exponential
backoff. Most transient errors (TLS handshake timeouts, brief
eventual-consistency hiccups, transient 5xx) absorb at the first
or second retry without operator intervention.

If retries are exhausted for an item that's KNOWN to exist (i.e.
appeared in the advancedsearch result list), `build_page`
collects the IDs in `failed_metadata` and **exits 2** rather than
writing the output file. The non-zero exit aborts the subsequent
`firebase deploy` step in `cloud/upload_validated.sh`, so a
broken-URL page never goes live.

Symptom in the wild: the **2026-05-10 California "page not
found" incident** — one SSL handshake timeout during the
upload's site-regen step caused `fetch_item_details` to return
`None`, which made the rendering loop silently fall through to
the static undated `osm-california.zim` URL (which doesn't
exist on archive.org since every upload uses dated filenames
per `feedback_dated_filenames_over_swap.md`). With this guard
in place, that timeout would now exit 2, the deploy would skip,
and the previous good site would remain live. Operator re-runs
`web/generate.py --deploy` once the network's stable.
