#!/bin/bash
# Menu gates v2 (review S1/S2/S7). For every map language L of the menu:
#   recipe   recipe2 menu (packs must pass pack_gate.py), merge_zim.py, the
#            recipe materialized == merge_zim's file (byte-exact)
#   verify   verify_merge.py (entry level, S3 UUID, S4 pack lineage)
#   zimcheck official zim-tools 3.8.0 -A
#   route    cloud/route_cli.py on the merged file
#   search   search_gate.py: Kiwix's search (libzim suggestions) finds pack-only
#            article titles in the merged file (LANGPACK_SEARCH_GATE=report:
#            recorded, not required)
#   kiwix    kiwix-serve: /suggest, /search, an article + pack.json per pack,
#            /catalog/v2/entries Title and Language == the merged metadata
#   viewer   viewer_gate.mjs: WIKI_PACKS, source order, the offered articles
#   ui       ui_gate.mjs: <html lang> + a translated string, labels (name:L,
#            the city's displayed label, a bilingual label), popup -> pack
#            article -> images -> back-to-map, the map's search (native, English)
#   smoke    cloud/zim_viewer_smoke.mjs (map, chips, Find, page errors)
# plus the base alone (smoke, viewer, ui). GATE_JOBS (1) entries at a time,
# each a job of its own (its kiwix-serve on its own port, its own logs): an
# entry's merged file is made, checked (served alone by kiwix-serve) and
# deleted (KEEP_MERGED=1 keeps them), so the disk holds at most GATE_JOBS
# merged files and their recipe copies: the run refuses to start while the
# merged folder's disk has less than twice the base plus
# LANGPACK_DISK_MARGIN_GB free; an entry waits for a running one to end while
# there is no such room for it beside the running ones, and with none
# running, neither it nor any later entry runs. compose_verdicts.py writes one
# verdict per entry ($LANGPACK_WORK/verdicts/<base>/<L>.json), keyed to the
# recipe's md5 and uuid. RESUME=1 keeps the last run's logs and skips an entry
# whose verdict passes for the very recipe this run makes (and the base's
# checks when they passed). docs/language-pack-pipeline.md has the pipeline.
# Usage: run_gates.sh BASE PACKS_DIR LANGS REGION_LANGS REGION REGION_JSON PORT
# Env: LANGUAGE_META (list|map), TITLES (JSON {L: title}),
#   LANGPACK_WORK (default ./langpack-work: recipes, merged files, verdicts, gates/),
#   PYTHON (python3), PACK_GATE_ZIMCHECK (the official zim-tools zimcheck, else
#   zimcheck on PATH; anything else is refused), KIWIX_SERVE (kiwix-serve), NODE (node), PLAYWRIGHT_CORE
#   (path to playwright-core's index.mjs; else the playwright-core package),
#   CHROME_PATH (a Chromium for it), BROWSER_LIBS (extra LD_LIBRARY_PATH for it),
#   LANGPACK_MIN_FREE_GB (pause while MemAvailable is below it; default 0),
#   LANGPACK_DISK_MARGIN_GB (free disk asked beyond 2 x the base; default 20),
#   KEEP_MERGED (1: keep every merged file), RESUME (1: see above),
#   GATE_JOBS (entries checked at a time, 1), LANGPACK_MERGED (the merged
#   files' folder; default $LANGPACK_WORK/merged: ship_packs.sh puts it on the
#   NVMe scratch).
set -uo pipefail
TL=$(cd "$(dirname "$0")" && pwd)
LP_ROOT=$(cd "$TL/../.." && pwd); export LP_ROOT
export LANGPACK_WORK=${LANGPACK_WORK:-$PWD/langpack-work}
W=$LANGPACK_WORK
export PYTHONDONTWRITEBYTECODE=1
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
KS=${KIWIX_SERVE:-kiwix-serve}
NODE=${NODE:-node}
LIM=(); command -v prlimit > /dev/null && LIM=(prlimit --as=8589934592 --)
LIMB=(); command -v prlimit > /dev/null && LIMB=(prlimit --data=8589934592 --)   # Chromium fails under --as
N="nice -n 19 ${LIM[*]} timeout 7200"
NB="${LIMB[*]} timeout 900 nice -n 19"
MIN_GB=${LANGPACK_MIN_FREE_GB:-0}
wait_mem() { while [ "$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)" -lt "$MIN_GB" ]; do
  echo "paused $(TZ=America/Los_Angeles date "+%T %Z"): MemAvailable < $MIN_GB GB" >&2; sleep 60; done; }
BASE=$1; PACKS=$2; LANGS=$3; RL=$4; REGION=$5; RJ=$6; PORT=$7
LANGUAGE_META=${LANGUAGE_META:-map}; TITLES=${TITLES:-'{}'}
TAG=$(basename "$BASE" .zim)
OUT=$W/gates/$TAG
RESUME=${RESUME:-0}; KEEP=${KEEP_MERGED:-0}
JOBS=${GATE_JOBS:-1}
case "$JOBS" in ''|*[!0-9]*|0) echo "$(basename "$0"): GATE_JOBS must be a positive integer, not '$JOBS'" >&2; exit 1 ;; esac
MD=${LANGPACK_MERGED:-$W/merged}
[ "$RESUME" = 1 ] || rm -rf "$OUT"
mkdir -p "$OUT" "$MD"
printf '%s\n' "$ZV" > "$OUT/zimcheck.version"   # compose_verdicts.py records it
# Disk: per entry one merged file (about the base plus its packs) and,
# during the recipe = merge check, its copy from the recipe.
JOB_KB=$(( $(stat -L -c %s "$BASE") / 1024 * 2 ))
MARGIN_KB=$(( ${LANGPACK_DISK_MARGIN_GB:-20} * 1048576 ))
NEED_KB=$(( JOB_KB + MARGIN_KB ))
disk_ok() { [ "$(df -Pk "$MD" | awk 'NR == 2 {print $4}')" -ge $(( JOB_KB * ${1:-1} + MARGIN_KB )) ]; }   # [entries]
if ! disk_ok 1; then
  echo "$(basename "$0"): $MD has $(df -Pk "$MD" | awk 'NR == 2 {print int($4/1048576)}') GB free;" \
       "a merged file needs $(( NEED_KB / 1048576 )) GB (2 x the base + LANGPACK_DISK_MARGIN_GB)" >&2
  exit 1
fi
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
# zimcheck -A, single-threaded: with --threads=4, zim-tools 3.8.0 twice
# stopped making progress in its redundant-data pass on Greece's merged
# files (el 2026-10-21, ja 2026-10-22: 41 min at 100% CPU, 193 log lines,
# where --threads=1 passed in 27 s). A run killed by a signal (the OOM
# killer, a shutdown) is run once more; one that ended on its own without
# its verdict line, or hit the 2 h timeout, fails: the same run would only
# do the same again. A Fail is never retried.
zimcheck_merged() {
  $N $ZC -A --threads=1 "$1" > "$2" 2>&1
  local rc=$?
  grep -q 'Overall Test Status:' "$2" && return
  { [ $rc -gt 128 ] && [ $rc != 124 ]; } || return 0
  mv -f "$2" "$2.first"
  echo "zimcheck: killed (rc=$rc), run again ($2.first)" >&2
  $N $ZC -A --threads=1 "$1" > "$2" 2>&1
}

B=http://127.0.0.1:$PORT
SRV=
serve() {     # LABEL BOOK...: kiwix-serve on 127.0.0.1:$PORT (this job's) with these books (nothing is served beyond this host)
  local lbl=$1; shift
  wait_mem
  setsid nohup "${LIM[@]}" timeout 7200 "$KS" --address 127.0.0.1 --port "$PORT" "$@" > "$OUT/$lbl.kiwix-serve.log" 2>&1 < /dev/null &
  SRV=$!
  for _ in $(seq 1 90); do curl -s -m 2 -o /dev/null "$B/catalog/v2/entries" && break; sleep 1; done
  curl -s -m 30 "$B/catalog/v2/entries" > "$OUT/$lbl.catalog.xml"
}
unserve() {
  [ -n "$SRV" ] && kill "$SRV" 2>/dev/null
  SRV=
  for _ in $(seq 1 30); do ss -ltn | grep -q ":$PORT " || break; sleep 1; done
}

# The browser checks (and, merged, the kiwix-serve ones) of one served book.
browser_checks() {   # M L(base|lang)
  local M=$1 L=$2 LL MODE PF EP COMMON book
  book=$(basename "$M" .zim | tr 'A-Z' 'a-z')
  if [ "$L" = base ]; then LL=en; MODE=base; PF=-; else
    LL=$L; MODE=merged
    PF=$($PY -c 'import json,sys,os; r=json.load(open(sys.argv[1])); p=r["packs"].get(sys.argv[2]); print(os.path.join(sys.argv[3], p["file"]) if p else "-")' "$W/recipes/$TAG/$L.json" "$L" "$PACKS")
    # kiwix-serve: suggest, search, an article + pack.json per pack, catalog Title/Language
    # (carried over by a light gate)
    [ "${LIGHT:-0}" = 1 ] || \
      $PY - "$B" "$book" "$W/recipes/$TAG/$L.json" "$PACKS" "$OUT/$L.catalog.xml" "$SEARCH" "$OUT/$L.search.json" > "$OUT/$L.kiwix.json" 2>&1 <<'PY'
import json, os, re, sys, urllib.parse, urllib.request
sys.path.insert(0, os.environ["LP_ROOT"])
from tools.langpack.zimfmt import ZimReader
B, book, rp, pd, cat, q, sgj = sys.argv[1:8]
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
    if p.get("kind") == "search":
        continue
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
# pack-only titles (search_gate.py's sample) through kiwix-serve's /suggest and /search
try:
    rows = json.loads([x for x in open(sgj, encoding="utf-8") if x.startswith("{")][-1]).get("rows") or []
except (OSError, ValueError, IndexError):
    rows = []
found = []
for row in rows[:3]:
    t = urllib.parse.quote(row["title"])
    st, body = get(f"{B}/suggest?content={book}&term={t}&count=10")
    sug = st == 200 and any(str(d.get("path", "")).startswith("~w/") for d in json.loads(body or b"[]"))
    st, body = get(f"{B}/search?books.name={book}&pattern={t}&format=xml")
    ful = st == 200 and b"/~w/" in body
    found.append({"title": row["title"], "suggest": sug, "search": ful})
res["pack_titles"] = found
if rows:
    res["checks"]["pack_titles_suggest"] = all(x["suggest"] for x in found)
res["pass"] = all(res["checks"].values())
print(json.dumps(res, ensure_ascii=False))
PY
  fi
  COMMON=$($N $PY "$TL/viewer_qids.py" "$BASE" "$PF" "$NAMED")
  wait_mem
  # the Wikipedia packs the viewer must load (a search pack is not one)
  EP=$([ $MODE = merged ] && $PY -c 'import json,sys; print(sum(1 for p in json.load(open(sys.argv[1]))["packs"].values() if p.get("kind") != "search"))' "$W/recipes/$TAG/$L.json" || echo 0)
  EXPECT_PACKS=$EP TMPDIR=$CT $NB "$NODE" "$TL/viewer_gate.mjs" "$B/content/$book" "$LL" "$NAMED" "$COMMON" $([ $MODE = base ] && echo base) > "$OUT/$L.viewer.json" 2>&1
  wait_mem
  TMPDIR=$CT $NB "$NODE" "$TL/ui_gate.mjs" "$B/content/$book" "$LL" "$RJ" $([ $MODE = base ] && echo base) > "$OUT/$L.ui.json" 2>&1
  wait_mem
  TMPDIR=$CT ZIM_ORIGIN="$B/content/$book" SMOKE_SEARCH="$SEARCH" $NB "$NODE" \
    "$LP_ROOT/cloud/zim_viewer_smoke.mjs" > "$OUT/$L.smoke.log" 2>&1
}

# A check log that passed: the browser JSON lines and the smoke's tally.
passed() {   # L
  $PY - "$OUT" "$1" <<'PY'
import json, re, sys
out, L = sys.argv[1:3]
def j(p):
    try:
        lines = [x for x in open(p, encoding="utf-8", errors="replace") if x.startswith("{")]
        return json.loads(lines[-1]).get("pass") is True
    except (OSError, ValueError, IndexError):
        return False
try:
    sm = re.search(r"^(\d+) passed, (\d+) failed", open(f"{out}/{L}.smoke.log", errors="replace").read(), re.M)
except OSError:
    sm = None
sys.exit(0 if j(f"{out}/{L}.viewer.json") and j(f"{out}/{L}.ui.json") and sm and sm.group(2) == "0" and int(sm.group(1)) > 0 else 1)
PY
}

# Is L's verdict all-PASS for the recipe this run just wrote (RESUME=1)?
verdict_current() {   # L
  [ -f "$OUT/$1.ui.json" ] || return 1
  $PY - "$BASE" "$1" "$W/recipes/$TAG/$1.json" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["LP_ROOT"])
from tools.langpack.gatelib import entry_verdict_ok
ok, _ = entry_verdict_ok(sys.argv[1], sys.argv[2], json.load(open(sys.argv[3])))
sys.exit(0 if ok else 1)
PY
}

# Each language's logs are stamped with the recipe they were made for (its
# md5 and uuid); compose_verdicts.py fails logs without this run's stamp, so
# logs a resumed run did not redo can never pass for a new recipe.
stamp() { $PY -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r["md5"], r["uuid"])' "$W/recipes/$TAG/$1.json"; }
free_port() { $PY -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }

# One entry's checks, in a job of its own: its port (GATE_JOBS > 1: a free one
# each), its kiwix-serve stopped when the job ends, its own temporary folder.
job_setup() {   # LABEL
  [ "$JOBS" = 1 ] || PORT=$(free_port)
  B=http://127.0.0.1:$PORT; SRV=
  CT=$CT0/$1; mkdir -p "$CT"
  trap 'unserve; rm -rf "${CT:?}"' EXIT
  trap 'exit 143' TERM INT HUP
}
base_job() {
  job_setup base
  rm -f "${OUT:?}"/base.*
  serve base "$BASE"; browser_checks "$BASE" base; unserve
  printf '%s\n' "$BID" > "$OUT/base.identity"
}
entry_job() {   # MENU-LINE L LIGHT
  local line=$1 L=$2 LIGHT=$3 P IDX IA M T TA
  job_setup "$L"
  rm -f "${OUT:?}/${L:?}".*
  stamp "$L" > "$OUT/$L.stamp"
  [ "$LIGHT" = 1 ] && cp "$CT0/$L.light" "$OUT/$L.light.json"
  mapfile -t P < <($PY -c 'import json,sys; [print(f) for f in json.loads(sys.argv[1])["pack_files"]]' "$line")
  # the entry's search pack (index_pack.py; recipe2 menu found it), if any
  IDX=$($PY -c 'import json,sys; print(json.loads(sys.argv[1]).get("index_file") or "")' "$line")
  IA=(); [ -n "$IDX" ] && IA=(--index "$IDX")
  M="$MD/m-$TAG-$L.zim"
  T=$($PY -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], ""))' "$TITLES" "$L"); TA=(); [ -n "$T" ] && TA=(--title "$T")
  wait_mem
  $N $PY "$TL/merge_zim.py" "$BASE" "${P[@]}" "${IA[@]}" -o "$M" --lang "$L" --region-langs "$RL" --region "$REGION" "${TA[@]}" \
     --language-meta "$LANGUAGE_META" > "$OUT/$L.merge.json" 2>&1
  IDX="$IDX" $N $PY - "$W/recipes/$TAG/$L.json" "$BASE" "$M" "${P[@]}" > "$OUT/$L.same" 2>&1 <<'PY'
import json, sys, os
sys.path.insert(0, os.environ["LP_ROOT"])
from tools.langpack.recipe2 import materialize
from tools.langpack.merge_zim import SEARCH, open_pack
r = json.load(open(sys.argv[1])); base, merged = sys.argv[2], sys.argv[3]
packs = {open_pack(p).lang: p for p in sys.argv[4:]}
if os.environ.get("IDX"):
    packs[SEARCH] = os.environ["IDX"]
tmp = merged + ".fromrecipe"
materialize(r, base, packs, tmp)
same = os.system(f"cmp -s '{tmp}' '{merged}'") == 0
os.remove(tmp); print("PASS" if same else "FAIL")
PY
  wait_mem
  $N $PY "$TL/verify_merge.py" "$M" "$BASE" "${P[@]}" "${IA[@]}" --lang "$L" --region-langs "$RL" --region "$REGION" "${TA[@]}" \
     --language-meta "$LANGUAGE_META" --queries "$SEARCH" > "$OUT/$L.verify.log" 2>&1
  wait_mem
  zimcheck_merged "$M" "$OUT/$L.zimcheck.log"
  if [ "$LIGHT" = 0 ]; then
    $N $PY "$LP_ROOT/cloud/route_cli.py" --zim "$M" --src "$ROUTE_SRC" --dst "$ROUTE_DST" --mode astar > "$OUT/$L.route.log" 2>&1
    $N $PY "$TL/search_gate.py" "$M" "$BASE" --lang "$L" > "$OUT/$L.search.json" 2>&1
  fi
  if [ -s "$M" ]; then serve "$L" "$M"; LIGHT=$LIGHT browser_checks "$M" "$L"; unserve; fi
  [ "$KEEP" = 1 ] || rm -f "$M" "$M.fromrecipe"
}

# The jobs: at most GATE_JOBS at a time; stopped with the run.
NRUN=0
launch() { "$@" & NRUN=$(( NRUN + 1 )); }
reap_one() { wait -n; NRUN=$(( NRUN - 1 )); }
CT0=$CT
# A job waits for its command (run under GNU timeout, which leads a process
# group of its own: a signal to the run's group misses it) before its trap can
# stop its kiwix-serve. So the jobs are signalled first (a signalled job starts
# no further command), then their commands, until every job has ended.
stop_jobs() {
  local j _
  kill $(jobs -p) 2>/dev/null
  for _ in $(seq 1 120); do
    [ -n "$(jobs -pr)" ] || return 0
    for j in $(jobs -pr); do kill $(ps -o pid= --ppid "$j") 2>/dev/null; done
    sleep 0.25
  done
}
trap 'trap - TERM INT HUP; stop_jobs; wait; exit 143' TERM INT HUP
trap 'rm -rf "${CT0:?}"' EXIT

# The base alone first. (A base patched in place keeps its name: RESUME
# trusts the last run's base checks only for the same size and mtime.)
BID=$(stat -L -c '%s %Y' "$BASE")
if [ "$RESUME" = 1 ] && [ "$(cat "$OUT/base.identity" 2>/dev/null)" = "$BID" ] && passed base; then
  echo "base: its checks passed in the last run (RESUME=1); not run again"
else
  launch base_job
fi

# (the menu on fd 3: nothing in the loop may eat it from stdin)
SHORT=0
while read -r line <&3; do
  L=$($PY -c 'import json,sys; print(json.loads(sys.argv[1])["L"])' "$line")
  if [ "$RESUME" = 1 ] && verdict_current "$L"; then
    echo "$L: its verdict passes for this recipe (RESUME=1); not run again"; continue
  fi
  # room for one more entry beside the running ones: else wait for one to end
  while [ $SHORT = 0 ] && [ $NRUN -gt 0 ] && { [ $NRUN -ge "$JOBS" ] || ! disk_ok $(( NRUN + 1 )); }; do reap_one; done
  if [ $SHORT = 1 ] || ! disk_ok $(( NRUN + 1 )); then
    [ $SHORT = 1 ] || echo "$L: not run, and neither is any later language: $MD has under $(( NEED_KB / 1048576 )) GB free" >&2
    SHORT=1
    rm -f "${OUT:?}/${L:?}".*          # no logs: its verdict FAILS
    continue
  fi
  # Light gate (RESUME=1): the last verdict passed for a recipe with the same
  # fingerprint (recipe2.fingerprint: everything but the viewer slots and the
  # UUID/MD5 they change, the merger's generated bytes included) and the
  # base's MD5 is not the one that verdict checked, i.e. the base was patched
  # in place (cloud/patch_viewer_inplace.py). Its checks that the viewer
  # cannot change (route, kiwix-serve, Kiwix search) carry over; recipe =
  # merge, verify, zimcheck (its link checks read the viewer's HTML) and the
  # browser checks run again.
  LIGHT=0
  if [ "$RESUME" = 1 ] && [ "${LIGHT_GATE:-1}" = 1 ] && $PY - "$BASE" "$L" "$W/recipes/$TAG/$L.json" > "$CT0/$L.light" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["LP_ROOT"])
from tools.langpack.gatelib import verdict_path
from tools.langpack.merge_zim import file_md5
base, L, rp = sys.argv[1:4]
r = json.load(open(rp))
try:
    v = json.load(open(verdict_path(base, L)))
except (OSError, ValueError):
    sys.exit(1)
ok = (v.get("all_pass") and r.get("fingerprint") and v.get("fingerprint") == r["fingerprint"]
      and v.get("base_md5") and v["base_md5"] != file_md5(base))
if not ok:
    sys.exit(1)
json.dump(v, sys.stdout)
PY
  then LIGHT=1; echo "$L: the base changed only in its viewer slots: light gate (route, kiwix, search carried)"; fi
  launch entry_job "$line" "$L" "$LIGHT"
done 3< "$OUT/menu.jsonl"
while [ $NRUN -gt 0 ]; do reap_one; done
$PY "$TL/compose_verdicts.py" "$OUT" "$BASE" "$LANGS" | tee "$OUT/summary.txt"
