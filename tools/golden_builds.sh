#!/usr/bin/env bash
# Build Monaco from two versions of the builder with identical inputs and
# caches, and compare the ZIMs entry by entry (docs/golden-builds.md).
#
#   tools/golden_builds.sh [--tilemaker] BEFORE AFTER WORKDIR [-- BUILD FLAGS...]
#
# BEFORE and AFTER are git refs (commits, branches, tags) of this
# repository, or the word WORKTREE for the checkout as it is on disk,
# uncommitted changes included. BEFORE is built twice: the second build is
# the control, which shows what differs between two runs of the same code.
#
# Inputs are fetched into WORKDIR/inputs once and reused by every build:
#   monaco.osm.pbf    Geofabrik's Monaco extract (search, routing)
#   monaco.mbtiles    OpenFreeMap's Monaco tiles (the default mode)
# Put your own files there first to pin them. --tilemaker builds the tiles
# with tilemaker instead of reading monaco.mbtiles; it needs tilemaker 3 on
# PATH and the shapefiles in WORKDIR/inputs/{coastline,landcover}
# (scripts/fetch-shapefiles.sh WORKDIR/inputs). All builds share one
# download cache, WORKDIR/cache. BUILD FLAGS are added to every build.
#
# PYTHON (default python3) runs the builds and the comparison; it needs
# requirements.txt installed. The exit status is tools/golden_diff.py's:
# 0 when the two versions differ only in the expected ways.
set -euo pipefail

usage() { sed -n '5p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

tilemaker=0
if [ "${1:-}" = "--tilemaker" ]; then tilemaker=1; shift; fi
[ $# -ge 3 ] || usage
before=$1; after=$2; work=$3; shift 3
if [ $# -gt 0 ]; then
    [ "$1" = "--" ] || usage
    shift
fi
py=${PYTHON:-python3}
repo=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$work"
work=$(cd "$work" && pwd)
inputs=$work/inputs
mkdir -p "$inputs"

if [ ! -s "$inputs/monaco.osm.pbf" ]; then
    curl -fsSL --retry 3 -o "$inputs/monaco.osm.pbf.part" \
        https://download.geofabrik.de/europe/monaco-latest.osm.pbf
    mv "$inputs/monaco.osm.pbf.part" "$inputs/monaco.osm.pbf"
fi
if [ "$tilemaker" = 0 ] && [ ! -s "$inputs/monaco.mbtiles" ]; then
    "$py" "$repo/scripts/fetch-openfreemap-mbtiles.py" monaco -o "$inputs/monaco.mbtiles"
fi

# The source tree of one side: the checkout itself, or `git archive` of the
# ref's commit into WORKDIR/src-<sha> (the repository is only read).
source_tree() {
    local ref=$1 sha dest
    if [ "$ref" = WORKTREE ]; then
        echo "$repo"
        return
    fi
    sha=$(git -C "$repo" rev-parse --verify "$ref^{commit}")
    dest=$work/src-$sha
    if [ ! -f "$dest/create_osm_zim.py" ]; then
        mkdir -p "$dest"
        git -C "$repo" archive "$sha" | tar -x -C "$dest"
    fi
    echo "$dest"
}

build() {
    local src=$1 out=$2
    shift 2
    mkdir -p "$out"
    local tiles=(--mbtiles "$inputs/monaco.mbtiles")
    if [ "$tilemaker" = 1 ]; then
        tiles=()
        ln -sfn "$inputs/coastline" "$out/coastline"
        ln -sfn "$inputs/landcover" "$out/landcover"
    fi
    echo "building $out from $src"
    (cd "$out" && STREETZIM_CACHE_DIR="$work/cache" "$py" "$src/create_osm_zim.py" \
        ${tiles[@]+"${tiles[@]}"} --pbf "$inputs/monaco.osm.pbf" \
        --bbox 7.40,43.72,7.44,43.76 --name Monaco \
        --routing --spatial-chunk-scale 10 --split-find-chips \
        "$@" -o monaco.zim > build.log 2>&1) \
        || { echo "build failed; see $out/build.log" >&2; exit 1; }
}

src_before=$(source_tree "$before")
src_after=$(source_tree "$after")
# BEFORE first, so its downloads fill the cache the other two builds read.
build "$src_before" "$work/before" "$@"
build "$src_before" "$work/control" "$@"
build "$src_after" "$work/after" "$@"

decode=()
if [ "$tilemaker" = 1 ]; then decode=(--decode-tiles --coord-tolerance 0.0001); fi
"$py" "$repo/tools/golden_diff.py" "$work/before/monaco.zim" "$work/after/monaco.zim" \
    --control "$work/control/monaco.zim" ${decode[@]+"${decode[@]}"}
