#!/usr/bin/env bash
# Download the shapefiles resources/tilemaker/config-openmaptiles.json reads:
#   coastline/water_polygons.shp            OSM water polygons (ocean), ~900 MB zip
#   landcover/ne_10m_urban_areas/…          Natural Earth 10m (public domain)
#   landcover/ne_10m_antarctic_ice_shelves_polys/…
#   landcover/ne_10m_glaciated_areas/…
#
# tilemaker opens them relative to the directory it runs in, so run this in
# the directory you build from (the production wrappers cd to the repo root):
#
#   scripts/fetch-shapefiles.sh            # into the current directory
#   scripts/fetch-shapefiles.sh /some/dir  # into /some/dir
#
# Existing files are kept; delete coastline/ or landcover/ to refresh.
# Without these, tilemaker only prints "Unable to open …" and the ZIM has
# no ocean (create_osm_zim.py warns about it).
set -euo pipefail

DEST="${1:-.}"
mkdir -p "$DEST"
cd "$DEST"

WATER_URL="${WATER_URL:-https://osmdata.openstreetmap.de/download/water-polygons-split-4326.zip}"
NE_BASE="${NE_BASE:-https://naciscdn.org/naturalearth/10m}"

fetch() {  # url out
  curl -fL --retry 4 --retry-delay 5 -o "$2.part" "$1"
  mv "$2.part" "$2"
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [ ! -s coastline/water_polygons.shp ]; then
  echo "coastline: $WATER_URL"
  fetch "$WATER_URL" "$tmp/water.zip"
  unzip -q "$tmp/water.zip" -d "$tmp/water"
  mkdir -p coastline
  mv "$tmp"/water/water-polygons-split-4326/water_polygons.* coastline/
else
  echo "coastline: present"
fi

for spec in cultural/ne_10m_urban_areas \
            physical/ne_10m_antarctic_ice_shelves_polys \
            physical/ne_10m_glaciated_areas; do
  name="${spec#*/}"
  if [ -s "landcover/$name/$name.shp" ]; then
    echo "landcover/$name: present"
    continue
  fi
  echo "landcover/$name: $NE_BASE/$spec.zip"
  fetch "$NE_BASE/$spec.zip" "$tmp/$name.zip"
  mkdir -p "landcover/$name"
  unzip -q -o "$tmp/$name.zip" -d "landcover/$name"
done

ls -1 coastline/water_polygons.shp landcover/*/*.shp
