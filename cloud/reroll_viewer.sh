#!/usr/bin/env bash
# Re-emit each region's already-shipped local ZIM with the *current*
# viewer baked in AND any missing modern features (chip-split Find,
# spatial-cell routing, sub-bucketed hot-search-chunks), then upload
# as today's date.
#
# History — and why this is more than just a viewer swap:
#
#   2026-04-25 routing rewrite (full-first chain, sparse-state Maps,
#   debug overlay, …) lives in resources/viewer/index.html. The PWA
#   picks it up automatically; Kiwix-app users + downloaders see only
#   the version baked into their ZIM. So we re-package every shipped
#   ZIM and re-upload.
#
#   2026-04-26 reroll v1: passed NO flags. Result: iran + central-asia
#   shipped without chip-split (sources were built before the flag
#   existed); egypt shipped with monolithic 3.4 M-node routing graph
#   (also pre-spatial). Every reroll preserves what's there, but
#   doesn't ADD missing modernizations. So callers said "the reroll
#   didn't do what 'make it current' implied."
#
#   2026-04-26 reroll v2 (this version): inspects each source ZIM and
#   passes the right repackage_zim.py flags so the output is
#   uniformly current — chips + spatial + sub-bucketed search.
#
# Sequential. Each region takes ~10–60 min depending on size; ~12
# regions ⇒ ~6 hours. Doesn't touch un-dated osm-<id>.zim filenames
# on archive.org per the dated-filenames rule.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

today=$(date +%Y-%m-%d)
log() { printf "[reroll %s] %s\n" "$(date '+%H:%M:%S')" "$*"; }

# Detect which features the source ZIM already has and emit the
# additional repackage_zim.py flags needed to reach the current
# baseline (chips + spatial + sub-bucketed search). Always passes
# --split-hot-search-chunks-mb 10 since sub-bucketing is idempotent
# and benefits any region with hot Latin/CJK prefixes.
detect_flags() {
    local src="$1"
    venv312/bin/python3 - "$src" <<'PY'
import sys
from libzim.reader import Archive
src = sys.argv[1]
a = Archive(src)
has_mono = False
has_spatial = False
has_chips = False
for i in range(a.entry_count):
    try: e = a._get_entry_by_id(i)
    except Exception: continue
    p = e.path
    if p == 'routing-data/graph.bin': has_mono = True
    elif p.startswith('routing-data/graph-cell-'): has_spatial = True
    elif p.startswith('category-index/chip-'): has_chips = True
    if has_mono and has_spatial and has_chips: break
flags = ["--split-hot-search-chunks-mb", "10"]
# Only convert monolithic → spatial. Already-spatial sources can't
# be re-converted (the script needs graph.bin or graph-chunk-N.bin
# to reassemble; cell-chunked has neither and would produce a ZIM
# with no routing).
if has_mono and not has_spatial:
    flags += ["--spatial-chunk-scale", "10"]
# Add chip-split if missing. Idempotent if already present (re-emits
# from the same poi.json + park.json source the original build used).
if not has_chips:
    flags += ["--split-find-chips"]
print(" ".join(flags))
PY
}

# Region manifest. Source = the best-current local ZIM; the source
# may carry chips and/or spatial routing already, in which case the
# corresponding flag is suppressed by detect_flags() above.
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

    flags=$(detect_flags "$src")
    log "BEGIN $id  src=$src  flags='$flags'"
    log "  repackage..."
    # shellcheck disable=SC2086 - $flags intentionally word-splits
    if ! ./venv312/bin/python3 cloud/repackage_zim.py "$src" "$reroll_tmp" $flags \
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
