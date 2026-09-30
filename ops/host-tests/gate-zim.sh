#!/usr/bin/env bash
# gate-zim.sh <zim> <region id> : ship-region.sh's five gates (lines copied
# verbatim from ops/ship-region.sh at d5c32b6), on a ZIM built elsewhere,
# run in the test clone (its own web/), no build and no upload.
. "$HOME/sz-env.sh" || exit
cd /storage/streetzim/sz-tests/next || exit
Z=$(readlink -f "$1"); ID=$2; TODAY=gate-$(date +%H%M)
IFS=$'\t' read -r _ NAME BBOX _ SRC DST SEARCH _ < <(grep -P "^$ID\t" /storage/streetzim/cloud/regions.tsv)
# Overrides for a region not in regions.tsv (or a pair that fits a smaller area).
BBOX=${GATE_BBOX:-$BBOX}; SRC=${GATE_SRC:-$SRC}; DST=${GATE_DST:-$DST}; SEARCH=${GATE_SEARCH:-$SEARCH}
ln -sfn "$Z" "./$(basename "$Z")"; ZIM=$(basename "$Z")
LOG=/storage/streetzim/sz-tests/results/gate-$ID-$TODAY.log; : > "$LOG"
log() { echo "[$(date -Is)] $*" | tee -a "$LOG"; }
log "gating $Z ($ID: $SRC -> $DST, search '$SEARCH')"
FAILED=""
if [ "${GATE_SKIP_TERRAIN:-0}" = 1 ]; then log "GATE 1/5 terrain coverage: SKIPPED (GATE_SKIP_TERRAIN=1: the ZIM has no terrain layer)"; else
log "GATE 1/5 terrain coverage"
timeout 900 "$PY" cloud/check_terrain_coverage.py --vrt /storage/streetzim/terrain_cache/dem_sources/comprehensive.vrt --zooms 10-12 -- "$ZIM" "$BBOX" >> "$LOG" 2>&1 \
  && log "  terrain OK" || { FAILED="$FAILED terrain"; log "  terrain FAIL"; }

fi
log "GATE 2/5 validator"
TERRAIN_STRIPE_TOLERATE=10 timeout 1800 "$PY" cloud/validate_zim.py "$ZIM" >> "$LOG" 2>&1 \
  && log "  validate OK" || { FAILED="$FAILED validate"; log "  validate FAIL"; }

log "GATE 3/5 live routing $SRC -> $DST"
# A* is the engine the viewer runs, so it is the gate. The hwy2 two-pass
# is an optimisation that legitimately finds nothing where a region has no
# motorway-tier corridor between the pair (Iceland's ring road is
# trunk/primary; Hawaii's Kailua end has no highway entry; West Asia spent
# 638 s proving it before failing). Report it, don't gate on it — and
# don't pay for it: --mode=astar keeps the gate to seconds.
RO=$(timeout 2400 "$PY" cloud/route_cli.py --zim="$ZIM" --src="$SRC" --dst="$DST" --mode=astar --max-pops=5000000 2>&1)
echo "$RO" | tail -20 >> "$LOG"
if echo "$RO" | grep -q "route OK"; then
  log "  routing OK ($(echo "$RO" | grep -oE 'distance: [0-9.]+ km' | head -1))"
else
  FAILED="$FAILED routing"; log "  routing FAIL (A* found no route)"
fi

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
def n(p):
    return len(json.loads(bytes(a.get_entry_by_path(p).get_item().content).decode()))
# The manifest names the files: geo shards (chip-food-g000.json…,
# cloud/chip_shards.py) or name-hash buckets. A listed file that is missing
# or unreadable counts the chip as 0, which fails the gate.
# Chip id: "food" since the 2026-09-16 Restaurants+Cafés merge, "restaurants"
# on every ZIM built before it — naming one would fail the gate on the other.
cid = 'food'
try:
    chips = json.loads(bytes(a.get_entry_by_path(
        'category-index/manifest.json').get_item().content))['chips']
    cid = 'food' if 'food' in chips else 'restaurants'
    meta = chips[cid]
    subs = meta.get('sub_chunks') or []
    total = (sum(n(f'category-index/chip-{cid}-{s}.json') for s in subs)
             if subs else n(f'category-index/chip-{cid}.json'))
except Exception: total = 0
# Fallbacks for a manifest that couldn't be read: try both chip ids, or a
# gate on a merged ZIM counts 0 and blocks the ship.
for fid in ('food', 'restaurants'):
    if total:
        break
    for c1 in '0123456789abcdef':
        for c2 in [''] + list('0123456789abcdef'):
            try:
                total += n(f'category-index/chip-{fid}-{c1}{c2}.json')
            except Exception: pass
for fid in ('food', 'restaurants'):
    if total:
        break
    try:
        total = n(f'category-index/chip-{fid}.json')
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
"$PY" scripts/serve-web-local.py "$PORT" /storage/streetzim/sz-tests/next/web > "$TMPDIR/serve-$PORT.log" 2>&1 &
HTTP=$!; SMOKE_HTTP=$HTTP
sleep 2
# ZIM_FILE hands the SW the ZIM as an on-disk File, exactly as the picker
# does for a real user. Without it the smoke fetches the URL and calls
# resp.blob(), making Chrome materialise the whole ZIM — that fails outright
# at 12.4 GB, which is why east-coast-us could never clear this gate.
STREETZIM_SITE="http://localhost:$PORT" ZIM_URL="http://localhost:$PORT/$ZIM" \
  ZIM_FILE="$Z" \
  SMOKE_ROUTE="$SRC;$DST" SMOKE_SEARCH="$SEARCH" timeout 900 node cloud/pwa_smoke_test.mjs \
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
rm -f "./$ZIM"
