#!/bin/bash
# The language step after each map of the rebuild round: as the round ships
# a map (its latest row in rebuild-old.tsv, written after this queue first
# started, says `uploaded`; or `upload-pending` and archive.org now lists
# the file with its MD5), this runs ship_packs.sh for it, then
# publishes its recipes into the PREVIEW site and redeploys the preview
# channel. Never the live site: production's web/build/recipes stays empty
# until the page goes live.
#
#   ops/langpack/pack_queue.sh [--once]
#
# In the order the maps shipped, one ship_packs.sh run at a time while the
# round builds (the disk is saturated: its scheduler ignores ionice), up to
# QUEUE_JOBS (2) once no round driver runs (QUEUE_ROUND_PATTERNS: processes
# whose command line names one of these scripts). A second run starts only
# while the host has QUEUE_MIN_MEM_GB (24)
# of memory available, no memory pressure, and room on LANGPACK_WORK's disk
# for its merged files (2 x the base + QUEUE_DISK_MARGIN_GB, 40). Each run
# holds one merged file at a time; ship_packs.sh serializes its network step
# (the shared Wikipedia cache), uploads go one at a time under the host's
# upload lock, and publishing and deploying are done one at a time here.
# Per map:
#   - eligible: not a light/ultralight map, its base on disk, at most
#     QUEUE_MAX_BASE_GB (8; also passed to ship_packs.sh as SHIP_MAX_BASE_GB),
#     multilingual tiles (map-config tileLanguages). A continent is refused by
#     ship_packs.sh itself (exit 4).
#   - ops/langpack/ship_packs.sh REGION BASE with production's items
#     (streetzim-<region>, streetzim-<region>-lang), LANGPACK_WORK,
#     LANGPACK_WEBSEED and host-env.sh's settings, under ionice/nice.
#     Exit 0: published; 6 (archive.org listing pending) or 2 (a lock held):
#     run again after QUEUE_RETRY_SECS; 4 and 5 (v1 gives this map no packs,
#     or nothing is publishable): logged, skipped for this base; anything
#     else: a loud failure, not retried in this run.
#   - published: the preview clone (PREVIEW_DIR, a clone of PROD_REPO moved
#     to its current commit first; made with QUEUE_SEED's recipes already in
#     it, the Swiss preview the channel shows now, which production's
#     Switzerland replaces when it ships) gets the region (publish_site.sh), then
#     `firebase hosting:channel:deploy $PREVIEW_CHANNEL --expires 30d
#     --project $FIREBASE_PROJECT` from it. A failed deploy is tried again
#     at the next pass (nothing is shipped again for it).
# Resumable: a map published for the same base UUID is not shipped again; a
# new file for the region is. The queue's start (QUEUE_DIR/since) is kept,
# so a restart still ignores what shipped before the first start.
#
# Files, all in QUEUE_DIR ($LANGPACK_WORK/pack-queue): lock, since, queue.log
# (PDT), status.tsv (region, state, zim, base uuid, updated, retry after,
# note), published.tsv (region, uuid, zim, time), deploy-pending, stop (a
# file that ends the loop after the current pass), ship-<region>.out. The
# round's files are only read (ROUND_TSV).
#
# Env (production defaults): ROUND_TSV (/storage/streetzim/rebuild-old.tsv),
# MAPS_DIR (/storage/streetzim), PROD_REPO (/storage/streetzim), PREVIEW_DIR
# (/storage/experiments/build-preview/streetzim), PREVIEW_CHANNEL
# (swiss-languages), FIREBASE_PROJECT (streetzim), FIREBASE (firebase),
# LANGPACK_WEBSEED (https://streetzim-build-proxy.tiltastech.workers.dev),
# QUEUE_POLL_SECS (300), QUEUE_RETRY_SECS (1800), QUEUE_MAX_BASE_GB (8),
# QUEUE_JOBS (2), QUEUE_MIN_MEM_GB (24), QUEUE_DISK_MARGIN_GB (40),
# QUEUE_SINCE (first run only: count maps shipped since this date, e.g. the
# round's start, instead of since now), QUEUE_SEED (REGION=SITE_DIR ...: site
# folders, SITE_DIR/recipes/REGION sealed by ship_packs.sh, published into a
# new preview clone; default the Swiss test site), QUEUE_ROUND_PATTERNS,
# IA (the archive.org CLI, for upload-pending rows).
# Tests: QUEUE_SHIP and QUEUE_PUBLISH (stand-ins for ship_packs.sh and
# publish_site.sh).
# Exit: 0; 2 another queue holds the lock; 1 (--once) a map failed.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$ROOT/ops/langpack/host-env.sh"
export LANGPACK_WEBSEED=${LANGPACK_WEBSEED:-https://streetzim-build-proxy.tiltastech.workers.dev}
ROUND_TSV=${ROUND_TSV:-/storage/streetzim/rebuild-old.tsv}
MAPS_DIR=${MAPS_DIR:-/storage/streetzim}
PROD_REPO=${PROD_REPO:-/storage/streetzim}
PREVIEW_DIR=${PREVIEW_DIR:-/storage/experiments/build-preview/streetzim}
CHANNEL=${PREVIEW_CHANNEL:-swiss-languages}
PROJECT=${FIREBASE_PROJECT:-streetzim}
FIREBASE=${FIREBASE:-firebase}
SHIP=${QUEUE_SHIP:-$ROOT/ops/langpack/ship_packs.sh}
POLL=${QUEUE_POLL_SECS:-300}
RETRY=${QUEUE_RETRY_SECS:-1800}
MAXGB=${QUEUE_MAX_BASE_GB:-8}
JOBS=${QUEUE_JOBS:-2}
MIN_MEM_GB=${QUEUE_MIN_MEM_GB:-24}
DISK_MARGIN_GB=${QUEUE_DISK_MARGIN_GB:-40}
PY=${PYTHON:-python3}
IA=${IA:-/storage/streetzim/venv-linux/bin/ia}
SEED=${QUEUE_SEED-switzerland=/storage/experiments/lang-torrents/swiss-site2}
ROUND_PATTERNS=${QUEUE_ROUND_PATTERNS-cloud/rebuild_old_regions.sh ops/cloud/parallel_rebuild.sh sz-tests/rebuild-all/run.sh}
Q=${QUEUE_DIR:-$LANGPACK_WORK/pack-queue}
ONCE=0; [ "${1:-}" = --once ] && ONCE=1
mkdir -p "$Q" || exit 1
LOG=$Q/queue.log STATUS=$Q/status.tsv DONE=$Q/published.tsv
say() { echo "$(TZ=America/Los_Angeles date '+%F %T %Z') $*" >> "$LOG"; }
loud() { say "FAILED: $*"; echo "pack_queue: FAILED: $*" >&2; }
exec 9> "$Q/lock"
flock -n 9 || { echo "pack_queue: another queue holds $Q/lock" >&2; exit 2; }
# the start: now, or QUEUE_SINCE (a date, e.g. the round's start) on the first run
if [ ! -s "$Q/since" ]; then
  if [ -n "${QUEUE_SINCE:-}" ]; then date -d "$QUEUE_SINCE" +%s > "$Q/since" || { echo "pack_queue: QUEUE_SINCE '$QUEUE_SINCE' is not a date" >&2; exit 1; }
  else date +%s > "$Q/since"; fi
fi
SINCE=$(cat "$Q/since")
touch "$STATUS" "$DONE"
declare -A FAILED_NOW=() RUN_PID=() RUN_ZIM=() RUN_UUID=()
ANY_FAILED=0

# region state zim uuid [retry-after] [note]: one line per region, replaced atomically
set_state() {
  local t
  t=$(mktemp "$Q/.status.XXXXXX") || return 1
  awk -F'\t' -v r="$1" '$1 != r' "$STATUS" > "$t"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$(TZ=America/Los_Angeles date '+%F %T %Z')" "${5:--}" "${6:-}" >> "$t"
  mv -f "$t" "$STATUS"
}
state_of() { awk -F'\t' -v r="$1" -v u="$2" '$1 == r && $4 == u {print $2 "\t" $6}' "$STATUS"; }

# region <TAB> zim <TAB> epoch <TAB> status: the maps whose latest row, written since the start, is
# `uploaded` or `upload-pending` (archive.org's listing to be checked), in that order
candidates() {
  "$PY" - "$ROUND_TSV" "$SINCE" <<'PY'
import datetime, sys
tsv, since = sys.argv[1], int(sys.argv[2])
last = {}
try:
    lines = open(tsv, encoding="utf-8").read().splitlines()
except OSError:
    lines = []
for i, line in enumerate(lines):
    f = line.split("\t")
    if i == 0 or len(f) < 4 or not f[0] or f[0].startswith("#"):
        continue
    last[f[0]] = f
rows = []
for rid, f in last.items():
    try:
        t = datetime.datetime.fromisoformat(f[3]).timestamp()
    except ValueError:
        continue
    if f[1] in ("uploaded", "upload-pending") and t >= since:
        rows.append((t, rid, f[2], f[1]))
for t, rid, z, st in sorted(rows):
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

# The local file's MD5, computed once per file (name, size, mtime).
file_md5() {
  local f=$1 k
  k=$Q/md5/$(basename "$f").$(stat -c %s.%Y "$f")
  [ -s "$k" ] || { mkdir -p "$Q/md5" && ionice -c3 nice -n19 md5sum "$f" | cut -d' ' -f1 > "$k.tmp" && mv -f "$k.tmp" "$k"; } || return 1
  cat "$k"
}
# Does archive.org list the region's file with the local file's MD5? (an upload-pending row)
listed() {  # region base
  local md5
  md5=$(file_md5 "$2") || return 1
  timeout 300 "$IA" metadata "streetzim-$1" 2>/dev/null | "$PY" "$ROOT/ops/cloud/archive_has_file.py" "$(basename "$2")" "$md5"
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
    if LANGPACK_WORK=$w bash "${QUEUE_PUBLISH:-$PREVIEW_DIR/ops/langpack/publish_site.sh}" "$rid" "$PREVIEW_DIR/web" >> "$LOG" 2>&1; then
      say "seeded the preview with $rid from $dir"
    else loud "seed $rid: publish_site.sh refused $dir/recipes/$rid"; fi
  done
}

# the preview clone at production's current commit (web/build/recipes is ignored by git: kept)
preview_update() {
  if [ ! -d "$PREVIEW_DIR/.git" ]; then
    mkdir -p "$(dirname "$PREVIEW_DIR")" && git clone -q "$PROD_REPO" "$PREVIEW_DIR" || return 1
    say "preview clone made: $PREVIEW_DIR"
    seed_preview
  fi
  local want have
  want=$(git -C "$PROD_REPO" rev-parse HEAD) || return 1
  have=$(git -C "$PREVIEW_DIR" rev-parse HEAD 2>/dev/null)
  if [ "$want" != "$have" ]; then
    git -C "$PREVIEW_DIR" fetch -q "$PROD_REPO" HEAD && git -C "$PREVIEW_DIR" checkout -q --detach "$want" || return 1
    say "preview clone moved to $(git -C "$PREVIEW_DIR" rev-parse --short HEAD)"
  fi
}

deploy() {
  say "deploy: firebase hosting:channel:deploy $CHANNEL --expires 30d --project $PROJECT (from $PREVIEW_DIR)"
  if (cd "$PREVIEW_DIR" && timeout 3600 "$FIREBASE" hosting:channel:deploy "$CHANNEL" --expires 30d --project "$PROJECT") >> "$LOG" 2>&1; then
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
  if ! LANGPACK_WORK=$LANGPACK_WORK bash "$pub" "$1" "$PREVIEW_DIR/web" >> "$LOG" 2>&1; then
    loud "$1: publish_site.sh into $PREVIEW_DIR/web failed"; return 1
  fi
  say "$1: published into $PREVIEW_DIR/web/build/recipes"
}

# Is the map to be shipped now? Records why not. Sets UUID.
eligible() {  # region zim round-status
  local rid=$1 zim=$2 base=$MAPS_DIR/$2 ntile st state retry gb
  UUID=
  [ -n "${RUN_PID[$rid]:-}" ] && return 1                     # shipping now
  case "$rid" in *-light|*-light-*|*ultralight*) [ -n "$(state_of "$rid" -)" ] || { set_state "$rid" ineligible-light "$zim" - - "a light map carries no Wikipedia"; say "$rid: a light map: no packs"; }; return 1 ;; esac
  if [ ! -s "$base" ]; then
    [ "$(state_of "$rid" - | cut -f1)" = missing ] || { loud "$rid: $base is not on disk"; set_state "$rid" missing "$zim" - - "not in $MAPS_DIR"; }
    ANY_FAILED=1; return 1
  fi
  read -r UUID ntile < <(baseinfo "$base") || true
  if [ -z "${UUID:-}" ]; then loud "$rid: could not read $base"; set_state "$rid" failed "$zim" - - "unreadable"; ANY_FAILED=1; return 1; fi
  grep -qP "^\Q$rid\E\t$UUID\t" "$DONE" && return 1            # published for this base already
  st=$(state_of "$rid" "$UUID"); state=${st%%$'\t'*}; retry=${st#*$'\t'}
  case "$state" in
    refused-*|ineligible-*) return 1 ;;
    failed*) [ -n "${FAILED_NOW[$rid]:-}" ] && return 1 ;;
    pending) [ "$retry" != - ] && [ "$(date +%s)" -lt "$retry" ] && return 1 ;;
  esac
  gb=$("$PY" -c 'import sys; print(f"{int(sys.argv[1]) / 1e9:.2f}")' "$(stat -c %s "$base")")
  if [ "$("$PY" -c 'import sys; print(int(int(sys.argv[1]) > float(sys.argv[2]) * 1e9))' "$(stat -c %s "$base")" "$MAXGB")" = 1 ]; then
    set_state "$rid" ineligible-size "$zim" "$UUID" - "$gb GB > $MAXGB GB"
    say "$rid: $zim is $gb GB, over the $MAXGB GB limit: no language packs in v1"; return 1
  fi
  if [ "${ntile:-0}" = 0 ]; then
    set_state "$rid" ineligible-no-ml-tiles "$zim" "$UUID" - "no tileLanguages"; say "$rid: $zim has no multilingual tiles: no packs"; return 1
  fi
  if [ "${3:-uploaded}" = upload-pending ] && ! listed "$rid" "$base"; then
    [ "$state" = waiting-listing ] || { set_state "$rid" waiting-listing "$zim" "$UUID" - "upload-pending: archive.org does not list it yet"
                                        say "$rid: upload-pending; waiting until archive.org lists $zim with its MD5"; }
    return 1
  fi
  return 0
}

# Room for one more run beside those going: memory available, no pressure,
# disk for its merged files.
round_running() {
  local p pat c
  [ -n "$ROUND_PATTERNS" ] || return 1
  for p in $(pgrep -x bash); do
    c=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null) || continue
    for pat in $ROUND_PATTERNS; do case "$c" in *"$pat"*) return 0 ;; esac; done
  done
  return 1
}
room_for() {  # base
  [ ${#RUN_PID[@]} = 0 ] && return 0
  [ ${#RUN_PID[@]} -lt "$JOBS" ] || return 1
  round_running && return 1                                   # the round builds: one run at a time
  local avail full need free
  avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  [ "${avail:-0}" -ge $(( MIN_MEM_GB * 1048576 )) ] || return 1
  full=$(awk '/^full/ {sub("avg10=", "", $2); print int($2)}' /proc/pressure/memory 2>/dev/null)
  [ "${full:-0}" -lt 5 ] || return 1
  need=$(( $(stat -c %s "$1") / 1024 * 2 + DISK_MARGIN_GB * 1048576 ))
  for r in "${!RUN_ZIM[@]}"; do need=$(( need + $(stat -c %s "$MAPS_DIR/${RUN_ZIM[$r]}" 2>/dev/null || echo 0) / 1024 * 2 )); done
  free=$(df -Pk "$LANGPACK_WORK" | awk 'NR == 2 {print $4}')
  [ "${free:-0}" -ge "$need" ]
}

start_ship() {  # region zim uuid
  local rid=$1 zim=$2 base=$MAPS_DIR/$2
  set_state "$rid" shipping "$zim" "$3"
  rm -f "$Q/rc-$rid"
  say "$rid: ship_packs.sh $rid $base (runs going: $(( ${#RUN_PID[@]} + 1 )))"
  ( cd "$ROOT" && env SHIP_MAX_BASE_GB="$MAXGB" ionice -c3 nice -n10 bash "$SHIP" "$rid" "$base" >> "$Q/ship-$rid.out" 2>&1 < /dev/null
    echo $? > "$Q/rc-$rid" ) 9>&- &
  RUN_PID[$rid]=$!; RUN_ZIM[$rid]=$zim; RUN_UUID[$rid]=$3
}

# A run that ended: its outcome (publishing and deploying one at a time, here).
finish() {  # region
  local rid=$1 zim=${RUN_ZIM[$1]} uuid=${RUN_UUID[$1]} rc
  wait "${RUN_PID[$rid]}" 2>/dev/null
  rc=$(cat "$Q/rc-$rid" 2>/dev/null || echo 1); rm -f "$Q/rc-$rid"
  unset "RUN_PID[$rid]" "RUN_ZIM[$rid]" "RUN_UUID[$rid]"
  case $rc in
    0)
      say "$rid: packs shipped"
      if publish "$rid"; then
        printf '%s\t%s\t%s\t%s\n' "$rid" "$uuid" "$zim" "$(TZ=America/Los_Angeles date '+%F %T %Z')" >> "$DONE"
        set_state "$rid" published "$zim" "$uuid"
        deploy || set_state "$rid" published-not-deployed "$zim" "$uuid" - "deploy pending"
      else
        set_state "$rid" failed-publish "$zim" "$uuid" - "see queue.log"; FAILED_NOW[$rid]=1; ANY_FAILED=1
      fi ;;
    6|2)
      set_state "$rid" pending "$zim" "$uuid" "$(( $(date +%s) + RETRY ))" "ship_packs rc=$rc"
      say "$rid: ship_packs rc=$rc ($([ "$rc" = 6 ] && echo 'archive.org listing pending' || echo 'a lock held')): again after $RETRY s" ;;
    4|5)
      set_state "$rid" "refused-$rc" "$zim" "$uuid" - "ship_packs rc=$rc"
      say "$rid: ship_packs rc=$rc ($([ "$rc" = 4 ] && echo 'v1 gives this map no packs' || echo 'nothing publishable')): skipped" ;;
    *)
      set_state "$rid" "failed" "$zim" "$uuid" - "ship_packs rc=$rc"; FAILED_NOW[$rid]=1; ANY_FAILED=1
      loud "$rid: ship_packs.sh rc=$rc (see $Q/ship-$rid.out and its log under $LANGPACK_WORK/logs)" ;;
  esac
}

# Finish the runs that ended; with "all", wait for every one.
reap() {
  local r
  while :; do
    for r in "${!RUN_PID[@]}"; do kill -0 "${RUN_PID[$r]}" 2>/dev/null || finish "$r"; done
    [ "${1:-}" = all ] && [ ${#RUN_PID[@]} -gt 0 ] || return 0
    sleep "${QUEUE_REAP_SECS:-5}"
  done
}

pass() {
  [ -e "$Q/deploy-pending" ] && { preview_update && deploy || true; }
  local rid zim t st
  while IFS=$'\t' read -r rid zim t st <&3; do
    [ -n "$rid" ] || continue
    reap
    eligible "$rid" "$zim" "$st" || continue
    # in the order they shipped: wait here until there is room for this one
    until room_for "$MAPS_DIR/$zim"; do sleep "${QUEUE_REAP_SECS:-5}"; reap; done
    start_ship "$rid" "$zim" "$UUID"
  done 3< <(candidates)
  reap all
}

say "== pack_queue at $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?'), maps shipped since $(TZ=America/Los_Angeles date -d @"$SINCE" '+%F %T %Z'), $ROUND_TSV"
while :; do
  pass
  [ $ONCE = 1 ] && break
  [ -e "$Q/stop" ] && { say "stop file: stopping"; break; }
  sleep "$POLL"
done
[ $ONCE = 1 ] && [ $ANY_FAILED = 1 ] && exit 1
exit 0
