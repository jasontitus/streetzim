#!/bin/bash
# Upload StreetZim caches to gs://streetzim-cache for use by ephemeral
# build VMs. Run from the streetzim project root.
#
# Uses `gcloud storage` (the modern replacement for gsutil) which doesn't
# have the macOS CLOSE_WAIT hang that gsutil suffers from.
#
# Strategy:
#   - Large files (PBF, MBTiles, JSONL): direct copy/rsync — fast
#   - DEM source GeoTIFFs: rsync — large enough to be fast
#   - Tile cache directories (millions of small WebP/AVIF files):
#       tar + upload as a single object (per-file HTTP overhead would
#       otherwise dominate)
#
# Re-running this script is safe — `gcloud storage rsync` only copies changes.
set -e
cd "$(dirname "$0")/.."  # streetzim project root

BUCKET=gs://streetzim-cache
PROJECT=streetzim

echo "=== StreetZim cache upload to $BUCKET ==="
echo ""

# ----------------------------------------------------------------------------
# 1. Large single files (fast, no per-file overhead)
# ----------------------------------------------------------------------------
echo ">>> [1/6] Large files: world-data/*.pbf, world-tiles-v2.mbtiles"
# Exclude old world-tiles.mbtiles (v1) and idx/tmp dirs.
gcloud storage rsync world-data/ "$BUCKET/world-data/" \
  --recursive \
  --exclude='.*\.idx.*|.*\.tmp.*|.*world-tiles\.mbtiles$|\.DS_Store' \
  --project="$PROJECT"

echo ""
echo ">>> [2/6] us-tiles.mbtiles"
gcloud storage cp us-tiles.mbtiles "$BUCKET/us-tiles.mbtiles" \
  --project="$PROJECT" --no-clobber

echo ""
echo ">>> [3/6] search_cache/world.jsonl (16 GB)"
gcloud storage cp search_cache/world.jsonl "$BUCKET/search_cache/world.jsonl" \
  --project="$PROJECT" --no-clobber

echo ""
echo ">>> [4/6] wikidata_cache/ (~1.3 GB, ~93 small files)"
gcloud storage rsync wikidata_cache/ "$BUCKET/wikidata_cache/" \
  --recursive --project="$PROJECT"

# ----------------------------------------------------------------------------
# 2. DEM source GeoTIFFs (~547 GB across 26K files)
# ----------------------------------------------------------------------------
echo ""
echo ">>> [5/6] terrain_cache/dem_sources/ (DEM source GeoTIFFs)"
gcloud storage rsync terrain_cache/dem_sources/ "$BUCKET/terrain_cache/dem_sources/" \
  --recursive --project="$PROJECT"

# ----------------------------------------------------------------------------
# 3. Tile cache directories — tar to avoid per-file HTTP overhead
# ----------------------------------------------------------------------------
echo ""
echo ">>> [6/6] Tarball uploads (avoids per-file overhead on millions of small files)"

upload_tarball() {
  local src_dir="$1"
  local remote_name="$2"
  shift 2
  local tar_args=("$@")
  if [ ! -d "$src_dir" ]; then
    echo "    SKIP: $src_dir does not exist"
    return
  fi
  echo "    Tarring $src_dir -> $remote_name (streaming directly to GCS)..."
  tar -cf - "${tar_args[@]}" -C "$(dirname "$src_dir")" "$(basename "$src_dir")" \
    | gcloud storage cp - "$BUCKET/tarballs/$remote_name" --project="$PROJECT"
  echo "    Done: $BUCKET/tarballs/$remote_name"
}

# Satellite cache: ~35 GB AVIF tiles
upload_tarball satellite_cache_avif_256 satellite_cache_avif_256.tar

# Terrain generated tiles: exclude dem_sources/ (already uploaded above as raw files)
upload_tarball terrain_cache terrain_cache_tiles.tar --exclude='terrain_cache/dem_sources'

echo ""
echo "=== Upload complete ==="
gcloud storage du -s "$BUCKET/" --project="$PROJECT" 2>&1 | tail -1
