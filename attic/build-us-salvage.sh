#!/usr/bin/env bash
# Salvage US rebuild after machine reboot killed the build during [10/10]
# ZIM creation (was 600/3572 search chunks added). The enriched search
# jsonl (14.7M OSM addrs + 130M Overture addrs + 16.8M Overture POIs)
# was preserved from /storage/streetzim/tmp/osm_zim_p_dt7y4w/search_features.jsonl
# into /storage/streetzim/salvage/us-2026-05-01.search-with-overture.jsonl
# (reflinked copy, ~zero disk).
#
# Reuses that cache via --skip-address-extract; routing extract redoes
# Pass 1 + Pass 2 (sparse_file_array on /storage HDD; del-loc-handler
# patch in place so post-Pass-2 doesn't thrash on the 30 GB mmap squat).
set -euo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
export ZSTD_CLEVEL="${ZSTD_CLEVEL:-22}"
mkdir -p "$TMPDIR"

PY=/storage/streetzim/venv-linux/bin/python3
SCRIPT=/storage/streetzim/create_osm_zim.py

ID=united-states
BBOX="-125.0,24.5,-66.9,49.4"
NAME="United States"

MBTILES=/storage/streetzim/world-data/regions/${ID}.mbtiles
PBF=/storage/streetzim/world-data/regions/${ID}.osm.pbf
SEARCH=/storage/streetzim/salvage/us-2026-05-01.search-with-overture.jsonl
WD=/storage/streetzim/wikidata_cache
TERRAIN=/storage/streetzim/terrain_cache
LOWZ=/storage/streetzim/terrain_cache/dem_sources/world_dem_32k.tif

TODAY=$(date +%Y-%m-%d)
OUT_RAW=osm-${ID}.zim
OUT_FINAL=osm-${ID}-${TODAY}.zim
LOG=/storage/streetzim/${ID}-rebuild-${TODAY}.log

echo "=== build $ID (salvage, skip-address-extract) @ $(date -Iseconds) ==="
echo "  bbox: $BBOX  out: $OUT_FINAL  log: $LOG"
echo "  search: $SEARCH ($(du -h "$SEARCH" | cut -f1))"
echo

[ -f "$OUT_FINAL" ] && { echo "ALREADY EXISTS: $OUT_FINAL — skipping" | tee -a "$LOG"; exit 0; }

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
    --skip-address-extract
    --chunk-graph-mb 200
    --split-hot-search-chunks-mb 10
    --split-find-chips
    --keep-temp
    --output "$OUT_RAW"
)
[ -f "$LOWZ" ] && ARGS+=( --low-zoom-world-vrt "$LOWZ" )

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
    echo "(validate non-zero — review)" | tee -a "$LOG"

echo "=== done $ID @ $(date -Iseconds), size: $(du -h "$OUT_FINAL" | cut -f1) ===" | tee -a "$LOG"
