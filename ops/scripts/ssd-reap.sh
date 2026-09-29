#!/usr/bin/env bash
# Reclaim SSD scratch space left behind by a tile build.
#
# POLICY: /mnt/data is an NVMe shared with another project. Nothing of
# ours rests there — it holds only the tilemaker node/way store, which
# exists for the duration of one run. Four things enforce that:
#   1. build-world-tiles.sh traps EXIT/TERM/INT and wipes the store.
#   2. an flock stops a second run from sharing (or deleting) the store.
#   3. scripts/nvme-guard.sh stops the build before free space runs out.
#   4. this reaper covers what a trap cannot: SIGKILL, an OOM kill of the
#      wrapper, or a reboot. Run it after any hard stop, and before
#      starting a new tile build.
#
# It only ever removes the store directory, and only when no tilemaker
# container is running. It never touches anything else under /mnt/data.
#
# Usage: ./scripts/ssd-reap.sh [--force]
#        (without --force it reports and asks nothing, deleting only a
#         store that is provably orphaned)
set -uo pipefail
STORE="${STORE:-/mnt/data/tilemaker/store}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

printf 'SSD (%s):\n' "$(dirname "$STORE")"
df -h "$(dirname "$STORE")" | tail -1 | awk '{printf "  %s total, %s used, %s free (%s)\n", $2, $3, $4, $5}'
for d in "$(dirname "$STORE")"/../*; do
    [ -d "$d" ] || continue
    printf '  %-28s %s\n' "$(basename "$d")" "$(du -sh "$d" 2>/dev/null | cut -f1)"
done

if [ ! -d "$STORE" ]; then
    echo "no tilemaker store present — nothing to reap"
    exit 0
fi
SIZE=$(du -sh "$STORE" 2>/dev/null | cut -f1)

RUNNING=$(docker ps -q --filter name=streetzim-tilemaker 2>/dev/null | head -1)
WRAPPER=$(pgrep -f 'build-world-tiles.sh' | head -1)
if [ -n "$RUNNING" ] || [ -n "$WRAPPER" ]; then
    echo
    echo "store is $SIZE and IN USE (container=${RUNNING:-none} wrapper=${WRAPPER:-none}) — not touching it."
    echo "It is wiped automatically when that run ends."
    [ "$FORCE" = "1" ] && echo "(--force ignored: refusing to delete a live run's scratch)"
    exit 1
fi

echo
echo "ORPHANED store: $SIZE at $STORE (no tilemaker container, no wrapper)"
echo "removing..."
rm -rf "$STORE"
rmdir "$(dirname "$STORE")" 2>/dev/null || true
df -h "$(dirname "$STORE")" 2>/dev/null | tail -1 | awk '{printf "reclaimed — now %s free\n", $4}' \
  || df -h /mnt/data | tail -1 | awk '{printf "reclaimed — now %s free\n", $4}'
