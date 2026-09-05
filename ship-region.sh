#!/usr/bin/env bash
# Build ONE region from the refreshed data, gate it, and upload it.
#
# Deliberately uses the proven pieces (build-region-fast.sh + the gate
# sequence that shipped Carolinas in .carolinas-gated-ship.sh) rather
# than build-refresh-queue.sh, so a single region can ship while the
# queue is still under review.
#
# Usage: ./ship-region.sh <id> [--no-upload]
#   Region id must exist in cloud/regions.tsv (bbox, smoke pair and
#   search term all come from there).
# Env: OVERTURE_RELEASE (default 2026-08-19.0)
#      WAIT_FOR_PBF=1  block until the extractor has finished this
#                      region's PBF instead of failing
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
export OVERTURE_RELEASE="${OVERTURE_RELEASE:-2026-08-19.0}"
# 403/429/5xx/timeouts in the liveness cache are mostly bot-blocked live
# sites, not closed businesses — only these statuses drop a record.
# `parked` (redirects to a domain-squatter page) and `url` (syntactically
# invalid) are the strongest evidence a business is gone, so they stay in
# the drop set alongside the hard-dead statuses.
export STREETZIM_URL_DEAD_STATUSES="${STREETZIM_URL_DEAD_STATUSES:-404,410,dns,parked,url}"

ID="${1:?region id required}"; shift || true
UPLOAD=1
[ "${1:-}" = "--no-upload" ] && UPLOAD=0
PY=/storage/streetzim/venv-linux/bin/python3
# The smoke symlinks the ZIM into web/ and starts a local server. If we die
# in between, that symlink is left where `web/generate.py --deploy` would
# hand a multi-GB file to Firebase, and the server keeps the port bound.
SMOKE_LINK=""; SMOKE_HTTP=""
cleanup() {
  [ -n "$SMOKE_HTTP" ] && kill "$SMOKE_HTTP" 2>/dev/null
  [ -n "$SMOKE_LINK" ] && rm -f "$SMOKE_LINK"
  return 0
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
PLANET=/storage/streetzim/world-data/planet-2026-08-31.osm.pbf
TODAY=$(date +%Y-%m-%d)
LOG=/storage/streetzim/ship-${ID}-${TODAY}.log
log() { printf "[%s] %s\n" "$(date -Iseconds)" "$*" | tee -a "$LOG"; }

# IFS=tab is required: names contain spaces ("The Carolinas").
IFS=$'\t' read -r RID NAME BBOX TIER SRC DST SEARCH NOTES < <(
  awk -F'\t' -v id="$ID" '$1==id {print; exit}' cloud/regions.tsv)
[ "${RID:-}" = "$ID" ] || { echo "no such region in cloud/regions.tsv: $ID" >&2; exit 2; }
log "=== ship $ID ($NAME) bbox=$BBOX overture=$OVERTURE_RELEASE"

PBF=world-data/regions/${ID}.osm.pbf
if [ "${WAIT_FOR_PBF:-0}" = "1" ]; then
  while [ ! -f "$PBF" ] || [ -L "$PBF" ] || [ ! "$PBF" -nt "$PLANET" ] || [ -f "$PBF.part" ]; do
    log "waiting for a fresh $PBF from the extractor..."
    sleep 120
  done
fi
[ -f "$PBF" ] && [ ! -L "$PBF" ] && [ "$PBF" -nt "$PLANET" ] || {
  log "FATAL: $PBF is missing, a symlink, or older than the planet"; exit 1; }
log "PBF: $(du -h "$PBF" | cut -f1)"

for theme in addresses places; do
  PQ="overture_cache/${theme}-${ID}-${OVERTURE_RELEASE}.parquet"
  if [ ! -s "$PQ" ]; then
    log "downloading Overture $theme $OVERTURE_RELEASE"
    "$PY" download_overture_data.py "$theme" --bbox="$BBOX" \
      --release "$OVERTURE_RELEASE" --out "$PQ" >> "$LOG" 2>&1 \
      || { log "FATAL: Overture $theme download failed"; rm -f "$PQ"; exit 1; }
  fi
  log "overture $theme: $(du -h "$PQ" | cut -f1)"
done

T0=$(date +%s)
# build-region-fast.sh exits 0 with "ALREADY EXISTS" if today's ZIM is
# present, so a re-run after a gate failure would silently re-gate the
# unfixed artefact. FORCE_REBUILD=1 clears it first.
[ "${FORCE_REBUILD:-0}" = "1" ] && rm -f "osm-${ID}-${TODAY}.zim"
touch "$TMPDIR/.ship-t0-$ID"
log "building (this is the long one) — tail ${ID}-rebuild-${TODAY}.log"
bash build-region-fast.sh "$ID" "$BBOX" "$NAME" > "${ID}-build.out" 2>&1
RC=$?
ZIM="osm-${ID}-${TODAY}.zim"
[ -s "$ZIM" ] || ZIM=$(ls -t osm-${ID}-20??-??-??.zim 2>/dev/null | head -1)
MIN=$(( ($(date +%s) - T0) / 60 ))
if [ "$RC" -ne 0 ] || [ -z "$ZIM" ] || [ ! -s "$ZIM" ] || [ ! "$ZIM" -nt "$TMPDIR/.ship-t0-$ID" ]; then
  log "BUILD FAILED rc=$RC zim=${ZIM:-none} after ${MIN} min — see ${ID}-build.out"
  exit 3
fi
log "built $ZIM ($(du -h "$ZIM" | cut -f1)) in ${MIN} min"

FAILED=""
log "GATE 1/5 terrain coverage"
timeout 900 "$PY" cloud/check_terrain_coverage.py --zooms 10-12 -- "$ZIM" "$BBOX" >> "$LOG" 2>&1 \
  && log "  terrain OK" || { FAILED="$FAILED terrain"; log "  terrain FAIL"; }

log "GATE 2/5 validator"
TERRAIN_STRIPE_TOLERATE=10 timeout 1800 "$PY" cloud/validate_zim.py "$ZIM" >> "$LOG" 2>&1 \
  && log "  validate OK" || { FAILED="$FAILED validate"; log "  validate FAIL"; }

log "GATE 3/5 live routing $SRC -> $DST"
RO=$(timeout 2400 "$PY" cloud/route_cli.py --zim="$ZIM" --src="$SRC" --dst="$DST" --mode=all --max-pops=5000000 2>&1)
echo "$RO" | tail -20 >> "$LOG"
# --mode=all runs astar + hwy2 and prints "route OK" per mode; requiring
# both means one working mode cannot mask a broken graph.
NOK=$(echo "$RO" | grep -c "route OK")
[ "${NOK:-0}" -ge 2 ] && log "  routing OK (both modes)" \
  || { FAILED="$FAILED routing"; log "  routing FAIL (only ${NOK:-0}/2 modes routed)"; }

log "GATE 4/5 search '$SEARCH' + find chips"
SN=$("$PY" - "$ZIM" "$SEARCH" <<'PYEOF' 2>/dev/null || echo 0
import sys
from libzim.reader import Archive
from libzim.search import Query, Searcher
print(Searcher(Archive(sys.argv[1])).search(Query().set_query(sys.argv[2])).getEstimatedMatches())
PYEOF
)
FN=$("$PY" - "$ZIM" <<'PYEOF' 2>/dev/null || echo 0
import sys, json
from libzim.reader import Archive
a = Archive(sys.argv[1]); total = 0
for c1 in '0123456789abcdef':
    for c2 in [''] + list('0123456789abcdef'):
        try:
            e = a.get_entry_by_path(f'category-index/chip-restaurants-{c1}{c2}.json')
            total += len(json.loads(bytes(e.get_item().content).decode()))
        except Exception: pass
if total == 0:
    try:
        e = a.get_entry_by_path('category-index/chip-restaurants.json')
        total = len(json.loads(bytes(e.get_item().content).decode()))
    except Exception: pass
print(total)
PYEOF
)
case "$SN" in ''|*[!0-9]*) SN=0 ;; esac
case "$FN" in ''|*[!0-9]*) FN=0 ;; esac
[ "$SN" -ge 1 ] && log "  search OK ($SN hits)" || { FAILED="$FAILED search"; log "  search FAIL"; }
[ "$FN" -ge 1 ] && log "  find OK ($FN restaurant records)" || { FAILED="$FAILED find"; log "  find FAIL"; }

log "GATE 5/5 real-browser smoke"
export PATH=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin:$PATH
export CHROME_PATH=/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
PORT=$(( 8810 + RANDOM % 80 ))
SMOKE_LINK="web/$ZIM"
ln -sfn "../$ZIM" "$SMOKE_LINK"
"$PY" scripts/serve-web-local.py "$PORT" /storage/streetzim/web > "$TMPDIR/serve-$PORT.log" 2>&1 &
HTTP=$!; SMOKE_HTTP=$HTTP
sleep 2
STREETZIM_SITE="http://localhost:$PORT" ZIM_URL="http://localhost:$PORT/$ZIM" \
  SMOKE_ROUTE="$SRC;$DST" timeout 900 node cloud/pwa_smoke_test.mjs \
  > "${ID}-smoke-${TODAY}.log" 2>&1
SMOKE=$?
kill "$HTTP" 2>/dev/null; wait "$HTTP" 2>/dev/null; SMOKE_HTTP=""
rm -f "$SMOKE_LINK"; SMOKE_LINK=""
grep -aE '\[(PASS|FAIL)\]' "${ID}-smoke-${TODAY}.log" | sed 's/^/    /' >> "$LOG"
[ $SMOKE -eq 0 ] && log "  browser smoke OK" || { FAILED="$FAILED browser"; log "  browser smoke FAIL (${ID}-smoke-${TODAY}.log)"; }

if [ -n "$FAILED" ]; then
  log "=== GATES FAILED:$FAILED — NOT uploading $ZIM"
  exit 4
fi
log "=== all gates passed"
if [ "$UPLOAD" -eq 0 ]; then log "--no-upload: stopping here with $ZIM"; exit 0; fi

log "uploading to archive.org/details/streetzim-$ID"
if PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
     bash cloud/upload_validated.sh "$ID" "$ZIM" >> "$LOG" 2>&1; then
  log "=== SHIPPED $ZIM → https://archive.org/details/streetzim-$ID"
  # The build ran with --keep-temp so a failed build leaves its scratch
  # for inspection. Nothing ever reuses it (there is no resume path), so
  # once the region has shipped it is just ~20-120 GB of dead weight.
  # Delete exactly the directory THIS build reported ("Temp files kept
  # at: …" is printed by create_osm_zim --keep-temp and lands in the
  # build log). A "newest osm_zim_* dir" search could pick another
  # region's live scratch when the queue and ship-region.sh overlap.
  TD=$(grep -a '^Temp files kept at: ' "${ID}-rebuild-${TODAY}.log" 2>/dev/null | tail -1 | sed 's/^Temp files kept at: //')
  case "$TD" in
    "$TMPDIR"/osm_zim_*)
      if [ -d "$TD" ]; then
        log "reclaiming build scratch $TD ($(du -sh "$TD" 2>/dev/null | cut -f1))"
        ionice -c3 nice -n 19 rm -rf "$TD"
      fi ;;
    *) log "scratch dir not identified from the build log — leaving it" ;;
  esac
else
  log "=== UPLOAD FAILED (ZIM kept: $ZIM)"; exit 5
fi
