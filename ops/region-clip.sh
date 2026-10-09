# shellcheck shell=bash
# Border clip for a region build: sourced by build-region-fast.sh, not run.
#
# region_clip_args <id> <src_id> <bbox> sets CLIP_ARGS to the --clip-poly
# flags and prints one status line. A region cloud/region-outlines.tsv does
# not list (by <src_id>: a variant uses its parent's row), or CLIP=0, gets no
# flags and builds by its box, as before. A listed region whose outline
# cannot be made returns 1: the build stops rather than falling back to the
# box silently. The outline is remade for every build (ops/region-outline.py,
# about a second), so a changed bbox or table row is always picked up.
#
# STREETZIM_ROOT (default /storage/streetzim) and PY are read for tests.
region_clip_args() {
  local id="$1" src_id="$2" bbox="$3"
  local root="${STREETZIM_ROOT:-/storage/streetzim}"
  local py="${PY:-$root/venv-linux/bin/python3}"
  local here table poly
  here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
  table="$root/cloud/region-outlines.tsv"
  CLIP_ARGS=()
  if [ "${CLIP:-1}" = 0 ]; then
    echo "  border clip: off (CLIP=0), building by the box"
    return 0
  fi
  [ -f "$table" ] || return 0
  # awk: 0 listed, 1 not listed (build by the box), anything else an
  # unreadable table, which must not pass for "not listed".
  awk -F'\t' -v id="$src_id" '$1==id {f=1} END {exit !f}' "$table"
  case $? in
    0) ;;
    1) return 0 ;;
    *) echo "FATAL: cannot read $table" >&2; return 1 ;;
  esac
  if ! poly=$("$py" "$here/region-outline.py" --outline-id "$src_id" -- "$id" "$bbox") \
     || [ ! -s "$poly" ]; then
    echo "FATAL: no border outline for $id (ops/region-outline.py); CLIP=0 builds by the box" >&2
    return 1
  fi
  CLIP_ARGS=( --clip-poly "$poly" --clip-buffer-km 10 --clip-min-zoom 10 )
  echo "  border clip: $(cut -f1 "$poly.source" 2>/dev/null | paste -sd+ -) (+10 km; zoom 0-10 over the whole box)"
}
