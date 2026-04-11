#!/bin/bash
# Wait for California + Texas builds to finish, then build West Coast and East Coast.
set -e
cd /Users/jasontitus/experiments/streetzim
source venv312/bin/activate
export ZSTD_CLEVEL=22

echo "Waiting for California + Texas to finish..."
while pgrep -f "create_osm_zim.*California\|create_osm_zim.*Texas" > /dev/null 2>&1; do
  sleep 60
done
echo "California + Texas done. Starting coasts..."

echo "=== West Coast US ==="
python3 create_osm_zim.py \
  --mbtiles us-tiles.mbtiles \
  --pbf world-data/planet-2026-03-10.osm.pbf \
  --bbox="-124.8,32.5,-114.1,49.0" \
  --name "West Coast US" \
  --satellite --terrain --wikidata \
  --search-cache search_cache/world.jsonl \
  --keep-temp \
  2>&1 | tee west-coast-us-build.log

ia upload streetzim-west-coast-us osm-west-coast-us.zim \
  --metadata="title:StreetZim - Offline Map of the U.S. West Coast (Washington, Oregon, California)" \
  --metadata="description:Offline map of the U.S. West Coast: Washington, Oregon, and California. Seattle, Portland, San Francisco, Los Angeles, San Diego, and everything in between. Includes vector maps, satellite imagery, 3D terrain, Wikipedia info, and full-text search. Open in Kiwix (free) — no internet needed. Built with StreetZim: https://github.com/jasontitus/streetzim" \
  --metadata="creator:StreetZim (create_osm_zim.py)" \
  --metadata="date:$(date +%Y-%m-%d)" \
  --metadata="subject:openstreetmap;offline maps;zim;kiwix;west coast;washington;oregon;california;seattle;portland;san francisco;los angeles" \
  --metadata="mediatype:data" \
  --metadata="licenseurl:https://github.com/jasontitus/streetzim/blob/main/LICENSE" \
  --metadata="source:https://github.com/jasontitus/streetzim" \
  --metadata="collection:opensource_media" \
  --retries 5

python3 web/generate.py --deploy
echo "=== West Coast done ==="

echo "=== East Coast US ==="
python3 create_osm_zim.py \
  --mbtiles us-tiles.mbtiles \
  --pbf world-data/planet-2026-03-10.osm.pbf \
  --bbox="-81.7,24.5,-66.9,47.5" \
  --name "East Coast US" \
  --satellite --terrain --wikidata \
  --search-cache search_cache/world.jsonl \
  --keep-temp \
  2>&1 | tee east-coast-us-build.log

ia upload streetzim-east-coast-us osm-east-coast-us.zim \
  --metadata="title:StreetZim - Offline Map of the U.S. East Coast (Maine to Florida)" \
  --metadata="description:Offline map of the U.S. East Coast from Maine to Florida. New York, Boston, Philadelphia, Washington D.C., Atlanta, Miami, Charlotte, Baltimore, and the entire Eastern Seaboard. Includes vector maps, satellite imagery, 3D terrain, Wikipedia info, and full-text search. Open in Kiwix (free) — no internet needed. Built with StreetZim: https://github.com/jasontitus/streetzim" \
  --metadata="creator:StreetZim (create_osm_zim.py)" \
  --metadata="date:$(date +%Y-%m-%d)" \
  --metadata="subject:openstreetmap;offline maps;zim;kiwix;east coast;new york;boston;philadelphia;washington dc;miami;atlanta" \
  --metadata="mediatype:data" \
  --metadata="licenseurl:https://github.com/jasontitus/streetzim/blob/main/LICENSE" \
  --metadata="source:https://github.com/jasontitus/streetzim" \
  --metadata="collection:opensource_media" \
  --retries 5

python3 web/generate.py --deploy
echo "=== East Coast done ==="
