#!/usr/bin/env bash
# Rebuild the four regions that shipped without Overture enrichment.
# Each rebuild goes through cloud/build_region.sh, which runs preflight +
# create_osm_zim (with --overture-places/--overture-addresses, spatial
# routing, --split-find-chips, world-VRT low-zoom terrain) + validator.
#
# Why these four: build-and-upload-queue.sh (obsolete) produced them
# without the Overture flags, so Find-page rows show no websites/phones/
# socials. A repackage pass can't add those — the data was never built in.
#
# Sequential by default. Each build is ~4-10 hours; running in parallel
# would OOM this mac and fight for libzim compression threads.
#
# Usage:
#   bash cloud/rebuild_overture_regions.sh          # rebuild all 4
#   bash cloud/rebuild_overture_regions.sh id1 id2  # only these ids
#
# Resume-friendly: if osm-{id}.zim already exists and validates, skips it.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

log() { printf "[rebuild %s] %s\n" "$(date '+%H:%M:%S')" "$*"; }

# ------------------------------------------------------------------
# Region manifest. Columns: id, display name, bbox.
# bboxes pulled from prior successful rebuild-queue.log runs.
# ------------------------------------------------------------------
regions=(
  "australia-nz   Australia & New Zealand   112.0,-48.0,179.0,-10.0"
  "west-coast-us  West Coast US             -125.0,32.0,-116.5,49.5"
  "east-coast-us  East Coast US             -82.0,24.0,-66.5,47.6"
  "west-asia      West Asia                 25.0,12.0,63.0,45.0"
)

# ------------------------------------------------------------------
# Filter to requested ids (or all).
# ------------------------------------------------------------------
requested=("$@")
if [ ${#requested[@]} -eq 0 ]; then
    targets=("${regions[@]}")
else
    targets=()
    for want in "${requested[@]}"; do
        found=""
        for row in "${regions[@]}"; do
            id="${row%%[[:space:]]*}"
            if [ "$id" = "$want" ]; then
                targets+=("$row"); found=1; break
            fi
        done
        [ -z "$found" ] && { log "unknown region: $want"; exit 2; }
    done
fi

failures=()
for row in "${targets[@]}"; do
    id="${row%%[[:space:]]*}"
    rest="${row#$id}"
    # Strip leading whitespace; last column (bbox) has no spaces.
    rest="${rest#"${rest%%[! 	]*}"}"
    bbox="${rest##* }"
    # everything between id and bbox, trimmed, is the display name
    name="${rest% *}"
    name="${name%"${name##*[! 	]}"}"

    out="osm-${id}.zim"
    if [ -s "$out" ]; then
        # Already built — verify it has Overture before skipping.
        flags=$(./venv312/bin/python3 -c "
from libzim.reader import Archive
import json
try:
    a = Archive('$out')
    e = a.get_entry_by_path('streetzim-meta.json').get_item().content.tobytes().decode('utf-8')
    j = json.loads(e)
    print('Y' if j.get('hasOvertureAddresses') else 'N')
except Exception:
    print('N')
" 2>/dev/null)
        if [ "$flags" = "Y" ]; then
            log "skip $id — already built with Overture"
            continue
        fi
        log "existing $out lacks Overture — rebuilding"
    fi

    log "BUILD $id ($name) bbox=$bbox"
    if bash cloud/build_region.sh "$id" "$name" "$bbox"; then
        log "OK $id"
    else
        log "FAIL $id"
        failures+=("$id")
    fi
done

if [ ${#failures[@]} -gt 0 ]; then
    log "FAILED: ${failures[*]}"
    exit 1
fi
log "all done (${#targets[@]} region(s))"
