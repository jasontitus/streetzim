#!/usr/bin/env bash
# Gated single-region build.
#
# Defaults:
#   * Satellite ON, terrain ON, wikidata ON, routing ON
#   * Overture addresses + places ON (auto-downloaded if not cached)
#   * Preflight BEFORE build (structural gate)
#   * Validate AFTER build (semantic gate)
#
# Usage:
#   bash cloud/build_region.sh <id> "<name>" "<minlon,minlat,maxlon,maxlat>"
#
# Examples:
#   bash cloud/build_region.sh egypt  "Egypt"  "24.5,21.5,37.0,32.0"
#   bash cloud/build_region.sh canada "Canada" "-141.0,41.5,-52.0,83.5"
#
# Does NOT upload — that's a separate step (cloud/upload_validated.sh).

set -euo pipefail

id="$1"
name="$2"
bbox="$3"
release="${OVERTURE_RELEASE:-2026-04-15.0}"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

addr="overture_cache/addresses-${id}-${release}.parquet"
places="overture_cache/places-${id}-${release}.parquet"
out="osm-${id}.zim"
log="${id}-build.log"

log() { printf "[build %s] %s\n" "$id" "$*"; }

# ------------------------------------------------------------------
# 1. Ensure Overture parquets exist. Download in parallel if not.
#    Address data is sparse outside US/Europe — a 479-byte parquet
#    is legitimate for some regions (Central Asia, Egypt, Iran) and
#    still gets passed to create_osm_zim (no-op effect).
# ------------------------------------------------------------------
missing=()
[ ! -s "$addr" ] && missing+=("$addr")
[ ! -s "$places" ] && missing+=("$places")
if [ ${#missing[@]} -gt 0 ]; then
    log "overture cache miss: ${missing[*]} — downloading..."
    if [ ! -s "$addr" ]; then
        ./venv312/bin/python3 download_overture_data.py addresses \
            --bbox="$bbox" --release "$release" --out "$addr" \
            > "overture-${id}-addresses.log" 2>&1 &
        addr_pid=$!
    fi
    if [ ! -s "$places" ]; then
        ./venv312/bin/python3 download_overture_data.py places \
            --bbox="$bbox" --release "$release" --out "$places" \
            > "overture-${id}-places.log" 2>&1 &
        places_pid=$!
    fi
    [ -n "${addr_pid:-}" ] && wait $addr_pid || true
    [ -n "${places_pid:-}" ] && wait $places_pid || true
    log "overture: addresses=$(stat -f %z "$addr")B places=$(stat -f %z "$places")B"
fi

# ------------------------------------------------------------------
# 2. Preflight — refuses build if inputs/caches are missing or
#    content-corrupt.
# ------------------------------------------------------------------
log "preflight..."
if ! ./venv312/bin/python3 cloud/preflight.py \
        --bbox="$bbox" --name "$id" \
        --zooms 0-12 --workers 16 --audit-content \
        > "${id}-preflight.log" 2>&1; then
    echo "[FATAL] preflight failed for $id — see ${id}-preflight.log"
    echo "        (some failures can be post-fixed; see README)"
    # Preflight exits nonzero on ANY failed gate. Some regions have
    # known-not-a-bug issues (sparse wikidata, ocean-coast false
    # positives). Continue only if the $FORCE flag is set. This
    # keeps us from shipping broken ZIMs by accident while still
    # letting us force known-OK cases when needed.
    [ "${FORCE:-0}" = "1" ] || exit 1
fi

# ------------------------------------------------------------------
# 3. Build with every knob turned on — natively emits chunked graph,
#    split-hot search chunks, and world-VRT z=0-7 terrain in a single
#    pass. No post-build repackage needed.
# ------------------------------------------------------------------
vrt32k="terrain_cache/dem_sources/world_dem_32k.tif"
LOW_ZOOM_VRT_ARG=()
if [ -s "$vrt32k" ]; then
    LOW_ZOOM_VRT_ARG=(--low-zoom-world-vrt "$vrt32k")
fi

log "build start bbox=$bbox name=\"$name\""
rm -f "$out"
if ! ./venv312/bin/python3 create_osm_zim.py \
        --mbtiles world-data/world-tiles-v2.mbtiles \
        --pbf world-data/planet-2026-03-10.osm.pbf \
        --bbox="$bbox" \
        --name "$name" \
        --satellite --satellite-download-zoom 12 \
        --terrain --wikidata --routing \
        --search-cache search_cache/world.jsonl \
        --overture-addresses "$addr" \
        --overture-places "$places" \
        --chunk-graph-mb 200 \
        --split-hot-search-chunks-mb 10 \
        "${LOW_ZOOM_VRT_ARG[@]}" \
        --output "$out" \
        --keep-temp \
        > "$log" 2>&1; then
    echo "[FATAL] build failed for $id — see $log"
    exit 2
fi

# ------------------------------------------------------------------
# 5. Validate — hard gate on structural + content integrity.
# ------------------------------------------------------------------
log "validate..."
if ! ./venv312/bin/python3 cloud/validate_zim.py "$out" \
        > "${id}-validate.log" 2>&1; then
    echo "[FATAL] validator failed for $id — see ${id}-validate.log"
    exit 4
fi

log "OK: $out"
echo "$out"
