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
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Resolve to whichever venv has a working python+ia. venv312 is the Mac
# convention; venv-linux exists on the Linux build host and contains a
# real Linux interpreter — venv312 may be present there as an rsynced
# stub with Mac-pathed shebangs that fail to execute. Prefer the venv
# whose `python` runs without error.
ACTIVE_VENV=""
for v in venv-linux venv312; do
    if "$PROJECT_DIR/$v/bin/python" --version >/dev/null 2>&1; then
        ACTIVE_VENV="$PROJECT_DIR/$v"
        break
    fi
done
if [ -n "$ACTIVE_VENV" ]; then
    PYTHON="$ACTIVE_VENV/bin/python"
    IA="$ACTIVE_VENV/bin/ia"
    # cleanup_old_zims.py + stamp_item_metadata.py shell out to bare
    # `ia` via subprocess.run; prepend venv bin so those Python children
    # inherit a PATH that resolves `ia`.
    export PATH="$ACTIVE_VENV/bin:$PATH"
else
    PYTHON="${PYTHON:-python3}"
    IA="${IA:-ia}"
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
# A failed upload must be FATAL: continuing used to bump the item date,
# stamp features, prune the previous good ZIMs and redeploy the site —
# all on top of a file that never arrived — and exit 0 so every wrapper
# reported success.
if ! "$IA" upload "streetzim-${id}" "$(basename "$dated")" --retries 5; then
    echo "FATAL ${id}: ia upload failed for $(basename "$dated") — nothing pruned, site not redeployed" >&2
    exit 3
fi
sleep 30

# --- 3. metadata modify ---
"$IA" metadata "streetzim-${id}" --modify="date:${today}" || true

# --- 4. stamp feature flags ---
"$PYTHON" cloud/stamp_item_metadata.py "streetzim-${id}" \
    --routing --overture --terrain --satellite --wikidata \
    || echo "WARN stamp skipped for ${id}"

# --- 4b. Wait for `ia metadata` to reflect the upload before
# pruning. The 30-second blanket sleep above isn't enough on its own:
# archive.org's metadata API is eventually consistent, and on 2026-04-28
# a DC upload finished cleanup before the new ZIM was visible, so
# cleanup saw N-1 dated files, decided nothing needed pruning, and
# left osm-washington-dc-2026-04-20.zim (176 MB) plus two newer
# versions in place — 700 MB of stale data showing on the site. Poll
# the metadata until the just-uploaded file is listed (or 3 minutes
# elapse), then proceed.
target_file="$(basename "$dated")"
metadata_deadline=$(( $(date +%s) + 180 ))
metadata_started=$(date +%s)
echo "waiting for archive.org metadata to list ${target_file}..."
while [ "$(date +%s)" -lt "$metadata_deadline" ]; do
    if "$IA" metadata "streetzim-${id}" 2>/dev/null \
            | "$PYTHON" -c "import sys, json; m=json.load(sys.stdin); sys.exit(0 if any(f.get('name')==sys.argv[1] for f in m.get('files', [])) else 1)" \
                "$target_file"; then
        echo "  metadata reflects ${target_file} after $(( $(date +%s) - metadata_started ))s"
        break
    fi
    sleep 10
done
if [ "$(date +%s)" -ge "$metadata_deadline" ]; then
    echo "WARN ${id}: metadata still didn't list ${target_file} after 3 min — cleanup may be stale"
fi

# --- 4b'. verify the remote listing matches the local file (size). Runs
# AFTER the consistency poll above — right after upload the metadata API
# usually doesn't list the file yet and the check would be skipped.
local_size=$(stat -c %s "$dated" 2>/dev/null || stat -f %z "$dated")
remote_size=$("$IA" metadata "streetzim-${id}" 2>/dev/null \
    | "$PYTHON" -c "import sys, json; m=json.load(sys.stdin); print(next((f.get('size') for f in m.get('files', []) if f.get('name')==sys.argv[1]), ''))" \
        "$target_file")
if [ -z "$remote_size" ]; then
    echo "FATAL ${id}: ${target_file} is not listed by archive.org — refusing to prune or redeploy" >&2
    exit 3
fi
if [ "$remote_size" != "$local_size" ]; then
    echo "FATAL ${id}: archive.org lists ${target_file} as ${remote_size} B but local is ${local_size} B — partial upload?" >&2
    exit 3
fi
echo "  remote size matches local (${local_size} B)"

# --- 4c. refresh the site's torrent for this region from the LOCAL file
# so web/torrents/<id>.torrent names the file the Download button links
# (the committed torrents had drifted several builds behind and their
# webseeds pointed at files the keep-2 prune below had already deleted).
if [ -f cloud/build_torrent.py ]; then
    if "$PYTHON" cloud/build_torrent.py \
            "https://archive.org/download/streetzim-${id}/$(basename "$dated")" \
            "web/torrents/${id}.torrent" --local "$dated"; then
        # The torrent is tracked in git: deploying it without committing
        # means the next checkout regresses to the stale one (site hides
        # the button, cleanup protects the wrong file). Commit unless the
        # operator opts out with STREETZIM_NO_TORRENT_COMMIT=1.
        if [ "${STREETZIM_NO_TORRENT_COMMIT:-0}" != "1" ] \
                && git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
                && [ -n "$(git status --porcelain -- "web/torrents/${id}.torrent")" ]; then
            # (git status, not git diff: a brand-new region's torrent is
            # untracked, and `git diff --quiet` exits 0 for untracked files)
            git add "web/torrents/${id}.torrent" \
                && git commit -q -m "torrents: ${id} → $(basename "$dated")" -- "web/torrents/${id}.torrent" \
                || echo "WARN could not commit web/torrents/${id}.torrent — commit it by hand"
        fi
    else
        echo "WARN torrent refresh failed for ${id} (site will hide the Torrent button)"
    fi
fi

# --- 5. prune old dated ZIMs (keep last 2) ---
"$PYTHON" cloud/cleanup_old_zims.py "streetzim-${id}" --keep 2 \
    || echo "WARN cleanup skipped for ${id}"

# --- 6. web deploy ---
"$PYTHON" web/generate.py --deploy \
    || echo "WARN web deploy failed for ${id} — continuing"

echo "DONE ${id}"
