#!/bin/bash
# Rebuild regions from a list, one at a time, gated on an earlier region having
# packed (default brazil: the streaming packer's first continent-scale test).
#
# The list is RE-READ before every region, so editing it mid-run is safe in
# every way people actually edit files: append, comment a line out, reorder, or
# replace it atomically (vim, sed -i). The previous runner held the file open on
# fd 3 and consumed it by byte offset, which dropped appends without a trailing
# newline, ended early on in-place rewrites, never saw atomic replacements, and
# read garbage after a comment-out shifted the offset.
#
# A region listed N times runs N times (a rebuild is just a second line). Order
# follows the file: a line is due when its region has run fewer times than it
# appears up to and including that line.
cd /storage/streetzim
LOG="${LOG:-/storage/streetzim/continent-chain.log}"
LIST="${LIST:-/storage/streetzim/continent-queue.list}"
TSV="${TSV:-/storage/streetzim/queue-refresh.tsv}"
REGISTRY="${REGISTRY:-/storage/streetzim/cloud/regions.tsv}"
QUEUE="${QUEUE:-./build-refresh-queue.sh}"
GATE_REGION="${GATE_REGION:-brazil}"
MIN_FREE_GB="${MIN_FREE_GB:-500}"
log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" >> "$LOG"; }

# Match only a bash actually running the queue script. A bare
# `pgrep -f build-refresh-queue.sh` also matched shells, editors and `tail`
# processes that merely mention the name, and could wait forever.
queue_running(){ pgrep -f '^(/usr)?(/bin/)?bash (\./|/storage/streetzim/)?build-refresh-queue\.sh( |$)' >/dev/null 2>&1; }

log "=== chain armed (list re-read per region); waiting for any running queue"
if [ "${WAIT_FOR_QUEUE:-1}" = 1 ]; then
  while queue_running; do sleep 120; done
fi

if [ -n "$GATE_REGION" ]; then
  GS=$(awk -F'\t' -v r="$GATE_REGION" '$1==r{s=$2} END{print s}' "$TSV" 2>/dev/null)
  log "$GATE_REGION final status: ${GS:-unknown}"
  case "$GS" in
    uploaded|gate-failed|upload-failed) log "$GATE_REGION packed — proceeding" ;;
    *) log "$GATE_REGION did NOT pack (status=${GS:-unknown}). STOPPING."; exit 1 ;;
  esac
fi

export WORLD_MBTILES="${WORLD_MBTILES:-/storage/streetzim/world-data/world-tiles-v3.mbtiles}"
export WORLD_SEARCH="${WORLD_SEARCH:-/storage/streetzim/search_cache/world-2026-08-31.jsonl}"
export ALLOW_NO_SWAP=1
declare -A RUNS=()

next_region(){
  declare -A seen=()
  local line r
  while IFS= read -r line || [ -n "$line" ]; do
    r="${line%%#*}"; r="$(echo "$r" | tr -d '[:space:]')"
    [ -z "$r" ] && continue
    seen[$r]=$(( ${seen[$r]:-0} + 1 ))
    if [ "${seen[$r]}" -gt "${RUNS[$r]:-0}" ]; then echo "$r"; return 0; fi
  done < "$LIST"
  return 1
}

while region=$(next_region); do
  RUNS[$region]=$(( ${RUNS[$region]:-0} + 1 ))
  if ! awk -F'\t' -v r="$region" '$1==r{f=1} END{exit !f}' "$REGISTRY" 2>/dev/null; then
    log "SKIPPING '$region': not a region id in $REGISTRY (typo?)"
    continue
  fi
  free_gb=$(df -BG --output=avail /storage | tail -1 | tr -dc '0-9')
  if [ "${free_gb:-0}" -lt "$MIN_FREE_GB" ]; then
    log "STOPPING before $region: only ${free_gb}G free on /storage (need ${MIN_FREE_GB}G)"
    exit 2
  fi
  log "=== $region (run #${RUNS[$region]}, free ${free_gb}G)"
  $QUEUE --browser-smoke hard --only "$region" >> queue-continents.out 2>&1
  rc=$?
  log "    $region finished rc=$rc — $(awk -F'\t' -v r="$region" '$1==r{s=$2" "$4} END{print s}' "$TSV")"
done
log "=== chain complete"
