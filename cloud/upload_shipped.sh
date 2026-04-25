#!/usr/bin/env bash
# Watch for osm-*-shipped.zim files to appear (from a running
# rollout) and upload each to archive.org as soon as it lands +
# passes validation. Runs until every expected region is handled
# or the caller kills it.
#
# Runs sequentially — the ia CLI rate-limits parallel uploads from
# one account. Each upload includes metadata stamp + old-ZIM prune
# + web/generate.py --deploy via cloud/upload_validated.sh.
#
# Usage:
#   bash cloud/upload_shipped.sh              # all 10 shipped regions
#   bash cloud/upload_shipped.sh texas iran   # just these two
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Expected regions (must match the rollout_viewer_swap.sh manifest).
if [ $# -gt 0 ]; then
    regions=("$@")
else
    regions=(
        japan iran australia-nz texas central-us
        east-coast-us west-coast-us west-asia central-asia egypt
    )
fi

today=$(date +%Y-%m-%d)
log() { printf "[upload %s] %s\n" "$(date '+%H:%M:%S')" "$*"; }

already_done=()
for id in "${regions[@]}"; do
    log "waiting for osm-${id}-shipped.zim..."
    shipped="osm-${id}-shipped.zim"
    # Wait up to 2h for the file to appear + be stable (2 mtimes apart).
    waited=0
    while [ $waited -lt 7200 ]; do
        if [ -s "$shipped" ]; then
            # Require the file to be stable (unchanged for 30 s) so we
            # don't upload mid-write.
            sz1=$(stat -f %z "$shipped" 2>/dev/null || echo 0)
            sleep 30; waited=$((waited+30))
            sz2=$(stat -f %z "$shipped" 2>/dev/null || echo 0)
            if [ "$sz1" = "$sz2" ] && [ "$sz1" != "0" ]; then
                break
            fi
            continue
        fi
        sleep 30; waited=$((waited+30))
    done
    if [ ! -s "$shipped" ]; then
        log "TIMEOUT waiting for $shipped — skipping"
        continue
    fi
    dated="osm-${id}-${today}.zim"
    if [ ! -f "$dated" ] || [ "$dated" -ot "$shipped" ]; then
        log "staging ${id} → ${dated}"
        cp "$shipped" "$dated"
    fi
    log "uploading ${id}..."
    if ! bash cloud/upload_validated.sh "$id" "$dated" \
            > "${id}-upload.log" 2>&1; then
        log "UPLOAD FAILED for ${id} — see ${id}-upload.log"
        continue
    fi
    already_done+=("$id")
    log "DONE ${id}"
done

log "finished: uploaded ${#already_done[@]} / ${#regions[@]} (${already_done[*]})"
