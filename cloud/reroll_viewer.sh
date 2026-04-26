#!/usr/bin/env bash
# Re-emit each region's already-shipped local ZIM with the *current*
# viewer baked in, then upload as today's date.
#
# Why this exists: the routing rewrite (full-first chain, sparse-state
# Maps, debug overlay, etc. — committed 2026-04-25 ba3150c) lives in
# resources/viewer/index.html. The PWA picks it up automatically;
# Kiwix-app users see only the version baked into their ZIM. So we
# need to re-package every shipped ZIM and re-upload.
#
# Strategy: NO chip-split, NO spatial-chunk-scale here — those are
# already in the source ZIMs. Plain repackage_zim with default
# behaviour just swaps the viewer (and reassembles graph if needed).
#
# Sequential. Each region takes ~10–30 min depending on size; 10
# regions ⇒ ~3 hours. Doesn't touch un-dated osm-<id>.zim filenames
# on archive.org per the dated-filenames rule.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

today=$(date +%Y-%m-%d)
log() { printf "[reroll %s] %s\n" "$(date '+%H:%M:%S')" "$*"; }

# Region manifest. Source = the best-current local ZIM that already
# has chip-split + spatial + Overture. (silicon-valley is mono;
# everything else is spatial.) When SRC doesn't exist locally we
# skip — those would need a fresh download from archive.
regions=(
  "silicon-valley   osm-silicon-valley.zim"
  "iran             osm-iran-shipped.zim"
  "egypt            osm-egypt-chips.zim"
  "central-asia     osm-central-asia-shipped.zim"
  "japan            osm-japan-chips-v2.zim"
  "texas            osm-texas-chips-v2.zim"
  "australia-nz     osm-australia-nz.zim"
  "west-coast-us    osm-west-coast-us.zim"
  "central-us       osm-central-us-chips.zim"
  "east-coast-us    osm-east-coast-us.zim"
  "west-asia        osm-west-asia.zim"
  # Canada uploaded 2026-04-25 — re-roll with current viewer.
  # Source is the dated archive copy so we don't accidentally
  # repackage a half-finished swap of osm-canada.zim.
  "canada           osm-canada-2026-04-25.zim"
)

failures=()
for row in "${regions[@]}"; do
    read -r id src <<< "$row"
    out="osm-${id}-${today}.zim"
    reroll_tmp="osm-${id}-${today}-reroll.zim"

    if [ ! -s "$src" ]; then
        log "skip $id — source $src not present locally"
        continue
    fi
    if [ -s "$out" ]; then
        log "skip $id — $out already staged (assume previously rolled today)"
        continue
    fi

    log "BEGIN $id  src=$src"
    log "  repackage..."
    if ! ./venv312/bin/python3 cloud/repackage_zim.py "$src" "$reroll_tmp" \
            > "${id}-reroll.log" 2>&1; then
        log "FAIL repackage $id — see ${id}-reroll.log"
        failures+=("$id")
        rm -f "$reroll_tmp"
        continue
    fi

    log "  validate..."
    if ! ./venv312/bin/python3 cloud/validate_zim.py "$reroll_tmp" \
            > "${id}-reroll-validate.log" 2>&1; then
        log "FAIL validate $id — see ${id}-reroll-validate.log"
        failures+=("$id")
        rm -f "$reroll_tmp"
        continue
    fi

    log "  upload..."
    cp "$reroll_tmp" "$out"
    if ! bash cloud/upload_validated.sh "$id" "$out" \
            > "${id}-reroll-upload.log" 2>&1; then
        log "FAIL upload $id — see ${id}-reroll-upload.log"
        failures+=("$id")
        # Keep the staged file in case the user wants to retry upload
        # without re-running the repackage.
        continue
    fi

    log "OK $id"
    rm -f "$reroll_tmp"
done

if [ ${#failures[@]} -gt 0 ]; then
    log "FAILED: ${failures[*]}"
    exit 1
fi
log "rolled ${#regions[@]} regions OK"
