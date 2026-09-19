#!/bin/bash
# Swap the layout-fixed viewer in, PROVE no overlaps, screenshot, gate, ship.
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
PY=/storage/streetzim/venv-linux/bin/python3
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
export CHROME_PATH=/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
ID=switzerland; SRC=osm-switzerland-2026-09-18.zim; OUT=osm-switzerland-2026-09-19c.zim
LOG=/storage/streetzim/layout-ship.log
KS=$(readlink -f tools/kiwix-tools_*/kiwix-serve)
log(){ echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
[ -e "$OUT" ] && { log "$OUT exists"; exit 1; }

log "=== swap layout-fixed viewer -> $OUT"
"$PY" -u cloud/swap_viewer_rust.py "$SRC" "$OUT" >> "$LOG" 2>&1 || { log "SWAP FAILED"; exit 1; }
log "swap done $(du -h "$OUT"|cut -f1)"

"$PY" - "$OUT" <<'PYEOF' 2>&1 | tee -a "$LOG"
import sys
from libzim.reader import Archive
b = bytes(Archive(sys.argv[1]).get_entry_by_path("index.html").get_item().content)
checks = {
  "safe-area guard":   b"--top-inset: 0px" in b and b"sz-standalone" in b,
  "search gutter 128": b"calc(100% - 128px)" in b,
  "controls at 152":   b"152px + var(--top-inset" in b,
  "info at 84":        b"84px + var(--safe-bottom, 0px)); left: 10px" in b,
  "attr-btn at 58":    b"58px + var(--safe-bottom, 0px)); right: 64px" in b,
  "scale bar at 48":   b"48px + var(--safe-bottom, 0px)); }" in b,
  "wiki retry":        b"retryWikiGeoIndex" in b,
  "no dead /drive/":   b'href="/drive/?picker=1"' not in b,
}
for k, v in checks.items(): print(f"  {k:20s} {v}")
sys.exit(0 if all(checks.values()) else 1)
PYEOF
[ "${PIPESTATUS[0]}" = 0 ] || { log "ZIM missing a fix — not shipping"; exit 2; }

# HARD GATE: zero overlaps at three widths, else stop.
BOOK=$(basename "$OUT" .zim)
setsid nohup "$KS" --port 8877 "$OUT" > tmp/lay-serve.log 2>&1 < /dev/null &
for _ in $(seq 1 60); do sleep 2
  [ "$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://localhost:8877/content/$BOOK/index.html")" = 200 ] && break; done
BAD=0
for vw in 320 390 430; do
  log "--- overlap check @ ${vw}px"
  ZIM_ORIGIN="http://localhost:8877/content/$BOOK" VW=$vw timeout 240 "$NODE" tmp/overlap-check.mjs > "tmp/ov-$vw.txt" 2>&1
  if grep -q "^NO OVERLAPS" "tmp/ov-$vw.txt"; then log "    ${vw}px: NO OVERLAPS"
  else log "    ${vw}px: OVERLAPS REMAIN"; grep -A8 "^OVERLAPS:" "tmp/ov-$vw.txt" | head -9 | tee -a "$LOG"; BAD=1; fi
done
ZIM_ORIGIN="http://localhost:8877/content/$BOOK" \
  SHOT_FIXED=/storage/streetzim/tmp/shot-layout-final.png \
  SHOT_SIM=/storage/streetzim/tmp/shot-layout-final-sim.png \
  timeout 300 "$NODE" tmp/kiwix-shot.mjs >> "$LOG" 2>&1
for p in $(pgrep -f "kiwix-serve --port 8877"); do kill $p 2>/dev/null; done
[ "$BAD" = 0 ] || { log "LAYOUT STILL BROKEN — not gating, not shipping"; echo LAYOUT-BAD; exit 3; }
log "layout clean at 320/390/430"
echo LAYOUT-CLEAN

REG=$(awk -F'\t' -v id="$ID" '$1==id' cloud/regions.tsv | head -1)
RSRC=$(echo "$REG"|cut -f5); RDST=$(echo "$REG"|cut -f6); SEARCH=$(echo "$REG"|cut -f7)
HELP=$(mktemp "$TMPDIR/lay.XXXXXX.sh")
sed -n '/^smoke_find()/,/^}/p;/^smoke_search()/,/^}/p;/^browser_smoke()/,/^}/p' retrofit-chips-queue.sh > "$HELP"
. "$HELP"; trap 'rm -f "$HELP"' EXIT
PORT=8878; G=""
TERRAIN_STRIPE_TOLERATE=10 timeout 7200 "$PY" cloud/validate_zim.py "$OUT" >> "$LOG" 2>&1 && log "  validate OK" || { G="$G validate"; log "  validate FAIL"; }
RO=$(timeout 2400 "$PY" cloud/route_cli.py --zim="$OUT" --src="$RSRC" --dst="$RDST" --mode=astar --max-pops=5000000 2>&1)
echo "$RO"|grep -q "route OK" && log "  routing OK" || { G="$G routing"; log "  routing FAIL"; }
SN=$(smoke_search "$OUT" "$SEARCH"); FN=$(smoke_find "$OUT")
[ "${SN:-0}" -ge 1 ] 2>/dev/null && log "  search $SN" || { G="$G search"; log "  search FAIL"; }
[ "${FN:-0}" -ge 1 ] 2>/dev/null && log "  find $FN" || { G="$G find"; log "  find FAIL"; }
browser_smoke "$OUT" "$RSRC" "$RDST" "$SEARCH" "/storage/streetzim/layout-smoke.log" && log "  browser OK" || { G="$G browser"; log "  browser FAIL"; }
timeout 2400 bash cloud/kiwix_viewer_gate.sh "$OUT" 8879 "$SEARCH" >> "$LOG" 2>&1 && log "  kiwix OK" || { G="$G kiwix"; log "  kiwix FAIL"; }
[ -n "$G" ] && { log "GATES FAILED:$G"; exit 4; }
log "all gates passed — uploading"
flock /storage/streetzim/.retrofit-upload.lock env PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
  bash cloud/upload_validated.sh "$ID" "$OUT" >> "$LOG" 2>&1
rc=$?
case $rc in
  0|6) log "SHIPPED https://archive.org/download/streetzim-$ID/$OUT" ;;
  *)   log "UPLOAD FAILED rc=$rc" ;;
esac
