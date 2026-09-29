#!/usr/bin/env bash
# Complete regions whose ZIM transferred but which archive.org had not yet
# listed (upload_validated exit 6 → pending-uploads.tsv).
#
# archive.org lists a file only after its archive.php task runs, and that
# queue can sit for hours. Re-running upload_validated with --checksum
# skips the transfer entirely and just does the metadata stamp, torrent,
# prune and deploy. Safe to run repeatedly; still-pending rows stay.
#
# Other jobs (the build queue, retrofit-chips-queue.sh) append rows while
# this runs — it can wait hours for a build gap — so the rewrite at the end
# keeps every row added after the start instead of replacing the file with
# this run's leftovers. One finisher at a time (flock).
set -uo pipefail
cd /storage/streetzim
PENDING=pending-uploads.tsv
LOCK=/storage/streetzim/.finish-pending.lock
exec 9> "$LOCK"
flock -n 9 || { echo "another finish_pending_uploads.sh is running"; exit 0; }
[ -s "$PENDING" ] || { echo "nothing pending"; exit 0; }
LOG=/storage/streetzim/finish-pending.log
n0=$(wc -l < "$PENDING")
still=$(mktemp "${TMPDIR:-/storage/streetzim/tmp}/pending.XXXXXX")
declare -A seen=()
while IFS=$'\t' read -r id zim when; do
  [ -n "${id:-}" ] || continue
  [ -n "${seen[$id/$zim]:-}" ] && continue
  seen[$id/$zim]=1
  [ -s "$zim" ] || { echo "$id: $zim gone locally — dropping" | tee -a "$LOG"; continue; }
  # One `ia metadata` call answers both questions: is this ZIM listed, and has
  # a LATER build of the same region already been listed? A superseded row is
  # not just wasted work -- completing it rebuilds web/torrents/<id>.torrent
  # from the older file, which makes generate.py drop the region's Torrent
  # button, and then cleanup_old_zims --keep 2 deletes the very file the row
  # was about. Observed 2026-09-27 with southeast-asia 09-17 behind 09-20.
  state=$(venv-linux/bin/ia metadata "streetzim-$id" 2>/dev/null \
    | venv-linux/bin/python3 -c '
import sys, json, re
want = sys.argv[1]
names = [f.get("name", "") for f in json.load(sys.stdin).get("files", [])]
def stamp(n):
    m = re.match(r"osm-.+-(\d{4}-\d{2}-\d{2})([a-z]?)\.zim$", n)
    return (m.group(1), m.group(2)) if m else None
mine = stamp(want)
newer = sorted((n for n in names if stamp(n) and mine and stamp(n) > mine), key=stamp)
print(("yes" if want in names else "no") + "\t" + (newer[-1] if newer else ""))
' "$zim" 2>/dev/null)
  listed=${state%%$'\t'*}
  superseded=${state#*$'\t'}
  if [ -n "$superseded" ]; then
    echo "$id: $zim superseded by $superseded — dropping" | tee -a "$LOG"
    continue
  fi
  if [ "$listed" != "yes" ]; then
    echo "$id: still not listed (queued since $when)" | tee -a "$LOG"
    printf '%s\t%s\t%s\n' "$id" "$zim" "$when" >> "$still"
    continue
  fi
  # Wait for a build gap so this never competes for the disk.
  while pgrep -f '[c]reate_osm_zim' >/dev/null; do sleep 60; done
  echo "=== $id listed — completing prune/torrent/deploy" | tee -a "$LOG"
  if PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 \
       bash cloud/upload_validated.sh "$id" "$zim" >> "$LOG" 2>&1; then
    echo "  $id done" | tee -a "$LOG"
  else
    rc=$?; echo "  $id rc=$rc — keeping it pending" | tee -a "$LOG"
    # upload_validated exit 6 has already appended its own row again; a
    # second copy would only be dropped as a duplicate on the next run.
    [ "$rc" -eq 6 ] || printf '%s\t%s\t%s\n' "$id" "$zim" "$when" >> "$still"
  fi
done < <(head -n "$n0" "$PENDING")
# Rows appended after this run started (including exit-6 re-appends above).
tail -n +"$((n0 + 1))" "$PENDING" >> "$still"
awk -F'\t' '!seen[$1 "/" $2]++' "$still" > "$still.dedup" && mv -f "$still.dedup" "$still"
mv -f "$still" "$PENDING"
echo "remaining pending: $(wc -l < "$PENDING")"
