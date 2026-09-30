#!/usr/bin/env bash
# run-nl-full.sh <tag> <image> : the Netherlands, full profile, the openZIM recipe with
# its Wikipedia ZIM option (--wikipedia-zim-url, the host's copy mounted read-only where
# the command's download cache expects it), in a Zimfarm-sized container (--memory 16g,
# --cpu-shares 3072). --dl starts from nl-full-seed (production's Wikidata cache and
# Wikipedia titles), so the Wikimedia APIs are asked only for what those lack.
S=/storage/streetzim/sz-tests; tag=$1 img=$2; D=$S/measured-$tag; C=sz-test-measured-$tag
W=/storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim; WN=_wiki_wikipedia_en_all_maxi_2026-02.zim
[ -e "$D/out" ] && { echo "exists: $D/out"; exit 2; }
mkdir -p "$D/out" "$D/tmp" && cp -a --reflink=auto $S/nl-full-seed/dl "$D/dl" && touch "$D/dl/wikipedia/$WN"
for f in $S/profile-netherlands/dl/osm/*netherlands-latest.osm.pbf*; do mkdir -p "$D/dl/osm"; ln "$f" "$D/dl/osm/"; done
echo "start $tag $(date -Is) image $(docker image inspect "$img" -f '{{.Id}}')"
( until docker inspect "$C" >/dev/null 2>&1; do sleep 1; done
  bash $S/memanon.sh "$C" "$D/memanon.tsv" & bash $S/memprofile.sh "$C" "$D/out/build.log" "$D/memprofile.tsv"; wait ) &
docker run --rm --init --name "$C" --user "$(id -u):$(id -g)" \
  --memory 16g --memory-swap 16g --cpu-shares 3072 \
  -v "$D:/work" -v /storage/streetzim/wiki-src:/wiki:ro -v "$W:/work/dl/wikipedia/$WN:ro" \
  "$img" python /app/tools/measure_build.py --json /work/out/measure.json \
    --watch /work/tmp --watch /work/out --log /work/out/build.log -- \
  streetzim --name osm_en_netherlands --title Netherlands --description "Offline map of the Netherlands" \
    --include-poly https://download.geofabrik.de/europe/netherlands.poly --profile full \
    --wikipedia-zim-url file:///wiki/wikipedia_en_all_maxi_2026-02.zim \
    --output /work/out --tmp /work/tmp --dl /work/dl --stats-filename /work/out/task_progress.json
echo "docker exit $? $tag $(date -Is)"; wait
