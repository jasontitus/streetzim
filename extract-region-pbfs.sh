#!/usr/bin/env bash
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
mkdir -p "$OUTDIR"

todo=()
while IFS=$'\t' read -r id name bbox tier src dst search notes; do
  [ -z "$id" ] || [ "${id:0:1}" = "#" ] && continue
  [ -n "$ONLY" ] && [[ "$ONLY" != *",$id,"* ]] && continue
  out="$OUTDIR/$id.osm.pbf"
  if [ $FORCE -eq 0 ] && [ -f "$out" ] && [ ! -L "$out" ] && [ "$out" -nt "$PLANET" ]; then
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
      printf '  {"output": "%s.osm.pbf.part", "output_format": "pbf", "bbox": [%s]}' "$id" "$bbox"
    done
    echo "] }"
  } > "$cfg"
  echo "--- pass $((i/BATCH+1)): ${chunk[*]%%|*}"
  osmium extract -c "$cfg" "$PLANET" --overwrite --strategy complete_ways --progress
  for spec in "${chunk[@]}"; do
    id=${spec%%|*}
    rm -f "$OUTDIR/$id.osm.pbf"           # drops parent-region symlinks too
    mv -f "$OUTDIR/$id.osm.pbf.part" "$OUTDIR/$id.osm.pbf"
    printf "  %-28s %s\n" "$id" "$(du -h "$OUTDIR/$id.osm.pbf" | cut -f1)"
  done
  rm -f "$cfg"
  i=$((i+BATCH))
done
echo "=== done @ $(date -Iseconds)"
