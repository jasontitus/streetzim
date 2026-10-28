#!/bin/bash
# The language step after each map of the rebuild round: as the round ships
# a map (its latest row in rebuild-old.tsv, written after this queue first
# started, says `uploaded`; or `upload-pending` and archive.org now lists
# the file with its MD5), this runs ship_packs.sh for it, then
# publishes its recipes into the PREVIEW site and redeploys the preview
# channel, then puts them on the live builder page (/build/) and deploys the
# site (below: live).
#
#   ops/langpack/pack_queue.sh [--once]
#   ops/langpack/pack_queue.sh enqueue REGION ZIM   (a map by hand: ZIM, a file
#       in MAPS_DIR, is a candidate whatever the round's file says, until the
#       round writes a new row for REGION; works while the queue runs)
#
# First the maps of regions whose recipes are for another file (rebuilt maps:
# the builder page drops such a region until its packs are shipped again), on
# the live builder page (PROD_REPO's web/build/recipes/REGION/region.json) or
# else as last published here; then the others; each in the order the maps
# shipped; the list is read once a pass. A run that ends while the next map
# waits for room sends the pass back to the top of its list: a map whose
# retry time has come (passed over earlier in the pass, or its own run ended
# in it, e.g. archive.org's listing pending; or archive.org lists its
# upload-pending base now) takes the slot before the later ones (so does a run
# that ends while the pass reads on); a map is started at most twice in a pass, so retries that keep
# failing cannot hold up the rest (QUEUE_RETRY_IN_PASS=0: each map at most
# once a pass). Prefetch ahead (QUEUE_PREFETCH_AHEAD=1, the default): the
# network step of ship_packs.sh (Wikipedia dumps, Wikidata labels, whose
# throttling can take hours) runs for the next maps beside the runs, one at a
# time (ship_packs.sh SHIP_PREFETCH_ONLY=1, in the list's order, for the first
# map the pass found eligible whose packs/<base>/prefetched does not name its
# file; with the memory a second run needs while the round builds or a run
# goes, and not while a run going may still prefetch itself: until its
# packs/<base>/prefetch.ended names its file); a map starts once its prefetch is done (its run
# then skips the step), and a later map whose prefetch is done goes before
# one still prefetching; a map whose prefetch ended twice without the mark
# (a failed dump, the 6 h limit) starts once no prefetch goes (none starts
# while it waits, or while a run going lacks its mark), and its run fetches
# itself; while a map waits for room the pass looks one map further ahead
# per wait (so the maps after it are prefetched meanwhile); at the end of the
# list the pass waits for a prefetch or a run to end, or QUEUE_POLL_SECS. One ship_packs.sh run at a time while the round builds (the disk is
# saturated; QUEUE_ROUND_JOBS more, each past the first under the memory and
# disk rules below), and
# only while the host has QUEUE_MIN_MEM_GB (24) of memory available and no
# memory pressure; up to QUEUE_JOBS (2) once no round driver runs, a second
# run under the same memory rule and with room on LANGPACK_WORK's disk for
# its merged files (2 x the base + QUEUE_DISK_MARGIN_GB, 40). The round runs
# while a pid file of QUEUE_ROUND_PIDFILES names a live process whose command
# line has the given text, or a process holds one of QUEUE_ROUND_LOCKS (read
# from /proc/locks: the round's locks are never taken here). Each run holds
# one merged file at a time; ship_packs.sh serializes its network step (the
# shared Wikipedia cache), uploads go one at a time under the host's upload
# lock, and publishing and deploying are done one at a time here.
# Per map:
#   - eligible: not a light/ultralight map, its base on disk, at most
#     QUEUE_MAX_BASE_GB (8; also passed to ship_packs.sh as SHIP_MAX_BASE_GB),
#     multilingual tiles (map-config tileLanguages). A continent is refused by
#     ship_packs.sh itself (exit 4).
#   - ops/langpack/ship_packs.sh REGION BASE from a clean environment (HOME,
#     USER, LOGNAME, PATH, LANG, LC_ALL, SHELL; nothing else of this shell's:
#     no SHIP_* knob or test stand-in left over from a test run), with
#     production's items (SHIP_BASE_ITEM=streetzim-<region>,
#     SHIP_PACK_ITEM=streetzim-<region>-lang), LANGPACK_WORK, WP_CACHE,
#     LANGPACK_WEBSEED and SHIP_MAX_BASE_GB; host-env.sh gives it the rest.
#     At nice 10 and best-effort I/O, lowest level (the HDD's mq-deadline
#     serves the idle class only when the disk is idle, which a round's builds
#     never leave it). Exit 0: published; but when ship_packs.sh lists
#     languages whose pack builds ran out of time
#     ($LANGPACK_WORK/packs/<base>/retry-langs), published without them,
#     `published-partial`, and run again after QUEUE_RETRY_SECS for them (the
#     packs done are not redone), at most QUEUE_PARTIAL_RETRIES (3) times, then
#     recorded as published with a warning; 7 (nothing publishable yet:
#     packs ran out of time, e.g. a region language's, which every entry
#     needs): run again after QUEUE_RETRY_SECS within the same
#     QUEUE_PARTIAL_RETRIES, then a loud failure; 6 (archive.org listing pending):
#     run again after QUEUE_RETRY_SECS; 2 (a lock held), 8 (archive.org
#     unreachable: its metadata, or an upload), or a run killed or ended without an
#     exit code: again after QUEUE_RETRY_SECS, a loud failure after
#     QUEUE_MAX_AGAIN (8) in a row; 4 and 5 (v1 gives this map
#     no packs, or nothing is publishable): logged, skipped for this base;
#     anything else: a loud failure. A failure is not retried, in this run or
#     after a restart, unless the queue starts with QUEUE_RETRY_FAILED=1.
#   - published: the preview clone (PREVIEW_DIR, a clone of PROD_REPO moved
#     to its current commit first; made with QUEUE_SEED's recipes already in
#     it, the Swiss preview the channel shows now, which production's
#     Switzerland replaces when it ships) gets the region (publish_site.sh), then
#     `firebase hosting:channel:deploy $PREVIEW_CHANNEL --expires 30d
#     --project $FIREBASE_PROJECT` from it. A failed deploy is tried again
#     at the next pass (nothing is shipped again for it).
#   - live (QUEUE_LIVE=1, the default unless QUEUE_FORCE_WORK=1): the region
#     is marked live-pending-<region>. Once its run ends it goes into
#     PROD_REPO's web/ (publish_site.sh) if the site-deploy lock is free at
#     once, else at the end of the pass, when one that failed before is
#     tried again too,
#     then the site's pages are built (web/generate.py) and deployed (firebase
#     deploy --only hosting): generate.py --deploy in two steps, as after an
#     upload. So its map card offers "Build in another language". All of it
#     runs under the site-deploy lock (PROD_REPO/tmp/.site-deploy.lock, which
#     upload_validated.sh takes for its deploy; QUEUE_SITE_LOCK_WAIT, 3600 s);
#     the steps keep the lock's descriptor, so a restarted queue does not free
#     it while they run. Pages that cannot be built put the builder page's
#     index back as it was, without the regions just published (a deploy that
#     skips generate.py then offers none of them unchecked); a failed deploy
#     leaves the built pages, to be deployed again. A region that fails QUEUE_MAX_AGAIN times in a
#     row is a loud failure, kept as live-failed-<region> until a restart with
#     QUEUE_RETRY_FAILED=1. A run published without some languages
#     (published-partial) does not replace what the live site already offers
#     for the same base.
# Refused at the start (exit 3): a PREVIEW_DIR that is, holds or sits in
# PROD_REPO or /storage/streetzim; PREVIEW_CHANNEL empty or `live`; unless
# QUEUE_FORCE_WORK=1 (tests), a LANGPACK_WORK, WP_CACHE, LANGPACK_WEBSEED,
# ROUND_TSV, MAPS_DIR or PROD_REPO other than production's, or QUEUE_SHIP,
# QUEUE_PUBLISH, QUEUE_LIVE_PUBLISH, QUEUE_LIVE_BUILD or QUEUE_DIR set.
# Resumable: a map published for the same base (UUID and file MD5) is not
# shipped again; a new or rewritten file for the region is. The queue's
# start (QUEUE_DIR/since) is kept, so a restart still ignores what shipped
# before the first start. Runs going when the queue stopped (runs/) are
# counted after a restart and finished here when they end.
#
# Files, all in QUEUE_DIR ($LANGPACK_WORK/pack-queue): lock, since, queue.log
# (PDT), status.tsv (region, state, zim, base key: uuid/md5 or uuid,
# updated, retry after, note), published.tsv (region, uuid, md5, zim,
# time, recipe scheme), enqueued.tsv (region, zim, epoch, the round file's line count then), runs/<region> (pid, start time,
# zim, base key, recipe scheme; none: 1), deploy-pending, live-pending-<region> (its failed tries),
# live-failed-<region>, prefetch (the prefetch ahead going: pid, start time, region, zim; a restart
# counts it while it goes), prefetch-<region>.out, stop (a file: nothing new starts, the
# running ones finish, then the queue ends; remove it before starting
# again), ship-<region>.out. The round's files are only read (ROUND_TSV).
#
# Env (production defaults): ROUND_TSV (/storage/streetzim/rebuild-old.tsv),
# MAPS_DIR (/storage/streetzim), PROD_REPO (/storage/streetzim), PREVIEW_DIR
# (/storage/experiments/build-preview/streetzim), PREVIEW_CHANNEL
# (swiss-languages), FIREBASE_PROJECT (streetzim), FIREBASE (firebase),
# LANGPACK_WEBSEED (https://streetzim-build-proxy.tiltastech.workers.dev),
# QUEUE_POLL_SECS (300), QUEUE_RETRY_SECS (1800), QUEUE_MAX_AGAIN (8), QUEUE_PARTIAL_RETRIES (3),
# QUEUE_RETRY_FAILED (0), QUEUE_MAX_BASE_GB (8),
# QUEUE_JOBS (2), QUEUE_MIN_MEM_GB (24), QUEUE_DISK_MARGIN_GB (40), QUEUE_RETRY_IN_PASS (1),
# QUEUE_PREFETCH_AHEAD (1), QUEUE_ROUND_JOBS (1: runs at once while the round builds, each
# past the first under the memory and disk rules; QUEUE_JOBS still the most),
# QUEUE_SINCE (first run only: count maps shipped since this date, e.g. the
# round's start, instead of since now), QUEUE_SEED (REGION=SITE_DIR ...: site
# folders, SITE_DIR/recipes/REGION sealed by ship_packs.sh, published into a
# new preview clone; default the Swiss test site), QUEUE_ROUND_PIDFILES
# (FILE:TEXT ..., FILE may be a glob; default the driver's
# /storage/streetzim/.rebuild-old.pid and the parallel runner's
# rebuild-par/pids/*.pid, TEXT rebuild_old_regions.sh), QUEUE_ROUND_LOCKS
# (default rebuild-par/run.lock and sz-tests/rebuild-all/run.lock under
# /storage/streetzim), IA (the archive.org CLI, for upload-pending rows).
# Tests: QUEUE_PRESSURE_FILE (read in place of /proc/pressure/memory), QUEUE_SHIP and QUEUE_PUBLISH (stand-ins for ship_packs.sh and
# publish_site.sh), QUEUE_LIVE_PUBLISH and QUEUE_LIVE_BUILD (for the live
# step: publish_site.sh and a shell command in place of web/generate.py),
# QUEUE_DIR, QUEUE_FORCE_WORK=1.
# Exit: 0; 2 another queue holds the lock; 3 refused (above); 1 (--once) a
# map failed in this run; enqueue: 1 a bad region or file.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PROD_WORK=/storage/streetzim/langpack-work
PROD_WEBSEED=https://streetzim-build-proxy.tiltastech.workers.dev
REFUSED=0
refuse() { echo "pack_queue: refused: $*" >&2; REFUSED=1; }
same_dir() { [ "$(realpath -m "$1")" = "$(realpath -m "$2")" ]; }
covers() { [ "$1" = "$2" ] || [ "$1" = / ] || case "$2" in "$1"/*) true ;; *) false ;; esac; }  # folder $1 is or holds $2
# production's files, webseed and scripts only: an env.sh or a test's settings left in this shell
# must not reach production
if [ "${QUEUE_FORCE_WORK:-0}" != 1 ]; then
  for v in ROUND_TSV=/storage/streetzim/rebuild-old.tsv MAPS_DIR=/storage/streetzim PROD_REPO=/storage/streetzim; do
    n=${v%%=*} d=${v#*=}
    [ -z "${!n:-}" ] || same_dir "${!n}" "$d" || refuse "$n=${!n} is not production's $d (QUEUE_FORCE_WORK=1 allows it)"
  done
  for v in QUEUE_SHIP QUEUE_PUBLISH QUEUE_LIVE_PUBLISH QUEUE_LIVE_BUILD QUEUE_DIR; do   # (QUEUE_DIR: a second queue beside production's)
    [ -z "${!v:-}" ] || refuse "$v=${!v}: a test's setting (QUEUE_FORCE_WORK=1 allows it)"
  done
  [ -z "${LANGPACK_WORK:-}" ] || same_dir "$LANGPACK_WORK" "$PROD_WORK" \
    || refuse "LANGPACK_WORK=$LANGPACK_WORK is not production's $PROD_WORK (QUEUE_FORCE_WORK=1 allows it)"
  [ -z "${WP_CACHE:-}" ] || same_dir "$WP_CACHE" "$PROD_WORK/wpcache" \
    || refuse "WP_CACHE=$WP_CACHE is not production's $PROD_WORK/wpcache (QUEUE_FORCE_WORK=1 allows it)"
  [ -z "${LANGPACK_WEBSEED:-}" ] || [ "$LANGPACK_WEBSEED" = "$PROD_WEBSEED" ] \
    || refuse "LANGPACK_WEBSEED=$LANGPACK_WEBSEED is not production's $PROD_WEBSEED (QUEUE_FORCE_WORK=1 allows it)"
fi
. "$ROOT/ops/langpack/host-env.sh"
WEBSEED=${LANGPACK_WEBSEED:-$PROD_WEBSEED}
ROUND_TSV=${ROUND_TSV:-/storage/streetzim/rebuild-old.tsv}
MAPS_DIR=${MAPS_DIR:-/storage/streetzim}
PROD_REPO=${PROD_REPO:-/storage/streetzim}
PREVIEW_DIR=${PREVIEW_DIR:-/storage/experiments/build-preview/streetzim}
CHANNEL=${PREVIEW_CHANNEL-swiss-languages}
PROJECT=${FIREBASE_PROJECT:-streetzim}
FIREBASE=${FIREBASE:-firebase}
LIVE=${QUEUE_LIVE:-$([ "${QUEUE_FORCE_WORK:-0}" = 1 ] && echo 0 || echo 1)}
SITE_LOCK=$PROD_REPO/tmp/.site-deploy.lock
SITE_LOCK_WAIT=${QUEUE_SITE_LOCK_WAIT:-3600}
# the preview clone is never production, the channel never the live site
for p in "$PROD_REPO" /storage/streetzim; do
  pv=$(realpath -m "$PREVIEW_DIR") pr=$(realpath -m "$p")
  if covers "$pr" "$pv" || covers "$pv" "$pr"; then
    refuse "PREVIEW_DIR=$PREVIEW_DIR ($pv) is, holds or sits in $p ($pr): the preview clone is never production"
  fi
done
case "${CHANNEL,,}" in ''|live) refuse "PREVIEW_CHANNEL='$CHANNEL': a preview channel only, never the live site" ;; esac
[ $REFUSED = 0 ] || exit 3
SHIP=${QUEUE_SHIP:-$ROOT/ops/langpack/ship_packs.sh}
POLL=${QUEUE_POLL_SECS:-300}
RETRY=${QUEUE_RETRY_SECS:-1800}
MAX_AGAIN=${QUEUE_MAX_AGAIN:-8}
# The recipes' scheme: a map published under an older one ships again (ship_packs.sh redoes
# only what changed). 2: each version's search pack is indexed in its own language (2026-10-23).
SCHEME=2
PARTIAL_MAX=${QUEUE_PARTIAL_RETRIES:-3}
RETRY_FAILED=${QUEUE_RETRY_FAILED:-0}
MAXGB=${QUEUE_MAX_BASE_GB:-8}
JOBS=${QUEUE_JOBS:-2}
ROUND_JOBS=${QUEUE_ROUND_JOBS:-1}
[[ $ROUND_JOBS =~ ^[0-9]+$ ]] || ROUND_JOBS=1
IN_PASS=${QUEUE_RETRY_IN_PASS:-1}
AHEAD=${QUEUE_PREFETCH_AHEAD:-1}
MIN_MEM_GB=${QUEUE_MIN_MEM_GB:-24}
DISK_MARGIN_GB=${QUEUE_DISK_MARGIN_GB:-40}
PY=${PYTHON:-python3}
IA=${IA:-/storage/streetzim/venv-linux/bin/ia}
SEED=${QUEUE_SEED-switzerland=/storage/experiments/lang-torrents/swiss-site2}
ROUND_PIDFILES=${QUEUE_ROUND_PIDFILES-/storage/streetzim/.rebuild-old.pid:rebuild_old_regions.sh /storage/streetzim/rebuild-par/pids/*.pid:rebuild_old_regions.sh}
ROUND_LOCKS=${QUEUE_ROUND_LOCKS-/storage/streetzim/rebuild-par/run.lock /storage/streetzim/sz-tests/rebuild-all/run.lock}
Q=${QUEUE_DIR:-$LANGPACK_WORK/pack-queue}
mkdir -p "$Q/runs" || exit 1
LOG=$Q/queue.log STATUS=$Q/status.tsv DONE=$Q/published.tsv ENQ=$Q/enqueued.tsv
say() { echo "$(TZ=America/Los_Angeles date '+%F %T %Z') $*" >> "$LOG"; }
loud() { say "FAILED: $*"; echo "pack_queue: FAILED: $*" >&2; }
warn() { say "WARNING: $*"; echo "pack_queue: WARNING: $*" >&2; }

if [ "${1:-}" = enqueue ]; then   # no lock: one short line appended, read at the next pass
  rid=${2:-} zim=${3:-}
  [[ $rid =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "pack_queue: enqueue REGION ZIM: '$rid' is not a region id" >&2; exit 1; }
  case $zim in ''|*/*) echo "pack_queue: enqueue REGION ZIM: ZIM is a file name in $MAPS_DIR, not '$zim'" >&2; exit 1 ;; esac
  [ -s "$MAPS_DIR/$zim" ] || { echo "pack_queue: enqueue: $MAPS_DIR/$zim is not on disk" >&2; exit 1; }
  n=$(wc -l 2>/dev/null < "$ROUND_TSV") || n=0
  printf '%s\t%s\t%s\t%s\n' "$rid" "$zim" "$(date +%s)" "$n" >> "$ENQ" || exit 1
  say "$rid: enqueued by hand: $zim"; echo "pack_queue: $rid enqueued ($zim); the queue takes it at its next pass"
  exit 0
fi
case "${1:-}" in
  '') ONCE=0 ;;
  --once) ONCE=1 ;;
  *) echo "usage: pack_queue.sh [--once] | enqueue REGION ZIM" >&2; exit 1 ;;
esac
# fd 9 holds the lock: every child that may outlive a killed queue (sleeps, md5sum and the subshell
# around it, the base and listing reads, firebase, the runs) is started without it, so a restart is
# not locked out (a subshell drops it with exec 9>&-: a function call's 9>&- keeps a copy to restore);
# -w for the brief ones
exec 9> "$Q/lock"
flock -w 10 9 || { echo "pack_queue: another queue holds $Q/lock" >&2; exit 2; }
# the start: now, or QUEUE_SINCE (a date, e.g. the round's start) on the first run
if [ ! -s "$Q/since" ]; then
  if [ -n "${QUEUE_SINCE:-}" ]; then date -d "$QUEUE_SINCE" +%s > "$Q/since" || { echo "pack_queue: QUEUE_SINCE '$QUEUE_SINCE' is not a date" >&2; exit 1; }
  else date +%s > "$Q/since"; fi
fi
SINCE=$(cat "$Q/since")
touch "$STATUS" "$DONE"
declare -A FAILED_NOW=() RUN_PID=() RUN_START=() RUN_ZIM=() RUN_KEY=() RUN_SCHEME=() PF_TRIES=() PF_WANT=() PF_AHEAD=() PF_NOPARENT=()
ANY_FAILED=0 STOP_SAID= PF_PID= PF_RID= PF_ZIM= PF_START= PF_SCAN= PF_ENDS=0 list=()
# what a ship_packs.sh run starts from: the login basics, then production's settings
SHIP_ENV=()
for v in HOME USER LOGNAME PATH LANG LC_ALL SHELL; do [ -n "${!v+x}" ] && SHIP_ENV+=("$v=${!v}"); done
SHIP_ENV+=("LANGPACK_WORK=$LANGPACK_WORK" "WP_CACHE=$WP_CACHE" "LANGPACK_WEBSEED=$WEBSEED" "SHIP_MAX_BASE_GB=$MAXGB")

# region state zim key [retry-after] [note]: one line per region, replaced atomically
set_state() {
  local t
  t=$(mktemp "$Q/.status.XXXXXX") || return 1
  awk -F'\t' -v r="$1" '$1 != r' "$STATUS" > "$t"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$(TZ=America/Los_Angeles date '+%F %T %Z')" "${5:--}" "${6:-}" >> "$t"
  mv -f "$t" "$STATUS"
}
state_of() { awk -F'\t' -v r="$1" -v k="$2" '$1 == r && $4 == k {print $2 "\t" $6}' "$STATUS"; }

# region <TAB> zim <TAB> epoch <TAB> status: the maps whose latest row, written since the start, is
# `uploaded` or `upload-pending` (archive.org's listing to be checked), or enqueued by hand
# after that row was written (`enqueued`); first those whose region's recipes are for another
# file (header), each in that order
candidates() {
  "$PY" - "$ROUND_TSV" "$SINCE" "$ENQ" "$DONE" "$PROD_REPO/web/build/recipes" <<'PY'
import datetime, json, os, sys
tsv, since, enq, done, recipes = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]

def lines(p):
    try:
        return open(p, encoding="utf-8").read().splitlines()
    except OSError:
        return []

last = {}
for i, line in enumerate(lines(tsv)):
    f = line.split("\t")
    if i == 0 or len(f) < 4 or not f[0] or f[0].startswith("#"):
        continue
    try:
        last[f[0]] = (datetime.datetime.fromisoformat(f[3]).timestamp(), f[2], f[1], i)
    except ValueError:
        last[f[0]] = (0.0, f[2], "unreadable", i)
rows = {rid: v for rid, v in last.items() if v[2] in ("uploaded", "upload-pending") and v[0] >= since}
for line in lines(enq):  # an enqueue wins over the rows the round's file had then, not over a later one
    f = line.split("\t")
    if len(f) == 4 and f[2].isdigit() and f[3].isdigit() and last.get(f[0], (0, "", "", -1))[3] < int(f[3]):
        rows[f[0]] = (float(f[2]), f[1], "enqueued", -1)
published = {}  # region -> the file of its last published.tsv row
for line in lines(done):
    f = line.split("\t")
    if len(f) > 3 and f[0]:
        published[f[0]] = f[3]

def rebuilt(rid, z):  # are the region's recipes, live or else published here, for another file?
    try:
        with open(os.path.join(recipes, rid, "region.json"), encoding="utf-8") as fh:
            old = json.load(fh)["base"]["file"]
    except (OSError, ValueError, KeyError, TypeError):
        old = published.get(rid)
    return old is not None and old != z

for _, t, rid, z, st in sorted((not rebuilt(rid, v[1]), v[0], rid, v[1], v[2]) for rid, v in rows.items()):
    print(f"{rid}\t{z}\t{int(t)}\t{st}")
PY
}

# BASE -> "uuid tileLanguages-count"
baseinfo() {
  "$PY" - "$ROOT" "$1" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from tools.langpack.merge_zim import KEY_MAPCFG, blob_of
from tools.langpack.zimfmt import ZimReader
z = ZimReader(sys.argv[2])
try:
    cfg = json.loads(blob_of(z, KEY_MAPCFG) or b"{}")
except ValueError:
    cfg = {}
tl = cfg.get("tileLanguages") if isinstance(cfg, dict) else None
print(z.header.uuid.hex(), len(tl) if isinstance(tl, list) else 0)
PY
}

# The local file's MD5, computed once per file (name, inode, size, mtime in ns).
file_md5() {
  local f=$1 k t
  k=$Q/md5/$(basename "$f").$(stat -c %i.%s "$f").$(stat -c %.9Y "$f")
  t=$k.$BASHPID.tmp                     # its own: a killed queue's md5 of this base may still be going
  # best effort, as the runs: behind the round's builds the HDD served the idle class a trickle (hours for 5 GB)
  [ -s "$k" ] || { mkdir -p "$Q/md5" && ionice -c2 -n7 nice -n19 md5sum "$f" 9>&- | cut -d' ' -f1 > "$t" && mv -f "$t" "$k"; } || { rm -f "$t"; return 1; }
  cat "$k"
}
# Does archive.org list the region's file with the local file's MD5? (an upload-pending row)
listed() {  # region base md5
  timeout 300 "$IA" metadata "streetzim-$1" 2>/dev/null 9>&- | "$PY" "$ROOT/ops/cloud/archive_has_file.py" "$(basename "$2")" "$3" 9>&-
}

# QUEUE_SEED's site folders into a new preview clone (what the channel shows already)
seed_preview() {
  local item rid dir w
  for item in $SEED; do
    rid=${item%%=*}; dir=${item#*=}
    [ -d "$PREVIEW_DIR/web/build/recipes/$rid" ] && continue
    [ -d "$dir/recipes/$rid" ] || { loud "seed $rid: no $dir/recipes/$rid"; continue; }
    w=$Q/seed/$rid
    rm -rf "$w" && mkdir -p "$w/site" && ln -s "$dir" "$w/site/$rid" || continue
    if LANGPACK_WORK=$w bash "${QUEUE_PUBLISH:-$PREVIEW_DIR/ops/langpack/publish_site.sh}" "$rid" "$PREVIEW_DIR/web" >> "$LOG" 2>&1 9>&-; then
      say "seeded the preview with $rid from $dir"
    else loud "seed $rid: publish_site.sh refused $dir/recipes/$rid"; fi
  done
}

# the preview clone at production's current commit (web/build/recipes is ignored by git: kept)
preview_update() {
  if [ ! -d "$PREVIEW_DIR/.git" ]; then
    mkdir -p "$(dirname "$PREVIEW_DIR")" && git clone -q "$PROD_REPO" "$PREVIEW_DIR" 9>&- || return 1
    say "preview clone made: $PREVIEW_DIR"
    seed_preview
  fi
  local want have
  want=$(git -C "$PROD_REPO" rev-parse HEAD) || return 1
  have=$(git -C "$PREVIEW_DIR" rev-parse HEAD 2>/dev/null)
  if [ "$want" != "$have" ]; then
    # The channel deploy's predeploy (scripts/sync-drive-viewer.sh) rewrites
    # tracked files in the clone (web/drive/build-info.js): a plain checkout
    # refuses once production changed one of them. -f drops such changes;
    # ignored files (the published recipes in web/build/recipes) are kept.
    local dirty
    dirty=$(exec 9>&-; git -C "$PREVIEW_DIR" status --porcelain --untracked-files=no 2>/dev/null | cut -c4- | tr '\n' ' ')
    git -C "$PREVIEW_DIR" fetch -q "$PROD_REPO" HEAD 9>&- && git -C "$PREVIEW_DIR" checkout -q -f --detach "$want" 9>&- || return 1
    say "preview clone moved to $(git -C "$PREVIEW_DIR" rev-parse --short HEAD)${dirty:+ (local changes dropped: ${dirty% })}"
  fi
}

deploy() {
  say "deploy: firebase hosting:channel:deploy $CHANNEL --expires 30d --project $PROJECT (from $PREVIEW_DIR)"
  if (cd "$PREVIEW_DIR" && timeout 3600 "$FIREBASE" hosting:channel:deploy "$CHANNEL" --expires 30d --project "$PROJECT") >> "$LOG" 2>&1 9>&-; then
    rm -f "$Q/deploy-pending"; say "deployed the preview channel $CHANNEL"
    # what was published and waited for a deploy is live on the channel now
    local t; t=$(mktemp "$Q/.status.XXXXXX") && \
      awk -F'\t' -v OFS='\t' '$2 == "published-not-deployed" {$2 = "published"; $7 = "deployed later"} {print}' "$STATUS" > "$t" && mv -f "$t" "$STATUS"
    return 0
  fi
  touch "$Q/deploy-pending"; loud "the preview deploy failed (tried again at the next pass)"; return 1
}

publish() {  # region
  preview_update || { loud "$1: could not bring $PREVIEW_DIR to $PROD_REPO's commit"; return 1; }
  local pub=${QUEUE_PUBLISH:-$PREVIEW_DIR/ops/langpack/publish_site.sh}
  if ! LANGPACK_WORK=$LANGPACK_WORK bash "$pub" "$1" "$PREVIEW_DIR/web" >> "$LOG" 2>&1 9>&-; then
    loud "$1: publish_site.sh into $PREVIEW_DIR/web failed"; return 1
  fi
  say "$1: published into $PREVIEW_DIR/web/build/recipes"
}

# The live site (header: live).
live_mark() {  # region: a run that ended is a fresh start (no failed try yet)
  [ "$LIVE" = 1 ] || return 0
  rm -f "$Q/live-failed-$1"
  echo 0 > "$Q/live-pending-$1"
}
# Does the live site already offer this region's recipes for this very base?
same_base_live() {  # region
  "$PY" - "$PROD_REPO/web/build/recipes/$1/region.json" "$LANGPACK_WORK/site/$1/recipes/$1/region.json" <<'PY'
import json, sys
try:
    a, b = (json.load(open(p, encoding="utf-8"))["base"] for p in sys.argv[1:3])
    sys.exit(0 if a.get("md5") and a.get("md5") == b.get("md5") else 1)
except (OSError, ValueError, KeyError, TypeError, AttributeError):
    sys.exit(1)
PY
}
live_count() {  # region: one more failed try in a row
  local n=0
  read -r n < "$Q/live-pending-$1" 2>/dev/null || n=0
  n=$(( ${n:-0} + 1 ))
  if [ "$n" -ge "$MAX_AGAIN" ]; then
    mv -f "$Q/live-pending-$1" "$Q/live-failed-$1"
    loud "$1: not on the live site after $n tries in a row (see $LOG): left until a restart with QUEUE_RETRY_FAILED=1"
  else
    echo "$n" > "$Q/live-pending-$1"
  fi
}
# The builder page's index as it was before (BEFORE), without REGIONs.
unlist() {  # BEFORE INDEX REGION...
  "$PY" - "$@" <<'PY'
import json, os, sys, tempfile
before, idx, drop = sys.argv[1], sys.argv[2], set(sys.argv[3:])
try:
    with open(before, encoding="utf-8") as fh:
        d = json.load(fh)
except (OSError, ValueError):
    d = {"format": "streetzim-build-index/1", "regions": []}
d["regions"] = [r for r in d.get("regions", []) if isinstance(r, dict) and r.get("id") not in drop]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(idx), prefix="index.json.", suffix=".tmp")
os.fchmod(fd, 0o644)                      # a site file: mkstemp's 0600 otherwise
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=1)
os.replace(tmp, idx)
PY
}
# [new] (once a run ends): only the regions marked since (no failed try yet),
# and only if the site-deploy lock is free at once: a deploy holding it does
# not hold up the next ship; else every pending one not tried in this pass
# already (LIVE_TRIED, emptied at each pass's start), waiting for the lock.
declare -A LIVE_TRIED=()
live_all() {  # [new]
  [ "$LIVE" = 1 ] || return 0
  local f rid n pending=() ok=() why= idx=$PROD_REPO/web/build/recipes/index.json before=$Q/index.json.before
  for f in "$Q"/live-pending-*; do
    [ -e "$f" ] || continue
    rid=${f##*/live-pending-}
    n=0; read -r n < "$f" 2>/dev/null || n=0
    if [ "${1:-}" = new ]; then [ "${n:-0}" = 0 ] || continue
    else [ -z "${LIVE_TRIED[$rid]:-}" ] || continue; fi
    pending+=("$rid")
  done
  [ ${#pending[@]} -gt 0 ] || return 0
  mkdir -p "${SITE_LOCK%/*}"
  exec 7>> "$SITE_LOCK"
  if [ "${1:-}" = new ]; then
    flock -n 7 || { exec 7>&-; say "the site-deploy lock is busy: ${pending[*]} go live at the end of the pass"; return 0; }
  elif ! flock -w "$SITE_LOCK_WAIT" 7 9>&-; then
    exec 7>&-
    loud "the site-deploy lock $SITE_LOCK stayed busy for $SITE_LOCK_WAIT s: ${pending[*]} wait for the next pass"
    return 1
  fi
  for rid in "${pending[@]}"; do LIVE_TRIED[$rid]=1; done
  rm -f "$before"; [ ! -f "$idx" ] || cp -p "$idx" "$before"
  local pub=${QUEUE_LIVE_PUBLISH:-$PROD_REPO/ops/langpack/publish_site.sh}
  # the steps keep fd 7 (the lock lasts while they run), never fd 9 (the queue's)
  for rid in "${pending[@]}"; do
    if LANGPACK_WORK=$LANGPACK_WORK bash "$pub" "$rid" "$PROD_REPO/web" >> "$LOG" 2>&1 9>&-; then ok+=("$rid")
    else loud "$rid: publish_site.sh into $PROD_REPO/web failed (tried again at the next pass)"; live_count "$rid"; fi
  done
  if [ ${#ok[@]} -gt 0 ]; then
    if [ -n "${QUEUE_LIVE_BUILD:-}" ]; then
      (cd "$PROD_REPO" && timeout 1800 bash -c "$QUEUE_LIVE_BUILD") >> "$LOG" 2>&1 9>&- || why=build
    else
      (cd "$PROD_REPO" && timeout 1800 "$PY" web/generate.py) >> "$LOG" 2>&1 9>&- || why=build
    fi
    # (with the publishes, under the hour upload_validated.sh waits for the lock)
    [ -n "$why" ] || (cd "$PROD_REPO" && timeout 1500 "$FIREBASE" deploy --only hosting) >> "$LOG" 2>&1 9>&- || why=deploy
    case $why in
      '') for rid in "${ok[@]}"; do rm -f "$Q/live-pending-$rid"; say "$rid: live on the site's builder page (/build/)"; done ;;
      build)
        loud "the site's pages could not be built (${ok[*]}): the builder page's index is put back without them; tried again at the next pass"
        unlist "$before" "$idx" "${ok[@]}" || loud "could not put back $idx"
        for rid in "${ok[@]}"; do live_count "$rid"; done ;;
      deploy)
        loud "the site's deploy failed (${ok[*]}): its pages are built; deployed again at the next pass"
        for rid in "${ok[@]}"; do live_count "$rid"; done ;;
    esac
  fi
  rm -f "$before"
  exec 7>&-
}

# Is the map to be shipped now? Records why not. Sets KEY (base uuid/md5).
eligible() {  # region zim round-status
  local rid=$1 zim=$2 base=$MAPS_DIR/$2 uuid ntile md5 st state retry gb
  KEY=
  [ -n "${RUN_PID[$rid]:-}" ] && return 1                     # shipping now
  case "$rid" in *-light|*-light-*|*ultralight*) [ -n "$(state_of "$rid" -)" ] || { set_state "$rid" ineligible-light "$zim" - - "a light map carries no Wikipedia"; say "$rid: a light map: no packs"; }; return 1 ;; esac
  if [ ! -s "$base" ]; then
    [ "$(state_of "$rid" - | cut -f1)" = missing ] || { loud "$rid: $base is not on disk"; set_state "$rid" missing "$zim" - - "not in $MAPS_DIR"; }
    ANY_FAILED=1; return 1
  fi
  read -r uuid ntile < <(exec 9>&-; baseinfo "$base") || true
  if [ -z "${uuid:-}" ]; then loud "$rid: could not read $base"; set_state "$rid" failed "$zim" - - "unreadable"; ANY_FAILED=1; return 1; fi
  # whatever the file's bytes: the size and the tiles (logged once per base)
  gb=$("$PY" -c 'import sys; print(f"{int(sys.argv[1]) / 1e9:.2f}")' "$(stat -c %s "$base")")
  if [ "$("$PY" -c 'import sys; print(int(int(sys.argv[1]) > float(sys.argv[2]) * 1e9))' "$(stat -c %s "$base")" "$MAXGB")" = 1 ]; then
    [ "$(state_of "$rid" "$uuid" | cut -f1)" = ineligible-size ] && return 1
    set_state "$rid" ineligible-size "$zim" "$uuid" - "$gb GB > $MAXGB GB"
    say "$rid: $zim is $gb GB, over the $MAXGB GB limit: no language packs in v1"; return 1
  fi
  if [ "${ntile:-0}" = 0 ]; then
    [ "$(state_of "$rid" "$uuid" | cut -f1)" = ineligible-no-ml-tiles ] && return 1
    set_state "$rid" ineligible-no-ml-tiles "$zim" "$uuid" - "no tileLanguages"; say "$rid: $zim has no multilingual tiles: no packs"; return 1
  fi
  md5=$(exec 9>&-; file_md5 "$base") || { loud "$rid: could not read $base for its MD5"; ANY_FAILED=1; return 1; }
  KEY=$uuid/$md5
  # published for this base already, under this recipe scheme; under an older one: shipped again
  # (ship_packs.sh redoes only what the scheme changes)
  grep -qP "^\Q$rid\E\t$uuid\t$md5\t[^\t]*\t[^\t]*\t$SCHEME$" "$DONE" && return 1
  if grep -qP "^\Q$rid\E\t$uuid\t$md5\t" "$DONE"; then
    case "$(state_of "$rid" "$KEY" | cut -f1)" in
      published*) say "$rid: published under an older recipe scheme: shipped again for scheme $SCHEME (only the entries whose recipes change are redone)" ;;
    esac
  fi
  st=$(state_of "$rid" "$KEY"); state=${st%%$'\t'*}; retry=${st#*$'\t'}
  case "$state" in
    refused-*) return 1 ;;
    failed*) [ "$RETRY_FAILED" = 1 ] && [ -z "${FAILED_NOW[$rid]:-}" ] || return 1 ;;
    pending|published-partial) [ "$retry" != - ] && [ "$(date +%s)" -lt "$retry" ] && return 1 ;;
  esac
  if [ "${3:-uploaded}" = upload-pending ] && ! listed "$rid" "$base" "$md5"; then
    [ "$state" = waiting-listing ] || { set_state "$rid" waiting-listing "$zim" "$KEY" - "upload-pending: archive.org does not list it yet"
                                        say "$rid: upload-pending; waiting until archive.org lists $zim with its MD5"; }
    return 1
  fi
  return 0
}

# Does the round build now? Its drivers' pid files (a live process with the
# given text in its command line: a reused pid is not it) and its locks (held,
# as /proc/locks shows: never taken here). Sets ROUND_WHY.
lock_held() {  # file
  local ino dev
  read -r ino dev < <(stat -L -c '%i %d' "$1" 2>/dev/null) || return 1
  awk -v k="$(printf '%02x:%02x:%s' $(( (dev >> 8) & 0xfff )) $(( (dev & 0xff) | ((dev >> 12) & 0xfff00) )) "$ino")" \
    '{for (i = 1; i <= NF; i++) if ($i == k) {f = 1; exit}} END {exit !f}' /proc/locks
}
round_running() {
  local item f pid c
  ROUND_WHY=
  for item in $ROUND_PIDFILES; do
    for f in ${item%:*}; do
      pid=$(head -c 32 "$f" 2>/dev/null | tr -dc 0-9)
      [ -n "$pid" ] && c=$(tr '\0' ' ' 2>/dev/null < "/proc/$pid/cmdline") || continue
      case "$c" in *"${item##*:}"*) ROUND_WHY="the round builds (pid $pid in $f)"; return 0 ;; esac
    done
  done
  for f in $ROUND_LOCKS; do
    lock_held "$f" && { ROUND_WHY="the round builds ($f is held)"; return 0; }
  done
  return 1
}
# Room for one more run beside those going? Sets WHY when not.
room_for() {  # base
  local n=${#RUN_PID[@]} rnd=0 avail full need free r
  WHY=
  round_running && rnd=1
  [ "$n" -lt "$ROUND_JOBS" ] || [ $rnd = 0 ] || { WHY="$( [ "$ROUND_JOBS" = 1 ] && echo 'one run at a time' || echo "$n runs going (QUEUE_ROUND_JOBS=$ROUND_JOBS)") while $ROUND_WHY"; return 1; }
  [ "$n" -lt "$JOBS" ] || { WHY="$n runs going (QUEUE_JOBS=$JOBS)"; return 1; }
  [ "$n" = 0 ] && [ $rnd = 0 ] && return 0                   # the first run while no round builds
  avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  [ "${avail:-0}" -ge $(( MIN_MEM_GB * 1048576 )) ] \
    || { WHY="$(( ${avail:-0} / 1048576 )) GB of memory available, under QUEUE_MIN_MEM_GB=$MIN_MEM_GB${ROUND_WHY:+ while $ROUND_WHY}"; return 1; }
  full=$(awk '/^full/ {sub("avg10=", "", $2); print int($2)}' "${QUEUE_PRESSURE_FILE:-/proc/pressure/memory}" 2>/dev/null)
  [ "${full:-0}" -lt 5 ] || { WHY="memory pressure (full avg10=$full)"; return 1; }
  [ "$n" = 0 ] && return 0                                   # the first run: ship_packs.sh minds the disk itself
  need=$(( $(stat -c %s "$1") / 1024 * 2 + DISK_MARGIN_GB * 1048576 ))
  for r in "${!RUN_ZIM[@]}"; do need=$(( need + $(stat -c %s "$MAPS_DIR/${RUN_ZIM[$r]}" 2>/dev/null || echo 0) / 1024 * 2 )); done
  free=$(df -Pk "$LANGPACK_WORK" | awk 'NR == 2 {print $4}')
  [ "${free:-0}" -ge "$need" ] || { WHY="$(( ${free:-0} / 1048576 )) GB free on $LANGPACK_WORK's disk, under the $(( need / 1048576 )) GB the runs need"; return 1; }
}

starttime() {  # pid -> its start time (/proc/PID/stat field 22); fails for a gone or zombie process
  local s; s=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
  s=${s##*) }; set -- $s
  [ "$1" != Z ] && echo "${20}"
}
alive() {  # region: is its run going? (the same process: pid and start time)
  local s; s=$(starttime "${RUN_PID[$1]}") || return 1
  [ -z "${RUN_START[$1]}" ] || [ "$s" = "${RUN_START[$1]}" ]
}
stopping() {
  [ -e "$Q/stop" ] || return 1
  [ -n "$STOP_SAID" ] || { STOP_SAID=1; say "stop file: starting nothing new; ${#RUN_PID[@]} running finish first"; }
}

start_ship() {  # region zim key
  local rid=$1 zim=$2 base=$MAPS_DIR/$2 pid
  set_state "$rid" shipping "$zim" "$3"
  # an earlier run's mark of its step 2's end is not this run's (pf_tick)
  rm -f "$Q/rc-$rid" "$LANGPACK_WORK/packs/${zim%.zim}/prefetch.ended"
  say "$rid: ship_packs.sh $rid $base (runs going: $(( ${#RUN_PID[@]} + 1 )))"
  ( cd "$ROOT" && env -i "${SHIP_ENV[@]}" SHIP_PACK_ITEM="streetzim-$rid-lang" SHIP_BASE_ITEM="streetzim-$rid" \
      ionice -c2 -n7 nice -n10 bash "$SHIP" "$rid" "$base" >> "$Q/ship-$rid.out" 2>&1 < /dev/null
    echo "$? $SCHEME" > "$Q/rc-$rid" ) 9>&- &
  pid=$!
  RUN_PID[$rid]=$pid; RUN_START[$rid]=$(starttime "$pid"); RUN_ZIM[$rid]=$zim; RUN_KEY[$rid]=$3; RUN_SCHEME[$rid]=$SCHEME
  # the recipe scheme the run ships under: a queue restarted with a newer one records this one
  echo "$pid ${RUN_START[$rid]:--} $zim $3 $SCHEME" > "$Q/runs/$rid.tmp" && mv -f "$Q/runs/$rid.tmp" "$Q/runs/$rid"
}

# Exit 2 or 8, a killed run or one without an exit code: again after RETRY, a loud
# failure after MAX_AGAIN of them in a row for the same base, naming each cause
# and how often (again-<region>: "<key> <count>", then one cause per line).
again() {  # region zim key why
  local n=0 k c whys=
  [ -s "$Q/again-$1" ] && read -r k c < "$Q/again-$1" && [ "$k" = "$3" ] && n=$c
  [ "$n" -gt 0 ] && whys=$(tail -n +2 "$Q/again-$1")
  whys=$(printf '%s\n%s\n' "$whys" "$4" | sed '/^$/d')
  n=$(( n + 1 ))
  if [ "$n" -ge "$MAX_AGAIN" ]; then
    rm -f "$Q/again-$1"
    local all
    all=$(printf '%s\n' "$whys" | sort | uniq -c | sort -rn | sed 's/^ *\([0-9]*\) \(.*\)$/\2 x\1/' | paste -sd ';' - | sed 's/;/; /g')
    set_state "$1" failed-again "$2" "$3" - "$n times in a row: $all"; FAILED_NOW[$1]=1; ANY_FAILED=1
    loud "$1: $n times in a row: $all (see $Q/ship-$1.out): not tried again until a restart with QUEUE_RETRY_FAILED=1"
  else
    printf '%s %s\n%s\n' "$3" "$n" "$whys" > "$Q/again-$1"
    set_state "$1" pending "$2" "$3" "$(( $(date +%s) + RETRY ))" "$4 ($n of $MAX_AGAIN)"
    say "$1: $4: again after $RETRY s ($n of $MAX_AGAIN in a row)"
  fi
}

# A run that ended: its outcome (publishing and deploying one at a time, here).
finish() {  # region
  local rid=$1 zim=${RUN_ZIM[$1]} key=${RUN_KEY[$1]} rc= scheme=
  wait "${RUN_PID[$rid]}" 2>/dev/null
  # "<exit code> <scheme>"; a run started by a queue of before the schemes wrote the code alone: scheme 1
  [ -e "$Q/rc-$rid" ] && read -r rc scheme < "$Q/rc-$rid"
  scheme=${scheme:-${RUN_SCHEME[$rid]:-1}}
  unset "RUN_PID[$rid]" "RUN_START[$rid]" "RUN_ZIM[$rid]" "RUN_KEY[$rid]" "RUN_SCHEME[$rid]"
  case $rc in 2|8|''|129|1[3-5][0-9]) ;; *) rm -f "$Q/again-$rid" ;; esac
  case $rc in
    0)
      say "$rid: packs shipped"
      if publish "$rid"; then
        local missing n=0 k c
        missing=$(tr -s ' \n' ' ' 2>/dev/null < "$LANGPACK_WORK/packs/${zim%.zim}/retry-langs" | sed 's/^ //; s/ $//')
        [ -s "$Q/partial-$rid" ] && read -r k c < "$Q/partial-$rid" && [ "$k" = "$key" ] && n=$c
        if [ -n "$missing" ] && [ "$n" -lt "$PARTIAL_MAX" ]; then
          echo "$key $(( n + 1 ))" > "$Q/partial-$rid"
          set_state "$rid" published-partial "$zim" "$key" "$(( $(date +%s) + RETRY ))" "without $missing ($(( n + 1 )) of $PARTIAL_MAX)"
          say "$rid: published without $missing (their pack builds ran out of time): ship_packs.sh runs again after $RETRY s for them ($(( n + 1 )) of $PARTIAL_MAX)"
          deploy || true
          same_base_live "$rid" || live_mark "$rid"
        else
          rm -f "$Q/partial-$rid"
          [ -z "$missing" ] || warn "$rid: published without $missing: their pack builds ran out of time $PARTIAL_MAX times"
          printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$rid" "${key%/*}" "${key#*/}" "$zim" "$(TZ=America/Los_Angeles date '+%F %T %Z')" "$scheme" >> "$DONE"
          set_state "$rid" published "$zim" "$key" - "${missing:+without $missing}"
          deploy || set_state "$rid" published-not-deployed "$zim" "$key" - "deploy pending"
          live_mark "$rid"
        fi
      else
        set_state "$rid" failed-publish "$zim" "$key" - "see queue.log"; FAILED_NOW[$rid]=1; ANY_FAILED=1
      fi ;;
    7)
      local missing n=0 k c
      missing=$(tr -s ' \n' ' ' 2>/dev/null < "$LANGPACK_WORK/packs/${zim%.zim}/retry-langs" | sed 's/^ //; s/ $//')
      [ -s "$Q/partial-$rid" ] && read -r k c < "$Q/partial-$rid" && [ "$k" = "$key" ] && n=$c
      if [ "$n" -lt "$PARTIAL_MAX" ]; then
        echo "$key $(( n + 1 ))" > "$Q/partial-$rid"
        set_state "$rid" pending "$zim" "$key" "$(( $(date +%s) + RETRY ))" "ship_packs rc=7: ${missing:-packs} ran out of time ($(( n + 1 )) of $PARTIAL_MAX)"
        say "$rid: nothing publishable yet (${missing:-some packs} ran out of time): ship_packs.sh runs again after $RETRY s ($(( n + 1 )) of $PARTIAL_MAX)"
      else
        rm -f "$Q/partial-$rid"
        set_state "$rid" failed-timeout "$zim" "$key" - "${missing:-packs} ran out of time $PARTIAL_MAX times"; FAILED_NOW[$rid]=1; ANY_FAILED=1
        loud "$rid: no packs: ${missing:-some packs} ran out of time $PARTIAL_MAX times in a row (see $Q/ship-$rid.out); QUEUE_RETRY_FAILED=1 on a restart tries again"
      fi ;;
    6)
      set_state "$rid" pending "$zim" "$key" "$(( $(date +%s) + RETRY ))" "ship_packs rc=6"
      say "$rid: ship_packs rc=6 (archive.org listing pending): again after $RETRY s" ;;
    2) again "$rid" "$zim" "$key" "ship_packs rc=2 (a lock held)" ;;
    8) again "$rid" "$zim" "$key" "ship_packs rc=8 (archive.org unreachable: its listing or an upload)" ;;
    '') again "$rid" "$zim" "$key" "the run ended without an exit code (the queue or the host stopped)" ;;
    129|1[3-5][0-9]) again "$rid" "$zim" "$key" "ship_packs.sh was killed (rc=$rc)" ;;
    4|5)
      set_state "$rid" "refused-$rc" "$zim" "$key" - "ship_packs rc=$rc"
      say "$rid: ship_packs rc=$rc ($([ "$rc" = 4 ] && echo 'v1 gives this map no packs' || echo 'nothing publishable')): skipped" ;;
    *)
      set_state "$rid" "failed" "$zim" "$key" - "ship_packs rc=$rc"; FAILED_NOW[$rid]=1; ANY_FAILED=1
      loud "$rid: ship_packs.sh rc=$rc (see $Q/ship-$rid.out and its log under $LANGPACK_WORK/logs)" ;;
  esac
  rm -f "$Q/rc-$rid" "$Q/runs/$rid"
}

# Finish the runs that ended; with "all", wait for every one.
# Prefetch ahead (header): one at a time, beside the runs.
pf_marked() {  # zim: does packs/<base>/prefetched name this very file? (ship_packs.sh step 2)
  local id
  id=$(stat -L -c '%s %.9Y' "$MAPS_DIR/$1" 2> /dev/null) || return 1
  # languages added since (packs/<base>/prefetch.added, ship_packs.sh): not prefetched until they are
  [ ! -e "$LANGPACK_WORK/packs/${1%.zim}/prefetch.added" ] || return 1
  [ "$(sed -n 2p "$LANGPACK_WORK/packs/${1%.zim}/prefetched" 2> /dev/null)" = "$id" ] && return 0
  # a viewer swap's file takes its parent's mark (ops/langpack/prefetch_mark.py): its Wikipedia
  # is the parent's, in the cache already (once a pass: a parent without one stays without)
  [ -e "$MAPS_DIR/$1.parent.json" ] && [ -z "${PF_NOPARENT[$1 $id]:-}" ] || return 1
  if "$PY" "$ROOT/ops/langpack/prefetch_mark.py" "$MAPS_DIR/$1" "$LANGPACK_WORK/packs" > /dev/null 2>&1 9>&-; then
    say "${1%.zim}: its prefetch mark taken from its viewer swap's parent (nothing to fetch again)"
    return 0
  fi
  PF_NOPARENT[$1 $id]=1
  return 1
}
pf_ended() {  # zim: is a run's step 2 over for this file? (packs/<base>/prefetch.ended: it takes the lock no more)
  local id
  id=$(stat -L -c '%s %.9Y' "$MAPS_DIR/$1" 2> /dev/null) || return 1
  [ "$(head -n 1 "$LANGPACK_WORK/packs/${1%.zim}/prefetch.ended" 2> /dev/null)" = "$id" ]
}
prefetched() {  # region zim: may its run start? (its prefetch done; none going for the region)
  [ "$PF_RID" != "$1" ] || return 1
  [ "$AHEAD" = 1 ] || return 0
  pf_marked "$2" && return 0
  if [ "${PF_TRIES[$1 $2]:-0}" -ge 2 ]; then [ -z "$PF_PID" ]; return; fi   # its run fetches itself
  return 1
}
pf_alive() {
  local s; s=$(starttime "$PF_PID") || return 1
  [ -z "$PF_START" ] || [ "$s" = "$PF_START" ]
}
pf_start() {  # region zim
  local rid=$1 base=$MAPS_DIR/$2
  say "$rid: prefetch ahead: ship_packs.sh $rid $base with SHIP_PREFETCH_ONLY=1 (beside the runs)"
  ( cd "$ROOT" && env -i "${SHIP_ENV[@]}" SHIP_PACK_ITEM="streetzim-$rid-lang" SHIP_BASE_ITEM="streetzim-$rid" SHIP_PREFETCH_ONLY=1 \
      ionice -c2 -n7 nice -n10 bash "$SHIP" "$rid" "$base" >> "$Q/prefetch-$rid.out" 2>&1 < /dev/null ) 9>&- &
  PF_PID=$! PF_RID=$rid PF_ZIM=$2
  PF_START=$(starttime "$PF_PID")
  echo "$PF_PID ${PF_START:--} $rid $2" > "$Q/prefetch.tmp" && mv -f "$Q/prefetch.tmp" "$Q/prefetch"
}
# (from reap) the prefetch that ended; in a pass, the next one: the first map of its list the
# pass found eligible (PF_WANT) whose file has no prefetch, while the host has the memory a
# second run needs and no run going may still prefetch itself (it would wait on the cache's lock)
pf_tick() {
  if [ -n "$PF_PID" ]; then
    pf_alive && return 0
    PF_TRIES[$PF_RID $PF_ZIM]=$(( ${PF_TRIES[$PF_RID $PF_ZIM]:-0} + 1 ))
    if pf_marked "$PF_ZIM"; then say "$PF_RID: prefetch ahead done"
    else say "$PF_RID: prefetch ahead ended without its mark (try ${PF_TRIES[$PF_RID $PF_ZIM]} of 2; see $Q/prefetch-$PF_RID.out and its ship log)"; fi
    PF_ENDS=$(( PF_ENDS + 1 )) PF_SCAN=1 PF_PID= PF_RID= PF_ZIM= PF_START=
    rm -f "$Q/prefetch"
  fi
  [ "$AHEAD" = 1 ] && [ -n "$PF_SCAN" ] && [ $(( ${#PF_WANT[@]} + ${#PF_AHEAD[@]} )) -gt 0 ] || return 0
  stopping && return 0
  local j r z rest avail full
  for r in "${!RUN_PID[@]}"; do pf_marked "${RUN_ZIM[$r]}" || pf_ended "${RUN_ZIM[$r]}" || return 0; done
  for r in "${!PF_WANT[@]}"; do                 # a map that runs next and fetches itself
    [ "${PF_TRIES[$r ${PF_WANT[$r]}]:-0}" -lt 2 ] || pf_marked "${PF_WANT[$r]}" || return 0
  done
  if [ ${#RUN_PID[@]} -gt 0 ] || round_running; then   # the memory a second run needs (room_for)
    avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
    [ "${avail:-0}" -ge $(( MIN_MEM_GB * 1048576 )) ] || return 0
    full=$(awk '/^full/ {sub("avg10=", "", $2); print int($2)}' "${QUEUE_PRESSURE_FILE:-/proc/pressure/memory}" 2>/dev/null)
    [ "${full:-0}" -lt 5 ] || return 0
  fi
  PF_SCAN=
  for j in "${list[@]}"; do
    IFS=$'\t' read -r r z rest <<< "$j"
    [ -n "$r" ] && [ -z "${RUN_PID[$r]:-}" ] || continue
    [ "${PF_WANT[$r]:-}" = "$z" ] || { [ "${PF_AHEAD[$r]:-}" = "$z" ] && [ "${PF_TRIES[$r $z]:-0}" = 0 ]; } || continue
    pf_marked "$z" && continue
    pf_start "$r" "$z"
    break
  done
}

reap() {
  local r ended
  while :; do
    pf_tick
    ended=0
    for r in "${!RUN_PID[@]}"; do alive "$r" || { finish "$r"; ended=1; }; done
    [ $ended = 0 ] || live_all new || true              # a region goes live once its own run ends
    [ "${1:-}" = all ] && [ ${#RUN_PID[@]} -gt 0 ] || return 0
    sleep "${QUEUE_REAP_SECS:-5}" 9>&-
  done
}

# (pass, while a map waits for room) the next map of the list after it, one a wait (from the
# waiting map on at each new wait): eligible and not prefetched, it goes into PF_AHEAD and its
# prefetch ahead can start now, not once the pass reaches it; one try only before then, and it
# never holds the picker as a map that fetches itself (KEY, eligible's, is kept for the map
# that waits)
look_ahead() {
  [ "$AHEAD" = 1 ] && [ -z "$PF_PID" ] || return 0
  local r z tt s key=${KEY:-}
  [ "$ahead" -ge "$i" ] || ahead=$i
  while [ "$ahead" -lt ${#list[@]} ]; do
    IFS=$'\t' read -r r z tt s <<< "${list[$ahead]}"
    ahead=$(( ahead + 1 ))
    [ -n "$r" ] && [ -z "${passed[$r $z]:-}" ] && [ "${tries[$r $z]:-0}" = 0 ] && [ -z "${RUN_PID[$r]:-}" ] \
      && [ -z "${PF_WANT[$r]:-}" ] && [ -z "${PF_AHEAD[$r]:-}" ] || continue
    if eligible "$r" "$z" "$s" && ! prefetched "$r" "$z"; then
      PF_AHEAD[$r]=$z PF_SCAN=1
      pf_tick
    fi
    break
  done
  KEY=$key
}

# (pass, going back to the top) the round's maps again, with what it uploaded since; a prefetch
# wanted for a map no longer listed (a newer row not uploaded yet) is wanted no more
relist() {
  local e r z
  local -A have=()
  mapfile -t list < <(exec 9>&-; candidates)
  for e in "${list[@]}"; do IFS=$'\t' read -r r z _ <<< "$e"; [ -z "$r" ] || have[$r $z]=1; done
  for r in "${!PF_WANT[@]}"; do [ -n "${have[$r ${PF_WANT[$r]}]:-}" ] || unset "PF_WANT[$r]"; done
  for r in "${!PF_AHEAD[@]}"; do [ -n "${have[$r ${PF_AHEAD[$r]}]:-}" ] || unset "PF_AHEAD[$r]"; done
}

pass() {
  LIVE_TRIED=()
  [ -e "$Q/deploy-pending" ] && { preview_update && deploy || true; }
  local rid zim t st n i=0 said= top notready= ends wstart= ahead=0 list=() chores=$SECONDS
  local -A tries=() passed=()
  mapfile -t list < <(exec 9>&-; candidates)
  PF_SCAN=1 PF_WANT=() PF_AHEAD=() PF_NOPARENT=() ends=$PF_ENDS
  while :; do
    if [ "$i" -ge ${#list[@]} ]; then
      # maps waiting for their prefetch ahead: wait for one (or a run) to end, then from the top
      [ -n "$notready" ] || break
      stopping && break
      if [ "$PF_ENDS" = "$ends" ]; then
        [ -n "$PF_PID" ] || [ ${#RUN_PID[@]} -gt 0 ] || break    # neither going: the next pass
        [ -n "$wstart" ] || wstart=$SECONDS
        n=${#RUN_PID[@]}
        sleep "${QUEUE_REAP_SECS:-5}" 9>&-; reap
        [ ${#RUN_PID[@]} -lt "$n" ] || [ "$PF_ENDS" != "$ends" ] || [ $(( SECONDS - wstart )) -ge "$POLL" ] || continue
      fi
      i=0 notready= ends=$PF_ENDS wstart=
      relist
      # a pass held open by prefetches (hours) does what its end does, every QUEUE_PASS_CHORES_SECS:
      # a region whose going live failed is tried again, and a map started twice in it (a listing
      # still pending) may start again (else it waited for the pass's end, never)
      if [ $(( SECONDS - chores )) -ge "${QUEUE_PASS_CHORES_SECS:-3600}" ]; then
        chores=$SECONDS LIVE_TRIED=() PF_NOPARENT=() tries=() passed=()
        live_all || true
      fi
      continue
    fi
    IFS=$'\t' read -r rid zim t st <<< "${list[$i]}"
    i=$(( i + 1 ))
    [ -n "$rid" ] || continue
    # back from the top (header): a map (region and file: a new file of the region is another)
    # started or passed over in this pass goes again only for its retry, and is started at
    # most twice in a pass (QUEUE_RETRY_IN_PASS=0: not again)
    if [ -n "${passed[$rid $zim]:-}" ] || [ "${tries[$rid $zim]:-0}" -gt 0 ]; then
      [ "$IN_PASS" = 1 ] && [ "${tries[$rid $zim]:-0}" -lt 2 ] || continue
      case "$(awk -F'\t' -v r="$rid" '$1 == r {print $2}' "$STATUS")" in pending|published-partial|waiting-listing) ;; *) continue ;; esac
    fi
    n=${#RUN_PID[@]}
    reap
    stopping && break
    [ "$IN_PASS" = 1 ] && [ ${#RUN_PID[@]} -lt "$n" ] && [ "$i" -gt 1 ] && { i=0 notready= ends=$PF_ENDS; relist; continue; }   # a run ended: from the top
    # a new file of a region whose run for another goes: again once that run ends
    [ -n "${RUN_PID[$rid]:-}" ] && [ "${RUN_ZIM[$rid]:-}" != "$zim" ] && { notready=1; continue; }
    eligible "$rid" "$zim" "$st" || { passed[$rid $zim]=1; continue; }
    if ! prefetched "$rid" "$zim"; then
      [ "${PF_WANT[$rid]:-}" = "$zim" ] || PF_SCAN=1
      notready=1 PF_WANT[$rid]=$zim
      pf_tick                                   # its prefetch (or an earlier map's) now, not at the next reap
      [ "$said" = "$rid prefetch" ] || { said="$rid prefetch"; say "$rid: waiting for its prefetch ahead (a later map whose prefetch is done goes first)"; }
      continue
    fi
    # one that fetches itself holds the prefetch ahead while it waits (pf_tick)
    [ "$AHEAD" = 1 ] && ! pf_marked "$zim" && PF_WANT[$rid]=$zim
    # in this order: wait here until there is room for this one (looking ahead from it)
    n=${#RUN_PID[@]} top= ahead=$i
    until room_for "$MAPS_DIR/$zim"; do
      [ "$said" = "$rid" ] || { said=$rid; say "$rid: waiting: $WHY"; }
      sleep "${QUEUE_REAP_SECS:-5}" 9>&-; reap
      stopping && break 2
      if [ ${#RUN_PID[@]} -lt "$n" ]; then
        [ "$IN_PASS" = 1 ] && { top=1; break; }
        n=${#RUN_PID[@]}
      fi
      look_ahead
    done
    [ -z "$top" ] || { i=0 notready= ends=$PF_ENDS; relist; continue; }   # a run ended: from the top (this map too, if still first)
    start_ship "$rid" "$zim" "$KEY"
    unset "PF_WANT[$rid]" "PF_AHEAD[$rid]"
    tries[$rid $zim]=$(( ${tries[$rid $zim]:-0} + 1 )) said=
  done
  reap all
  live_all || true
}

say "== pack_queue at $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?'), maps shipped since $(TZ=America/Los_Angeles date -d @"$SINCE" '+%F %T %Z'), $ROUND_TSV"
# the NVMe scratch's folders of ship_packs.sh runs that were killed (tools/langpack/nvme.py; live runs' are kept)
SCR=${LANGPACK_SCRATCH-/mnt/data/tilemaker/streetzim-packs}
[ -z "$SCR" ] || [ ! -d "$SCR" ] || while read -r l; do say "NVMe scratch: $l"; done < <(exec 9>&-; "$PY" "$ROOT/tools/langpack/nvme.py" sweep --scratch "$SCR" 2>&1)
# runs from before a restart: counted while they go, finished here when they end
for f in "$Q"/runs/*; do
  [ -f "$f" ] || continue
  case $f in *.tmp) rm -f "$f"; continue ;; esac
  rid=${f##*/}
  scheme=
  read -r pid start zim key scheme < "$f" || { rm -f "$f"; continue; }
  RUN_PID[$rid]=$pid RUN_START[$rid]=$start RUN_ZIM[$rid]=$zim RUN_KEY[$rid]=$key RUN_SCHEME[$rid]=${scheme:-1}
  if [ "$start" != - ] && alive "$rid"; then say "$rid: its ship_packs.sh run from before the restart is still going (pid $pid): counted"
  else say "$rid: its run from before the restart has ended: its outcome now"; fi
done
# the prefetch ahead from before a restart: counted while it goes
if [ -f "$Q/prefetch" ]; then
  read -r PF_PID PF_START PF_RID PF_ZIM < "$Q/prefetch" || true
  if [ -n "$PF_PID" ] && [ -n "$PF_RID" ] && [ -n "$PF_ZIM" ] && [ "$PF_START" != - ] && pf_alive; then
    say "$PF_RID: its prefetch ahead from before the restart is still going (pid $PF_PID): counted"
  else PF_PID= PF_RID= PF_ZIM= PF_START=; rm -f "$Q/prefetch"; fi
fi
if [ "$RETRY_FAILED" = 1 ]; then
  for f in "$Q"/live-failed-*; do [ -e "$f" ] && echo 0 > "$Q/live-pending-${f##*/live-failed-}" && rm -f "$f"; done
fi
failed=$(awk -F'\t' '$2 ~ /^failed/ && $4 != "-" {printf "%s ", $1}' "$STATUS")
[ -z "$failed" ] || say "failed before: ${failed% } ($([ "$RETRY_FAILED" = 1 ] && echo 'tried again: QUEUE_RETRY_FAILED=1' || echo 'not tried again; QUEUE_RETRY_FAILED=1 retries them'))"
while :; do
  pass
  [ $ONCE = 1 ] && break
  stopping && break
  sleep "$POLL" 9>&-
done
[ -e "$Q/stop" ] && say "stop file: stopped"
[ $ONCE = 1 ] && [ $ANY_FAILED = 1 ] && exit 1
exit 0
