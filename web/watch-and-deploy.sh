#!/bin/bash
# Poll Archive.org every 5 minutes; if the streetzim-* item count changes,
# regenerate the site and redeploy. Stops itself once all expected regions
# from web/generate.py REGIONS list are live (or after 24h).
set -e
cd /Users/jasontitus/experiments/streetzim
source venv312/bin/activate

# Total number of regions defined in REGIONS — when this many are live, exit.
EXPECTED=10
MAX_HOURS=24

start=$(date +%s)
last_count=-1

echo "watch-and-deploy: polling Archive.org every 5 minutes (target: $EXPECTED live)"

while true; do
  count=$(curl -sf "https://archive.org/advancedsearch.php?q=identifier%3Astreetzim-*&fl%5B%5D=identifier&rows=100&output=json" \
            | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('response',{}).get('docs',[])))" 2>/dev/null || echo "$last_count")

  if [ "$count" != "$last_count" ] && [ "$count" -gt 0 ] 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] Archive.org item count: $last_count -> $count, refreshing site..."
    if python3 web/generate.py --deploy 2>&1 | tail -5; then
      last_count=$count
    fi
  fi

  if [ "$count" -ge "$EXPECTED" ] 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] All $EXPECTED regions live. Watcher exiting."
    exit 0
  fi

  elapsed=$(( $(date +%s) - start ))
  if [ "$elapsed" -gt $((MAX_HOURS * 3600)) ]; then
    echo "[$(date '+%H:%M:%S')] Max watch time (${MAX_HOURS}h) reached. Watcher exiting."
    exit 0
  fi

  sleep 300
done
