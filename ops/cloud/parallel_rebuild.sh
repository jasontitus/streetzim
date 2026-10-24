#!/usr/bin/env bash
# Rebuild regions several at a time (user 2026-10-23: "I want all the European
# ones redone in the modern multilingual way in a day or so"), each in its own
# instance of the single-region round driver, cloud/rebuild_old_regions.sh:
# extract, tiles, Overture, build-region-fast.sh, validate, markers, gates,
# upload -- unchanged, so a parallel build is the same build as a serial one.
#
# Workers and lanes
#  * PAR_WORKERS (3) driver instances, one region each, from PAR_QUEUE. The
#    queue is split into groups by "## <name>" lines, each listed small to
#    large; all workers take from the first group that still has work.
#    Worker 0 takes the LARGEST remaining region of that group, the others
#    the smallest, so the long builds start early.
#  * At most PAR_BIG_MAX (2) regions whose current ZIM is >= PAR_BIG_GB (8)
#    at once. A PAR_ALONE region (europe) starts only when nothing else runs,
#    and nothing starts beside it; PAR_HDD_SCRATCH regions (europe: ~212 GB
#    of scratch) keep their scratch on /storage.
#  * A Light variant (cloud/region-variants.tsv) and its parent share the
#    parent's <id>.ml.mbtiles: neither starts while a build-region-fast.sh of
#    the other runs anywhere on the host. A region that must wait is skipped,
#    never blocking the queue.
#  * Each driver: its own list, pid file, log and gate ports (PAR_PORT_BASE +
#    2k, +1; any kiwix-serve still listening there is stopped first),
#    REBUILD_NO_SKIP=1, REBUILD_STAGE_MBTILES=1 (the region's .ml.mbtiles is
#    staged on the NVMe), CPU affinity PAR_CPUS_PER_WORKER cores (12), and an
#    environment scrubbed of STREETZIM_*, LANGPACK_*, BUILD_*, REBUILD_*,
#    NVME_*, OVERTURE_*, SHIP_*, ZIM_BUILDER, PACK_THREADS, ZSTD_CLEVEL,
#    WIKI_IMAGES, STAGE_MBTILES_NVME, CLIP and RAYON_NUM_THREADS, then given
#    STREETZIM_ML_TILES=1, STREETZIM_REQUIRE_WIKI=1 (unresolved Wikipedia
#    titles stop the build), STREETZIM_WIKI_MAX_PER_MIN=PAR_WIKI_MAX_PER_MIN
#    (40: three builds stay under the per-IP pace) and STREETZIM_KEEP_ZIMS=3
#    (a region uploaded twice keeps its pre-round file), plus
#    $PAR_STATE/env/<id> (KEY=VALUE lines, e.g. STREETZIM_SEARCH_CACHE,
#    OVERTURE_RELEASE for a region whose round parquets Overture no longer
#    serves, STREETZIM_ML_COMMIT to pin the tile image while CI is red).
#    Rows still go to rebuild-old.tsv, appended under a lock exactly as the
#    driver writes them; nothing is dropped.
#
# NVMe (/mnt/data, shared with another project; never below PAR_NVME_MIN_GB)
#  * Build scratch in PAR_SCRATCH/w<k> (BUILD_SCRATCH: TMPDIR and the
#    builder's output; build-region-fast.sh copies the finished ZIM to
#    /storage/streetzim under a temporary name and renames it). Wiped when
#    the region ends; the build log is on /storage.
#  * The English Wikipedia ZIM read from a verified NVMe copy
#    (PAR_WIKI_NVME=1: copied once, in the background, to PAR_WIKI_DIR if
#    room, md5-checked against the stream it was copied from; the /storage
#    original is never touched) -- the article bundling reads it at random.
#    A build started before the copy is verified reads the original.
#  * Start guard: free - (what running builds may still write) - (live
#    tile-store reservations) - (this region's projection: PAR_SCRATCH_FACTOR_X10/10
#    x its current ZIM, at least PAR_SCRATCH_FLOOR_GB, plus its staged
#    .ml.mbtiles) must stay >= PAR_NVME_MIN_GB (100).
#  * Watchdog: below PAR_NVME_MIN_GB the newest running build is paused
#    (SIGSTOP to its session) as long as another keeps running -- but never
#    one holding the upload, tile or extract lock or gating/uploading; above
#    PAR_NVME_RESUME_GB (120) the oldest paused one resumes; below
#    PAR_NVME_KILL_GB (60) the newest build is stopped, its scratch wiped,
#    and it is requeued with a doubled projection -- never the last running
#    build (an ALERT is logged instead).
#  * Memory: no start while MemAvailable < PAR_MEM_MIN_GB (30), or
#    PAR_MEM_BIG_GB (50) for a big region.
#
# Before the first start
#  * Refuses unless the driver, build-region-fast.sh and upload_validated.sh
#    carry this runner's knobs (it must never fall back to a driver that
#    would ignore its list and rebuild the round's own list), and records
#    the checkout's HEAD: if the code changes during the round (any commit
#    touching more than web/torrents, which every upload commits), nothing
#    new starts. Logs the PATH and where ia, docker, osmium, firebase and
#    node resolve.
#  * Waits while sz-tests/rebuild-all/run.sh prepares its driver (it rewrites
#    rebuild-old.tsv once, before "started driver").
#  * Cuts the multilingual tiles of the first queue group one at a time in
#    the background, in the order the workers will take the regions
#    (ops/region-ml-tiles.py reuses tiles whose key matches; a region whose
#    extract is missing or cut for another box is left to its build). A
#    region of that group starts only once its tiles are cut (or the cut
#    failed, or was left to the build), so builds never queue on the tile
#    lock while holding scratch.
#
# Afterwards
#  * A region ending upload-pending (archive.org had not listed it) is
#    finished in the background: once listed with the local md5,
#    upload_validated.sh runs again (torrent, prune, deploy) and an
#    `uploaded` row is appended.
#  * Resumable: $PAR_STATE/status/<id>. A rerun re-adopts drivers that are
#    still running (pid + start time), retries interrupted regions, skips
#    shipped ones, and leaves failed ones unless PAR_RETRY_FAILED=1.
#  * $PAR_STATE/STOP: start nothing new; exit when the running ones end.
#  * Every failure is logged as FAILED; exit 1 if any region failed.
#
# Rolling a region back to its pre-round file: `ia delete streetzim-<id>
# <new file>`, `git revert` the "torrents: <id> -> <new file>" commit, then
# `python3 web/generate.py --deploy` (the pre-round file is still in the item:
# STREETZIM_KEEP_ZIMS=3).
#
# After the round (queue empty): `bash ops/cloud/parallel_rebuild.sh
# --cleanup-wiki` removes the NVMe Wikipedia copy (refuses while a runner or
# any reader is active).
#
# A region whose gates failed for a reason since fixed: `bash
# ops/cloud/parallel_rebuild.sh --regate REGION ZIM` (gates and upload of the
# built ZIM, no rebuild; see regate() below).
#
# Logs: $PAR_STATE/run.out (PDT), $PAR_STATE/status.txt (a table with start,
# end and duration in PDT, rewritten every poll), $PAR_STATE/logs/<id>.log.
# Usage (from /storage/streetzim, with $PAR_STATE/queue.txt written):
#   setsid nohup bash ops/cloud/parallel_rebuild.sh > rebuild-par/run.nohup 2>&1 < /dev/null &
# Stop after the current builds:  touch rebuild-par/STOP
set -uo pipefail
ROOT=${PAR_ROOT:-/storage/streetzim}
STATE=${PAR_STATE:-$ROOT/rebuild-par}
QUEUE=${PAR_QUEUE:-$STATE/queue.txt}
SCRATCH=${PAR_SCRATCH:-/mnt/data/tilemaker/streetzim-par}
ML_SCRATCH=${PAR_ML_SCRATCH:-/mnt/data/tilemaker/streetzim-scratch}
NVME=${PAR_NVME:-/mnt/data}
WORKERS=${PAR_WORKERS:-3}
POLL=${PAR_POLL:-30}
STAGGER=${PAR_STAGGER:-120}
NVME_MIN_GB=${PAR_NVME_MIN_GB:-100}
NVME_RESUME_GB=${PAR_NVME_RESUME_GB:-120}
NVME_KILL_GB=${PAR_NVME_KILL_GB:-60}
SCRATCH_FACTOR_X10=${PAR_SCRATCH_FACTOR_X10:-40}
SCRATCH_FLOOR_GB=${PAR_SCRATCH_FLOOR_GB:-5}
MEM_MIN_GB=${PAR_MEM_MIN_GB:-30}
MEM_BIG_GB=${PAR_MEM_BIG_GB:-50}
BIG_GB=${PAR_BIG_GB:-8}
BIG_MAX=${PAR_BIG_MAX:-2}
ALONE=" ${PAR_ALONE-europe} "
HDD_SCRATCH=" ${PAR_HDD_SCRATCH-europe} "
PORT_BASE=${PAR_PORT_BASE:-9811}
CPUS_PER_WORKER=${PAR_CPUS_PER_WORKER:-12}
WIKI_MAX_PER_MIN=${PAR_WIKI_MAX_PER_MIN:-40}
DRIVER=${PAR_DRIVER:-$ROOT/cloud/rebuild_old_regions.sh}
WRAPPER=${PAR_WRAPPER:-$ROOT/ops/build-region-fast.sh}
UPLOADER=${PAR_UPLOADER:-$ROOT/ops/cloud/upload_validated.sh}
TSV=${PAR_TSV:-$ROOT/rebuild-old.tsv}
MEMWATCH=${PAR_MEMWATCH:-$ROOT/sz-tests/round-resume/memwatch.sh}
RETRY_FAILED=${PAR_RETRY_FAILED:-0}
VARIANTS=${PAR_VARIANTS:-$ROOT/cloud/region-variants.tsv}
REGISTRY=${PAR_REGISTRY:-$ROOT/cloud/regions.tsv}
RUNSH=$(readlink -f "${PAR_RUNSH:-$ROOT/sz-tests/rebuild-all/run.sh}" 2>/dev/null || echo "${PAR_RUNSH:-$ROOT/sz-tests/rebuild-all/run.sh}")
RUNSH_OUT=${PAR_RUNSH_OUT:-$ROOT/sz-tests/rebuild-all/run.out}
WIKI_NVME=${PAR_WIKI_NVME:-1}
WIKI_SRC=${PAR_WIKI_SRC:-$ROOT/wiki-src/wikipedia_en_all_maxi_2026-02.zim}
WIKI_DIR=${PAR_WIKI_DIR:-/mnt/data/tilemaker/streetzim-wiki}
PRECUT=${PAR_PRECUT:-1}
PRECUT_CMD=${PAR_PRECUT_CMD:-}
PY=${PAR_PY:-$ROOT/venv-linux/bin/python3}
IA=${PAR_IA:-$ROOT/venv-linux/bin/ia}
PENDING_EVERY=${PAR_PENDING_EVERY:-600}
DRIVER_PATH=${PAR_PATH:-$PATH}   # the drivers need docker, node, osmium and ia on it

OUT=$STATE/run.out
# --cleanup-wiki: after the round (queue empty), remove the NVMe copy of the
# English Wikipedia ZIM. Refuses while a runner holds the lock or any process
# has the copy open.
if [ "${1:-}" = --cleanup-wiki ]; then
  mkdir -p "$STATE"
  exec 7> "$STATE/run.lock"
  flock -n 7 || { echo "a parallel_rebuild.sh is running (it holds $STATE/run.lock); not removing the copy" >&2; exit 1; }
  _d="$WIKI_DIR/$(basename "$WIKI_SRC")"
  for _f in "$_d" "$_d.part"; do
    if [ -e "$_f" ] && fuser -s "$_f" 2>/dev/null; then echo "$_f is open by a process; not removing it" >&2; exit 1; fi
  done
  rm -f "$_d" "$_d.part" "$_d.verified" "$_d.srcmd5"
  rmdir "$WIKI_DIR" 2>/dev/null || true
  echo "removed the NVMe Wikipedia copy ($_d)"; exit 0
fi
mkdir -p "$STATE/status" "$STATE/logs" "$STATE/lists" "$STATE/pids" "$STATE/env" "$STATE/pending" || exit 1
say() { echo "$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M:%S %Z') $*" >> "$OUT"; }
die() { say "FATAL: $*"; exit 1; }
pdt() { [ -n "$1" ] && [ "$1" != 0 ] && TZ=America/Los_Angeles date -d "@$1" '+%m-%d %H:%M' || echo -; }
[ -s "$QUEUE" ] || die "no queue $QUEUE"
case "$WORKERS" in ''|*[!0-9]*|0) die "PAR_WORKERS must be a positive integer, not '$WORKERS'" ;; esac
for _v in STAGGER NVME_MIN_GB NVME_RESUME_GB NVME_KILL_GB SCRATCH_FACTOR_X10 SCRATCH_FLOOR_GB MEM_MIN_GB \
          MEM_BIG_GB BIG_GB BIG_MAX PORT_BASE CPUS_PER_WORKER WIKI_MAX_PER_MIN PENDING_EVERY; do
  case "${!_v}" in ''|*[!0-9]*) die "PAR_$_v must be a whole number, not '${!_v}'" ;; esac
done
case "$SCRATCH" in
  /mnt/data/tilemaker/*|"$STATE"/*|/tmp/*) ;;
  *) [ -n "${PAR_SCRATCH_ANYWHERE:-}" ] || die "PAR_SCRATCH '$SCRATCH' is outside /mnt/data/tilemaker (set PAR_SCRATCH_ANYWHERE=1 to allow)" ;;
esac
mkdir -p "$SCRATCH" || die "cannot create $SCRATCH"
# The knobs this runner relies on. A driver without them would ignore the
# per-worker list and rebuild the round's own list next to everything else.
for _k in REBUILD_LIST REBUILD_PIDFILE REBUILD_LOG REBUILD_OV_PORT REBUILD_KW_PORT REBUILD_NO_SKIP REBUILD_STAGE_MBTILES; do
  grep -q "$_k" "$(readlink -f "$DRIVER")" 2>/dev/null || die "$DRIVER has no $_k: it is not the parallel-capable driver; not starting"
done
for _k in BUILD_SCRATCH BUILD_WIKI_ZIM; do
  grep -q "$_k" "$(readlink -f "$WRAPPER")" 2>/dev/null || die "$WRAPPER has no $_k; not starting"
done
grep -q STREETZIM_KEEP_ZIMS "$(readlink -f "$UPLOADER")" 2>/dev/null || die "$UPLOADER has no STREETZIM_KEEP_ZIMS; not starting"
# Freeze on map-build code, not on HEAD: every upload commits a torrent
# (web/torrents) in this checkout, and language-pack merges (ops/langpack,
# tools/langpack, tests/langpack, web/build) and docs never touch a map build;
# none of them may stall the round. `git diff A B` compares trees only (no
# index refresh, so no index lock).
HEAD0=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo "")
FREEZE_IGNORE=(web/torrents ops/langpack tools/langpack tests/langpack web/build docs)
code_moved() {
  [ -n "$HEAD0" ] || return 1
  ! git -C "$ROOT" diff --quiet "$HEAD0" HEAD -- . "${FREEZE_IGNORE[@]/#/:(exclude)}" 2>/dev/null
}

# ---- measurements (overridable for tests) ---------------------------------
free_gb() {
  if [ -n "${PAR_FREE_GB_CMD:-}" ]; then bash -c "$PAR_FREE_GB_CMD"; return; fi
  df -BG --output=avail "$NVME" 2>/dev/null | tail -1 | tr -dc '0-9'
}
mem_gb() {
  if [ -n "${PAR_MEM_GB_CMD:-}" ]; then bash -c "$PAR_MEM_GB_CMD"; return; fi
  awk '/^MemAvailable:/ {print int($2/1048576)}' /proc/meminfo
}
gib_up() { echo $(( (${1:-0} + 1073741823) / 1073741824 )); }
used_gb() { gib_up "$(du -sb "$1" 2>/dev/null | cut -f1)"; }
ml_reserved_gb() {  # live reservations by ops/region-ml-tiles.py's tile stores
  local f pid b st cur sum=0
  for f in "$ML_SCRATCH"/.reserve-ml-*; do
    [ -e "$f" ] || continue
    pid=${f##*-}
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    b=""; st=""; read -r b st < "$f" || true
    case "$b" in ''|*[!0-9]*) continue ;; esac
    [ "${#b}" -le 15 ] || b=1000000000000000000
    cur=$(sed 's/.*) //' "/proc/$pid/stat" 2>/dev/null | cut -d' ' -f20) || cur=""
    if [ -n "$cur" ] && [ "$cur" = "$st" ]; then sum=$(( sum + 10#$b )); fi
  done
  gib_up "$sum"
}
src_of() {  # the region whose extracts and tiles $1 uses (its parent for a variant)
  local p
  p=$(awk -F'\t' -v id="$1" '$1 !~ /^#/ && $1 == id {print $2; exit}' "$VARIANTS" 2>/dev/null)
  echo "${p:-$1}"
}
zim_gb() {  # the region's newest dated ZIM in ROOT, GiB rounded up (0 if none)
  local f
  f=$(cd "$ROOT" && ls -1 "osm-$1-"20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]*.zim 2>/dev/null \
      | grep -E "^osm-$1-20[0-9]{2}-[0-9]{2}-[0-9]{2}[a-z]?\.zim$" | sort | tail -1)
  [ -n "$f" ] || { echo 0; return; }
  gib_up "$(stat -c %s "$ROOT/$f" 2>/dev/null || echo 0)"
}
is_big() { [ "$(zim_gb "$1")" -ge "$BIG_GB" ]; }
proj_gb() {  # projected NVMe use of a region: scratch + its staged .ml.mbtiles
  local z p mult mb
  z=$(zim_gb "$1"); mult=$(cat "$STATE/status/$1.mult" 2>/dev/null || echo 1)
  if [[ "$HDD_SCRATCH" == *" $1 "* ]]; then p=0
  else
    p=$(( (z * SCRATCH_FACTOR_X10 * mult + 9) / 10 ))
    [ "$p" -ge "$SCRATCH_FLOOR_GB" ] || p=$SCRATCH_FLOOR_GB
  fi
  mb=$(stat -c %s "$ROOT/world-data/regions/$(src_of "$1").ml.mbtiles" 2>/dev/null || echo 0)
  echo $(( p + $(gib_up "$mb") ))
}
start_time() { sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f20; }
session_pids() { ps -eo pid=,sid= 2>/dev/null | awk -v s="$1" '$2 == s {print $1}'; }
signal_session() {  # $1 signal, $2 session leader (driver pid)
  local p
  for p in $(session_pids "$2"); do kill "-$1" "$p" 2>/dev/null || true; done
}
# A session may be paused only while none of its processes holds a shared
# lock or talks to the outside: upload (and its flock), extract (PBF lock),
# tile cut (tile lock), gates (kiwix-serve and the browser checks), validate.
pausable() {
  local p c
  for p in $(session_pids "$1"); do
    c=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null) || continue
    case "$c" in
      *upload_validated.sh*|*flock*|*osmium*|*region-ml-tiles.py*|*kiwix_viewer_gate.sh*|*kiwix-serve*| \
      *device-matrix.mjs*|*overlap-check.mjs*|*map-health.mjs*|*validate_zim.py*|*cleanup_old_zims*| \
      *generate.py*|*ia\ upload*|*download_overture_data.py*) return 1 ;;
    esac
  done
  return 0
}
wipe_scratch() {  # $1 worker index; only PAR_SCRATCH/w<digits>
  local d="$SCRATCH/w$1"
  case "$1" in ''|*[!0-9]*) say "refusing to wipe worker '$1'"; return 1 ;; esac
  [ -d "$d" ] || { mkdir -p "$d"; return 0; }
  rm -rf -- "${d:?}"/* "${d:?}"/.[!.]* 2>/dev/null
  return 0
}
# A build-region-fast.sh for region $1 running anywhere on the host (its
# cmdline: bash ...build-region-fast.sh <id> <bbox> <name>).
build_running() {
  local p
  for p in $(pgrep -x bash 2>/dev/null); do
    [ "$p" = "$$" ] && continue
    tr '\0' '\n' < "/proc/$p/cmdline" 2>/dev/null | awk -v id="$1" '
      prev ~ /build-region-fast\.sh$/ && $0 == id {found = 1} {prev = $0} END {exit !found}' && return 0
  done
  return 1
}
conflicts_of() {  # regions sharing $1's tiles: its parent, or its variants
  [ -f "$VARIANTS" ] || return 0
  awk -F'\t' -v id="$1" '$1 !~ /^#/ && NF >= 2 { if ($1 == id) print $2; else if ($2 == id) print $1 }' "$VARIANTS"
}
runsh_preparing() {  # $RUNSH is running but has not logged "started driver" since its start
  local p a cwd since
  for p in $(pgrep -x bash 2>/dev/null); do
    cwd=$(readlink "/proc/$p/cwd" 2>/dev/null) || continue
    while IFS= read -r -d '' a; do
      case "$a" in */run.sh|run.sh) ;; *) continue ;; esac
      case "$a" in /*) ;; *) a="$cwd/$a" ;; esac
      [ "$(readlink -f "$a" 2>/dev/null)" = "$RUNSH" ] || continue
      [ -f "$RUNSH_OUT" ] || return 0
      since=$(grep -n 'run\.sh at ' "$RUNSH_OUT" | tail -1 | cut -d: -f1)
      tail -n +"${since:-1}" "$RUNSH_OUT" | grep -q 'started driver' || return 0
    done < "/proc/$p/cmdline"
  done
  return 1
}
stray_listeners() {  # kiwix-serve processes listening on port $1 (pids)
  ss -ltnpH 2>/dev/null | awk -v p=":$1" '$4 ~ p"$"' | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u \
    | while read -r q; do tr '\0' ' ' < "/proc/$q/cmdline" 2>/dev/null | grep -q 'kiwix-serve' && echo "$q"; done
}
scrub_args() {
  local e
  for e in $(compgen -e | grep -E '^(STREETZIM_|LANGPACK_|BUILD_|REBUILD_|NVME_|OVERTURE_|SHIP_|ZIM_BUILDER$|PACK_THREADS$|ZSTD_CLEVEL$|WIKI_IMAGES$|STAGE_MBTILES_NVME$|CLIP$|RAYON_NUM_THREADS$)'); do
    printf -- '-u\n%s\n' "$e"
  done
}
region_env() {  # KEY=VALUE lines of $STATE/env/<id>, validated
  local f="$STATE/env/$1" l
  [ -f "$f" ] || return 0
  while IFS= read -r l; do
    case "$l" in ''|\#*) continue ;; esac
    [[ "$l" =~ ^[A-Z_][A-Z0-9_]*=.*$ ]] || { say "$1: ignoring bad env line '$l'"; continue; }
    printf '%s\n' "$l"
  done < "$f"
}
append_row() {  # the driver's row format, under the driver's lock
  { flock -x 3; printf "%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$(date -Iseconds)" >> "$TSV"; } 3>> "$TSV.lock"
}
status_of() { head -1 "$STATE/status/$1" 2>/dev/null | awk '{print $1}'; }
set_status() { local id=$1; shift; echo "$*" > "$STATE/status/$id.tmp" && mv -f "$STATE/status/$id.tmp" "$STATE/status/$id"; }
cpu_range() {  # worker $1's cores
  local n a b
  n=$(nproc 2>/dev/null || echo 1)
  a=$(( ($1 * CPUS_PER_WORKER) % n )); b=$(( a + CPUS_PER_WORKER - 1 ))
  [ "$b" -lt "$n" ] || b=$(( n - 1 ))
  echo "$a-$b"
}

# ---- --regate REGION ZIM: gates and upload of an already-built ZIM ----------
# For a region whose build finished but whose gates failed for a reason since
# fixed: runs one driver instance with REBUILD_REGATE_ZIM (validate, markers,
# gates, upload -- no extract, tiles or build), on ports outside the workers'
# range, under the upload lock like every upload, appending rows as the driver
# does, and records the result in $PAR_STATE/status so a running runner skips
# the region (an upload-pending result goes to its finisher). Runs in the
# foreground; does not take the runner's lock, so it works while the runner
# runs. Usage: bash ops/cloud/parallel_rebuild.sh --regate iceland osm-iceland-2026-10-23.zim
regate() {
  local id=${1:-} zim=${2:-} ov kw lst rc t0 row st q
  [ -n "$id" ] && [ -n "$zim" ] || die "usage: $0 --regate REGION ZIM"
  zim=$(basename "$zim")
  case "$zim" in "osm-$id-"20??-??-??*.zim) ;; *) die "regate: $zim is not a ZIM of $id" ;; esac
  [ -s "$ROOT/$zim" ] || die "regate: no $ROOT/$zim"
  # A driver without the knob would rebuild the region from scratch.
  grep -q REBUILD_REGATE_ZIM "$(readlink -f "$DRIVER")" 2>/dev/null || die "regate: $DRIVER has no REBUILD_REGATE_ZIM; not starting"
  # One --regate at a time (they share the ports). fd 6 stays open in the
  # driver and its children: the lock is held until the last of them (an
  # upload, a kiwix-serve) is gone.
  exec 6> "$STATE/regate.lock"
  flock -n 6 || die "regate: another --regate holds $STATE/regate.lock"
  [ "$(status_of "$id")" != running ] || die "regate: $id is building in the runner now"
  ov=$(( PORT_BASE + 2 * 16 )); kw=$(( ov + 1 ))
  for q in $(stray_listeners "$ov") $(stray_listeners "$kw"); do
    say "regate: stopping stray kiwix-serve $q on $ov/$kw"; kill "$q" 2>/dev/null || true
  done
  lst="$STATE/lists/regate-$id.list"; printf '%s\n' "$id" > "$lst"
  local -a scrub=()
  mapfile -t scrub < <(scrub_args)
  t0=$(date +%s)
  say "regate $id: $zim (gates and upload only, ports $ov/$kw)"
  set_status "$id" regating - - - "$t0"
  env "${scrub[@]}" -u STREETZIM_ROOT PATH="$DRIVER_PATH" STREETZIM_ML_TILES=1 STREETZIM_REQUIRE_WIKI=1 \
      STREETZIM_KEEP_ZIMS=3 REBUILD_LIST="$lst" REBUILD_PIDFILE="$STATE/pids/regate-$id.pid" \
      REBUILD_LOG="$STATE/logs/$id.regate.log" REBUILD_OV_PORT="$ov" REBUILD_KW_PORT="$kw" \
      REBUILD_NO_SKIP=1 REBUILD_REGATE_ZIM="$zim" \
      bash "$DRIVER" > "$STATE/logs/$id.regate.out" 2>&1 < /dev/null
  rc=$?
  row=$(awk -F'\t' -v id="$id" -v t0="$t0" 'NR > 1 && $1 == id {
          cmd = "date -d \"" $4 "\" +%s"; cmd | getline e; close(cmd)
          if (e + 0 >= t0 - 5) last = $2 } END {print last}' "$TSV" 2>/dev/null)
  st=${row:-no-row}
  case "$st" in
    uploaded) set_status "$id" uploaded regate - - "$t0" "$(date +%s)" "$zim" ;;
    upload-pending) set_status "$id" upload-pending regate - - "$t0" "$(date +%s)" "$zim"
                    printf '%s\n' "$zim" > "$STATE/pending/$id"
                    say "regate $id: listing pending: a running runner's finisher completes it" ;;
    *) set_status "$id" failed regate - - "$t0" "$(date +%s)" "$st" "$zim"
       say "FAILED regate $id: $st (driver rc $rc; log $STATE/logs/$id.regate.log)"; return 1 ;;
  esac
  say "regate $id: $st $zim"
  return 0
}
if [ "${1:-}" = --regate ]; then
  say() { local m; m="$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M:%S %Z') $*"; echo "$m" >> "$OUT"; echo "$m"; }
  regate "${2:-}" "${3:-}"; exit $?
fi
exec 7> "$STATE/run.lock"
# -w: a killed runner's last `sleep` may still hold the lock for a moment.
flock -w 60 7 || die "another parallel_rebuild.sh holds $STATE/run.lock"

# ---- slots ---------------------------------------------------------------
declare -a S_PID S_ID S_T0 S_ST S_PAUSED S_PROJ
for ((k = 0; k < WORKERS; k++)); do S_PID[k]=""; S_ID[k]=""; S_T0[k]=0; S_ST[k]=""; S_PAUSED[k]=0; S_PROJ[k]=0; done
clear_slot() { S_PID[$1]=""; S_ID[$1]=""; S_T0[$1]=0; S_ST[$1]=""; S_PAUSED[$1]=0; S_PROJ[$1]=0; }
running() { local k n=0; for ((k = 0; k < WORKERS; k++)); do [ -n "${S_PID[k]}" ] && n=$((n + 1)); done; echo "$n"; }
big_running() { local k n=0; for ((k = 0; k < WORKERS; k++)); do [ -n "${S_PID[k]}" ] && is_big "${S_ID[k]}" && n=$((n + 1)); done; echo "$n"; }
alone_running() { local k; for ((k = 0; k < WORKERS; k++)); do [ -n "${S_PID[k]}" ] && [[ "$ALONE" == *" ${S_ID[k]} "* ]] && return 0; done; return 1; }
any_paused() { local k; for ((k = 0; k < WORKERS; k++)); do [ -n "${S_PID[k]}" ] && [ "${S_PAUSED[k]}" = 1 ] && return 0; done; return 1; }
alive() { [ -n "$1" ] && [ -d "/proc/$1" ] && [ "$(start_time "$1")" = "$2" ]; }

# ---- resume: re-adopt live drivers, requeue dead ones ----------------------
for f in "$STATE"/status/*; do
  [ -f "$f" ] || continue
  case "$f" in *.tmp|*.mult) continue ;; esac
  st=""; w=""; pid=""; pst=""; t0=""
  read -r st w pid pst t0 _rest < "$f" || true
  [ "$st" = running ] || continue
  id=${f##*/}
  k=${w#w}
  if alive "$pid" "$pst" && [[ "$k" =~ ^[0-9]+$ ]] && [ "$k" -lt "$WORKERS" ] && [ -z "${S_PID[k]}" ]; then
    S_PID[k]=$pid; S_ID[k]=$id; S_ST[k]=$pst; S_T0[k]=${t0:-$(date +%s)}; S_PAUSED[k]=0; S_PROJ[k]=$(proj_gb "$id")
    [ "$(ps -o stat= -p "$pid" 2>/dev/null | cut -c1)" != T ] || S_PAUSED[k]=1
    say "$id: re-adopted running driver $pid in w$k"
  elif alive "$pid" "$pst"; then
    die "driver $pid for $id is alive but cannot be re-adopted into '$w' (PAR_WORKERS=$WORKERS); not starting beside it"
  else
    say "$id: was running in an earlier run (driver $pid gone); requeued"
    set_status "$id" interrupted
  fi
done
for d in "$SCRATCH"/w*; do
  [ -d "$d" ] || continue
  k=${d##*/w}
  case "$k" in ''|*[!0-9]*) continue ;; esac
  [ -z "${S_PID[k]:-}" ] || continue
  [ -z "$(ls -A "$d" 2>/dev/null)" ] || { say "reaping leftover scratch in $d ($(used_gb "$d") GiB)"; wipe_scratch "$k"; }
done

# ---- queue -----------------------------------------------------------------
eligible() {  # $1 region: not done/failed/running
  case "$(status_of "$1")" in
    running|uploaded|upload-pending) return 1 ;;
    regating) ! flock -n "$STATE/regate.lock" true 2>/dev/null && return 1 ;;  # unless that --regate died
    failed) [ "$RETRY_FAILED" = 1 ] || return 1 ;;
  esac
  return 0
}
current_group() {  # the regions of the first group with an eligible region, in order
  awk '/^## / {g++; next} /^[[:space:]]*(#|$)/ {next} {print g+0, $1}' "$QUEUE" | while read -r g id; do
    eligible "$id" && { echo "$g"; break; }
  done | head -1
}
# The next region for slot $1: worker 0 takes the largest (last) eligible
# region of the current group, the others the smallest (first); a region
# sharing tiles with a running build is passed over (WAITING_ON).
pick_region() {
  local slot=$1 g ids id c skip
  WAITING_ON=""
  g=$(current_group); [ -n "$g" ] || return 1
  ids=$(awk -v want="$g" '/^## / {g++; next} /^[[:space:]]*(#|$)/ {next} (g+0) == want {print $1}' "$QUEUE")
  [ "$slot" = 0 ] && [ "$WORKERS" -gt 1 ] && ids=$(echo "$ids" | tac)
  for id in $ids; do
    eligible "$id" || continue
    skip=""
    for c in $(conflicts_of "$id"); do build_running "$c" && skip=$c; done
    if [ -n "$skip" ]; then WAITING_ON="$WAITING_ON $id(shares tiles with $skip)"; continue; fi
    echo "$id"; return 0
  done
  return 1
}

start_region() {  # $1 slot $2 region
  local k=$1 id=$2 ov kw pid lst q cpus scratch_env=()
  wipe_scratch "$k" || return 1
  ov=$(( PORT_BASE + 2 * k )); kw=$(( ov + 1 ))
  for q in $(stray_listeners "$ov") $(stray_listeners "$kw"); do
    say "stopping stray kiwix-serve $q on w$k's ports ($ov/$kw)"; kill "$q" 2>/dev/null || true
  done
  lst="$STATE/lists/w$k.list"; printf '%s\n' "$id" > "$lst"
  : > "$STATE/pids/w$k.pid"
  [[ "$HDD_SCRATCH" == *" $id "* ]] || scratch_env=(BUILD_SCRATCH="$SCRATCH/w$k")
  local -a scrub=() extra=() wiki=()
  mapfile -t scrub < <(scrub_args)
  mapfile -t extra < <(region_env "$id")
  [ "$WIKI_NVME" != 1 ] || ! wiki_ready || wiki=(BUILD_WIKI_ZIM="$WIKI_DST")
  cpus=$(cpu_range "$k")
  setsid taskset -c "$cpus" env "${scrub[@]}" -u STREETZIM_ROOT \
      PATH="$DRIVER_PATH" STREETZIM_WIKI_WAIT_BUDGET=14400 STREETZIM_ML_TILES=1 \
      STREETZIM_REQUIRE_WIKI=1 STREETZIM_WIKI_MAX_PER_MIN="$WIKI_MAX_PER_MIN" STREETZIM_KEEP_ZIMS=3 \
      REBUILD_LIST="$lst" REBUILD_PIDFILE="$STATE/pids/w$k.pid" REBUILD_LOG="$STATE/logs/$id.log" \
      REBUILD_OV_PORT="$ov" REBUILD_KW_PORT="$kw" REBUILD_NO_SKIP=1 REBUILD_STAGE_MBTILES=1 \
      "${scratch_env[@]}" "${wiki[@]}" "${extra[@]}" \
      bash "$DRIVER" 7>&- > "$STATE/logs/$id.driver.out" 2>&1 < /dev/null &
  pid=$!
  sleep 1
  S_PID[k]=$pid; S_ID[k]=$id; S_T0[k]=$(date +%s); S_ST[k]=$(start_time "$pid"); S_PAUSED[k]=0
  S_PROJ[k]=$(proj_gb "$id")
  set_status "$id" running "w$k" "$pid" "${S_ST[k]:-0}" "${S_T0[k]}" "ports=$ov,$kw" "cpus=$cpus" "proj=${S_PROJ[k]}G"
  say "$id: started in w$k (driver $pid, ports $ov/$kw, cpus $cpus, NVMe projection ${S_PROJ[k]} GiB${scratch_env[*]:+}${extra[*]:+, env ${extra[*]}})"
}

finish_slot() {  # $1 slot; the driver is gone
  local k=$1 id=${S_ID[$1]} t0=${S_T0[$1]} row st zim
  row=$(awk -F'\t' -v id="$id" -v t0="$t0" 'NR > 1 && $1 == id {
          cmd = "date -d \"" $4 "\" +%s"; cmd | getline e; close(cmd)
          if (e + 0 >= t0 - 5) last = $2 "\t" $3 } END {print last}' "$TSV" 2>/dev/null)
  st=${row%%$'\t'*}; zim=${row#*$'\t'}
  [ -n "$row" ] || { st=no-row; zim=-; }
  case "$st" in
    uploaded) set_status "$id" uploaded "w$k" - - "$t0" "$(date +%s)" "$zim" ;;
    upload-pending)
      set_status "$id" upload-pending "w$k" - - "$t0" "$(date +%s)" "$zim"
      printf '%s\n' "$zim" > "$STATE/pending/$id"
      say "$id: uploaded, listing pending: the finisher will complete it" ;;
    *) set_status "$id" failed "w$k" - - "$t0" "$(date +%s)" "$st" "$zim"
       say "FAILED $id: $st ${zim:-} (log $STATE/logs/$id.log)"; FAILED="$FAILED $id" ;;
  esac
  say "$id: w$k finished after $(( ($(date +%s) - t0) / 60 )) min: $st ${zim:-}"
  wipe_scratch "$k"
  clear_slot "$k"
}

reap() {
  local k
  for ((k = 0; k < WORKERS; k++)); do
    [ -n "${S_PID[k]}" ] || continue
    alive "${S_PID[k]}" "${S_ST[k]}" && continue
    wait "${S_PID[k]}" 2>/dev/null
    finish_slot "$k"
  done
}

newest_slot() {  # $1: unpaused|any; $2: 1 = only pausable sessions
  local k best=-1 t=0
  for ((k = 0; k < WORKERS; k++)); do
    [ -n "${S_PID[k]}" ] || continue
    [ "$1" = unpaused ] && [ "${S_PAUSED[k]}" = 1 ] && continue
    [ "${2:-0}" = 1 ] && ! pausable "${S_PID[k]}" && continue
    if [ "${S_T0[k]}" -ge "$t" ]; then t=${S_T0[k]}; best=$k; fi
  done
  echo "$best"
}
oldest_paused() {
  local k best=-1 t=99999999999
  for ((k = 0; k < WORKERS; k++)); do
    [ -n "${S_PID[k]}" ] && [ "${S_PAUSED[k]}" = 1 ] || continue
    if [ "${S_T0[k]}" -le "$t" ]; then t=${S_T0[k]}; best=$k; fi
  done
  echo "$best"
}
unpaused_count() { local k n=0; for ((k = 0; k < WORKERS; k++)); do [ -n "${S_PID[k]}" ] && [ "${S_PAUSED[k]}" = 0 ] && n=$((n + 1)); done; echo "$n"; }

last_alert=0
watchdog() {
  local free k id
  free=$(free_gb); [ -n "$free" ] || { say "cannot read NVMe free space"; return; }
  if [ "$free" -lt "$NVME_KILL_GB" ] && [ "$(running)" -gt 0 ]; then
    if [ "$(running)" -lt 2 ]; then
      if [ $(( $(date +%s) - last_alert )) -ge 600 ]; then
        k=$(newest_slot any)
        say "ALERT: NVMe free ${free} GiB < ${NVME_KILL_GB} with one build running (${S_ID[k]}); not stopping the last build -- free space on $NVME by hand if it keeps falling"
        last_alert=$(date +%s)
      fi
      return
    fi
    k=$(newest_slot any); id=${S_ID[k]}
    say "NVMe free ${free} GiB < ${NVME_KILL_GB}: stopping $id in w$k, wiping its scratch, requeueing it with twice the projection"
    signal_session CONT "${S_PID[k]}"; signal_session TERM "${S_PID[k]}"
    for _ in $(seq 1 60); do [ -n "$(session_pids "${S_PID[k]}")" ] || break; sleep 1; done
    signal_session KILL "${S_PID[k]}"
    wait "${S_PID[k]}" 2>/dev/null
    echo $(( $(cat "$STATE/status/$id.mult" 2>/dev/null || echo 1) * 2 )) > "$STATE/status/$id.mult"
    set_status "$id" interrupted "w$k" - - "${S_T0[k]}" "$(date +%s)" nvme-low
    wipe_scratch "$k"
    clear_slot "$k"
    say "$id: stopped, scratch wiped, requeued (projection x$(cat "$STATE/status/$id.mult"))"
    return
  fi
  if [ "$free" -lt "$NVME_MIN_GB" ] && [ "$(unpaused_count)" -gt 1 ]; then
    k=$(newest_slot unpaused 1)
    if [ "$k" -ge 0 ]; then
      signal_session STOP "${S_PID[k]}"; S_PAUSED[k]=1
      say "NVMe free ${free} GiB < ${NVME_MIN_GB}: paused ${S_ID[k]} (w$k)"
    elif [ $(( $(date +%s) - last_alert )) -ge 600 ]; then
      say "NVMe free ${free} GiB < ${NVME_MIN_GB}: no build can be paused now (all hold a lock or are gating/uploading); starting nothing new"
      last_alert=$(date +%s)
    fi
  elif [ "$free" -gt "$NVME_RESUME_GB" ] && any_paused; then
    k=$(oldest_paused)
    signal_session CONT "${S_PID[k]}"; S_PAUSED[k]=0
    say "NVMe free ${free} GiB > ${NVME_RESUME_GB}: resumed ${S_ID[k]} (w$k)"
  fi
}

can_start() {  # $1 region; prints why not, returns 1
  local id=$1 free mem need committed k u resv proj
  any_paused && { echo "a build is paused for NVMe space"; return 1; }
  if [ "$PRECUT" = 1 ] && [[ " $PRECUT_GROUP " == *" $id "* ]] && [ "$(tiles_state "$id")" = pending ]; then
    echo "its tiles are being cut (one at a time, in the background)"; return 1
  fi
  ! code_moved || { echo "the checkout's code changed since ${HEAD0:0:9} (outside ${FREEZE_IGNORE[*]}) during the round"; return 1; }
  if is_big "$id" && [ "$(big_running)" -ge "$BIG_MAX" ]; then echo "$BIG_MAX big regions already running"; return 1; fi
  free=$(free_gb); mem=$(mem_gb); proj=$(proj_gb "$id")
  [ -n "$free" ] && [ -n "$mem" ] || { echo "cannot read free space or memory"; return 1; }
  need=$MEM_MIN_GB; ! is_big "$id" || need=$MEM_BIG_GB
  [ "$mem" -ge "$need" ] || { echo "MemAvailable ${mem} GiB < ${need}"; return 1; }
  committed=0
  for ((k = 0; k < WORKERS; k++)); do
    [ -n "${S_PID[k]}" ] || continue
    u=$(used_gb "$SCRATCH/w$k")
    [ "${S_PROJ[k]}" -le "$u" ] || committed=$(( committed + S_PROJ[k] - u ))
  done
  resv=$(ml_reserved_gb)
  if [ $(( free - committed - resv - proj )) -lt "$NVME_MIN_GB" ]; then
    echo "NVMe: free ${free} - still to write ${committed} - tile stores ${resv} - ${id} ${proj} < ${NVME_MIN_GB} GiB"; return 1
  fi
  return 0
}

write_table() {
  local id st w t0 t1 rest line
  {
    printf '# %s  workers=%s  NVMe free=%s GiB  MemAvailable=%s GiB  HEAD=%s\n' \
      "$(TZ=America/Los_Angeles date '+%F %T %Z')" "$WORKERS" "$(free_gb)" "$(mem_gb)" "${HEAD0:0:9}"
    printf '%-28s %-15s %-4s %-11s %-11s %6s  %s\n' region state slot "start PDT" "end PDT" min info
    while read -r id _; do
      case "$id" in ''|\#*) continue ;; esac
      line=$(head -1 "$STATE/status/$id" 2>/dev/null)
      if [ -z "$line" ]; then printf '%-28s %s\n' "$id" queued; continue; fi
      read -r st w _p _s t0 t1 rest <<< "$line"
      case "$st" in running) rest="$t1 $rest"; t1=0 ;; esac
      [[ "$t0" =~ ^[0-9]+$ ]] || t0=0; [[ "${t1:-0}" =~ ^[0-9]+$ ]] || t1=0
      printf '%-28s %-15s %-4s %-11s %-11s %6s  %s\n' "$id" "$st" "${w:--}" "$(pdt "$t0")" "$(pdt "${t1:-0}")" \
        "$( [ "$t0" != 0 ] && echo $(( ( $( [ "${t1:-0}" != 0 ] && echo "$t1" || date +%s ) - t0 ) / 60 )) || echo -)" "${rest:-}"
    done < "$QUEUE"
  } > "$STATE/status.txt.tmp" && mv -f "$STATE/status.txt.tmp" "$STATE/status.txt"
}

# ---- the English Wikipedia ZIM on the NVMe ---------------------------------
WIKI_DST="$WIKI_DIR/$(basename "$WIKI_SRC")"
wiki_ready() {  # the verified copy, if it matches the original
  local size
  size=$(stat -c %s "$WIKI_SRC" 2>/dev/null) || return 1
  [ -f "$WIKI_DST" ] && [ -f "$WIKI_DST.verified" ] && [ "$(stat -c %s "$WIKI_DST")" = "$size" ] \
    && [ "$(cat "$WIKI_DST.verified")" = "$size $(stat -c %Y "$WIKI_SRC")" ]
}
wiki_copy() {
  local dst=$WIKI_DST size free need srcmd5 dstmd5
  size=$(stat -c %s "$WIKI_SRC" 2>/dev/null) || { say "wiki copy: no $WIKI_SRC; builds read /storage"; return; }
  mkdir -p "$WIKI_DIR" || { say "wiki copy: cannot create $WIKI_DIR"; return; }
  if wiki_ready; then say "wiki copy: using verified $dst"; return; fi
  if [ -e "$dst.part" ] && fuser -s "$dst.part" 2>/dev/null; then
    say "wiki copy: $dst.part is still open by another process (an earlier copy?); not starting another; builds read /storage"; return
  fi
  rm -f "$dst" "$dst.part" "$dst.verified" "$dst.srcmd5"
  free=$(free_gb); need=$(( $(gib_up "$size") + NVME_MIN_GB + 50 ))
  if [ "${free:-0}" -lt "$need" ]; then say "wiki copy: NVMe free ${free} GiB < ${need}; builds read /storage"; return; fi
  say "wiki copy: copying $(gib_up "$size") GiB to $dst (md5 of the stream as it is read)"
  if ! { cat "$WIKI_SRC" | tee >(md5sum | cut -d' ' -f1 > "$dst.srcmd5") > "$dst.part"; }; then
    say "wiki copy: copy failed; builds read /storage"; rm -f "$dst.part" "$dst.srcmd5"; return
  fi
  for _ in $(seq 1 30); do [ -s "$dst.srcmd5" ] && break; sleep 1; done
  srcmd5=$(cat "$dst.srcmd5" 2>/dev/null); dstmd5=$(md5sum "$dst.part" | cut -d' ' -f1)
  if [ -z "$srcmd5" ] || [ "$srcmd5" != "$dstmd5" ] || [ "$(stat -c %s "$dst.part")" != "$size" ]; then
    say "wiki copy: verification failed (stream $srcmd5, copy $dstmd5); builds read /storage"; rm -f "$dst.part" "$dst.srcmd5"; return
  fi
  mv -f "$dst.part" "$dst" && echo "$size $(stat -c %Y "$WIKI_SRC")" > "$dst.verified"
  say "wiki copy: done, md5 $dstmd5 verified; builds started from now read it"
}

# ---- tiles for the first group, in the background -------------------------
PRECUT_GROUP=""
precut_order() {  # the first group's eligible regions in the order the workers take them
  local g ids
  g=$(current_group); [ -n "$g" ] || return 0
  ids=$(awk -v want="$g" '/^## / {g++; next} /^[[:space:]]*(#|$)/ {next} (g+0) == want {print $1}' "$QUEUE" \
        | while read -r id; do eligible "$id" && echo "$id"; done)
  # Each round: the small regions the other workers take next, then the
  # large one worker 0 takes, so small maps never idle behind a big cut.
  echo "$ids" | awk -v w="$WORKERS" 'NF {a[n++] = $1} END {
      i = 0; j = n - 1; small = (w > 1 ? w - 1 : 1)
      while (i <= j) {
        for (k = 0; k < small && i <= j; k++) print a[i++]
        if (w > 1 && i <= j) print a[j--]
      }}'
}
tiles_state() { local f; for f in ok skip fail; do [ -e "$STATE/tiles/$1.$f" ] && { echo "$f"; return; }; done; echo pending; }
precut() {
  local id src bbox pbf rc
  mkdir -p "$STATE/tiles"
  [ -f "$ROOT/ops/region-bbox.sh" ] && . "$ROOT/ops/region-bbox.sh"
  for id in $PRECUT_GROUP; do
    [ "$(tiles_state "$id")" = pending ] || continue
    [ -e "$STATE/STOP" ] && return 0
    if [ "$(status_of "$id")" = running ]; then : > "$STATE/tiles/$id.skip"; continue; fi
    src=$(src_of "$id")
    bbox=$(awk -F'\t' -v id="$id" '$1 == id {print $3; exit}' "$REGISTRY" 2>/dev/null)
    if [ -z "$bbox" ]; then say "tiles $id: no registry row; left to its build"; : > "$STATE/tiles/$id.skip"; continue; fi
    pbf="$ROOT/world-data/regions/$src.osm.pbf"
    if [ ! -s "$pbf" ] || { type bbox_stale > /dev/null 2>&1 && bbox_stale "$pbf" "$bbox"; } \
       || { type pbf_complete > /dev/null 2>&1 && ! pbf_complete "$pbf"; }; then
      say "tiles $id: extract missing, cut for another box or without complete multipolygons; left to its build"
      : > "$STATE/tiles/$id.skip"; continue
    fi
    local -a scrub=()
    mapfile -t scrub < <(scrub_args)
    if [ -n "$PRECUT_CMD" ]; then
      env PRECUT_ID="$id" PRECUT_SRC="$src" PRECUT_BBOX="$bbox" bash -c "$PRECUT_CMD" >> "$STATE/logs/tiles.log" 2>&1
    else
      (cd "$ROOT" && env "${scrub[@]}" PATH="$DRIVER_PATH" STREETZIM_ML_TILES=1 \
         "$PY" ops/region-ml-tiles.py --src-id "$src" -- "$id" "$bbox") >> "$STATE/logs/tiles.log" 2>&1
    fi
    rc=$?
    if [ "$rc" = 0 ]; then : > "$STATE/tiles/$id.ok"; else : > "$STATE/tiles/$id.fail"; fi
    say "tiles $id: rc=$rc ($(tail -1 "$STATE/logs/tiles.log" 2>/dev/null | cut -c1-120))"
  done
}

# ---- finishing upload-pending regions --------------------------------------
pending_loop() {  # runs in the background until the runner exits
  local parent=$1 f id zim md5 rc tries
  while [ -d "/proc/$parent" ]; do
    for f in "$STATE"/pending/*; do
      [ -f "$f" ] || continue
      case "$f" in *.md5|*.tries) continue ;; esac
      id=${f##*/}; zim=$(head -1 "$f")
      [ -f "$ROOT/$zim" ] || { say "pending $id: $zim is gone from $ROOT; giving up"; rm -f "$f"*; continue; }
      [ -s "$f.md5" ] || md5sum "$ROOT/$zim" | cut -d' ' -f1 > "$f.md5"
      md5=$(cat "$f.md5")
      if [ -n "${PAR_LISTED_CMD:-}" ]; then
        env LISTED_ID="$id" LISTED_ZIM="$zim" LISTED_MD5="$md5" bash -c "$PAR_LISTED_CMD" || continue
      else
        "$IA" metadata "streetzim-$id" 2>/dev/null | "$PY" "$ROOT/ops/cloud/archive_has_file.py" "$zim" "$md5" || continue
      fi
      say "pending $id: archive.org lists $zim with md5 $md5; upload_validated.sh again"
      local -a scrub=()
      mapfile -t scrub < <(scrub_args)
      if [ -n "${PAR_FINISH_CMD:-}" ]; then
        env "${scrub[@]}" PATH="$DRIVER_PATH" FINISH_ID="$id" FINISH_ZIM="$zim" STREETZIM_KEEP_ZIMS=3 \
          bash -c "$PAR_FINISH_CMD"; rc=$?
      else
        (cd "$ROOT" && flock .retrofit-upload.lock env "${scrub[@]}" PATH="$DRIVER_PATH" PROJECT_DIR="$ROOT" \
           TERRAIN_STRIPE_TOLERATE=10 STREETZIM_KEEP_ZIMS=3 bash "$UPLOADER" "$id" "$zim") >> "$STATE/logs/$id.log" 2>&1; rc=$?
      fi
      if [ "$rc" = 0 ]; then
        append_row "$id" uploaded "$zim"
        set_status "$id" uploaded - - - "$(awk '{print $6}' "$STATE/status/$id" 2>/dev/null || echo 0)" "$(date +%s)" "$zim"
        rm -f "$f" "$f.md5" "$f.tries"
        say "pending $id: done, uploaded"
      else
        tries=$(( $(cat "$f.tries" 2>/dev/null || echo 0) + 1 )); echo "$tries" > "$f.tries"
        say "pending $id: upload_validated.sh rc=$rc (try $tries)"
        if [ "$tries" -ge 5 ]; then
          say "FAILED pending $id: upload_validated.sh failed 5 times; finish by hand"
          set_status "$id" failed - - - 0 "$(date +%s)" finish-failed "$zim"; rm -f "$f" "$f.md5" "$f.tries"
        fi
      fi
    done
    sleep "$PENDING_EVERY"
  done
}
pending_count() { local n=0 f; for f in "$STATE"/pending/*; do [ -f "$f" ] || continue; case "$f" in *.md5|*.tries) ;; *) n=$((n+1)) ;; esac; done; echo "$n"; }

# ---- main ------------------------------------------------------------------
say "parallel_rebuild.sh at HEAD ${HEAD0:0:9}: $WORKERS workers, scratch $SCRATCH, queue $(grep -cvE '^[[:space:]]*(#|$)' "$QUEUE") regions, driver $DRIVER"
say "PATH for the drivers: $DRIVER_PATH"
for _t in ia docker osmium firebase node; do say "  $_t -> $(PATH="$DRIVER_PATH" command -v "$_t" || echo 'NOT FOUND')"; done
[ "${PAR_MEMWATCH_OFF:-0}" = 1 ] || { [ -f "$MEMWATCH" ] && setsid nohup bash "$MEMWATCH" $$ 7>&- > /dev/null 2>&1 < /dev/null & }
# Background jobs run in process groups of their own (job control on while
# they start), so the exit trap can stop each with everything it started.
BG_PGIDS=""
bg_start() { set -m; "$@" 7>&- & BG_PGIDS="$BG_PGIDS $!"; set +m; }
bg_stop() { local g; for g in $BG_PGIDS; do kill -- "-$g" 2>/dev/null || true; done; }
trap bg_stop EXIT
bg_start pending_loop $$
while runsh_preparing; do say "waiting: sz-tests/rebuild-all/run.sh is preparing its driver"; sleep "$POLL" 7>&-; done
[ "$WIKI_NVME" != 1 ] || bg_start wiki_copy
if [ "$PRECUT" = 1 ]; then
  PRECUT_GROUP=$(precut_order | tr '\n' ' ')
  rm -rf "$STATE/tiles"; mkdir -p "$STATE/tiles"
  say "tiles: cutting in this order: $PRECUT_GROUP"
  bg_start precut
fi
last_start=0
blocked=""
FAILED=""
while :; do
  reap
  watchdog
  write_table
  if [ -e "$STATE/STOP" ]; then
    [ "$(running)" -gt 0 ] || { say "STOP file: no build running; exiting"; break; }
    sleep "$POLL" 7>&-; continue
  fi
  started=""; why=""; nxt=""; any_free=""; any_cand=""; WAITING_ALL=""
  # Every free slot is offered its region in turn, so a slot whose region
  # must wait (its tiles, a variant's parent) does not hold up the others.
  for ((k = 0; k < WORKERS; k++)); do
    [ -z "${S_PID[k]}" ] || continue
    any_free=1
    nxt=$(pick_region "$k") || nxt=""
    if [ -z "$nxt" ]; then pick_region "$k" > /dev/null || true; WAITING_ALL="$WAITING_ALL$WAITING_ON"; continue; fi
    any_cand=1
    if alone_running; then why="a region that runs alone is running"
    elif [[ "$ALONE" == *" $nxt "* ]] && [ "$(running)" -gt 0 ]; then why="$nxt runs alone: draining $(running) build(s)"
    elif runsh_preparing; then why="sz-tests/rebuild-all/run.sh is preparing its driver (rewrites rebuild-old.tsv)"
    elif [ $(( $(date +%s) - last_start )) -lt "$STAGGER" ]; then why="spacing starts"
    else why=$(can_start "$nxt") && why=""
    fi
    if [ -z "$why" ]; then
      start_region "$k" "$nxt" && last_start=$(date +%s)
      started=1; blocked=""; break
    fi
    if [ "$why" != "$blocked" ] && [ "$why" != "spacing starts" ]; then
      say "$nxt waits: $why"; blocked=$why
    fi
  done
  if [ -n "$any_free" ] && [ -z "$any_cand" ] && [ -z "$started" ] && [ "$(running)" -eq 0 ] && [ -z "$WAITING_ALL" ]; then
    if [ "$(pending_count)" -gt 0 ]; then sleep "$POLL" 7>&-; continue; fi
    say "queue done"
    [ "$WIKI_NVME" != 1 ] || say "the round's queue is empty: free the NVMe Wikipedia copy with: bash $0 --cleanup-wiki"
    break
  fi
  sleep "$POLL" 7>&-
done
write_table
[ -z "$FAILED" ] || say "FAILED regions this run:$FAILED"
say "summary: $(for f in "$STATE"/status/*; do case "$f" in *.tmp|*.mult) continue ;; esac; [ -f "$f" ] && awk '{print $1}' "$f"; done | sort | uniq -c | awk '{printf "%s=%s ", $2, $1}')"
[ -z "$FAILED" ] || exit 1
exit 0
