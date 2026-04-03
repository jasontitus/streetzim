#!/bin/bash
# Sequential rebuild queue — run after Europe build finishes
# Usage: source venv312/bin/activate && bash rebuild-queue.sh 2>&1 | tee rebuild-queue.log
set -e
export ZSTD_CLEVEL=22
PY="python3 /Users/jasontitus/experiments/streetzim/create_osm_zim.py"
WORLD_TILES="world-data/world-tiles-v2.mbtiles"
US_TILES="us-tiles.mbtiles"
PBF="world-data/planet-2026-03-10.osm.pbf"
SEARCH="search_cache/world.jsonl"
COMMON="--pbf $PBF --satellite --terrain --wikidata --search-cache $SEARCH --keep-temp"

echo "=== Rebuild Queue Started: $(date) ==="

echo ">>> [1/5] Indian Subcontinent"
$PY --mbtiles $WORLD_TILES $COMMON \
  --bbox="60.0,5.0,97.5,37.0" --name "Indian Subcontinent" \
  2>&1 | tee /Users/jasontitus/experiments/streetzim/indian-subcontinent-build.log

echo ">>> [2/5] California"
$PY --mbtiles $US_TILES $COMMON \
  --bbox="-124.48,32.53,-114.13,42.01" --name "California" \
  2>&1 | tee /Users/jasontitus/experiments/streetzim/california-build-v6.log

echo ">>> [3/5] Colorado"
$PY --mbtiles $US_TILES $COMMON \
  --bbox="-109.06,36.99,-102.04,41.00" --name "Colorado" \
  2>&1 | tee /Users/jasontitus/experiments/streetzim/colorado-build-v6.log

echo ">>> [4/5] Iran"
$PY --mbtiles $WORLD_TILES $COMMON \
  --bbox="44.0,25.0,63.5,39.8" --name "Iran" \
  2>&1 | tee /Users/jasontitus/experiments/streetzim/iran-build-v2.log

echo ">>> [5/5] Hispaniola"
$PY --mbtiles $WORLD_TILES $COMMON \
  --bbox="-74.5,17.5,-68.3,20.1" --name "Hispaniola" \
  2>&1 | tee /Users/jasontitus/experiments/streetzim/hispaniola-build-v3.log

echo "=== Rebuild Queue Complete: $(date) ==="
