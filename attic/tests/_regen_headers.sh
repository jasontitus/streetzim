#!/bin/bash
# One-shot: regenerate any golden corpus whose first line is NOT a _meta
# record. Needed once because the metadata header was added after several
# corpora had already been generated.
#
# Uses the same params the suite runner would use (seed, min/max dist).
set -euo pipefail
cd "$(dirname "$0")/.."
source venv312/bin/activate

# region-id  ZIM                                  pairs  min_m  max_m  workers
REGIONS=(
    "silicon-valley    osm-silicon-valley-2026-04-22.zim    2000  500  40000  4"
    "washington-dc     osm-washington-dc-2026-04-22.zim     2000  300  20000  3"
    "hispaniola        osm-hispaniola-2026-04-22.zim        2000  500  60000  3"
    "colorado          osm-colorado-2026-04-22.zim          2000  500 150000  4"
    "baltics           osm-baltics-2026-04-22.zim           1500 1000 200000  4"
)

for row in "${REGIONS[@]}"; do
    read -r id zim pairs min_m max_m workers <<< "$row"
    out="tests/golden/${id}.jsonl"
    if [ ! -s "$out" ]; then
        echo "MISSING $out — skipping (run the main suite first)"
        continue
    fi
    if head -1 "$out" | grep -q '"_meta":true'; then
        echo "SKIP $id — already has header"
        continue
    fi
    if [ ! -f "$zim" ]; then
        echo "SKIP $id — $zim not found"
        continue
    fi
    echo "=== regenerating $id with header ==="
    python -m tests.generate_golden_corpus \
        --zim "$zim" \
        --out "$out" \
        --pairs "$pairs" --seed 42 \
        --min-dist-m "$min_m" --max-dist-m "$max_m" \
        --workers "$workers" --progress-every 500
done
echo "done"
