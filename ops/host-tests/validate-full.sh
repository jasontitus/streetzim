#!/usr/bin/env bash
# validate-full.sh <out dir> <name> <title> <image> [GATE_* overrides in the env]
# Every check a full-profile openZIM build must pass before it is uploaded,
# nothing skipped: the openZIM output check (metadata, illustration, progress,
# routing, terrain), the full-profile content check (Overture, Wikidata,
# Wikipedia, credited), ship-region.sh's five gates, and the round's driver
# gates (markers incl. the Wikipedia geo-index, overlap, device matrix, render,
# Kiwix in-ZIM). Exit 0 only if all pass.
S=/storage/streetzim/sz-tests; OUT=$(readlink -f "$1"); NAME=$2; TITLE=$3; IMG=$4; ID=${GATE_ID:-$NAME}
Z=$(ls "$OUT"/${NAME}_*.zim | head -1); F=""
echo "=== full validation of $Z ($(date -Is))"
docker run --rm --init --name "sz-test-validate-$$" --user "$(id -u):$(id -g)" -v "$OUT:/o:ro" "$IMG" sh -c "cd /app && \
  python tools/check_openzim_output.py /o '$NAME' --title '$TITLE' --routing --terrain --stats /o/task_progress.json && \
  python tools/check_full_profile.py '/o/$(basename "$Z")'" || F="$F openzim-checks"
unset GATE_SKIP_TERRAIN
bash $S/gate-zim.sh "$Z" "$ID" || F="$F ship-region-gates"
bash $S/run-driver-gates.sh "$Z" "${GATE_SEARCH:-$TITLE}" || F="$F driver-gates"
[ -z "$F" ] && { echo "=== FULL VALIDATION PASSED: $Z"; exit 0; }
echo "=== FULL VALIDATION FAILED:$F — do not upload $Z"; exit 1
