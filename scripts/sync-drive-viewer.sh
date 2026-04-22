#!/usr/bin/env bash
# Populate web/drive/viewer/ with the bits the Firebase PWA needs:
#   - resources/viewer/index.html        (canonical viewer — unmodified)
#   - maplibre-gl.js + .css               (same version the ZIM builder uses)
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
MAPLIBRE_VERSION="5.23.0"
FZSTD_VERSION="0.1.1"

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

# 1. Copy the canonical viewer. We copy (not symlink) so firebase deploy's
#    tarball sees a plain file regardless of the host filesystem.
cp "resources/viewer/index.html" "$OUT/index.html"
echo "  viewer HTML  → $OUT/index.html ($(wc -c < "$OUT/index.html") bytes)"

# 2. Download MapLibre. Already on disk? Skip.
MAPLIBRE_BASE="https://unpkg.com/maplibre-gl@${MAPLIBRE_VERSION}/dist"
for asset in maplibre-gl.js maplibre-gl.css; do
  target="$OUT/$asset"
  if [ ! -s "$target" ]; then
    echo "  fetching     → $target"
    fetch "$MAPLIBRE_BASE/$asset" "$target"
  else
    echo "  cached       → $target"
  fi
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

# 4. Emit a version stamp so the SW can bust the shell cache when we
#    change anything here. Hash of concatenated asset sizes — cheap but
#    stable enough for our purposes.
STAMP=$(sha1sum "$OUT"/*.* "$fzstd_target" | sha1sum | awk '{print substr($1,1,10)}')
echo "$STAMP" > "$OUT/.version"
echo "  version stamp: $STAMP"

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
