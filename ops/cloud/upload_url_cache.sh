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
# Upload the URL liveness cache to gs://streetzim-cache/ for the build VM.
# Safe to run mid-crawl (uploads current snapshot). Re-run after crawl finishes
# for the final cache.
set -euo pipefail

cd "$(dirname "$0")/.."

LOCAL=url_validation_cache.json
REMOTE=gs://streetzim-cache/url_validation_cache.json

if [ ! -f "$LOCAL" ]; then
  echo "missing: $LOCAL" >&2
  exit 1
fi

SIZE=$(ls -lh "$LOCAL" | awk '{print $5}')
echo "[upload-url-cache] uploading $LOCAL ($SIZE) -> $REMOTE"
gcloud storage cp "$LOCAL" "$REMOTE"
echo "[upload-url-cache] done"
