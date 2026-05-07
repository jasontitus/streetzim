#!/usr/bin/env bash
# Build → repack → smoke → upload one region.
#
# Generalises build-ukraine.sh. The smoke gate is the difference: we
# refuse to upload a ZIM whose embedded viewer fails the harness, since
# a passing validator alone isn't sufficient (Ukraine 2026-05-05 shipped
# a SZCI v2 ZIM whose v1-only viewer broke routing on iOS).
#
# Usage:
#   bash build-region-and-upload.sh <id> "<name>" "<minlon,minlat,maxlon,maxlat>"
#
# Examples:
#   bash build-region-and-upload.sh silicon-valley "Silicon Valley" "-122.6,37.2,-121.7,37.9"
#   bash build-region-and-upload.sh california "California" "-125.0,32.0,-114.0,42.0"

set -euo pipefail

ID="${1:?id required}"
NAME="${2:?name required}"
BBOX="${3:?bbox required}"
TODAY="$(date +%Y-%m-%d)"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

log() { printf "[%s %s] %s\n" "$ID" "$(date +%H:%M:%S)" "$*"; }

# ------------------------------------------------------------------
# 1. Fresh build via cloud/build_region.sh (preflight + create_osm_zim
#    with all features + conditional spatial repack inside).
# ------------------------------------------------------------------
log "build_region.sh start (FORCE=1 to bypass terrain-edge-stripe preflight)"
FORCE=1 bash cloud/build_region.sh "$ID" "$NAME" "$BBOX"
log "build_region.sh OK"

RAW="osm-${ID}.zim"
TMP="osm-${ID}-${TODAY}-tmp.zim"
OUT="osm-${ID}-${TODAY}.zim"
[ -s "$RAW" ] || { echo "[FATAL] no $RAW after build" >&2; exit 2; }

# ------------------------------------------------------------------
# 2. Repack: drop LLM bundle, ensure spatial routing, split find chips,
#    split hot search chunks. Viewer is taken from disk so the embedded
#    HTML stays current with our latest fixes.
#
# build_region.sh internally runs spatial-chunk-scale 1 when the
# monolithic graph exceeds 500 MB, leaving the source already spatial
# with no graph.bin. Passing --spatial-chunk-scale here in that case
# would WIPE routing entirely (`no routing-data/graph.bin in source —
# nothing to upgrade. Output will have no routing.`). Detect and skip.
# ------------------------------------------------------------------
FLAGS=()
HAS_GRAPH_BIN=0
HAS_POI=0
./venv312/bin/python3 -c "
from libzim.reader import Archive
import sys
a = Archive(sys.argv[1])
try: a.get_entry_by_path('routing-data/graph.bin'); has_graph=1
except Exception: has_graph=0
try: a.get_entry_by_path('category-index/poi.json'); has_poi=1
except Exception: has_poi=0
print(has_graph, has_poi)
" "$RAW" | read HAS_GRAPH_BIN HAS_POI

# spatial-chunk-scale converts a MONOLITHIC graph.bin into the spatial
# layout. If the source is already spatial (build_region.sh's internal
# repack triggers when graph > 500 MB), passing this flag WIPES routing
# entirely (`no routing-data/graph.bin in source — nothing to upgrade`).
if [ "$HAS_GRAPH_BIN" = "1" ]; then
    log "source has graph.bin — passing --spatial-chunk-scale 10"
    FLAGS+=(--spatial-chunk-scale 10)
else
    log "source already spatial — skipping --spatial-chunk-scale"
fi

# split-find-chips DROPS existing chip-*.json and regenerates from
# poi.json. If poi.json was dropped (LLM-bundle drop default in the
# internal repack), this flag would leave NO chips at all. Only pass
# it when poi.json is present so chips can be re-derived.
if [ "$HAS_POI" = "1" ]; then
    log "source has poi.json — passing --split-find-chips"
    FLAGS+=(--split-find-chips --chip-split-threshold-mb 10)
else
    log "source has no poi.json — passing through existing chip-*.json"
fi

log "repackage start (flags: ${FLAGS[*]} --split-hot-search-chunks-mb 10)"
./venv312/bin/python3 cloud/repackage_zim.py "$RAW" "$TMP" \
    "${FLAGS[@]}" \
    --split-hot-search-chunks-mb 10
log "repackage OK"

# ------------------------------------------------------------------
# 3. Validate (terrain stripe edge tolerance for regional bboxes).
# ------------------------------------------------------------------
log "validate start"
TERRAIN_STRIPE_TOLERATE=10 ./venv312/bin/python3 cloud/validate_zim.py "$TMP"
log "validate OK"

mv "$TMP" "$OUT"
log "renamed → $OUT"

# ------------------------------------------------------------------
# 4. Smoke test against a local http.server. NOT optional — catches
#    embedded-viewer-vs-ZIM-format mismatches the validator can't see.
# ------------------------------------------------------------------
log "smoke start"
SERVER_PID=""
cleanup_server() {
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup_server EXIT

if ! lsof -nP -iTCP:8765 -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
    python3 -m http.server 8765 > /tmp/http8765-${ID}.log 2>&1 &
    SERVER_PID=$!
    sleep 2
fi

if ! ZIM_URL="http://localhost:8765/$OUT" node cloud/pwa_smoke_test.mjs \
        > "${ID}-smoke.log" 2>&1; then
    cleanup_server
    echo "[FATAL] smoke test failed for $ID — see ${ID}-smoke.log" >&2
    tail -30 "${ID}-smoke.log" >&2
    exit 5
fi
cleanup_server
log "smoke OK"

# ------------------------------------------------------------------
# 5. Upload — validate-once-more inside, then ia upload + metadata
#    stamp + cleanup + site regen + deploy.
# ------------------------------------------------------------------
log "upload start"
TERRAIN_STRIPE_TOLERATE=10 bash cloud/upload_validated.sh "$ID" "$OUT"
log "upload OK"

log "DONE — $OUT shipped to streetzim-${ID}"
