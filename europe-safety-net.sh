#!/bin/bash
# Catch europe when build-refresh-queue.sh throws it away over a cosmetic crash.
#
# create_osm_zim.py's main() had a local `import shutil` (line 7202) that made
# `shutil` a local for the whole function, so the `finally: shutil.rmtree()`
# cleanup raised UnboundLocalError AFTER a fully successful build and exited
# non-zero. build-refresh-queue.sh:282 reads `BUILD_RC -ne 0` as BUILD FAILED,
# logs it, and `continue`s — it does NOT delete the ZIM. Switzerland-nosat hit
# this on 2026-09-18 and its ZIM was perfectly valid (97,772 entries,
# check()==True).
#
# The source is fixed, but europe's python (pid 3420711) loaded the module
# two days ago and still runs the buggy bytecode, and the queue bash likewise
# reads its own old inode — so europe WILL be marked build-failed. This script
# waits for the pack to finish, verifies the ZIM independently, and runs the
# normal gates + upload so a two-day build isn't lost.
#
# Usage: setsid nohup ./europe-safety-net.sh > europe-safety-net.out 2>&1 &
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
PY=/storage/streetzim/venv-linux/bin/python3
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
PORT="${PORT:-8831}"
KIWIX_PORT="${KIWIX_PORT:-8832}"
ID=europe
ZIM=osm-europe-2026-09-16.zim
LOG=/storage/streetzim/europe-safety-net.log
LOCK=/storage/streetzim/.retrofit-upload.lock
UPLOAD="${UPLOAD:-0}"   # default: gate only. Set UPLOAD=1 to ship.

log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" | tee -a "$LOG"; }

# Identify the real worker by interpreter+script, never by a loose pattern
# (a bare `pgrep -f` matches this script and bash wrappers).
pack_running(){
  for p in $(pgrep -f streetzim-pack 2>/dev/null); do
    [ -r "/proc/$p/cmdline" ] || continue
    case "$(tr '\0' ' ' < "/proc/$p/cmdline")" in
      *streetzim-pack\ *europe*) return 0 ;;
    esac
  done
  return 1
}
build_running(){
  for p in $(pgrep -f create_osm_zim.py 2>/dev/null); do
    [ -r "/proc/$p/cmdline" ] || continue
    case "$(tr '\0' ' ' < "/proc/$p/cmdline")" in
      *python3*create_osm_zim.py*europe*) return 0 ;;
    esac
  done
  return 1
}

log "watching europe; pack_running=$(pack_running && echo yes || echo no)"
while pack_running || build_running; do sleep 120; done
log "europe build/pack finished"

sleep 60   # let the process actually exit and flush
[ -s "$ZIM" ] || { log "no $ZIM on disk — nothing to rescue"; exit 1; }
log "$ZIM present: $(du -h "$ZIM" | cut -f1)"

# Independent structural check first: if the ZIM is genuinely broken the
# queue was right and we must not ship it.
"$PY" - "$ZIM" <<'PYEOF' 2>&1 | tee -a "$LOG"
import sys
from libzim.reader import Archive
a = Archive(sys.argv[1])
print(f"  entries={a.all_entry_count} checksum_ok={a.check()}")
sys.exit(0 if a.check() else 1)
PYEOF
[ "${PIPESTATUS[0]}" = 0 ] || { log "ZIM FAILED its own checksum — leaving it alone"; exit 2; }

REG=$(awk -F'\t' -v id="$ID" '$1==id' cloud/regions.tsv | head -1)
BBOX=$(echo "$REG" | cut -f3); RSRC=$(echo "$REG" | cut -f5)
RDST=$(echo "$REG" | cut -f6); SEARCH=$(echo "$REG" | cut -f7)
log "gates: bbox=$BBOX route=$RSRC -> $RDST search='$SEARCH'"

HELP=$(mktemp "$TMPDIR/eunet.XXXXXX.sh")
sed -n '/^smoke_find()/,/^}/p;/^smoke_search()/,/^}/p;/^browser_smoke()/,/^}/p' \
    retrofit-chips-queue.sh > "$HELP"
. "$HELP"; trap 'rm -f "$HELP"' EXIT

G=""
_tsz=$(( $(stat -c%s "$ZIM") / 1073741824 )); _tto=$(( 900 + _tsz * 300 ))
[ "$_tto" -gt 14400 ] && _tto=14400
timeout "$_tto" nice -n 10 "$PY" cloud/check_terrain_coverage.py --zooms 10-12 -- "$ZIM" "$BBOX" >> "$LOG" 2>&1 \
  && log "  terrain: OK" || { G="$G terrain"; log "  terrain: FAIL"; }
TERRAIN_STRIPE_TOLERATE=10 timeout 14400 "$PY" cloud/validate_zim.py "$ZIM" >> "$LOG" 2>&1 \
  && log "  validate: OK" || { G="$G validate"; log "  validate: FAIL"; }
RO=$(timeout 3600 "$PY" cloud/route_cli.py --zim="$ZIM" --src="$RSRC" --dst="$RDST" --mode=astar --max-pops=5000000 2>&1)
echo "$RO" | grep -q "route OK" && log "  routing: OK" || { G="$G routing"; log "  routing: FAIL"; }
SN=$(smoke_search "$ZIM" "$SEARCH"); FN=$(smoke_find "$ZIM")
[ "${SN:-0}" -ge 1 ] 2>/dev/null && log "  search('$SEARCH'): $SN" || { G="$G search"; log "  search: FAIL"; }
[ "${FN:-0}" -ge 1 ] 2>/dev/null && log "  find: $FN" || { G="$G find"; log "  find: FAIL"; }
browser_smoke "$ZIM" "$RSRC" "$RDST" "$SEARCH" "/storage/streetzim/europe-safetynet-smoke.log" \
  && log "  browser (web shell): OK" || { G="$G browser"; log "  browser: FAIL"; }
timeout 2400 bash cloud/kiwix_viewer_gate.sh "$ZIM" "$KIWIX_PORT" "$SEARCH" >> "$LOG" 2>&1 \
  && log "  kiwix (in-ZIM viewer): OK" || { G="$G kiwix"; log "  kiwix: FAIL"; }

if [ -n "$G" ]; then log "GATES FAILED:$G — europe NOT shipped, artifact kept"; exit 3; fi
log "all gates passed"
if [ "$UPLOAD" != 1 ]; then log "UPLOAD=0 — stopping before upload (rerun with UPLOAD=1)"; exit 0; fi
flock "$LOCK" env PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
  bash cloud/upload_validated.sh "$ID" "$ZIM" >> "$LOG" 2>&1
log "upload rc=$?"
