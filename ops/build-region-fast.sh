#!/usr/bin/env bash
# ops split, stage 1: this file lives in ops/ and a symlink at its old path
# runs it; the paths below assume that path (and so do pgrep and the locks).
# Started directly (ops/..., or from inside ops/), it re-runs by the old path.
if [ ! -L "$0" ] && _ops_real="$(readlink -f "$0" 2>/dev/null)"; then
  case "$_ops_real" in
    */ops/*) _ops_old="${_ops_real%/ops/*}/${_ops_real##*/ops/}"
             if [ -L "$_ops_old" ]; then exec bash "$_ops_old" "$@"; fi ;;
  esac
fi
unset _ops_real _ops_old
# Fast-path build wrapper: in-build spatial-cells + no LLM bundle +
# the manifest ZIM writer. Eliminates the post-build repackage_zim.py step.
# Mirrors build-region.sh's CLI: <id> <bbox> <name>.
#
# Per the streetzim f38cfb4 commit (2026-05-08): California 1h55m → 1h27m
# (-24%); Silicon Valley 31m → 27m (-12%). Europe-scale saves ~3-4h of
# repackage. Uses:
#   - the Python manifest packer (--zim-builder=manifest), the packer these
#     builds have run since 17c82fc; ZIM_BUILDER=rust selects a built Rust
#     streetzim-pack instead, and the build fails if there is none
#   - (optional, for --xapian=builder) xapianbuilder; without it the build
#     uses libzim (--zim-builder=python --xapian=libzim)

set -euo pipefail
cd /storage/streetzim
# BUILD_SCRATCH (ops/cloud/parallel_rebuild.sh): a folder on a fast disk (the
# shared NVMe) for everything this build writes and throws away -- TMPDIR and
# create_zim's .<output>.building-* folder next to the output. The finished
# ZIM is copied to BUILD_FINAL_DIR (default /storage/streetzim) under a
# temporary name and renamed into place, so a half-copied file never carries
# the final name. Unset: everything stays on /storage as before.
# BUILD_FINAL_DIR / BUILD_LOG_DIR: where the ZIM and the build log end up
# (default /storage/streetzim; a dry run points them elsewhere).
BUILD_SCRATCH="${BUILD_SCRATCH:-}"
FINAL_DIR="${BUILD_FINAL_DIR:-/storage/streetzim}"
LOG_DIR="${BUILD_LOG_DIR:-/storage/streetzim}"
if [ -n "$BUILD_SCRATCH" ]; then
    mkdir -p "$BUILD_SCRATCH/tmp"
    export TMPDIR="$BUILD_SCRATCH/tmp"
else
    export TMPDIR=/storage/streetzim/tmp
fi
export ZSTD_CLEVEL="${ZSTD_CLEVEL:-22}"
mkdir -p "$TMPDIR"

ID="$1"; BBOX="$2"; NAME="$3"
TODAY=$(date +%Y-%m-%d)
PY=/storage/streetzim/venv-linux/bin/python3
SCRIPT=/storage/streetzim/create_osm_zim.py

# Derived variants (switzerland-light, africa-light, ...) reuse their
# PARENT's extracts and parquets and differ only in flags. See
# cloud/region-variants.tsv for why this table exists rather than a
# per-variant copy of this script.
VARIANTS=/storage/streetzim/cloud/region-variants.tsv
SRC_ID="$ID"
VAR_SAT=yes
VAR_MAXZOOM=""
if [ -f "$VARIANTS" ]; then
    _row=$(awk -F'\t' -v id="$ID" '$1==id && $1 !~ /^#/ {print; exit}' "$VARIANTS")
    if [ -n "$_row" ]; then
        SRC_ID=$(printf '%s' "$_row" | cut -f2)
        VAR_SAT=$(printf '%s' "$_row" | cut -f3)
        VAR_MAXZOOM=$(printf '%s' "$_row" | cut -f4)
        echo "  variant of $SRC_ID: satellite=$VAR_SAT max_zoom=${VAR_MAXZOOM:-default}"
    fi
fi

MBTILES=/storage/streetzim/world-data/regions/${SRC_ID}.mbtiles

PBF=/storage/streetzim/world-data/regions/${SRC_ID}.osm.pbf
# STREETZIM_SEARCH_CACHE: another search-feature cache, e.g. one extracted
# from the region's multilingual tiles (names in every tile language, so
# every place gets its search names in them: docs/search-records.md, ln).
SEARCH=${STREETZIM_SEARCH_CACHE:-/storage/streetzim/world-data/regions/${SRC_ID}.search.jsonl}
WD=/storage/streetzim/wikidata_cache
TERRAIN=/storage/streetzim/terrain_cache
LOWZ=/storage/streetzim/terrain_cache/dem_sources/world_dem_32k.tif
# Only reads the cache, so it needs the release of the cached parquets: no
# default (the old one is gone from the bucket, and a missing parquet
# silently drops --overture-places), and the callers resolve `latest` first.
if [ -z "${OVERTURE_RELEASE:-}" ]; then
    echo "FATAL: OVERTURE_RELEASE is not set — pass the release of the overture_cache/ parquets (e.g. OVERTURE_RELEASE=2026-08-19.0)" >&2
    exit 1
fi
if [ "$OVERTURE_RELEASE" = latest ]; then
    echo "FATAL: OVERTURE_RELEASE=latest — pass the resolved name (download_overture_data.py addresses places --print-release)" >&2
    exit 1
fi
ADDR=/storage/streetzim/overture_cache/addresses-${SRC_ID}-${OVERTURE_RELEASE}.parquet
PLACES=/storage/streetzim/overture_cache/places-${SRC_ID}-${OVERTURE_RELEASE}.parquet
# Offline Wikipedia: every linkable POI gets its article (trimmed reader
# page) and, with WIKI_IMAGES, its pictures — from the local enwiki maxi
# ZIM, never the network. The size check guards against a truncated copy
# (sha256 bf0853bf… was verified 2026-05; only the byte count is cheap
# enough to check per build). WIKI_IMAGES=lead|all|none (default all:
# ~107 KB/article on California, +37% of the ZIM; lead ~12 KB, +4%).
WIKI_ZIM=/storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim
WIKI_ZIM_SIZE=123980647016
# BUILD_WIKI_ZIM: a verified copy on a faster disk (parallel_rebuild.sh puts one
# on the NVMe: the article bundling reads it at random). Used only when its
# size matches; otherwise the /storage original, as before.
if [ -n "${BUILD_WIKI_ZIM:-}" ]; then
    if [ -f "$BUILD_WIKI_ZIM" ] && [ "$(stat -c%s "$BUILD_WIKI_ZIM")" = "$WIKI_ZIM_SIZE" ]; then
        WIKI_ZIM="$BUILD_WIKI_ZIM"
    else
        echo "  BUILD_WIKI_ZIM=$BUILD_WIKI_ZIM is missing or the wrong size; using $WIKI_ZIM"
    fi
fi
WIKI_IMAGES="${WIKI_IMAGES:-all}"
WIKI_TITLE_CACHE=/storage/streetzim/wiki_articles_cache/${SRC_ID}_qid_titles.json

OUT_FINAL=osm-${ID}-${TODAY}.zim
LOG=${LOG_DIR}/${ID}-rebuild-${TODAY}.log
# Where the builder writes: next to the final file, or in BUILD_SCRATCH.
OUT_PATH="$FINAL_DIR/$OUT_FINAL"
OUT_BUILD="${BUILD_SCRATCH:+$BUILD_SCRATCH/}$OUT_FINAL"
[ -n "$BUILD_SCRATCH" ] || OUT_BUILD="$OUT_PATH"

if [ "$SRC_ID" != "$ID" ]; then
    for _f in "$MBTILES" "$PBF" "$SEARCH" "$ADDR" "$PLACES"; do
        [ -s "$_f" ] || { echo "FATAL: variant $ID needs $SRC_ID input $_f, which is missing." >&2
                          echo "       Refusing to build: a missing parquet is dropped silently by the" >&2
                          echo "       guards below and costs the region its Overture places." >&2
                          exit 1; }
    done
fi

echo "=== build $ID FAST @ $(date -Iseconds) ==="
echo "  bbox: $BBOX  out: $OUT_PATH  log: $LOG"
[ -z "$BUILD_SCRATCH" ] || echo "  scratch: $BUILD_SCRATCH (TMPDIR and the builder's output; copied to $FINAL_DIR when done)"
echo "  overture release: $OVERTURE_RELEASE (addr=$([ -f "$ADDR" ] && echo yes || echo MISSING) places=$([ -f "$PLACES" ] && echo yes || echo MISSING))"
echo

if [ -f "$OUT_PATH" ]; then
    echo "ALREADY EXISTS: $OUT_PATH — skipping" | tee -a "$LOG"
    exit 0
fi
if [ -n "$BUILD_SCRATCH" ] && [ -e "$OUT_BUILD" ]; then
    echo "FATAL: $OUT_BUILD is already in the scratch folder (a dead build's?); refusing to build over it" >&2
    exit 1
fi

# Cleanup, installed before the first step that needs it (the multilingual
# tile step, below; the NVMe copy after it).
STAGED_MBTILES=""
ML_PID=""
ML_OUT=""
COPY_TMP=""
cleanup_staged() {
    if [ -n "$STAGED_MBTILES" ]; then rm -f "$STAGED_MBTILES" 2>/dev/null || true; fi
    STAGED_MBTILES=""
    if [ -n "$COPY_TMP" ]; then rm -f "$COPY_TMP" 2>/dev/null || true; fi
    COPY_TMP=""
}
# A signal reaches the multilingual tile step too (it runs as a child we
# wait for, so the trap runs at once): it removes its container and store
# before we go. A trap's `wait` returns when the child has.
stop_ml() {
    if [ -n "$ML_PID" ]; then
        kill "-$1" "$ML_PID" 2>/dev/null || true
        wait "$ML_PID" 2>/dev/null || true
        ML_PID=""
    fi
    if [ -n "$ML_OUT" ]; then rm -f "$ML_OUT"; fi
}
# EXIT does the cleanup. The signal traps must also END the script: a trap that
# merely returns lets bash resume, so a killed build ran on to validate and
# "done", and a kill during the copy fell back to a full build from /storage.
trap cleanup_staged EXIT
trap 'stop_ml INT;  cleanup_staged; trap - INT;  kill -INT  $$' INT
trap 'stop_ml TERM; cleanup_staged; trap - TERM; kill -TERM $$' TERM
trap 'stop_ml HUP;  cleanup_staged; trap - HUP;  kill -HUP  $$' HUP

# Language-ready builds (docs/language-ready.md), opt in with
# STREETZIM_ML_TILES=1: tiles carrying name:xx for the matrix languages, cut
# from the region's own extract by ops/region-ml-tiles.py (tilemaker from
# main's CI image with this checkout's tile profile; reused while its bbox,
# languages, profile and extract are unchanged) instead of the world tiles,
# and the region's own languages from cloud/region-languages.tsv. Either
# failing stops the build: there is no fallback to the world tiles. Unset
# or 0: nothing changes.
ML_ARGS=()
case "${STREETZIM_ML_TILES:-0}" in
    0) ;;
    1)
        REGION_LANGS=$(awk -F'\t' -v id="$ID" '$1==id {print $2; f=1; exit} END {exit !f}' \
                       /storage/streetzim/cloud/region-languages.tsv) \
            || { echo "FATAL: no row for $ID in cloud/region-languages.tsv" >&2; exit 1; }
        # In the background and waited for, so a signal's trap runs at once
        # and passes the signal on (stop_ml).
        ML_OUT=$(mktemp "$TMPDIR/ml-tiles-$ID.XXXXXX")
        "$PY" /storage/streetzim/ops/region-ml-tiles.py --src-id "$SRC_ID" -- "$ID" "$BBOX" > "$ML_OUT" &
        ML_PID=$!
        ML_RC=0
        wait "$ML_PID" || ML_RC=$?
        ML_PID=""
        ML_MBTILES=$(tail -n 1 "$ML_OUT"); rm -f "$ML_OUT"; ML_OUT=""
        if [ "$ML_RC" != 0 ]; then
            echo "FATAL: multilingual tiles for $ID failed (STREETZIM_ML_TILES=1, rc=$ML_RC); not building from the world tiles" >&2
            exit 1
        fi
        if [ ! -s "$ML_MBTILES" ] || [ ! -s "$ML_MBTILES.langs" ]; then
            echo "FATAL: ops/region-ml-tiles.py gave '$ML_MBTILES', not tiles with a .langs sidecar" >&2
            exit 1
        fi
        MBTILES="$ML_MBTILES"
        ML_ARGS=( --tile-languages "$(cat "$ML_MBTILES.langs")" --region-languages "$REGION_LANGS" )
        echo "  multilingual tiles: $MBTILES ($(cat "$MBTILES.langs")); region languages: $REGION_LANGS"
        ;;
    *) echo "FATAL: STREETZIM_ML_TILES must be 0 or 1, not '$STREETZIM_ML_TILES'" >&2; exit 1 ;;
esac

# Stage the MBTiles on NVMe for the tile phase.
#
# Tile reading is random small-record access into a multi-GB SQLite file. On
# /storage (spinning) an early central-asia reading showed 1,660 IOPS for
# 8 MB/s at 61% util while both NVMes idled. Honest caveat from review: whole-
# build tile rates staged (brazil 8,262/s, indian-subcontinent 8,510/s) and
# unstaged (mexico 9,259/s, west-asia 7,900/s) are similar, so this is harmless
# rather than proven faster; the copy itself costs ~20 s per 4 GB.
#
# Rules, because /mnt/data is shared with another tenant's st-bridge-models:
# - Stage only AFTER the "already exists" skip, so a skipped region copies nothing.
# - One stager at a time (flock), and free space is checked under that lock, so
#   two builds cannot both pass the check and breach NVME_RESERVE_GB together.
# - Sweep copies left by dead builds (SIGKILL/reboot cannot run a trap).
# - Remove our copy on every exit path, and let signals actually kill us.
# /mnt/data itself is root-owned (shared host); /mnt/data/tilemaker is ours.
NVME_SCRATCH="${NVME_SCRATCH:-/mnt/data/tilemaker/streetzim-scratch}"
NVME_RESERVE_GB="${NVME_RESERVE_GB:-120}"
# The multilingual tile step (ops/region-ml-tiles.py) keeps its tilemaker
# store here when there is room, for hours, and reserves what it may still
# grow into: one .reserve-ml-<pid> file each, "<bytes> <start time>". Print
# the bytes reserved by live runs (the pid started at the recorded time:
# /proc/<pid>/stat field 22, so a reused pid does not count), and by dead
# ones whose store is still being written (a SIGKILLed client's container
# goes on). A stager counts them as used and deletes nothing: only the tile
# step's own reap, which removes the containers first, removes stores.
nvme_ml_reserved() {
    local f pid b st cur sum=0
    for f in "$NVME_SCRATCH"/.reserve-ml-*; do
        [ -e "$f" ] || continue
        pid=${f##*-}
        [ -n "$pid" ] && [ "$pid" -eq "$pid" ] 2>/dev/null || continue
        b=""; st=""
        read -r b st < "$f" || true
        case "$b" in ''|*[!0-9]*) continue ;; esac
        # More than 15 digits (999 TB) would overflow bash's arithmetic:
        # count it as more than any disk, so nothing is staged beside it.
        [ "${#b}" -le 15 ] || b=1000000000000000000
        cur=$(sed 's/.*) //' "/proc/$pid/stat" 2>/dev/null | cut -d' ' -f20) || cur=""
        if { [ -n "$cur" ] && [ "$cur" = "$st" ]; } \
           || [ -n "$(find "$NVME_SCRATCH" -maxdepth 3 -path "*/ml-store-*-$pid/*" -type f -mmin -10 -print -quit 2>/dev/null)" ]; then
            sum=$(( sum + 10#$b ))   # 10#: "09" is not octal
            [ "$sum" -le 1000000000000000000 ] || sum=1000000000000000000
        fi
    done
    echo "$sum"
}
if [ "${STAGE_MBTILES_NVME:-1}" = 1 ] && [ -f "$MBTILES" ] && mkdir -p "$NVME_SCRATCH" 2>/dev/null \
   && exec 9>"$NVME_SCRATCH/.stage.lock" && flock -w 900 9; then
    for _old in "$NVME_SCRATCH"/*.mbtiles; do
        [ -e "$_old" ] || continue
        _pid=${_old%.mbtiles}; _pid=${_pid##*.}
        # /proc, not kill -0: kill -0 fails on another user's live PID and would
        # make us delete a copy that is still in use.
        if [ -n "$_pid" ] && [ "$_pid" -eq "$_pid" ] 2>/dev/null && [ ! -d "/proc/$_pid" ]; then
            echo "  removing stale NVMe copy from dead build: $(basename "$_old")"
            rm -f "$_old" || true
        fi
    done
    _resv_gb=$(( ($(nvme_ml_reserved) + 1073741823) / 1073741824 ))
    _need_gb=$(( $(stat -c%s "$MBTILES") / 1073741824 + 1 ))
    _free_gb=$(df -BG --output=avail "$NVME_SCRATCH" 2>/dev/null | tail -1 | tr -dc '0-9')
    [ "$_resv_gb" = 0 ] || echo "  NVMe: ${_resv_gb}G reserved by tile stores, counted as used"
    if [ -n "$_free_gb" ] && [ $(( _free_gb - _resv_gb - _need_gb )) -ge "$NVME_RESERVE_GB" ]; then
        _dst="$NVME_SCRATCH/${ID}.$$.mbtiles"
        echo "  staging ${ID}.mbtiles (${_need_gb}G) on NVMe — ${_free_gb}G free, keeping ${NVME_RESERVE_GB}G reserve"
        # Record the path BEFORE copying, so a signal mid-copy removes the partial.
        STAGED_MBTILES="$_dst"
        if cp "$MBTILES" "$_dst" 2>/dev/null; then
            MBTILES="$_dst"
        else
            echo "  NVMe staging failed — using /storage"
            cleanup_staged
        fi
    else
        echo "  not staging on NVMe: need ${_need_gb}G + ${NVME_RESERVE_GB}G reserve, have ${_free_gb:-?}G"
    fi
    flock -u 9 || true
    exec 9>&-
fi


# Pick the writer and xapian mode by whether xapianbuilder is available.
# `--xapian=builder` needs a manifest writer (`--zim-builder=manifest`, or
# `rust` for a built Rust packer: ZIM_BUILDER=rust), which cannot run libzim's
# indexer. Without xapianbuilder, fall back to libzim with its auto-indexer
# (`--zim-builder=python --xapian=libzim`): slower, but the ZIM keeps Kiwix
# native search, and the in-build pipeline still skips the post-build
# repackage, which is the bigger win. (`--xapian=libzim` with a manifest
# writer is rejected by create_osm_zim.py.)
ZIM_BUILDER="${ZIM_BUILDER:-manifest}"
case "$ZIM_BUILDER" in
    manifest|rust) ;;
    *) echo "FATAL: ZIM_BUILDER must be manifest or rust, not '$ZIM_BUILDER'" >&2; exit 1 ;;
esac
WRITER_FLAGS="--zim-builder=python --xapian=libzim"
for cand in /home/ot/experiments/xapianbuilder/target/release/xapianbuilder \
            /home/ot/experiments/xapianbuilder/target/debug/xapianbuilder; do
    if [ -x "$cand" ]; then
        WRITER_FLAGS="--zim-builder=$ZIM_BUILDER --xapian=builder --xapianbuilder-bin=$cand"
        echo "  using xapianbuilder: $cand (--zim-builder=$ZIM_BUILDER)"
        break
    fi
done
case "$WRITER_FLAGS" in
    *--xapian=libzim*) echo "  no xapianbuilder: building with libzim (--zim-builder=python --xapian=libzim)" ;;
esac

SAT_ARGS=(--satellite --satellite-download-zoom 12)
[ "$VAR_SAT" = no ] && SAT_ARGS=()
ZOOM_ARGS=()
[ -n "$VAR_MAXZOOM" ] && ZOOM_ARGS=(--max-zoom "$VAR_MAXZOOM")

ARGS=(
    "${SAT_ARGS[@]}"
    "${ZOOM_ARGS[@]}"
    --mbtiles "$MBTILES"
    --pbf "$PBF"
    --bbox="$BBOX"
    --name "$NAME"
    --terrain
    --wikidata --wikidata-cache "$WD"
    --terrain-dir "$TERRAIN"
    --routing
    --search-cache "$SEARCH"
    --split-hot-search-chunks-mb 10
    --split-find-chips
    --keep-temp
    --output "$OUT_BUILD"
    # Fast-path flags (eliminate post-build repackage). The writer and
    # --xapian come from WRITER_FLAGS above.
    --no-llm-bundle
    --spatial-chunk-scale 10
)
# shellcheck disable=SC2206
ARGS+=( $WRITER_FLAGS )
ARGS+=( "${ML_ARGS[@]}" )
# A region's extra builder flags (cloud/region-build-flags.tsv: id, then the flags), a data file so
# one region gets a flag without an edit here: Bahrain's --ocr, a test map for the sign reader.
BUILD_FLAGS_TSV=${STREETZIM_BUILD_FLAGS_TSV:-/storage/streetzim/cloud/region-build-flags.tsv}
EXTRA_FLAGS=$(awk -F'\t' -v id="$ID" '$1==id && $1 !~ /^#/ {print $2; exit}' "$BUILD_FLAGS_TSV" 2>/dev/null)
# shellcheck disable=SC2206
[ -z "$EXTRA_FLAGS" ] || { ARGS+=( $EXTRA_FLAGS ); echo "  extra builder flags for $ID: $EXTRA_FLAGS"; }
# Terrain is always on here: without the world DEM the builder would
# switch to its fresh-machine layout (streetzim/terrain.py). Refuse instead.
if [ -f "$LOWZ" ]; then
    ARGS+=( --low-zoom-world-vrt "$LOWZ" )
else
    echo "FATAL: world DEM $LOWZ is missing" >&2
    exit 1
fi
if [ "$WIKI_IMAGES" = "off" ]; then
    echo "  wikipedia: disabled (WIKI_IMAGES=off)"
elif [ -f "$WIKI_ZIM" ] && [ "$(stat -c%s "$WIKI_ZIM")" = "$WIKI_ZIM_SIZE" ]; then
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

# Border clip (ops/region-clip.sh): a region cloud/region-outlines.tsv lists
# is cut to its outline widened by 10 km instead of its box, with zoom 0-10
# kept over the whole box as greyed context. CLIP=0 builds by the box.
. /storage/streetzim/ops/region-clip.sh
region_clip_args "$ID" "$SRC_ID" "$BBOX" || exit 1
ARGS+=( "${CLIP_ARGS[@]}" )

# streetzim-pack compresses clusters with one zstd context per rayon
# thread. At ZSTD_CLEVEL=22 (windowLog 27) each context is hundreds of MB,
# so on this 36-core host a large region's pack peaked around 40 GB and
# Brazil's was OOM-killed after six hours of building (2026-09-07) — the
# host has no /storage/swapfile active and we cannot enable it.
#
# Capping rayon's threads cuts peak memory nearly linearly and leaves the
# OUTPUT IDENTICAL (same level, same clusters) — it only costs pack
# wall-time. Scale the cap by the region's PBF, the best proxy we have
# for manifest size. PACK_THREADS overrides.
_pbf_gb=$(( $(stat -c%s "$PBF" 2>/dev/null || echo 0) / 1000000000 ))
if [ -n "${PACK_THREADS:-}" ]; then
    export RAYON_NUM_THREADS="$PACK_THREADS"
elif [ "$_pbf_gb" -ge 4 ]; then
    export RAYON_NUM_THREADS=6
elif [ "$_pbf_gb" -ge 2 ]; then
    export RAYON_NUM_THREADS=10
fi
[ -n "${RAYON_NUM_THREADS:-}" ] && \
    echo "  pack: ${_pbf_gb} GB PBF -> RAYON_NUM_THREADS=$RAYON_NUM_THREADS (zstd $ZSTD_CLEVEL, output unchanged)"

# -a: a same-day retry appends to the earlier attempt's log instead of erasing it.
"$PY" "$SCRIPT" "${ARGS[@]}" 2>&1 | tee -a "$LOG"

if [ "$OUT_BUILD" != "$OUT_PATH" ]; then
    # Copy under a temporary name on the final filesystem, check the size,
    # then rename: a kill mid-copy leaves only the temporary (removed by the
    # EXIT trap), never a truncated file with the final name.
    [ -s "$OUT_BUILD" ] || { echo "FATAL: the builder left no $OUT_BUILD" | tee -a "$LOG" >&2; exit 1; }
    [ ! -e "$OUT_PATH" ] || { echo "FATAL: $OUT_PATH appeared during the build; not overwriting it" | tee -a "$LOG" >&2; exit 1; }
    COPY_TMP="$FINAL_DIR/.$OUT_FINAL.copy-$$"
    echo "=== copy $OUT_BUILD -> $OUT_PATH @ $(date -Iseconds) ===" | tee -a "$LOG"
    cp "$OUT_BUILD" "$COPY_TMP"
    [ "$(stat -c%s "$COPY_TMP")" = "$(stat -c%s "$OUT_BUILD")" ] \
        || { echo "FATAL: the copy of $OUT_BUILD is short" | tee -a "$LOG" >&2; exit 1; }
    mv -n "$COPY_TMP" "$OUT_PATH"
    [ ! -e "$COPY_TMP" ] || { echo "FATAL: could not rename the copy to $OUT_PATH (it exists?)" | tee -a "$LOG" >&2; exit 1; }
    COPY_TMP=""
    rm -f "$OUT_BUILD"
fi

echo "=== validate @ $(date -Iseconds) ===" | tee -a "$LOG"
"$PY" /storage/streetzim/cloud/validate_zim.py "$OUT_PATH" 2>&1 | tee -a "$LOG" || \
    echo "(validate non-zero — review)" | tee -a "$LOG"

echo "=== done $ID @ $(date -Iseconds), size: $(du -h "$OUT_PATH" | cut -f1) ===" | tee -a "$LOG"
