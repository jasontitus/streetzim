#!/usr/bin/env bash
# Build a world ZIM at vector z0-13 + terrain (z0-12) + wiki + named search.
# No satellite, no routing.
#
# Inputs:
#   world-data/world-tiles-v2.mbtiles    — full vector tiles MBTiles (z0-14);
#                                          create_osm_zim.py filters at read-time via
#                                          --max-zoom 13 so z14 is skipped.
#   world-data/planet-2026-03-10.osm.pbf — planet PBF (Q-ID cache already exists)
#   search_cache/world.jsonl             — full search index incl. streets (~17.5 GB / 121M features)
#   wikidata_cache/                       — 90-bucket Wikidata cache
#   terrain_cache/                        — z0-12 webp terrain-RGB tiles (~30 GB cached at z12;
#                                          regional COMPLETED_z12_* markers cover most land;
#                                          per-tile cache hits skip DEM regen for the rest)
#
# Output:
#   /storage/streetzim/osm-world-z13-named-YYYY-MM-DD.zim

set -euo pipefail
cd /storage/streetzim

export TMPDIR=/storage/streetzim/tmp
export ZSTD_CLEVEL="${ZSTD_CLEVEL:-22}"
mkdir -p "$TMPDIR"

PY=/storage/streetzim/venv-linux/bin/python3
SCRIPT=/storage/streetzim/create_osm_zim.py

MBTILES=/storage/streetzim/world-data/world-tiles-v2.mbtiles
PBF=/storage/streetzim/world-data/planet-2026-03-10.osm.pbf
SEARCH=/storage/streetzim/search_cache/world.jsonl
WD_CACHE=/storage/streetzim/wikidata_cache
TERRAIN_DIR=/storage/streetzim/terrain_cache

OUTPUT=/storage/streetzim/osm-world-z13-named-$(date +%Y-%m-%d).zim
LOG=/storage/streetzim/world-z13-build.log

# Sanity checks
for f in "$MBTILES" "$PBF" "$SEARCH" "$SCRIPT"; do
  [ -f "$f" ] || { echo "Missing input: $f" >&2; exit 1; }
done
[ -d "$WD_CACHE" ]    || { echo "Missing wikidata cache dir: $WD_CACHE" >&2; exit 1; }
[ -d "$TERRAIN_DIR" ] || { echo "Missing terrain cache dir: $TERRAIN_DIR" >&2; exit 1; }
[ ! -e "$OUTPUT" ]    || { echo "Output already exists: $OUTPUT" >&2; exit 1; }

echo "=== World ZIM build (vector z0-13 + terrain z0-12 + wiki + full search incl streets) ==="
echo "  mbtiles: $MBTILES ($(du -h "$MBTILES" | cut -f1))"
echo "  pbf:     $PBF ($(du -h "$PBF" | cut -f1))"
echo "  search:  $SEARCH ($(du -h "$SEARCH" | cut -f1))"
echo "  wd:      $WD_CACHE ($(du -sh "$WD_CACHE" | cut -f1))"
echo "  terrain: $TERRAIN_DIR ($(du -sh "$TERRAIN_DIR" | cut -f1))"
echo "  output:  $OUTPUT"
echo "  log:     $LOG"
echo "  ZSTD_CLEVEL=$ZSTD_CLEVEL"
echo "  TMPDIR=$TMPDIR"
echo "  start:   $(date -Iseconds)"
echo

"$PY" "$SCRIPT" \
    --mbtiles "$MBTILES" \
    --pbf "$PBF" \
    --bbox="-180,-85,180,85" \
    --name "World" \
    --max-zoom 13 \
    --terrain \
    --terrain-zoom 12 \
    --terrain-dir "$TERRAIN_DIR" \
    --cluster-size 8192 \
    --wikidata \
    --wikidata-cache "$WD_CACHE" \
    --search-cache "$SEARCH" \
    --keep-temp \
    -o "$OUTPUT" 2>&1 | tee "$LOG"

echo
echo "=== Done at $(date -Iseconds) ==="
echo "  output: $OUTPUT ($(du -h "$OUTPUT" 2>/dev/null | cut -f1 || echo "?"))"
