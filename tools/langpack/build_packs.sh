#!/bin/bash
# Build a region's Wikipedia packs against its base with tools/build_wiki_pack.py
# (streetzim/wiki_pack.py), each into OUTDIR/.staging/; a pack reaches OUTDIR
# only when the builder's own check passes (builder_ok.py: this run's stats
# file, only zimru url_internal findings excused) and pack_gate.py passes.
# --offline: Wikidata text from the cache only.
# Usage: build_packs.sh REGION BASE OUTDIR "el de fr ..."
# Packs are named osm_<REGION>_wiki-<L>_<date>_<first 8 hex of the base's UUID>.zim.
# Env: WIKI_SRC (required: the Kiwix Wikipedia ZIMs, --source), PYTHON (python3),
#   LANGPACK_WORK (default ./langpack-work: locks/, tmp/), WP_CACHE (the builder's
#   --cache; default its own), PACK_DATE (file-name date, default today),
#   PACK_GATE_ZIMCHECK (the official zimcheck, else zimcheck on PATH; anything
#   else is refused; the builder gets it as ZIMCHECK_BIN too), BUILDER (tests
#   substitute a fake), LANGPACK_MIN_FREE_GB (pause while MemAvailable is below it),
#   LANGPACK_TMP (the builder's TMPDIR; default $LANGPACK_WORK/tmp),
#   PACK_BUILD_TIMEOUT (seconds a build may take; default 3 h per 3 GB of base,
#   at least 3 h: during a rebuild round the source ZIMs' random reads share the
#   disk with the map builds), PACK_GATE_TIMEOUT (pack_gate.py's, 3600),
#   WIKI_NVME_DIR (/mnt/data/tilemaker/streetzim-wiki: a source ZIM read from
#   the verified copy parallel_rebuild.sh keeps there of the English one, when
#   its size and its .verified marker match the original; else from WIKI_SRC).
# A build that ran out of its time (the builder's or the gate's timeout, exit
# 124) leaves OUTDIR/.timeout-<L>: ship_packs.sh tells the queue to try that
# language again later; any other failure leaves none.
set -uo pipefail
R=$1; B=$(readlink -f "$2"); LANGS=$4
T=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$T/../.." && pwd)
SRC=${WIKI_SRC:?set WIKI_SRC to the Kiwix Wikipedia ZIMs, the --source of the builder}
PY=${PYTHON:-python3}
ZC=${PACK_GATE_ZIMCHECK:-zimcheck}
# Only the official zimcheck (zim-tools) counts: zimru's prints the same
# "Overall Test Status" line but misreads hrefs with HTML entities.
ZV=$("$ZC" --version 2>&1 | head -n 1)
case "$ZV" in
  "zim-tools "*) ;;
  *) echo "$(basename "$0"): $ZC is not the official zimcheck (--version: '$ZV');" \
       "set PACK_GATE_ZIMCHECK to zim-tools' zimcheck" >&2; exit 1 ;;
esac
# the builder's own check (zimcheck -A -J) and pack_gate.py both use it
export ZIMCHECK_BIN=$ZC PACK_GATE_ZIMCHECK=$ZC
# checks done: only now create folders
mkdir -p "$3"; OUT=$(cd "$3" && pwd)
export LANGPACK_WORK=${LANGPACK_WORK:-$PWD/langpack-work}
DATE=${PACK_DATE:-$(date +%F)}
# The base's UUID (first 8 hex digits) is part of every pack's file name:
# packs for two bases of a region never share a name, on archive.org or here.
BU8=$(od -An -tx1 -j8 -N4 "$B" | tr -d ' \n')
[ ${#BU8} = 8 ] || { echo "$(basename "$0"): cannot read the base's UUID from $B" >&2; exit 1; }
TMPD=${LANGPACK_TMP:-$LANGPACK_WORK/tmp}
mkdir -p "$TMPD"
export PYTHONDONTWRITEBYTECODE=1 TMPDIR=$TMPD
BYTES=$(stat -L -c %s "$B")
TMO=${PACK_BUILD_TIMEOUT:-$(( ( (BYTES + 2999999999) / 3000000000 ) * 10800 ))}
[ "$TMO" -ge 10800 ] || [ -n "${PACK_BUILD_TIMEOUT:-}" ] || TMO=10800
GTMO=${PACK_GATE_TIMEOUT:-3600}
for _v in TMO GTMO; do case "${!_v}" in ''|*[!0-9]*|0) echo "$(basename "$0"): PACK_BUILD_TIMEOUT and PACK_GATE_TIMEOUT are seconds, not '${!_v}'" >&2; exit 1 ;; esac; done
NVD=${WIKI_NVME_DIR-/mnt/data/tilemaker/streetzim-wiki}
# L -> the builder's --source: the verified NVMe copy of L's source ZIM when
# there is one of exactly the original (size, and the "size mtime" of the
# original its .verified marker records, as parallel_rebuild.sh writes it),
# else WIKI_SRC (the builder picks the same file there: wiki_pack.pick_source)
source_for() {
  local orig copy size
  [ -n "$NVD" ] && orig=$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1])
from streetzim.wiki_pack import pick_source; print(pick_source(sys.argv[2], sys.argv[3]))' "$ROOT" "$SRC" "$1" 2>/dev/null) \
    && size=$(stat -c %s "$orig" 2>/dev/null) || { echo "$SRC"; return; }
  copy=$NVD/$(basename "$orig")
  if [ -f "$copy" ] && [ "$(stat -c %s "$copy")" = "$size" ] && [ "$(cat "$copy.verified" 2>/dev/null)" = "$size $(stat -c %Y "$orig")" ]; then
    echo "$copy"
  else
    echo "$SRC"
  fi
}
BUILDER=${BUILDER:-$ROOT/tools/build_wiki_pack.py}  # tests substitute a fake
CACHE=(); [ -n "${WP_CACHE:-}" ] && CACHE=(--cache "$WP_CACHE")
LIM=(); command -v prlimit > /dev/null && LIM=(prlimit --as=8589934592 --)
MIN_GB=${LANGPACK_MIN_FREE_GB:-0}
wait_mem() { while [ "$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)" -lt "$MIN_GB" ]; do
  echo "paused $(TZ=America/Los_Angeles date "+%T %Z"): MemAvailable < $MIN_GB GB"; sleep 60; done; }
cd "$OUT"
# one build per region+language at a time, whatever OUT (review r4): two runs would
# share the stats file, the run marker and the staging path
LOCKS=$LANGPACK_WORK/locks; mkdir -p "$LOCKS"; RC_ALL=0
for L in $LANGS; do
  LOCK=$LOCKS/build-$R-$L.lock
  exec {LK}>"$LOCK"
  if ! flock -n "$LK"; then
    echo "== $L REFUSED: another build_packs.sh run is building $R/$L (lock $LOCK held); nothing touched"
    exec {LK}>&-; RC_ALL=2; continue
  fi
  wait_mem
  echo "== $L start $(TZ=America/Los_Angeles date "+%T %Z")"
  # build into .staging/; the pack reaches OUT only if pack_gate.py passes (review S1)
  F=osm_${R}_wiki-${L}_${DATE}_${BU8}.zim; ST=$OUT/.staging; mkdir -p "$ST"; rm -f "$ST/$F" "$ST/$F.gate.json"
  # this run's stats only (review r3): a stats file left by an earlier build must
  # never excuse this one, so delete it and mark the run's start
  STATS=$OUT/${F%.zim}.stats.json; MARK=$ST/.run-$L
  rm -f "$STATS" "$OUT/.timeout-$L"; touch "$MARK"
  LSRC=$(source_for "$L")
  [ "$LSRC" = "$SRC" ] || echo "   source: $LSRC (the verified NVMe copy)"
  echo "   time limit: $TMO s"
  nice -n 19 "${LIM[@]}" timeout "$TMO" \
    "$PY" "$BUILDER" "$B" --lang "$L" \
      --source "$LSRC" --region "$R" --images lead --offline \
      "${CACHE[@]}" --jobs 4 --workers 2 \
      -o "$ST/$F" --stats "$STATS" \
      > "$OUT/build-$L.log" 2>&1
  brc=$?
  # the builder's exit code counts (review r2 #4): only its zimru url_internal findings are
  # excused (entity false positives; pack_gate re-checks every link with the official zimcheck)
  "$PY" "$T/builder_ok.py" $brc "$STATS" "$ST/$F" "$MARK" > "$OUT/builder-ok-$L.log" 2>&1; bok=$?
  echo "   builder: $(cat "$OUT/builder-ok-$L.log")"
  grc=-
  if [ $bok = 0 ] && [ -s "$ST/$F" ]; then
    nice -n 19 "${LIM[@]}" timeout "$GTMO" "$PY" "$T/pack_gate.py" "$ST/$F" --base "$B" > "$OUT/gate-$L.log" 2>&1
    grc=$?
  fi
  if [ "$grc" = 0 ]; then
    mv "$ST/$F" "$OUT/$F" && mv "$ST/$F.gate.json" "$OUT/$F.gate.json"
    echo "== $L builder_rc=$brc gate=PASS $(TZ=America/Los_Angeles date "+%T %Z") $(stat -c %s "$OUT/$F")"
  else
    echo "== $L builder_rc=$brc builder_ok=$bok gate=FAIL $(TZ=America/Los_Angeles date "+%T %Z") (kept in .staging; not usable)"
    if [ $brc = 124 ] || [ "$grc" = 124 ]; then
      echo "   $L ran out of its time ($([ $brc = 124 ] && echo "the builder's $TMO s" || echo "pack_gate.py's $GTMO s")): a later run tries it again"
      TZ=America/Los_Angeles date "+%F %T %Z" > "$OUT/.timeout-$L"
    fi
  fi
  exec {LK}>&-  # release the region+language lock
done
echo "== all done $(TZ=America/Los_Angeles date "+%T %Z")"
exit $RC_ALL
