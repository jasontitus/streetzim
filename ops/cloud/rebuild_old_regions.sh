#!/bin/bash
# ops split, stage 1: this file lives in ops/ and a symlink at its old path
# runs it; the paths below assume that path (and so do pgrep and the locks).
# Started directly (ops/..., or from inside ops/), it re-runs by the old path.
if [ ! -L "$0" ] && _ops_real="$(readlink -f "$0" 2>/dev/null)"; then
  case "$_ops_real" in
    */ops/*) _ops_old="${_ops_real%/ops/*}/${_ops_real##*/ops/}"
             if [ -L "$_ops_old" ]; then exec bash "$_ops_old" "$@"; fi ;;
  esac
fi
unset _ops_real _ops_old
# Rebuild the regions built before 2026-09-22, which each ship the ENTIRE
# global Wikidata set (3.3 M entries, 624 MB uncompressed, ~108 MB compressed
# -- byte-identical in hawaii and the baltics). Builds since carry only the
# region's own. That is ~9% of every such ZIM spent on other continents, and
# wikidata/ is not in a viewer slot, so only a rebuild fixes it.
#
# The list is rebuild-old.list, smallest first (washington-dc 0.2 GB ->
# europe 65 GB), read once when the driver starts (line ORDER= below): a
# change to it takes effect only when the driver is restarted.
#
# Per region: osmium extract -> symlink world tiles/search -> Overture
# download -> build-region-fast.sh -> validate -> markers -> gates -> upload.
# With STREETZIM_ML_TILES=1 in the environment, build-region-fast.sh cuts
# multilingual tiles from the extract instead of using the world tiles
# (ops/region-ml-tiles.py), and markers() checks they reached the ZIM.
#
# Notes earned elsewhere in this project:
#  * The PBF must be a REAL extract, never a planet symlink: wikidata_cache's
#    extract_qids_from_pbf walks the whole file with no bbox filter, so a
#    symlink turns a 5-minute scan into 9+ hours (ops/docs/new-region-setup.md).
#  * mbtiles and search.jsonl ARE bbox-aware at read time, so symlinks are
#    correct and save ~110 GB per region.
#  * Overture `addresses` coming back ~0 rows is EXPECTED for regions Overture
#    does not cover; `places` returning 0 is not. Only places is checked.
#  * create_osm_zim.py writes viewer slots as of 2026-09-21, so these builds
#    are patchable from birth. markers() FAILS a region that lacks them.
#  * Overture downloads keep the host's s3:// transport
#    (--transport "${OVERTURE_TRANSPORT:-s3}"; https opts in).
#  * osmium extracts serialise on tmp/.regions-pbf.lock -- two writers on one
#    path yield a truncated-but-valid PBF that nothing downstream notices.
set -uo pipefail
cd /storage/streetzim
# One instance per worker under ops/cloud/parallel_rebuild.sh, which gives
# each its own list, pid file, log and gate ports (defaults: the single
# round's files and ports) and REBUILD_NO_SKIP=1 (it rebuilds regions this
# file lists as uploaded; the runner keeps its own record of what it shipped).
export TMPDIR=/storage/streetzim/tmp
export ZSTD_CLEVEL="${ZSTD_CLEVEL:-22}"
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
PY=/storage/streetzim/venv-linux/bin/python3
# bbox_crosses / bbox_stale / bbox_mark / bbox_osmium_area
. ops/region-bbox.sh || exit 1
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
KS=$(readlink -f /storage/streetzim/tools/kiwix-tools_*/kiwix-serve)
PLANET=/storage/streetzim/world-data/planet-2026-08-31.osm.pbf
WORLD_MB=/storage/streetzim/world-data/world-tiles-v3.mbtiles
# Schema 2 (name_native: places found by their own script's name). The
# schema-1 world-2026-08-31.jsonl it replaced lacks it, and a build only
# warns about that.
WORLD_SEARCH=/storage/streetzim/search_cache/world-2026-08-31.s2.jsonl
REL=2026-08-19.0
REGISTRY=/storage/streetzim/cloud/regions.tsv
VARIANTS=/storage/streetzim/cloud/region-variants.tsv
LOG=${REBUILD_LOG:-/storage/streetzim/rebuild-old.log}
TSV=/storage/streetzim/rebuild-old.tsv
LOCK=/storage/streetzim/.retrofit-upload.lock
PBFLOCK=/storage/streetzim/tmp/.regions-pbf.lock
OV=${REBUILD_OV_PORT:-9641}; KW=${REBUILD_KW_PORT:-9642}
log(){ echo "[$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M:%S %Z')] $*" | tee -a "$LOG"; }
# Appends under a lock: parallel_rebuild.sh runs several instances at once.
row(){ { flock -x 3; printf "%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$(date -Iseconds)" >> "$TSV"; } 3>> "$TSV.lock"; }
[ -f "$TSV" ] || printf "id\tstatus\tzim\tfinished\n" > "$TSV"

ORDER=$(cat "${REBUILD_LIST:-/storage/streetzim/rebuild-old.list}")

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
    # Every shipped region with Wikipedia articles carries a Wikipedia
    # geo-index. The build-region.sh builds did not, and it took the browser
    # gate -- after hours of building -- to notice. One lookup here catches it
    # in a second. A build without articles says so (hasWikiArticles false).
    "wiki geo-index":  __import__("json").loads(bytes(a.get_entry_by_path(
        "map-config.json").get_item().content)).get("hasWikiArticles") is False
        or (a.has_entry_by_path("wiki-geo-index.json") and len(
        bytes(a.get_entry_by_path("wiki-geo-index.json").get_item().content)) > 2),
    # create_osm_zim.py pads the viewer into fixed uncompressed slots as of
    # 2026-09-21. Without them a region is born owing a full re-pack at the
    # next viewer change, so fail the build rather than discover it later.
    "slot marker idx": b"SZVSLOT1:index.html" in idx,
    "slot marker plc": b"SZVSLOT1:places.html" in bytes(
        a.get_entry_by_path("places.html").get_item().content),
    "archive check":   a.check(),
}
# A language-ready round (STREETZIM_ML_TILES=1, docs/language-ready.md): the
# ZIM carries what the multilingual tiles and cloud/region-languages.tsv gave
# build-region-fast.sh; a build from the world tiles would not.
if __import__("os").environ.get("STREETZIM_ML_TILES") == "1":
    mc = __import__("json").loads(bytes(a.get_entry_by_path("map-config.json").get_item().content))
    checks["ml languages"] = bool(mc.get("tileLanguages")) and bool(mc.get("regionLanguages"))
for k, v in checks.items(): print(f"    {k:18s} {v}")
sys.exit(0 if all(checks.values()) else 1)
PYEOF
}

# A browser gate that fails by timing out -- the page never loaded while the
# disk was saturated (iceland 2026-10-23: "Navigation timeout of 90000 ms
# exceeded" at 390 px; a re-run passed) -- is not a finding, so it is run
# once more after RETRY_WAIT (60 s). The overlap and render gates are retried
# only then: an overlap report or a render that drew too little is never
# retried. The device matrix and the kiwix in-ZIM gate are retried once on
# ANY failure, and the second run's verdict stands: under load they fail
# checks that wait on the page (switzerland-light 2026-10-23: "Find page
# returns places from the ZIM -- clicked=null"; a re-run passed 18/18), and
# a deterministic failure fails the second run too. Both runs are logged.
# Used by the overlap and render gates, which retry only without a verdict;
# "Target closed|Protocol error" is the browser dying under memory pressure,
# no verdict either.
TIMEOUT_RE='TimeoutError|Navigation timeout|Timeout [0-9]+ ?ms exceeded|net::ERR_|Target closed|Protocol error'
RETRY_WAIT=${REBUILD_GATE_RETRY_WAIT:-60}
timed_out(){  # $1 output file, $2 exit code
  [ "$2" = 124 ] || grep -qE "$TIMEOUT_RE" "$1" 2>/dev/null
}

gate(){  # $1=zim $2=term
  local Z=$1 TERM=$2 B G="" VW q c up=0 i f rc try MH n2
  B=$(basename "$Z" .zim)
  if ss -ltn 2>/dev/null | grep -q ":$OV "; then log "  port $OV busy — NOT a geometry failure"; return 1; fi
  # 6>&-: parallel_rebuild.sh --regate's lock (fd 6), held by this driver;
  # a kiwix-serve left behind must not hold it.
  setsid nohup "$KS" --port "$OV" "$Z" > "$TMPDIR/ec-$B.log" 2>&1 < /dev/null 6>&- &
  for i in $(seq 1 150); do sleep 2
    [ "$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://localhost:$OV/content/$B/index.html")" = 200 ] && { up=1; break; }
  done
  if [ "$up" != 1 ]; then
    log "  kiwix-serve never came up — NOT a geometry failure"
    for q in $(pgrep -x kiwix-serve); do c=$(tr '\0' ' ' < /proc/$q/cmdline); case "$c" in *"--port $OV"*) kill $q;; esac; done
    return 1
  fi
  for VW in 320 390 430; do
    f="$TMPDIR/ec-$B-$VW.txt"
    for try in 1 2; do
      ZIM_ORIGIN="http://localhost:$OV/content/$B" VW=$VW timeout 600 "$NODE" tmp/overlap-check.mjs > "$f" 2>&1; rc=$?
      grep -q "^NO OVERLAPS" "$f" && break
      if [ "$try" = 1 ] && ! grep -q '^OVERLAPS:' "$f" && timed_out "$f" "$rc"; then
        log "    overlap @ ${VW}px timed out (not a finding); once more in ${RETRY_WAIT} s"; sleep "$RETRY_WAIT"; continue
      fi
      break
    done
    grep -q "^NO OVERLAPS" "$f" || { G="$G overlap@$VW"; log "    overlaps @ ${VW}px"; }
  done
  f="$TMPDIR/ec-$B-matrix.txt"
  for try in 1 2; do
    SEARCH_TERM="$TERM" ZIM_ORIGIN="http://localhost:$OV/content/$B" \
      timeout 3600 "$NODE" tmp/device-matrix.mjs > "$f" 2>&1; rc=$?
    grep -q "^all 7 devices passed" "$f" && break
    if [ "$try" = 1 ]; then
      cp -f "$f" "$f.run1"
      log "  device matrix run 1 FAILED (rc $rc$(grep -cE '^FAIL ' "$f" | sed 's/^/, devices failed: /')); once more in ${RETRY_WAIT} s:"
      grep -E "^ +!" "$f" | sort -u | head -4 | while read -r l; do log "      $l"; done
      sleep "$RETRY_WAIT"; continue
    fi
    break
  done
  n2=""; [ "$try" = 1 ] || n2=" (run 2)"
  if grep -q "^all 7 devices passed" "$f"; then log "  device matrix: 7/7 PASS$n2"
  else G="$G device-matrix"; log "  device matrix FAILED$n2:"
       grep -E "^ +!" "$f" | sort -u | head -4 | while read -r l; do log "      $l"; done; fi
  # Render gate: the device matrix waits for the canvas ELEMENT and would
  # pass a blank map. This asserts the style loaded, the tile source loaded,
  # and real features were drawn.
  f="$TMPDIR/ec-$B-health.txt"
  for try in 1 2; do
    TAG="$B" ZIM_ORIGIN="http://localhost:$OV/content/$B" timeout 600 "$NODE" tmp/map-health.mjs > "$f" 2>&1; rc=$?
    MH=$(grep '^{' "$f" | tail -1)
    if echo "$MH" | "$PY" -c 'import sys,json
d=json.loads(sys.stdin.read() or "{}")
src=d.get("sourceLoaded") or {}
ok=d.get("styleLoaded") and src and all(v is True for v in src.values()) and (d.get("renderedFeatures") or 0)>=100 and not d.get("errors")
sys.exit(0 if ok else 1)' 2>/dev/null; then break; fi
    # No result at all (the page never loaded) is a timeout, not a finding.
    if [ "$try" = 1 ] && [ -z "$MH" ] && timed_out "$f" "$rc"; then
      log "  render gate timed out (not a finding); once more in ${RETRY_WAIT} s"; sleep "$RETRY_WAIT"; continue
    fi
    break
  done
  if echo "$MH" | "$PY" -c 'import sys,json
d=json.loads(sys.stdin.read() or "{}")
src=d.get("sourceLoaded") or {}
ok=d.get("styleLoaded") and src and all(v is True for v in src.values()) and (d.get("renderedFeatures") or 0)>=100 and not d.get("errors")
sys.exit(0 if ok else 1)' 2>/dev/null; then
    log "  render gate: OK ($(echo "$MH" | grep -oE '"renderedFeatures":[0-9]+'))"
  else G="$G render"; log "  render gate: FAIL $(echo "$MH" | head -c 200)"; fi
  for q in $(pgrep -x kiwix-serve); do c=$(tr '\0' ' ' < /proc/$q/cmdline); case "$c" in *"--port $OV"*) kill $q;; esac; done
  f="$TMPDIR/ec-$B-kiwix.txt"
  for try in 1 2; do
    timeout 2400 bash cloud/kiwix_viewer_gate.sh "$Z" "$KW" "$TERM" > "$f" 2>&1 6>&-; rc=$?
    cat "$f" >> "$LOG"
    [ "$rc" = 0 ] && break
    if [ "$try" = 1 ]; then
      cp -f "$f" "$f.run1"
      log "  kiwix in-ZIM gate run 1 FAILED (rc $rc$(grep -m1 'FAIL' "$f" | sed 's/^ */: /')); once more in ${RETRY_WAIT} s"
      sleep "$RETRY_WAIT"; continue
    fi
    break
  done
  n2=""; [ "$try" = 1 ] || n2=" (run 2)"
  [ "$rc" = 0 ] && log "  kiwix in-ZIM gate: OK$n2" || { G="$G kiwix"; log "  kiwix in-ZIM gate: FAIL$n2"; }
  [ -z "$G" ] && return 0
  log "  GATES FAILED:$G"; return 1
}

# ---- wait for every heavy job already in flight -------------------------
echo $$ > "${REBUILD_PIDFILE:-/storage/streetzim/.rebuild-old.pid}"
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
# Inherited by build-region-fast.sh and markers(): STREETZIM_ML_TILES=1 builds
# language-ready ZIMs (multilingual tiles; docs/language-ready.md).
log "=== multilingual tiles (STREETZIM_ML_TILES): ${STREETZIM_ML_TILES:-0}"

for ID in $ORDER; do
  if [ "${REBUILD_NO_SKIP:-0}" != 1 ]; then
    grep -qP "^$ID\t(uploaded|upload-pending)" "$TSV" 2>/dev/null && { log "skip $ID: done"; continue; }
  fi
  R=$(awk -F'\t' -v id="$ID" '$1==id' "$REGISTRY" | head -1)
  BBOX=$(echo "$R" | cut -f3); NAME=$(echo "$R" | cut -f2); TERM=$(echo "$R" | cut -f7)
  [ -n "$BBOX" ] || { log "skip $ID: no registry row"; row "$ID" no-registry "-"; continue; }
  # REBUILD_REGATE_ZIM (parallel_rebuild.sh --regate): an already-built ZIM of
  # this region -- gates and upload only, no extract, tiles or build (e.g. a
  # region whose gates failed for a reason since fixed). The else branch (the
  # data steps and the build) is left unindented to keep it as it was.
  if [ -n "${REBUILD_REGATE_ZIM:-}" ]; then
    T0=$(date +%s)
    log "=== $ID ($NAME) bbox=$BBOX"
    OUT=$(basename "$REBUILD_REGATE_ZIM")
    case "$OUT" in
      osm-${ID}-20??-??-??*.zim) ;;
      *) log "  regate: $OUT is not a ZIM of $ID"; row "$ID" no-output "$OUT"; continue ;;
    esac
    [ -s "$OUT" ] || { log "  regate: no $OUT in $(pwd)"; row "$ID" no-output "$OUT"; continue; }
    log "  regate $OUT ($(du -h "$OUT" | cut -f1)): gates and upload only, no build"
  else
  # A Light variant (cloud/region-variants.tsv) is built by build-region-fast.sh
  # from its parent's extracts and Overture parquets, so the data steps below
  # check the parent's files (DATA_ID). Without this, africa-light would get an
  # extract of all Africa and an Overture download of its own that the build
  # never reads. Only under the parent's box: under another box those steps
  # would re-cut the parent's own files.
  DATA_ID=$(awk -F'\t' -v id="$ID" '$1==id {print $2; exit}' "$VARIANTS" 2>/dev/null)
  if [ -n "$DATA_ID" ]; then
    PBOX=$(awk -F'\t' -v id="$DATA_ID" '$1==id' "$REGISTRY" | head -1 | cut -f3)
    if [ "$PBOX" != "$BBOX" ]; then
      log "skip $ID: a variant of $DATA_ID under another box ($BBOX, not ${PBOX:-none})"
      row "$ID" variant-box "-"; continue
    fi
  else DATA_ID=$ID; fi
  T0=$(date +%s)
  log "=== $ID ($NAME) bbox=$BBOX"
  [ "$DATA_ID" = "$ID" ] || log "  variant of $DATA_ID: checks and uses its extracts and Overture parquets"

  PBF=world-data/regions/${DATA_ID}.osm.pbf
  if [ ! -s "$PBF" ] || bbox_stale "$PBF" "$BBOX"; then
    log "  osmium extract -> $PBF"
    # -b "$BBOX"; across the antimeridian a two-ring .poly, never -b.
    bbox_osmium_area "$BBOX" "$PBF.poly" \
      || { log "  POLY FAILED for $ID (bbox $BBOX crosses the antimeridian)"; row "$ID" extract-failed "-"; continue; }
    flock "$PBFLOCK" osmium extract "${AREA[@]}" "$PLANET" -o "$PBF" --overwrite >> "$LOG" 2>&1 \
      || { log "  EXTRACT FAILED"; rm -f "$PBF"; row "$ID" extract-failed "-"; continue; }
    bbox_mark "$PBF" "$BBOX"
    log "  pbf $(du -h "$PBF" | cut -f1)"
  else log "  pbf present ($(du -h "$PBF" | cut -f1))"; fi

  # NEVER `ln -sfn` blindly here. That line was copied from the africa queue,
  # where the four regions were brand new and had no extracts. 50 of the 51
  # regions in this queue already have REAL extract files, and `ln -sfn` over
  # a regular file deletes it: measured 199.9 GB across the list, including
  # europe.mbtiles 31.1 GB, canada 15.2 GB, russia 13.3 GB. There is no
  # tilemaker on this host, so they could only be re-derived from the 114 GB
  # world file. Symlink ONLY when nothing is there.
  # A search cut without native-script names (no schema-2 .schema sidecar;
  # the cuts of 2026-09-06 have none) is as stale as one cut for another
  # box: a build from it only warns. Its sidecars are parked with it, and a
  # symlink to the world file gets a copy of the world file's .schema beside
  # it (a copy: a symlinked sidecar would be written through by the next
  # derive).
  for _pair in "${DATA_ID}.mbtiles:$WORLD_MB" "${DATA_ID}.search.jsonl:$WORLD_SEARCH"; do
    _dst="world-data/regions/${_pair%%:*}"; _src="${_pair#*:}"
    _why=""
    if [ -e "$_dst" ] && [ ! -L "$_dst" ]; then
      if bbox_stale "$_dst" "$BBOX"; then _why="cut for another bbox"
      elif [ "${_dst%.search.jsonl}" != "$_dst" ] && ! grep -qE '"search_schema": *([2-9]|[1-9][0-9]+)[,}]' "$_dst.schema" 2>/dev/null; then
        _why="no native-script names (search schema < 2)"; fi
    fi
    if [ -n "$_why" ]; then
      # Park it (never delete; see above), use the world file.
      _park="$_dst.stale-$(date +%F-%H%M%S)"
      mv -n "$_dst" "$_park"
      if [ -e "$_dst" ] && [ ! -L "$_dst" ]; then    # never ln -sfn over it
        log "  could not park $(basename "$_dst") — not building"; row "$ID" park-failed "-"; continue 2
      fi
      for _side in bbox schema; do [ ! -e "$_dst.$_side" ] || mv -n "$_dst.$_side" "$_park.$_side"; done
      ln -sfn "$_src" "$_dst"
      rm -f "$_dst.schema"; [ ! -e "$_src.schema" ] || cp "$_src.schema" "$_dst.schema"
      log "  $(basename "$_dst"): $_why, parked as $(basename "$_park"); now -> world file"
    elif [ -e "$_dst" ] && [ ! -L "$_dst" ]; then
      log "  keeping existing extract $(basename "$_dst") ($(du -h "$_dst" | cut -f1))"
    else
      ln -sfn "$_src" "$_dst"
      rm -f "$_dst.schema"; [ ! -e "$_src.schema" ] || cp "$_src.schema" "$_dst.schema"
    fi
  done

  for THEME in addresses places; do
    OUT=overture_cache/${THEME}-${DATA_ID}-${REL}.parquet
    [ -s "$OUT" ] && ! bbox_stale "$OUT" "$BBOX" && continue
    rm -f "$OUT" "$OUT.bbox"               # cut for another bbox (or absent)
    log "  overture $THEME"
    if "$PY" download_overture_data.py "$THEME" --bbox="$BBOX" --release "$REL" --transport "${OVERTURE_TRANSPORT:-s3}" --out "$OUT" >> "$LOG" 2>&1; then
      bbox_mark "$OUT" "$BBOX"
    else
      log "  overture $THEME failed (continuing; build guards on file presence)"
    fi
  done
  PLACES=overture_cache/places-${DATA_ID}-${REL}.parquet
  if [ ! -s "$PLACES" ]; then
    log "  NO PLACES PARQUET — refusing to build (would silently drop --overture-places)"
    row "$ID" no-places "-"; continue
  fi

  # PRODUCTION wrapper. The first run of this queue used build-region.sh
  # (named in ops/docs/new-region-setup.md), which is missing 12 flags that every
  # shipped region was built with -- no --bundle-wiki-articles, no url-cache,
  # the python writer instead of rust. All four countries it built shipped
  # WITHOUT a Wikipedia layer; the in-ZIM Kiwix gate caught it.
  #   OVERTURE_RELEASE must match the parquets downloaded above, or the
  #     wrapper looks for 2026-04-15.0 files and silently drops Overture.
  #   WIKI_IMAGES follows build-refresh-queue.sh: lead for continents, all
  #     otherwise.
  #   STAGE_MBTILES_NVME=0: the mbtiles here is the 107 GB world tile file;
  #     staging would copy it to the shared NVMe for every build, and the
  #     wrapper's own notes call the speedup unproven. REBUILD_STAGE_MBTILES=1
  #     (parallel_rebuild.sh, where the tiles are the region's own
  #     <id>.ml.mbtiles, a few GB) stages them.
  TIER=$(echo "$R" | cut -f4)
  case "$TIER" in continent) WI=lead ;; *) WI=all ;; esac
  log "  build-region-fast.sh $ID (tier=$TIER wiki_images=$WI overture=$REL)"
  if ! env OVERTURE_RELEASE="$REL" WIKI_IMAGES="$WI" STAGE_MBTILES_NVME="${REBUILD_STAGE_MBTILES:-0}" \
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
  fi
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
    7) log "  NOT UPLOADED rc=7 — frozen: published base (language recipes on /build/ use it; LANGPACK_RESHIP_AFTER_PATCH=1, then ops/langpack/reship_queue.sh)"; row "$ID" frozen-published "$OUT" ;;
    *) log "  UPLOAD FAILED rc=$RC"; row "$ID" upload-failed "$OUT" ;;
  esac
done
log "=== rebuild complete ==="
