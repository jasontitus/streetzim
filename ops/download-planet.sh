#!/usr/bin/env bash
# Download a dated planet PBF into world-data/ with resume + md5 verification.
#
# Usage: ./download-planet.sh [YYMMDD]      (default: 260831)
# Result: world-data/planet-20YY-MM-DD.osm.pbf  (+ .md5 sidecar)
#
# planet.openstreetmap.org serves ~40 MB/s to this host (measured
# 2026-09-04) → ~40 min for the ~95 GB file. Resumable via curl -C -.
set -euo pipefail
cd /storage/streetzim
STAMP="${1:-260831}"
DATED="20${STAMP:0:2}-${STAMP:2:2}-${STAMP:4:2}"
URL="https://planet.openstreetmap.org/pbf/planet-${STAMP}.osm.pbf"
OUT="world-data/planet-${DATED}.osm.pbf"
UA="streetzim-build/1.0 (https://github.com/jasontitus/streetzim)"

if [ -s "$OUT" ] && [ -s "$OUT.md5" ] && grep -q OK "$OUT.md5.verified" 2>/dev/null; then
    echo "already downloaded and verified: $OUT"; exit 0
fi

echo "=== planet $STAMP → $OUT @ $(date -Iseconds)"
curl -fSL -A "$UA" -o "$OUT.md5" "$URL.md5"
curl -fL -A "$UA" -C - --retry 20 --retry-delay 30 -o "$OUT.part" "$URL"
mv -f "$OUT.part" "$OUT"

echo "=== verifying md5 @ $(date -Iseconds)"
EXPECT=$(awk '{print $1}' "$OUT.md5")
ACTUAL=$(md5sum "$OUT" | awk '{print $1}')
if [ "$EXPECT" != "$ACTUAL" ]; then
    echo "MD5 MISMATCH expect=$EXPECT actual=$ACTUAL — deleting $OUT" >&2
    rm -f "$OUT" "$OUT.md5.verified"; exit 1
fi
echo "OK $ACTUAL" > "$OUT.md5.verified"
osmium fileinfo "$OUT" | grep -iE 'timestamp|bbox' || true
echo "=== done @ $(date -Iseconds): $(du -h "$OUT" | cut -f1)"
