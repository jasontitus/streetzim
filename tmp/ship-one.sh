#!/bin/bash
# Re-swap ONE region with the safe-area-guarded viewer (49672cc) and ship it.
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
PY=/storage/streetzim/venv-linux/bin/python3
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
export CHROME_PATH=/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
ID="${1:?region}"; SRC="${2:?src zim}"
PORT=8871; KIWIX_PORT=8872
OUT="osm-${ID}-$(date +%Y-%m-%d).zim"
LOG=/storage/streetzim/ship-one.log
log(){ echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
[ -e "$OUT" ] && { log "$OUT exists"; exit 1; }
log "=== $ID: $SRC -> $OUT (safe-area-guarded viewer)"
"$PY" -u cloud/swap_viewer_rust.py "$SRC" "$OUT" >> "$LOG" 2>&1 || { log "SWAP FAILED"; exit 1; }
log "swap done $(du -h "$OUT"|cut -f1)"
# the ZIM must carry the GUARD, not just the merged viewer
"$PY" - "$OUT" <<'PYEOF' 2>&1 | tee -a "$LOG"
import sys
from libzim.reader import Archive
b = bytes(Archive(sys.argv[1]).get_entry_by_path("index.html").get_item().content)
ok = (b"--top-inset: 0px" in b and b"sz-standalone" in b
      and b"retryWikiGeoIndex" in b and b'href="/drive/?picker=1"' not in b)
print(f"  in-ZIM viewer {len(b)} B  safe-area-guarded={ok}")
sys.exit(0 if ok else 1)
PYEOF
[ "${PIPESTATUS[0]}" = 0 ] || { log "ZIM lacks the guard — not shipping"; exit 2; }
REG=$(awk -F'\t' -v id="$ID" '$1==id' cloud/regions.tsv | head -1)
BBOX=$(echo "$REG"|cut -f3); RSRC=$(echo "$REG"|cut -f5); RDST=$(echo "$REG"|cut -f6); SEARCH=$(echo "$REG"|cut -f7)
HELP=$(mktemp "$TMPDIR/s1.XXXXXX.sh")
sed -n '/^smoke_find()/,/^}/p;/^smoke_search()/,/^}/p;/^browser_smoke()/,/^}/p' retrofit-chips-queue.sh > "$HELP"
. "$HELP"; trap 'rm -f "$HELP"' EXIT
G=""
TERRAIN_STRIPE_TOLERATE=10 timeout 7200 "$PY" cloud/validate_zim.py "$OUT" >> "$LOG" 2>&1 && log "  validate OK" || { G="$G validate"; log "  validate FAIL"; }
RO=$(timeout 2400 "$PY" cloud/route_cli.py --zim="$OUT" --src="$RSRC" --dst="$RDST" --mode=astar --max-pops=5000000 2>&1)
echo "$RO"|grep -q "route OK" && log "  routing OK" || { G="$G routing"; log "  routing FAIL"; }
SN=$(smoke_search "$OUT" "$SEARCH"); FN=$(smoke_find "$OUT")
[ "${SN:-0}" -ge 1 ] 2>/dev/null && log "  search $SN" || { G="$G search"; log "  search FAIL"; }
[ "${FN:-0}" -ge 1 ] 2>/dev/null && log "  find $FN" || { G="$G find"; log "  find FAIL"; }
browser_smoke "$OUT" "$RSRC" "$RDST" "$SEARCH" "/storage/streetzim/${ID}-shipone-smoke.log" && log "  browser OK" || { G="$G browser"; log "  browser FAIL"; }
timeout 2400 bash cloud/kiwix_viewer_gate.sh "$OUT" "$KIWIX_PORT" "$SEARCH" >> "$LOG" 2>&1 && log "  kiwix OK" || { G="$G kiwix"; log "  kiwix FAIL"; }
[ -n "$G" ] && { log "GATES FAILED:$G — not uploading"; exit 3; }
log "all gates passed — uploading"
flock /storage/streetzim/.retrofit-upload.lock env PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
  bash cloud/upload_validated.sh "$ID" "$OUT" >> "$LOG" 2>&1
rc=$?
case $rc in
  0) log "SHIPPED https://archive.org/download/streetzim-$ID/$OUT" ;;
  6) log "uploaded, listing pending: https://archive.org/download/streetzim-$ID/$OUT" ;;
  *) log "UPLOAD FAILED rc=$rc" ;;
esac
