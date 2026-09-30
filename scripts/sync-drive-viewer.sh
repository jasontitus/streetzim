#!/usr/bin/env bash
# Populate web/drive/viewer/ with the bits the Firebase PWA needs:
#   - resources/viewer/index.html        (canonical viewer — unmodified)
#   - maplibre-gl.js + .css               (the vendored copy the ZIM builder uses)
#   - fzstd.js                            (zstd decoder for ZIM clusters)
#
# The PWA's service worker caches these as the "shell" on install; after
# that, all other viewer requests (tiles, fonts, map-config.json, etc.)
# are served from the user's local ZIM file via zim-reader.js.
#
# Run from repo root. Intended to be invoked by `firebase deploy`'s
# predeploy hook (see firebase.json) and by anyone iterating locally.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OUT="web/drive/viewer"
FZSTD_VERSION="0.1.1"
# sha256 of unpkg.com/fzstd@${FZSTD_VERSION}/umd/index.js; change both together.
FZSTD_SHA256="fbea1c25c4413b620ca3f59e6037d9f6c675c5c7f690e4d660143188f686461d"

mkdir -p "$OUT"

# curl with retry — CDNs occasionally 503 during deploys.
fetch() {
  local url="$1" out="$2"
  for attempt in 1 2 3 4; do
    if curl -fsSL "$url" -o "$out"; then return 0; fi
    sleep $((attempt * 2))
  done
  echo "  FAILED after 4 attempts: $url" >&2
  return 1
}

# 1. Copy the canonical viewer + the find-places mini-app. We copy
#    (not symlink) so firebase deploy's tarball sees plain files
#    regardless of the host filesystem.
cp "resources/viewer/index.html" "$OUT/index.html"
echo "  viewer HTML  → $OUT/index.html ($(wc -c < "$OUT/index.html") bytes)"

if [ -f "resources/viewer/places.html" ]; then
  cp "resources/viewer/places.html" "$OUT/places.html"
  echo "  places HTML  → $OUT/places.html ($(wc -c < "$OUT/places.html") bytes)"
fi

# Off-main-thread routing engine. PWA-only for now; ZIM-baked viewers
# will start including it at the next rebuild (see in-zim-apps.md), at
# which point Kiwix Desktop / iOS get the worker too. Until then they
# fall back to the main-thread engine — no breakage.
if [ -f "resources/viewer/routing-worker.js" ]; then
  cp "resources/viewer/routing-worker.js" "$OUT/routing-worker.js"
  echo "  routing wrkr → $OUT/routing-worker.js ($(wc -c < "$OUT/routing-worker.js") bytes)"
fi

# 2. MapLibre: the vendored copy, checked against the lock file first
#    (resources/viewer-assets.lock.json; docs/viewer-supply-chain.md).
python3 tools/pin_viewer_assets.py --check
for asset in maplibre-gl.js maplibre-gl.css; do
  cp "resources/vendor/maplibre-gl/$asset" "$OUT/$asset"
  echo "  maplibre     → $OUT/$asset"
done

# 3. Download fzstd (MIT) for ZSTD cluster decompression. Lives one dir
#    up from the viewer — only the service worker consumes it, not the
#    viewer itself.
FZSTD_URL="https://unpkg.com/fzstd@${FZSTD_VERSION}/umd/index.js"
fzstd_target="$(dirname "$OUT")/fzstd.js"
if [ ! -s "$fzstd_target" ]; then
  echo "  fetching     → $fzstd_target"
  fetch "$FZSTD_URL" "$fzstd_target"
else
  echo "  cached       → $fzstd_target"
fi
if ! echo "$FZSTD_SHA256  $fzstd_target" | sha256sum -c --quiet -; then
  echo "  $fzstd_target does not match FZSTD_SHA256; not deploying it" >&2
  rm -f "$fzstd_target"
  exit 1
fi

# 4. Emit a version stamp so the SW can bust the shell cache when we
#    change anything here. By default a hash of concatenated asset
#    sizes — cheap but stable enough.
#
#    `cloud/deploy_pwa.sh` exports its own git-derived stamp via
#    $STAMP_OVERRIDE so build-info.js, viewer/.version, and the SW's
#    SHELL_CACHE key all agree (the verify step on deploy_pwa.sh
#    used to fail because predeploy clobbered the git stamp with
#    the content-hash one).
if [ -n "${STAMP_OVERRIDE:-}" ]; then
  STAMP="$STAMP_OVERRIDE"
else
  STAMP=$(sha1sum "$OUT"/*.* "$fzstd_target" | sha1sum | awk '{print substr($1,1,10)}')
fi
echo "$STAMP" > "$OUT/.version"
echo "  version stamp: $STAMP"

# 4b. Bump the service worker's cache generation to the same stamp. A
#     plain `firebase deploy` (the documented path in ops/docs/site-deploy.md)
#     used to leave sw.js byte-identical, so the browser never installed
#     a new worker and the offline precache stayed stale indefinitely —
#     only the network-first fetch path hid it. Portable (no sed -i
#     dialect issues between GNU and BSD sed).
python3 - "$STAMP" <<'PY'
import re, sys, pathlib
stamp = sys.argv[1]
p = pathlib.Path("web/drive/sw.js")
src = p.read_text(encoding="utf-8")
new, n = re.subn(r"^const SHELL_CACHE = 'streetzim-drive-shell-[^']*';",
                 f"const SHELL_CACHE = 'streetzim-drive-shell-{stamp}';",
                 src, count=1, flags=re.M)
if n != 1:
    sys.exit("sync-drive-viewer: SHELL_CACHE line not found in web/drive/sw.js")
if new != src:
    p.write_text(new, encoding="utf-8")
    print(f"  SHELL_CACHE  → streetzim-drive-shell-{stamp}")
else:
    print("  SHELL_CACHE  unchanged")
PY

# 5. Emit build-info.js for the /drive/ picker page footer — gives the
#    user a visible "am I on the fresh deploy?" indicator independent of
#    the service worker's cache state.
BUILD_TIME=$(date '+%Y-%m-%d %H:%M %Z')
cat > "web/drive/build-info.js" <<EOF
(function(){
  var info = "${BUILD_TIME} · ${STAMP}";
  var el = document.getElementById('build-stamp');
  if (el) el.textContent = info;
  window.__STREETZIM_BUILD__ = { time: "${BUILD_TIME}", stamp: "${STAMP}" };
})();
EOF
echo "  build-info.js → web/drive/build-info.js ($BUILD_TIME)"

echo "web/drive/viewer ready."
