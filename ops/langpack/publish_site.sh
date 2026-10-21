#!/bin/bash
# Put a region's checked recipes on the builder page (/build/), after
# ops/langpack/ship_packs.sh REGION exited 0:
#
#   ops/langpack/publish_site.sh REGION [WEB_DIR]
#
# Refuses unless $LANGPACK_WORK/site/REGION/recipes/REGION/ is exactly what
# ship_packs.sh's site_check.py passed (checked.json: every file's SHA-256).
# Then the folder replaces WEB_DIR/build/recipes/REGION/ (WEB_DIR: this
# checkout's web/) in one atomic exchange, and WEB_DIR/build/recipes/index.json
# is rewritten (tools/langpack/site_index.py); if that fails, the previous
# folder is put back. The recipes are site data, not source (gitignored).
# Then deploy the site as usual (python3 web/generate.py --deploy): a map
# card gets its "Build in another language" link, and the region stays on
# the page, only while archive.org's live ZIM is the recipes' base (name
# and MD5).
# Exit: 0 published; 1 refused or failed (nothing changed).
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$ROOT/ops/langpack/host-env.sh"
REGION=${1:?usage: publish_site.sh REGION [WEB_DIR]}
WEB=${2:-$ROOT/web}
PY=${PYTHON:-python3}
TL=$ROOT/tools/langpack
SRC=$LANGPACK_WORK/site/$REGION/recipes/$REGION
DST=$WEB/build/recipes/$REGION
[ -d "$WEB/build" ] || { echo "publish_site: $WEB/build is not the builder page" >&2; exit 1; }
"$PY" "$TL/checked.py" verify "$SRC" > /dev/null || { echo "publish_site: $SRC is not what ship_packs.sh checked; not published" >&2; exit 1; }
mkdir -p "$WEB/build/recipes"
rm -rf "$DST.new" && cp -a "$SRC" "$DST.new" || exit 1
"$PY" "$TL/checked.py" verify "$DST.new" > /dev/null || { echo "publish_site: the copy differs from the checked files" >&2; rm -rf "$DST.new"; exit 1; }
"$PY" "$TL/checked.py" swap "$DST.new" "$DST" || { rm -rf "$DST.new"; exit 1; }
if ! "$PY" "$TL/site_index.py" "$WEB/build/recipes"; then
  echo "publish_site: site_index.py failed; the previous $REGION is put back" >&2
  if [ -d "$DST.new" ]; then "$PY" "$TL/checked.py" swap "$DST.new" "$DST"; else rm -rf "$DST"; fi
  "$PY" "$TL/site_index.py" "$WEB/build/recipes" > /dev/null 2>&1
  rm -rf "$DST.new"; exit 1
fi
rm -rf "$DST.new"
echo "published $REGION: $(ls "$DST" | grep -c '^[a-z-]*\.json$') files in $DST ($("$PY" "$TL/checked.py" verify "$DST")); deploy the site to put it live"
