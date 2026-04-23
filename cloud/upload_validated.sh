#!/bin/bash
# Drop-in replacement for the `ia upload` block inside upload_and_deploy.
# Validates the ZIM first; aborts (non-zero exit) before any archive.org
# mutation if the validator fails. Warnings are logged but don't block.
#
# Usage inside a rollout script:
#   bash cloud/upload_validated.sh <id> <path-to-dated-zim>
#
# Expands to:
#   1. "$PYTHON" cloud/validate_zim.py <dated>   — hard-fail on any error-severity check
#   2. ia upload streetzim-<id> <basename>
#   3. ia metadata --modify=date:<today>
#   4. cloud/stamp_item_metadata.py
#   5. cloud/cleanup_old_zims.py --keep 2
#   6. web/generate.py --deploy
#
# Exits 2 when the validator blocks the upload (so the caller can
# distinguish a validation abort from a network-level upload failure).
set -u
id="${1:?id required}"
dated="${2:?path to dated ZIM required}"
today=$(date +%Y-%m-%d)

# Resolve the Python interpreter. venv312 is the project convention — if
# the caller already activated it, $PYTHON below picks it up; otherwise
# we fall back to the absolute path. Prevents ``python3`` from resolving
# to a broken anaconda install that happens to be earlier on $PATH.
PROJECT_DIR="${PROJECT_DIR:-/Users/jasontitus/experiments/streetzim}"
if [ -x "$PROJECT_DIR/venv312/bin/python" ]; then
    PYTHON="$PROJECT_DIR/venv312/bin/python"
else
    PYTHON="${PYTHON:-python3}"
fi

if [ ! -s "$dated" ]; then
    echo "FATAL ${id}: source ${dated} missing or empty" >&2
    exit 1
fi

# --- 1. Pre-upload validation ---
echo "validating $(basename "$dated")..."
if ! "$PYTHON" cloud/validate_zim.py "$dated"; then
    echo "FATAL ${id}: validator rejected $(basename "$dated") — NOT uploading" >&2
    exit 2
fi
echo "validation passed."

# --- 2. ia upload (same pattern as overture-rollout-redo.sh) ---
ia upload "streetzim-${id}" "$(basename "$dated")" --retries 5 \
    || echo "WARN upload reported issues for ${id} — continuing"
sleep 30

# --- 3. metadata modify ---
ia metadata "streetzim-${id}" --modify="date:${today}" || true

# --- 4. stamp feature flags ---
"$PYTHON" cloud/stamp_item_metadata.py "streetzim-${id}" \
    --routing --overture --terrain --satellite --wikidata \
    || echo "WARN stamp skipped for ${id}"

# --- 5. prune old dated ZIMs (keep last 2) ---
"$PYTHON" cloud/cleanup_old_zims.py "streetzim-${id}" --keep 2 \
    || echo "WARN cleanup skipped for ${id}"

# --- 6. web deploy ---
"$PYTHON" web/generate.py --deploy \
    || echo "WARN web deploy failed for ${id} — continuing"

echo "DONE ${id}"
