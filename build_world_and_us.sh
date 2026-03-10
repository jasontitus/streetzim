#!/usr/bin/env bash
#
# build_world_and_us.sh - Build World and US-only ZIM files in one run.
#
# Downloads planet/US OSM data, generates vector tiles, and packages
# into ZIM files optimized for Kiwix (ZSTD level 22, 8 MiB clusters).
#
# Requirements: tilemaker, python3, python-libzim
# Disk space: ~80 GB free recommended (planet PBF is ~70 GB)
# RAM: 32+ GB recommended for planet-scale tilemaker
#
# Usage:
#   ./build_world_and_us.sh              # Build both world and US
#   ./build_world_and_us.sh --us-only    # Build US only
#   ./build_world_and_us.sh --world-only # Build world only
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREATE_SCRIPT="$SCRIPT_DIR/create_osm_zim.py"

# Activate venv if present
VENV_DIR="$SCRIPT_DIR/venv"
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
fi

# Output directory (current dir by default, override with OUT_DIR env var)
OUT_DIR="${OUT_DIR:-$(pwd)}"

# Compression settings
export ZSTD_CLEVEL="${ZSTD_CLEVEL:-22}"
CLUSTER_SIZE_KIB="${CLUSTER_SIZE_KIB:-8192}"  # 8 MiB
MAX_ZOOM="${MAX_ZOOM:-14}"

# Tilemaker performance flags
TILEMAKER_FAST="${TILEMAKER_FAST:-true}"    # Trade RAM for speed (needs 32+ GB)
TILEMAKER_STORE="${TILEMAKER_STORE:-}"      # On-disk temp store path (reduces RAM)

# Date stamp for filenames
DATE_STAMP="$(date +%Y-%m-%d)"

# Parse arguments
BUILD_WORLD=true
BUILD_US=true

for arg in "$@"; do
    case "$arg" in
        --us-only)    BUILD_WORLD=false ;;
        --world-only) BUILD_US=false ;;
        --help|-h)
            echo "Usage: $0 [--us-only|--world-only]"
            echo ""
            echo "Environment variables:"
            echo "  OUT_DIR         Output directory (default: current dir)"
            echo "  ZSTD_CLEVEL     ZSTD compression level (default: 22)"
            echo "  CLUSTER_SIZE_KIB  ZIM cluster size in KiB (default: 8192 = 8 MiB)"
            echo "  MAX_ZOOM        Max tile zoom level (default: 14)"
            echo "  TILEMAKER_FAST  Use --fast mode (default: true, needs 32+ GB RAM)"
            echo "  TILEMAKER_STORE Path for on-disk temp store (reduces RAM for planet)"
            echo "  PLANET_PBF      Path to existing planet PBF (skip download)"
            echo "  US_PBF          Path to existing US PBF (skip download)"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg"
            exit 1
            ;;
    esac
done

log() {
    echo ""
    echo "========================================"
    echo "  $1"
    echo "  $(date '+%Y-%m-%d %H:%M:%S')"
    echo "========================================"
    echo ""
}

elapsed() {
    local start=$1
    local end=$(date +%s)
    local dur=$((end - start))
    printf '%dh %dm %ds' $((dur/3600)) $((dur%3600/60)) $((dur%60))
}

download_with_retry() {
    local url="$1"
    local dest="$2"
    local desc="$3"
    local max_retries=4
    local wait=2

    for attempt in $(seq 1 $max_retries); do
        echo "  Downloading $desc (attempt $attempt/$max_retries)..."
        if wget -c -q --show-progress -O "$dest" "$url"; then
            echo "  Download complete: $(du -h "$dest" | cut -f1)"
            return 0
        fi
        if [ $attempt -lt $max_retries ]; then
            echo "  Download failed, retrying in ${wait}s..."
            sleep $wait
            wait=$((wait * 2))
        fi
    done
    echo "  ERROR: Failed to download $desc after $max_retries attempts"
    return 1
}

OVERALL_START=$(date +%s)

# ============================================================
# Build US ZIM
# ============================================================
if $BUILD_US; then
    log "Building US ZIM"
    US_START=$(date +%s)

    US_OUTPUT="$OUT_DIR/osm-united-states-${DATE_STAMP}.zim"
    US_GEOFABRIK="north-america/us"
    US_BBOX="-125.0,24.4,-66.9,49.4"
    US_PBF_URL="https://download.geofabrik.de/${US_GEOFABRIK}-latest.osm.pbf"

    # Download or reuse US PBF
    US_PBF="${US_PBF:-}"
    if [ -z "$US_PBF" ]; then
        US_PBF="$OUT_DIR/us-latest.osm.pbf"
        if [ -f "$US_PBF" ]; then
            echo "  Reusing existing US PBF: $(du -h "$US_PBF" | cut -f1)"
        else
            download_with_retry "$US_PBF_URL" "$US_PBF" "US OSM extract (~8 GB)"
        fi
    else
        echo "  Using provided US PBF: $US_PBF"
    fi

    echo "  Running create_osm_zim.py for US..."
    TILEMAKER_ARGS=()
    if [ "$TILEMAKER_FAST" = "true" ]; then TILEMAKER_ARGS+=(--fast); fi
    if [ -n "$TILEMAKER_STORE" ]; then TILEMAKER_ARGS+=(--store "$TILEMAKER_STORE"); fi

    python3 "$CREATE_SCRIPT" \
        --pbf "$US_PBF" \
        --bbox="$US_BBOX" \
        --name "United States" \
        --max-zoom "$MAX_ZOOM" \
        --cluster-size "$CLUSTER_SIZE_KIB" \
        --keep-temp \
        "${TILEMAKER_ARGS[@]}" \
        -o "$US_OUTPUT"

    echo ""
    echo "  US ZIM: $US_OUTPUT"
    echo "  Size:   $(du -h "$US_OUTPUT" | cut -f1)"
    echo "  Time:   $(elapsed $US_START)"
fi

# ============================================================
# Build World ZIM
# ============================================================
if $BUILD_WORLD; then
    log "Building World ZIM"
    WORLD_START=$(date +%s)

    WORLD_OUTPUT="$OUT_DIR/osm-world-${DATE_STAMP}.zim"
    PLANET_PBF_URL="https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf"

    # Download or reuse planet PBF
    PLANET_PBF="${PLANET_PBF:-}"
    if [ -z "$PLANET_PBF" ]; then
        PLANET_PBF="$OUT_DIR/planet-latest.osm.pbf"
        if [ -f "$PLANET_PBF" ]; then
            echo "  Reusing existing planet PBF: $(du -h "$PLANET_PBF" | cut -f1)"
        else
            download_with_retry "$PLANET_PBF_URL" "$PLANET_PBF" "Planet OSM extract (~70 GB)"
        fi
    else
        echo "  Using provided planet PBF: $PLANET_PBF"
    fi

    echo "  Running create_osm_zim.py for World..."
    TILEMAKER_ARGS=()
    if [ "$TILEMAKER_FAST" = "true" ]; then TILEMAKER_ARGS+=(--fast); fi
    if [ -n "$TILEMAKER_STORE" ]; then TILEMAKER_ARGS+=(--store "$TILEMAKER_STORE"); fi

    python3 "$CREATE_SCRIPT" \
        --pbf "$PLANET_PBF" \
        --name "World" \
        --max-zoom "$MAX_ZOOM" \
        --cluster-size "$CLUSTER_SIZE_KIB" \
        --keep-temp \
        "${TILEMAKER_ARGS[@]}" \
        -o "$WORLD_OUTPUT"

    echo ""
    echo "  World ZIM: $WORLD_OUTPUT"
    echo "  Size:     $(du -h "$WORLD_OUTPUT" | cut -f1)"
    echo "  Time:     $(elapsed $WORLD_START)"
fi

# ============================================================
# Summary
# ============================================================
log "Build Complete"
echo "  Total time: $(elapsed $OVERALL_START)"
echo "  ZSTD level: $ZSTD_CLEVEL"
echo "  Cluster size: $((CLUSTER_SIZE_KIB / 1024)) MiB"
echo "  Max zoom: $MAX_ZOOM"
echo ""
if $BUILD_US; then
    echo "  US:    $US_OUTPUT ($(du -h "$US_OUTPUT" | cut -f1))"
fi
if $BUILD_WORLD; then
    echo "  World: $WORLD_OUTPUT ($(du -h "$WORLD_OUTPUT" | cut -f1))"
fi
echo ""
echo "  Transfer .zim files to your device and open with Kiwix."
echo ""
