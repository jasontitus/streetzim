#!/bin/bash
# Canada is the one live region whose viewer is NOT in fixed slots (built
# before create_osm_zim.py started padding them, 2026-09-21), so
# patch_viewer_inplace.py cannot touch it and the rollout skips it.
#
# cloud/swap_viewer_rust.py rewrites every entry into a NEW file and, since
# 2026-09-21, pads the viewer into slots on the way out. So this one
# expensive re-pack (~3 h for 29.9 GB, vs 3 s for a patch) is also the last
# one Canada ever needs: after this it joins the fast path.
#
# It mints a NEW UUID, which is correct here -- Kiwix dedupes books by UUID,
# so a same-UUID rewrite would leave anyone who already has Canada on the old
# viewer. The output therefore also gets a new date in its filename.
#
# Runs last: after the european countries, the african regions, and the
# 425 GB viewer rollout. Waits on all three by PID from their own pidfiles
# (never pgrep -f: this script's cmdline contains every string it searches
# for -- feedback_process_scan_self_match).
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
export ZSTD_CLEVEL="${ZSTD_CLEVEL:-22}"
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
PY=/storage/streetzim/venv-linux/bin/python3
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
KS=$(readlink -f /storage/streetzim/tools/kiwix-tools_*/kiwix-serve)
LOG=/storage/streetzim/canada-repack.log
LOCK=/storage/streetzim/.retrofit-upload.lock
SRC=osm-canada-2026-09-20.zim
TERM=Toronto
OV=9631; KW=9632

log(){ echo "[$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M:%S %Z')] $*" | tee -a "$LOG"; }

echo $$ > /storage/streetzim/.canada-repack.pid

log "=== waiting for the europe queue, the africa queue and the viewer rollout"
while :; do
  busy=""
  for pf in .europe-countries.pid .africa-rest.pid .rollout-viewer.pid; do
    [ -f "$pf" ] || continue
    p=$(cat "$pf" 2>/dev/null) || continue
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null && busy="$busy $pf($p)"
  done
  [ -z "$busy" ] && break
  log "  waiting on:$busy"
  sleep 600
done
log "=== clear — starting canada re-pack"

[ -s "$SRC" ] || { log "FATAL: $SRC missing"; exit 1; }
DST=osm-canada-$(TZ=America/Los_Angeles date +%Y-%m-%d).zim
if [ "$DST" = "$SRC" ]; then DST=osm-canada-$(TZ=America/Los_Angeles date +%Y-%m-%d)b.zim; fi

# ~60 GB transient (source + destination side by side). Refuse rather than
# fill the disk out from under the other tenant.
FREE=$(df --output=avail -k /storage | tail -1)
NEED=$(( $(stat -c %s "$SRC") / 1024 * 2 ))
if [ "$FREE" -lt "$NEED" ]; then
  log "FATAL: need $((NEED/1024/1024)) GB free, have $((FREE/1024/1024)) GB"; exit 1
fi

T0=$(date +%s)
log "  swap_viewer_rust $SRC -> $DST (expect ~3 h)"
if ! "$PY" cloud/swap_viewer_rust.py "$SRC" "$DST" >> "$LOG" 2>&1; then
  log "REPACK FAILED"; rm -f "$DST"; exit 1
fi
log "  repacked in $(( ($(date +%s)-T0)/60 )) min ($(du -h "$DST" | cut -f1))"

if ! TERRAIN_STRIPE_TOLERATE=10 timeout 21600 "$PY" cloud/validate_zim.py "$DST" >> "$LOG" 2>&1; then
  log "validate: FAIL — not uploading"; exit 1
fi
log "  validate OK"

# Same assertions the rollout makes, plus the slots this re-pack exists to add.
if ! "$PY" - "$DST" >> "$LOG" 2>&1 <<'PYEOF'
import sys
from libzim.reader import Archive
a = Archive(sys.argv[1])
idx = bytes(a.get_entry_by_path("index.html").get_item().content)
plc = bytes(a.get_entry_by_path("places.html").get_item().content)
checks = {
    "safe-area shim":  b"Kiwix iOS safe-area shim" in idx,
    "popup z-index 4": b".maplibregl-popup { z-index: 4; }" in idx,
    "chip touch-act":  b"touch-action: pan-x;" in idx,
    "popup gap":       b"_szPopupGap" in idx,
    "no diag panel":   b"sz-chip-diag" not in idx and b"sz-safearea-diag" not in idx,
    "lakes layer":     b"water-lowzoom" in idx and b"_SZ_LAKES" in idx,
    "chip nearest":    b"_ranked.sort" in idx,
    # The whole point of the re-pack: Canada joins the fast path.
    "slot marker idx": b"SZVSLOT1:index.html" in idx,
    "slot marker plc": b"SZVSLOT1:places.html" in plc,
    "slot size idx":   a.get_entry_by_path("index.html").get_item().size == 1048576,
    "wiki geo-index":  a.has_entry_by_path("wiki-geo-index.json"),
    "archive check":   a.check(),
}
for k, v in checks.items(): print(f"    {k:18s} {v}")
sys.exit(0 if all(checks.values()) else 1)
PYEOF
then log "MARKERS FAILED"; exit 1; fi
log "  markers OK"

B=$(basename "$DST" .zim)
up=0
if ss -ltn 2>/dev/null | grep -q ":$OV "; then log "port $OV busy"; exit 1; fi
setsid nohup "$KS" --port "$OV" "$DST" > "$TMPDIR/ca-$B.log" 2>&1 < /dev/null &
for i in $(seq 1 150); do sleep 2
  [ "$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://localhost:$OV/content/$B/index.html")" = 200 ] && { up=1; break; }
done
[ "$up" = 1 ] || { log "kiwix-serve never came up"; exit 1; }
G=""
for VW in 320 390 430; do
  ZIM_ORIGIN="http://localhost:$OV/content/$B" VW=$VW timeout 400 "$NODE" tmp/overlap-check.mjs > "$TMPDIR/ca-$B-$VW.txt" 2>&1
  grep -q "^NO OVERLAPS" "$TMPDIR/ca-$B-$VW.txt" || { G="$G overlap@$VW"; }
done
SEARCH_TERM="$TERM" ZIM_ORIGIN="http://localhost:$OV/content/$B" \
  timeout 1800 "$NODE" tmp/device-matrix.mjs > "$TMPDIR/ca-$B-matrix.txt" 2>&1
grep -q "^all 7 devices passed" "$TMPDIR/ca-$B-matrix.txt" \
  && log "  device matrix: 7/7 PASS" || { G="$G device-matrix"; log "  device matrix FAILED"; }
MH=$(TAG="$B" ZIM_ORIGIN="http://localhost:$OV/content/$B" timeout 200 "$NODE" tmp/map-health.mjs 2>&1 | grep '^{' | tail -1)
if echo "$MH" | "$PY" -c 'import sys,json
d=json.loads(sys.stdin.read() or "{}")
src=d.get("sourceLoaded") or {}
ok=d.get("styleLoaded") and src and all(v is True for v in src.values()) and (d.get("renderedFeatures") or 0)>=100 and not d.get("errors")
sys.exit(0 if ok else 1)' 2>/dev/null; then
  log "  render gate: OK ($(echo "$MH" | grep -oE '"renderedFeatures":[0-9]+'))"
else G="$G render"; log "  render gate: FAIL $(echo "$MH" | head -c 200)"; fi
for q in $(pgrep -x kiwix-serve); do c=$(tr '\0' ' ' < /proc/$q/cmdline); case "$c" in *"--port $OV"*) kill $q;; esac; done
timeout 2400 bash cloud/kiwix_viewer_gate.sh "$DST" "$KW" "$TERM" >> "$LOG" 2>&1 \
  && log "  kiwix in-ZIM gate: OK" || { G="$G kiwix"; log "  kiwix in-ZIM gate: FAIL"; }
[ -z "$G" ] || { log "GATES FAILED:$G — not uploading"; exit 1; }

log "  uploading $DST as streetzim-canada"
flock "$LOCK" env PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
  bash cloud/upload_validated.sh canada "$DST" >> "$LOG" 2>&1
RC=$?; MIN=$(( ($(date +%s)-T0)/60 ))
case $RC in
  0) log "=== SHIPPED $DST ($MIN min)" ;;
  6) log "=== uploaded, listing pending ($MIN min)" ;;
  *) log "=== UPLOAD FAILED rc=$RC" ;;
esac
