#!/usr/bin/env bash
# Build a world ZIM at vector z0-12 with named-feature search + wikidata.
# No satellite, no terrain, no routing. Targets ~45 GB output.
#
# Inputs:
#   world-data/world-tiles-v2.mbtiles    — full vector tiles MBTiles (z0-14, 113 GB);
#                                          create_osm_zim.py filters at read-time via
#                                          --max-zoom 12 so z13/14 are skipped (no truncate file).
#   world-data/planet-2026-03-10.osm.pbf — planet PBF (Q-ID cache already exists)
#   search_cache/world-no-streets.jsonl  — search index, streets dropped (~7 GB / 51M features)
#   wikidata_cache/                       — 90-bucket Wikidata cache (~1.3 GB / 3.15M entries)
#
# Output:
#   /storage/streetzim/osm-world-z12-named.zim

set -euo pipefail
cd /storage/streetzim

export TMPDIR=/storage/streetzim/tmp
export ZSTD_CLEVEL="${ZSTD_CLEVEL:-22}"
mkdir -p "$TMPDIR"

PY=/storage/streetzim/venv-linux/bin/python3
SCRIPT=/storage/streetzim/create_osm_zim.py

MBTILES=/storage/streetzim/world-data/world-tiles-v2.mbtiles
PBF=/storage/streetzim/world-data/planet-2026-03-10.osm.pbf
SEARCH=/storage/streetzim/search_cache/world-no-streets.jsonl
WD_CACHE=/storage/streetzim/wikidata_cache

OUTPUT=/storage/streetzim/osm-world-z12-named-$(date +%Y-%m-%d).zim
LOG=/storage/streetzim/world-z12-build.log

# Sanity checks
for f in "$MBTILES" "$PBF" "$SEARCH" "$SCRIPT"; do
  [ -f "$f" ] || { echo "Missing input: $f" >&2; exit 1; }
done
[ -d "$WD_CACHE" ] || { echo "Missing wikidata cache dir: $WD_CACHE" >&2; exit 1; }
[ ! -e "$OUTPUT" ] || { echo "Output already exists: $OUTPUT" >&2; exit 1; }

echo "=== World ZIM build (vector z0-12 + wiki + named search) ==="
echo "  mbtiles: $MBTILES ($(du -h "$MBTILES" | cut -f1))"
echo "  pbf:     $PBF ($(du -h "$PBF" | cut -f1))"
echo "  search:  $SEARCH ($(du -h "$SEARCH" | cut -f1))"
echo "  wd:      $WD_CACHE ($(du -sh "$WD_CACHE" | cut -f1))"
echo "  output:  $OUTPUT"
echo "  log:     $LOG"
echo "  ZSTD_CLEVEL=$ZSTD_CLEVEL"
echo "  TMPDIR=$TMPDIR"
echo "  start:   $(date -Iseconds)"
echo

"$PY" "$SCRIPT" \
    --mbtiles "$MBTILES" \
    --pbf "$PBF" \
    --name "World" \
    --max-zoom 12 \
    --cluster-size 8192 \
    --wikidata \
    --wikidata-cache "$WD_CACHE" \
    --search-cache "$SEARCH" \
    --keep-temp \
    -o "$OUTPUT" 2>&1 | tee "$LOG"

echo
echo "=== Done at $(date -Iseconds) ==="
echo "  output: $OUTPUT ($(du -h "$OUTPUT" 2>/dev/null | cut -f1 || echo "?"))"
