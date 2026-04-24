#!/usr/bin/env bash
# Gated Firebase deploy for the Streetzim Drive PWA.
#
# Why this script: `firebase deploy` alone blindly ships whatever's in
# `web/` — we've shipped a stale viewer (`resources/viewer/index.html`
# missing the current API tokens) and had users see a ~3h sync lag
# because the build-info stamp didn't change. This wrapper bumps the
# cache markers atomically, runs a local sanity check, deploys, and
# verifies the live origin serves the bumped stamp.
#
# Usage:
#   bash cloud/deploy_pwa.sh              # bump + deploy + verify
#   bash cloud/deploy_pwa.sh --dry-run    # bump + sanity-check, skip deploy
#   bash cloud/deploy_pwa.sh --force      # deploy even if sanity-check warns

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DRY_RUN=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --force)   FORCE=1   ;;
        *) echo "usage: $0 [--dry-run] [--force]" >&2; exit 2 ;;
    esac
done

log() { printf "[pwa-deploy] %s\n" "$*"; }

# ---------------------------------------------------------------
# 1. Sanity checks on the source viewer before we let it ship.
# ---------------------------------------------------------------
log "sanity check resources/viewer/*"

for f in resources/viewer/index.html resources/viewer/places.html; do
    if [ ! -s "$f" ]; then
        echo "[FATAL] $f missing or empty — refusing to deploy"
        exit 1
    fi
done

# Contract tokens — the current viewer MUST reference these. A stale
# copy missing them would ship to field users (the exact bug that
# burned us on 2026-04-22 when Kiwix users saw the old viewer).
REQUIRED_TOKENS=(
    "_queryPlaces"     # routing typeahead uses this
    "manifest.json"    # search manifest fetch
    "sub_chunks"       # hot-split expansion
    "maplibre-gl"      # mapping lib
)
missing_tokens=()
for tok in "${REQUIRED_TOKENS[@]}"; do
    if ! grep -qF "$tok" resources/viewer/index.html; then
        missing_tokens+=("$tok")
    fi
done
if [ ${#missing_tokens[@]} -gt 0 ]; then
    echo "[FATAL] resources/viewer/index.html is missing tokens: ${missing_tokens[*]}"
    echo "        → viewer is stale. rebuild before deploy."
    [ "$FORCE" -eq 1 ] || exit 1
fi

# ---------------------------------------------------------------
# 2. Bump the cache markers. All three have to move in lockstep,
#    otherwise the SW/PWA caches go out of sync and users see a
#    mix of old+new for 3+ hours.
# ---------------------------------------------------------------
stamp=$(git rev-parse --short=10 HEAD 2>/dev/null || echo "$(date +%Y%m%d%H)")
when=$(date "+%Y-%m-%d %H:%M %Z")

log "bump build-info → $stamp @ $when"

# build-info.js — shown on /drive picker page ("am I looking at the fresh deploy?")
cat > web/drive/build-info.js <<EOF
(function(){
  var info = "$when · $stamp";
  var el = document.getElementById('build-stamp');
  if (el) el.textContent = info;
  window.__STREETZIM_BUILD__ = { time: "$when", stamp: "$stamp" };
})();
EOF

# viewer/.version — the SW reads this on install to decide when to
# clear the shell cache
echo -n "$stamp" > web/drive/viewer/.version

# SW cache key — new name ⇒ new cache entry ⇒ old cache evicted
# on activate. Use the same stamp so all three agree.
new_cache="streetzim-drive-shell-$stamp"
if grep -q "^const SHELL_CACHE = " web/drive/sw.js; then
    # macOS sed requires the '' argument to -i
    sed -i '' -E \
        "s/^const SHELL_CACHE = '[^']+';/const SHELL_CACHE = '$new_cache';/" \
        web/drive/sw.js
    log "sw cache key → $new_cache"
else
    echo "[WARN] could not find SHELL_CACHE in web/drive/sw.js"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    log "dry-run: stopping before deploy"
    exit 0
fi

# ---------------------------------------------------------------
# 3. Deploy. The predeploy hook in firebase.json runs
#    scripts/sync-drive-viewer.sh which copies resources/viewer
#    into web/drive/viewer, so our bumped .version is preserved
#    and the fresh HTML lands with it.
# ---------------------------------------------------------------
log "firebase deploy --only hosting"
firebase deploy --only hosting

# ---------------------------------------------------------------
# 4. Verify live origin serves the bumped stamp — this is what
#    lets us trust that users are actually getting the new deploy.
# ---------------------------------------------------------------
log "verifying live origin..."
# Retry a few times — Fastly edge caches can linger briefly after
# a fresh deploy (< 1 minute typical).
for attempt in 1 2 3 4 5; do
    sleep $((attempt * 3))
    live=$(curl -fsSL --max-time 10 "https://streetzim.web.app/drive/build-info.js?bust=$$" \
            | grep -oE '· [a-f0-9]{10,}' | head -1 | tr -d ' ·')
    if [ "$live" = "$stamp" ]; then
        log "OK: live serves $stamp"
        exit 0
    fi
    log "attempt $attempt: live stamp = '$live' (want '$stamp') — retrying"
done
echo "[FATAL] after 5 attempts, live origin still serves '$live' not '$stamp'"
echo "        — check the Firebase console + Fastly cache + try ?bust=1"
exit 1
