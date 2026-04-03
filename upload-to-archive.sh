#!/bin/bash
# Upload StreetZim ZIM files to Archive.org
# Run: source venv312/bin/activate && bash upload-to-archive.sh
#
# Prerequisites:
#   pip install internetarchive
#   ia configure  (one-time login — run interactively first)
#
# Each ZIM becomes an Archive.org "item" with automatic torrent generation.
# After upload, torrents are available at:
#   https://archive.org/download/streetzim-<region>/streetzim-<region>_archive.torrent

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATE=$(date +%Y-%m-%d)
VERSION="2026-04"
CREATOR="StreetZim (create_osm_zim.py)"
LICENSE_URL="https://github.com/jasontitus/streetzim/blob/main/LICENSE"
PROJECT_URL="https://github.com/jasontitus/streetzim"

COMMON_DESC="Fully offline OpenStreetMap viewer packaged as a ZIM file for the Kiwix reader (iOS, Android, desktop). \
Opens instantly with no internet connection required.

Features:
- Vector map tiles rendered client-side with MapLibre GL JS (smooth zoom, rotation, tilt)
- Sentinel-2 cloudless satellite imagery (10m/pixel)
- Copernicus GLO-30 terrain with hillshade and 3D elevation
- Wikidata place info with Wikipedia article extracts
- Full-text search across all place names, streets, parks, peaks, airports, and points of interest
- Works in Kiwix on iPhone, iPad, Android, Mac, Windows, and Linux

Data sources and licenses:
- Map data: OpenStreetMap contributors (ODbL)
- Tile schema: OpenMapTiles (CC-BY 4.0)
- Satellite imagery: Sentinel-2 cloudless by EOX (CC BY-NC-SA 4.0)
- Elevation: Copernicus GLO-30 DEM, provided by ESA/DLR/Airbus under COPERNICUS programme
- Place info: Wikidata (CC0) / Wikipedia (CC BY-SA 3.0)

Source code: https://github.com/jasontitus/streetzim"

# Each entry: zim_file | display_name | region_description (with country list)
declare -A ZIMS
ZIMS=(
  ["united-states"]="osm-united-states.zim|United States|Continental United States including all 48 contiguous states and Washington, D.C."
  ["midwest-us"]="osm-midwest-us.zim|Midwest US|U.S. Midwest: Ohio, Indiana, Illinois, Michigan, Wisconsin, Minnesota, Iowa, Missouri, North Dakota, South Dakota, Nebraska, and Kansas."
  ["west-asia"]="osm-west-asia.zim|West Asia|West Asia and Northeast Africa: Turkey, Syria, Lebanon, Israel, Palestine, Jordan, Iraq, Iran, Kuwait, Saudi Arabia, Bahrain, Qatar, United Arab Emirates, Oman, Yemen, Egypt (Sinai), and parts of Afghanistan and Pakistan."
  ["europe"]="osm-europe.zim|Europe|Europe from Iceland to western Russia: United Kingdom, Ireland, France, Spain, Portugal, Germany, Italy, Netherlands, Belgium, Switzerland, Austria, Poland, Czech Republic, Slovakia, Hungary, Romania, Bulgaria, Greece, Sweden, Norway, Finland, Denmark, the Baltic states, Ukraine, Belarus, and western Russia."
  ["indian-subcontinent"]="osm-indian-subcontinent.zim|Indian Subcontinent|South Asia: India, Pakistan, Bangladesh, Sri Lanka, Nepal, Bhutan, and the Maldives."
  ["washington-dc"]="osm-washington-dc.zim|Washington, D.C.|Washington, D.C. — the U.S. capital and surrounding area."
  ["california"]="osm-california.zim|California|California, USA — from the Oregon border to Mexico, coast to the Sierra Nevada."
  ["colorado"]="osm-colorado.zim|Colorado|Colorado, USA — the Rocky Mountain state."
  ["iran"]="osm-iran.zim|Iran|Iran — from the Caspian Sea to the Persian Gulf, including major cities Tehran, Isfahan, Shiraz, Tabriz, and Mashhad."
  ["hispaniola"]="osm-hispaniola.zim|Hispaniola|The island of Hispaniola: Haiti and the Dominican Republic."
)

upload_zim() {
  local id="$1"
  local entry="${ZIMS[$id]}"
  local zim_file=$(echo "$entry" | cut -d'|' -f1)
  local name=$(echo "$entry" | cut -d'|' -f2)
  local region_desc=$(echo "$entry" | cut -d'|' -f3)
  local item_id="streetzim-${id}"
  local zim_path="${SCRIPT_DIR}/${zim_file}"

  if [ ! -f "$zim_path" ]; then
    echo "SKIP: $zim_file not found"
    return
  fi

  local size_bytes=$(stat -f%z "$zim_path")
  local size_gb=$(echo "scale=1; $size_bytes / 1073741824" | bc)
  local size_mb=$(( size_bytes / 1048576 ))
  local size_label="${size_gb} GB"
  if [ "$(echo "$size_gb < 1" | bc)" -eq 1 ]; then
    size_label="${size_mb} MB"
  fi

  local full_desc="${COMMON_DESC}

---
Region: ${region_desc}
File size: ${size_label}
Version: ${VERSION}
Format: ZIM (open in Kiwix — https://kiwix.org)"

  echo "Uploading: $zim_file ($size_label) -> archive.org/details/$item_id"

  ia upload "$item_id" "$zim_path" \
    --metadata="title:StreetZim - ${name} Offline Map (${VERSION})" \
    --metadata="description:${full_desc}" \
    --metadata="creator:${CREATOR}" \
    --metadata="date:${DATE}" \
    --metadata="subject:openstreetmap;offline maps;zim;kiwix;maplibre;vector tiles;satellite imagery;terrain;${name}" \
    --metadata="mediatype:data" \
    --metadata="licenseurl:${LICENSE_URL}" \
    --metadata="source:${PROJECT_URL}" \
    --metadata="collection:opensource_media" \
    --retries 5

  echo "Done: https://archive.org/details/$item_id"
  echo "Torrent: https://archive.org/download/$item_id/${item_id}_archive.torrent"
  echo
}

# Upload specific region or all
if [ "$1" = "all" ]; then
  for id in $(echo "${!ZIMS[@]}" | tr ' ' '\n' | sort); do
    upload_zim "$id"
  done
elif [ -n "$1" ]; then
  for region in "$@"; do
    if [ -z "${ZIMS[$region]}" ]; then
      echo "Unknown region: $region"
      echo "Available: $(echo "${!ZIMS[@]}" | tr ' ' '\n' | sort | tr '\n' ' ')"
      exit 1
    fi
    upload_zim "$region"
  done
else
  echo "Usage: bash upload-to-archive.sh <region> [region2 ...]"
  echo "       bash upload-to-archive.sh all"
  echo
  echo "Available regions:"
  for id in $(echo "${!ZIMS[@]}" | tr ' ' '\n' | sort); do
    entry="${ZIMS[$id]}"
    zim_file=$(echo "$entry" | cut -d'|' -f1)
    name=$(echo "$entry" | cut -d'|' -f2)
    exists="missing"
    if [ -f "${SCRIPT_DIR}/${zim_file}" ]; then
      size=$(( $(stat -f%z "${SCRIPT_DIR}/${zim_file}") / 1048576 ))
      exists="${size} MB"
    fi
    printf "  %-25s %-30s %s\n" "$id" "$name" "[$exists]"
  done
fi
