#!/bin/bash
# The post-ship language step for one published map: its Wikipedia packs for
# the core languages, the gates, the packs on archive.org and the builder
# page's recipes, ready to publish (docs/language-pack-pipeline.md, "In
# production").
#
#   ops/langpack/ship_packs.sh REGION BASE.zim
#
# BASE is the map as published: archive.org item streetzim-<REGION> lists it
# with this MD5 (site_check.py checks). SHIP_BASE_ITEM and SHIP_PACK_ITEM
# name other items (a preview on test items: streetzim-langtest-<REGION> and
# streetzim-langtest-<REGION>-lang), SHIP_ITEM_TITLE the packs item's title. Steps, each logged with PDT times to
# $LANGPACK_WORK/logs/ship-<REGION>-<base>.log:
#   1. region_meta.py: the languages (core + the map's regionLanguages that
#      have a Wikipedia ZIM; a language without one is skipped and logged),
#      the packs they need, the merged files' titles.
#   2. prefetch.py: SQL dumps, title maps and Wikidata text for those packs
#      (network; the pack builds then run offline).
#   3. build_packs.sh for every pack language without a gated pack for this
#      base in $LANGPACK_WORK/packs/<base>/ (a rerun skips the ones done), then
#      search_packs.py: a search pack per distinct pack set (index_pack.py).
#   4. run_gates.sh, streamed (one merged file on disk at a time), RESUME=1:
#      an entry whose verdict already passes for its recipe is not run again.
#      The region file is tools/langpack/regions/<REGION>.json when there is
#      one, else region_json.py's.
#   5. archive.org: the packs the menu uses go to item streetzim-<REGION>-lang
#      (created on the first upload: mediatype data, collection
#      opensource_media like the bases, licence CC BY-SA 4.0 (Wikipedia's
#      text), one language value per pack language);
#      a pack already listed with its MD5 is not sent again, and a name the
#      item holds with another MD5 stops the run (a published recipe may use
#      that file). Not all listed yet: exit 6, run again later.
#   6. make_site.py into $LANGPACK_WORK/site/<REGION>/recipes/<REGION>/
#      (URLs "{proxy}/<item>/<file>": the page's proxy, web/drive/preview-config.js),
#      then, with LANGPACK_WEBSEED set (the build worker's origin, build-proxy/),
#      lang_torrents.py: a torrent per entry, webseeded by the worker, its
#      segment index and blobs, beside the recipes (the region's base
#      torrent, web/torrents/<REGION>.torrent, lends its piece hashes); an
#      entry whose torrent fails just has none, and if the tool itself fails
#      every torrent file is stripped; then site_check.py (archive.org
#      listings, the served bytes, the torrent files).
# Publishing those recipes on the site is a separate step:
# ops/langpack/publish_site.sh REGION, then the site's deploy.
#
# Exit: 0 recipes ready; 6 packs transferred, archive.org listing pending
# (run again later; nothing is redone); 2 another run holds the region's lock,
# or a language's pack build lock; 3 SHIP_SKIP_UPLOAD=1 stopped after the
# gates (a dry run: nothing published); 4 a map v1 gives no packs (a
# continent, or a base over SHIP_MAX_BASE_GB, 8: refused in step 1, before
# any download or build); 5 nothing publishable: every entry with a
# Wikipedia pack that passed its gates is over the per-entry budget
# (make_site.py's; each refusal is logged as REFUSED, before anything is
# sent), or make_site.py offers no entry;
# 1 anything else (the log says what).
#
# Env: ops/langpack/host-env.sh (sourced here; set values win). CORE (the
# core languages, comma-separated; default region_meta.py's), REGION_PACKS_MAX
# (a continent or multi-country map's most spoken languages every entry
# carries; default 3; a country map carries all its languages), IA (the
# archive.org CLI; default venv-linux's ia), UPLOAD_LOCK (the host's upload
# lock, default /storage/streetzim/.retrofit-upload.lock), SHIP_RESEND_AFTER_H
# (72), SHIP_SKIP_UPLOAD=1 (stop after
# the gates: a dry run), PACK_DATE (pack file names; default today),
# GATES_PORT (default a free port). Tests: SHIP_SKIP_ENV_CHECK=1, SHIP_PREFETCH
# and SHIP_RUN_GATES (stand-ins for prefetch.py and run_gates.sh), BUILDER
# (build_packs.sh's), SHIP_IA_FETCH= and SHIP_PROBE_URLS= (site_check.py
# reads $LANGPACK_WORK/ia-cache only and probes nothing), SHIP_LISTING_POLLS,
# SHIP_LISTING_POLL_SECS.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TL=$ROOT/tools/langpack
export LP_ROOT=$ROOT
. "$ROOT/ops/langpack/host-env.sh"
REGION=${1:?usage: ship_packs.sh REGION BASE.zim}
BASE=$(readlink -f "${2:?usage: ship_packs.sh REGION BASE.zim}")
PY=${PYTHON:-python3}
IA=${IA:-/storage/streetzim/venv-linux/bin/ia}
UPLOAD_LOCK=${UPLOAD_LOCK:-/storage/streetzim/.retrofit-upload.lock}
export PYTHONDONTWRITEBYTECODE=1
case "$REGION" in *[!a-z0-9-]*|"") echo "ship_packs: region id '$REGION': a-z 0-9 - only" >&2; exit 1 ;; esac
# Light and ultralight maps carry no Wikipedia: no packs for them (the
# German Swiss maps, switzerland-de*, are not excluded: their base is German).
case "$REGION" in *-light|*-light-*|*ultralight*)
  [ "${SHIP_ALLOW_LIGHT:-0}" = 1 ] || { echo "ship_packs: $REGION is a light map: no language packs (SHIP_ALLOW_LIGHT=1 to force)" >&2; exit 1; } ;;
esac
[ -s "$BASE" ] || { echo "ship_packs: no base $BASE" >&2; exit 1; }
if [ "${SHIP_SKIP_ENV_CHECK:-0}" != 1 ]; then
  langpack_env_check || { echo "ship_packs: fix the host setup first (ops/langpack/host-env.sh)" >&2; exit 1; }
fi
TAG=$(basename "$BASE" .zim)
W=$LANGPACK_WORK
PACKS=$W/packs/$TAG
SITE=$W/site/$REGION
ITEM=${SHIP_PACK_ITEM:-streetzim-$REGION-lang}
BASE_ITEM=${SHIP_BASE_ITEM:-streetzim-$REGION}
mkdir -p "$W/logs" "$W/locks" "$W/regions" "$PACKS"
LOG=$W/logs/ship-$REGION-$TAG.log
exec 9> "$W/locks/ship-$REGION.lock"
flock -n 9 || { echo "ship_packs: another run is shipping $REGION ($W/locks/ship-$REGION.lock)" >&2; exit 2; }
say() { echo "$(TZ=America/Los_Angeles date '+%F %T %Z') $*" | tee -a "$LOG"; }
LOW=(nice -n 19); command -v ionice > /dev/null && LOW=(ionice -c3 nice -n 19)
LIM=(); command -v prlimit > /dev/null && LIM=(prlimit --as=24000000000 --)
say "== ship_packs $REGION $BASE"

# 1. languages
CORE_ARG=(--region "$REGION"); [ -n "${CORE:-}" ] && CORE_ARG+=(--core "$CORE")
[ -n "${REGION_PACKS_MAX:-}" ] && CORE_ARG+=(--region-packs-max "$REGION_PACKS_MAX")
META=$("${LOW[@]}" "$PY" "$TL/region_meta.py" "$BASE" --wiki-src "$WIKI_SRC" --cache "$WP_CACHE" "${CORE_ARG[@]}") \
  || { say "region_meta.py failed"; exit 1; }
printf '%s\n' "$META" > "$W/regions/$TAG.meta.json"
mget() { "$PY" -c 'import json,sys; v=json.loads(sys.argv[1])[sys.argv[2]]; print(v if isinstance(v, str) else (",".join(v) if isinstance(v, list) else json.dumps(v, ensure_ascii=False)))' "$META" "$1"; }
LANGS=$(mget langs); RL=$(mget region_langs); PACK_LANGS=$(mget pack_langs); TITLE=$(mget region_title)
# v1: no packs for a continent, or for a base over SHIP_MAX_BASE_GB (its
# search packs alone are over the per-entry budget), before any work
TIER=$(mget tier); BASE_BYTES=$(stat -c %s "$BASE")
if [ "$TIER" = continent ] || "$PY" -c 'import sys; sys.exit(0 if int(sys.argv[1]) > float(sys.argv[2]) * 1e9 else 1)' \
     "$BASE_BYTES" "${SHIP_MAX_BASE_GB:-8}"; then
  say "$REGION: a ${TIER/null/unregistered} map, $(( BASE_BYTES / 1000000 )) MB: no language packs in v1 (continents and bases over ${SHIP_MAX_BASE_GB:-8} GB come after a measured design)"
  exit 4
fi
say "menu: $LANGS; region languages: ${RL:-none}; packs: $PACK_LANGS"
"$PY" -c 'import json,sys; [print("skipped %s: %s" % (d["lang"], d["why"])) for d in json.loads(sys.argv[1])["dropped"]]' "$META" \
  | while read -r l; do say "$l"; done
[ -n "$LANGS" ] || { say "no languages to offer"; exit 1; }

# 2. network first
if [ -n "$PACK_LANGS" ]; then
  say "prefetch: $PACK_LANGS"
  "${LOW[@]}" "${LIM[@]}" timeout 21600 "$PY" "${SHIP_PREFETCH:-$TL/prefetch.py}" "$BASE" --langs "$PACK_LANGS" --cache "$WP_CACHE" \
    >> "$LOG" 2>&1 || say "prefetch incomplete (see above); the packs say what they lack"
fi

# The region's name in each language is in the Wikidata cache only now.
META=$("${LOW[@]}" "$PY" "$TL/region_meta.py" "$BASE" --wiki-src "$WIKI_SRC" --cache "$WP_CACHE" "${CORE_ARG[@]}") \
  || { say "region_meta.py failed"; exit 1; }
printf '%s\n' "$META" > "$W/regions/$TAG.meta.json"
say "titles: $(mget titles)"

# 3. packs not yet built and gated for this base
TODO=$("$PY" - "$BASE" "$PACKS" "$REGION" "$PACK_LANGS" <<'PY'
import glob, json, os, sys
sys.path.insert(0, os.environ["LP_ROOT"])
from tools.langpack.gatelib import pack_gate_ok
from tools.langpack.merge_zim import base_lineage, open_pack
from tools.langpack.zimfmt import ZimReader
base, packs, region, langs = sys.argv[1:5]
lineage = base_lineage(base, ZimReader(base).header.uuid)
todo = []
for L in [x for x in langs.split(",") if x]:
    ok = False
    for p in sorted(glob.glob(os.path.join(packs, f"osm_{region}_wiki-{L}_*.zim"))):
        try:
            ok = pack_gate_ok(p)[0] and open_pack(p).base_uuid in lineage
        except Exception:  # noqa: BLE001 - an unreadable pack is not done
            ok = False
        if ok:
            break
    if not ok:
        todo.append(L)
print(" ".join(todo))
PY
) || { say "could not list the packs done"; exit 1; }
if [ -n "$TODO" ]; then
  say "build_packs: $TODO"
  "${LOW[@]}" "$TL/build_packs.sh" "$REGION" "$BASE" "$PACKS" "$TODO" >> "$LOG" 2>&1
  BRC=$?
  say "build_packs rc=$BRC"
  # 2: another build_packs.sh holds a language's lock (another ship_packs.sh
  # of the same base under another region id, or a hand run)
  [ $BRC = 2 ] && { say "another run is building some of these packs: stopping (run again later)"; exit 2; }
else
  say "packs: all gated for this base already"
fi

# 3b. search packs: Kiwix's indexes of the base's pages and each entry's
# packs (index_pack.py), one per distinct pack set; the gates require Kiwix
# to find pack articles
say "search packs"
"${LOW[@]}" "${LIM[@]}" timeout 43200 "$PY" "$TL/search_packs.py" "$BASE" "$PACKS" --region "$REGION" \
  --langs "$LANGS" --region-langs "$RL" --cache "$WP_CACHE" ${PACK_DATE:+--date "$PACK_DATE"} >> "$LOG" 2>&1 \
  || say "search_packs.py rc=$? (an entry without its search pack fails the search gate)"

# 4. gates
RJ=$TL/regions/$REGION.json
if [ ! -f "$RJ" ]; then
  RJ=$W/regions/$TAG.json
  "$PY" "$TL/region_json.py" "$BASE" -o "$RJ" >> "$LOG" 2>&1 || { say "no region file (region_json.py failed)"; exit 1; }
fi
PORT=${GATES_PORT:-$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}
say "gates: region file $RJ, port $PORT"
RESUME=1 LANGUAGE_META=map TITLES=$(mget titles) "${LOW[@]}" timeout 172800 \
  "${SHIP_RUN_GATES:-$TL/run_gates.sh}" "$BASE" "$PACKS" "$LANGS" "$RL" "$REGION" "$RJ" "$PORT" >> "$LOG" 2>&1
GRC=$?
SUMMARY=$W/gates/$TAG/summary.txt
[ -f "$SUMMARY" ] && tee -a "$LOG" < "$SUMMARY" > /dev/null
[ $GRC = 0 ] || { say "run_gates.sh rc=$GRC"; exit 1; }
PASSED=$("$PY" - "$BASE" "$LANGS" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["LP_ROOT"])
from tools.langpack.gatelib import verdict_path
base, langs = sys.argv[1:3]
ok = []
for L in langs.split(","):
    try:
        if json.load(open(verdict_path(base, L))).get("all_pass"):
            ok.append(L)
    except (OSError, ValueError):
        pass
print(",".join(ok))
PY
)
say "gates passed: ${PASSED:-none}"
[ -n "$PASSED" ] || { say "no entry passed its gates; nothing to publish"; exit 1; }
# the per-entry budget (make_site.py's), on the gated recipes: an entry over
# it is refused here, said out loud, and none of its packs is sent for it
BUDGET=$("$PY" - "$W/recipes/$TAG" "$PASSED" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["LP_ROOT"])
from tools.langpack.make_site import over_budget, touched_bytes
d, langs = sys.argv[1:3]
ok, wiki = [], 0
for L in langs.split(","):
    p = os.path.join(d, f"{L}.json")
    r = json.load(open(p))
    why = over_budget(os.path.getsize(p), sum(touched_bytes(r).values()))
    if why:
        print(f"REFUSED {L}: {why}")
    else:
        ok.append(L)
        wiki += any(k != "search" for k in r["packs"])
print("OK " + ",".join(ok))
print(f"WIKI {wiki}")
PY
) || { say "budget check failed"; exit 1; }
while read -r l; do case "$l" in REFUSED*) say "$l";; esac; done <<< "$BUDGET"
PASSED=$(sed -n 's/^OK //p' <<< "$BUDGET")
# an entry without a Wikipedia pack (the base's own language) is no reason to publish
[ "$(sed -n 's/^WIKI //p' <<< "$BUDGET")" != 0 ] || { say "every entry with a Wikipedia pack is over the per-entry budget: nothing to publish"; exit 5; }
if [ "${SHIP_SKIP_UPLOAD:-0}" = 1 ]; then say "SHIP_SKIP_UPLOAD=1: stopping before archive.org (exit 3: gated, nothing published)"; exit 3; fi

# 5. archive.org: the packs the passing entries use
mapfile -t FILES < <("$PY" - "$W/recipes/$TAG" "$PASSED" <<'PY'
import json, os, sys
d, langs = sys.argv[1:3]
files = set()
for L in langs.split(","):
    files.update(p["file"] for p in json.load(open(os.path.join(d, f"{L}.json")))["packs"].values())
print("\n".join(sorted(files)))
PY
)
# The item's listing, fetched once per check; each pack's MD5 computed once.
declare -A MD5
for f in "${FILES[@]}"; do [ -n "$f" ] && MD5[$f]=$(md5sum "$PACKS/$f" | cut -d' ' -f1); done
ITEM_META=
fetch_meta() { ITEM_META=$("$IA" metadata "$ITEM" 2>/dev/null) || ITEM_META='{}'; }
listed() { printf '%s' "$ITEM_META" | "$PY" "$ROOT/ops/cloud/archive_has_file.py" "$1" "${MD5[$1]}"; }
name_taken() {   # the item lists this name with another MD5
  printf '%s' "$ITEM_META" | "$PY" -c 'import json,sys
try:
    m = json.load(sys.stdin)
except ValueError:
    m = {}
fs = m.get("files") if isinstance(m, dict) else None
f = next((x for x in fs or [] if isinstance(x, dict) and x.get("name") == sys.argv[1]), None)
sys.exit(0 if f and str(f.get("md5", "")).lower() != sys.argv[2] else 1)' "$1" "${MD5[$1]}"
}
# one --metadata=language:<ISO 639-3> per pack language (archive.org keeps a list)
mapfile -t LANG_META < <("$PY" -c 'import sys; sys.path.insert(0, sys.argv[2]); from streetzim.languages import pack_iso639_3; [print("--metadata=language:" + pack_iso639_3(x)) for x in sys.argv[1].split(",") if x]' "$PACK_LANGS" "$ROOT")
# A pack sent before (its name and MD5 in $LANGPACK_WORK/uploads/<item>.tsv)
# is never sent again while archive.org has not listed it yet: `ia upload
# --checksum` does not skip a file while the item has a pending task, and a
# second copy stays in history/. After SHIP_RESEND_AFTER_H (72) hours
# unlisted, the run stops and says so; the operator looks at the item's
# tasks and deletes the record line to send it again.
SENT=$W/uploads/$ITEM.tsv
mkdir -p "$W/uploads"; touch "$SENT"
sent_at() { awk -F'\t' -v f="$1" -v m="$2" '$1 == f && $2 == m {t = $3} END {print t}' "$SENT"; }
SEND=()
fetch_meta
for f in "${FILES[@]}"; do
  [ -n "$f" ] || continue
  if listed "$f"; then continue; fi
  if name_taken "$f"; then
    say "archive.org $ITEM already holds $f with another MD5: not overwritten"
    exit 1
  fi
  t=$(sent_at "$f" "${MD5[$f]}")
  if [ -n "$t" ]; then
    if [ $(( $(date +%s) - t )) -gt $(( ${SHIP_RESEND_AFTER_H:-72} * 3600 )) ]; then
      say "$ITEM/$f was sent $(( ($(date +%s) - t) / 3600 )) h ago and is still not listed: check the item's tasks; delete its line in $SENT to send it again"
      exit 1
    fi
    say "$f: sent $(( ($(date +%s) - t) / 60 )) min ago, waiting for archive.org to list it (not sent again)"
    continue
  fi
  SEND+=("$PACKS/$f")
done
if [ ${#SEND[@]} -gt 0 ]; then
  say "upload to $ITEM: ${#SEND[@]} packs"
  # One pack per `ia upload`, recorded as soon as it is sent: a run that
  # fails part way never sends again what it sent before the failure (one
  # batch call recorded nothing then, so a rerun sent them all again).
  # Behind the host's upload lock, as every upload (MAINTAINING.md).
  for p in "${SEND[@]}"; do
    f=${p##*/}
    ( cd "$PACKS" && flock "$UPLOAD_LOCK" "${LOW[@]}" "$IA" upload "$ITEM" "$f" --retries 5 --checksum \
        --metadata="mediatype:data" --metadata="collection:opensource_media" \
        --metadata="licenseurl:https://creativecommons.org/licenses/by-sa/4.0/" \
        --metadata="title:${SHIP_ITEM_TITLE:-StreetZim $TITLE: Wikipedia language packs}" \
        "${LANG_META[@]}" \
        --metadata="description:Wikipedia packs for the StreetZim map of $TITLE, assembled into a map in your language by https://streetzim.web.app/build/" \
        >> "$LOG" 2>&1 ) || { say "ia upload of $f failed (the packs sent before it are recorded in $SENT)"; exit 1; }
    printf '%s\t%s\t%s\n' "$f" "${MD5[$f]}" "$(date +%s)" >> "$SENT"
  done
fi
# Every pack listed with its MD5, checked a round at a time.
LEFT=("${FILES[@]}")
for round in $(seq 1 "${SHIP_LISTING_POLLS:-30}"); do
  fetch_meta
  NOW=()
  for f in "${LEFT[@]}"; do [ -n "$f" ] && ! listed "$f" && NOW+=("$f"); done
  LEFT=("${NOW[@]}")
  [ ${#LEFT[@]} -eq 0 ] && break
  [ "$round" -lt "${SHIP_LISTING_POLLS:-30}" ] && sleep "${SHIP_LISTING_POLL_SECS:-20}"
done
if [ ${#LEFT[@]} -gt 0 ]; then
  for f in "${LEFT[@]}"; do say "PENDING: archive.org does not list $ITEM/$f with md5 ${MD5[$f]} yet"; done
  say "packs transferred, listing pending: run ship_packs.sh $REGION again later"; exit 6
fi
say "archive.org lists every pack"

# 6. site files, checked where they are built; they replace the region's
# previous ones only once site_check.py passed (sealed with checked.json,
# which publish_site.sh requires).
BF=$(basename "$BASE")
STG=$W/site/.$REGION.tmp
OUT=$STG/recipes/$REGION
rm -rf "$STG" && mkdir -p "$OUT"
"${LOW[@]}" "$PY" "$TL/make_site.py" "$BASE" --packs-dir "$PACKS" --region "$REGION" --region-title "$TITLE" \
  --region-langs "$RL" --langs "$PASSED" --titles "$(mget titles)" --language-meta map \
  --url-template "{proxy}/$ITEM/{file}" --recipe-prefix "recipes/$REGION/" \
  --base-download "https://archive.org/download/$BASE_ITEM/$BF" --base-url "{proxy}/$BASE_ITEM/$BF" \
  -o "$OUT" >> "$LOG" 2>&1 || { say "make_site.py failed"; exit 1; }
"$PY" -c 'import json,sys; [print("REFUSED %s: %s" % (r.get("lang") or r.get("pack"), r["why"])) for r in json.load(open(sys.argv[1]))]' \
  "$OUT/refused.json" | while read -r l; do say "$l"; done
MENU=$("$PY" -c 'import json,sys; print(",".join(m["lang"] for m in json.load(open(sys.argv[1]))["regions"][0]["menu"]))' "$OUT/index.json")
[ -n "$MENU" ] || { say "make_site.py offers no entry: nothing to publish ($STG kept)"; exit 5; }
if [ -n "${LANGPACK_WEBSEED:-}" ]; then
  BT=$ROOT/web/torrents/$REGION.torrent
  BTA=(); [ -f "$BT" ] && BTA=(--base-torrent "$BT")
  # the base's and the packs' archive.org metadata: their ETags go in the index
  mkdir -p "$W/ia-cache"
  if [ -n "${SHIP_IA_FETCH---ia-fetch}" ]; then
    for it in "$BASE_ITEM" "$ITEM"; do
      "$IA" metadata "$it" > "$W/ia-cache/ia-$it.json.tmp" 2>/dev/null && mv -f "$W/ia-cache/ia-$it.json.tmp" "$W/ia-cache/ia-$it.json" \
        || rm -f "$W/ia-cache/ia-$it.json.tmp"
    done
  fi
  TLIM=(); command -v prlimit > /dev/null && TLIM=(prlimit --as=24000000000 --)
  read -ra LTA <<< "${SHIP_LANG_TORRENTS_ARGS:-}"     # extra arguments (operators, tests)
  TTO=${SHIP_TORRENT_TIMEOUT:-7200}
  "${LOW[@]}" timeout "$TTO" "${TLIM[@]}" "$PY" "$TL/lang_torrents.py" "$OUT" --base "$BASE" \
    --packs-dir "$PACKS" --webseed "$LANGPACK_WEBSEED" --ia-cache "$W/ia-cache" "${BTA[@]}" "${LTA[@]}" >> "$LOG" 2>&1
  TRC=$?
  case $TRC in
    0) say "language torrents: $(ls "$OUT" | grep -c '\.torrent$') (webseed $LANGPACK_WEBSEED)" ;;
    3) say "WARNING: language torrents: some entries failed (the log says which); they are published without one" ;;
    124) say "WARNING: lang_torrents.py ran out of its ${TTO} s (SHIP_TORRENT_TIMEOUT): publishing WITHOUT torrents"
       TORRENTS_OFF="lang_torrents.py ran out of its ${TTO} s"
       "$PY" "$TL/lang_torrents.py" "$OUT" --strip >> "$LOG" 2>&1 || { say "could not strip the torrent files"; exit 1; } ;;
    *) say "WARNING: lang_torrents.py failed (rc $TRC; the log says why): publishing WITHOUT torrents"
       TORRENTS_OFF="lang_torrents.py failed (rc $TRC)"
       "$PY" "$TL/lang_torrents.py" "$OUT" --strip >> "$LOG" 2>&1 || { say "could not strip the torrent files"; exit 1; } ;;
  esac
fi
"${LOW[@]}" "$PY" "$TL/site_check.py" "$STG" --recipes-dir "$OUT" --base "$BASE" --packs-dir "$PACKS" \
  --ia-cache "$W/ia-cache" ${SHIP_IA_FETCH---ia-fetch} ${SHIP_PROBE_URLS---probe-urls} > "$STG.check" 2>&1
CRC=$?
cat "$STG.check" >> "$LOG"
if [ $CRC != 0 ] && "$PY" "$TL/lang_torrents.py" "$OUT" --only-torrent-failures "$STG.check"; then
  # only the torrent files failed: publish the recipes without them
  say "WARNING: site_check.py: only torrent checks failed; stripping the torrent files and checking again"
  TORRENTS_OFF="the torrent files failed site_check.py"
  "$PY" "$TL/lang_torrents.py" "$OUT" --strip >> "$LOG" 2>&1 || { say "could not strip the torrent files"; exit 1; }
  "${LOW[@]}" "$PY" "$TL/site_check.py" "$STG" --recipes-dir "$OUT" --base "$BASE" --packs-dir "$PACKS" \
    --ia-cache "$W/ia-cache" ${SHIP_IA_FETCH---ia-fetch} ${SHIP_PROBE_URLS---probe-urls} > "$STG.check" 2>&1
  CRC=$?
  cat "$STG.check" >> "$LOG"
fi
if [ $CRC != 0 ]; then
  say "site_check.py FAILED (the log says which check): nothing replaced, nothing to publish ($STG kept)"
  exit 1
fi
"$PY" "$TL/checked.py" write "$OUT" --summary "$(grep '^SUMMARY' "$STG.check" | tail -1)" || { say "could not seal $OUT"; exit 1; }
rm -f "$STG.check"
"$PY" "$TL/checked.py" swap "$STG" "$SITE" || { say "could not replace $SITE"; exit 1; }
rm -rf "$STG" || say "$SITE replaced; the previous files in $STG could not all be removed"
OUT=$SITE/recipes/$REGION
say "== ready: $OUT ($("$PY" -c 'import json,sys; print(",".join(m["lang"] for m in json.load(open(sys.argv[1]))["regions"][0]["menu"]))' "$OUT/index.json"))"
[ -n "${TORRENTS_OFF:-}" ] && say "   WARNING: no language torrents this time: ${TORRENTS_OFF} (the log says why)"
say "   publish: ops/langpack/publish_site.sh $REGION, then deploy the site"
exit 0
