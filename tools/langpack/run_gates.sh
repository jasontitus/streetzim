#!/bin/bash
# Menu gates v2 (review S1/S2/S7). For every map language L of the menu:
#   recipe   recipe2 menu (packs must pass pack_gate.py), merge_zim.py, the
#            recipe materialized == merge_zim's file (byte-exact)
#   verify   verify_merge.py (entry level, S3 UUID, S4 pack lineage)
#   zimcheck official zim-tools 3.8.0 -A
#   route    cloud/route_cli.py on the merged file
#   kiwix    kiwix-serve: /suggest, /search, an article + pack.json per pack,
#            /catalog/v2/entries Title and Language == the merged metadata
#   viewer   viewer_gate.mjs: WIKI_PACKS, source order, the offered articles
#   ui       ui_gate.mjs: <html lang> + a translated string, labels (name:L,
#            the city's displayed label, a bilingual label), popup -> pack
#            article -> images -> back-to-map, the map's search (native, English)
#   smoke    cloud/zim_viewer_smoke.mjs (map, chips, Find, page errors)
# plus the base alone (smoke, viewer, ui). kiwix-serve batches: the base, then
# at most 3 merged files per server. compose_verdicts.py writes one verdict per
# entry ($LANGPACK_WORK/verdicts/<base>/<L>.json), keyed to the recipe's md5
# and uuid. docs/language-pack-pipeline.md has the whole pipeline.
# Usage: run_gates.sh BASE PACKS_DIR LANGS REGION_LANGS REGION REGION_JSON PORT
# Env: LANGUAGE_META (list|map), TITLES (JSON {L: title}),
#   LANGPACK_WORK (default ./langpack-work: recipes, merged files, verdicts, gates/),
#   PYTHON (python3), PACK_GATE_ZIMCHECK (the official zim-tools zimcheck; else
#   zimcheck on PATH), KIWIX_SERVE (kiwix-serve), NODE (node), PLAYWRIGHT_CORE
#   (path to playwright-core's index.mjs; else the playwright-core package),
#   CHROME_PATH (a Chromium for it), BROWSER_LIBS (extra LD_LIBRARY_PATH for it),
#   LANGPACK_MIN_FREE_GB (pause while MemAvailable is below it; default 0).
set -uo pipefail
TL=$(cd "$(dirname "$0")" && pwd)
LP_ROOT=$(cd "$TL/../.." && pwd); export LP_ROOT
export LANGPACK_WORK=${LANGPACK_WORK:-$PWD/langpack-work}
W=$LANGPACK_WORK
export PYTHONDONTWRITEBYTECODE=1
PY=${PYTHON:-python3}
ZC=${PACK_GATE_ZIMCHECK:-zimcheck}
KS=${KIWIX_SERVE:-kiwix-serve}
NODE=${NODE:-node}
LIM=(); command -v prlimit > /dev/null && LIM=(prlimit --as=8589934592 --)
LIMB=(); command -v prlimit > /dev/null && LIMB=(prlimit --data=8589934592 --)   # Chromium fails under --as
N="nice -n 19 ${LIM[*]} timeout 7200"
NB="${LIMB[*]} timeout 900 nice -n 19"
MIN_GB=${LANGPACK_MIN_FREE_GB:-0}
wait_mem() { while [ "$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)" -lt "$MIN_GB" ]; do
  echo "paused $(date +%T): MemAvailable < $MIN_GB GB" >&2; sleep 60; done; }
BASE=$1; PACKS=$2; LANGS=$3; RL=$4; REGION=$5; RJ=$6; PORT=$7
LANGUAGE_META=${LANGUAGE_META:-map}; TITLES=${TITLES:-'{}'}
TAG=$(basename "$BASE" .zim)
OUT=$W/gates/$TAG; rm -rf "$OUT"; mkdir -p "$OUT" "$W/merged"
# Chromium's profile sockets must fit a unix socket path (~107 bytes): a short TMPDIR
CT=$(mktemp -d /tmp/lpgates.XXXXXX); trap 'rm -rf "$CT"' EXIT
[ -n "${BROWSER_LIBS:-}" ] && export LD_LIBRARY_PATH=$BROWSER_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
jget() { $PY -c 'import json,sys; d=json.load(open(sys.argv[1]));
for k in sys.argv[2].split("."): d = d.get(k, "") if isinstance(d, dict) else ""
print(d)' "$1" "$2"; }
SEARCH=$(jget "$RJ" search.english.q); [ -n "$SEARCH" ] || SEARCH=$(jget "$RJ" search.english)
NAMED=$(jget "$RJ" named_qids)
ROUTE_SRC=$(jget "$RJ" route.src); ROUTE_DST=$(jget "$RJ" route.dst)

wait_mem
$N $PY "$TL/recipe2.py" menu "$BASE" --packs-dir "$PACKS" --langs "$LANGS" --region-langs "$RL" --region "$REGION" \
   --titles "$TITLES" --language-meta "$LANGUAGE_META" --out-dir "$W/recipes/$TAG" > "$OUT/menu.jsonl" 2> "$OUT/menu.err" \
   || { echo "menu failed"; cat "$OUT/menu.err"; exit 1; }
files=()
while read -r line; do
  L=$($PY -c 'import json,sys; print(json.loads(sys.argv[1])["L"])' "$line")
  mapfile -t P < <($PY -c 'import json,sys; [print(f) for f in json.loads(sys.argv[1])["pack_files"]]' "$line")
  M="$W/merged/m-$TAG-$L.zim"
  T=$($PY -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], ""))' "$TITLES" "$L"); TA=(); [ -n "$T" ] && TA=(--title "$T")
  wait_mem
  $N $PY "$TL/merge_zim.py" "$BASE" "${P[@]}" -o "$M" --lang "$L" --region-langs "$RL" --region "$REGION" "${TA[@]}" \
     --language-meta "$LANGUAGE_META" > "$OUT/$L.merge.json" 2>&1
  $N $PY - "$W/recipes/$TAG/$L.json" "$BASE" "$M" "${P[@]}" > "$OUT/$L.same" 2>&1 <<'PY'
import json, sys, os
sys.path.insert(0, os.environ["LP_ROOT"])
from tools.langpack.recipe2 import materialize
from tools.langpack.merge_zim import open_pack
r = json.load(open(sys.argv[1])); base, merged = sys.argv[2], sys.argv[3]
packs = {open_pack(p).lang: p for p in sys.argv[4:]}
tmp = merged + ".fromrecipe"
materialize(r, base, packs, tmp)
same = os.system(f"cmp -s '{tmp}' '{merged}'") == 0
os.remove(tmp); print("PASS" if same else "FAIL")
PY
  wait_mem
  $N $PY "$TL/verify_merge.py" "$M" "$BASE" "${P[@]}" --lang "$L" --region-langs "$RL" --region "$REGION" "${TA[@]}" \
     --language-meta "$LANGUAGE_META" --queries "$SEARCH" > "$OUT/$L.verify.log" 2>&1
  wait_mem
  $N $ZC -A --threads=4 "$M" > "$OUT/$L.zimcheck.log" 2>&1
  $N $PY "$LP_ROOT/cloud/route_cli.py" --zim "$M" --src "$ROUTE_SRC" --dst "$ROUTE_DST" --mode astar > "$OUT/$L.route.log" 2>&1
  files+=("$M")
done < "$OUT/menu.jsonl"

B=http://localhost:$PORT
check_batch() {
  wait_mem
  setsid nohup "${LIM[@]}" timeout 7200 "$KS" --port "$PORT" "$@" > "$OUT/kiwix-serve.log" 2>&1 < /dev/null &
  local SRV=$!
  for _ in $(seq 1 90); do curl -s -m 2 -o /dev/null "$B/catalog/v2/entries" && break; sleep 1; done
  curl -s -m 30 "$B/catalog/v2/entries" > "$OUT/catalog.xml"
  for M in "$@"; do
    book=$(basename "$M" .zim | tr 'A-Z' 'a-z')
    if [ "$M" = "$BASE" ]; then L=base; LL=en; MODE=base; PF=-; else
      L=${M##*-}; L=${L%.zim}; LL=$L; MODE=merged
      PF=$($PY -c 'import json,sys,os; r=json.load(open(sys.argv[1])); p=r["packs"].get(sys.argv[2]); print(os.path.join(sys.argv[3], p["file"]) if p else "-")' "$W/recipes/$TAG/$L.json" "$L" "$PACKS")
      # kiwix-serve: suggest, search, an article + pack.json per pack, catalog Title/Language
      $PY - "$B" "$book" "$W/recipes/$TAG/$L.json" "$PACKS" "$OUT/catalog.xml" "$SEARCH" > "$OUT/$L.kiwix.json" 2>&1 <<'PY'
import json, os, re, sys, urllib.parse, urllib.request
sys.path.insert(0, os.environ["LP_ROOT"])
from tools.langpack.zimfmt import ZimReader
B, book, rp, pd, cat, q = sys.argv[1:7]
r = json.load(open(rp))
def get(u):
    try:
        with urllib.request.urlopen(u, timeout=60) as x:
            return x.status, x.read()
    except Exception as e:
        return getattr(e, "code", 0), b""
res = {"checks": {}}
st, body = get(f"{B}/suggest?content={book}&term={urllib.parse.quote(q)}&count=5")
n = sum(1 for d in json.loads(body or b"[]") if d.get("path")) if st == 200 else 0
res["suggest"] = n; res["checks"]["suggest"] = n >= 1
st, body = get(f"{B}/search?books.name={book}&pattern={urllib.parse.quote(q)}&format=xml")
n = body.count(b"<item>"); res["search"] = {"status": st, "items": n}; res["checks"]["search"] = st == 200 and n >= 1
for lang, p in r["packs"].items():
    z = ZimReader(os.path.join(pd, p["file"])); v1 = z.v1_listing()
    d = z.dirent_at(next(iter(z.iter_u32_blob(*v1))))
    st1, _ = get(f"{B}/content/{book}/{urllib.parse.quote(d.path.decode())}")
    st2, pj = get(f"{B}/content/{book}/~w/{lang}/pack.json")
    okp = st1 == 200 and st2 == 200 and json.loads(pj).get("lang") == lang
    res[f"pack_{lang}"] = {"article": st1, "pack_json": st2}; res["checks"][f"pack_{lang}"] = okp
xml = open(cat, encoding="utf-8").read()
ent = next((e for e in re.findall(r"<entry>(.*?)</entry>", xml, re.S) if f'/content/{book}"' in e), "")
title = (re.search(r"<title>(.*?)</title>", ent) or [None, None])[1]
lang = (re.search(r"<language>(.*?)</language>", ent) or [None, None])[1]
import html
res["catalog"] = {"title": html.unescape(title or ""), "language": lang, "want_title": r["title"], "want_language": r["language"]}
res["checks"]["catalog"] = html.unescape(title or "") == r["title"] and lang == r["language"]
res["pass"] = all(res["checks"].values())
print(json.dumps(res, ensure_ascii=False))
PY
    fi
    COMMON=$($N $PY "$TL/viewer_qids.py" "$BASE" "$PF" "$NAMED")
    wait_mem
    EP=$([ $MODE = merged ] && $PY -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["packs"]))' "$W/recipes/$TAG/$L.json" || echo 0)
    EXPECT_PACKS=$EP TMPDIR=$CT $NB "$NODE" "$TL/viewer_gate.mjs" "$B/content/$book" "$LL" "$NAMED" "$COMMON" $([ $MODE = base ] && echo base) > "$OUT/$L.viewer.json" 2>&1
    wait_mem
    TMPDIR=$CT $NB "$NODE" "$TL/ui_gate.mjs" "$B/content/$book" "$LL" "$RJ" $([ $MODE = base ] && echo base) > "$OUT/$L.ui.json" 2>&1
    wait_mem
    TMPDIR=$CT ZIM_ORIGIN="$B/content/$book" SMOKE_SEARCH="$SEARCH" $NB "$NODE" \
      "$LP_ROOT/cloud/zim_viewer_smoke.mjs" > "$OUT/$L.smoke.log" 2>&1
  done
  kill "$SRV" 2>/dev/null
  for _ in $(seq 1 30); do ss -ltn | grep -q ":$PORT " || break; sleep 1; done
}
check_batch "$BASE"
for ((i = 0; i < ${#files[@]}; i += 3)); do check_batch "${files[@]:i:3}"; done
$PY "$TL/compose_verdicts.py" "$OUT" "$BASE" "$LANGS" | tee "$OUT/summary.txt"
