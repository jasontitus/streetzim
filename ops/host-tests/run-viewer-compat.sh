#!/usr/bin/env bash
# New /drive/ viewer vs old, each against published ZIMs. Serves only copies under $SZT; production web/ untouched.
. "$HOME/sz-env.sh" || exit
export PATH=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin:$PATH
export CHROME_PATH=/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
OUT=$SZT/results/viewer-compat; mkdir -p "$OUT"
# id zim src dst search
ROWS="washington-dc osm-washington-dc-2026-09-26.zim 38.9076,-77.0723 38.8973,-77.0063 Georgetown
hispaniola osm-hispaniola-2026-09-06.zim 18.4861,-69.9312 19.4517,-70.6970 Santo_Domingo
alaska osm-alaska-2026-09-26.zim 61.2181,-149.9003 64.8378,-147.7164 Anchorage
east-coast-us osm-east-coast-us-2026-09-29.zim 40.7128,-74.0060 39.9526,-75.1652 Boston"
echo "start $(date -Is)"
for side in old new; do
  if [ $side = old ]; then WEB=$SZT/web-old; PORT=8950; else WEB=$SZT/next/web; PORT=8951; fi
  "$PY" "$SZT/next/scripts/serve-web-local.py" $PORT "$WEB" > "$OUT/serve-$side.log" 2>&1 &
  HTTP=$!; sleep 2
  while read -r id zim src dst search; do
    search=${search//_/ }
    ln -sfn "/storage/streetzim/$zim" "$WEB/$zim"
    STREETZIM_SITE="http://localhost:$PORT" ZIM_URL="http://localhost:$PORT/$zim" ZIM_FILE="/storage/streetzim/$zim" \
      SMOKE_ROUTE="$src;$dst" SMOKE_SEARCH="$search" timeout 900 node "$SZT/next/cloud/pwa_smoke_test.mjs" > "$OUT/$side-$id.log" 2>&1
    rc=$?
    rm -f "$WEB/$zim"
    printf "%-4s %-14s rc=%s  pass=%s fail=%s\n" $side $id $rc "$(grep -ac '\[PASS\]' "$OUT/$side-$id.log")" "$(grep -ac '\[FAIL\]' "$OUT/$side-$id.log")"
  done <<< "$ROWS"
  kill $HTTP 2>/dev/null; wait $HTTP 2>/dev/null
done
echo "end $(date -Is)"
