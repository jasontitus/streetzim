#!/bin/bash
# Wait for Europe v4 to finish, then build Australia & New Zealand.
set -e
cd /Users/jasontitus/experiments/streetzim
source venv312/bin/activate
export ZSTD_CLEVEL=22

echo "Waiting for Europe v4 to finish..."
while pgrep -f "create_osm_zim.*Europe" > /dev/null 2>&1; do
  sleep 60
done
echo "Europe done. Starting Australia & NZ..."

python3 create_osm_zim.py \
  --mbtiles world-data/world-tiles-v2.mbtiles \
  --pbf world-data/planet-2026-03-10.osm.pbf \
  --bbox="110.0,-50.0,180.0,-8.0" \
  --name "Australia & New Zealand" \
  --satellite --terrain --wikidata \
  --search-cache search_cache/world.jsonl \
  --keep-temp \
  2>&1 | tee australia-nz-build.log

ia upload streetzim-australia-nz osm-australia-nz.zim \
  --metadata="title:StreetZim - Offline Map of Australia & New Zealand (Sydney, Melbourne, Auckland)" \
  --metadata="description:Offline map of Australia and New Zealand. Major cities include Sydney, Melbourne, Brisbane, Perth, Adelaide, Auckland, Wellington, and Christchurch. Features the Great Barrier Reef, Uluru, the Outback, Tasmania, the Southern Alps, and Milford Sound. Includes vector maps, satellite imagery, 3D terrain, Wikipedia info, and full-text search. Open in Kiwix (free) — no internet needed. Built with StreetZim: https://github.com/jasontitus/streetzim" \
  --metadata="creator:StreetZim (create_osm_zim.py)" \
  --metadata="date:$(date +%Y-%m-%d)" \
  --metadata="subject:openstreetmap;offline maps;zim;kiwix;australia;new zealand;sydney;melbourne;auckland" \
  --metadata="mediatype:data" \
  --metadata="licenseurl:https://github.com/jasontitus/streetzim/blob/main/LICENSE" \
  --metadata="source:https://github.com/jasontitus/streetzim" \
  --metadata="collection:opensource_media" \
  --retries 5

python3 web/generate.py --deploy
echo "=== Australia & NZ done ==="
