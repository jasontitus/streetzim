#!/bin/bash
# Build remaining ZIMs sequentially and upload each to Archive.org as it finishes.
# Order: US (rebuild) -> Indian Subcontinent -> California -> Colorado -> Iran -> Hispaniola
set -e
cd /Users/jasontitus/experiments/streetzim
source venv312/bin/activate
export ZSTD_CLEVEL=22

PY="python3 /Users/jasontitus/experiments/streetzim/create_osm_zim.py"
WORLD_TILES="world-data/world-tiles-v2.mbtiles"
US_TILES="us-tiles.mbtiles"
PBF="world-data/planet-2026-03-10.osm.pbf"
SEARCH="search_cache/world.jsonl"
COMMON_BUILD="--pbf $PBF --satellite --terrain --wikidata --search-cache $SEARCH --keep-temp"
DATE_TAG=$(date +%Y-%m-%d)

# Common boilerplate appended to every description
COMMON_DESC_FOOTER="

This is a complete offline map viewer packaged as a ZIM file for the free Kiwix reader app (iOS, Android, Mac, Windows, Linux). No internet connection required — just open the file in Kiwix and browse.

What's included:
- Detailed street-level vector maps rendered client-side with MapLibre GL JS
- Sentinel-2 cloudless satellite imagery (10m/pixel resolution)
- 3D terrain with hillshade from Copernicus GLO-30 elevation data
- Wikipedia articles and Wikidata info for cities, landmarks, and points of interest
- Full-text search across place names, streets, parks, peaks, airports, and POIs

How to use:
1. Download the .zim file
2. Install Kiwix (free) from https://kiwix.org
3. Open the .zim file in Kiwix — that's it!

Data sources and licenses:
- Map data: OpenStreetMap contributors (ODbL)
- Tile schema: OpenMapTiles (CC-BY 4.0)
- Satellite imagery: Sentinel-2 cloudless by EOX (CC BY-NC-SA 4.0)
- Elevation: Copernicus GLO-30 DEM, ESA/DLR/Airbus under COPERNICUS programme
- Place info: Wikidata (CC0) / Wikipedia (CC BY-SA 3.0)

Built with StreetZim: https://github.com/jasontitus/streetzim"

# upload <item-id> <zim-file> <title> <intro-line> <subjects>
upload_zim() {
  local item_id="$1"
  local zim_file="$2"
  local title="$3"
  local intro="$4"
  local subjects="$5"

  if [ ! -f "$zim_file" ]; then
    echo "ERROR: $zim_file not found, skipping upload"
    return 1
  fi

  local desc="${intro}${COMMON_DESC_FOOTER}"
  echo ">>> Uploading $zim_file -> archive.org/details/$item_id"
  ia upload "$item_id" "$zim_file" \
    --metadata="title:${title}" \
    --metadata="description:${desc}" \
    --metadata="creator:StreetZim (create_osm_zim.py)" \
    --metadata="date:${DATE_TAG}" \
    --metadata="subject:${subjects}" \
    --metadata="mediatype:data" \
    --metadata="licenseurl:https://github.com/jasontitus/streetzim/blob/main/LICENSE" \
    --metadata="source:https://github.com/jasontitus/streetzim" \
    --metadata="collection:opensource_media" \
    --retries 5
  echo ">>> Done: https://archive.org/details/$item_id"

  # Refresh the public site so the newly-uploaded ZIM appears as live.
  echo ">>> Refreshing https://streetzim.web.app ..."
  python3 /Users/jasontitus/experiments/streetzim/web/generate.py --deploy || \
    echo ">>> Warning: site refresh failed (continuing)"
}

echo "=== Build & Upload Queue Started: $(date) ==="

# ========================================================================
# 1. United States (rebuild — terrain fix + tab title fix + attribution)
# ========================================================================
echo ""
echo "=== [1/6] United States ==="
$PY --mbtiles $US_TILES $COMMON_BUILD \
  --bbox="-125.0,24.4,-66.9,49.4" --name "United States" \
  2>&1 | tee us-build-v12.log
upload_zim "streetzim-united-states" "osm-united-states.zim" \
  "StreetZim - Offline Map of the United States (Continental US, all 48 states)" \
  "Offline map of the Continental United States, including all 48 contiguous states and the District of Columbia. Major cities include New York, Los Angeles, Chicago, Houston, Phoenix, Philadelphia, San Antonio, San Diego, Dallas, Austin, San Francisco, Seattle, Denver, Boston, Washington D.C., Miami, Atlanta, and many more." \
  "openstreetmap;offline maps;zim;kiwix;maplibre;vector tiles;satellite imagery;terrain;united states;usa;continental us;north america"

# ========================================================================
# 2. Indian Subcontinent
# ========================================================================
echo ""
echo "=== [2/6] Indian Subcontinent ==="
$PY --mbtiles $WORLD_TILES $COMMON_BUILD \
  --bbox="60.0,5.0,97.5,37.0" --name "Indian Subcontinent" \
  2>&1 | tee indian-subcontinent-build.log
upload_zim "streetzim-indian-subcontinent" "osm-indian-subcontinent.zim" \
  "StreetZim - Offline Map of the Indian Subcontinent (India, Pakistan, Bangladesh, Sri Lanka, Nepal, Bhutan)" \
  "Offline map of the Indian Subcontinent (South Asia), including India, Pakistan, Bangladesh, Sri Lanka, Nepal, Bhutan, and the Maldives. Major cities include New Delhi, Mumbai, Bangalore, Kolkata, Chennai, Hyderabad, Karachi, Lahore, Islamabad, Dhaka, Colombo, Kathmandu, and Thimphu." \
  "openstreetmap;offline maps;zim;kiwix;maplibre;vector tiles;satellite imagery;terrain;india;pakistan;bangladesh;sri lanka;nepal;bhutan;south asia;indian subcontinent"

# ========================================================================
# 3. California
# ========================================================================
echo ""
echo "=== [3/6] California ==="
$PY --mbtiles $US_TILES $COMMON_BUILD \
  --bbox="-124.48,32.53,-114.13,42.01" --name "California" \
  2>&1 | tee california-build-v6.log
upload_zim "streetzim-california" "osm-california.zim" \
  "StreetZim - Offline Map of California (Los Angeles, San Francisco, San Diego, Yosemite)" \
  "Offline map of California, USA — the most populous U.S. state. Coverage spans from the Oregon border in the north to the Mexican border in the south, and from the Pacific coast east to the Sierra Nevada and Death Valley. Major cities include Los Angeles, San Diego, San Francisco, San Jose, Sacramento, Oakland, Fresno, Long Beach, Bakersfield, and Anaheim. National parks include Yosemite, Sequoia, Kings Canyon, Joshua Tree, Death Valley, Redwood, Channel Islands, and Lassen Volcanic." \
  "openstreetmap;offline maps;zim;kiwix;maplibre;vector tiles;satellite imagery;terrain;california;usa;los angeles;san francisco;san diego;yosemite;sierra nevada"

# ========================================================================
# 4. Colorado
# ========================================================================
echo ""
echo "=== [4/6] Colorado ==="
$PY --mbtiles $US_TILES $COMMON_BUILD \
  --bbox="-109.06,36.99,-102.04,41.00" --name "Colorado" \
  2>&1 | tee colorado-build-v6.log
upload_zim "streetzim-colorado" "osm-colorado.zim" \
  "StreetZim - Offline Map of Colorado (Denver, Boulder, Aspen, Rocky Mountains)" \
  "Offline map of Colorado, USA — the Rocky Mountain state. Includes Denver, Colorado Springs, Aurora, Fort Collins, Boulder, Pueblo, Greeley, Grand Junction, Aspen, Vail, Breckenridge, and Telluride. Features the Rocky Mountains, the Continental Divide, and Rocky Mountain National Park, Mesa Verde, Great Sand Dunes, and Black Canyon of the Gunnison National Parks." \
  "openstreetmap;offline maps;zim;kiwix;maplibre;vector tiles;satellite imagery;terrain;colorado;usa;denver;rocky mountains;continental divide"

# ========================================================================
# 5. Iran
# ========================================================================
echo ""
echo "=== [5/6] Iran ==="
$PY --mbtiles $WORLD_TILES $COMMON_BUILD \
  --bbox="44.0,25.0,63.5,39.8" --name "Iran" \
  2>&1 | tee iran-build-v2.log
upload_zim "streetzim-iran" "osm-iran.zim" \
  "StreetZim - Offline Map of Iran (Tehran, Isfahan, Shiraz, Mashhad)" \
  "Offline map of Iran, including the Caspian Sea coast in the north, the Persian Gulf and Strait of Hormuz in the south, and the Zagros and Alborz mountain ranges. Major cities include Tehran, Mashhad, Isfahan, Karaj, Shiraz, Tabriz, Qom, Ahvaz, Kermanshah, and Urmia. Includes historical sites such as Persepolis, Pasargadae, and the historic centers of Yazd, Kashan, and Isfahan." \
  "openstreetmap;offline maps;zim;kiwix;maplibre;vector tiles;satellite imagery;terrain;iran;persia;tehran;isfahan;shiraz;persian gulf"

# ========================================================================
# 6. Hispaniola
# ========================================================================
echo ""
echo "=== [6/6] Hispaniola ==="
$PY --mbtiles $WORLD_TILES $COMMON_BUILD \
  --bbox="-74.5,17.5,-68.3,20.1" --name "Hispaniola" \
  2>&1 | tee hispaniola-build-v3.log
upload_zim "streetzim-hispaniola" "osm-hispaniola.zim" \
  "StreetZim - Offline Map of Hispaniola (Haiti and Dominican Republic)" \
  "Offline map of the Caribbean island of Hispaniola, shared by two countries: Haiti (in the west) and the Dominican Republic (in the east). Major cities include Santo Domingo, Santiago de los Caballeros, La Romana, San Pedro de Macorís, Port-au-Prince, Cap-Haïtien, Carrefour, and Delmas. Features the Cordillera Central mountains and Pico Duarte, the highest peak in the Caribbean." \
  "openstreetmap;offline maps;zim;kiwix;maplibre;vector tiles;satellite imagery;terrain;hispaniola;haiti;dominican republic;caribbean"

echo ""
echo "=== Build & Upload Queue Complete: $(date) ==="
