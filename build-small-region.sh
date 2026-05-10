#!/usr/bin/env bash
# Drives one small-region rebuild end-to-end:
#   build → date-rename → validate → smoke → background upload
#
# Usage:
#   bash build-small-region.sh <id> "<name>" "<bbox>"
#
# Example:
#   bash build-small-region.sh washington-dc "Washington DC" "-77.2,38.7,-76.8,39.0"
#
# Each step is gated. Failure prints which step failed and exits non-zero.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

ID="${1:?id required}"
NAME="${2:?name required}"
BBOX="${3:?bbox required}"

TODAY="$(date +%Y-%m-%d)"
RAW="osm-${ID}.zim"
OUT="osm-${ID}-${TODAY}.zim"
LOG="${ID}-build-${TODAY}.log"

log() { printf "[%s %s] %s\n" "$ID" "$(date +%H:%M:%S)" "$*"; }

log "build start (build_region.sh canonical flags)"
rm -f "$RAW"
bash cloud/build_region.sh "$ID" "$NAME" "$BBOX" >> "$LOG" 2>&1
log "build OK ($(du -h "$RAW" | awk '{print $1}'))"

mv "$RAW" "$OUT"
log "renamed → $OUT"

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
