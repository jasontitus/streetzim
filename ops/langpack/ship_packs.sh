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
#      (network; the pack builds then run offline). When it ends with every
#      language's dumps and title map in the cache (exit 0; or 1, or cut short
#      by the 6 h limit after prefetch.py's titles_done line, with only
#      Wikidata text incomplete: throttled), $LANGPACK_WORK/packs/<base>/prefetched
#      names those pack languages and the base file (size, mtime), and a later
#      run for that file skips this step for them (a rerun; or the queue's
#      prefetch ahead: SHIP_PREFETCH_ONLY=1 runs steps 1 and 2 only and exits 0,
#      so the Wikidata waits overlap another map's run); else (a failed dump
#      or title map, the 6 h limit before every title map was in) a later run
#      fetches again, and the Wikidata text already cached is not asked for
#      twice. Either way a run (not SHIP_PREFETCH_ONLY=1) then writes
#      packs/<base>/prefetch.ended naming the file: it takes the cache's lock no
#      more (pack_queue.sh, which removes the file as it starts a run, waits
#      for that).
#   3. build_packs.sh for every pack language without a gated pack for this
#      base in $LANGPACK_WORK/packs/<base>/ (a rerun skips the ones done; a
#      base from a viewer swap, with a <base>.parent.json naming it, first
#      gets there, as hard links, the parent's packs for the languages it has
#      none for yet and their search packs: they fit by lineage, so nothing is
#      built or sent again). A
#      language whose build ran out of its time (exit 124, build_packs.sh's
#      .timeout-<L>) and still has no pack is listed in
#      $LANGPACK_WORK/packs/<base>/retry-langs: the run goes on without it and
#      pack_queue.sh runs the region again later for it (no file: none).
#      Then the NVMe scratch (tools/langpack/nvme.py): when the shared volume
#      has room beside what the map builds may still write, this run's merged
#      files, temporary files and search-pack builds go to its own folder in
#      LANGPACK_SCRATCH, with a verified copy of the base the search packs and
#      gates read, reserved there while the run lives and removed before the
#      uploads; else on LANGPACK_WORK, one job at a time, as before. Then
#      search_packs.py: a search pack per distinct pack set (index_pack.py),
#      up to LANGPACK_SEARCH_JOBS (4) at a time.
#   4. run_gates.sh, streamed (each entry's merged file made, checked and
#      deleted), LANGPACK_GATE_JOBS (4) entries at a time on the NVMe (fewer
#      when its room or MemAvailable says so), RESUME=1: an entry whose
#      verdict already passes for its recipe is not run again.
#      The region file is tools/langpack/regions/<REGION>.json when there is
#      one, else region_json.py's.
#   5. archive.org: the packs the menu uses go to item streetzim-<REGION>-lang
#      (created on the first upload: mediatype data, collection
#      opensource_media like the bases, licence CC BY-SA 4.0 (Wikipedia's
#      text), one language value per pack language);
#      a pack already listed with its MD5 is not sent again, and a name the
#      item holds with another MD5 stops the run (a published recipe may use
#      that file); the listing must be read first (archive.org's metadata
#      unavailable: exit 8, nothing sent, run again later), and an upload
#      that fails (archive.org timing out) exits 8 too: the packs sent before
#      it are recorded, so the run again sends only the rest. Not all listed
#      yet: exit 6, run again later.
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
# (run again later; nothing is redone); 8 archive.org unreachable: the item's
# listing could not be read before the upload (nothing sent), or an upload
# failed (what was sent before it is not sent again): run again later; 2 another run holds the region's lock,
# or a language's pack build lock; 3 SHIP_SKIP_UPLOAD=1 stopped after the
# gates (a dry run: nothing published); 4 a map v1 gives no packs (a
# continent, or a base over SHIP_MAX_BASE_GB (8 GB): refused in step 1, before
# any download or build); 5 nothing publishable: every entry with a
# Wikipedia pack that passed its gates is over the per-entry budget
# (make_site.py's; each refusal is logged as REFUSED, before anything is
# sent), or make_site.py offers no entry; 7 nothing publishable YET: as 5,
# but with languages whose pack builds ran out of time (retry-langs; a region
# language's missing pack refuses every entry): run again later;
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
# GATES_PORT (default a free port), LANGPACK_SCRATCH (the NVMe scratch,
# /mnt/data/tilemaker/streetzim-packs; empty: none), LANGPACK_GATE_JOBS and
# LANGPACK_SEARCH_JOBS (4 each), LANGPACK_NVME_MIN_GB (100: what the shared
# volume keeps free, as parallel_rebuild.sh). I/O at best effort, lowest
# level (ionice -c2 -n7; CPU at nice 19): the HDD's scheduler (mq-deadline)
# serves the idle class only when the disk is idle, which a rebuild round's
# builds never leave it. SHIP_PREFETCH_ONLY=1: steps 1 and 2 only (step 2).
# Tests: SHIP_SKIP_ENV_CHECK=1, SHIP_PREFETCH
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
# best effort (the queue may have started this at the idle class)
LOW=(nice -n 19); command -v ionice > /dev/null && { ionice -c2 -n7 -p $$ 2> /dev/null; LOW=(ionice -c2 -n7 nice -n 19); }
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

# 2. network first (once per base file and pack languages: step 2 above)
PF_MARK=$PACKS/prefetched
PF_ID=$(printf '%s\n%s' "$PACK_LANGS" "$(stat -c '%s %.9Y' "$BASE")")
# prefetch.py's titles_done or summary line (its last JSON line naming "failed"): every language's dumps
# and title map are in
titles_ok() { "$PY" - "$1" <<'PY'
import json, sys
rows = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        rows.append(json.loads(line))
    except ValueError:
        pass
s = [r for r in rows if isinstance(r, dict) and "failed" in r]
sys.exit(0 if s and s[-1]["failed"] == [] else 1)
PY
}
if [ -n "$PACK_LANGS" ] && [ "$(head -n 2 "$PF_MARK" 2> /dev/null)" = "$PF_ID" ]; then
  say "prefetch: done already for $PACK_LANGS ($(TZ=America/Los_Angeles date -r "$PF_MARK" '+%F %T %Z'))"
elif [ -n "$PACK_LANGS" ]; then
  say "prefetch: $PACK_LANGS"
  # one prefetch at a time on a cache (ship_packs.sh runs side by side,
  # ops/langpack/pack_queue.sh): it downloads into and appends to shared files
  mkdir -p "$WP_CACHE"
  "${LOW[@]}" flock "$WP_CACHE/.prefetch.lock" "${LIM[@]}" timeout 21600 "$PY" "${SHIP_PREFETCH:-$TL/prefetch.py}" "$BASE" --langs "$PACK_LANGS" --cache "$WP_CACHE" \
    2>&1 | tee -a "$LOG" > "$PACKS/prefetch.out"
  rc=${PIPESTATUS[0]}
  if [ "$rc" = 0 ] || { { [ "$rc" = 1 ] || [ "$rc" = 124 ]; } && titles_ok "$PACKS/prefetch.out"; }; then
    [ "$rc" = 0 ] || say "prefetch: Wikidata text incomplete (throttled: see above); the packs say what they lack"
    printf '%s\n' "$PF_ID" > "$PF_MARK.tmp" && mv -f "$PF_MARK.tmp" "$PF_MARK"
  else
    rm -f "$PF_MARK"
    say "prefetch incomplete (exit $rc: see above): a later run for this base fetches again; the packs say what they lack"
  fi
fi
if [ "${SHIP_PREFETCH_ONLY:-0}" = 1 ]; then
  say "prefetch only (SHIP_PREFETCH_ONLY=1): done"
  exit 0
fi
stat -c '%s %.9Y' "$BASE" > "$PACKS/prefetch.ended.tmp" && mv -f "$PACKS/prefetch.ended.tmp" "$PACKS/prefetch.ended"

# The region's name in each language is in the Wikidata cache only now.
META=$("${LOW[@]}" "$PY" "$TL/region_meta.py" "$BASE" --wiki-src "$WIKI_SRC" --cache "$WP_CACHE" "${CORE_ARG[@]}") \
  || { say "region_meta.py failed"; exit 1; }
printf '%s\n' "$META" > "$W/regions/$TAG.meta.json"
say "titles: $(mget titles)"

# A base made from another by a viewer swap (tools/langpack/swap_base_viewer.py; its sidecar
# <base>.parent.json names the parent's file) first gets, as hard links with their sidecars and
# gates, the parent's gated packs for the languages it has no fitting pack for yet (the newest
# name of each), and the parent's search packs built on exactly those packs: only when the
# sidecar names this base (merge_zim.base_lineage checks its child UUID and MD5) and only packs
# built for a base of its lineage. They fit it, so nothing is built or sent again.
reuse_parent_packs() { "$PY" - "$BASE" "$PACKS" "$REGION" "$W/packs" "$TAG" <<'PY'
import glob, json, os, sys
sys.path.insert(0, os.environ["LP_ROOT"])
from tools.langpack.gatelib import pack_gate_ok
from tools.langpack.merge_zim import base_lineage, open_pack
from tools.langpack.zimfmt import ZimReader
base, packs, region, root, tag = sys.argv[1:6]
try:
    side = json.load(open(os.path.realpath(base) + ".parent.json"))
    parent = str(side.get("parent_file") or "")
    puuid = str(side.get("parent_uuid") or "").replace("-", "").lower()
except (OSError, ValueError, AttributeError):
    sys.exit(0)
ptag = parent[:-4] if parent.endswith(".zim") and "/" not in parent else ""
src = os.path.join(root, ptag)
if not ptag or ptag == tag or not os.path.isdir(src):
    sys.exit(0)
lineage = base_lineage(base, ZimReader(base).header.uuid)
if puuid not in lineage:
    print(f"not reused: the packs of {ptag} (its .parent.json does not name this base)")
    sys.exit(0)


def fit_lang(p):
    """The pack's language when it passes its gate and was built for this lineage, else None."""
    try:
        if pack_gate_ok(p)[0]:
            pk = open_pack(p)
            if pk.base_uuid in lineage:
                return pk.lang
    except Exception:  # noqa: BLE001 - an unreadable pack does not fit
        pass
    return None


def link(p):
    n = 0
    for x in (p, p + ".json", p + ".gate.json"):
        d = os.path.join(packs, os.path.basename(x))
        if os.path.isfile(x) and not os.path.lexists(d):
            os.link(x, d)
            n += 1
    return n


wiki = f"osm_{region}_wiki-"
have = {fit_lang(p) for p in glob.glob(os.path.join(packs, wiki + "*.zim"))} - {None}
langs, n, searches = [], 0, 0
for p in sorted(glob.glob(os.path.join(src, wiki + "*.zim")), reverse=True):     # the newest name first
    L = fit_lang(p)
    if L and L not in have:
        n += link(p)
        have.add(L)
        langs.append(L)
for p in sorted(glob.glob(os.path.join(src, f"osm_{region}_search-*.zim"))):
    try:
        s = json.load(open(p + ".json"))
        named = list(s.get("packs") or {})
        ok = (bool(named) and str(s.get("base_uuid", "")).replace("-", "").lower() in lineage
              and all("/" not in f and os.path.isfile(os.path.join(packs, f))
                      and os.path.samefile(os.path.join(packs, f), os.path.join(src, f)) for f in named)
              and pack_gate_ok(p)[0])
    except (OSError, ValueError, AttributeError, TypeError):
        ok = False
    if ok and (k := link(p)):
        n += k
        searches += 1
if n:
    print(f"reused {n} pack files of {ptag} (its viewer-swap parent; they fit by lineage): "
          f"packs {','.join(sorted(langs)) or '-'}, {searches} search packs")
PY
}
REUSED=$(reuse_parent_packs) || say "reusing the parent's packs failed: they are built"
[ -z "$REUSED" ] || say "$REUSED"

# 3. packs not yet built and gated for this base
todo_langs() { "$PY" - "$BASE" "$PACKS" "$REGION" "$PACK_LANGS" <<'PY'
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
}
TODO=$(todo_langs) || { say "could not list the packs done"; exit 1; }
if [ -n "$TODO" ]; then
  say "build_packs: $TODO"
  "${LOW[@]}" "$TL/build_packs.sh" "$REGION" "$BASE" "$PACKS" "$TODO" >> "$LOG" 2>&1
  BRC=$?
  say "build_packs rc=$BRC"
  # 2: another build_packs.sh holds a language's lock (another ship_packs.sh
  # of the same base under another region id, or a hand run)
  [ $BRC = 2 ] && { say "another run is building some of these packs: stopping (run again later)"; exit 2; }
  LEFT=$(todo_langs) || { say "could not list the packs done"; exit 1; }
  TIMED=""
  for L in $LEFT; do [ -e "$PACKS/.timeout-$L" ] && TIMED+="${TIMED:+ }$L"; done
  if [ -n "$TIMED" ]; then
    printf '%s\n' "$TIMED" > "$PACKS/retry-langs"
    say "ran out of time: $TIMED (no pack this run; listed in $PACKS/retry-langs: the queue runs $REGION again later for them)"
  else
    rm -f "$PACKS/retry-langs"
  fi
else
  say "packs: all gated for this base already"
  rm -f "$PACKS/retry-langs"
fi

# 3a. the NVMe scratch, when the shared volume has room (tools/langpack/nvme.py)
SCR=${LANGPACK_SCRATCH-/mnt/data/tilemaker/streetzim-packs}
GJ=1; SJ=1; RUN=; HOLD=; WBASE=$BASE; NV=()
release_scratch() {
  [ -n "$HOLD" ] && kill "$HOLD" 2> /dev/null && wait "$HOLD" 2> /dev/null
  HOLD=
  [ -n "$RUN" ] && rm -rf "${RUN:?}"
  RUN=
}
# The long steps run in the background and are waited for: a signal then
# stops the step at once (timeout's process group, which a signal to this
# run's group does not reach) and releases the scratch; a foreground step
# would hold the trap until it ended.
STEP=
run_step() {
  "$@" &
  STEP=$!
  wait "$STEP"
  local rc=$?
  STEP=
  return $rc
}
on_signal() {
  if [ -n "$STEP" ]; then
    kill -TERM -- -"$STEP" 2> /dev/null || kill -TERM "$STEP" 2> /dev/null
    wait "$STEP" 2> /dev/null
  fi
  exit 143
}
trap release_scratch EXIT
trap on_signal TERM INT HUP
if [ -n "$SCR" ] && mkdir -p "$SCR" 2> /dev/null; then
  "$PY" "$TL/nvme.py" sweep --scratch "$SCR" | while read -r l; do say "NVMe scratch: $l"; done
  PLAN=$("$PY" "$TL/nvme.py" plan --scratch "$SCR" --base "$BASE" --packs-dir "$PACKS" \
           --gate-jobs "${LANGPACK_GATE_JOBS:-4}" --search-jobs "${LANGPACK_SEARCH_JOBS:-4}" 2>> "$LOG") || PLAN='{"use": false, "why": "nvme.py plan failed (the log says why)"}'
  pget() { "$PY" -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], ""))' "$PLAN" "$1"; }
  if [ "$(pget use)" = True ]; then
    RUN=$SCR/run-$REGION-$$
    mkdir -p "$RUN/merged" "$RUN/tmp" "$RUN/search" \
      && printf '%s %s\n' $$ "$(sed 's/.*) //' /proc/$$/stat | cut -d' ' -f20)" > "$RUN/.owner" || RUN=
  fi
  if [ -n "$RUN" ]; then
    "$PY" "$TL/nvme.py" hold --run-dir "$RUN" --need "$(pget need)" --parent $$ >> "$LOG" 2>&1 &
    HOLD=$!
    say "NVMe scratch: $RUN, $(pget gate_jobs) gate and $(pget search_jobs) search jobs ($PLAN)"
    if WBASE=$("${LOW[@]}" "$PY" "$TL/nvme.py" stage "$BASE" "$RUN/base" 2>> "$LOG"); then
      GJ=$(pget gate_jobs); SJ=$(pget search_jobs)
      NV=(LANGPACK_MERGED="$RUN/merged" LANGPACK_TMP="$RUN/tmp" TMPDIR="$RUN/tmp" LANGPACK_DISK_MARGIN_GB="${LANGPACK_NVME_MIN_GB:-100}")
      say "base staged: $WBASE (size and MD5 checked)"
    else
      say "NVMe scratch: the base could not be staged (the log says why): working on $W, one job at a time"
      WBASE=$BASE; release_scratch
    fi
  else
    say "NVMe scratch: not used ($(pget why)): working on $W, one job at a time"
  fi
fi

# 3b. search packs: Kiwix's indexes of the base's pages and each entry's
# packs (index_pack.py), one per distinct pack set; the gates require Kiwix
# to find pack articles
say "search packs"
SW=(); [ -n "$RUN" ] && SW=(--work-dir "$RUN/search")
run_step env "${NV[@]}" "${LOW[@]}" "${LIM[@]}" timeout 43200 "$PY" "$TL/search_packs.py" "$WBASE" "$PACKS" --region "$REGION" \
  --langs "$LANGS" --region-langs "$RL" --cache "$WP_CACHE" ${PACK_DATE:+--date "$PACK_DATE"} --jobs "$SJ" "${SW[@]}" >> "$LOG" 2>&1 \
  || say "search_packs.py rc=$? (an entry without its search pack fails the search gate)"

# 4. gates
RJ=$TL/regions/$REGION.json
if [ ! -f "$RJ" ]; then
  RJ=$W/regions/$TAG.json
  "$PY" "$TL/region_json.py" "$BASE" -o "$RJ" >> "$LOG" 2>&1 || { say "no region file (region_json.py failed)"; exit 1; }
fi
PORT=${GATES_PORT:-$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}
say "gates: region file $RJ, port $PORT, $GJ at a time"
run_step env "${NV[@]}" GATE_JOBS="$GJ" RESUME=1 LANGUAGE_META=map TITLES="$(mget titles)" "${LOW[@]}" timeout 172800 \
  "${SHIP_RUN_GATES:-$TL/run_gates.sh}" "$WBASE" "$PACKS" "$LANGS" "$RL" "$REGION" "$RJ" "$PORT" >> "$LOG" 2>&1
GRC=$?
release_scratch                 # nothing reads the copy or the scratch after the gates
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
# nothing publishable while some packs ran out of time: not a refusal, a run to do again (exit 7)
nothing_yet() {   # MESSAGE: exit 7 when packs ran out of time this run, else 5
  if [ -s "$PACKS/retry-langs" ]; then
    say "$1, while $(tr '\n' ' ' < "$PACKS/retry-langs")ran out of time: run again later (exit 7)"; exit 7
  fi
  say "$1"; exit 5
}
[ "$(sed -n 's/^WIKI //p' <<< "$BUDGET")" != 0 ] || nothing_yet "every entry with a Wikipedia pack is over the per-entry budget: nothing to publish"
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
# The item's listing; a failed `ia metadata` returns 1 and leaves no listing
# (ITEM_META {}): what archive.org holds is then unknown, not empty.
fetch_meta() { ITEM_META=$("$IA" metadata "$ITEM" 2>/dev/null) && return 0; ITEM_META='{}'; return 1; }
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
# Without the listing, a name archive.org holds with another MD5 would look
# free and be sent over it: stop, and let the queue run the region again.
fetch_meta || { say "archive.org metadata unavailable: run again later (\`ia metadata $ITEM\` failed; exit 8)"; exit 8; }
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
        >> "$LOG" 2>&1 ) || { say "ia upload of $f failed (the packs sent before it are recorded in $SENT): run again later (exit 8)"; exit 8; }
    printf '%s\t%s\t%s\n' "$f" "${MD5[$f]}" "$(date +%s)" >> "$SENT"
  done
fi
# Every pack listed with its MD5, checked a round at a time.
LEFT=("${FILES[@]}")
for round in $(seq 1 "${SHIP_LISTING_POLLS:-30}"); do
  fetch_meta || true          # a failed fetch: nothing counts as listed this round
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
[ -n "$MENU" ] || nothing_yet "make_site.py offers no entry: nothing to publish ($STG kept)"
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
