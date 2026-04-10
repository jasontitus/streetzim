#!/bin/bash
# StreetZim build VM startup script.
#
# Reads region config from instance metadata, pulls caches from GCS,
# runs the build, uploads the resulting ZIM to Archive.org and GCS,
# then deletes the VM.
#
# Required instance metadata:
#   region-id        e.g. "africa", "asia"
#   region-name      e.g. "Africa"
#   region-bbox      e.g. "-18.0,-35.0,52.0,38.0"
#   ia-access-key    Archive.org S3 access key
#   ia-secret-key    Archive.org S3 secret key
#   description      Long description with country list (URL-encoded)
set -euxo pipefail

LOG=/var/log/streetzim-build.log
exec > >(tee -a $LOG) 2>&1

echo "=== StreetZim build VM started: $(date) ==="

# ----------------------------------------------------------------------------
# Push updated caches back to GCS — runs on exit regardless of success/failure
# so that satellite tiles, terrain tiles, and DEM cells downloaded during the
# build are never lost to a retry.
# ----------------------------------------------------------------------------
push_caches() {
  echo "=== [trap] Pushing updated caches back to GCS ==="
  cd /work/streetzim 2>/dev/null || return 0
  gcloud storage rsync satellite_cache_avif_256/ gs://streetzim-cache/satellite_cache_avif_256/ --recursive 2>&1 || true
  # Include dem_sources — fresh DEM cells downloaded by this build.
  gcloud storage rsync terrain_cache/ gs://streetzim-cache/terrain_cache/ --recursive 2>&1 || true
  gcloud storage rsync wikidata_cache/ gs://streetzim-cache/wikidata_cache/ --recursive 2>&1 || true
  echo "=== [trap] Cache push complete ==="
}
trap push_caches EXIT

# ----------------------------------------------------------------------------
# Read instance metadata
# ----------------------------------------------------------------------------
META=http://metadata.google.internal/computeMetadata/v1/instance/attributes
fetch_meta() { curl -sf -H "Metadata-Flavor: Google" "$META/$1"; }

REGION_ID=$(fetch_meta region-id)
REGION_NAME=$(fetch_meta region-name)
REGION_BBOX=$(fetch_meta region-bbox)
IA_ACCESS=$(fetch_meta ia-access-key)
IA_SECRET=$(fetch_meta ia-secret-key)
DESCRIPTION=$(fetch_meta description | python3 -c "import sys, urllib.parse; print(urllib.parse.unquote(sys.stdin.read()))")

INSTANCE_NAME=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/name)
ZONE=$(curl -sf -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone | awk -F/ '{print $NF}')

echo "Region: $REGION_NAME ($REGION_ID) bbox=$REGION_BBOX"

# ----------------------------------------------------------------------------
# System setup
# ----------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3 python3-pip python3-venv \
  build-essential cmake pkg-config libxapian-dev libzstd-dev liblzma-dev \
  libicu-dev gdal-bin libgdal-dev pigz curl

mkdir -p /work
cd /work

# ----------------------------------------------------------------------------
# Clone repo (latest main)
# ----------------------------------------------------------------------------
git clone --depth 1 https://github.com/jasontitus/streetzim.git
cd streetzim

# ----------------------------------------------------------------------------
# Python venv + deps
# ----------------------------------------------------------------------------
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install internetarchive libzim osmium

# ----------------------------------------------------------------------------
# Pull caches from GCS
# ----------------------------------------------------------------------------
echo "=== Pulling caches from gs://streetzim-cache ==="
mkdir -p world-data search_cache wikidata_cache satellite_cache_avif_256 terrain_cache/dem_sources

# Pull only what we need — skip world-tiles.mbtiles (v1, deprecated; we use v2).
# Use gcloud storage (newer, doesn't hang like gsutil).
# Skip dem_sources entirely — at 547 GB it would dwarf the disk. The build
# script downloads only the DEM cells covering its bbox from the public
# Copernicus S3 bucket on demand.
gcloud storage cp gs://streetzim-cache/world-data/world-tiles-v2.mbtiles world-data/world-tiles-v2.mbtiles
gcloud storage cp gs://streetzim-cache/world-data/planet-2026-03-10.osm.pbf world-data/planet-2026-03-10.osm.pbf
gcloud storage cp gs://streetzim-cache/search_cache/world.jsonl            search_cache/world.jsonl
gcloud storage rsync gs://streetzim-cache/wikidata_cache/                  wikidata_cache/ --recursive

# Restore PBF mtime to match the cached qids_*.json key. The wikidata_cache.py
# key encoding is qids_<pbf_name>_<size>_<mtime>.json — if we don't restore the
# original mtime, the script re-scans the full 85 GB PBF unnecessarily.
for cached in wikidata_cache/qids_planet-*.json; do
  [ -e "$cached" ] || continue
  # Extract mtime (last _NNNN.json component before the extension)
  mtime=$(basename "$cached" .json | awk -F_ '{print $NF}')
  if [ -n "$mtime" ] && [ -f world-data/planet-2026-03-10.osm.pbf ]; then
    echo "Restoring PBF mtime to $mtime for wikidata cache hit"
    touch -d "@$mtime" world-data/planet-2026-03-10.osm.pbf
    break
  fi
done

# Tarballs (streamed and extracted in place)
if gcloud storage objects describe gs://streetzim-cache/tarballs/satellite_cache_avif_256.tar &>/dev/null; then
  echo "Extracting satellite_cache_avif_256.tar..."
  gcloud storage cat gs://streetzim-cache/tarballs/satellite_cache_avif_256.tar | tar -xf -
fi
if gcloud storage objects describe gs://streetzim-cache/tarballs/terrain_cache_tiles.tar &>/dev/null; then
  echo "Extracting terrain_cache_tiles.tar..."
  gcloud storage cat gs://streetzim-cache/tarballs/terrain_cache_tiles.tar | tar -xf -
fi

# ----------------------------------------------------------------------------
# Configure Archive.org credentials
# ----------------------------------------------------------------------------
mkdir -p ~/.config/internetarchive
cat > ~/.config/internetarchive/ia.ini <<EOF
[s3]
access = $IA_ACCESS
secret = $IA_SECRET
EOF

# ----------------------------------------------------------------------------
# Run the build
# ----------------------------------------------------------------------------
ZIM_FILE="osm-${REGION_ID}.zim"

# Periodic cache push — save progress every 15 minutes so a sudden
# shutdown doesn't lose hours of satellite/terrain download work.
(
  while true; do
    sleep 900
    echo "[periodic] Pushing caches to GCS ($(date '+%H:%M'))..."
    gcloud storage rsync satellite_cache_avif_256/ gs://streetzim-cache/satellite_cache_avif_256/ --recursive 2>/dev/null || true
    gcloud storage rsync terrain_cache/ gs://streetzim-cache/terrain_cache/ --recursive 2>/dev/null || true
  done
) &
PERIODIC_PUSH_PID=$!

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

echo "=== Build complete, ZIM size: $(du -h $ZIM_FILE | cut -f1) ==="

# Push caches now so they're safely in GCS before the Archive.org upload
# (which takes a while). The EXIT trap above will push again at the end
# to catch anything produced after this point, but double-rsync is cheap.
push_caches

# ----------------------------------------------------------------------------
# Upload finished ZIM to Archive.org
# ----------------------------------------------------------------------------
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

# Also save a copy in GCS
gcloud storage cp "$ZIM_FILE" "gs://streetzim-cache/output/$ZIM_FILE"

echo "=== All done: $(date) ==="

# ----------------------------------------------------------------------------
# Self-delete the VM
# ----------------------------------------------------------------------------
echo "=== Self-deleting VM $INSTANCE_NAME in $ZONE ==="
gcloud --quiet compute instances delete "$INSTANCE_NAME" --zone="$ZONE"
