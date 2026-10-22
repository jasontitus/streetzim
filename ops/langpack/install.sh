#!/bin/bash
# One-time setup of the language-pack pipeline on StreetZim's build host
# (ops/langpack/INSTALL.md says what each step is for).
#
#   ops/langpack/install.sh            # says what it would do, changes nothing
#   ops/langpack/install.sh --apply    # does it
#
# 1. zim-tools 3.8.0 (the official zimcheck the gates require) at
#    $TOOLS/zim-tools_linux-x86_64-3.8.0/: unpacked from the release tarball,
#    whose SHA-256 must be ZIM_TOOLS_SHA256 (the copy already on the host,
#    ZIM_TOOLS_TARBALL, is used when it matches; else it is downloaded),
#    in a temporary folder inside $TOOLS. A folder there whose zimcheck is
#    not 3.8.0 is moved aside (<name>.broken-<time>), never unpacked into.
# 2. playwright-core for the browser gates: `npm ci` in ops/langpack/ only
#    (its own package.json and lock; never npm at the repository root, which
#    would rewrite production's package.json), with --prefix naming that
#    folder too. Refused unless that folder has both files.
# 3. $LANGPACK_WORK (/storage/streetzim/langpack-work) with wpcache/ seeded
#    from SEED_WPCACHE (the Greece end-to-end's cache: 21 languages' SQL
#    dumps, title maps, Wikidata text; a reflink copy where the filesystem
#    can), only if wpcache/ does not exist yet.
# 4. langpack_env_check (ops/langpack/host-env.sh): every path the tools need.
#
# Nothing here touches running builds; every step is skipped when already done.
# Exit: 0 done (or, without --apply, nothing missing); 1 a step failed; 3
# without --apply: something to do.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
APPLY=0; [ "${1:-}" = --apply ] && APPLY=1
TOOLS=${TOOLS:-/storage/streetzim/tools}
ZT=$TOOLS/zim-tools_linux-x86_64-3.8.0
ZIM_TOOLS_URL=${ZIM_TOOLS_URL:-https://download.openzim.org/release/zim-tools/zim-tools_linux-x86_64-3.8.0.tar.gz}
ZIM_TOOLS_SHA256=${ZIM_TOOLS_SHA256:-9d4cbf68675670176fe72f096364c7f4ecf09950340e769bdb81dcbb5030da00}
ZIM_TOOLS_TARBALL=${ZIM_TOOLS_TARBALL:-/storage/experiments/langpack/merge/tools/dl/zim-tools.tar.gz}
SEED_WPCACHE=${SEED_WPCACHE:-/storage/experiments/langpack-prod/e2e/wpcache}
export LANGPACK_WORK=${LANGPACK_WORK:-/storage/streetzim/langpack-work}
TODO=0
step() { echo "== $*"; }
run() { if [ $APPLY = 1 ]; then echo "   + $*"; "$@"; else echo "   would run: $*"; TODO=1; fi; }

step "1. zim-tools 3.8.0 at $ZT"
if [ -x "$ZT/zimcheck" ] && "$ZT/zimcheck" --version 2>&1 | head -n 1 | grep -q '^zim-tools 3\.8\.0'; then
  echo "   present: $("$ZT/zimcheck" --version 2>&1 | head -n 1)"
else
  TMP=; [ $APPLY = 1 ] && { mkdir -p "$TOOLS" && TMP=$(mktemp -d "$TOOLS/.zimtools.XXXXXX") || exit 1; }
  TB=${TMP:-<tmp>}/zim-tools.tar.gz
  if [ -f "$ZIM_TOOLS_TARBALL" ] && [ "$(sha256sum "$ZIM_TOOLS_TARBALL" | cut -d' ' -f1)" = "$ZIM_TOOLS_SHA256" ]; then
    run cp "$ZIM_TOOLS_TARBALL" "$TB"
  else
    run curl -fsSL --retry 3 -o "$TB" "$ZIM_TOOLS_URL"
  fi
  if [ $APPLY = 1 ]; then
    got=$(sha256sum "$TB" | cut -d' ' -f1)
    [ "$got" = "$ZIM_TOOLS_SHA256" ] || { echo "   SHA-256 $got is not $ZIM_TOOLS_SHA256: not installed" >&2; rm -rf "$TMP"; exit 1; }
    tar -xzf "$TB" -C "$TMP" || { rm -rf "$TMP"; exit 1; }
    [ -x "$TMP/zim-tools_linux-x86_64-3.8.0/zimcheck" ] || { echo "   the tarball has no zim-tools_linux-x86_64-3.8.0/zimcheck" >&2; rm -rf "$TMP"; exit 1; }
    if [ -e "$ZT" ]; then                     # there, but its zimcheck is not 3.8.0: aside, not into it
      echo "   + mv $ZT $ZT.broken-$(date +%Y%m%d-%H%M%S)"
      mv "$ZT" "$ZT.broken-$(date +%Y%m%d-%H%M%S)" || { rm -rf "$TMP"; exit 1; }
    fi
    mv "$TMP/zim-tools_linux-x86_64-3.8.0" "$ZT" && rm -rf "$TMP"
    "$ZT/zimcheck" --version 2>&1 | head -n 1
  else
    echo "   would unpack zim-tools_linux-x86_64-3.8.0/ into $TOOLS (SHA-256 checked)"
  fi
fi

step "2. playwright-core in $ROOT/ops/langpack/node_modules"
if [ ! -f "$ROOT/ops/langpack/package.json" ] || [ ! -f "$ROOT/ops/langpack/package-lock.json" ]; then
  echo "   ops/langpack has no package.json + package-lock.json: refusing to run npm (it would walk up)" >&2; exit 1
fi
if [ -f "$ROOT/ops/langpack/node_modules/playwright-core/index.mjs" ]; then
  echo "   present: playwright-core $(grep '"version"' "$ROOT/ops/langpack/node_modules/playwright-core/package.json" | head -1 | tr -dc '0-9.')"
else
  NPM=${NPM:-$(dirname "${NODE:-/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node}")/npm}
  [ -x "$NPM" ] || NPM=npm
  # npm is a node script: its node on PATH
  if [ $APPLY = 1 ]; then (cd "$ROOT/ops/langpack" && echo "   + (cd ops/langpack && $NPM ci --prefix $ROOT/ops/langpack)" \
                            && PATH="$(dirname "$NPM"):$PATH" "$NPM" ci --prefix "$ROOT/ops/langpack" --no-audit --no-fund) || exit 1
  else echo "   would run: (cd $ROOT/ops/langpack && $NPM ci --prefix $ROOT/ops/langpack)"; TODO=1; fi
fi

step "3. $LANGPACK_WORK and its wpcache/"
if [ -d "$LANGPACK_WORK/wpcache" ]; then
  echo "   present: $LANGPACK_WORK/wpcache"           # (no du: a walk of the shared HDD)
else
  run mkdir -p "$LANGPACK_WORK"
  if [ -d "$SEED_WPCACHE" ]; then run cp -a --reflink=auto "$SEED_WPCACHE" "$LANGPACK_WORK/wpcache"
  else run mkdir -p "$LANGPACK_WORK/wpcache"; fi
fi

step "4. host-env.sh check"
. "$ROOT/ops/langpack/host-env.sh"
if langpack_env_check; then echo "   everything the pipeline needs is there"
elif [ $APPLY = 1 ]; then exit 1; else TODO=1; fi
[ $APPLY = 1 ] || [ $TODO = 0 ] || { echo "(dry run: run again with --apply)"; exit 3; }
exit 0
