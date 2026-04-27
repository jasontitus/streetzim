#!/usr/bin/env bash
# One-off: roll silicon-valley + iran with -c suffix in parallel
# with the main -c batch. Both are small regions (<3 GB) so they
# finish quickly and don't compete much for CPU with the larger
# regions in the main batch.
#
# Reusable: re-derives flags from each source ZIM the same way
# reroll_viewer.sh does — chip-split / spatial / sub-bucket
# decisions match.
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

today=$(date +%Y-%m-%d)c
log() { printf "[reroll-svi %s] %s\n" "$(date '+%H:%M:%S')" "$*"; }

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
flags = ["--split-hot-search-chunks-mb", "10"]
if m and not s: flags += ["--spatial-chunk-scale", "10"]
if not c: flags += ["--split-find-chips"]
print(" ".join(flags))
PY
}

for row in "silicon-valley osm-silicon-valley.zim" "iran osm-iran-shipped.zim"; do
    read -r id src <<< "$row"
    out="osm-${id}-${today}.zim"
    tmp="osm-${id}-${today}-reroll.zim"
    [ -s "$out" ] && { log "skip $id (already staged)"; continue; }
    flags=$(detect_flags "$src")
    log "BEGIN $id  flags='$flags'"
    # shellcheck disable=SC2086
    if ! ./venv312/bin/python3 cloud/repackage_zim.py "$src" "$tmp" $flags \
            > "${id}-reroll.log" 2>&1; then
        log "FAIL repackage $id"; rm -f "$tmp"; continue
    fi
    if ! ./venv312/bin/python3 cloud/validate_zim.py "$tmp" \
            > "${id}-reroll-validate.log" 2>&1; then
        log "FAIL validate $id"; rm -f "$tmp"; continue
    fi
    cp "$tmp" "$out"
    if ! bash cloud/upload_validated.sh "$id" "$out" \
            > "${id}-reroll-upload.log" 2>&1; then
        log "FAIL upload $id"; continue
    fi
    log "OK $id"; rm -f "$tmp"
done
log "done"
