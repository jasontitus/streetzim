#!/usr/bin/env bash
# The round's own markers() and gate() (ops/cloud/rebuild_old_regions.sh, extracted verbatim),
# run on a test ZIM. Ports 9661/9662 (the round uses 9641/9642). Writes under $SZT only.
# usage: run-driver-gates.sh <zim> <search term>
cd /storage/streetzim
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
PY=/storage/streetzim/venv-linux/bin/python3
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
KS=$(readlink -f /storage/streetzim/tools/kiwix-tools_*/kiwix-serve)
TMPDIR=/storage/streetzim/sz-tests/tmp; export TMPDIR
LOG=/storage/streetzim/sz-tests/results/driver-gates-$(basename "$1" .zim).log
OV=9661; KW=9662
log(){ echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }
markers(){ "$PY" - "$1" <<'PYEOF'
import sys
from libzim.reader import Archive
a = Archive(sys.argv[1])
idx = bytes(a.get_entry_by_path("index.html").get_item().content)
checks = {
    "safe-area shim":  b"Kiwix iOS safe-area shim" in idx,
    "popup z-index 4": b".maplibregl-popup { z-index: 4; }" in idx,
    "chip touch-act":  b"touch-action: pan-x;" in idx,
    "popup gap":       b"_szPopupGap" in idx,
    "no diag panel":   b"sz-chip-diag" not in idx and b"sz-safearea-diag" not in idx,
    # Every shipped region carries a Wikipedia geo-index. The build-region.sh
    # builds did not, and it took the browser gate -- after hours of building
    # -- to notice. One lookup here catches it in a second.
    "wiki geo-index":  a.has_entry_by_path("wiki-geo-index.json") and len(
        bytes(a.get_entry_by_path("wiki-geo-index.json").get_item().content)) > 2,
    # create_osm_zim.py pads the viewer into fixed uncompressed slots as of
    # 2026-09-21. Without them a region is born owing a full re-pack at the
    # next viewer change, so fail the build rather than discover it later.
    "slot marker idx": b"SZVSLOT1:index.html" in idx,
    "slot marker plc": b"SZVSLOT1:places.html" in bytes(
        a.get_entry_by_path("places.html").get_item().content),
    "archive check":   a.check(),
}
for k, v in checks.items(): print(f"    {k:18s} {v}")
sys.exit(0 if all(checks.values()) else 1)
PYEOF
}

gate(){  # $1=zim $2=term
  local Z=$1 TERM=$2 B G="" VW q c up=0 i
  B=$(basename "$Z" .zim)
  if ss -ltn 2>/dev/null | grep -q ":$OV "; then log "  port $OV busy — NOT a geometry failure"; return 1; fi
  setsid nohup "$KS" --port "$OV" "$Z" > "$TMPDIR/ec-$B.log" 2>&1 < /dev/null &
  for i in $(seq 1 150); do sleep 2
    [ "$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://localhost:$OV/content/$B/index.html")" = 200 ] && { up=1; break; }
  done
  if [ "$up" != 1 ]; then
    log "  kiwix-serve never came up — NOT a geometry failure"
    for q in $(pgrep -x kiwix-serve); do c=$(tr '\0' ' ' < /proc/$q/cmdline); case "$c" in *"--port $OV"*) kill $q;; esac; done
    return 1
  fi
  for VW in 320 390 430; do
    ZIM_ORIGIN="http://localhost:$OV/content/$B" VW=$VW timeout 400 "$NODE" tmp/overlap-check.mjs > "$TMPDIR/ec-$B-$VW.txt" 2>&1
    grep -q "^NO OVERLAPS" "$TMPDIR/ec-$B-$VW.txt" || { G="$G overlap@$VW"; log "    overlaps @ ${VW}px"; }
  done
  SEARCH_TERM="$TERM" ZIM_ORIGIN="http://localhost:$OV/content/$B" \
    timeout 1800 "$NODE" tmp/device-matrix.mjs > "$TMPDIR/ec-$B-matrix.txt" 2>&1
  if grep -q "^all 7 devices passed" "$TMPDIR/ec-$B-matrix.txt"; then log "  device matrix: 7/7 PASS"
  else G="$G device-matrix"; log "  device matrix FAILED:"
       grep -E "^ +!" "$TMPDIR/ec-$B-matrix.txt" | sort -u | head -4 | while read -r l; do log "      $l"; done; fi
  # Render gate: the device matrix waits for the canvas ELEMENT and would
  # pass a blank map. This asserts the style loaded, the tile source loaded,
  # and real features were drawn.
  local MH
  MH=$(TAG="$B" ZIM_ORIGIN="http://localhost:$OV/content/$B" timeout 200 "$NODE" tmp/map-health.mjs 2>&1 | grep '^{' | tail -1)
  if echo "$MH" | "$PY" -c 'import sys,json
d=json.loads(sys.stdin.read() or "{}")
src=d.get("sourceLoaded") or {}
ok=d.get("styleLoaded") and src and all(v is True for v in src.values()) and (d.get("renderedFeatures") or 0)>=100 and not d.get("errors")
sys.exit(0 if ok else 1)' 2>/dev/null; then
    log "  render gate: OK ($(echo "$MH" | grep -oE '"renderedFeatures":[0-9]+'))"
  else G="$G render"; log "  render gate: FAIL $(echo "$MH" | head -c 200)"; fi
  for q in $(pgrep -x kiwix-serve); do c=$(tr '\0' ' ' < /proc/$q/cmdline); case "$c" in *"--port $OV"*) kill $q;; esac; done
  timeout 2400 bash cloud/kiwix_viewer_gate.sh "$Z" "$KW" "$TERM" >> "$LOG" 2>&1 \
    && log "  kiwix in-ZIM gate: OK" || { G="$G kiwix"; log "  kiwix in-ZIM gate: FAIL"; }
  [ -z "$G" ] && return 0
  log "  GATES FAILED:$G"; return 1
}
log "=== driver gates on $1 (term: $2)"
M=0; if markers "$1" >> "$LOG" 2>&1; then log "  markers OK"; else log "  MARKERS FAILED"; M=1; fi
# A failed marker check fails the run too (it used to be logged and then
# ignored in the final line and the exit status).
if gate "$1" "$2" && [ $M = 0 ]; then log "  ALL DRIVER GATES PASSED"; else log "  DRIVER GATES FAILED$( [ $M = 1 ] && echo ' (markers)')"; exit 1; fi
