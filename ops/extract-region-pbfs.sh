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
# One-pass osmium extraction of every regional PBF in cloud/regions.tsv from
# a planet file. A single planet read (~95 GB) feeds all outputs, instead of
# one 10-30 min planet scan per region (49 regions ≈ a day of scans).
#
# Usage: ./extract-region-pbfs.sh [--only a,b] [--force]
# Env:   PLANET   (default world-data/planet-2026-08-31.osm.pbf)
#        REGISTRY (default cloud/regions.tsv)
#        BATCH    (default 16 outputs per osmium pass — bounds RAM/open files)
#
# Skips regions whose extract is already newer than $PLANET unless --force.
# Output: world-data/regions/<id>.osm.pbf (old symlinks to a parent region
# are replaced by real extracts, which is what every PBF phase wants — see
# docs/new-region-setup.md "extract a real regional PBF").
set -euo pipefail
cd /storage/streetzim
# bbox_crosses / bbox_stale / bbox_mark
. ops/region-bbox.sh
PLANET="${PLANET:-/storage/streetzim/world-data/planet-2026-08-31.osm.pbf}"
REGISTRY="${REGISTRY:-cloud/regions.tsv}"
BATCH="${BATCH:-16}"
OUTDIR=/storage/streetzim/world-data/regions
ONLY=""; FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY=",$2,"; shift 2 ;;
    --force) FORCE=1; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -s "$PLANET" ] || { echo "planet missing: $PLANET (run ./download-planet.sh)" >&2; exit 1; }
# Shared with build-refresh-queue.sh: both write world-data/regions/*.part,
# and two osmium processes on one path yield a truncated-but-valid PBF.
mkdir -p "${TMPDIR:-/storage/streetzim/tmp}"
exec 9>"${TMPDIR:-/storage/streetzim/tmp}/.regions-pbf.lock"
flock -n 9 || { echo "another PBF extractor or the build queue holds the regions lock" >&2; exit 1; }
mkdir -p "$OUTDIR"

todo=()
while IFS=$'\t' read -r id name bbox tier src dst search notes; do
  [ -z "$id" ] || [ "${id:0:1}" = "#" ] && continue
  [ -n "$ONLY" ] && [[ "$ONLY" != *",$id,"* ]] && continue
  out="$OUTDIR/$id.osm.pbf"
  if [ $FORCE -eq 0 ] && [ -f "$out" ] && [ ! -L "$out" ] && [ "$out" -nt "$PLANET" ] && ! bbox_stale "$out" "$bbox"; then
    echo "skip $id (extract newer than planet)"; continue
  fi
  todo+=("$id|$bbox")
done < "$REGISTRY"
echo "=== ${#todo[@]} regions to extract from $PLANET @ $(date -Iseconds)"

i=0
while [ $i -lt ${#todo[@]} ]; do
  chunk=("${todo[@]:$i:$BATCH}")
  cfg=$(mktemp "${TMPDIR:-/storage/streetzim/tmp}/extract.XXXXXX.json")
  {
    echo "{ \"directory\": \"$OUTDIR\", \"extracts\": ["
    first=1
    for spec in "${chunk[@]}"; do
      id=${spec%%|*}; bbox=${spec#*|}
      [ $first -eq 1 ] || echo ","
      first=0
      # write to a .part name; renamed after the pass so a crash never leaves a truncated <id>.osm.pbf
      if bbox_crosses "$bbox"; then
        # Across the antimeridian (minlon > maxlon): a box each side of 180.
        printf '  {"output": "%s.osm.pbf.part", "output_format": "pbf", %s}' "$id" "$(awk -F, '{
          e = ($3 > 180) ? $3 - 360 : $3
          printf "\"multipolygon\": [[[[%s,%s],[180,%s],[180,%s],[%s,%s],[%s,%s]]],", $1, $2, $2, $4, $1, $4, $1, $2
          printf "[[[-180,%s],[%s,%s],[%s,%s],[-180,%s],[-180,%s]]]]", $2, e, $2, e, $4, $4, $2
        }' <<< "$bbox")"
      else
        printf '  {"output": "%s.osm.pbf.part", "output_format": "pbf", "bbox": [%s]}' "$id" "$bbox"
      fi
    done
    echo "] }"
  } > "$cfg"
  echo "--- pass $((i/BATCH+1)): ${chunk[*]%%|*}"
  osmium extract -c "$cfg" "$PLANET" --overwrite --strategy complete_ways --progress
  for spec in "${chunk[@]}"; do
    id=${spec%%|*}; bbox=${spec#*|}
    rm -f "$OUTDIR/$id.osm.pbf"           # drops parent-region symlinks too
    mv -f "$OUTDIR/$id.osm.pbf.part" "$OUTDIR/$id.osm.pbf"
    bbox_mark "$OUTDIR/$id.osm.pbf" "$bbox"
    printf "  %-28s %s\n" "$id" "$(du -h "$OUTDIR/$id.osm.pbf" | cut -f1)"
  done
  rm -f "$cfg"
  i=$((i+BATCH))
done
echo "=== done @ $(date -Iseconds)"
