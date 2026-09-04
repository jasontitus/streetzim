#!/usr/bin/env bash
# Regenerate the planet vector-tile MBTiles + the world search cache from a
# new planet PBF. This is the expensive half of a data refresh (the March
# world-tiles-v2.mbtiles was produced on the Mac; this host has no tilemaker
# yet — see docs/rebuild-2026-09-plan.md "Prerequisites").
#
# Usage: ./build-world-tiles.sh [planet.osm.pbf] [out.mbtiles]
# Env:   TILEMAKER (binary; default: tilemaker on PATH)
#        STORE     (on-disk node/way store; default tmp/tilemaker-store —
#                   put this on NVMe (/data) if root grants a writable dir;
#                   the HDD works but is several× slower)
#
# Uses the same config/lua the regional builds do (resources/tilemaker/) so
# the OpenMapTiles layers match what the viewer style expects. coastline/
# and landcover/ shapefiles are referenced relative to the repo root.
set -euo pipefail
cd /storage/streetzim
PLANET="${1:-/storage/streetzim/world-data/planet-2026-08-31.osm.pbf}"
OUT="${2:-/storage/streetzim/world-data/world-tiles-v3.mbtiles}"
TILEMAKER="${TILEMAKER:-tilemaker}"
STORE="${STORE:-/storage/streetzim/tmp/tilemaker-store}"
LOG=/storage/streetzim/world-tiles-$(date +%Y-%m-%d).log

command -v "$TILEMAKER" >/dev/null || { echo "tilemaker not found (build v3 from https://github.com/systemed/tilemaker; apt's 2.4 is too old for resources/tilemaker/*)" >&2; exit 1; }
[ -s "$PLANET" ] || { echo "planet missing: $PLANET" >&2; exit 1; }
[ -e "$OUT" ] && { echo "refusing to overwrite $OUT" >&2; exit 1; }
[ -s coastline/water_polygons.shp ] || { echo "coastline/water_polygons.shp missing" >&2; exit 1; }
mkdir -p "$STORE" "$(dirname "$OUT")"

echo "=== tilemaker $($TILEMAKER --help 2>&1 | head -1) planet=$PLANET out=$OUT store=$STORE @ $(date -Iseconds)" | tee "$LOG"
"$TILEMAKER" \
  --input "$PLANET" \
  --output "$OUT.part" \
  --config resources/tilemaker/config-openmaptiles.json \
  --process resources/tilemaker/process-openmaptiles.lua \
  --store "$STORE" \
  --skip-integrity \
  2>&1 | tee -a "$LOG"
mv -f "$OUT.part" "$OUT"
echo "=== mbtiles done: $(du -h "$OUT" | cut -f1) @ $(date -Iseconds)" | tee -a "$LOG"
rm -rf "$STORE"

DATED=$(basename "$PLANET" .osm.pbf); DATED=${DATED#planet-}
SEARCH=/storage/streetzim/search_cache/world-${DATED}.jsonl
echo "=== search cache → $SEARCH @ $(date -Iseconds)" | tee -a "$LOG"
TMPDIR=/storage/streetzim/tmp /storage/streetzim/venv-linux/bin/python3 -u \
  cloud/build_search_cache.py --mbtiles "$OUT" --out "$SEARCH" 2>&1 | tee -a "$LOG"
echo "=== done @ $(date -Iseconds). Next: point the queue at these:" | tee -a "$LOG"
echo "    WORLD_MBTILES=$OUT WORLD_SEARCH=$SEARCH ./build-refresh-queue.sh ..." | tee -a "$LOG"
