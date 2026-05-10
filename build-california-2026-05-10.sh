#!/usr/bin/env bash
# California rebuild on 2026-05-10 — primary motivation:
# `--spatial-chunk-scale 10` (0.1° routing cells) instead of the
# scale=1 we shipped on 2026-05-09. User's local-route profiles
# (e.g. 0.4 km route) showed cellHttpMs ≈ 2300 ms for a single
# 51 MB cell load through SW + IDB blob slice on iPhone Safari.
# Scale-10 emits ~11k tiny ~500 KB cells per region; first-fetch
# wall time should drop from ~2 s to ~150-300 ms cold for routes
# fully inside one cell. Long routes are roughly neutral.
#
# All other build flags match build-california-2026-05-09.sh
# (rust path, xapianbuilder, --no-llm-bundle, ZSTD_CLEVEL=22).
# Then validate → smoke → background upload.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

ID=california
NAME=California
BBOX="-125.0,32.0,-114.0,42.0"
TODAY="$(date +%Y-%m-%d)"
RAW="osm-${ID}.zim"
OUT="osm-${ID}-${TODAY}.zim"
LOG="${ID}-build-${TODAY}.log"

log() { printf "[%s %s] %s\n" "$ID" "$(date +%H:%M:%S)" "$*"; }

log "build start (rust path, --xapian builder, --spatial-chunk-scale 10)"
rm -f "$RAW"
ZSTD_CLEVEL=22 ./venv312/bin/python3 create_osm_zim.py \
    --mbtiles world-data/world-tiles-v2.mbtiles \
    --pbf world-data/planet.osm.pbf \
    --bbox="$BBOX" \
    --name "$NAME" \
    --satellite --satellite-download-zoom 12 \
    --terrain \
    --wikidata \
    --routing \
    --search-cache search_cache/world.jsonl \
    --overture-addresses "overture_cache/addresses-${ID}-2026-04-15.0.parquet" \
    --overture-places    "overture_cache/places-${ID}-2026-04-15.0.parquet" \
    --split-hot-search-chunks-mb 10 \
    --split-find-chips \
    --no-llm-bundle \
    --spatial-chunk-scale 10 \
    --xapian builder \
    --zim-builder rust \
    --low-zoom-world-vrt terrain_cache/dem_sources/world_dem_32k.tif \
    --output "$RAW" \
    --keep-temp \
    > "$LOG" 2>&1
log "build OK ($(du -h "$RAW" | awk '{print $1}'))"

mv "$RAW" "$OUT"
log "renamed → $OUT"

log "validate start"
TERRAIN_STRIPE_TOLERATE=10 ./venv312/bin/python3 cloud/validate_zim.py "$OUT" \
    >> "$LOG" 2>&1
log "validate OK"

log "smoke start"
SERVER_PID=""
cleanup_server() { [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup_server EXIT
if ! lsof -nP -iTCP:8765 -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
    python3 -m http.server 8765 > "/tmp/http8765-${ID}.log" 2>&1 &
    SERVER_PID=$!
    sleep 2
fi
ZIM_URL="http://localhost:8765/$OUT" node cloud/pwa_smoke_test.mjs \
    > "${ID}-smoke-${TODAY}.log" 2>&1
cleanup_server
log "smoke OK"

log "upload start (background → ${ID}-upload-${TODAY}.log)"
nohup bash -c \
    "TERRAIN_STRIPE_TOLERATE=10 bash cloud/upload_validated.sh '$ID' '$OUT'" \
    > "${ID}-upload-${TODAY}.log" 2>&1 &
log "upload pid=$! — wrapper exits, upload continues"
log "DONE (build/validate/smoke) — $OUT staged; upload running in background"
