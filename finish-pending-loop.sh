#!/bin/bash
# Run cloud/finish_pending_uploads.sh every 30 min while the chip retrofit
# queue is running or anything is still pending; stop when both are done.
# Never edit while running.
cd /storage/streetzim
log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*"; }
queue_running(){ ps -eo args= | awk '$1 ~ /(^|\/)bash$/ && $2 ~ /(^|\/)retrofit-chips-queue\.sh$/ {f=1} END{exit !f}'; }
log "finish-pending loop start"
while :; do
  if [ -s pending-uploads.tsv ]; then
    log "running finisher ($(wc -l < pending-uploads.tsv) pending)"
    bash cloud/finish_pending_uploads.sh
  fi
  if ! queue_running && [ ! -s pending-uploads.tsv ]; then
    log "retrofit queue finished and nothing pending — exiting"; break
  fi
  sleep 1800
done
