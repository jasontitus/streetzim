#!/usr/bin/env bash
# ops split, stage 1: this file lives in ops/ and a symlink at its old path
# runs it; the paths below assume that path (and so do pgrep and the locks).
# Started directly (ops/..., or from inside ops/), it re-runs by the old path.
if [ ! -L "$0" ] && _ops_real="$(readlink -f "$0" 2>/dev/null)"; then
  case "$_ops_real" in
    */ops/*) _ops_old="${_ops_real%/ops/*}/${_ops_real##*/ops/}"
             if [ -L "$_ops_old" ]; then exec bash "$_ops_old" "$@"; fi ;;
  esac
fi
unset _ops_real _ops_old
# Complete regions whose ZIM transferred but which archive.org had not yet
# listed (upload_validated exit 6 → pending-uploads.tsv).
#
# archive.org lists a file only after its archive.php task runs, and that
# queue can sit for hours. A row is complete once archive.org lists the file
# WITH ITS MD5: a same-named re-upload (a viewer patch) is listed by name at
# once, as the previous file, and re-running upload_validated then would send
# the whole ZIM again. Listed with its md5, the re-run skips the transfer
# (archive_has_file.py) and does the metadata stamp, torrent, prune and
# deploy. Safe to run repeatedly; still-pending rows stay.
#
# The md5 is the row's 4th column (upload_validated writes it) unless the
# file changed after the row was written (patched again in place: the row's
# md5 would never be listed), or the row predates the column; then it is
# computed, only for rows a later build has not superseded. "Changed" is the
# inode's ctime, not the mtime: a patch that keeps the old mtime (touch -r)
# still moves the ctime. (A new hard link moves it too; that costs a re-read
# per run, nothing worse.)
#
# The LAST row per id/zim decides: a file re-uploaded while pending gets a
# newer row with the new md5, and the older row is dropped with it.
#
# Other jobs (the build queue, retrofit-chips-queue.sh) append rows while
# this runs — it can wait hours for a build gap — so the rewrite at the end
# keeps every row added after the start instead of replacing the file with
# this run's leftovers. One finisher at a time (flock).
#
# STREETZIM_ROOT, IA, PYTHON and STREETZIM_FINISH_NO_BUILD_WAIT=1 are for
# tests (ops/tests/test_finish_pending_e2e.py); production sets none of them.
set -uo pipefail
ROOT=${STREETZIM_ROOT:-/storage/streetzim}
cd "$ROOT" || exit 1
IA=${IA:-$ROOT/venv-linux/bin/ia}
PYTHON=${PYTHON:-$ROOT/venv-linux/bin/python3}
PENDING=pending-uploads.tsv
LOCK=$ROOT/.finish-pending.lock
exec 9> "$LOCK"
flock -n 9 || { echo "another finish_pending_uploads.sh is running"; exit 0; }
[ -s "$PENDING" ] || { echo "nothing pending"; exit 0; }
LOG=$ROOT/finish-pending.log
# One `ia metadata` answer, two questions: is this ZIM listed with this md5
# (argv 2; empty: never listed), and has a LATER build of the same region
# already been listed? A superseded row is not just wasted work -- completing
# it rebuilds web/torrents/<id>.torrent from the older file, which makes
# generate.py drop the region's Torrent button, and then cleanup_old_zims
# --keep 2 deletes the very file the row was about. Observed 2026-09-27 with
# southeast-asia 09-17 behind 09-20. (No single quotes in here.)
STATE_PY='
import sys, json, re
want = sys.argv[1]
md5 = (sys.argv[2] if len(sys.argv) > 2 else "").strip().lower()
files = [f for f in json.load(sys.stdin).get("files", []) if isinstance(f, dict)]
names = [f.get("name", "") for f in files]
# Listed = listed with this md5 (the archive_has_file.py rule: the first entry
# of that name decides).
entry = next((f for f in files if f.get("name") == want), None)
listed = bool(re.match(r"^[0-9a-f]{32}$", md5)) and entry is not None \
    and str(entry.get("md5", "")).strip().lower() == md5
def stamp(n):
    m = re.match(r"osm-.+-(\d{4}-\d{2}-\d{2})([a-z]?)\.zim$", n)
    return (m.group(1), m.group(2)) if m else None
mine = stamp(want)
newer = sorted((n for n in names if stamp(n) and mine and stamp(n) > mine), key=stamp)
print(("yes" if listed else "no") + "\t" + (newer[-1] if newer else ""))
'
n0=$(wc -l < "$PENDING")
still=$(mktemp "${TMPDIR:-$ROOT/tmp}/pending.XXXXXX")
declare -A seen=()
while IFS=$'\t' read -r id zim when md5; do
  [ -n "${id:-}" ] || continue
  [ -n "${seen[$id/$zim]:-}" ] && continue      # read newest first (tac)
  seen[$id/$zim]=1
  [ -s "$zim" ] || { echo "$id: $zim gone locally — dropping" | tee -a "$LOG"; continue; }
  meta=$("$IA" metadata "streetzim-$id" 2>/dev/null)
  state=$(printf '%s' "$meta" | "$PYTHON" -c "$STATE_PY" "$zim" "" 2>/dev/null)
  superseded=${state#*$'\t'}
  if [ -n "$superseded" ]; then
    echo "$id: $zim superseded by $superseded — dropping" | tee -a "$LOG"
    continue
  fi
  # The row's md5, unless the file changed (ctime) after the row was written.
  t_row=""   # (`date -d ""` is today's midnight, not an error)
  [ -n "${when:-}" ] && t_row=$(date -d "$when" +%s 2>/dev/null) || t_row=""
  if ! [[ "${md5:-}" =~ ^[0-9a-f]{32}$ ]] || [ -z "$t_row" ] \
      || [ "$(stat -c %Z "$zim")" -gt "$t_row" ]; then
    md5=$(ionice -c3 nice -n10 md5sum "$zim" | cut -d' ' -f1)
  fi
  state=$(printf '%s' "$meta" | "$PYTHON" -c "$STATE_PY" "$zim" "$md5" 2>/dev/null)
  listed=${state%%$'\t'*}
  if [ "$listed" != "yes" ]; then
    echo "$id: not listed with md5 ${md5:-?} yet (queued since $when)" | tee -a "$LOG"
    printf '%s\t%s\t%s\t%s\n' "$id" "$zim" "$when" "$md5" >> "$still"
    continue
  fi
  # Wait for a build gap so this never competes for the disk.
  if [ -z "${STREETZIM_FINISH_NO_BUILD_WAIT:-}" ]; then
    while pgrep -f '[c]reate_osm_zim' >/dev/null; do sleep 60; done
  fi
  echo "=== $id listed with md5 $md5 — completing prune/torrent/deploy" | tee -a "$LOG"
  if PROJECT_DIR="$ROOT" TERRAIN_STRIPE_TOLERATE=10 \
       bash cloud/upload_validated.sh "$id" "$zim" >> "$LOG" 2>&1; then
    echo "  $id done" | tee -a "$LOG"
  else
    rc=$?; echo "  $id rc=$rc — keeping it pending" | tee -a "$LOG"
    # upload_validated exit 6 has already appended its own (newer) row.
    [ "$rc" -eq 6 ] || printf '%s\t%s\t%s\t%s\n' "$id" "$zim" "$when" "$md5" >> "$still"
  fi
done < <(head -n "$n0" "$PENDING" | tac)
# Rows appended after this run started (including exit-6 re-appends above)
# come last, so the keep-last dedupe below lets them win.
tail -n +"$((n0 + 1))" "$PENDING" >> "$still"
tac "$still" | awk -F'\t' '!seen[$1 "/" $2]++' | tac > "$still.dedup" && mv -f "$still.dedup" "$still"
mv -f "$still" "$PENDING"
echo "remaining pending: $(wc -l < "$PENDING")"
