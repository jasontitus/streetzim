#!/usr/bin/env bash
# Roll out the current resources/viewer/*.html into every built ZIM,
# validate, and upload to archive.org. Finishes with a web/generate.py
# --deploy so the site matches.
#
# Input: the list of (region_id, source_zim) pairs below — one row per
# region we've built. Each source must already pass the validator
# (that was Phase 1's work). This script just swaps in the current
# viewer set + re-uploads.
#
# Concurrency: 3 regions in parallel to leave room for Canada to
# keep running.
#
# Usage:
#   bash cloud/rollout_viewer_swap.sh          # full rollout
#   bash cloud/rollout_viewer_swap.sh --dry    # repackage+validate only
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
DRY=0
[ "${1:-}" = "--dry" ] && DRY=1

log() { printf "[rollout %s] %s\n" "$(date '+%H:%M:%S')" "$*"; }

# ----------------------------------------------------------------
# Region manifest. Columns: id, source ZIM, routing layout.
#
# layout = "mono"    — under the iOS WebView memory ceiling (~500MB):
#                      ship monolithic graph.bin only. Kiwix iOS +
#                      Desktop + PWA all load it directly, no
#                      reassembly, no lazy cells.
# layout = "spatial" — over the ceiling: ship spatial-chunked
#                      SZCI + SZRC format. The PWA viewer + mcpzim
#                      + Kiwix iOS only load per-cell edges on
#                      demand (a few MB per route), keeping peak
#                      memory bounded regardless of region size.
#
# Thresholds measured on 2026-04-24: Iran (520 MB chunked) failed
# on iOS; Egypt (316 MB monolithic) succeeded. 500 MB is the line.
# ----------------------------------------------------------------
regions=(
  "japan            osm-japan-v3.zim              spatial"
  "iran             osm-iran-v3.zim               spatial"
  "australia-nz     osm-australia-nz-v3.zim       spatial"
  "texas            osm-texas-v3.zim              spatial"
  "central-us       osm-central-us-v3.zim         spatial"
  "east-coast-us    osm-east-coast-us-v3.zim      spatial"
  "west-coast-us    osm-west-coast-us-v3.zim      spatial"
  "west-asia        osm-west-asia-v3.zim          spatial"
  "central-asia     osm-central-asia-final.zim    spatial"
  "egypt            osm-egypt-mono.zim            mono"
)

# ----------------------------------------------------------------
# Repackage a single region. Routing layout decision made by the
# caller based on graph total size (see region manifest above):
#   * "mono"    — keep source as-is, just swap viewer.
#   * "spatial" — switch to SZCI+SZRC format so mobile clients
#                 lazy-load per-cell edges and never hold the
#                 whole graph in memory.
# Validator runs last to block bad ships.
# ----------------------------------------------------------------
repack_one() {
    local id="$1" src="$2" layout="$3"
    local dst="osm-${id}-shipped.zim"
    local log="${id}-shipped.log"
    log "repackaging $id ($layout) from $src → $dst"
    local extra_args=()
    if [ "$layout" = "spatial" ]; then
        # --spatial-chunk-scale 1 = 1° cells. The source must be SZRG v4
        # (our default build); repackage_zim reassembles chunks if the
        # source shipped chunked.
        extra_args+=(--spatial-chunk-scale 1)
    fi
    if ! ./venv312/bin/python3 cloud/repackage_zim.py \
            "$src" "$dst" "${extra_args[@]}" > "$log" 2>&1; then
        log "FAIL repackage $id — see $log"
        return 1
    fi
    log "validating $dst"
    if ! ./venv312/bin/python3 cloud/validate_zim.py "$dst" \
            > "${id}-shipped-validate.log" 2>&1; then
        log "FAIL validate $id — see ${id}-shipped-validate.log"
        return 2
    fi
    log "OK $id"
}

# ----------------------------------------------------------------
# Parallel repackage+validate, 3 at a time.
# ----------------------------------------------------------------
log "beginning viewer swap on ${#regions[@]} regions (parallelism 3)"
export -f repack_one log
failures=()
running=0
pids=()
for row in "${regions[@]}"; do
    # Parse 3-column row: id, src, layout (mono|spatial). Whitespace-
    # delimited with arbitrary spacing so the manifest stays readable.
    read -r id src layout <<< "$row"
    repack_one "$id" "$src" "$layout" &
    pids+=($!)
    running=$((running+1))
    if [ $running -ge 3 ]; then
        wait -n 2>/dev/null || wait "${pids[0]}"
        running=$((running-1))
    fi
done
wait
log "all repackage+validate complete"

# ----------------------------------------------------------------
# Verify every produced ZIM exists + passed validation (the logs say
# so). Any failures → abort before uploading.
# ----------------------------------------------------------------
ok=()
for row in "${regions[@]}"; do
    id="${row%% *}"
    if grep -q "— PASS ===" "${id}-shipped-validate.log" 2>/dev/null; then
        ok+=("$id")
    else
        log "NOT READY: ${id}  (see ${id}-shipped-validate.log)"
        failures+=("$id")
    fi
done
if [ ${#failures[@]} -gt 0 ]; then
    log "ABORT: ${#failures[@]} region(s) failed validation: ${failures[*]}"
    exit 1
fi
log "${#ok[@]} regions passed validation: ${ok[*]}"

if [ $DRY -eq 1 ]; then
    log "--dry: stopping before upload"
    exit 0
fi

# ----------------------------------------------------------------
# Copy each to a dated filename (the archive.org naming convention
# used elsewhere in the project) + upload via the gated wrapper,
# which also does metadata modify, feature stamping, old-ZIM prune,
# and a web deploy.
#
# Sequential, not parallel — the ia CLI occasionally rate-limits and
# parallel uploads from one account tend to slow each other down
# rather than speed up.
# ----------------------------------------------------------------
today=$(date +%Y-%m-%d)
for id in "${ok[@]}"; do
    dated="osm-${id}-${today}.zim"
    log "staging ${id} → ${dated}"
    cp "osm-${id}-shipped.zim" "${dated}"
    log "uploading ${id}..."
    if ! bash cloud/upload_validated.sh "$id" "${dated}" \
            > "${id}-upload.log" 2>&1; then
        log "UPLOAD FAILED for ${id} — see ${id}-upload.log"
        failures+=("$id")
    else
        log "uploaded ${id}"
    fi
done

if [ ${#failures[@]} -gt 0 ]; then
    log "SOME UPLOADS FAILED: ${failures[*]}"
    exit 2
fi

# upload_validated.sh runs web/generate.py --deploy per region, so
# the site already reflects the new state. Nothing else to do.
log "rollout complete: ${#ok[@]} regions"
