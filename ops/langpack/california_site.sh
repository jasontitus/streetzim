#!/bin/bash
# StreetZim's own California language-pack site (builder page files) and the
# pre-deploy check, with the archive.org items and range proxy it is served
# from: the 13b base in item streetzim-langproto-california-v2, the packs in
# streetzim-langproto-california (fr: streetzim-langproto-california-fr).
# The tools are core (tools/langpack/; docs/language-pack-pipeline.md).
# Env (defaults: ops/langpack/host-env.sh): LANGPACK_WORK, PYTHON; BASE,
#   PACKS_DIR, SITE_OUT (default under $LANGPACK_WORK).
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TL=$ROOT/tools/langpack
. "$ROOT/ops/langpack/host-env.sh"
export PYTHONDONTWRITEBYTECODE=1
export TITLES='{"de": "Kalifornien", "th": "แคลิฟอร์เนีย", "zh": "加利福尼亚州", "es": "California", "ja": "カリフォルニア州", "ko": "캘리포니아주", "fr": "Californie", "en": "California"}'
PY=${PYTHON:-python3}
LIM=(); command -v prlimit > /dev/null && LIM=(prlimit --as=8589934592 --)
N=(nice -n 19 "${LIM[@]}" timeout 7200)
PX=https://streetzim-preview-proxy.tiltastech.workers.dev
B=${BASE:-$LANGPACK_WORK/bases/osm-california-langproto-2026-10-13b.zim}
PK=${PACKS_DIR:-$LANGPACK_WORK/packs-california}
OUT=${SITE_OUT:-$LANGPACK_WORK/site/california}
TAG=$(basename "$B" .zim)
LANGS=de,th,zh,es,ja,ko,fr,en
PITEM=streetzim-langproto-california
BITEM=streetzim-langproto-california-v2
mkdir -p "$OUT/recipes"
echo "== site $(date +%T)"
"${N[@]}" "$PY" "$TL/make_site.py" "$B" --packs-dir "$PK" --region california --region-title California \
  --region-langs en --langs "$LANGS" --titles "$TITLES" --language-meta map \
  --url-template "$PX/$PITEM/{file}" --url-override "fr=$PX/$PITEM-fr/{file}" \
  --base-download "https://archive.org/download/$BITEM/$TAG.zim" \
  --base-url "$PX/$BITEM/$TAG.zim" -o "$OUT/recipes"
printf '{"url": "%s"}\n' "$PX/$BITEM/$TAG.zim" > "$OUT/region-base-url.json"
echo "== site check $(date +%T)"
"${N[@]}" "$PY" "$TL/site_check.py" "$OUT" --base "$B" --packs-dir "$PK" \
  --ia-cache "$LANGPACK_WORK/ia-cache" --ia-fetch --probe-urls
rc=$?
echo "== done rc=$rc $(date +%T)"
exit $rc
