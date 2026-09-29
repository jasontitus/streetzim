#!/bin/bash
# Re-gate and upload retrofit artifacts the queue will never revisit.
#
# The queue picks "newest local dated ZIM" and requires it to be the file
# archive.org currently lists. Once a region has been retrofitted, its newest
# local ZIM *is* the retrofit output, which is not live — so the queue skips
# it forever ("not live"). These regions retrofitted fine on 2026-09-17/18 and
# then gate-failed only on the equivalence keys bug (fixed 2026-09-18: added
# manifest keys such as category_shards are expected, dropped ones still fail).
# Their artifacts are intact, so re-gate in place rather than burn hours
# rebuilding.
#
# Uses PORT 8821 so it cannot collide with the retrofit queue (8811) or
# build-refresh-queue.sh (8801), and takes the same upload lock so only one
# upload runs at a time.
#
# Usage: ./regate-stranded.sh [region ...]      (default: all in STRANDED)
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
PY=/storage/streetzim/venv-linux/bin/python3
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
PORT="${PORT:-8821}"
REGISTRY=/storage/streetzim/cloud/regions.tsv
LOG=/storage/streetzim/regate-stranded.log
TSV=/storage/streetzim/regate-stranded.tsv
UPLOAD_LOCK=/storage/streetzim/.retrofit-upload.lock
UPLOAD="${UPLOAD:-1}"

# region:source:output — source is the live archive.org file, output the
# retrofit artifact to gate. central-us is absent on purpose: its output was
# truncated when the queue was killed and has been deleted.
STRANDED=(
  "turkey:osm-turkey-2026-09-06.zim:osm-turkey-2026-09-18.zim"
  "japan:osm-japan-2026-09-16.zim:osm-japan-2026-09-18.zim"
  "mexico:osm-mexico-2026-09-10.zim:osm-mexico-2026-09-18.zim"
  "southeast-asia:osm-southeast-asia-2026-09-16.zim:osm-southeast-asia-2026-09-17.zim"
  "west-asia:osm-west-asia-2026-09-09.zim:osm-west-asia-2026-09-18.zim"
  "indian-subcontinent:osm-indian-subcontinent-2026-09-14.zim:osm-indian-subcontinent-2026-09-18.zim"
  "central-asia:osm-central-asia-2026-09-11.zim:osm-central-asia-2026-09-18.zim"
  "south-america:osm-south-america-2026-09-15.zim:osm-south-america-2026-09-16.zim"
)

log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" | tee -a "$LOG"; }
row(){ printf "%s\t%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$4" "$(date -Iseconds)" >> "$TSV"; }
[ -f "$TSV" ] || printf "id\tstatus\tzim\tnote\tfinished\n" > "$TSV"

# Gate helpers are the queue's own, extracted verbatim so the two paths cannot
# drift: smoke_find, smoke_search, equivalence, browser_smoke.
HELPERS=$(mktemp "$TMPDIR/regate-helpers.XXXXXX.sh")
sed -n '/^smoke_find()/,/^}/p;/^smoke_search()/,/^}/p;/^equivalence()/,/^}/p;/^browser_smoke()/,/^}/p' \
    retrofit-chips-queue.sh > "$HELPERS"
# shellcheck disable=SC1090
. "$HELPERS"
trap 'rm -f "$HELPERS"' EXIT

TARGETS=("$@")
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=("${STRANDED[@]}")

for spec in "${TARGETS[@]}"; do
  # allow bare region ids on the command line
  case "$spec" in
    *:*) ID="${spec%%:*}"; rest="${spec#*:}"; SRC="${rest%%:*}"; OUT="${rest##*:}" ;;
    *)   ID="$spec"; SRC=""; OUT=""
         for s in "${STRANDED[@]}"; do
           [ "${s%%:*}" = "$ID" ] || continue
           rest="${s#*:}"; SRC="${rest%%:*}"; OUT="${rest##*:}"
         done ;;
  esac
  if [ -z "${OUT:-}" ] || [ ! -s "$OUT" ]; then
    log "skip $ID: no artifact ($OUT)"; row "$ID" skipped "${OUT:-?}" "missing"; continue
  fi

  REG=$(awk -F'\t' -v id="$ID" '$1==id' "$REGISTRY" | head -1)
  BBOX=$(echo "$REG" | cut -f3); RSRC=$(echo "$REG" | cut -f5); RDST=$(echo "$REG" | cut -f6); SEARCH=$(echo "$REG" | cut -f7)
  if [ -z "$BBOX" ] || [ -z "$RSRC" ] || [ -z "$RDST" ] || [ -z "$SEARCH" ]; then
    log "skip $ID: incomplete registry row"; row "$ID" skipped "$OUT" "registry"; continue
  fi

  log "=== $ID: gating $OUT ($(du -h "$OUT" | cut -f1)) against $SRC  search='$SEARCH'"
  T0=$(date +%s); G=""

  # check_terrain_coverage.py takes TWO positionals: zim and bbox. Timeout
  # scales with size, same formula the queue uses.
  _tsz=$(( $(stat -c%s "$OUT") / 1073741824 ))
  _tto=$(( 900 + _tsz * 300 )); [ "$_tto" -gt 14400 ] && _tto=14400
  if timeout "$_tto" nice -n 10 "$PY" cloud/check_terrain_coverage.py --zooms 10-12 -- "$OUT" "$BBOX" >> "$LOG" 2>&1; then
    log "  gate terrain: OK"
  else
    _trc=$?; G="$G terrain"
    [ "$_trc" -eq 124 ] && log "  gate terrain: FAIL (timed out after ${_tto}s — inconclusive)" || log "  gate terrain: FAIL"
  fi

  if TERRAIN_STRIPE_TOLERATE=10 timeout 7200 "$PY" cloud/validate_zim.py "$OUT" >> "$LOG" 2>&1; then
    log "  gate validate: OK"; else G="$G validate"; log "  gate validate: FAIL"; fi

  ROUTE_OUT=$(timeout 2400 "$PY" cloud/route_cli.py --zim="$OUT" --src="$RSRC" --dst="$RDST" --mode=all --max-pops=5000000 2>&1)
  echo "$ROUTE_OUT" | tail -6 | sed 's/^/    /' >> "$LOG"
  ASTAR_OK=$(echo "$ROUTE_OUT" | awk '/=== mode: astar/{f=1} f&&/route OK/{print 1; exit} /=== mode: hwy2/{f=0}')
  if [ "${ASTAR_OK:-0}" = 1 ]; then log "  gate routing: OK (astar)"; else G="$G routing"; log "  gate routing: FAIL"; fi

  SN=$(smoke_search "$OUT" "$SEARCH"); FN=$(smoke_find "$OUT")
  [ "${SN:-0}" -ge 1 ] 2>/dev/null && log "  gate search('$SEARCH'): $SN" || { G="$G search"; log "  gate search: FAIL ($SN)"; }
  [ "${FN:-0}" -ge 1 ] 2>/dev/null && log "  gate find(restaurants): $FN" || { G="$G find"; log "  gate find: FAIL ($FN)"; }

  EQ=$(equivalence "$SRC" "$OUT" "$SEARCH" 2>&1); EQRC=$?
  echo "$EQ" | tail -4 | sed 's/^/    /' >> "$LOG"
  if [ $EQRC -eq 0 ]; then log "  gate equivalence: OK"; else G="$G equivalence"; log "  gate equivalence: FAIL"; fi

  SMOKELOG="/storage/streetzim/$ID-regate-smoke-$(date +%Y-%m-%d).log"
  if browser_smoke "$OUT" "$RSRC" "$RDST" "$SEARCH" "$SMOKELOG"; then
    log "  gate browser: OK"
  else
    G="$G browser"; log "  gate browser: FAIL (see $SMOKELOG)"
  fi

  MIN=$(( ($(date +%s) - T0) / 60 ))
  if [ -n "$G" ]; then
    log "  GATES FAILED:$G — NOT uploading $OUT (kept)"
    row "$ID" gate-failed "$OUT" "$G ($MIN min)"; continue
  fi
  if [ "$UPLOAD" != 1 ]; then
    log "  all gates passed — UPLOAD=0, stopping here"; row "$ID" gated-ok "$OUT" "$MIN min"; continue
  fi

  log "  all gates passed — uploading $OUT"
  flock "$UPLOAD_LOCK" env PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
    bash cloud/upload_validated.sh "$ID" "$OUT" >> "$LOG" 2>&1
  URC=$?
  case $URC in
    0) log "  uploaded → https://archive.org/details/streetzim-$ID"; row "$ID" uploaded "$OUT" "$MIN min" ;;
    6) log "  upload transferred, not yet listed — pending"; row "$ID" upload-pending "$OUT" "rc=6" ;;
    2) log "  VALIDATOR BLOCKED the upload"; row "$ID" upload-blocked "$OUT" "rc=2" ;;
    *) log "  UPLOAD FAILED rc=$URC"; row "$ID" upload-failed "$OUT" "rc=$URC" ;;
  esac
done
log "=== regate-stranded complete"
