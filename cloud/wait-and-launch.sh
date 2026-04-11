#!/bin/bash
# Wait for critical caches to finish uploading to GCS, then launch the
# Africa and Asia build VMs (sequentially to avoid quota issues).
set -euo pipefail
cd "$(dirname "$0")"

BUCKET=gs://streetzim-cache

# Critical caches needed for non-US builds (we don't need us-tiles.mbtiles)
NEEDED=(
  "$BUCKET/world-data/world-tiles-v2.mbtiles"
  "$BUCKET/world-data/planet-2026-03-10.osm.pbf"
  "$BUCKET/search_cache/world.jsonl"
  "$BUCKET/wikidata_cache/manifest.json"
)

echo "Waiting for caches in $BUCKET ..."
while true; do
  missing=0
  for path in "${NEEDED[@]}"; do
    if ! gsutil -q stat "$path" 2>/dev/null; then
      missing=1
      echo "  [$(date '+%H:%M:%S')] Still waiting: $path"
      break
    fi
  done
  if [ $missing -eq 0 ]; then
    echo "All required caches present in GCS."
    break
  fi
  sleep 60
done

echo ""
echo "=== Launching Africa build VM ==="
bash launch-build-vm.sh africa

# Asia is bigger — launch immediately too (separate VM, no contention)
echo ""
echo "=== Launching Asia build VM ==="
bash launch-build-vm.sh asia

echo ""
echo "Both VMs launched. Tail logs with:"
echo "  gcloud compute ssh streetzim-build-africa --zone=us-central1-a -- tail -f /var/log/streetzim-build.log"
echo "  gcloud compute ssh streetzim-build-asia   --zone=us-central1-a -- tail -f /var/log/streetzim-build.log"
