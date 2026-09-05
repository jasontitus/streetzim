#!/usr/bin/env bash
# Regenerate the planet vector-tile MBTiles from a planet PBF, then the
# world search cache that the regional builds read.
#
# Runs tilemaker v3 in Docker (ghcr.io/systemed/tilemaker) because this
# host has no tilemaker and no build toolchain — apt's 2.4 is too old for
# resources/tilemaker/*, and cmake/boost/lua headers would need root.
# The container runs as the invoking user so every file it writes is
# ours to delete.
#
# Usage: ./build-world-tiles.sh [planet.osm.pbf] [out.mbtiles]
# Env:
#   STORE   node/way scratch store (default /mnt/data/tilemaker/store —
#           NVMe; the store is random-I/O heavy and /storage is a single
#           spinning disk). ALWAYS deleted when this script exits, however
#           it exits: the NVMe is shared with other work and must not keep
#           our files. Nothing else of ours is left there.
#   MEMORY  container memory cap (default 64g). --shard-stores keeps real
#           usage far below this; the cap exists so a runaway container is
#           killed instead of the host.
#   THREADS tilemaker worker threads (default 28 of 36, leaving cores for
#           a concurrent regional build).
#
# The output MBTiles stays on /storage (it is ~120 GB and permanent).
set -uo pipefail
cd /storage/streetzim

PLANET="${1:-/storage/streetzim/world-data/planet-2026-08-31.osm.pbf}"
OUT="${2:-/storage/streetzim/world-data/world-tiles-v3.mbtiles}"
STORE="${STORE:-/mnt/data/tilemaker/store}"
MEMORY="${MEMORY:-64g}"
THREADS="${THREADS:-28}"
IMAGE="${IMAGE:-ghcr.io/systemed/tilemaker:master}"
NAME="streetzim-tilemaker-$(date +%Y%m%d-%H%M%S)"
LOG=/storage/streetzim/world-tiles-$(date +%Y-%m-%d).log

log() { printf "[%s] %s\n" "$(date -Iseconds)" "$*" | tee -a "$LOG"; }

cleanup() {
    rc=$?
    log "cleanup (rc=$rc): stopping container + wiping the NVMe store"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    if [ -n "${STORE:-}" ] && [ -d "$STORE" ]; then
        du -sh "$STORE" 2>/dev/null | sed 's/^/    freeing /' | tee -a "$LOG"
        rm -rf "$STORE"
    fi
    # Leave the parent dir empty but present (it was created for us).
    rmdir "$(dirname "$STORE")"/* 2>/dev/null || true
    log "NVMe now: $(df -h "$(dirname "$STORE")" | tail -1 | awk '{print $4" free"}')"
    exit $rc
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

[ -s "$PLANET" ] || { echo "planet missing: $PLANET" >&2; exit 1; }
[ -e "$OUT" ] && { echo "refusing to overwrite $OUT" >&2; exit 1; }
[ -s coastline/water_polygons.shp ] || { echo "coastline/water_polygons.shp missing" >&2; exit 1; }
[ -s resources/tilemaker/config-openmaptiles.json ] || { echo "tilemaker config missing" >&2; exit 1; }
mkdir -p "$STORE" || { echo "cannot create $STORE" >&2; exit 1; }

AVAIL_GB=$(df -BG --output=avail "$(dirname "$STORE")" | tail -1 | tr -dc 0-9)
[ "$AVAIL_GB" -ge 300 ] || log "WARNING: only ${AVAIL_GB} GB free for the store (planet wants ~300)"

log "=== world tiles: planet=$(basename "$PLANET") out=$(basename "$OUT")"
log "    store=$STORE (${AVAIL_GB} GB free, wiped on exit)  mem=$MEMORY  threads=$THREADS"

docker run --rm --name "$NAME" \
    --user "$(id -u):$(id -g)" \
    --memory "$MEMORY" \
    -v /storage/streetzim:/srv \
    -v "$STORE":/store \
    -w /srv \
    "$IMAGE" \
    --input "/srv/${PLANET#/storage/streetzim/}" \
    --output "/srv/${OUT#/storage/streetzim/}.part" \
    --config resources/tilemaker/config-openmaptiles.json \
    --process resources/tilemaker/process-openmaptiles.lua \
    --store /store \
    --shard-stores \
    --threads "$THREADS" \
    --skip-integrity \
    2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
[ "$rc" -eq 0 ] || { log "tilemaker FAILED rc=$rc — leaving $OUT.part for inspection"; exit "$rc"; }

mv -f "$OUT.part" "$OUT"
log "=== mbtiles done: $(du -h "$OUT" | cut -f1)"

DATED=$(basename "$PLANET" .osm.pbf); DATED=${DATED#planet-}
SEARCH=/storage/streetzim/search_cache/world-${DATED}.jsonl
log "=== search cache → $SEARCH"
TMPDIR=/storage/streetzim/tmp /storage/streetzim/venv-linux/bin/python3 -u \
    cloud/build_search_cache.py --mbtiles "$OUT" --out "$SEARCH" 2>&1 | tee -a "$LOG"

log "=== done. Point the queue at the new inputs:"
log "    WORLD_MBTILES=$OUT WORLD_SEARCH=$SEARCH ./build-refresh-queue.sh ..."
