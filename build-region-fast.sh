#!/usr/bin/env bash
# Fast-path build wrapper: in-build spatial-cells + no LLM bundle +
# zim-builder=rust. Eliminates the post-build repackage_zim.py step.
# Mirrors build-region.sh's CLI: <id> <bbox> <name>.
#
# Per the streetzim f38cfb4 commit (2026-05-08): California 1h55m → 1h27m
# (-24%); Silicon Valley 31m → 27m (-12%). Europe-scale saves ~3-4h of
# repackage. Requires:
#   - rust/streetzim-pack/target/release/streetzim-pack
#   - (optional, for --xapian=builder) ../xapianbuilder/target/release/xapianbuilder

set -euo pipefail
cd /storage/streetzim
export TMPDIR=/storage/streetzim/tmp
export ZSTD_CLEVEL="${ZSTD_CLEVEL:-22}"
mkdir -p "$TMPDIR"

ID="$1"; BBOX="$2"; NAME="$3"
TODAY=$(date +%Y-%m-%d)
PY=/storage/streetzim/venv-linux/bin/python3
SCRIPT=/storage/streetzim/create_osm_zim.py

MBTILES=/storage/streetzim/world-data/regions/${ID}.mbtiles
PBF=/storage/streetzim/world-data/regions/${ID}.osm.pbf
SEARCH=/storage/streetzim/world-data/regions/${ID}.search.jsonl
WD=/storage/streetzim/wikidata_cache
TERRAIN=/storage/streetzim/terrain_cache
LOWZ=/storage/streetzim/terrain_cache/dem_sources/world_dem_32k.tif
OVERTURE_RELEASE="${OVERTURE_RELEASE:-2026-04-15.0}"
ADDR=/storage/streetzim/overture_cache/addresses-${ID}-${OVERTURE_RELEASE}.parquet
PLACES=/storage/streetzim/overture_cache/places-${ID}-${OVERTURE_RELEASE}.parquet
# Offline Wikipedia: every linkable POI gets its article (trimmed reader
# page) and, with WIKI_IMAGES, its pictures — from the local enwiki maxi
# ZIM, never the network. The size check guards against a truncated copy
# (sha256 bf0853bf… was verified 2026-05; only the byte count is cheap
# enough to check per build). WIKI_IMAGES=lead|all|none (default all:
# ~107 KB/article on California, +37% of the ZIM; lead ~12 KB, +4%).
WIKI_ZIM=/storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim
WIKI_ZIM_SIZE=123980647016
WIKI_IMAGES="${WIKI_IMAGES:-all}"
WIKI_TITLE_CACHE=/storage/streetzim/wiki_articles_cache/${ID}_qid_titles.json

OUT_FINAL=osm-${ID}-${TODAY}.zim
LOG=/storage/streetzim/${ID}-rebuild-${TODAY}.log

echo "=== build $ID FAST @ $(date -Iseconds) ==="
echo "  bbox: $BBOX  out: $OUT_FINAL  log: $LOG"
echo "  overture release: $OVERTURE_RELEASE (addr=$([ -f "$ADDR" ] && echo yes || echo MISSING) places=$([ -f "$PLACES" ] && echo yes || echo MISSING))"
echo

if [ -f "$OUT_FINAL" ]; then
    echo "ALREADY EXISTS: $OUT_FINAL — skipping" | tee -a "$LOG"
    exit 0
fi

# Pick xapian mode based on whether xapianbuilder is available.
# `--xapian=builder` requires `--zim-builder=rust` per the create_osm_zim
# constraint; without xapianbuilder, fall back to the default `libzim`
# auto-indexer (slower, but the in-build pipeline still skips the
# post-build repackage which is the bigger win).
XAPIAN_FLAG="--xapian=libzim"
for cand in /home/ot/experiments/xapianbuilder/target/release/xapianbuilder \
            /home/ot/experiments/xapianbuilder/target/debug/xapianbuilder; do
    if [ -x "$cand" ]; then
        XAPIAN_FLAG="--xapian=builder --xapianbuilder-bin=$cand"
        echo "  using xapianbuilder: $cand"
        break
    fi
done

ARGS=(
    --mbtiles "$MBTILES"
    --pbf "$PBF"
    --bbox="$BBOX"
    --name "$NAME"
    --satellite --satellite-download-zoom 12
    --terrain
    --wikidata --wikidata-cache "$WD"
    --terrain-dir "$TERRAIN"
    --routing
    --search-cache "$SEARCH"
    --split-hot-search-chunks-mb 10
    --split-find-chips
    --keep-temp
    --output "$OUT_FINAL"
    # Fast-path flags (eliminate post-build repackage):
    --zim-builder=rust
    --no-llm-bundle
    --spatial-chunk-scale 10
)
# shellcheck disable=SC2206
ARGS+=( $XAPIAN_FLAG )
[ -f "$LOWZ" ]   && ARGS+=( --low-zoom-world-vrt "$LOWZ" )
if [ "$WIKI_IMAGES" != "off" ] && [ -f "$WIKI_ZIM" ] && [ "$(stat -c%s "$WIKI_ZIM")" = "$WIKI_ZIM_SIZE" ]; then
    mkdir -p /storage/streetzim/wiki_articles_cache
    ARGS+=( --resolve-wikidata-titles --wikidata-title-cache "$WIKI_TITLE_CACHE"
            --bundle-wiki-articles --wiki-articles-source "$WIKI_ZIM"
            --wiki-images "$WIKI_IMAGES" --wiki-image-max-kb 128 )
    echo "  wikipedia: bundling articles + images=$WIKI_IMAGES from $(basename "$WIKI_ZIM")"
else
    echo "  WARNING: enwiki source missing or wrong size — building WITHOUT bundled Wikipedia" | tee -a "$LOG"
fi
[ -f "$ADDR" ]   && ARGS+=( --overture-addresses "$ADDR" )
[ -f "$PLACES" ] && ARGS+=( --overture-places "$PLACES" )

# URL liveness cache from the webcheck crawl
# (gs://streetzim-cache/url_validation_cache.json — see
# cloud/upload_url_cache.sh / cloud/validate_overture_urls.py).
# When present, the Overture-places merge drops add-new rows whose
# `ws` site is dead and scrubs dead `ws` from OSM-POI enrichments.
URL_CACHE=/storage/streetzim/url_validation_cache.json
if [ -f "$URL_CACHE" ]; then
    ARGS+=( --url-cache "$URL_CACHE" --url-cache-policy drop-record )
    echo "  url cache: $URL_CACHE ($(du -h "$URL_CACHE" | cut -f1)) policy=drop-record"
fi

"$PY" "$SCRIPT" "${ARGS[@]}" 2>&1 | tee "$LOG"

echo "=== validate @ $(date -Iseconds) ===" | tee -a "$LOG"
"$PY" /storage/streetzim/cloud/validate_zim.py "$OUT_FINAL" 2>&1 | tee -a "$LOG" || \
    echo "(validate non-zero — review)" | tee -a "$LOG"

echo "=== done $ID @ $(date -Iseconds), size: $(du -h "$OUT_FINAL" | cut -f1) ===" | tee -a "$LOG"
