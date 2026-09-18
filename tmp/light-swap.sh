#!/bin/bash
cd /storage/streetzim
PY=/storage/streetzim/venv-linux/bin/python3
SRC=osm-switzerland-nosat-z13-2026-09-18.zim
OUT=osm-switzerland-light-2026-09-19.zim
echo "== light viewer swap start $(date '+%H:%M:%S %Z')"
nice -n 5 "$PY" -u cloud/swap_viewer_rust.py "$SRC" "$OUT" 2>&1 | tail -20
echo "== swap rc=${PIPESTATUS[0]} done $(date '+%H:%M:%S %Z')"
ls -la "$OUT" 2>/dev/null | awk '{printf "   OUTPUT %.4f GB\n",$5/1073741824}'
echo "LIGHT-SWAP-DONE"
