#!/bin/bash
# build-refresh-queue.sh — rebuild every region in cloud/regions.tsv from a
# fresh planet PBF + Overture release, gate each ZIM, and upload it.
#
# Per region (registry order = smallest first, continents last):
#   1. regions/<id>.mbtiles + .search.jsonl → symlink to $WORLD_MBTILES /
#      $WORLD_SEARCH unless a dedicated regional extract newer than the world
#      file exists (those are kept — they are faster on continent bboxes)
#   2. regions/<id>.osm.pbf → osmium extract from $PLANET when missing, a
#      symlink, or older than $PLANET   (pre-run ./extract-region-pbfs.sh to
#      do all of these in one planet pass instead)
#   3. overture_cache/{addresses,places}-<id>-$OVERTURE_RELEASE.parquet
#      downloaded if absent (DuckDB → S3, minutes per region)
#   4. OVERTURE_RELEASE=… ./build-region-fast.sh <id> <bbox> <name>
#   5. gates, all mandatory except the browser smoke (see --browser-smoke):
#        terrain coverage (cloud/check_terrain_coverage.py, catches blank land)
#        cloud/validate_zim.py
#        live routing   (cloud/route_cli.py src→dst must print "route OK")
#        xapian search  (>=1 hit for smoke_search)
#        find chips     (>=1 restaurant chip record)
#        browser smoke  (cloud/pwa_smoke_test.mjs; soft by default)
#   6. cloud/upload_validated.sh <id> <zim>   (skipped with --no-upload)
#
# Usage:
#   ./build-refresh-queue.sh [--only a,b] [--skip a,b] [--tier local,country,…]
#                            [--no-upload] [--dry-run] [--browser-smoke soft|hard|off]
#                            [--continue]   # skip regions already OK in today's .tsv
# Detach:  setsid nohup ./build-refresh-queue.sh … > queue-refresh.out 2>&1 < /dev/null &
# Env:     PLANET OVERTURE_RELEASE WORLD_MBTILES WORLD_SEARCH REGISTRY URL_DEAD_STATUSES
# Results: queue-refresh-<date>.log (narrative) + queue-refresh-<date>.tsv (one row per region)
set -uo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp

PLANET="${PLANET:-/storage/streetzim/world-data/planet-2026-08-31.osm.pbf}"
export OVERTURE_RELEASE="${OVERTURE_RELEASE:-2026-08-19.0}"
# No defaults on purpose. The previous round's world-tiles-v2.mbtiles and
# world.jsonl are still on disk; defaulting to them would staple an August
# road graph to March vector tiles and a March POI index, and no gate can
# see that (terrain comes from the DEM, the validator checks structure,
# routing comes from the PBF). Name them, and they must post-date the planet.
WORLD_MBTILES="${WORLD_MBTILES:?set WORLD_MBTILES to the tiles built from this planet (see build-world-tiles.sh), or TIER_A=1 to accept older tiles}"
WORLD_SEARCH="${WORLD_SEARCH:?set WORLD_SEARCH to the search cache extracted from those tiles, or TIER_A=1}"
REGISTRY="${REGISTRY:-/storage/streetzim/cloud/regions.tsv}"
# Only these liveness-cache statuses drop/scrub a business record. 403/429/
# 5xx/timeouts are mostly bot-blocking, not dead businesses (see
# project_overture_dead_url_false_positives): keep those.
export STREETZIM_URL_DEAD_STATUSES="${URL_DEAD_STATUSES:-404,410,dns,parked,url}"
PY=/storage/streetzim/venv-linux/bin/python3
REGDIR=/storage/streetzim/world-data/regions
TODAY=$(date +%Y-%m-%d)
LOG=/storage/streetzim/queue-refresh-${TODAY}.log
# One results file for the whole round, not per day: a weeks-long run gets
# resumed on a later date, and a fresh empty TSV would make --continue
# rebuild and re-upload every region that already shipped.
TSV="${RESULTS:-/storage/streetzim/queue-refresh.tsv}"
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}

ONLY=""; SKIP=""; TIERS=""; UPLOAD=1; DRY=0; BROWSER=soft; CONTINUE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY=",$2,"; shift 2 ;;
    --skip) SKIP=",$2,"; shift 2 ;;
    --tier) TIERS=",$2,"; shift 2 ;;
    --no-upload) UPLOAD=0; shift ;;
    --dry-run) DRY=1; shift ;;
    --browser-smoke) BROWSER="$2"; shift 2 ;;
    --continue) CONTINUE=1; shift ;;
    -h|--help) sed -n 2,30p "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ts() { date -Iseconds; }
log() { printf "[%s] %s\n" "$(ts)" "$*" | tee -a "$LOG"; }
row() { printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$4" "$5" "$(ts)" >> "$TSV"; }  # id status minutes size note

# ---------- preflight ----------
fail=0
[ -s "$PLANET" ] || { echo "PLANET missing: $PLANET  (./download-planet.sh)"; fail=1; }
[ -s "$WORLD_MBTILES" ] || { echo "WORLD_MBTILES missing: $WORLD_MBTILES"; fail=1; }
[ -s "$WORLD_SEARCH" ] || { echo "WORLD_SEARCH missing: $WORLD_SEARCH"; fail=1; }
[ -s "$REGISTRY" ] || { echo "REGISTRY missing: $REGISTRY"; fail=1; }
"$PY" -c "import libzim, duckdb, osmium" 2>/dev/null || { echo "venv-linux lacks libzim/duckdb/osmium"; fail=1; }
command -v osmium >/dev/null || { echo "osmium not on PATH"; fail=1; }
[ -x rust/streetzim-pack/target/release/streetzim-pack ] || { echo "streetzim-pack binary missing (cargo build --release in rust/streetzim-pack)"; fail=1; }
[ -f terrain_cache/dem_sources/comprehensive.vrt ] || { echo "terrain DEM VRT missing (terrain gate needs it)"; fail=1; }
if [ "${TIER_A:-0}" != "1" ]; then
  [ "$WORLD_MBTILES" -nt "$PLANET" ] || { echo "WORLD_MBTILES ($WORLD_MBTILES) is older than $PLANET — set TIER_A=1 to build on older tiles deliberately"; fail=1; }
  [ "$WORLD_SEARCH"  -nt "$PLANET" ] || { echo "WORLD_SEARCH ($WORLD_SEARCH) is older than $PLANET — set TIER_A=1 to accept it"; fail=1; }
else
  echo "TIER_A=1: building on $(basename "$WORLD_MBTILES") / $(basename "$WORLD_SEARCH"), which predate $(basename "$PLANET"). Routing and business data refresh; map tiles and the search index do not."
fi
# extract-region-pbfs.sh writes the same world-data/regions/<id>.osm.pbf.part
# this queue would write. Two osmium processes on one path silently produce a
# truncated-but-valid PBF, which passes every gate and ships a half-empty map.
if pgrep -f 'extract-region-pbfs.sh' >/dev/null; then
  echo "extract-region-pbfs.sh is running — refusing to start (both write world-data/regions/*.osm.pbf.part)"; fail=1
fi
exec 9>"$TMPDIR/.regions-pbf.lock"
flock -n 9 || { echo "another build/extract holds $TMPDIR/.regions-pbf.lock"; fail=1; }
FREE_GB=$(df -BG --output=avail /storage | tail -1 | tr -dc 0-9)
[ "$FREE_GB" -ge 500 ] || { echo "only ${FREE_GB} GB free on /storage (want >= 500)"; fail=1; }
if ! swapon --show | grep -q /storage/swapfile; then
  echo "/storage/swapfile is not active — continent builds have OOMed without it. Run:"
  echo "    sudo swapon /storage/swapfile && sudo sysctl vm.swappiness=100"
  echo "  (or set ALLOW_NO_SWAP=1 to proceed anyway)"
  [ "${ALLOW_NO_SWAP:-0}" = "1" ] || fail=1
fi
# A malformed registry row shifts every later field (tab is IFS whitespace,
# so consecutive tabs collapse and bbox can end up holding the tier).
BADROWS=$(awk -F'\t' '!/^#/ && NF && NF!=8 {print NR": "$1" ("NF" fields)"}' "$REGISTRY")
[ -z "$BADROWS" ] || { echo "cloud/regions.tsv rows without exactly 8 fields:"; echo "$BADROWS"; fail=1; }
[ "$(tail -c1 "$REGISTRY" | xxd -p)" = "0a" ] || { echo "cloud/regions.tsv has no trailing newline — the last region would be skipped"; fail=1; }
if [ "$UPLOAD" -eq 1 ] && ! /storage/streetzim/venv-linux/bin/ia --version >/dev/null 2>&1; then
  echo "ia CLI not runnable in venv-linux (needed for upload)"; fail=1
fi
if [ "$BROWSER" != off ] && ! [ -x "$NODE" -a -x "$CHROME_PATH" ]; then
  echo "WARN: node/chromium for the browser smoke not found — forcing --browser-smoke off"; BROWSER=off
fi
[ $fail -eq 0 ] || { echo "preflight FAILED"; exit 1; }
[ -f "$TSV" ] || printf "id\tstatus\tminutes\tsize\tnote\tfinished\n" > "$TSV"

log "=== refresh queue start: planet=$(basename "$PLANET") overture=$OVERTURE_RELEASE tiles=$(basename "$WORLD_MBTILES") search=$(basename "$WORLD_SEARCH") upload=$UPLOAD browser=$BROWSER dry=$DRY"

link_world() {  # link_world <world-file> <regions/<id>.ext>
  local target="$1" link="$2"
  if [ -L "$link" ] || [ ! -e "$link" ]; then ln -sfn "$target" "$link"; return; fi
  if [ "$link" -nt "$target" ]; then log "  keeping dedicated $(basename "$link") (newer than $(basename "$target"))"; return; fi
  mv -f "$link" "$link.stale-$TODAY"; ln -sfn "$target" "$link"
  log "  $(basename "$link"): stale regional extract parked as .stale-$TODAY, now → $(basename "$target")"
}

smoke_find() {  # prints count of restaurant chip records
  "$PY" - "$1" <<'PYEOF' 2>/dev/null || echo 0
import sys, json
from libzim.reader import Archive
a = Archive(sys.argv[1]); total = 0
for c1 in '0123456789abcdef':
    for c2 in [''] + list('0123456789abcdef'):
        try:
            e = a.get_entry_by_path(f'category-index/chip-restaurants-{c1}{c2}.json')
            total += len(json.loads(bytes(e.get_item().content).decode()))
        except Exception: pass
if total == 0:
    try:
        e = a.get_entry_by_path('category-index/chip-restaurants.json')
        total = len(json.loads(bytes(e.get_item().content).decode()))
    except Exception: pass
print(total)
PYEOF
}

smoke_search() {  # prints estimated xapian matches
  "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null || echo 0
import sys
from libzim.reader import Archive
from libzim.search import Query, Searcher
a = Archive(sys.argv[1])
print(Searcher(a).search(Query().set_query(sys.argv[2])).getEstimatedMatches())
PYEOF
}

browser_smoke() {  # browser_smoke <zim> <src> <dst> ; returns node exit code
  local zim="$1" port=8801 out="/storage/streetzim/${ID}-smoke-${TODAY}.log"
  ln -sfn "../$(basename "$zim")" "web/$(basename "$zim")"
  # NOT python -m http.server: the site relies on Firebase cleanUrls/
  # trailingSlash, and plain http.server 404s on /drive/viewer/places/,
  # which the Find page and the routing bootstrap both fetch — every
  # browser gate would fail.
  "$PY" scripts/serve-web-local.py "$port" /storage/streetzim/web > "$TMPDIR/http-$port.log" 2>&1 &
  local http=$!; sleep 2
  STREETZIM_SITE="http://localhost:$port" ZIM_URL="http://localhost:$port/$(basename "$zim")" \
    SMOKE_ROUTE="$2;$3" timeout 600 "$NODE" cloud/pwa_smoke_test.mjs > "$out" 2>&1
  local rc=$?
  kill "$http" 2>/dev/null; rm -f "web/$(basename "$zim")"
  tail -3 "$out" | sed 's/^/    /' | tee -a "$LOG" >/dev/null
  return $rc
}

n_ok=0; n_fail=0; n_skip=0
while IFS=$'\t' read -r -u 3 ID NAME BBOX TIER SRC DST SEARCH NOTES; do
  [ -z "$ID" ] || [ "${ID:0:1}" = "#" ] && continue
  [ -n "$ONLY" ] && [[ "$ONLY" != *",$ID,"* ]] && continue
  [ -n "$SKIP" ] && [[ "$SKIP" == *",$ID,"* ]] && continue
  [ -n "$TIERS" ] && [[ "$TIERS" != *",$TIER,"* ]] && continue
  if [ $CONTINUE -eq 1 ] && awk -F'\t' -v id="$ID" '$1==id && ($2=="uploaded" || $2=="built-ok")' "$TSV" | grep -q .; then
    log "skip $ID (already OK in $TSV)"; n_skip=$((n_skip+1)); continue
  fi
  if [[ "$NOTES" == *RECONSTRUCTED* ]]; then
    log "NOTE $ID: bbox is RECONSTRUCTED — confirm before shipping ($BBOX)"
  fi
  log "=== $ID ($NAME) tier=$TIER bbox=$BBOX"
  if [ $DRY -eq 1 ]; then
    pbf="$REGDIR/$ID.osm.pbf"; st="extract"
    [ -f "$pbf" ] && [ ! -L "$pbf" ] && [ "$pbf" -nt "$PLANET" ] && st="ok"
    log "  dry-run: pbf=$st overture=$([ -f overture_cache/places-$ID-$OVERTURE_RELEASE.parquet ] && echo cached || echo download) out=osm-$ID-$TODAY.zim"
    continue
  fi
  T0=$(date +%s)

  link_world "$WORLD_MBTILES" "$REGDIR/$ID.mbtiles"
  link_world "$WORLD_SEARCH"  "$REGDIR/$ID.search.jsonl"

  PBF="$REGDIR/$ID.osm.pbf"
  if [ ! -f "$PBF" ] || [ -L "$PBF" ] || [ ! "$PBF" -nt "$PLANET" ]; then
    log "  extract PBF from $(basename "$PLANET")"
    rm -f "$PBF"
    if ! osmium extract -b "$BBOX" "$PLANET" -o "$PBF.part" --overwrite --strategy complete_ways >> "$LOG" 2>&1; then
      log "  EXTRACT FAILED"; row "$ID" extract-failed 0 - "osmium extract"; n_fail=$((n_fail+1)); continue
    fi
    mv -f "$PBF.part" "$PBF"
  fi
  log "  PBF: $(du -h "$PBF" | cut -f1)"

  ov_ok=1
  for theme in addresses places; do
    PQ="/storage/streetzim/overture_cache/${theme}-${ID}-${OVERTURE_RELEASE}.parquet"
    if [ ! -s "$PQ" ]; then
      log "  download Overture $theme $OVERTURE_RELEASE"
      if ! "$PY" download_overture_data.py "$theme" --bbox="$BBOX" --release "$OVERTURE_RELEASE" --out "$PQ" >> "$LOG" 2>&1; then
        log "  OVERTURE $theme DOWNLOAD FAILED"; rm -f "$PQ"; ov_ok=0
      fi
    fi
  done
  if [ $ov_ok -eq 0 ]; then
    row "$ID" overture-failed 0 - "download $OVERTURE_RELEASE"; n_fail=$((n_fail+1)); continue
  fi

  touch "$TMPDIR/.queue-t0-$ID"   # this run's start: the ZIM must post-date it
  log "  build (log: ${ID}-rebuild-${TODAY}.log, stdout: ${ID}-build.out)"
  bash /storage/streetzim/build-region-fast.sh "$ID" "$BBOX" "$NAME" > "/storage/streetzim/${ID}-build.out" 2>&1
  BUILD_RC=$?
  ZIM=$(ls -t /storage/streetzim/osm-${ID}-20*.zim 2>/dev/null | grep -v '\.tmp$' | head -1)
  MIN=$(( ($(date +%s) - T0) / 60 ))
  if [ "$BUILD_RC" -ne 0 ] || [ -z "$ZIM" ] || [ ! -s "$ZIM" ] || [ ! "$ZIM" -nt "$TMPDIR/.queue-t0-$ID" ]; then
    log "  BUILD FAILED rc=$BUILD_RC zim=${ZIM:-none} after ${MIN} min"
    row "$ID" build-failed "$MIN" - "rc=$BUILD_RC"; n_fail=$((n_fail+1)); continue
  fi
  SIZE=$(du -h "$ZIM" | cut -f1)
  log "  built $(basename "$ZIM") ($SIZE) in ${MIN} min"

  # ---------- gates ----------
  G=""
  if timeout 900 "$PY" cloud/check_terrain_coverage.py --zooms 10-12 -- "$ZIM" "$BBOX" >> "$LOG" 2>&1; then log "  gate terrain: OK"; else G="$G terrain"; log "  gate terrain: FAIL"; fi
  if TERRAIN_STRIPE_TOLERATE=10 timeout 1800 "$PY" cloud/validate_zim.py "$ZIM" >> "$LOG" 2>&1; then log "  gate validate: OK"; else G="$G validate"; log "  gate validate: FAIL"; fi
  ROUTE_OUT=$(timeout 2400 "$PY" cloud/route_cli.py --zim="$ZIM" --src="$SRC" --dst="$DST" --mode=all --max-pops=5000000 2>&1)
  echo "$ROUTE_OUT" | tail -6 | sed 's/^/    /' >> "$LOG"
  NOK=$(echo "$ROUTE_OUT" | grep -c "route OK")   # --mode=all runs astar + hwy2
  if [ "${NOK:-0}" -ge 2 ]; then log "  gate routing: OK (both modes)"; else G="$G routing"; log "  gate routing: FAIL (${NOK:-0}/2 modes)"; fi
  SN=$(smoke_search "$ZIM" "$SEARCH"); FN=$(smoke_find "$ZIM")
  [ "${SN:-0}" -ge 1 ] && log "  gate search('$SEARCH'): $SN" || { G="$G search"; log "  gate search: FAIL ($SN)"; }
  [ "${FN:-0}" -ge 1 ] && log "  gate find(restaurants): $FN" || { G="$G find"; log "  gate find: FAIL ($FN)"; }
  if [ "$BROWSER" != off ]; then
    if browser_smoke "$ZIM" "$SRC" "$DST"; then log "  gate browser: OK"
    elif [ "$BROWSER" = hard ]; then G="$G browser"; log "  gate browser: FAIL (hard)"
    else log "  gate browser: FAIL (soft — see ${ID}-smoke-${TODAY}.log)"; fi
  fi

  if [ -n "$G" ]; then
    log "  GATES FAILED:$G — NOT uploading $(basename "$ZIM")"
    row "$ID" gate-failed "$MIN" "$SIZE" "$G"; n_fail=$((n_fail+1)); continue
  fi
  if [ $UPLOAD -eq 0 ]; then
    row "$ID" built-ok "$MIN" "$SIZE" "gates passed, --no-upload"; n_ok=$((n_ok+1)); continue
  fi
  log "  all gates passed — uploading"
  if PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 bash cloud/upload_validated.sh "$ID" "$(basename "$ZIM")" >> "$LOG" 2>&1; then
    log "  uploaded → https://archive.org/details/streetzim-$ID"
    row "$ID" uploaded "$MIN" "$SIZE" "$(basename "$ZIM")"; n_ok=$((n_ok+1))
    # --keep-temp scratch is only useful for a failed build; reclaim it now.
    TD=$(find "$TMPDIR" -maxdepth 1 -type d -name 'osm_zim_*' -newer "$TMPDIR/.queue-t0-$ID" 2>/dev/null | head -1)
    if [ -n "$TD" ]; then
      log "  reclaiming build scratch $TD ($(du -sh "$TD" 2>/dev/null | cut -f1))"
      ionice -c3 nice -n 19 rm -rf "$TD"
    fi
  else
    log "  UPLOAD FAILED (rc=$?) — ZIM kept: $ZIM"
    row "$ID" upload-failed "$MIN" "$SIZE" "$(basename "$ZIM")"; n_fail=$((n_fail+1))
  fi
done 3< "$REGISTRY"

log "=== refresh queue complete: ok=$n_ok failed=$n_fail skipped=$n_skip — see $TSV"
[ $n_fail -eq 0 ]
