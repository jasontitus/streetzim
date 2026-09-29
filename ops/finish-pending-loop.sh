#!/bin/bash
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
