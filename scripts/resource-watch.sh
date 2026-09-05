#!/usr/bin/env bash
# Emit a line ONLY when something crosses a threshold or a watched job
# ends. Silence means healthy. Designed as a Monitor command: every
# stdout line becomes a notification, so it must stay quiet when fine
# but must never stay quiet when a job dies.
#
# Re-alerts on a still-breached condition every RENOTIFY seconds so a
# slow leak is not reported once and then forgotten.
INTERVAL=${INTERVAL:-120}
RENOTIFY=${RENOTIFY:-1800}

# thresholds (GB unless noted)
STORAGE_MIN=${STORAGE_MIN:-600}      # /storage: builds need headroom
NVME_MIN=${NVME_MIN:-60}             # /mnt/data: shared with another project
ROOT_MIN=${ROOT_MIN:-8}              # / : 79 GB total, /tmp lives here
MEMAVAIL_MIN=${MEMAVAIL_MIN:-12}
SWAP_MAX=${SWAP_MAX:-4}
CTR_MEM_MAX=${CTR_MEM_MAX:-56}       # container cap is 64g

declare -A last              # condition -> last time we said it
say() {   # say <key> <message>
  local key="$1"; shift
  local now; now=$(date +%s)
  local prev=${last[$key]:-0}
  if [ $((now - prev)) -ge "$RENOTIFY" ]; then
    printf '[%s] %s\n' "$(date '+%H:%M')" "$*"
    last[$key]=$now
  fi
}
gb_avail() { df -BG --output=avail "$1" 2>/dev/null | tail -1 | tr -dc 0-9; }

# Which jobs we expect to be alive; when one vanishes we report how it ended.
watch_job() {  # watch_job <key> <pgrep-pattern> <logfile> <label>
  local key="$1" pat="$2" log="$3" label="$4"
  if pgrep -f "$pat" >/dev/null 2>&1; then
    seen[$key]=1
  elif [ "${seen[$key]:-0}" = "1" ]; then
    seen[$key]=0
    local tail_txt; tail_txt=$(tail -c 300 "$log" 2>/dev/null | tr '\n' ' ' | tail -c 200)
    printf '[%s] JOB ENDED: %s — last log: %s\n' "$(date '+%H:%M')" "$label" "$tail_txt"
  fi
}
declare -A seen

while true; do
  s=$(gb_avail /storage); n=$(gb_avail /mnt/data); r=$(gb_avail /)
  ma=$(awk '/MemAvailable/{printf "%d", $2/1048576}' /proc/meminfo)
  sw=$(free -g | awk '/^Swap:/{print $3}')

  [ -n "$s" ]  && [ "$s"  -lt "$STORAGE_MIN"  ] && say storage "LOW /storage: ${s} GB free (threshold ${STORAGE_MIN})"
  [ -n "$n" ]  && [ "$n"  -lt "$NVME_MIN"     ] && say nvme    "LOW /mnt/data (NVMe, shared): ${n} GB free — tilemaker store is $(du -sh /mnt/data/tilemaker/store 2>/dev/null | cut -f1)"
  [ -n "$r" ]  && [ "$r"  -lt "$ROOT_MIN"     ] && say root    "LOW / : ${r} GB free"
  [ -n "$ma" ] && [ "$ma" -lt "$MEMAVAIL_MIN" ] && say mem     "LOW memory: ${ma} GB available, swap ${sw} GB used"
  [ -n "$sw" ] && [ "$sw" -gt "$SWAP_MAX"     ] && say swap    "SWAP in use: ${sw} GB (mem available ${ma} GB)"

  # tilemaker container approaching its cap (silent OOM-kill risk)
  cm=$(docker stats --no-stream --format '{{.MemUsage}}' $(docker ps -q --filter name=streetzim-tilemaker) 2>/dev/null \
       | awk '{print $1}' | sed 's/GiB//' | cut -d. -f1)
  [ -n "$cm" ] && [ "$cm" -gt "$CTR_MEM_MAX" ] && say ctrmem "tilemaker container at ${cm} GiB of 64 GiB cap — silent OOM-kill risk"

  watch_job tiles   'build-world-tiles.sh'    /storage/streetzim/world-tiles-2026-09-05.out      'world tile build'
  watch_job extract 'extract-region-pbfs.sh'  /storage/streetzim/extract-regions-2026-09-05.out  'regional PBF extraction'
  watch_job queue   'build-refresh-queue.sh'  "/storage/streetzim/queue-refresh-$(date +%Y-%m-%d).log" 'region build queue'

  sleep "$INTERVAL"
done
