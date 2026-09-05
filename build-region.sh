#!/usr/bin/env bash
# Build a regional ZIM from planet PBF + world MBTiles + caches.
# Mirrors docs/remote-rebuild.md Path B canonical command.
#
# Usage: build-region.sh <id> <bbox> <name>
#   id    - region id (matches overture_cache filenames)
#   bbox  - "minlon,minlat,maxlon,maxlat"
#   name  - display name
#
# Output: osm-${id}-YYYY-MM-DD.zim (after spatial-chunk repackage)
# Logs to: /storage/streetzim/${id}-rebuild-YYYY-MM-DD.log

set -euo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
export ZSTD_CLEVEL="${ZSTD_CLEVEL:-22}"
mkdir -p "$TMPDIR"

ID="$1"; BBOX="$2"; NAME="$3"
TODAY=$(date +%Y-%m-%d)
PY=/storage/streetzim/venv-linux/bin/python3
SCRIPT=/storage/streetzim/create_osm_zim.py

MBTILES=/storage/streetzim/world-data/regions/${ID}.mbtiles
PBF=/storage/streetzim/world-data/regions/${ID}.osm.pbf
SEARCH=/storage/streetzim/world-data/regions/${ID}.search.jsonl
WD=/storage/streetzim/wikidata_cache
TERRAIN=/storage/streetzim/terrain_cache
LOWZ=/storage/streetzim/terrain_cache/dem_sources/world_dem_32k.tif
ADDR=/storage/streetzim/overture_cache/addresses-${ID}-2026-04-15.0.parquet
PLACES=/storage/streetzim/overture_cache/places-${ID}-2026-04-15.0.parquet

OUT_RAW=osm-${ID}.zim
OUT_FINAL=osm-${ID}-${TODAY}.zim
LOG=/storage/streetzim/${ID}-rebuild-${TODAY}.log

echo "=== build $ID @ $(date -Iseconds) ==="
echo "  bbox: $BBOX"
echo "  out: $OUT_FINAL"
echo "  log: $LOG"
echo

if [ -f "$OUT_FINAL" ]; then
    echo "ALREADY EXISTS: $OUT_FINAL — skipping" | tee -a "$LOG"
    exit 0
fi

# Build args. Conditionally add overture/low-zoom VRT if files present
# (silicon-valley has parquet too, but unfamiliar regions may not).
ARGS=(
    --mbtiles "$MBTILES"
    --pbf "$PBF"
    --bbox="$BBOX"
    --name "$NAME"
    --satellite --satellite-download-zoom 12
    --terrain
    --wikidata --wikidata-cache "$WD"
    --terrain-dir "$TERRAIN"
    --routing
    --search-cache "$SEARCH"
    --chunk-graph-mb 200
    --split-hot-search-chunks-mb 10
    --split-find-chips
    --keep-temp
    --output "$OUT_RAW"
)
[ -f "$LOWZ" ]   && ARGS+=( --low-zoom-world-vrt "$LOWZ" )
[ -f "$ADDR" ]   && ARGS+=( --overture-addresses "$ADDR" )
[ -f "$PLACES" ] && ARGS+=( --overture-places "$PLACES" )

"$PY" "$SCRIPT" "${ARGS[@]}" 2>&1 | tee "$LOG"

echo "=== mv + repackage @ $(date -Iseconds) ===" | tee -a "$LOG"
mv "$OUT_RAW" "$OUT_FINAL"
"$PY" /storage/streetzim/cloud/repackage_zim.py "$OUT_FINAL" "$OUT_FINAL.tmp" \
    --spatial-chunk-scale 10 \
    --split-find-chips \
    --chip-split-threshold-mb 10 \
    --split-hot-search-chunks-mb 10 \
    2>&1 | tee -a "$LOG"
mv "$OUT_FINAL.tmp" "$OUT_FINAL"

echo "=== validate @ $(date -Iseconds) ===" | tee -a "$LOG"
"$PY" /storage/streetzim/cloud/validate_zim.py "$OUT_FINAL" 2>&1 | tee -a "$LOG" || \
    echo "(validate non-zero — not blocking; review log)" | tee -a "$LOG"

echo "=== done $ID @ $(date -Iseconds), size: $(du -h "$OUT_FINAL" | cut -f1) ===" | tee -a "$LOG"
