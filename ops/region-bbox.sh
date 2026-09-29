# shellcheck shell=bash
# Region bboxes in the ops scripts: sourced, not run.
#
# Region artifacts (world-data/regions/<id>.osm.pbf, .mbtiles, .search.jsonl,
# overture_cache/<theme>-<id>-<release>.parquet) are named by region id, not
# by bbox. When a row's bbox changes (alaska grew its 172E side), an old
# artifact would be reused and the build would silently lose the new part.
# So each producer writes <file>.bbox holding the registry bbox it was cut
# for, and a consumer treats a file whose sidecar names another bbox as
# stale. A file with no sidecar (made before sidecars existed) is trusted
# for a normal row, and its sidecar written. For a row across the
# antimeridian it is treated as stale, loudly: such rows are new, and a
# file without a sidecar predates their bbox (ops/docs/new-region-setup.md).

# bbox_crosses <bbox>: true when minlon > maxlon (or maxlon > 180).
bbox_crosses() { awk -F, '{ exit !($1 > $3 || $3 > 180) }' <<< "$1"; }

_bbox_say() {
  if declare -F log > /dev/null; then log "$*"; else echo "$*" >&2; fi
}

# bbox_stale <file> <bbox>: true when <file> exists but was cut for another
# bbox (see above). False for a missing file: callers test that themselves.
bbox_stale() {
  local f="$1" bbox="$2"
  [ -e "$f" ] || return 1
  if [ -f "$f.bbox" ]; then
    [ "$(cat "$f.bbox")" != "$bbox" ] || return 1
    _bbox_say "  $(basename "$f") was cut for bbox $(cat "$f.bbox"), not $bbox: stale"
    return 0
  fi
  if bbox_crosses "$bbox"; then
    _bbox_say "  WARNING: $(basename "$f") has no .bbox sidecar and $bbox crosses the antimeridian; it may predate that bbox, treating it as STALE (ops/docs/new-region-setup.md)"
    return 0
  fi
  printf '%s\n' "$bbox" > "$f.bbox"
  return 1
}

# bbox_mark <file> <bbox>: record the bbox <file> was just cut for.
bbox_mark() { printf '%s\n' "$2" > "$1.bbox"; }

# bbox_osmium_area <bbox> <poly>: set AREA to the osmium extract arguments,
# `-b <bbox>` for a normal row. A bbox across the antimeridian must never
# reach `osmium extract -b`: osmium 1.16 takes minlon > maxlon without
# complaint and extracts the COMPLEMENT (the band round the other side of
# the world). It gets a two-ring .poly instead; if that cannot be written
# this fails (AREA empty) and the caller skips the row.
bbox_osmium_area() {
  AREA=(-b "$1")
  bbox_crosses "$1" || return 0
  AREA=()
  "$PY" -m streetzim.area poly "$1" "$2" && [ -s "$2" ] || return 1
  AREA=(-p "$2")
}
