#!/bin/bash
# Build a region's Wikipedia packs against its base with tools/build_wiki_pack.py
# (streetzim/wiki_pack.py), each into OUTDIR/.staging/; a pack reaches OUTDIR
# only when the builder's own check passes (builder_ok.py: this run's stats
# file, only zimru url_internal findings excused) and pack_gate.py passes.
# --offline: Wikidata text from the cache only.
# Usage: build_packs.sh REGION BASE OUTDIR "el de fr ..."
# Env: WIKI_SRC (required: the Kiwix Wikipedia ZIMs, --source), PYTHON (python3),
#   LANGPACK_WORK (default ./langpack-work: locks/, tmp/), WP_CACHE (the builder's
#   --cache; default its own), PACK_DATE (file-name date, default today),
#   PACK_GATE_ZIMCHECK (the official zimcheck, else zimcheck on PATH; anything
#   else is refused; the builder gets it as ZIMCHECK_BIN too), BUILDER (tests
#   substitute a fake), LANGPACK_MIN_FREE_GB (pause while MemAvailable is below it).
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
mkdir -p "$LANGPACK_WORK/tmp"
export PYTHONDONTWRITEBYTECODE=1 TMPDIR=$LANGPACK_WORK/tmp
BUILDER=${BUILDER:-$ROOT/tools/build_wiki_pack.py}  # tests substitute a fake
CACHE=(); [ -n "${WP_CACHE:-}" ] && CACHE=(--cache "$WP_CACHE")
LIM=(); command -v prlimit > /dev/null && LIM=(prlimit --as=8589934592 --)
MIN_GB=${LANGPACK_MIN_FREE_GB:-0}
wait_mem() { while [ "$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)" -lt "$MIN_GB" ]; do
  echo "paused $(date +%T): MemAvailable < $MIN_GB GB"; sleep 60; done; }
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
  echo "== $L start $(date +%T)"
  # build into .staging/; the pack reaches OUT only if pack_gate.py passes (review S1)
  F=osm_${R}_wiki-${L}_${DATE}.zim; ST=$OUT/.staging; mkdir -p "$ST"; rm -f "$ST/$F" "$ST/$F.gate.json"
  # this run's stats only (review r3): a stats file left by an earlier build must
  # never excuse this one, so delete it and mark the run's start
  STATS=$OUT/osm_${R}_wiki-${L}_${DATE}.stats.json; MARK=$ST/.run-$L
  rm -f "$STATS"; touch "$MARK"
  nice -n 19 "${LIM[@]}" timeout 3600 \
    "$PY" "$BUILDER" "$B" --lang "$L" \
      --source "$SRC" --region "$R" --images lead --offline \
      "${CACHE[@]}" --jobs 4 --workers 2 \
      -o "$ST/$F" --stats "$STATS" \
      > "$OUT/build-$L.log" 2>&1
  brc=$?
  # the builder's exit code counts (review r2 #4): only its zimru url_internal findings are
  # excused (entity false positives; pack_gate re-checks every link with the official zimcheck)
  "$PY" "$T/builder_ok.py" $brc "$STATS" "$ST/$F" "$MARK" > "$OUT/builder-ok-$L.log" 2>&1; bok=$?
  echo "   builder: $(cat "$OUT/builder-ok-$L.log")"
  if [ $bok = 0 ] && [ -s "$ST/$F" ] && nice -n 19 "${LIM[@]}" timeout 3600 "$PY" "$T/pack_gate.py" "$ST/$F" --base "$B" > "$OUT/gate-$L.log" 2>&1; then
    mv "$ST/$F" "$OUT/$F" && mv "$ST/$F.gate.json" "$OUT/$F.gate.json"
    echo "== $L builder_rc=$brc gate=PASS $(date +%T) $(stat -c %s "$OUT/$F")"
  else
    echo "== $L builder_rc=$brc builder_ok=$bok gate=FAIL $(date +%T) (kept in .staging; not usable)"
  fi
  exec {LK}>&-  # release the region+language lock
done
echo "== all done $(date +%T)"
exit $RC_ALL
