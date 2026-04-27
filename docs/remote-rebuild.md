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

# This is the canonical command for a regional build. All flags
# match what we ship today on -c:
./venv312/bin/python3 create_osm_zim.py \
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
    --chunk-graph-mb 200 \
    --split-hot-search-chunks-mb 10 \
    --split-find-chips \
    --low-zoom-world-vrt terrain_cache/dem_sources/world_dem_32k.tif \
    --output osm-${ID}.zim \
    --keep-temp \
    2>&1 | tee ${ID}-build.log
```

Notes:
- `--keep-temp` is important. Builds can fail at the ZIM-pack step
  and `--keep-temp` lets you resume without redoing the 4-hour
  routing extraction.
- Set `ZSTD_CLEVEL=22` in the env if you want maximum compression
  (it's the default).
- Set `TERRAIN_BLANK_TOLERATE=5` if the build aborts on the safety
  check from a few legit-low-elevation tiles (Caspian shoreline
  type cases). Memory file `project_terrain_blank_tile_bug.md`
  describes when to use this and when to fix the underlying DEM gap.

After build:

```sh
TODAY="$(date +%Y-%m-%d)"
OUT="osm-${ID}-${TODAY}.zim"
mv "osm-${ID}.zim" "$OUT"

# Spatial-chunk the routing graph for mobile (post-process, since
# create_osm_zim emits monolithic graph.bin — we then convert).
# Also drops the LLM bundle (addr/poi/street.json — viewer doesn't
# read them, ~10-15% size win) and sub-buckets fat chips (>10 MB).
./venv312/bin/python3 cloud/repackage_zim.py "$OUT" "$OUT.spatial.zim" \
    --spatial-chunk-scale 10 \
    --split-find-chips \
    --split-hot-search-chunks-mb 10 \
    --chip-split-threshold-mb 10
mv "$OUT.spatial.zim" "$OUT"

./venv312/bin/python3 cloud/validate_zim.py "$OUT"
bash cloud/upload_validated.sh "$ID" "$OUT"
```

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
