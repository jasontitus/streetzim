#!/bin/bash
# Route-identity differential suite: run every golden corpus against the
# candidate (post-change) ZIMs and diff the fingerprints.
#
# Assumes the golden corpora were generated against the current v4 ZIMs
# via ``tests/generate_golden_corpus.py``. A candidate build is any ZIM
# whose routing graph SHOULD produce byte-identical routes — e.g., a v5
# split-graph rebuild of the same region.
#
# Usage:
#   # Generate golden once (takes ~20 min on 20-core Mac):
#   bash tests/run_identity_suite.sh --generate
#
#   # After you build candidate ZIMs, compare:
#   bash tests/run_identity_suite.sh --compare \\
#       osm-silicon-valley-v5.zim \\
#       osm-washington-dc-v5.zim \\
#       osm-hispaniola-v5.zim
#
# A candidate ZIM is matched to its golden corpus by the region-id it
# carries in its filename (e.g. ``silicon-valley`` from ``osm-silicon-valley-*.zim``).

set -euo pipefail
cd "$(dirname "$0")/.."

GOLDEN_DIR="tests/golden"
SEED=42

# region-id  ZIM-glob                       pairs  min_m  max_m  workers
REGIONS=(
    "silicon-valley    osm-silicon-valley-2026-04-22.zim    2000  500  40000  4"
    "washington-dc     osm-washington-dc-2026-04-22.zim     2000  300  20000  3"
    "hispaniola        osm-hispaniola-2026-04-22.zim        2000  500  60000  3"
    "colorado          osm-colorado-2026-04-22.zim          2000  500 150000  3"
    "baltics           osm-baltics-2026-04-22.zim           1500 1000 200000  3"
)

usage() {
    cat <<EOF
Usage: $0 --generate                       # build golden corpora
       $0 --compare <zim> [<zim> ...]      # diff candidate ZIMs vs golden
       $0 --compare-all <region_glob>      # compare every ZIM matching <region>
EOF
    exit 1
}

if [ $# -eq 0 ]; then usage; fi
MODE=$1; shift

if [ "$MODE" = "--generate" ]; then
    mkdir -p "$GOLDEN_DIR"
    for row in "${REGIONS[@]}"; do
        read -r id zim pairs min_m max_m workers <<< "$row"
        if [ ! -f "$zim" ]; then
            echo "SKIP $id: $zim not found"
            continue
        fi
        echo "=== $id ($zim) — $pairs pairs, $workers workers ==="
        python -m tests.generate_golden_corpus \
            --zim "$zim" \
            --out "$GOLDEN_DIR/$id.jsonl" \
            --pairs "$pairs" --seed "$SEED" \
            --min-dist-m "$min_m" --max-dist-m "$max_m" \
            --workers "$workers" --progress-every 500
    done
    echo "golden corpora written to $GOLDEN_DIR/"
elif [ "$MODE" = "--compare" ]; then
    if [ $# -eq 0 ]; then usage; fi
    overall_fail=0
    for zim in "$@"; do
        id=""
        for row in "${REGIONS[@]}"; do
            read -r r_id r_zim _rest <<< "$row"
            if [[ "$zim" == *"$r_id"* ]]; then id=$r_id; break; fi
        done
        if [ -z "$id" ]; then
            echo "SKIP $zim: no region-id match"
            continue
        fi
        golden="$GOLDEN_DIR/$id.jsonl"
        if [ ! -f "$golden" ]; then
            echo "SKIP $zim: missing golden $golden (run --generate first)"
            continue
        fi
        # Regenerate candidate corpus against the new ZIM using the same pair selection.
        # Region-specific params pulled from REGIONS.
        read -r _ _ pairs min_m max_m workers <<< "$(for row in "${REGIONS[@]}"; do read -r x _rest <<< "$row"; if [ "$x" = "$id" ]; then echo "$row"; break; fi; done)"
        cand="$GOLDEN_DIR/$id.candidate.jsonl"
        echo "=== $id candidate ($zim) ==="
        python -m tests.generate_golden_corpus \
            --zim "$zim" \
            --out "$cand" \
            --pairs "$pairs" --seed "$SEED" \
            --min-dist-m "$min_m" --max-dist-m "$max_m" \
            --workers "$workers" --progress-every 500
        echo "--- diff $id ---"
        if python -m tests.diff_corpora "$golden" "$cand"; then
            echo "  $id: IDENTICAL"
        else
            overall_fail=1
            echo "  $id: DIVERGES"
        fi
    done
    exit "$overall_fail"
else
    usage
fi
