#!/bin/bash
# The gates of a map with a fresh viewer (tools/langpack/fresh_viewer.py), as
# Kiwix runs it: the file served alone by kiwix-serve on 127.0.0.1 (nothing is
# served beyond this host), then the browser checks run_gates.sh gives a base
#   ui       ui_gate.mjs: <html lang>, labels, a city's popup and its article,
#            the map's search
#   viewer   viewer_gate.mjs: the Wikipedia the viewer offers
#   smoke    cloud/zim_viewer_smoke.mjs: map, chips, Find, page errors
# and cloud/viewer_scroll_check.mjs (the search bar stays on screen, the
# "Search this area" button out of the way). Only the viewer differs from
# the live file, which passed the full gates when it was built; the routing,
# search and zimcheck gates see the same bytes and are not run again.
# Usage: fresh_viewer_gates.sh ZIM [REGION_JSON]
#   REGION_JSON: the gates' region file (tools/langpack/region_json.py makes
#   one from ZIM when it is not given). Logs in $FRESH_GATES_LOG (default
#   ZIM.gates/). Exit 0 when every check passed.
# Env as run_gates.sh, set by the caller: KIWIX_SERVE, NODE, PLAYWRIGHT_CORE,
# CHROME_PATH, BROWSER_LIBS, PYTHON (the build host's host-env.sh sets them).
set -u
ZIM=$(readlink -f "${1:?usage: fresh_viewer_gates.sh ZIM [REGION_JSON]}")
RJ=${2:-}
LP_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)
TL=$LP_ROOT/tools/langpack
export LP_ROOT
PY=${PYTHON:-python3}
KS=${KIWIX_SERVE:-kiwix-serve}
NODE=${NODE:-node}
OUT=${FRESH_GATES_LOG:-$ZIM.gates}
mkdir -p "$OUT"
[ -n "${BROWSER_LIBS:-}" ] && export LD_LIBRARY_PATH=$BROWSER_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
# Chromium's profile sockets must fit a unix socket path (~107 bytes): a short TMPDIR
CT=$(mktemp -d /tmp/fvgates.XXXXXX)
# kiwix-serve names the book after the file: a plain name for it
ln -s "$ZIM" "$CT/fresh.zim"
book=fresh
PORT=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
B=http://127.0.0.1:$PORT
SRV=
cleanup() { [ -n "$SRV" ] && kill "$SRV" 2>/dev/null; rm -rf "${CT:?}"; }
trap cleanup EXIT
trap 'exit 143' TERM INT HUP
if [ -z "$RJ" ]; then
  RJ=$OUT/region.json
  "$PY" "$TL/region_json.py" "$ZIM" -o "$RJ" > "$OUT/region_json.log" 2>&1 || { echo "fresh_viewer_gates: region_json.py failed (see $OUT/region_json.log)" >&2; exit 1; }
fi
jget() { "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1]));
for k in sys.argv[2].split("."): d = d.get(k, "") if isinstance(d, dict) else ""
print(d)' "$1" "$2"; }
SEARCH=$(jget "$RJ" search.english.q); [ -n "$SEARCH" ] || SEARCH=$(jget "$RJ" search.english)
NAMED=$(jget "$RJ" named_qids)

setsid nohup timeout 3600 "$KS" --address 127.0.0.1 --port "$PORT" "$CT/fresh.zim" > "$OUT/kiwix-serve.log" 2>&1 < /dev/null &
SRV=$!
for _ in $(seq 1 90); do curl -s -m 2 -o /dev/null "$B/catalog/v2/entries" && break; sleep 1; done
NB="timeout 900 nice -n 19"
COMMON=$("$PY" "$TL/viewer_qids.py" "$ZIM" - "$NAMED")
TMPDIR=$CT $NB "$NODE" "$TL/ui_gate.mjs" "$B/content/$book" en "$RJ" base > "$OUT/ui.json" 2>&1
EXPECT_PACKS=0 TMPDIR=$CT $NB "$NODE" "$TL/viewer_gate.mjs" "$B/content/$book" en "$NAMED" "$COMMON" base > "$OUT/viewer.json" 2>&1
TMPDIR=$CT ZIM_ORIGIN="$B/content/$book" SMOKE_SEARCH="$SEARCH" $NB "$NODE" "$LP_ROOT/cloud/zim_viewer_smoke.mjs" > "$OUT/smoke.log" 2>&1
TMPDIR=$CT ZIM_ORIGIN="$B/content/$book" SMOKE_SEARCH="$SEARCH" $NB "$NODE" "$LP_ROOT/cloud/viewer_scroll_check.mjs" > "$OUT/scroll.json" 2>&1

"$PY" - "$OUT" <<'PY'
import json, re, sys
out = sys.argv[1]
def last_json(p):
    try:
        lines = [x for x in open(p, encoding="utf-8", errors="replace") if x.startswith("{")]
        return json.loads(lines[-1])
    except (OSError, ValueError, IndexError):
        return {}
try:
    sm = re.search(r"^(\d+) passed, (\d+) failed", open(f"{out}/smoke.log", errors="replace").read(), re.M)
except OSError:
    sm = None
checks = {"ui": last_json(f"{out}/ui.json").get("pass") is True,
          "viewer": last_json(f"{out}/viewer.json").get("pass") is True,
          "smoke": bool(sm and sm.group(2) == "0" and int(sm.group(1)) > 0),
          "scroll": last_json(f"{out}/scroll.json").get("pass") is True}
ok = all(checks.values())
print(json.dumps({"pass": ok, "checks": checks, "logs": out}))
sys.exit(0 if ok else 1)
PY
