#!/bin/bash
# Restart the build on a running VM after fixing a dependency issue.
# Runs create_osm_zim.py + cache push + Archive.org upload + self-delete
# in a detached `nohup` session so it survives SSH disconnect.
set -euo pipefail
REGION="$1"
ZONE="${2:-us-central1-a}"
VM="streetzim-build-$REGION"

# Reload region config from the instance metadata we set at launch time.
REMOTE_SCRIPT='#!/bin/bash
set -euo pipefail
META=http://metadata.google.internal/computeMetadata/v1/instance/attributes
fetch() { curl -sf -H "Metadata-Flavor: Google" "$META/$1"; }

REGION_ID=$(fetch region-id)
REGION_NAME=$(fetch region-name)
REGION_BBOX=$(fetch region-bbox)
IA_ACCESS=$(fetch ia-access-key)
IA_SECRET=$(fetch ia-secret-key)
DESCRIPTION=$(fetch description | python3 -c "import sys, urllib.parse; print(urllib.parse.unquote(sys.stdin.read()))")
INSTANCE_NAME=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/name)
ZONE=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone | awk -F/ "{print \$NF}")

# Archive.org credentials
mkdir -p ~/.config/internetarchive
cat > ~/.config/internetarchive/ia.ini <<EOF
[s3]
access = $IA_ACCESS
secret = $IA_SECRET
EOF

cd /work/streetzim
source venv/bin/activate
ZIM_FILE="osm-${REGION_ID}.zim"

push_caches() {
  echo "=== [trap] Pushing updated caches back to GCS ==="
  gcloud storage rsync satellite_cache_avif_256/ gs://streetzim-cache/satellite_cache_avif_256/ --recursive 2>&1 || true
  gcloud storage rsync terrain_cache/ gs://streetzim-cache/terrain_cache/ --recursive 2>&1 || true
  gcloud storage rsync wikidata_cache/ gs://streetzim-cache/wikidata_cache/ --recursive 2>&1 || true
  echo "=== [trap] Cache push complete ==="
}
trap push_caches EXIT

echo "=== Building $REGION_NAME ==="
ZSTD_CLEVEL=22 python3 create_osm_zim.py \
  --mbtiles world-data/world-tiles-v2.mbtiles \
  --pbf world-data/planet-2026-03-10.osm.pbf \
  --bbox="$REGION_BBOX" \
  --name "$REGION_NAME" \
  --satellite --terrain --wikidata \
  --search-cache search_cache/world.jsonl \
  --keep-temp \
  --output "$ZIM_FILE"

push_caches

echo "=== Uploading to Archive.org ==="
ITEM_ID="streetzim-${REGION_ID}"
TITLE="StreetZim - Offline Map of $REGION_NAME"
ia upload "$ITEM_ID" "$ZIM_FILE" \
  --metadata="title:$TITLE" \
  --metadata="description:$DESCRIPTION" \
  --metadata="creator:StreetZim (create_osm_zim.py)" \
  --metadata="date:$(date +%Y-%m-%d)" \
  --metadata="subject:openstreetmap;offline maps;zim;kiwix;maplibre;vector tiles;satellite imagery;terrain;${REGION_ID}" \
  --metadata="mediatype:data" \
  --metadata="licenseurl:https://github.com/jasontitus/streetzim/blob/main/LICENSE" \
  --metadata="source:https://github.com/jasontitus/streetzim" \
  --metadata="collection:opensource_media" \
  --retries 5

gcloud storage cp "$ZIM_FILE" "gs://streetzim-cache/output/$ZIM_FILE"

echo "=== All done: $(date) ==="
gcloud --quiet compute instances delete "$INSTANCE_NAME" --zone="$ZONE"
'

# Launch as a detached root process so it survives SSH disconnect.
gcloud compute ssh "$VM" --zone="$ZONE" --project=streetzim --command="
sudo bash -c 'cat > /root/restart-build.sh' <<'SCRIPT'
$REMOTE_SCRIPT
SCRIPT
sudo chmod +x /root/restart-build.sh
sudo bash -c 'nohup /root/restart-build.sh > /var/log/streetzim-build.log 2>&1 &'
echo 'Build restarted; tail /var/log/streetzim-build.log'
"
