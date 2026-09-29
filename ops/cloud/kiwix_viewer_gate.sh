#!/bin/bash
# Gate a ZIM the way Kiwix actually runs it.
#
# Kiwix is by far the largest consumer of these files, and it is the ONE mode
# our existing gates could not see. cloud/pwa_smoke_test.mjs drives the
# website, whose service worker serves viewer/index.html, places.html and
# routing-worker.js from the SITE (VIEWER_SHELL_NAMES in web/drive/sw.js).
# That means the web gate passes using the on-disk viewer no matter how stale
# the copy inside the ZIM is — which is exactly how a broken Wikipedia panel
# and an outdated chip rail shipped to Kiwix users while the site looked fine.
#
# This gate serves the archive with real kiwix-serve (tools/kiwix-tools_*)
# and points the browser at /content/<book>/, so every byte comes from the
# ZIM. Nothing is read from web/.
#
# Usage: cloud/kiwix_viewer_gate.sh <file.zim> [port] [smoke-search]
# Env:   EXPECT_FIXES=0   assert the viewer is the OLD one (control run)
set -uo pipefail
cd /storage/streetzim

ZIM="${1:?usage: kiwix_viewer_gate.sh <file.zim> [port] [search]}"
PORT="${2:-8897}"
SEARCH="${3:-${SMOKE_SEARCH:-Zurich}}"
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
KS=$(readlink -f tools/kiwix-tools_*/kiwix-serve 2>/dev/null)
[ -x "$KS" ] || { echo "kiwix-serve not installed under tools/"; exit 3; }
[ -s "$ZIM" ] || { echo "no such ZIM: $ZIM"; exit 3; }

BOOK=$(basename "$ZIM" .zim)
LOG="tmp/kiwix-serve-$PORT.log"

# Detached: a plain `&` job dies when the calling tool/shell returns, and a
# large ZIM needs ~30 s to map before it answers at all.
setsid nohup "$KS" --port "$PORT" "$ZIM" > "$LOG" 2>&1 < /dev/null &
disown
SRV=""
for _ in $(seq 1 90); do
  sleep 2
  for p in $(pgrep -f kiwix-serve 2>/dev/null); do
    [ -r "/proc/$p/cmdline" ] || continue
    c=$(tr '\0' ' ' < "/proc/$p/cmdline")
    case "$c" in *kiwix-serve*--port\ $PORT*) SRV="$p" ;; esac
  done
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
         "http://localhost:$PORT/content/$BOOK/index.html" 2>/dev/null)
  [ "$code" = "200" ] && break
done
cleanup(){ [ -n "$SRV" ] && kill "$SRV" 2>/dev/null; }
trap cleanup EXIT

if [ "${code:-000}" != "200" ]; then
  echo "kiwix-serve never answered for book '$BOOK' (last=$code)"; sed 's/^/  /' "$LOG" | head -5; exit 4
fi
echo "  kiwix-serve up: http://localhost:$PORT/content/$BOOK/  (pid $SRV)"

# Cheap content assertion first — no point spending browser time on a ZIM
# whose viewer was not actually replaced.
BODY=$(curl -s -m 60 "http://localhost:$PORT/content/$BOOK/index.html")
w=$(printf '%s' "$BODY" | grep -c retryWikiGeoIndex)
c=$(printf '%s' "$BODY" | grep -c _findResolveChipDef)
echo "  in-ZIM viewer markers: retryWikiGeoIndex=$w _findResolveChipDef=$c"
if [ "${EXPECT_FIXES:-1}" = "1" ]; then
  [ "$w" -ge 1 ] && [ "$c" -ge 1 ] || { echo "  FAIL: ZIM still carries the OLD viewer"; exit 1; }
else
  [ "$w" -eq 0 ] && [ "$c" -eq 0 ] || { echo "  FAIL(control): expected the OLD viewer"; exit 1; }
fi

ZIM_ORIGIN="http://localhost:$PORT/content/$BOOK" SMOKE_SEARCH="$SEARCH" \
  timeout 900 "$NODE" cloud/zim_viewer_smoke.mjs
rc=$?
echo "  kiwix viewer smoke rc=$rc"
exit $rc
