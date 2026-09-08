#!/usr/bin/env bash
# Complete regions whose ZIM transferred but which archive.org had not yet
# listed (upload_validated exit 6 → pending-uploads.tsv).
#
# archive.org lists a file only after its archive.php task runs, and that
# queue can sit for hours. Re-running upload_validated with --checksum
# skips the transfer entirely and just does the metadata stamp, torrent,
# prune and deploy. Safe to run repeatedly; still-pending rows stay.
set -uo pipefail
cd /storage/streetzim
PENDING=pending-uploads.tsv
[ -s "$PENDING" ] || { echo "nothing pending"; exit 0; }
LOG=/storage/streetzim/finish-pending.log
still=$(mktemp "${TMPDIR:-/storage/streetzim/tmp}/pending.XXXXXX")
while IFS=$'\t' read -r id zim when; do
  [ -n "${id:-}" ] || continue
  [ -s "$zim" ] || { echo "$id: $zim gone locally — dropping" | tee -a "$LOG"; continue; }
  listed=$(venv-linux/bin/ia metadata "streetzim-$id" 2>/dev/null \
    | venv-linux/bin/python3 -c "import sys,json;m=json.load(sys.stdin);print('yes' if any(f.get('name')==sys.argv[1] for f in m.get('files',[])) else 'no')" "$zim" 2>/dev/null)
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
    printf '%s\t%s\t%s\n' "$id" "$zim" "$when" >> "$still"
  fi
done < "$PENDING"
mv -f "$still" "$PENDING"
echo "remaining pending: $(wc -l < "$PENDING")"
