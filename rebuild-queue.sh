#!/bin/bash
# Sequential rebuild of regions known to have >10% broken terrain tiles
# (see https://github.com/jasontitus/streetzim and docs/). Runs only after
# the terrain-cache purge + Central US v3 build finish. Small-to-large
# ordering so early wins free disk quickly; pauses if free disk < 30 GB.

set -u
cd /Users/jasontitus/experiments/streetzim

log()            { echo "[$(date '+%H:%M:%S')] $*" | tee -a rebuild-queue.log ; }
disk_free_gb()   { df -g /System/Volumes/Data | tail -1 | awk '{print $4}' ; }

wait_for_disk() {
    while :; do
        local free
        free=$(disk_free_gb)
        if [ "$free" -ge 30 ]; then return; fi
        log "Waiting — only ${free} GB free, need 30+"
        sleep 120
    done
}

build_region() {
    local id="$1" name="$2" bbox="$3"
    local logname="rebuild-${id}.log"
    local out="osm-${id}-rebuild.zim"

    wait_for_disk
    log "=== ${id} (${name}) bbox=${bbox} ==="
    # shellcheck disable=SC1091
    source venv312/bin/activate
    export ZSTD_CLEVEL=22
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
          --output "$out" > "$logname" 2>&1; then
        log "FAILED ${id} — see ${logname}. Skipping upload."
        return 1
    fi
    if [ ! -f "$out" ]; then
        log "FAILED ${id} — no ZIM produced"
        return 1
    fi

    local date dated
    date=$(date +%Y-%m-%d)
    dated="osm-${id}-${date}.zim"
    cp "$out" "$dated"
    # Validate + upload through the shared path: it refuses unvalidated
    # ZIMs and treats a failed upload as fatal (no date bump, no deploy).
    if ! bash cloud/upload_validated.sh "$id" "$dated" >>"$logname" 2>&1; then
        log "FAIL upload ${id} — see ${logname}"
        return 1
    fi
    log "=== DONE ${id} ==="
}

log "Starting queue (DC/Colorado/SV already rebuilt cleanly; retrying Iran/WCUS/Baltics/Central US that failed old audit)."

# build_region's `return 1` used to be silently swallowed (no set -e), so the
# queue printed ALL REBUILDS COMPLETE and exited 0 after failures.
FAILED=()
run() { build_region "$@" || FAILED+=("$1"); }

# Retry regions that failed prior audits + remaining tier 2/3.
run "iran"              "Iran"                    "44.0,25.0,63.0,40.0"
run "west-coast-us"     "West Coast US"           "-125.0,32.0,-116.5,49.5"
run "baltics"           "Baltics"                 "20.9,53.9,28.3,59.7"
run "central-us"        "Central US"              "-120.0,31.3,-104.0,49.0"
run "california"        "California"              "-125.0,32.0,-114.0,42.2"
run "east-coast-us"     "East Coast US"           "-82.0,24.0,-66.5,47.6"
run "west-asia"         "West Asia"               "25.0,12.0,63.0,45.0"
run "japan"             "Japan"                   "122.9,24.0,146.0,45.6"

# Large (last — require most disk headroom)
run "australia-nz"      "Australia & New Zealand" "112.0,-48.0,179.0,-10.0"
run "europe"            "Europe"                  "-25.0,34.0,50.5,72.0"
run "united-states"     "United States"           "-125.0,24.0,-66.5,49.5"

if [ ${#FAILED[@]} -gt 0 ]; then
    log "=== QUEUE FINISHED WITH FAILURES: ${FAILED[*]} ==="
    exit 4
fi
log "=== ALL REBUILDS COMPLETE ==="
