#!/bin/bash
# Re-ship the language recipes that ops/cloud/upload_validated.sh queued in
# langpack-reship.tsv (region <TAB> base file <TAB> when) after re-uploading,
# with LANGPACK_RESHIP_AFTER_PATCH=1, a base whose recipes are published on
# /build/ (an in-place viewer patch). Run it after the rollout: ship_packs.sh
# can take hours per region, which is why the rollout queues instead of
# running it inline.
#
#   ops/langpack/reship_queue.sh [PROJECT_DIR]      (default: this checkout)
#
# One region at a time, in queue order (a region queued twice runs once, for
# its latest base file, found in PROJECT_DIR). A row is dropped when
# ship_packs.sh exits 0 (recipes and torrents ready: ops/langpack/publish_site.sh
# REGION, then the site's deploy) and kept on any other exit (6: archive.org
# has not listed the base yet; run this again later). Exit 0 when the queue
# is empty afterwards, 1 otherwise. SHIP_PACKS overrides ship_packs.sh (tests).
set -uo pipefail
ROOT=${1:-$(cd "$(dirname "$0")/../.." && pwd)}
Q=$ROOT/langpack-reship.tsv
SHIP=${SHIP_PACKS:-$ROOT/ops/langpack/ship_packs.sh}
say() { echo "$(TZ=America/Los_Angeles date '+%F %T %Z') reship_queue: $*"; }
[ -s "$Q" ] || { say "nothing queued"; exit 0; }
exec 9> "$Q.lock"
flock -n 9 || { say "another reship_queue.sh holds the queue"; exit 1; }
# latest base per region, in first-queued order (rows appended while this
# runs are kept for the next run)
N0=$(wc -l < "$Q")
mapfile -t TODO < <(head -n "$N0" "$Q" | awk -F'\t' 'NF >= 2 { if (!($1 in f)) o[++n] = $1; f[$1] = $2 } END { for (i = 1; i <= n; i++) print o[i] "\t" f[o[i]] }')
done_list=()
for row in "${TODO[@]}"; do
  region=${row%%$'\t'*}; file=${row#*$'\t'}
  case "$region" in *[!a-z0-9-]*|"") say "skipping a bad region id '$region'"; continue ;; esac
  base=$ROOT/$file
  if [ ! -s "$base" ]; then say "$region: base $file not in $ROOT — kept"; continue; fi
  say "$region: ship_packs.sh $region $file"
  bash "$SHIP" "$region" "$base"
  rc=$?
  if [ "$rc" = 0 ]; then
    say "$region: recipes ready — publish: ops/langpack/publish_site.sh $region, then deploy the site"
    done_list+=("$region")
  else
    say "$region: ship_packs.sh rc=$rc — kept in the queue"
  fi
done
if [ ${#done_list[@]} -gt 0 ]; then
  # under the short write lock upload_validated.sh appends with
  ( flock 8
    T=$(mktemp "$Q.XXXXXX") || exit 1
    awk -F'\t' -v done=" ${done_list[*]} " -v n0="$N0" 'NR > n0 || index(done, " " $1 " ") == 0' "$Q" > "$T" && mv -f "$T" "$Q"
  ) 8> "$Q.write.lock" || { say "could not rewrite $Q"; exit 1; }
fi
if [ -s "$Q" ]; then say "still queued: $(cut -f1 "$Q" | sort -u | tr '\n' ' ')"; exit 1; fi
say "queue empty"
exit 0
