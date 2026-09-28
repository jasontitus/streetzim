#!/usr/bin/env bash
# Comprehensive rebuild: pulls every shipped streetzim region not yet
# on the -2026-04-26c chain, repackages with the full mobile-safety
# kit (spatial routing, chip-split + sub-bucketing, sub-bucketed
# search-data, dropped LLM bundle, fixed search-detail links, latest
# viewer), validates via zimru, uploads.
#
# Two phases:
#   1) DOWNLOAD-AND-ROLL — regions with no local source, fetched from
#      archive.org one at a time. Listed in size order so a Ctrl-C
#      doesn't lose 45 GB of europe download to interrupt the small
#      ones.
#   2) CHIP-RETROFIT — already-on-c regions whose biggest chip is
#      > 100 MB (japan / east-coast-us / canada / west-asia). Re-rolled
#      from their local source to force --split-find-chips +
#      sub-bucketing.
#
# Sequential. Waits for any in-flight reroll to finish before
# starting (avoids CPU/disk contention with au-retry-2 etc).
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

today="$(date +%Y-%m-%d)c"
log() { printf "[rebuild-all %s] %s\n" "$(date '+%H:%M:%S')" "$*"; }

# Wait for any other reroll to finish so we don't compete.
log "waiting for prior reroll batches to finish..."
while pgrep -f "reroll_viewer.sh|reroll-au-retry|reroll-canada-retry" \
        >/dev/null; do sleep 30; done
log "ready to start"

detect_flags() {
    venv312/bin/python3 - "$1" <<'PY'
import sys
from libzim.reader import Archive
a = Archive(sys.argv[1])
m = s = c = False
for i in range(a.entry_count):
    try: e = a._get_entry_by_id(i)
    except Exception: continue
    p = e.path
    if p == 'routing-data/graph.bin': m = True
    elif p.startswith('routing-data/graph-cell-'): s = True
    elif p.startswith('category-index/chip-'): c = True
    if m and s and c: break
flags = ["--split-hot-search-chunks-mb", "10",
         "--chip-split-threshold-mb", "10",
         "--refresh-terrain-tiles", "terrain_cache"]
if m and not s: flags += ["--spatial-chunk-scale", "10"]
# Always pass --split-find-chips when we want sub-bucketing on a
# region whose source still has the raw poi.json / park.json. The
# local sources (chips-v2.zim etc.) keep those; this script's targets
# all have them.
flags += ["--split-find-chips"]
print(" ".join(flags))
PY
}

roll_one() {
    local id="$1" src="$2"
    local out="osm-${id}-${today}.zim"
    local tmp="osm-${id}-${today}-reroll.zim"
    if [ -s "$out" ]; then
        log "skip $id (already staged today)"; return 0
    fi
    if [ ! -s "$src" ]; then
        log "FAIL $id — source $src missing"; return 1
    fi
    local flags
    flags=$(detect_flags "$src")
    log "BEGIN $id  src=$src  flags='$flags'"
    log "  repackage..."
    # shellcheck disable=SC2086
    if ! ./venv312/bin/python3 cloud/repackage_zim.py "$src" "$tmp" $flags \
            > "${id}-reroll.log" 2>&1; then
        log "FAIL repackage $id — see ${id}-reroll.log"
        rm -f "$tmp"; return 1
    fi
    log "  validate..."
    if ! ./venv312/bin/python3 cloud/validate_zim.py "$tmp" \
            > "${id}-reroll-validate.log" 2>&1; then
        log "FAIL validate $id — see ${id}-reroll-validate.log"
        rm -f "$tmp"; return 1
    fi
    cp "$tmp" "$out"
    log "  upload..."
    if ! bash cloud/upload_validated.sh "$id" "$out" \
            > "${id}-reroll-upload.log" 2>&1; then
        log "FAIL upload $id — see ${id}-reroll-upload.log"
        return 1
    fi
    log "OK $id"
    rm -f "$tmp"
}

# ---------------- PHASE 1: download-and-roll ----------------
# Sorted small → large so quick wins land first. europe is huge
# (45 GB), do it last.
phase1=(
  "hispaniola      streetzim-hispaniola      osm-hispaniola-2026-04-22.zim"
  "washington-dc   streetzim-washington-dc   osm-washington-dc-2026-04-20.zim"
  "colorado        streetzim-colorado        osm-colorado-2026-04-22.zim"
  "baltics         streetzim-baltics         osm-baltics-2026-04-22.zim"
  "midwest-us      streetzim-midwest-us      osm-midwest-us-2026-04-15.zim"
  "africa          streetzim-africa          osm-africa-2026-04-17.zim"
  "united-states   streetzim-united-states   osm-united-states-2026-04-13.zim"
  "europe          streetzim-europe          osm-europe-2026-04-17.zim"
)
for row in "${phase1[@]}"; do
    read -r id item file <<< "$row"
    src="osm-${id}-source.zim"
    if [ ! -s "$src" ]; then
        log "downloading $id from $item/$file..."
        if ! curl -fL "https://archive.org/download/${item}/${file}" \
                -o "$src" --silent --show-error; then
            log "FAIL download $id"; rm -f "$src"; continue
        fi
        log "  downloaded $id ($(ls -la "$src" | awk '{print $5}') bytes)"
    fi
    roll_one "$id" "$src" || true
    rm -f "$src"  # keep disk tight; can re-download next time
done

# ---------------- PHASE 2: chip-retrofit ----------------
# Already on -c, but biggest chip > 100 MB. Re-roll forces
# --split-find-chips so the >10 MB chips get sub-bucketed. Source
# is the LOCAL canonical (osm-X-chips-v2.zim etc.) which still has
# the raw poi.json/park.json that --split-find-chips needs.
phase2=(
  "japan            osm-japan-chips-v2.zim"
  "east-coast-us    osm-east-coast-us.zim"
  "west-asia        osm-west-asia.zim"
  "canada           osm-canada-2026-04-25.zim"
)
for row in "${phase2[@]}"; do
    read -r id src <<< "$row"
    # Phase 2 outputs use a "-d" suffix so the "-c" upload (which
    # already exists) isn't overwritten and bloated to history.
    today="$(date +%Y-%m-%d)d"
    roll_one "$id" "$src"
done

log "rebuild-all complete"
