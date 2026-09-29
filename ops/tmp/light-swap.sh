#!/bin/bash
cd /storage/streetzim
PY=/storage/streetzim/venv-linux/bin/python3
# Resolve the NEWEST z13 build. The 2026-09-18 one is quarantined in
# rejected-zims/ (built without --overture-places: chips at 44%, fuel empty).
SRC=$(ls -t osm-switzerland-nosat-z13-20??-??-??.zim 2>/dev/null | head -1)
OUT=osm-switzerland-light-$(date +%Y-%m-%d).zim
[ -n "$SRC" ] || { echo "no z13 build found"; exit 1; }
echo "   src=$SRC out=$OUT"
echo "== light viewer swap start $(date '+%H:%M:%S %Z')"
nice -n 5 "$PY" -u cloud/swap_viewer_rust.py "$SRC" "$OUT" 2>&1 | tail -20
echo "== swap rc=${PIPESTATUS[0]} done $(date '+%H:%M:%S %Z')"
ls -la "$OUT" 2>/dev/null | awk '{printf "   OUTPUT %.4f GB\n",$5/1073741824}'
echo "LIGHT-SWAP-DONE"
