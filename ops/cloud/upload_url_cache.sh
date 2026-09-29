#!/usr/bin/env bash
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
