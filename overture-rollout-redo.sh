#!/bin/bash
# Full re-rollout after viewer-sync bug (2026-04-22 late).
#
# Cause: the worktree's `resources/viewer/index.html` was 2h stale when
# every today's build packaged it. All 2026-04-22 dated ZIMs currently
# on archive.org have:
#   * enriched search chunks (ws/p/soc/cat/brand present — good)
#   * the OLD viewer that reads `w` for website and still has the
#     removed Route button's code paths (bad on Kiwix Desktop — PWA is
#     fine since it uses the fresh Firebase-deployed viewer).
#
# Fix: sync happened before this script runs. Rebuild every region
# that already has a 2026-04-22 upload + the queue of new regions.
# Order: small first so more regions get shipped before bedtime.
#
#   Wave 1 (×3): DC + Colorado + Baltics                  [rebuild]
#   Wave 2 (×3): Silicon Valley + Hispaniola + Texas      [SV rebuild]
#   Wave 3 (×3): Iran + California + Central US           [CA rebuild]
#   Wave 4 (×3): Midwest-US + Japan + Indian Subcontinent [JP rebuild]
#   Wave 5 (×2): West Coast US + Australia/NZ             [rebuilds]
#   Wave 6 solo: East Coast US                            [rebuild]
#   Wave 7 solo: Central Asia                             [fresh]
#   Wave 8 solo: West Asia
#   Wave 9 solo: Africa
#   Wave 10 solo: United States
#   Wave 11 solo: Europe

set -u
cd /Users/jasontitus/experiments/streetzim
# shellcheck disable=SC1091
source /Users/jasontitus/experiments/streetzim/venv312/bin/activate
export ZSTD_CLEVEL=22

ROLLOUT_LOG=overture-rollout-redo.log
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$ROLLOUT_LOG" ; }
disk_free_gb() { df -g /System/Volumes/Data | tail -1 | awk '{print $4}' ; }

wait_for_parquet() {
    local path="$1"
    while :; do
        if [ -s "$path" ] && ! pgrep -f "download_overture_data.py.*$(basename "$path")" >/dev/null 2>&1; then
            local size_mb=$(du -m "$path" | awk '{print $1}')
            log "Parquet ready: $(basename "$path") (${size_mb} MB)"
            return
        fi
        log "Waiting for parquet: $(basename "$path")"
        sleep 30
    done
}

upload_and_deploy() {
    local id="$1" src="$2"
    if [ ! -s "$src" ]; then
        log "SKIP ${id}: source ${src} missing or empty"
        return 1
    fi
    local today
    today=$(date +%Y-%m-%d)
    local dated="/Users/jasontitus/experiments/streetzim/osm-${id}-${today}.zim"
    cp "$src" "$dated"
    log "Uploading $(basename "$dated") → streetzim-${id}..."
    cd /Users/jasontitus/experiments/streetzim
    ia upload "streetzim-${id}" "$(basename "$dated")" --retries 5 \
        >> "/Users/jasontitus/experiments/streetzim/${ROLLOUT_LOG}" 2>&1 || \
        log "Upload reported issues for ${id} — continuing"
    sleep 30
    ia metadata "streetzim-${id}" --modify="date:${today}" \
        >> "/Users/jasontitus/experiments/streetzim/${ROLLOUT_LOG}" 2>&1 || true
    python3 cloud/stamp_item_metadata.py "streetzim-${id}" \
        --routing --overture --terrain --satellite --wikidata \
        >> "/Users/jasontitus/experiments/streetzim/${ROLLOUT_LOG}" 2>&1 || \
        log "stamp skipped for ${id}"
    python3 cloud/cleanup_old_zims.py "streetzim-${id}" --keep 2 \
        >> "/Users/jasontitus/experiments/streetzim/${ROLLOUT_LOG}" 2>&1 || \
        log "cleanup skipped for ${id}"
    python3 web/generate.py --deploy \
        >> "/Users/jasontitus/experiments/streetzim/${ROLLOUT_LOG}" 2>&1 || \
        log "Web deploy failed for ${id} — continuing"
    cd /Users/jasontitus/experiments/streetzim
    log "DONE ${id}"
}

build_and_ship() {
    local id="$1" name="$2" bbox="$3"
    local addr_parquet="overture_cache/addresses-${id}-2026-04-15.0.parquet"
    local places_parquet="overture_cache/places-${id}-2026-04-15.0.parquet"
    local out="osm-${id}.zim"
    local build_log="${id}-overture-rollout.log"

    wait_for_parquet "$addr_parquet"
    wait_for_parquet "$places_parquet"

    log "=== Building ${id} (${name}) bbox=${bbox} ==="

    # STRUCTURAL PRE-BUILD GATE. Refuse to start the build if any
    # required input/cache/viewer-asset/tile is missing, stale, or
    # content-wrong. Prevents another "shipped broken ZIM, found
    # the hole after the fact" cycle. --audit-content is expensive
    # but catches the fresh-mtime-but-zero-fill case that burned us
    # on Iran / Butte MT.
    if ! python3 cloud/preflight.py --bbox="$bbox" --name "$id" \
            --zooms 0-12 --workers 16 --audit-content \
            > "${id}-preflight.log" 2>&1; then
        log "PREFLIGHT FAILED ${id} — see ${id}-preflight.log; NOT building"
        return 1
    fi
    log "preflight passed for ${id}"

    rm -f "$out"
    if ! python3 create_osm_zim.py \
          --mbtiles world-data/world-tiles-v2.mbtiles \
          --pbf world-data/planet-2026-03-10.osm.pbf \
          --bbox="$bbox" \
          --name "$name" \
          --satellite --satellite-download-zoom 12 \
          --terrain --wikidata --routing \
          --search-cache search_cache/world.jsonl \
          --overture-addresses "$addr_parquet" \
          --overture-places    "$places_parquet" \
          --keep-temp \
          --output "$out" > "$build_log" 2>&1; then
        log "BUILD FAILED ${id} — see ${build_log}"
        return 1
    fi
    if [ ! -s "$out" ]; then
        log "BUILD FAILED ${id} — no ZIM produced"
        return 1
    fi

    # STRUCTURAL POST-BUILD GATE. Every ZIM must pass the full
    # validator (tile coverage, search chunk sizes, routing entry
    # sizes, places.html present, etc.) before it's uploaded.
    # Exits nonzero on any FAIL — upload is skipped.
    if ! python3 cloud/validate_zim.py "$out" \
            > "${id}-validate.log" 2>&1; then
        log "VALIDATOR FAILED ${id} — see ${id}-validate.log; NOT uploading"
        return 1
    fi
    log "validator passed for ${id}"
    upload_and_deploy "$id" "$out"
}

log "=== redo rollout start ==="
log "Disk free: $(disk_free_gb) GB"

# Wave 1 (×3): DC + Colorado + Baltics  [rebuild]
build_and_ship "washington-dc" "Washington, D.C." "-77.12,38.79,-76.91,38.99" &
build_and_ship "colorado"      "Colorado"         "-109.06,36.99,-102.04,41.00" &
build_and_ship "baltics"       "Baltics"          "20.9,53.9,28.3,59.7" &
wait
log "Wave 1 done"; log "Disk free: $(disk_free_gb) GB"

# Wave 2 (×3): Silicon Valley + Hispaniola + Texas
build_and_ship "silicon-valley" "Silicon Valley" "-122.6,37.2,-121.7,37.9" &
build_and_ship "hispaniola"     "Hispaniola"     "-74.5,17.5,-68.3,20.1"   &
build_and_ship "texas"          "Texas"          "-106.7,25.8,-93.5,36.5"  &
wait
log "Wave 2 done"; log "Disk free: $(disk_free_gb) GB"

# Wave 3 (×3): Iran + California + Central US
build_and_ship "iran"       "Iran"       "44.0,25.0,63.5,39.8"     &
build_and_ship "california" "California" "-125.0,32.0,-114.0,42.2" &
build_and_ship "central-us" "Central US" "-120.0,31.3,-104.0,49.0" &
wait
log "Wave 3 done"; log "Disk free: $(disk_free_gb) GB"

# Wave 4 (×3): Midwest-US + Japan + Indian Subcontinent
build_and_ship "midwest-us"           "Midwest US"          "-104.1,36.0,-80.5,49.4" &
build_and_ship "japan"                "Japan"               "122.9,24.0,146.0,45.6"  &
build_and_ship "indian-subcontinent"  "Indian Subcontinent" "60.0,5.0,97.5,37.0"     &
wait
log "Wave 4 done"; log "Disk free: $(disk_free_gb) GB"

# Wave 5 (×2): West Coast US + Australia/NZ
build_and_ship "west-coast-us" "West Coast US"           "-125.0,32.0,-116.5,49.5" &
build_and_ship "australia-nz"  "Australia & New Zealand" "112.0,-48.0,179.0,-10.0" &
wait
log "Wave 5 done"; log "Disk free: $(disk_free_gb) GB"

# Wave 6 solo: East Coast US
build_and_ship "east-coast-us" "East Coast US" "-82.0,24.0,-66.5,47.6"

# Wave 7 solo: Central Asia (with Mongolia)
build_and_ship "central-asia"  "Central Asia"  "40.0,25.0,120.0,56.0"

# Wave 8 solo: West Asia
build_and_ship "west-asia"     "West Asia"     "25.0,12.0,62.5,42.0"

# Wave 9 solo: Africa
build_and_ship "africa"        "Africa"        "-20.0,-35.0,55.0,38.0"

# Wave 10 solo: United States
build_and_ship "united-states" "United States" "-125.0,24.0,-66.5,49.5"

# Wave 11 solo: Europe
build_and_ship "europe"        "Europe"        "-25.0,34.0,50.5,72.0"

log "=== redo rollout complete ==="
