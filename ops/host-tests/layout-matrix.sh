#!/usr/bin/env bash
# layout-matrix.sh <zim> <port> : overlap-check at 7 portrait + 4 landscape sizes; one line per size.
cd /storage/streetzim
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export CHROME_PATH=/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
KS=$(readlink -f tools/kiwix-tools_*/kiwix-serve); Z=$1; P=$2; B=$(basename "$Z" .zim)
setsid nohup "$KS" --port $P "$Z" > /storage/streetzim/sz-tests/tmp/ks-$P.log 2>&1 < /dev/null & KPID=$!
for i in $(seq 1 60); do [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://localhost:$P/content/$B/index.html)" = 200 ] && break; sleep 2; done
for s in 375x667 375x812 390x844 402x874 430x932 440x956 320x568 844x390 667x375 932x430 568x320; do
  o=$(ZIM_ORIGIN="http://localhost:$P/content/$B" VW=${s%x*} VH=${s#*x} timeout 120 $NODE tmp/overlap-check.mjs 2>/dev/null | sed -n '/^OVERLAPS:/,/OVERLAP-CHECK-DONE/p' | grep -vE 'OVERLAPS:|DONE' | sed -E 's/\.maplibregl-ctrl-(top|bottom)-(right|left) > \.maplibregl-ctrl/\1-\2-ctrl/g; s/ +overlap / /; s/^ +//' | tr '\n' ';')
  printf "  %-8s %s\n" "$s" "${o:-ok}"
done
for q in $(pgrep -x kiwix-serve); do c=$(tr '\0' ' ' < /proc/$q/cmdline); case "$c" in *"--port $P "*) kill $q;; esac; done
