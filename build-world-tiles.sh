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
MEMORY="${MEMORY:-32g}"
THREADS="${THREADS:-28}"
IMAGE="${IMAGE:-ghcr.io/systemed/tilemaker:master}"
NAME="streetzim-tilemaker-$(date +%Y%m%d-%H%M%S)"
# NOT "$OUT.part": tilemaker selects its output driver from the extension
# ("target directory or .mbtiles/.pmtiles file"), so a .part suffix makes it
# write a directory of ~350M loose tile files instead of an MBTiles, and the
# failure only surfaces days later when build_search_cache.py cannot open it.
PART="${OUT%.mbtiles}.part.mbtiles"
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
    log "NVMe now: $(df -h "$(dirname "$STORE")" | tail -1 | awk '{print $4" free"}')"
    exit $rc
}
trap 'exit 143' TERM
trap 'exit 130' INT

# One tile build at a time. Without this a second invocation would share the
# store path and, once its EXIT trap armed, delete the running job's scratch.
exec 8>/storage/streetzim/tmp/.world-tiles.lock
flock -n 8 || { echo "another build-world-tiles.sh holds the lock — refusing" >&2; exit 1; }

[ -s "$PLANET" ] || { echo "planet missing: $PLANET" >&2; exit 1; }
[ -e "$OUT" ] && { echo "refusing to overwrite $OUT" >&2; exit 1; }
[ -s coastline/water_polygons.shp ] || { echo "coastline/water_polygons.shp missing" >&2; exit 1; }
[ -s resources/tilemaker/config-openmaptiles.json ] || { echo "tilemaker config missing" >&2; exit 1; }
# Holding the flock proves no other run owns the store, so anything left
# here is an orphan from a SIGKILL, an OOM kill or a reboot — the cases a
# trap cannot cover. Reclaim it before we add to the shared SSD.
if [ -d "$STORE" ]; then
    echo "reaping orphaned store ($(du -sh "$STORE" 2>/dev/null | cut -f1)) from a previous run"
    rm -rf "$STORE"
fi
mkdir -p "$STORE" || { echo "cannot create $STORE" >&2; exit 1; }
trap cleanup EXIT   # armed only now: every check above exits without touching the store

AVAIL_GB=$(df -BG --output=avail "$(dirname "$STORE")" | tail -1 | tr -dc 0-9)
MIN_START_GB="${MIN_START_GB:-340}"
if [ "$AVAIL_GB" -lt "$MIN_START_GB" ]; then
    echo "only ${AVAIL_GB} GB free at $(dirname "$STORE") — a planet store needs ~300 GB and this volume is shared." >&2
    echo "Set MIN_START_GB lower to override, or point STORE at /storage (slower)." >&2
    exit 1
fi

log "=== world tiles: planet=$(basename "$PLANET") out=$(basename "$OUT")"
log "    store=$STORE (${AVAIL_GB} GB free, wiped on exit)  mem=$MEMORY  threads=$THREADS"

docker run --rm --name "$NAME" \
    --user "$(id -u):$(id -g)" \
    --memory "$MEMORY" --memory-swap "$MEMORY" \
    -v /storage/streetzim:/srv \
    -v "$STORE":/store \
    -w /srv \
    "$IMAGE" \
    --input "/srv/${PLANET#/storage/streetzim/}" \
    --output "/srv/${PART#/storage/streetzim/}" \
    --config resources/tilemaker/config-openmaptiles.json \
    --process resources/tilemaker/process-openmaptiles.lua \
    --store /store \
    --shard-stores \
    --threads "$THREADS" \
    --skip-integrity \
    >> "$LOG" 2>&1 &
DOCKER_PID=$!
wait "$DOCKER_PID"
rc=$?
[ "$rc" -eq 0 ] || { log "tilemaker FAILED rc=$rc — leaving $PART for inspection"; [ "$rc" = "137" ] && log "  rc=137 = OOM-killed or stopped; if OOM, raise MEMORY (currently $MEMORY)"; exit "$rc"; }

[ -f "$PART" ] || { log "FATAL: tilemaker produced $(ls -ld "$PART" 2>/dev/null || echo nothing) — expected an MBTiles file"; exit 6; }
/storage/streetzim/venv-linux/bin/python3 -c "
import sqlite3,sys
c=sqlite3.connect('file:$PART?mode=ro', uri=True)
n=c.execute('select count(*) from tiles').fetchone()[0]
md=dict(c.execute('select name,value from metadata'))
print(f'  {n} tiles, maxzoom={md.get(\"maxzoom\")}, format={md.get(\"format\")}')
sys.exit(0 if n>1000 else 1)" 2>&1 | tee -a "$LOG" || { log "FATAL: $PART is not a usable MBTiles"; exit 6; }
mv -f "$PART" "$OUT"
log "=== mbtiles done: $(du -h "$OUT" | cut -f1)"

DATED=$(basename "$PLANET" .osm.pbf); DATED=${DATED#planet-}
SEARCH=/storage/streetzim/search_cache/world-${DATED}.jsonl
log "=== search cache → $SEARCH"
TMPDIR=/storage/streetzim/tmp /storage/streetzim/venv-linux/bin/python3 -u \
    cloud/build_search_cache.py --mbtiles "$OUT" --out "$SEARCH" >> "$LOG" 2>&1
src_rc=$?
if [ "$src_rc" -ne 0 ] || [ ! -s "$SEARCH" ]; then
    log "FATAL: search-cache extraction failed (rc=$src_rc). The MBTiles at $OUT is"
    log "  good and kept; rerun cloud/build_search_cache.py alone to finish."
    exit 7
fi

log "=== done. Point the queue at the new inputs:"
log "    WORLD_MBTILES=$OUT WORLD_SEARCH=$SEARCH ./build-refresh-queue.sh ..."
