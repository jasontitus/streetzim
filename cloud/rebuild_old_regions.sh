#!/bin/bash
# Rebuild the regions built before 2026-09-22, which each ship the ENTIRE
# global Wikidata set (3.3 M entries, 624 MB uncompressed, ~108 MB compressed
# -- byte-identical in hawaii and the baltics). Builds since carry only the
# region's own. That is ~9% of every such ZIM spent on other continents, and
# wikidata/ is not in a viewer slot, so only a rebuild fixes it.
#
# The list is rebuild-old.list, smallest first (washington-dc 0.2 GB ->
# europe 65 GB), re-read every iteration so it can be trimmed mid-run.
#
# Per region: osmium extract -> symlink world tiles/search -> Overture
# download -> build-region-fast.sh -> validate -> markers -> gates -> upload.
#
# Notes earned elsewhere in this project:
#  * The PBF must be a REAL extract, never a planet symlink: wikidata_cache's
#    extract_qids_from_pbf walks the whole file with no bbox filter, so a
#    symlink turns a 5-minute scan into 9+ hours (docs/new-region-setup.md).
#  * mbtiles and search.jsonl ARE bbox-aware at read time, so symlinks are
#    correct and save ~110 GB per region.
#  * Overture `addresses` coming back ~0 rows is EXPECTED for regions Overture
#    does not cover; `places` returning 0 is not. Only places is checked.
#  * create_osm_zim.py writes viewer slots as of 2026-09-21, so these builds
#    are patchable from birth. markers() FAILS a region that lacks them.
#  * osmium extracts serialise on tmp/.regions-pbf.lock -- two writers on one
#    path yield a truncated-but-valid PBF that nothing downstream notices.
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
export ZSTD_CLEVEL="${ZSTD_CLEVEL:-22}"
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
PY=/storage/streetzim/venv-linux/bin/python3
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
KS=$(readlink -f /storage/streetzim/tools/kiwix-tools_*/kiwix-serve)
PLANET=/storage/streetzim/world-data/planet-2026-08-31.osm.pbf
WORLD_MB=/storage/streetzim/world-data/world-tiles-v3.mbtiles
WORLD_SEARCH=/storage/streetzim/search_cache/world-2026-08-31.jsonl
REL=2026-08-19.0
REGISTRY=/storage/streetzim/cloud/regions.tsv
LOG=/storage/streetzim/rebuild-old.log
TSV=/storage/streetzim/rebuild-old.tsv
LOCK=/storage/streetzim/.retrofit-upload.lock
PBFLOCK=/storage/streetzim/tmp/.regions-pbf.lock
OV=9641; KW=9642
log(){ echo "[$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M:%S %Z')] $*" | tee -a "$LOG"; }
row(){ printf "%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$(date -Iseconds)" >> "$TSV"; }
[ -f "$TSV" ] || printf "id\tstatus\tzim\tfinished\n" > "$TSV"

ORDER=$(cat /storage/streetzim/rebuild-old.list)

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

# ---- wait for every heavy job already in flight -------------------------
echo $$ > /storage/streetzim/.rebuild-old.pid
# Wait for the viewer rollout. Resolve by PID from its own pidfile,
# never pgrep -f: this script's cmdline contains every string it searches for
# (feedback_process_scan_self_match). Builds are CPU+IO bound and the host is
# shared with another tenant, so only one build queue runs at a time.
log "=== waiting for the viewer rollout"
while :; do
  p=$(cat /storage/streetzim/.rollout-viewer.pid 2>/dev/null)
  [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null || break
  sleep 300
done
log "=== all prior jobs done; starting ${ORDER}"

for ID in $ORDER; do
  grep -qP "^$ID\t(uploaded|upload-pending)" "$TSV" 2>/dev/null && { log "skip $ID: done"; continue; }
  R=$(awk -F'\t' -v id="$ID" '$1==id' "$REGISTRY" | head -1)
  BBOX=$(echo "$R" | cut -f3); NAME=$(echo "$R" | cut -f2); TERM=$(echo "$R" | cut -f7)
  [ -n "$BBOX" ] || { log "skip $ID: no registry row"; row "$ID" no-registry "-"; continue; }
  T0=$(date +%s)
  log "=== $ID ($NAME) bbox=$BBOX"

  PBF=world-data/regions/${ID}.osm.pbf
  if [ ! -s "$PBF" ]; then
    log "  osmium extract -> $PBF"
    flock "$PBFLOCK" osmium extract -b "$BBOX" "$PLANET" -o "$PBF" --overwrite >> "$LOG" 2>&1 \
      || { log "  EXTRACT FAILED"; rm -f "$PBF"; row "$ID" extract-failed "-"; continue; }
    log "  pbf $(du -h "$PBF" | cut -f1)"
  else log "  pbf present ($(du -h "$PBF" | cut -f1))"; fi

  # NEVER `ln -sfn` blindly here. That line was copied from the africa queue,
  # where the four regions were brand new and had no extracts. 50 of the 51
  # regions in this queue already have REAL extract files, and `ln -sfn` over
  # a regular file deletes it: measured 199.9 GB across the list, including
  # europe.mbtiles 31.1 GB, canada 15.2 GB, russia 13.3 GB. There is no
  # tilemaker on this host, so they could only be re-derived from the 114 GB
  # world file. Symlink ONLY when nothing is there.
  for _pair in "${ID}.mbtiles:$WORLD_MB" "${ID}.search.jsonl:$WORLD_SEARCH"; do
    _dst="world-data/regions/${_pair%%:*}"; _src="${_pair#*:}"
    if [ -e "$_dst" ] && [ ! -L "$_dst" ]; then
      log "  keeping existing extract $(basename "$_dst") ($(du -h "$_dst" | cut -f1))"
    else
      ln -sfn "$_src" "$_dst"
    fi
  done

  for THEME in addresses places; do
    OUT=overture_cache/${THEME}-${ID}-${REL}.parquet
    [ -s "$OUT" ] && continue
    log "  overture $THEME"
    "$PY" download_overture_data.py "$THEME" --bbox="$BBOX" --release "$REL" --out "$OUT" >> "$LOG" 2>&1 \
      || log "  overture $THEME failed (continuing; build guards on file presence)"
  done
  PLACES=overture_cache/places-${ID}-${REL}.parquet
  if [ ! -s "$PLACES" ]; then
    log "  NO PLACES PARQUET — refusing to build (would silently drop --overture-places)"
    row "$ID" no-places "-"; continue
  fi

  # PRODUCTION wrapper. The first run of this queue used build-region.sh
  # (named in docs/new-region-setup.md), which is missing 12 flags that every
  # shipped region was built with -- no --bundle-wiki-articles, no url-cache,
  # the python writer instead of rust. All four countries it built shipped
  # WITHOUT a Wikipedia layer; the in-ZIM Kiwix gate caught it.
  #   OVERTURE_RELEASE must match the parquets downloaded above, or the
  #     wrapper looks for 2026-04-15.0 files and silently drops Overture.
  #   WIKI_IMAGES follows build-refresh-queue.sh: lead for continents, all
  #     otherwise.
  #   STAGE_MBTILES_NVME=0: the mbtiles here is the 107 GB world tile file;
  #     staging would copy it to the shared NVMe for every build, and the
  #     wrapper's own notes call the speedup unproven.
  TIER=$(echo "$R" | cut -f4)
  case "$TIER" in continent) WI=lead ;; *) WI=all ;; esac
  log "  build-region-fast.sh $ID (tier=$TIER wiki_images=$WI overture=$REL)"
  if ! env OVERTURE_RELEASE="$REL" WIKI_IMAGES="$WI" STAGE_MBTILES_NVME=0 \
       bash build-region-fast.sh "$ID" "$BBOX" "$NAME" >> "$LOG" 2>&1; then
    log "  BUILD FAILED"; row "$ID" build-failed "-"; continue
  fi
  # Must be THIS build's output. The wrapper exits 0 on "ALREADY EXISTS", in
  # which case `ls -t` would hand back an old file and we would gate it.
  # Dated shape only: `osm-${ID}-*.zim` also matches osm-switzerland-light-*,
  # osm-switzerland-nosat-* and osm-africa-light-*, which combined with the
  # wrapper's exit-0-on-ALREADY-EXISTS could hand a sibling variant's ZIM to
  # the gates and upload it under this region's id.
  OUT=$(ls -t osm-${ID}-20??-??-??*.zim 2>/dev/null | head -1)
  if [ -z "$OUT" ] || [ ! -s "$OUT" ] || [ "$(stat -c %Y "$OUT")" -lt "$T0" ]; then
    log "  no FRESH output (got '${OUT:-none}') — not gating a stale file"
    row "$ID" no-output "${OUT:--}"; continue
  fi
  log "  built $OUT ($(du -h "$OUT" | cut -f1)) in $(( ($(date +%s)-T0)/60 )) min"
  # The wrapper runs validate_zim but only echoes on failure and exits 0.
  if ! TERRAIN_STRIPE_TOLERATE=10 timeout 10800 "$PY" cloud/validate_zim.py "$OUT" >> "$LOG" 2>&1; then
    log "  gate validate: FAIL — not uploading"; row "$ID" validate-failed "$OUT"; continue
  fi
  log "  gate validate: OK"

  if ! markers "$OUT" >> "$LOG" 2>&1; then log "  MARKERS FAILED"; row "$ID" marker-failed "$OUT"; continue; fi
  log "  markers OK"
  if ! gate "$OUT" "$TERM"; then row "$ID" gate-failed "$OUT"; continue; fi

  log "  uploading $OUT as streetzim-$ID"
  flock "$LOCK" env PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
    bash cloud/upload_validated.sh "$ID" "$OUT" >> "$LOG" 2>&1
  RC=$?; MIN=$(( ($(date +%s)-T0)/60 ))
  case $RC in
    0) log "  SHIPPED $OUT ($MIN min)"; row "$ID" uploaded "$OUT" ;;
    6) log "  uploaded, listing pending ($MIN min)"; row "$ID" upload-pending "$OUT" ;;
    *) log "  UPLOAD FAILED rc=$RC"; row "$ID" upload-failed "$OUT" ;;
  esac
done
log "=== rebuild complete ==="
