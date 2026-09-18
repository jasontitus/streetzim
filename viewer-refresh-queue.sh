#!/bin/bash
# Re-swap the in-ZIM viewer into regions already shipped today, so native
# Kiwix users get the same viewer the website already serves them.
#
# Why: web /drive/ treats viewer/index.html, places.html and routing-worker.js
# as SHELL assets (sw.js VIEWER_SHELL_NAMES) served network-first from
# Firebase, so the site picked up today's fixes immediately. Kiwix has no
# service worker and runs the copy baked into the ZIM — which still has the
# one-shot wiki-geo-index fetch (california looked like it had no Wikipedia)
# and the pre-merge chip rail (three food chips, no Health/Landmarks/Libraries).
#
# Viewer swap only: no --reshard-chips / --reshard-search. The chips and
# search data in these ZIMs are already current; only the three viewer files
# are stale.
#
# Output is suffixed `b` because the live files already carry today's date.
# generate.py's dated-filename rule accepts the suffix, and
# cleanup_old_zims.py --keep 2 prunes the superseded upload, so nothing is
# deleted by hand.
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
PY=/storage/streetzim/venv-linux/bin/python3
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
PORT="${PORT:-8881}"
# kiwix-serve gets its own port so it cannot collide with the web smoke's
# server, the retrofit queue (8811) or build-refresh-queue.sh (8801).
KIWIX_PORT="${KIWIX_PORT:-8891}"
REGISTRY=/storage/streetzim/cloud/regions.tsv
LOG=/storage/streetzim/viewer-refresh.log
TSV=/storage/streetzim/viewer-refresh.tsv
LOCK=/storage/streetzim/.retrofit-upload.lock
UPLOAD="${UPLOAD:-1}"
TODAY=$(date +%Y-%m-%d)
# Live filenames per region, from archive.org (tmp/live-inventory.py).
LIVE_INVENTORY="${LIVE_INVENTORY:-/storage/streetzim/tmp/live-inventory.out}"

# smallest first, as asked
REGIONS=(switzerland argentina turkey west-coast-us japan central-us mexico
         midwest-us southeast-asia west-asia east-coast-us)

log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" | tee -a "$LOG"; }
row(){ printf "%s\t%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$4" "$(date -Iseconds)" >> "$TSV"; }
[ -f "$TSV" ] || printf "id\tstatus\tzim\tnote\tfinished\n" > "$TSV"

# reuse the queue's own gate helpers so the two paths cannot drift
HELP=$(mktemp "$TMPDIR/vrefresh.XXXXXX.sh")
sed -n '/^smoke_find()/,/^}/p;/^smoke_search()/,/^}/p;/^browser_smoke()/,/^}/p' \
    retrofit-chips-queue.sh > "$HELP"
. "$HELP"; trap 'rm -f "$HELP"' EXIT

for ID in "${REGIONS[@]}"; do
  grep -qP "^$ID\t(viewer-refreshed|uploaded)" "$TSV" 2>/dev/null && { log "skip $ID: already done"; continue; }
  # SRC must be the file archive.org CURRENTLY LISTS, never "newest local
  # dated ZIM": several regions have newer local artifacts that were built but
  # never shipped (stranded retrofits that gate-failed). Swapping a viewer into
  # one of those and uploading it would quietly publish un-gated chip/search
  # reshards under the banner of a viewer fix.
  SRC=$(awk -v r="$ID" '$1==r{print $NF}' "$LIVE_INVENTORY" 2>/dev/null)
  [ -n "$SRC" ] || { log "skip $ID: not listed on archive.org"; row "$ID" skipped - "not live"; continue; }
  [ -s "$SRC" ] || { log "skip $ID: live file $SRC not present locally"; row "$ID" skipped "$SRC" "no local copy"; continue; }
  OUT="osm-${ID}-${TODAY}b.zim"
  for sfx in b c d e; do OUT="osm-${ID}-${TODAY}${sfx}.zim"; [ -e "$OUT" ] || break; done
  [ -e "$OUT" ] && { log "skip $ID: no free name"; row "$ID" skipped "$SRC" "name"; continue; }

  REG=$(awk -F'\t' -v id="$ID" '$1==id' "$REGISTRY" | head -1)
  BBOX=$(echo "$REG" | cut -f3); RSRC=$(echo "$REG" | cut -f5)
  RDST=$(echo "$REG" | cut -f6); SEARCH=$(echo "$REG" | cut -f7)
  [ -n "$SEARCH" ] || { log "skip $ID: registry row incomplete"; row "$ID" skipped "$SRC" "registry"; continue; }

  log "=== $ID: $SRC ($(du -h "$SRC" | cut -f1)) -> $OUT  (viewer swap only)"
  T0=$(date +%s)
  if ! "$PY" -u cloud/swap_viewer_rust.py "$SRC" "$OUT" >> "$LOG" 2>&1; then
    log "  SWAP FAILED"; row "$ID" swap-failed "$SRC" "-"; rm -f "$OUT"; continue
  fi
  log "  swap done in $(( ($(date +%s) - T0) / 60 )) min ($(du -h "$OUT" | cut -f1))"

  # confirm the new viewer really is in there before spending gate time
  if ! "$PY" - "$OUT" <<'PYEOF'
import sys
from libzim.reader import Archive
b = bytes(Archive(sys.argv[1]).get_entry_by_path("index.html").get_item().content)
ok = (b"loadWikiGeoIndex" in b) and (b"_findResolveChipDef" in b)
print("  in-ZIM viewer carries wiki+chip fixes:", ok)
sys.exit(0 if ok else 1)
PYEOF
  then log "  VIEWER NOT UPDATED — not shipping"; row "$ID" stale-viewer "$OUT" "-"; continue; fi

  G=""
  TERRAIN_STRIPE_TOLERATE=10 timeout 7200 "$PY" cloud/validate_zim.py "$OUT" >> "$LOG" 2>&1 \
    && log "  gate validate: OK" || { G="$G validate"; log "  gate validate: FAIL"; }
  RO=$(timeout 2400 "$PY" cloud/route_cli.py --zim="$OUT" --src="$RSRC" --dst="$RDST" --mode=astar --max-pops=5000000 2>&1)
  echo "$RO" | grep -q "route OK" && log "  gate routing: OK" || { G="$G routing"; log "  gate routing: FAIL"; }
  SN=$(smoke_search "$OUT" "$SEARCH"); FN=$(smoke_find "$OUT")
  [ "${SN:-0}" -ge 1 ] 2>/dev/null && log "  gate search('$SEARCH'): $SN" || { G="$G search"; log "  gate search: FAIL"; }
  [ "${FN:-0}" -ge 1 ] 2>/dev/null && log "  gate find: $FN" || { G="$G find"; log "  gate find: FAIL"; }
  if browser_smoke "$OUT" "$RSRC" "$RDST" "$SEARCH" "/storage/streetzim/$ID-vrefresh-smoke-$TODAY.log"; then
    log "  gate browser (web shell): OK"
  else G="$G browser"; log "  gate browser (web shell): FAIL"; fi

  # The gate that actually covers this change. The web smoke above serves
  # index.html/places.html from web/ (sw.js VIEWER_SHELL_NAMES), so it passes
  # whether or not the swap touched the ZIM. This one runs real kiwix-serve
  # and reads every byte from the archive — the mode most of our users are in.
  if timeout 1800 bash cloud/kiwix_viewer_gate.sh "$OUT" "$KIWIX_PORT" "$SEARCH" \
       >> "$LOG" 2>&1; then
    log "  gate kiwix (in-ZIM viewer): OK"
  else G="$G kiwix"; log "  gate kiwix (in-ZIM viewer): FAIL"; fi

  MIN=$(( ($(date +%s) - T0) / 60 ))
  if [ -n "$G" ]; then log "  GATES FAILED:$G — not uploading $OUT"; row "$ID" gate-failed "$OUT" "$G"; continue; fi
  if [ "$UPLOAD" != 1 ]; then log "  gated OK (UPLOAD=0)"; row "$ID" viewer-refreshed "$OUT" "$MIN min"; continue; fi

  log "  all gates passed — uploading $OUT"
  flock "$LOCK" env PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
    bash cloud/upload_validated.sh "$ID" "$OUT" >> "$LOG" 2>&1
  RC=$?
  case $RC in
    0) log "  SHIPPED $OUT"; row "$ID" uploaded "$OUT" "$MIN min" ;;
    6) log "  uploaded, listing pending"; row "$ID" upload-pending "$OUT" "rc=6" ;;
    *) log "  UPLOAD FAILED rc=$RC"; row "$ID" upload-failed "$OUT" "rc=$RC" ;;
  esac
done
log "=== viewer refresh complete"
