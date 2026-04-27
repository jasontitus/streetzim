# Remote-box rebuild runbook

For the 128 GB / 14 TB remote box. Two paths:

- **REROLL** — pull an existing shipped ZIM from archive.org and
  re-emit it with the current viewer + mobile-safety upgrades. Cheap
  (10–60 min per region), no PBF processing.
- **FULL REBUILD** — start from the planet PBF and run
  `create_osm_zim.py` end-to-end. Expensive (hours per continental
  region) but produces a fresh routing graph, fresh OSM data, fresh
  Overture enrichment.

Always reroll first. Only do a full rebuild when the source ZIM
itself has bugs you can't patch via repackage (e.g. terrain
edge-stripe blocks, wrong bbox, schema breaks).

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

## Path A — REROLL a shipped archive.org ZIM

Use this when the source ZIM has good data but predates the
mobile-safety upgrades (any pre-2026-04-26 region falls here).

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

## Path B — FULL REBUILD from planet PBF

Use only when the source ZIM has unfixable issues (e.g. wrong bbox,
missing Overture enrichment, schema that predates the chunk format).

### Prereq: planet data on the remote box

```sh
mkdir -p world-data
# Latest planet PBF (~80 GB, slow download).
curl -fL "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf" \
    -o world-data/planet.osm.pbf

# Optional: world MBTiles for vector tiles. Fastest is to copy from
# the local Mac since regenerating tilemaker output is hours.
#   scp local-mac:~/experiments/streetzim/world-data/world-tiles-v2.mbtiles \
#       remote-box:~/experiments/streetzim/world-data/

# Terrain DEM cache. The Copernicus tiles are deterministic and the
# generator caches them. If you have the local Mac's
# terrain_cache/dem_sources/, sync it (~547 GB) so the build doesn't
# re-download from AWS.
#   rsync -aP local-mac:~/experiments/streetzim/terrain_cache \
#               remote-box:~/experiments/streetzim/

# Overture parquet caches per region (~1-3 GB each, much smaller
# than the full planet bundle — fetch only what's needed):
#   scp local-mac:~/experiments/streetzim/overture_cache/{addresses,places}-${ID}-2026-04-15.0.parquet \
#       remote-box:~/experiments/streetzim/overture_cache/
```

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
./venv312/bin/python3 cloud/repackage_zim.py "$OUT" "$OUT.spatial.zim" \
    --spatial-chunk-scale 10 \
    --chip-split-threshold-mb 10
mv "$OUT.spatial.zim" "$OUT"

./venv312/bin/python3 cloud/validate_zim.py "$OUT"
bash cloud/upload_validated.sh "$ID" "$OUT"
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
