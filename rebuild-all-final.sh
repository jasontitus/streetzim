#!/bin/bash
# Rebuild ALL regions with boundary-fixed terrain, YYYY-MM-DD naming.
# Runs 2 tracks in parallel. Each build auto-verifies terrain + uploads.
set -e
cd /Users/jasontitus/experiments/streetzim
source venv312/bin/activate
export ZSTD_CLEVEL=22

DATE=$(date +%Y-%m-%d)

build_and_upload() {
  local ID="$1"
  local BBOX="$2"
  local NAME="$3"
  local MBTILES="$4"
  local LOG="${ID}-final.log"

  echo "=== Building $NAME ==="
  # Delete terrain tiles + COMPLETED markers for this bbox so they
  # regenerate fresh from the buffered VRT (fixes boundary seams +
  # wrong-VRT artifacts from prior builds).
  python3 -c "
import os, glob, mercantile
for f in glob.glob('terrain_cache/COMPLETED_*'): os.unlink(f)
bbox = tuple(float(x) for x in '$BBOX'.split(','))
d = 0
for z in range(0, 13):
    for t in mercantile.tiles(*bbox, zooms=z):
        p = f'terrain_cache/{z}/{t.x}/{t.y}.webp'
        if os.path.exists(p): os.unlink(p); d += 1
print(f'Deleted {d} terrain tiles for $NAME')
"
  python3 create_osm_zim.py \
    --mbtiles "$MBTILES" \
    --pbf world-data/planet-2026-03-10.osm.pbf \
    --bbox="$BBOX" \
    --name "$NAME" \
    --satellite --satellite-download-zoom 12 \
    --terrain --wikidata \
    --search-cache search_cache/world.jsonl \
    --keep-temp \
    2>&1 | tee "$LOG"

  # Find output ZIM (dated filename)
  ACTUAL=$(ls -t osm-$(echo "$NAME" | tr '[:upper:] &' '[:lower:]-' | tr -s '-')*-${DATE}.zim 2>/dev/null | head -1)
  if [ -z "$ACTUAL" ]; then
    ACTUAL=$(ls -t osm-$(echo "$NAME" | tr '[:upper:] &' '[:lower:]-' | tr -s '-')*.zim 2>/dev/null | head -1)
  fi
  if [ -z "$ACTUAL" ]; then
    echo "ERROR: No ZIM found for $NAME"
    return 1
  fi

  echo "Uploading $ACTUAL..."
  ia upload "streetzim-${ID}" "$ACTUAL" --retries 5 2>&1 | tail -1
  ia metadata "streetzim-${ID}" --modify="date:${DATE}" 2>&1 | tail -1
  python3 web/generate.py --deploy 2>&1 | tail -1
  echo "=== $NAME done ==="
}

# Track 1: US-based regions (use us-tiles.mbtiles — fast)
track1() {
  build_and_upload "united-states" "-125.0,24.4,-66.9,49.4" "United States" "us-tiles.mbtiles"
  build_and_upload "east-coast-us" "-81.7,24.5,-66.9,47.5" "East Coast US" "us-tiles.mbtiles"
  build_and_upload "west-coast-us" "-124.8,32.5,-114.1,49.0" "West Coast US" "us-tiles.mbtiles"
  build_and_upload "midwest-us" "-104.1,36.0,-80.5,49.4" "Midwest US" "us-tiles.mbtiles"
  build_and_upload "california" "-124.48,32.53,-114.13,42.01" "California" "us-tiles.mbtiles"
  build_and_upload "texas" "-106.65,25.84,-93.51,36.50" "Texas" "us-tiles.mbtiles"
}

# Track 2: World-based regions (use world-tiles-v2.mbtiles)
track2() {
  build_and_upload "japan" "122.9,24.0,146.0,45.6" "Japan" "world-data/world-tiles-v2.mbtiles"
  build_and_upload "west-asia" "25.0,12.0,62.5,42.0" "West Asia" "world-data/world-tiles-v2.mbtiles"
  build_and_upload "australia-nz" "110.0,-50.0,180.0,-8.0" "Australia and New Zealand" "world-data/world-tiles-v2.mbtiles"
  build_and_upload "africa" "-18.0,-35.0,52.0,38.0" "Africa" "world-data/world-tiles-v2.mbtiles"
  build_and_upload "europe" "-25.0,34.0,50.5,72.0" "Europe" "world-data/world-tiles-v2.mbtiles"
}

echo "=== Full rebuild started: $(date) ==="
echo "Track 1: US regions (us-tiles.mbtiles)"
echo "Track 2: World regions (world-tiles-v2.mbtiles)"
echo ""

# Run both tracks in parallel
track1 > track1-final.log 2>&1 &
T1=$!
track2 > track2-final.log 2>&1 &
T2=$!

echo "Track 1 PID: $T1"
echo "Track 2 PID: $T2"

wait $T1
echo "Track 1 complete: $(date)"
wait $T2
echo "Track 2 complete: $(date)"

echo "=== Full rebuild done: $(date) ==="
