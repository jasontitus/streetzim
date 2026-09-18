#!/bin/bash
# Gate and ship the Light Switzerland build as its own region.
#
# Source: osm-switzerland-nosat-z13-2026-09-18.zim — no satellite, vector
# tiles capped at z13 (--max-zoom 13, which only works since 3910bfc; before
# that the flag was silently ignored for any region with mbtiles <= 5 GB).
# 1.3997 GB vs 2.0974 GB full. Verified z14 count = 0, 15,209 tiles, checksum OK.
#
# The build baked the PRE-MERGE viewer (425,345 B), so tmp/light-swap.sh swaps
# in the current merged+fixed one first — shipping a stale viewer to Kiwix is
# the exact problem the whole day was spent fixing.
#
# Uploads to a NEW item: streetzim-switzerland-light.
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
PY=/storage/streetzim/venv-linux/bin/python3
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
PORT=8841
KIWIX_PORT=8842
ID=switzerland-light
OUT=osm-switzerland-light-2026-09-19.zim
LOG=/storage/streetzim/light-ship.log
LOCK=/storage/streetzim/.retrofit-upload.lock
UPLOAD="${UPLOAD:-1}"
log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" | tee -a "$LOG"; }

log "waiting for the viewer swap"
for _ in $(seq 1 240); do
  grep -q LIGHT-SWAP-DONE tmp/light-swap.out 2>/dev/null && break
  sleep 15
done
grep -q LIGHT-SWAP-DONE tmp/light-swap.out 2>/dev/null || { log "swap never finished"; exit 1; }
[ -s "$OUT" ] || { log "no $OUT"; exit 1; }
log "swap done: $(du -h "$OUT" | cut -f1)"

# The ZIM must carry the CURRENT viewer, not the one the build baked.
"$PY" - "$OUT" <<'PYEOF' 2>&1 | tee -a "$LOG"
import sys
from libzim.reader import Archive
b = bytes(Archive(sys.argv[1]).get_entry_by_path("index.html").get_item().content)
ok = (b"retryWikiGeoIndex" in b and b"_findResolveChipDef" in b
      and b"retry.lastChild.setAttribute" in b and b'href="/drive/?picker=1"' not in b)
print(f"  in-ZIM viewer: {len(b)} B  current={ok}")
sys.exit(0 if ok else 1)
PYEOF
[ "${PIPESTATUS[0]}" = 0 ] || { log "ZIM does not carry the fixed viewer — not shipping"; exit 2; }

REG=$(awk -F'\t' -v id="$ID" '$1==id' cloud/regions.tsv | head -1)
BBOX=$(echo "$REG" | cut -f3); RSRC=$(echo "$REG" | cut -f5)
RDST=$(echo "$REG" | cut -f6); SEARCH=$(echo "$REG" | cut -f7)
log "gates: bbox=$BBOX route=$RSRC->$RDST search='$SEARCH'"

HELP=$(mktemp "$TMPDIR/lightgate.XXXXXX.sh")
sed -n '/^smoke_find()/,/^}/p;/^smoke_search()/,/^}/p;/^browser_smoke()/,/^}/p' \
    retrofit-chips-queue.sh > "$HELP"
. "$HELP"; trap 'rm -f "$HELP"' EXIT

G=""
TERRAIN_STRIPE_TOLERATE=10 timeout 7200 "$PY" cloud/validate_zim.py "$OUT" >> "$LOG" 2>&1 \
  && log "  validate: OK" || { G="$G validate"; log "  validate: FAIL"; }
timeout 3600 nice -n 10 "$PY" cloud/check_terrain_coverage.py --zooms 10-12 -- "$OUT" "$BBOX" >> "$LOG" 2>&1 \
  && log "  terrain: OK" || { G="$G terrain"; log "  terrain: FAIL"; }
RO=$(timeout 2400 "$PY" cloud/route_cli.py --zim="$OUT" --src="$RSRC" --dst="$RDST" --mode=astar --max-pops=5000000 2>&1)
echo "$RO" | grep -q "route OK" && log "  routing: OK" || { G="$G routing"; log "  routing: FAIL"; }
SN=$(smoke_search "$OUT" "$SEARCH"); FN=$(smoke_find "$OUT")
[ "${SN:-0}" -ge 1 ] 2>/dev/null && log "  search('$SEARCH'): $SN" || { G="$G search"; log "  search: FAIL"; }
[ "${FN:-0}" -ge 1 ] 2>/dev/null && log "  find: $FN" || { G="$G find"; log "  find: FAIL"; }
browser_smoke "$OUT" "$RSRC" "$RDST" "$SEARCH" "/storage/streetzim/light-smoke.log" \
  && log "  browser (web shell): OK" || { G="$G browser"; log "  browser: FAIL"; }
timeout 2400 bash cloud/kiwix_viewer_gate.sh "$OUT" "$KIWIX_PORT" "$SEARCH" >> "$LOG" 2>&1 \
  && log "  kiwix (in-ZIM viewer): OK" || { G="$G kiwix"; log "  kiwix: FAIL"; }

if [ -n "$G" ]; then log "GATES FAILED:$G — NOT uploading $OUT"; exit 3; fi
log "all gates passed"
[ "$UPLOAD" = 1 ] || { log "UPLOAD=0, stopping"; exit 0; }
log "uploading to NEW item streetzim-$ID"
flock "$LOCK" env PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
  bash cloud/upload_validated.sh "$ID" "$OUT" >> "$LOG" 2>&1
rc=$?
case $rc in
  0) log "SHIPPED https://archive.org/download/streetzim-$ID/$OUT" ;;
  6) log "uploaded, listing pending: https://archive.org/download/streetzim-$ID/$OUT" ;;
  *) log "UPLOAD FAILED rc=$rc" ;;
esac
