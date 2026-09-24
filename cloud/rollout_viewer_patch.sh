#!/bin/bash
# Roll the current resources/viewer into every live region, SMALLEST FIRST,
# then re-upload. What this ships: the low-zoom lake layer (f78011a) and the
# chip fallback distance fix (fb85341).
#
# Why in-place patch, not repackage: the viewer sits in fixed uncompressed
# slots (cloud/viewer_slots.py), so patch_viewer_inplace.py is a seek+write
# +checksum -- 2.6 s on a 1.2 GB ZIM vs hours for a repack. It does NOT
# shrink the upload: archive.org has no partial update, so the whole file
# still ships. Upload is the entire cost of this rollout.
#
# The work list is a DATA FILE (rollout-viewer.tsv), not a here-doc in this
# script: bash reads a running script incrementally, so editing this file
# mid-run corrupts the run. Regenerate/reorder the TSV instead; this script
# re-reads it before every region.
#
# Columns: id <TAB> zim <TAB> search-term <TAB> bytes
#
# Per region: patch -> validate -> markers -> gates -> upload.
# A region that fails any step is recorded and SKIPPED; the run continues.
# The patch is in place, so a failed gate leaves a patched-but-unshipped
# local file -- that is fine, the next run re-patches it (idempotent: the
# slot is overwritten wholesale, never appended).
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
PY=/storage/streetzim/venv-linux/bin/python3
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
KS=$(readlink -f /storage/streetzim/tools/kiwix-tools_*/kiwix-serve)
LOG=/storage/streetzim/rollout-viewer.log
TSV=/storage/streetzim/rollout-viewer.tsv
DONE=/storage/streetzim/rollout-viewer-status.tsv
LOCK=/storage/streetzim/.retrofit-upload.lock
OV=9611; KW=9612

log(){ echo "[$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M:%S %Z')] $*" | tee -a "$LOG"; }
row(){ printf "%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$(date -Iseconds)" >> "$DONE"; }
[ -f "$DONE" ] || printf "id\tstatus\tzim\tfinished\n" > "$DONE"

# Same marker set the europe queue gates on, minus the build-only checks.
markers(){ "$PY" - "$1" <<'PYEOF'
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
    # What this rollout exists to deliver -- assert both, or a region can
    # ship a 400 MB upload that changed nothing.
    "lakes layer":     b"water-lowzoom" in idx and b"_SZ_LAKES" in idx,
    "chip nearest":    b"_ranked.sort" in idx,
    "slot marker idx": b"SZVSLOT1:index.html" in idx,
    "slot marker plc": b"SZVSLOT1:places.html" in plc,
    "archive check":   a.check(),
}
for k, v in checks.items(): print(f"    {k:18s} {v}")
sys.exit(0 if all(checks.values()) else 1)
PYEOF
}

gate(){  # $1=zim $2=term
  local Z=$1 TERM=$2 B G="" VW q c up=0 i MH
  B=$(basename "$Z" .zim)
  if ss -ltn 2>/dev/null | grep -q ":$OV "; then log "  port $OV busy — NOT a geometry failure"; return 1; fi
  setsid nohup "$KS" --port "$OV" "$Z" > "$TMPDIR/rv-$B.log" 2>&1 < /dev/null &
  for i in $(seq 1 150); do sleep 2
    [ "$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://localhost:$OV/content/$B/index.html")" = 200 ] && { up=1; break; }
  done
  if [ "$up" != 1 ]; then
    log "  kiwix-serve never came up — NOT a geometry failure"
    for q in $(pgrep -x kiwix-serve); do c=$(tr '\0' ' ' < /proc/$q/cmdline); case "$c" in *"--port $OV"*) kill $q;; esac; done
    return 1
  fi
  for VW in 320 390 430; do
    ZIM_ORIGIN="http://localhost:$OV/content/$B" VW=$VW timeout 400 "$NODE" tmp/overlap-check.mjs > "$TMPDIR/rv-$B-$VW.txt" 2>&1
    grep -q "^NO OVERLAPS" "$TMPDIR/rv-$B-$VW.txt" || { G="$G overlap@$VW"; log "    overlaps @ ${VW}px"; }
  done
  SEARCH_TERM="$TERM" ZIM_ORIGIN="http://localhost:$OV/content/$B" \
    timeout 1800 "$NODE" tmp/device-matrix.mjs > "$TMPDIR/rv-$B-matrix.txt" 2>&1
  if grep -q "^all 7 devices passed" "$TMPDIR/rv-$B-matrix.txt"; then log "  device matrix: 7/7 PASS"
  else G="$G device-matrix"; log "  device matrix FAILED:"
       grep -E "^ +!" "$TMPDIR/rv-$B-matrix.txt" | sort -u | head -4 | while read -r l; do log "      $l"; done; fi
  # The only gate that fails a blank map: asserts style+source loaded and
  # real features drawn. A bad viewer patch shows up here first.
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

echo $$ > /storage/streetzim/.rollout-viewer.pid

# ---- wait for the build queues -----------------------------------------
# Builds own the CPU; this rollout owns the uplink. Running both means the
# upload starves the build's tile writer and vice versa. Resolve by PID from
# the queues' own pidfiles -- never pgrep -f, which matches this script's own
# cmdline (feedback_process_scan_self_match).
log "=== waiting for build queues to finish"
while :; do
  busy=""
  for pf in .europe-countries.pid .africa-rest.pid; do
    [ -f "$pf" ] || continue
    p=$(cat "$pf" 2>/dev/null) || continue
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null && busy="$busy $pf($p)"
  done
  [ -z "$busy" ] && break
  log "  waiting on:$busy"
  sleep 300
done
log "=== queues clear — starting rollout"

# Build the work list HERE, not days ago: regions that shipped while this
# script waited belong in it. A region that already carries the current
# viewer costs nothing -- the patch rewrites identical bytes and
# `ia upload --checksum` skips the transfer.
if ! "$PY" tools/make_rollout_list.py "$TSV" >> "$LOG" 2>&1; then
  log "FATAL: could not build the work list"; exit 1
fi
log "  work list: $(($(wc -l < "$TSV") - 1)) rows"

processed=""
while :; do
  # Re-read the work list every iteration: the TSV can be reordered or
  # extended while this runs (that is the point of it being a data file).
  next=""
  while IFS=$'\t' read -r id zim term bytes; do
    [ -n "${id:-}" ] || continue
    case "$id" in "#"*|id) continue;; esac
    case " $processed " in *" $id "*) continue;; esac
    grep -qP "^\Q$id\E\tuploaded\t" "$DONE" 2>/dev/null && { processed="$processed $id"; continue; }
    next="$id	$zim	$term	$bytes"; break
  done < "$TSV"
  [ -z "$next" ] && break

  IFS=$'\t' read -r id zim term bytes <<< "$next"
  processed="$processed $id"
  T0=$(date +%s)
  GB=$(echo "$bytes" | awk '{printf "%.1f", $1/1e9}')
  log "=== $id  ($zim, ${GB} GB)"

  if [ ! -s "$zim" ]; then log "  MISSING locally — skipped"; row "$id" missing "$zim"; continue; fi

  if ! "$PY" cloud/patch_viewer_inplace.py "$zim" >> "$LOG" 2>&1; then
    log "  PATCH FAILED (no slots?) — skipped"; row "$id" patch-failed "$zim"; continue
  fi
  log "  patched"

  if ! TERRAIN_STRIPE_TOLERATE=10 timeout 10800 "$PY" cloud/validate_zim.py "$zim" >> "$LOG" 2>&1; then
    log "  validate: FAIL — not uploading"; row "$id" validate-failed "$zim"; continue
  fi
  log "  validate OK"

  if ! markers "$zim" >> "$LOG" 2>&1; then log "  MARKERS FAILED"; row "$id" marker-failed "$zim"; continue; fi
  log "  markers OK"

  if ! gate "$zim" "$term"; then row "$id" gate-failed "$zim"; continue; fi

  log "  uploading $zim as streetzim-$id"
  flock "$LOCK" env PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
    bash cloud/upload_validated.sh "$id" "$zim" >> "$LOG" 2>&1
  RC=$?; MIN=$(( ($(date +%s)-T0)/60 ))
  case $RC in
    0) log "  SHIPPED $zim ($MIN min)"; row "$id" uploaded "$zim" ;;
    6) log "  uploaded, listing pending ($MIN min)"; row "$id" upload-pending "$zim" ;;
    *) log "  UPLOAD FAILED rc=$RC"; row "$id" upload-failed "$zim" ;;
  esac
done

log "=== rollout complete ==="
awk -F'\t' 'NR>1{c[$2]++} END{for(k in c) printf "  %-16s %d\n", k, c[k]}' "$DONE" | tee -a "$LOG"
