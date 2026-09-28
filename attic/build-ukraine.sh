#!/usr/bin/env bash
# Build Ukraine + Moldova + western Russia (Volgograd, Sochi) + Crimea.
#
# Pipeline:
#   1. cloud/build_region.sh — fresh build with all features, split
#      search chunks, split Find chips. Includes the LLM bundle.
#   2. cloud/repackage_zim.py — split routing graph spatially, drop the
#      LLM bundle (no mcpzim extras), re-bucket fat chips.
#   3. validate.
#   4. cloud/upload_validated.sh — upload to archive.org.
#
# Run from repo root.

set -euo pipefail

ID=ukraine
NAME="Ukraine, Moldova & Western Russia"
BBOX="22.0,43.0,46.0,53.0"      # W,S,E,N — Volgograd 44.5°E, Sochi 39.7°E, Crimea 32-37°E
TODAY="$(date +%Y-%m-%d)"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

log() { printf "[ukraine %s] %s\n" "$(date +%H:%M:%S)" "$*"; }

# ------------------------------------------------------------------
# 1. Fresh build
# ------------------------------------------------------------------
log "build_region.sh start"
bash cloud/build_region.sh "$ID" "$NAME" "$BBOX"
log "build_region.sh OK"

# ------------------------------------------------------------------
# 2. Repackage: spatial routing, drop LLM bundle, bucket fat chips.
#    drop_llm_bundle is the default in repackage_zim — explicit here
#    to make the "no mcpzim extras" intent obvious in the script.
# ------------------------------------------------------------------
RAW="osm-${ID}.zim"
TMP="osm-${ID}-${TODAY}-tmp.zim"
OUT="osm-${ID}-${TODAY}.zim"

if [ ! -s "$RAW" ]; then
    echo "[FATAL] build_region.sh did not produce $RAW" >&2
    exit 2
fi

log "repackage start"
./venv312/bin/python3 cloud/repackage_zim.py "$RAW" "$TMP" \
    --spatial-chunk-scale 10 \
    --split-find-chips \
    --split-hot-search-chunks-mb 10 \
    --chip-split-threshold-mb 10
log "repackage OK"

# ------------------------------------------------------------------
# 3. Validate
# ------------------------------------------------------------------
log "validate start"
./venv312/bin/python3 cloud/validate_zim.py "$TMP"
log "validate OK"

mv "$TMP" "$OUT"
log "renamed → $OUT"

# ------------------------------------------------------------------
# 4. Upload
# ------------------------------------------------------------------
log "upload start"
bash cloud/upload_validated.sh "$ID" "$OUT"
log "upload OK"

log "DONE — $OUT shipped to streetzim-${ID}"
