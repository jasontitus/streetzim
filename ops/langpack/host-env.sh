# StreetZim's build host: settings for the language-pack pipeline
# (docs/language-pack-pipeline.md). Source it before running
# tools/langpack/*.sh or ops/langpack/ship_packs.sh there:
#
#   . ops/langpack/host-env.sh
#
# Values already set in the environment win. `langpack_env_check` (below)
# says what is missing; ship_packs.sh runs it before anything else.
#
# One-time setup on the host (ops steps; nothing here installs anything):
#   - zim-tools 3.8.0 (the official zimcheck) next to kiwix-tools:
#       /storage/streetzim/tools/zim-tools_linux-x86_64-3.8.0/zimcheck
#     (download.openzim.org/release/zim-tools/zim-tools_linux-x86_64-3.8.0.tar.gz;
#     the zimcheck on PATH here is zimru's, which the scripts refuse).
#   - playwright-core for the browser gates, in its own folder (never npm at
#     the repository root: it would rewrite production's package.json):
#       (cd ops/langpack && npm ci)
#   - puppeteer for cloud/zim_viewer_smoke.mjs: the repository's own
#     node_modules (the smoke the builds already run).
#   - Chromium 1217 (playwright's) in ~/.cache/ms-playwright, with the
#     libraries it lacks in .browser-libs.
_lp_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# Working files (verdicts, gated recipes, gate logs, the merged file being
# checked): on /storage, never /home; the checkout's own langpack-work/
# (gitignored).
export LANGPACK_WORK=${LANGPACK_WORK:-/storage/streetzim/langpack-work}
# The pack builder's cache (SQL dumps, 0.1-2.5 GB a language; title maps,
# base scans, Wikidata text): kept from one round to the next.
export WP_CACHE=${WP_CACHE:-$LANGPACK_WORK/wpcache}
# The host is shared with other builds: pause the heavy steps below 40 GB.
export LANGPACK_MIN_FREE_GB=${LANGPACK_MIN_FREE_GB:-40}
export PYTHON=${PYTHON:-/storage/streetzim/venv-linux/bin/python3}
export WIKI_SRC=${WIKI_SRC:-/storage/streetzim/wiki-src}
export PACK_GATE_ZIMCHECK=${PACK_GATE_ZIMCHECK:-/storage/streetzim/tools/zim-tools_linux-x86_64-3.8.0/zimcheck}
export KIWIX_SERVE=${KIWIX_SERVE:-/storage/streetzim/tools/kiwix-tools_linux-x86_64-3.8.2/kiwix-serve}
export NODE=${NODE:-/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node}
export BROWSER_LIBS=${BROWSER_LIBS:-/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu}
export CHROME_PATH=${CHROME_PATH:-$HOME/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}
export PLAYWRIGHT_CORE=${PLAYWRIGHT_CORE:-$_lp_root/ops/langpack/node_modules/playwright-core/index.mjs}

# Prints what is missing, one line each; returns 1 when anything is.
langpack_env_check() {
  local bad=0 v
  for v in PYTHON PACK_GATE_ZIMCHECK KIWIX_SERVE NODE CHROME_PATH; do
    [ -x "${!v}" ] || { echo "langpack: $v=${!v} is not an executable" >&2; bad=1; }
  done
  [ -d "$WIKI_SRC" ] || { echo "langpack: WIKI_SRC=$WIKI_SRC is not a folder" >&2; bad=1; }
  [ -f "$PLAYWRIGHT_CORE" ] \
    || { echo "langpack: PLAYWRIGHT_CORE=$PLAYWRIGHT_CORE is missing (cd ops/langpack && npm ci)" >&2; bad=1; }
  [ -d "$_lp_root/node_modules/puppeteer" ] \
    || { echo "langpack: $_lp_root/node_modules/puppeteer is missing (cloud/zim_viewer_smoke.mjs imports it)" >&2; bad=1; }
  if [ -x "$PACK_GATE_ZIMCHECK" ]; then
    case "$("$PACK_GATE_ZIMCHECK" --version 2>&1 | head -n 1)" in
      "zim-tools "*) ;;
      *) echo "langpack: $PACK_GATE_ZIMCHECK is not zim-tools' zimcheck" >&2; bad=1 ;;
    esac
  fi
  return $bad
}
