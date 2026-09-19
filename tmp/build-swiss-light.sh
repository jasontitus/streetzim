#!/bin/bash
# Light Switzerland variants. Mirrors build-region-fast.sh's ARGS exactly,
# varying only what we are measuring:
#   switzerland-nosat      : no --satellite
#   switzerland-nosat-z13  : no --satellite, --max-zoom 13
# Everything else identical to the shipped 2.25 GB build so the size delta is
# attributable: same extracts, wiki-images=lead, same Overture release.
cd /storage/streetzim
set -uo pipefail
export TMPDIR=/storage/streetzim/tmp
export ZSTD_CLEVEL=22
PY=/storage/streetzim/venv-linux/bin/python3
WD=/storage/streetzim/wikidata_cache
TERRAIN=/storage/streetzim/terrain_cache
LOWZ=/storage/streetzim/terrain_cache/dem_sources/world_dem_32k.tif
WIKI_ZIM=/storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim
WIKI_TITLE_CACHE=/storage/streetzim/wikidata_title_cache.json
REL=2026-08-19.0
BBOX=5.4,45.7,11.2,48.2
TODAY=$(date +%Y-%m-%d)

build() {
  local ID="$1"; shift
  local OUT="osm-${ID}-${TODAY}.zim"
  if [ -s "$OUT" ]; then echo "== $ID: $OUT already exists, skipping"; return 0; fi
  echo "== $ID build start $(TZ=America/Los_Angeles date '+%H:%M') PDT  extra: $*"
  local ARGS=(
    --mbtiles "world-data/regions/${ID}.mbtiles"
    --pbf     "world-data/regions/${ID}.osm.pbf"
    --bbox="$BBOX"
    --name    "Switzerland and the Alps (light)"
    --terrain
    --wikidata --wikidata-cache "$WD"
    --terrain-dir "$TERRAIN"
    --routing
    --search-cache "world-data/regions/${ID}.search.jsonl"
    --split-hot-search-chunks-mb 10
    --split-find-chips
    --output "$OUT"
    --zim-builder=rust
    --no-llm-bundle
    --spatial-chunk-scale 10
  )
  # build-region-fast.sh:170-171 adds BOTH Overture flags, each guarded on the
  # file existing. The first pass of this script passed only --overture-addresses
  # and silently dropped --overture-places, which cost 748,654 Overture place
  # records (9,947,615 - 9,198,961 exactly): chips fell to 44% of the shipped
  # build, the fuel chip came out EMPTY, and the browser smoke failed on the Gas
  # chip. Nothing about --max-zoom was involved; A (z14) and B (z13) had
  # identical chip counts because both were missing the same data.
  _ADDR="overture_cache/addresses-${ID}-${REL}.parquet"
  _PLACES="overture_cache/places-${ID}-${REL}.parquet"
  [ -f "$_ADDR" ]   && ARGS+=( --overture-addresses "$_ADDR" ) \
                    || echo "   WARNING: missing $_ADDR"
  [ -f "$_PLACES" ] && ARGS+=( --overture-places "$_PLACES" ) \
                    || echo "   WARNING: missing $_PLACES"
  ARGS+=(
  )
  # build-region-fast.sh adds this whenever the binary exists; omitting it
  # silently falls back to --xapian=libzim, which builds the title/fulltext
  # indexes a different way and would contaminate the size comparison.
  for cand in /home/ot/experiments/xapianbuilder/target/release/xapianbuilder \
              /home/ot/experiments/xapianbuilder/target/debug/xapianbuilder; do
    if [ -x "$cand" ]; then
      ARGS+=( --xapian=builder --xapianbuilder-bin="$cand" )
      echo "   using xapianbuilder: $cand"
      break
    fi
  done
  [ -f "$LOWZ" ] && ARGS+=( --low-zoom-world-vrt "$LOWZ" )
  if [ -f "$WIKI_ZIM" ]; then
    ARGS+=( --resolve-wikidata-titles --wikidata-title-cache "$WIKI_TITLE_CACHE"
            --bundle-wiki-articles --wiki-articles-source "$WIKI_ZIM"
            --wiki-images lead --wiki-image-max-kb 128 )
  fi
  "$PY" create_osm_zim.py "${ARGS[@]}" "$@" > "${ID}-build.out" 2>&1
  local rc=$?
  echo "== $ID build done $(TZ=America/Los_Angeles date '+%H:%M') PDT rc=$rc"
  [ $rc -eq 0 ] && ls -la "$OUT" | awk '{printf "   OUTPUT %.3f GB  %s\n",$5/1073741824,$9}'
  return $rc
}

build switzerland-nosat
build switzerland-nosat-z13 --max-zoom 13
echo "== all light builds done $(TZ=America/Los_Angeles date '+%H:%M') PDT"
