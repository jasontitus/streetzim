#!/usr/bin/env bash
# Rebuild everything the 2026-09 round held back, after the round ends.
#
# The region list lives in rebuild-queue.list and is re-read per region,
# so it can be edited while this is running. Editing THIS file while it
# runs corrupts bash's read position — replace it by rename instead.
set -uo pipefail
cd /storage/streetzim
LOG=/storage/streetzim/after-round-ca-dc.log
LIST=/storage/streetzim/rebuild-queue.list
log() { printf "[%s] %s\n" "$(date -Iseconds)" "$*" | tee -a "$LOG"; }

log "=== waiting for the main round to finish"
while pgrep -f '[b]uild-refresh-queue.sh' >/dev/null; do sleep 120; done
log "round finished; $(awk -F'\t' 'NR>1 && $2=="uploaded"' queue-refresh.tsv | wc -l) uploaded rows"

done_ids=""
while :; do
  next=""
  while read -r id; do
    case "$id" in ''|\#*) continue ;; esac
    case " $done_ids " in *" $id "*) continue ;; esac
    next="$id"; break
  done < "$LIST"
  [ -n "$next" ] || break
  done_ids="$done_ids $next"
  id="$next"

  log "=== rebuilding $id"
  rm -f "osm-${id}-$(date +%Y-%m-%d).zim"
  FORCE_REBUILD=1 ./ship-region.sh "$id" >> "$LOG" 2>&1
  rc=$?
  log "  ship-region $id rc=$rc — $(tail -1 "ship-${id}-$(date +%Y-%m-%d).log" 2>/dev/null | cut -c1-120)"
  if [ $rc -eq 0 ]; then
    log "  verify: $(venv-linux/bin/python3 -c "
from libzim.reader import Archive
import glob
z=sorted(glob.glob('osm-${id}-20??-??-??.zim'))[-1]
h=bytes(Archive(z).get_entry_by_path('index.html').get_item().content).decode('utf-8','replace')
print(z, 'NEW viewer' if 'visualViewport' in h and 'viewport-fit=cover' in h else 'OLD VIEWER — STILL STALE')" 2>&1)"
  fi
done
log "=== done"
