#!/usr/bin/env bash
# run-measured.sh <tag> <image> <poly url> <name> <title> [extra streetzim args...]
# The openZIM recipe in a Zimfarm-sized container (--memory 16g, --cpu-shares
# 3072, no CPU quota), sampled by memprofile.sh. Output in $SZT/measured-<tag>/.
SZT=/storage/streetzim/sz-tests; tag=$1 img=$2 poly=$3 name=$4 title=$5; shift 5
D=$SZT/measured-$tag; C=sz-test-measured-$tag
[ -e "$D/out" ] && { echo "exists: $D/out"; exit 2; }
mkdir -p "$D/out" "$D/tmp" "$D/dl"
# Reuse a download of the same extract if one exists (a hard link, same bytes).
for f in $SZT/*/dl/osm/*"$(basename "${poly%.poly}")"-latest.osm.pbf; do
  [ -f "$f" ] && [ ! -e "$D/dl/osm/$(basename "$f")" ] && { mkdir -p "$D/dl/osm"; ln "$f" "$D/dl/osm/"; ln "$f.source.json" "$D/dl/osm/" 2>/dev/null; break; }
done
echo "start $tag $(date -Is) image $(docker image inspect "$img" -f '{{.Id}}')"
( until docker inspect "$C" >/dev/null 2>&1; do sleep 1; done
  bash $SZT/memprofile.sh "$C" "$D/out/build.log" "$D/memprofile.tsv" ) &
docker run --rm --init --name "$C" --user "$(id -u):$(id -g)" \
  --memory 16g --memory-swap 16g --cpu-shares 3072 \
  -v "$D:/work" "$img" python /app/tools/measure_build.py --json /work/out/measure.json \
    --watch /work/tmp --watch /work/out --log /work/out/build.log -- \
  streetzim --name "$name" --title "$title" --description "Offline map of $title" \
    --include-poly "$poly" --output /work/out --tmp /work/tmp --dl /work/dl "$@"
echo "docker exit $? $tag $(date -Is)"; wait
