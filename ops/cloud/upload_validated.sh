#!/bin/bash
# ops split, stage 1: this file lives in ops/ and a symlink at its old path
# runs it; the paths below assume that path (and so do pgrep and the locks).
# Started directly (ops/..., or from inside ops/), it re-runs by the old path.
if [ ! -L "$0" ] && _ops_real="$(readlink -f "$0" 2>/dev/null)"; then
  case "$_ops_real" in
    */ops/*) _ops_old="${_ops_real%/ops/*}/${_ops_real##*/ops/}"
             if [ -L "$_ops_old" ]; then exec bash "$_ops_old" "$@"; fi ;;
  esac
fi
unset _ops_real _ops_old
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
# distinguish a validation abort from a network-level upload failure);
# 7 when the file is the base of published language recipes and its bytes
# changed (step 1b: the in-place patch freeze).
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
# A rerun to finish a "listing pending" upload must not send the file
# again. `ia upload --checksum` skips it only when the item has NO pending
# task, and a queued derive can sit for a day (it re-sent 15-25 GB ZIMs and
# left permanent history/ copies), so ask first: listed with the same MD5
# is the same file (ops/cloud/archive_has_file.py).
# A pending row's `when` (finish_pending_uploads.sh trusts the row's md5 only
# for a file not changed since): taken before the md5 read starts, so a change
# made while it reads (minutes for 60 GB) is seen as one.
md5_when=$(date -Iseconds)
local_md5=$(md5sum "$dated" | cut -d' ' -f1)
# Every later step keys on it (the listing below waits for it): without one
# the upload could never be confirmed, and each rerun would send it again.
if ! [[ "$local_md5" =~ ^[0-9a-f]{32}$ ]]; then
    echo "FATAL ${id}: no md5 for $(basename "$dated") (got '${local_md5}') — nothing sent" >&2
    exit 3
fi
if "$IA" metadata "streetzim-${id}" 2>/dev/null \
        | "$PYTHON" "${PROJECT_DIR}/ops/cloud/archive_has_file.py" "$(basename "$dated")" "$local_md5"; then
    echo "  archive.org already holds $(basename "$dated") (md5 ${local_md5}): not sent again"
else
    # --- 1b. A base whose language recipes are published on /build/ ---
    # (only when bytes are about to be sent: a finisher for a file archive.org
    # already lists completes its torrent/prune/deploy.) The recipes and
    # language torrents pin the base's bytes: a re-upload under the same name
    # with other bytes (an in-place viewer patch) breaks every published entry
    # until ops/langpack/ship_packs.sh regenerates them. Frozen (user decision
    # 2026-10-21) unless LANGPACK_RESHIP_AFTER_PATCH=1, which queues the region
    # in langpack-reship.tsv for ops/langpack/reship_queue.sh. Exit 7 only on a
    # positive match; a checker that fails is a WARNING and the upload goes on.
    if [ -f "${PROJECT_DIR}/tools/langpack/published_base.py" ]; then
        pub_regions=$("$PYTHON" "${PROJECT_DIR}/tools/langpack/published_base.py" "$dated" \
            --web "${PROJECT_DIR}/web" --md5 "$local_md5"); pub_rc=$?
        if [ "$pub_rc" = 3 ]; then
            if [ "${LANGPACK_RESHIP_AFTER_PATCH:-0}" != 1 ]; then
                echo "REFUSED ${id}: frozen: published base — $(basename "$dated") is the base of language recipes published on /build/ ($(echo $pub_regions)) and its bytes changed; re-uploading it would break them and their torrents. Run with LANGPACK_RESHIP_AFTER_PATCH=1 (queues ops/langpack/reship_queue.sh). Nothing sent." >&2
                exit 7
            fi
            ( flock 8
              for r in $pub_regions; do
                  printf '%s\t%s\t%s\n' "$r" "$(basename "$dated")" "$(date -Iseconds)" >> "${PROJECT_DIR}/langpack-reship.tsv"
              done ) 8> "${PROJECT_DIR}/langpack-reship.tsv.write.lock"
            echo "NOTE ${id}: published language recipes ($(echo $pub_regions)) use $(basename "$dated"): queued in langpack-reship.tsv; run ops/langpack/reship_queue.sh after this upload"
        elif [ "$pub_rc" != 0 ]; then
            echo "WARNING ${id}: published_base.py failed (rc ${pub_rc}): the freeze was not checked; uploading anyway" >&2
        fi
    fi
    # --checksum still skips it when the metadata above was stale.
    if ! "$IA" upload "streetzim-${id}" "$(basename "$dated")" --retries 5 --checksum; then
        echo "FATAL ${id}: ia upload failed for $(basename "$dated") — nothing pruned, site not redeployed" >&2
        exit 3
    fi
    sleep 30
fi

# --- 3. metadata modify ---
"$IA" metadata "streetzim-${id}" --modify="date:${today}" || true

# --- 4. stamp feature flags ---
# Read them out of the ZIM we just shipped rather than asserting all five.
# Until 2026-09-27 this passed --routing --overture --terrain --satellite
# --wikidata unconditionally, so both Light variants — built with
# --no-satellite, hasSatellite:false in their own meta — carried a
# "Satellite" badge on the download page. --from-zim also writes an
# explicit "no", which is what clears a stale yes from an earlier upload.
# A ZIM too old to carry streetzim-meta.json leaves the badges untouched:
# skipping is honest, guessing is how the wrong badge got there.
"$PYTHON" cloud/stamp_item_metadata.py "streetzim-${id}" --from-zim "$dated" \
    || echo "WARN stamp skipped for ${id} (no readable streetzim-meta.json)"

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
# archive.org lists a file minutes after the transfer finishes, and the
# lag scales with size: 5.7 GB (japan) and 4.9 GB (ukraine) both blew a
# flat 180 s on 2026-09-06 and were reported as upload failures even
# though both files were already there. Allow 3 min + 1 min per GB, to
# a 30 min ceiling.
_size_gb=$(( ( $(stat -c %s "$dated" 2>/dev/null || stat -f %z "$dated") ) / 1000000000 ))
_poll_secs=$(( 180 + _size_gb * 60 )); [ "$_poll_secs" -gt 1800 ] && _poll_secs=1800
_poll_secs=${STREETZIM_LISTING_POLL_SECS:-$_poll_secs}   # tests
echo "  allowing ${_poll_secs}s for archive.org to list a ${_size_gb} GB file"
metadata_deadline=$(( $(date +%s) + _poll_secs ))
metadata_started=$(date +%s)
echo "waiting for archive.org metadata to list ${target_file}..."
# Listed WITH THIS FILE'S MD5, not just by name: a re-upload under the same
# name (a viewer patch keeps the size too) is listed by name at once, as the
# PREVIOUS file, until archive.org's task for the new one runs. On 2026-10-17
# east-coast-us passed "metadata reflects ... after 1s" and "remote size
# matches" that way, and got its torrent and deploy while archive.org still
# served the old bytes.
while [ "$(date +%s)" -lt "$metadata_deadline" ]; do
    if "$IA" metadata "streetzim-${id}" 2>/dev/null \
            | "$PYTHON" "${PROJECT_DIR}/ops/cloud/archive_has_file.py" "$target_file" "$local_md5"; then
        echo "  metadata lists ${target_file} with md5 ${local_md5} after $(( $(date +%s) - metadata_started ))s"
        break
    fi
    sleep "${STREETZIM_LISTING_POLL_INTERVAL:-10}"
done
if [ "$(date +%s)" -ge "$metadata_deadline" ]; then
    echo "WARN ${id}: metadata still didn't list ${target_file} after ${_poll_secs}s — cleanup may be stale"
fi

# --- 4b'. verify the remote listing matches the local file (size). Runs
# AFTER the consistency poll above — right after upload the metadata API
# usually doesn't list the file yet and the check would be skipped.
local_size=$(stat -c %s "$dated" 2>/dev/null || stat -f %z "$dated")
remote_size=$("$IA" metadata "streetzim-${id}" 2>/dev/null \
    | "$PYTHON" -c "import sys, json; m=json.load(sys.stdin); print(next((f.get('size') for f in m.get('files', []) if f.get('name')==sys.argv[1]), ''))" \
        "$target_file")
if ! "$IA" metadata "streetzim-${id}" 2>/dev/null \
        | "$PYTHON" "${PROJECT_DIR}/ops/cloud/archive_has_file.py" "$target_file" "$local_md5"; then
    # The transfer completed (ia upload returned 0 above). archive.org
    # lists a file only after its archive.php task runs, and that queue
    # can sit for hours — iceland's was still queued 4 h after a clean
    # 565 MiB transfer. That is not an upload failure, and calling it one
    # marks a good ZIM as failed and rebuilds it for nothing.
    #
    # Exit 6 = "transferred, listing pending". Prune and deploy are
    # correctly skipped (both need the listing to be safe); the region is
    # recorded for cloud/finish_pending_uploads.sh to complete later.
    # Not listed, or listed as the previous file under this name (another
    # md5): either way its task is still queued, so the same exit.
    if [ -z "$remote_size" ]; then
        echo "PENDING ${id}: ${target_file} transferred but archive.org has not listed it yet"
    else
        echo "PENDING ${id}: ${target_file} transferred but archive.org still lists the previous ${target_file} (not md5 ${local_md5})"
    fi
    echo "  (its archive.php task is queued; prune and deploy deferred)"
    # 4th column: the md5 finish_pending_uploads.sh waits for (the name alone
    # is listed at once for a same-named re-upload, as the previous file).
    printf '%s\t%s\t%s\t%s\n' "$id" "$target_file" "$md5_when" "$local_md5" \
        >> "${PROJECT_DIR}/pending-uploads.tsv"
    exit 6
fi
# Re-poll on a mismatch before calling it fatal. Re-uploading a file that
# already exists under the same name races archive.org's own metadata: the
# listing keeps reporting the PREVIOUS size for a while, so a single read
# compares new-local against stale-remote and condemns a good upload.
# Measured 2026-09-25 on hawaii: reported 147189877 (the old file) against a
# local 147189440, then matched exactly moments later. The rollout re-uploads
# same-named files constantly and only escaped this because its transfers run
# long enough for the metadata to settle.
if [ "$remote_size" != "$local_size" ]; then
    echo "  remote size ${remote_size} != local ${local_size}; re-polling (metadata may be stale)"
    for _try in $(seq 1 30); do
        sleep 20
        remote_size=$("$IA" metadata "streetzim-${id}" 2>/dev/null \
            | "$PYTHON" -c "import sys, json; m=json.load(sys.stdin); print(next((f.get('size') for f in m.get('files', []) if f.get('name')==sys.argv[1]), ''))" \
                "$target_file")
        [ "$remote_size" = "$local_size" ] && break
    done
fi
if [ "$remote_size" != "$local_size" ]; then
    echo "FATAL ${id}: archive.org lists ${target_file} as ${remote_size} B but local is ${local_size} B after 10 min of re-polling — partial upload?" >&2
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
