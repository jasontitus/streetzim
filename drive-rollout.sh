#!/bin/bash
# Drive-mode rollout queue (restarted 2026-04-21 after latest viewer fixes):
#   1. California + Japan — built in parallel (VRT race is fixed; shared
#      caches are read-only on the build path).
#   2. Australia / New Zealand — built solo after both of the above land,
#      to keep peak memory conservative.
# Each successful build is renamed to `osm-<id>-YYYY-MM-DD.zim`, uploaded
# to archive.org item `streetzim-<id>`, and followed by a
# `web/generate.py --deploy` so the listing on streetzim.web.app updates.

set -u
cd /Users/jasontitus/experiments/streetzim
# shellcheck disable=SC1091
source venv312/bin/activate
export ZSTD_CLEVEL=22

ROLLOUT_LOG=drive-rollout.log
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$ROLLOUT_LOG" ; }

disk_free_gb() { df -g /System/Volumes/Data | tail -1 | awk '{print $4}' ; }

upload_and_deploy() {
    local id="$1" src="$2"
    if [ ! -s "$src" ]; then
        log "SKIP ${id}: source ${src} missing or empty"
        return 1
    fi
    local today
    today=$(date +%Y-%m-%d)
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

# Builds a region synchronously; on success, uploads + deploys. Designed
# to be backgrounded so multiple regions can build concurrently.
build_and_ship() {
    local id="$1" name="$2" bbox="$3"
    local out="osm-${id}.zim"
    local build_log="${id}-drive-rollout.log"
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

log "=== Rollout restart (parallel CA+JP, then AusNZ solo) ==="
log "Disk free at start: $(disk_free_gb) GB"

# --- Wave 1: California + Japan in parallel --------------------------------
build_and_ship "california" "California" "-125.0,32.0,-114.0,42.2" &
CA_PID=$!
build_and_ship "japan"      "Japan"      "122.9,24.0,146.0,45.6"    &
JP_PID=$!

log "California PID=${CA_PID}, Japan PID=${JP_PID} — waiting for both"
wait "$CA_PID";  CA_RC=$?
wait "$JP_PID";  JP_RC=$?
log "Wave 1 complete (CA rc=${CA_RC}, JP rc=${JP_RC})"
log "Disk free after wave 1: $(disk_free_gb) GB"

# --- Wave 2: Australia / New Zealand ---------------------------------------
# Solo because the planet-PBF bbox extract + ZIM finalization are the two
# memory-heaviest stages and we'd rather not contend with another build.
if [ "$(disk_free_gb)" -lt 30 ]; then
    log "Aborting AusNZ: only $(disk_free_gb) GB free, need 30+"
    exit 1
fi
build_and_ship "australia-nz" "Australia & New Zealand" "112.0,-48.0,179.0,-10.0"

log "=== Drive-mode rollout complete ==="
