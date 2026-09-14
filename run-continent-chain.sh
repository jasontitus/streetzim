#!/bin/bash
# Rebuild the remaining continents, one at a time, after brazil proves the
# streaming packer (7a44564) survives a 93 GB manifest.
#
# Gated on brazil: if brazil dies with a build failure the packer is still the
# blocker, and running eight larger regions would burn days to fail the same
# way. Region list lives in continent-queue.list, re-read each iteration.
cd /storage/streetzim
LOG=/storage/streetzim/continent-chain.log
LIST=/storage/streetzim/continent-queue.list
MIN_FREE_GB="${MIN_FREE_GB:-500}"
log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" >> "$LOG"; }

log "=== chain armed; waiting for brazil"
while pgrep -f 'build-refresh-queue.sh' > /dev/null 2>&1; do sleep 120; done

BR=$(awk -F'\t' '$1=="brazil"{s=$2} END{print s}' queue-refresh.tsv 2>/dev/null)
log "brazil final status: ${BR:-unknown}"
case "$BR" in
  uploaded|gate-failed|upload-failed)
      log "brazil packed — the OOM class is closed; proceeding" ;;
  *)  log "brazil did NOT pack (status=${BR:-unknown}). STOPPING: the packer is"
      log "  still the blocker and eight larger regions would fail the same way."
      log "  Inspect brazil-build.out, then re-run this script to continue."
      exit 1 ;;
esac

export WORLD_MBTILES=/storage/streetzim/world-data/world-tiles-v3.mbtiles
export WORLD_SEARCH=/storage/streetzim/search_cache/world-2026-08-31.jsonl
export ALLOW_NO_SWAP=1

# fd 3, not stdin: build-refresh-queue.sh reads stdin and would otherwise eat
# lines out of the region list (the queue itself uses the same fd-3 trick).
while read -r -u 3 region; do
  case "$region" in ''|\#*) continue ;; esac
  free_gb=$(df -BG --output=avail /storage | tail -1 | tr -dc '0-9')
  if [ "${free_gb:-0}" -lt "$MIN_FREE_GB" ]; then
    log "STOPPING before $region: only ${free_gb}G free on /storage (need ${MIN_FREE_GB}G)"
    exit 2
  fi
  log "=== $region (free ${free_gb}G)"
  ./build-refresh-queue.sh --browser-smoke hard --only "$region" >> queue-continents.out 2>&1
  log "    $region finished rc=$? — $(awk -F'\t' -v r="$region" '$1==r{s=$2" "$4} END{print s}' queue-refresh.tsv)"
done 3< "$LIST"

log "=== chain complete"
