#!/bin/bash
# Put a region's checked recipes on the builder page (/build/), after
# ops/langpack/ship_packs.sh REGION exited 0:
#
#   ops/langpack/publish_site.sh REGION [WEB_DIR]
#
# Copies $LANGPACK_WORK/site/REGION/recipes/REGION/ to
# WEB_DIR/build/recipes/REGION/ (WEB_DIR: this checkout's web/), replacing
# the region's previous recipes in one rename, and rewrites
# WEB_DIR/build/recipes/index.json (tools/langpack/site_index.py). The recipes
# are site data, not source (gitignored). Then deploy the site as usual
# (python3 web/generate.py --deploy): the map card gets its "Build in another
# language" link once the live ZIM is the recipes' base.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$ROOT/ops/langpack/host-env.sh"
REGION=${1:?usage: publish_site.sh REGION [WEB_DIR]}
WEB=${2:-$ROOT/web}
PY=${PYTHON:-python3}
SRC=$LANGPACK_WORK/site/$REGION/recipes/$REGION
DST=$WEB/build/recipes/$REGION
[ -f "$SRC/index.json" ] && [ -f "$SRC/region.json" ] || { echo "publish_site: no checked site files in $SRC (run ship_packs.sh $REGION)" >&2; exit 1; }
[ -d "$WEB/build" ] || { echo "publish_site: $WEB/build is not the builder page" >&2; exit 1; }
mkdir -p "$WEB/build/recipes"
rm -rf "$DST.new" && cp -a "$SRC" "$DST.new" || exit 1
rm -rf "$DST.old"; [ -d "$DST" ] && mv "$DST" "$DST.old"
mv "$DST.new" "$DST" && rm -rf "$DST.old"
"$PY" "$ROOT/tools/langpack/site_index.py" "$WEB/build/recipes" || exit 1
echo "published $REGION: $(ls "$DST" | grep -c '^[a-z-]*\.json$') files in $DST; deploy the site to put it live"
