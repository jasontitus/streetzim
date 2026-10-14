# StreetZim's build host: settings for the language-pack pipeline
# (docs/language-pack-pipeline.md). Source it before running
# tools/langpack/build_packs.sh or run_gates.sh there:
#
#   . ops/langpack/host-env.sh
#
# Values already set in the environment win.
#
# Working files (verdicts, gated recipes, merged files, gate logs): on
# /storage, never /home; the checkout's own langpack-work/ (gitignored).
export LANGPACK_WORK=${LANGPACK_WORK:-/storage/streetzim/langpack-work}
# The host is shared with other builds: pause the heavy steps below 40 GB.
export LANGPACK_MIN_FREE_GB=${LANGPACK_MIN_FREE_GB:-40}
export PYTHON=${PYTHON:-/storage/streetzim/venv-linux/bin/python3}
export WIKI_SRC=${WIKI_SRC:-/storage/streetzim/wiki-src}
# zim-tools' zimcheck (the zimcheck on PATH here is zimru's, which the
# scripts refuse); unpack zim-tools 3.8.0 next to kiwix-tools.
export PACK_GATE_ZIMCHECK=${PACK_GATE_ZIMCHECK:-/storage/streetzim/tools/zim-tools_linux-x86_64-3.8.0/zimcheck}
export KIWIX_SERVE=${KIWIX_SERVE:-/storage/streetzim/tools/kiwix-tools_linux-x86_64-3.8.2/kiwix-serve}
export NODE=${NODE:-/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node}
export BROWSER_LIBS=${BROWSER_LIBS:-/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu}
# The browser gates also need PLAYWRIGHT_CORE (playwright-core's index.mjs)
# and CHROME_PATH (a Chromium); neither is installed in /storage/streetzim
# yet. The 2026-10 runs used /storage/experiments/langpack/browser's
# playwright-core and ~/.cache/ms-playwright/chromium-1217.
