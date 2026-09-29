#!/usr/bin/env bash
# ops split, stage 1: this file lives in ops/ and a symlink at its old path
# runs it; the paths below assume that path (and so do pgrep and the locks).
# Started directly (ops/..., or from inside ops/), it re-runs by the old path.
if [ ! -L "$0" ] && _ops_real="$(readlink -f "$0" 2>/dev/null)"; then
  case "$_ops_real" in
    */ops/*) _ops_old="${_ops_real%/ops/*}/${_ops_real##*/ops/}"
             if [ -L "$_ops_old" ]; then exec bash "$_ops_old" "$@"; fi ;;
  esac
fi
unset _ops_real _ops_old
# Protect the shared NVMe from the tilemaker store.
#
# /mnt/data is shared with another project (st-bridge-models). The planet
# store is sparse and grows unpredictably, so rather than gamble on it
# fitting, stop the container cleanly when free space gets low. Stopping
# it makes build-world-tiles.sh's `docker run` return, which fires its
# EXIT trap and wipes the store — so the abort also frees the space.
#
# Usage: ./scripts/nvme-guard.sh [min_free_gb]   (default 40)
set -uo pipefail
MIN_GB="${1:-40}"
INTERVAL="${INTERVAL:-60}"
LOG=/storage/streetzim/nvme-guard.log
log() { printf "[%s] %s\n" "$(date -Iseconds)" "$*" >> "$LOG"; }

log "=== guard armed: stop tilemaker if /mnt/data free < ${MIN_GB} GB"
while true; do
  CID=$(docker ps -q --filter name=streetzim-tilemaker 2>/dev/null | head -1)
  if [ -z "$CID" ]; then
    log "no tilemaker container running — guard exiting"
    exit 0
  fi
  FREE=$(df -BG --output=avail /mnt/data 2>/dev/null | tail -1 | tr -dc 0-9)
  if [ -n "$FREE" ] && [ "$FREE" -lt "$MIN_GB" ]; then
    log "TRIPPED: /mnt/data has ${FREE} GB free (< ${MIN_GB}) — stopping $CID"
    log "  store on disk: $(du -sh /mnt/data/tilemaker/store 2>/dev/null | cut -f1)"
    docker stop -t 120 "$CID" >> "$LOG" 2>&1
    log "stopped; build-world-tiles.sh's EXIT trap wipes the store"
    log "ACTION NEEDED: rerun with STORE on /storage (slower) or free NVMe space"
    exit 3
  fi
  sleep "$INTERVAL"
done
