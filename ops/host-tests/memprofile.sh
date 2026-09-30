#!/usr/bin/env bash
# memprofile.sh <container> <build.log> <out.tsv> : every 5 s, the container's
# total PSS (GB), its process count, the three largest processes by PSS, and the
# build log's latest line, so each memory peak can be tied to a build step.
C=$1; LOG=$2; OUT=$3
printf 't_s\tpss_gb\tnproc\ttop3\tlog\n' > "$OUT"; t0=$(date +%s)
while docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null | grep -q true; do
  pids=$(docker top "$C" -eo pid 2>/dev/null | tail -n +2)
  tot=0; rows=""
  for p in $pids; do
    k=$(awk '/^Pss:/{print $2}' /proc/$p/smaps_rollup 2>/dev/null); [ -n "$k" ] || continue
    tot=$((tot+k)); rows+="$k $(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | cut -c1-60 | tr '\t' ' ')"$'\n'
  done
  top=$(printf '%s' "$rows" | sort -rn | head -3 | awk '{printf "%.2fG %s %s %s; ", $1/1048576, $2, $3, $4}')
  last=$(tail -c 3000 "$LOG" 2>/dev/null | tr '\r' '\n' | grep -v '^\s*$' | tail -1 | cut -c1-120 | tr '\t' ' ')
  printf '%s\t%.2f\t%s\t%s\t%s\n' $(( $(date +%s)-t0 )) "$(echo "$tot/1048576" | bc -l)" "$(echo "$pids" | wc -w)" "$top" "$last" >> "$OUT"
  sleep 5
done
