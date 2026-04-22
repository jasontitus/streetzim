#!/bin/bash
# Drive-mode v4 rollout queue — big regions:
#   Wave 1: East Coast US + West Coast US  (parallel; similar size)
#   Wave 2: United States                  (solo; ~30M edges)
#   Wave 3: Europe                         (solo; ~50M edges, biggest)
# Sequential waves instead of 3-parallel keep peak RAM + temp-dir
# footprint predictable — US + Europe alone each approach the
# memory envelope that the SV+CA+JP trio stayed inside of.

set -u
cd /Users/jasontitus/experiments/streetzim
# shellcheck disable=SC1091
source venv312/bin/activate
export ZSTD_CLEVEL=22

ROLLOUT_LOG=drive-rollout.log
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$ROLLOUT_LOG" ; }

disk_free_gb() { df -g /System/Volumes/Data | tail -1 | awk '{print $4}' ; }

wait_for_disk() {
    while :; do
        local free; free=$(disk_free_gb)
        if [ "$free" -ge 40 ]; then return; fi
        log "Waiting — only ${free} GB free, need 40+"
        sleep 120
    done
}

upload_and_deploy() {
    local id="$1" src="$2"
    if [ ! -s "$src" ]; then
        log "SKIP ${id}: source ${src} missing or empty"
        return 1
    fi
    local today; today=$(date +%Y-%m-%d)
    local dated="osm-${id}-${today}.zim"
    if [ "$src" != "$dated" ]; then
        cp "$src" "$dated"
    fi
    log "Uploading ${dated} → streetzim-${id}..."
    ia upload "streetzim-${id}" "$dated" --retries 5 >>"$ROLLOUT_LOG" 2>&1 || \
        log "Upload reported issues for ${id} — continuing"
    sleep 30
    ia metadata "streetzim-${id}" --modify="date:${today}" >>"$ROLLOUT_LOG" 2>&1 || true
    python3 web/generate.py --deploy >>"$ROLLOUT_LOG" 2>&1 || \
        log "Web deploy failed for ${id} — continuing"
    log "DONE ${id}"
}

build_and_ship() {
    local id="$1" name="$2" bbox="$3"
    local out="osm-${id}.zim"
    local build_log="${id}-drive-rollout.log"
    wait_for_disk
    log "=== Building ${id} (${name}) bbox=${bbox} ==="
    rm -f "$out"
    if ! python3 create_osm_zim.py \
          --mbtiles world-data/world-tiles-v2.mbtiles \
          --pbf world-data/planet-2026-03-10.osm.pbf \
          --bbox="$bbox" \
          --name "$name" \
          --satellite --satellite-download-zoom 12 \
          --terrain --wikidata --routing \
          --search-cache search_cache/world.jsonl \
          --keep-temp \
          --output "$out" > "$build_log" 2>&1; then
        log "BUILD FAILED ${id} — see ${build_log}"
        return 1
    fi
    if [ ! -s "$out" ]; then
        log "BUILD FAILED ${id} — no ZIM produced"
        return 1
    fi
    upload_and_deploy "$id" "$out"
}

log "=== v4 big-regions rollout start ==="
log "Disk free at start: $(disk_free_gb) GB"

# --- Wave 1: East Coast US + West Coast US (parallel) --------------------
build_and_ship "east-coast-us" "East Coast US" "-82.0,24.0,-66.5,47.6"   &
EC_PID=$!
build_and_ship "west-coast-us" "West Coast US" "-125.0,32.0,-116.5,49.5" &
WC_PID=$!
log "EC=${EC_PID}, WC=${WC_PID} — waiting"
wait "$EC_PID"; EC_RC=$?
wait "$WC_PID"; WC_RC=$?
log "Wave 1 complete (EC rc=${EC_RC}, WC rc=${WC_RC})"
log "Disk free after wave 1: $(disk_free_gb) GB"

# --- Wave 2: United States (solo) ----------------------------------------
build_and_ship "united-states" "United States" "-125.0,24.0,-66.5,49.5"
log "Disk free after wave 2: $(disk_free_gb) GB"

# --- Wave 3: Europe (solo) -----------------------------------------------
build_and_ship "europe" "Europe" "-25.0,34.0,50.5,72.0"

log "=== v4 big-regions rollout complete ==="
