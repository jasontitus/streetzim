#!/bin/bash
# Retrofit shipped regional ZIMs with geographic Find-chip shards and replace
# the live files.
#
# For each id in retrofit-chips.list (re-read before every region):
#   1. source = newest local osm-<id>-YYYY-MM-DD.zim, and it must be the file
#      archive.org currently lists (never retrofit a ZIM nobody downloads)
#   2. cloud/swap_viewer_rust.py --reshard-chips → osm-<id>-<today>.zim
#      (keeps the Xapian fulltext + title indexes; repackage_zim.py drops the
#      title index and Kiwix suggestions go to 0)
#   3. the build queue's gates: terrain, validate, routing (A*), xapian
#      search, find — plus an equivalence check against the source (search
#      and suggestion counts, every chip's record count, manifest keys) and
#      the browser smoke (hard)
#   4. cloud/upload_validated.sh, one upload at a time
#
# Never edit this script while it runs (bash reads it incrementally); edit
# the list instead. DRY=1 prints the plan without doing anything.
set -uo pipefail
cd /storage/streetzim
LIST="${LIST:-/storage/streetzim/retrofit-chips.list}"
TSV="${TSV:-/storage/streetzim/retrofit-chips.tsv}"
LOG="${LOG:-/storage/streetzim/retrofit-chips.log}"
REGISTRY=/storage/streetzim/cloud/regions.tsv
PY=/storage/streetzim/venv-linux/bin/python3
IA=/storage/streetzim/venv-linux/bin/ia
NODE=/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node
export CHROME_PATH="${CHROME_PATH:-/home/ot/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
export LD_LIBRARY_PATH=/storage/streetzim/.browser-libs/ex/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export TMPDIR="${TMPDIR:-/storage/streetzim/tmp}"
PORT="${PORT:-8811}"                 # the build queue's browser smoke uses 8801
MIN_AVAIL_GB="${MIN_AVAIL_GB:-30}"
MIN_FREE_GB="${MIN_FREE_GB:-500}"
DRY="${DRY:-0}"
UPLOAD_LOCK=/storage/streetzim/.retrofit-upload.lock

log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" | tee -a "$LOG"; }
row(){ printf "%s\t%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$4" "$(date -Iseconds)" >> "$TSV"; }
[ "$DRY" = 1 ] || [ -f "$TSV" ] || printf "id\tstatus\tzim\tnote\tfinished\n" > "$TSV"

declare -A TRIED=()
next_region(){
  local line r
  while IFS= read -r line || [ -n "$line" ]; do
    r="${line%%#*}"; r="$(echo "$r" | tr -d '[:space:]')"
    [ -z "$r" ] && continue
    [ -n "${TRIED[$r]:-}" ] && continue
    if [ -f "$TSV" ] && awk -F'\t' -v id="$r" '$1==id && ($2=="uploaded" || $2=="upload-pending")' "$TSV" | grep -q .; then
      continue
    fi
    echo "$r"; return 0
  done < "$LIST"
  return 1
}

smoke_find() {  # prints count of restaurant chip records (same as build-refresh-queue.sh)
  "$PY" - "$1" <<'PYEOF' 2>/dev/null || echo 0
import sys, json
from libzim.reader import Archive
a = Archive(sys.argv[1]); total = 0
def n(p):
    return len(json.loads(bytes(a.get_entry_by_path(p).get_item().content).decode()))
try:
    meta = json.loads(bytes(a.get_entry_by_path(
        'category-index/manifest.json').get_item().content))['chips']['restaurants']
    subs = meta.get('sub_chunks') or []
    total = (sum(n(f'category-index/chip-restaurants-{s}.json') for s in subs)
             if subs else n('category-index/chip-restaurants.json'))
except Exception: total = 0
print(total)
PYEOF
}

smoke_search() {  # prints estimated xapian matches
  "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null || echo 0
import sys
from libzim.reader import Archive
from libzim.search import Query, Searcher
a = Archive(sys.argv[1])
print(Searcher(a).search(Query().set_query(sys.argv[2])).getEstimatedMatches())
PYEOF
}

equivalence() {  # equivalence <src> <out> <search> ; exit 0 when the retrofit changed only the chips
  "$PY" - "$1" "$2" "$3" <<'PYEOF'
import hashlib, sys, json
from libzim.reader import Archive
from libzim.search import Query, Searcher
from libzim.suggestion import SuggestionSearcher
SHARD = 2 * 1024 * 1024
src, out, q = sys.argv[1:4]

def search_prefixes(p):
    """{prefix: [chunk names]} from a ZIM's search manifest."""
    a = Archive(p)
    try:
        man = json.loads(bytes(a.get_entry_by_path("search-data/manifest.json").get_item().content))
    except Exception:
        return None, None
    groups = {}
    for name in man.get("chunks", {}):
        base = name.split("~", 1)[0].split("-", 1)[0]
        groups.setdefault(base, []).append(name)
    return a, groups


def search_digest(p, sample):
    """Distinct-record hash per sampled prefix, so a re-split that drops or
    mangles records is caught. Nothing else in the gates reads search-data:
    a lost place is invisible until a user looks for it.

    Sampled, not exhaustive — hashing every record of a 20 GB region took
    over 25 minutes and was killed, which is not a per-region gate. The
    sample is the hottest prefixes plus a deterministic spread, and it is
    the SET of records, since the character layout deliberately places a
    record in one leaf per qualifying word (~1.12x)."""
    a, groups = search_prefixes(p)
    if a is None:
        return None
    out_d = {}
    for base in sample:
        seen = set()
        for nm in groups.get(base, []):
            try:
                blob = bytes(a.get_entry_by_path(f"search-data/{nm}.json").get_item().content)
            except Exception:
                continue
            for rec in json.loads(blob):
                seen.add(json.dumps(rec, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False))
        h = hashlib.sha256()
        for line in sorted(seen):
            h.update(line.encode("utf-8"))
            h.update(b"\n")
        out_d[base] = (len(seen), h.hexdigest()[:16])
    return out_d


def search_sample(p, n_hot=4, n_spread=4):
    """Which prefixes to deep-check: the most fanned-out ones (where a
    re-split does the most work) plus an even spread over the rest."""
    a, groups = search_prefixes(p)
    if a is None:
        return []
    by_fanout = sorted(groups, key=lambda b: (-len(groups[b]), b))
    hot = by_fanout[:n_hot]
    rest = sorted(b for b in groups if b not in hot)
    step = max(1, len(rest) // max(1, n_spread))
    return hot + rest[::step][:n_spread]
def stats(p):
    a = Archive(p)
    m = json.loads(bytes(a.get_entry_by_path('category-index/manifest.json').get_item().content))
    chips = m.get('chips') or {}
    counts, layouts, sizes = {}, {}, {}
    for cid, meta in chips.items():
        subs = meta.get('sub_chunks') or []
        paths = ([f'category-index/chip-{cid}-{s}.json' for s in subs] if subs
                 else [f'category-index/chip-{cid}.json'])
        counts[cid] = sum(len(json.loads(bytes(a.get_entry_by_path(x).get_item().content))) for x in paths)
        layouts[cid] = meta.get('layout', 'legacy')
        sizes[cid] = meta.get('bytes', 0)
    return dict(search=Searcher(a).search(Query().set_query(q)).getEstimatedMatches(),
                suggest=SuggestionSearcher(a).suggest(q).getEstimatedMatches(),
                title_index=a.has_title_index, fulltext=a.has_fulltext_index,
                counts=counts, keys=sorted(k for k in m if k != 'chips'),
                categories=m.get('categories'), total=m.get('total'),
                layouts=layouts, sizes=sizes, entries=a.entry_count)
s, o = stats(src), stats(out)
bad = [k for k in ('search', 'suggest', 'title_index', 'fulltext', 'counts', 'categories', 'total')
       if s[k] != o[k]]
# Manifest keys: the retrofit ADDS keys on purpose -- category_shards once
# place.json crosses PLACE_SHARD_MIN_BYTES, char_split in search-data. Exact
# set equality failed 9 otherwise-perfect regions on 2026-09-17/18. A DROPPED
# key is still fatal: that would be real loss.
_dropped = sorted(set(s['keys']) - set(o['keys']))
_added = sorted(set(o['keys']) - set(s['keys']))
if _dropped:
    bad.append('keys(dropped ' + ','.join(_dropped) + ')')
if _added:
    print('keys added (expected for a reshard): ' + ','.join(_added))
unsharded = [c for c, b in s['sizes'].items() if b > SHARD and o['layouts'].get(c) != 'geo']
sample = search_sample(src)
sd_s = search_digest(src, sample) if sample else None
sd_o = search_digest(out, sample) if sample else None
sd_bad = []
if sd_s is not None and sd_o is not None:
    for base in sorted(set(sd_s) | set(sd_o)):
        if sd_s.get(base) != sd_o.get(base):
            sd_bad.append(f"{base}: {sd_s.get(base)} → {sd_o.get(base)}")
print(f"search {s['search']}→{o['search']} suggest {s['suggest']}→{o['suggest']} "
      f"chips {len(s['counts'])}→{len(o['counts'])} geo={sum(v == 'geo' for v in o['layouts'].values())} "
      f"records {sum(s['counts'].values())}→{sum(o['counts'].values())} entries {s['entries']}→{o['entries']} "
      f"search-prefixes {len(sd_s or {})}→{len(sd_o or {})}")
if bad: print("MISMATCH: " + ", ".join(bad))
if unsharded: print("NOT SHARDED: " + ", ".join(unsharded))
if sd_bad:
    print(f"SEARCH RECORDS CHANGED in {len(sd_bad)} prefix(es): " + "; ".join(sd_bad[:5]))
sys.exit(1 if bad or unsharded or sd_bad else 0)
PYEOF
}

browser_smoke() {  # browser_smoke <zim> <src> <dst> <search> <out-log> ; returns node exit code
  local zim="$1" out="$5"
  ln -sfn "../$(basename "$zim")" "web/$(basename "$zim")"
  "$PY" scripts/serve-web-local.py "$PORT" /storage/streetzim/web > "$TMPDIR/http-$PORT.log" 2>&1 &
  local http=$!; sleep 2
  STREETZIM_SITE="http://localhost:$PORT" ZIM_URL="http://localhost:$PORT/$(basename "$zim")" \
    ZIM_FILE="$(readlink -f "$zim")" \
    SMOKE_ROUTE="$2;$3" SMOKE_SEARCH="$4" timeout 600 "$NODE" cloud/pwa_smoke_test.mjs > "$out" 2>&1
  local rc=$?
  kill "$http" 2>/dev/null; rm -f "web/$(basename "$zim")"
  tail -3 "$out" | sed 's/^/    /' >> "$LOG"
  return $rc
}

wait_for_resources() {
  local said=0 avail free
  while :; do
    avail=$(awk '/^MemAvailable:/{print int($2/1048576)}' /proc/meminfo)
    free=$(df -BG --output=avail /storage | tail -1 | tr -dc '0-9')
    [ "$avail" -ge "$MIN_AVAIL_GB" ] && [ "$free" -ge "$MIN_FREE_GB" ] && return 0
    [ $said -eq 0 ] && log "  waiting: MemAvailable ${avail} GB (need ${MIN_AVAIL_GB}), /storage free ${free} GB (need ${MIN_FREE_GB})"
    said=1; sleep 300
  done
}

log "=== chip retrofit queue start (list=$LIST dry=$DRY port=$PORT)"
while ID=$(next_region); do
  TRIED[$ID]=1
  SRC=$(ls osm-"$ID"-20[0-9][0-9]-[0-9][0-9]-[0-9][0-9].zim 2>/dev/null | sort | tail -1)
  if [ -z "$SRC" ]; then log "skip $ID: no local dated ZIM"; [ "$DRY" = 1 ] || row "$ID" skipped - "no local ZIM"; continue; fi
  LIVE=$("$IA" metadata "streetzim-$ID" 2>/dev/null | "$PY" -c "
import sys, json
try:
    m = json.load(sys.stdin); print('yes' if any(f.get('name') == sys.argv[1] for f in m.get('files', [])) else 'no')
except Exception: print('err')" "$SRC")
  if [ "$LIVE" != yes ]; then log "skip $ID: $SRC is not the live file (ia: $LIVE)"; [ "$DRY" = 1 ] || row "$ID" skipped "$SRC" "not live ($LIVE)"; continue; fi
  REG=$(awk -F'\t' -v id="$ID" '$1==id' "$REGISTRY" | head -1)
  BBOX=$(echo "$REG" | cut -f3); RSRC=$(echo "$REG" | cut -f5); RDST=$(echo "$REG" | cut -f6); SEARCH=$(echo "$REG" | cut -f7)
  if [ -z "$BBOX" ] || [ -z "$RSRC" ] || [ -z "$RDST" ] || [ -z "$SEARCH" ]; then
    log "skip $ID: incomplete registry row"; [ "$DRY" = 1 ] || row "$ID" skipped "$SRC" "registry"; continue
  fi
  TODAY=$(date +%Y-%m-%d)
  OUT_BASE="osm-$ID-$TODAY.zim"
  for sfx in b c d e f; do
    [ -e "$OUT_BASE" ] || [ -e "$OUT_BASE.tmp" ] || break
    OUT_BASE="osm-$ID-$TODAY$sfx.zim"
  done
  if [ -e "$OUT_BASE" ]; then log "skip $ID: no free output name for $TODAY"; [ "$DRY" = 1 ] || row "$ID" skipped "$SRC" "name"; continue; fi
  log "=== $ID: $SRC ($(du -h "$SRC" | cut -f1)) → $OUT_BASE  search='$SEARCH' route $RSRC → $RDST"
  if [ "$DRY" = 1 ]; then continue; fi

  wait_for_resources
  T0=$(date +%s)
  RLOG="/storage/streetzim/$ID-retrofit-$TODAY.log"
  if ! nice -n 10 ionice -c2 -n7 "$PY" -u cloud/swap_viewer_rust.py "$SRC" "$OUT_BASE" \
       --reshard-chips --reshard-search > "$RLOG" 2>&1 \
     || [ ! -s "$OUT_BASE" ]; then
    log "  RETROFIT FAILED — see $RLOG"; rm -rf "$OUT_BASE" "$OUT_BASE.tmp" "$OUT_BASE.pack-stage"; row "$ID" retrofit-failed "$SRC" "$(tail -1 "$RLOG" | cut -c1-120)"; continue
  fi
  grep -E "^  chip-|^  chips:|X-namespace" "$RLOG" | sed 's/^/    /' >> "$LOG"
  log "  retrofit done in $(( ($(date +%s) - T0) / 60 )) min ($(du -h "$OUT_BASE" | cut -f1))"

  G=""
  _tsz=$(( $(stat -c%s "$OUT_BASE") / 1073741824 ))
  _tto=$(( 900 + _tsz * 300 )); [ "$_tto" -gt 14400 ] && _tto=14400
  if timeout "$_tto" nice -n 10 "$PY" cloud/check_terrain_coverage.py --zooms 10-12 -- "$OUT_BASE" "$BBOX" >> "$LOG" 2>&1; then
    log "  gate terrain: OK"
  else
    _trc=$?; G="$G terrain"
    [ "$_trc" -eq 124 ] && log "  gate terrain: FAIL (timed out after ${_tto}s — inconclusive)" || log "  gate terrain: FAIL"
  fi
  if TERRAIN_STRIPE_TOLERATE=10 timeout 7200 "$PY" cloud/validate_zim.py "$OUT_BASE" >> "$LOG" 2>&1; then log "  gate validate: OK"; else G="$G validate"; log "  gate validate: FAIL"; fi
  ROUTE_OUT=$(timeout 2400 "$PY" cloud/route_cli.py --zim="$OUT_BASE" --src="$RSRC" --dst="$RDST" --mode=all --max-pops=5000000 2>&1)
  echo "$ROUTE_OUT" | tail -6 | sed 's/^/    /' >> "$LOG"
  ASTAR_OK=$(echo "$ROUTE_OUT" | awk '/=== mode: astar/{f=1} f&&/route OK/{print 1; exit} /=== mode: hwy2/{f=0}')
  if [ "${ASTAR_OK:-0}" = 1 ]; then log "  gate routing: OK (astar)"; else G="$G routing"; log "  gate routing: FAIL (astar found no route)"; fi
  SN=$(smoke_search "$OUT_BASE" "$SEARCH"); FN=$(smoke_find "$OUT_BASE")
  [ "${SN:-0}" -ge 1 ] 2>/dev/null && log "  gate search('$SEARCH'): $SN" || { G="$G search"; log "  gate search: FAIL ($SN)"; }
  [ "${FN:-0}" -ge 1 ] 2>/dev/null && log "  gate find(restaurants): $FN" || { G="$G find"; log "  gate find: FAIL ($FN)"; }
  EQ=$(equivalence "$SRC" "$OUT_BASE" "$SEARCH" 2>&1); EQRC=$?
  echo "$EQ" | tail -4 | sed 's/^/    /' >> "$LOG"
  if [ $EQRC -eq 0 ]; then log "  gate equivalence: OK"; else G="$G equivalence"; log "  gate equivalence: FAIL"; fi
  if browser_smoke "$OUT_BASE" "$RSRC" "$RDST" "$SEARCH" "/storage/streetzim/$ID-retrofit-smoke-$TODAY.log"; then
    log "  gate browser: OK"
  else
    G="$G browser"; log "  gate browser: FAIL (see $ID-retrofit-smoke-$TODAY.log)"
  fi
  if [ -n "$G" ]; then
    log "  GATES FAILED:$G — NOT uploading $OUT_BASE (kept for inspection)"
    row "$ID" gate-failed "$OUT_BASE" "$G"; continue
  fi

  log "  all gates passed — uploading $OUT_BASE"
  ( flock -w 43200 9 || exit 99
    PROJECT_DIR=/storage/streetzim TERRAIN_STRIPE_TOLERATE=10 bash cloud/upload_validated.sh "$ID" "$OUT_BASE" >> "$LOG" 2>&1
  ) 9> "$UPLOAD_LOCK"
  URC=$?
  case $URC in
    0) log "  uploaded → https://archive.org/details/streetzim-$ID"; row "$ID" uploaded "$OUT_BASE" "$(( ($(date +%s) - T0) / 60 )) min" ;;
    6) log "  upload transferred but not yet listed — pending (cloud/finish_pending_uploads.sh)"; row "$ID" upload-pending "$OUT_BASE" "rc=6" ;;
    *) log "  UPLOAD FAILED rc=$URC — kept $OUT_BASE"; row "$ID" upload-failed "$OUT_BASE" "rc=$URC" ;;
  esac
done
log "=== chip retrofit queue complete"
