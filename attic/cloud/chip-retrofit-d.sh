#!/usr/bin/env bash
# Chip-sub-bucket retrofit for the 4 already-on-c regions whose
# biggest chip file exceeds 100 MB and is therefore tight against
# iOS heap when the Find page taps the chip.
#
# Each output ships as `osm-${id}-2026-04-26d.zim` so the existing
# `-2026-04-26c.zim` upload stays as a fallback (and the new -d
# becomes the lexicographically-newest, picked by the site).
#
# Sources are LOCAL — the four in cloud/reroll_viewer.sh's manifest
# (osm-X-chips-v2.zim / osm-X.zim / osm-canada-2026-04-25.zim).
# Those sources still carry poi.json / park.json (today's -c
# outputs dropped them per --include-llm-bundle default), which
# `--split-find-chips` needs to re-derive + sub-bucket.
#
# Sequential. ~3-4 hours wall time total (canada is the long pole
# at ~2 hours).
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

today="$(date +%Y-%m-%d)d"
log() { printf "[chip-d %s] %s\n" "$(date '+%H:%M:%S')" "$*"; }

# Pairs: <region-id> <local-source-zim>
regions=(
  "japan          osm-japan-chips-v2.zim"
  "east-coast-us  osm-east-coast-us.zim"
  "west-asia      osm-west-asia.zim"
  "canada         osm-canada-2026-04-25.zim"
)
for row in "${regions[@]}"; do
    read -r id src <<< "$row"
    out="osm-${id}-${today}.zim"
    tmp="osm-${id}-${today}-reroll.zim"
    if [ -s "$out" ]; then
        log "skip $id (already staged)"; continue
    fi
    if [ ! -s "$src" ]; then
        log "FAIL $id — source $src missing"; continue
    fi
    log "BEGIN $id  src=$src"
    log "  repackage..."
    if ! ./venv312/bin/python3 cloud/repackage_zim.py "$src" "$tmp" \
            --split-find-chips \
            --split-hot-search-chunks-mb 10 \
            --chip-split-threshold-mb 10 \
            > "${id}-d-reroll.log" 2>&1; then
        log "FAIL repackage $id"
        rm -f "$tmp"; continue
    fi
    log "  validate..."
    if ! TERRAIN_STRIPE_TOLERATE=20 \
            ./venv312/bin/python3 cloud/validate_zim.py "$tmp" \
            > "${id}-d-reroll-validate.log" 2>&1; then
        log "FAIL validate $id"
        rm -f "$tmp"; continue
    fi
    cp "$tmp" "$out"
    log "  upload..."
    if ! TERRAIN_STRIPE_TOLERATE=20 \
            bash cloud/upload_validated.sh "$id" "$out" \
            > "${id}-d-reroll-upload.log" 2>&1; then
        log "FAIL upload $id"; continue
    fi
    log "OK $id"
    rm -f "$tmp"
done
log "chip-retrofit-d complete"
