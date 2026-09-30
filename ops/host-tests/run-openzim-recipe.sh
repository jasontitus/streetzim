#!/usr/bin/env bash
# run-openzim-recipe.sh dc|switzerland
# The exact recipe from docs/zimfarm.md, as a Zimfarm task runs it: the `streetzim`
# command in the image, default profile (full: Wikidata, Wikipedia text from the
# API, Overture latest, terrain, POIs in Kiwix search; no satellite), tiles made
# with tilemaker from the Geofabrik extract, nothing pre-seeded. Limits as a
# maps recipe: 16 GiB memory, cpu shares 3072. One deviation, output-neutral:
# --tmp and --dl point at /storage instead of the container's own disk (the
# shared docker volume has ~56 GB free). --init so a SIGTERM reaches the build.
. "$HOME/sz-env.sh" || exit
sz_round_quiet || exit 4
case "$1" in
  dc) name=osm_en_district-of-columbia; title="Washington, D.C."; desc="Offline map of Washington, D.C. with search and routing"; poly=https://download.geofabrik.de/north-america/us/district-of-columbia.poly ;;
  switzerland) name=osm_en_switzerland; title="Switzerland"; desc="Offline map of Switzerland with search and routing"; poly=https://download.geofabrik.de/europe/switzerland.poly ;;
  *) echo "usage: $0 dc|switzerland"; exit 2 ;;
esac
D=$SZT/openzim-$1
[ -e "$D" ] && { echo "exists: $D"; exit 2; }
mkdir -p "$D/out" "$D/tmp" "$D/dl"
echo "start $(date -Is)"; git -C "$SZT/next" rev-parse HEAD; docker image inspect streetzim:hosttest -f '{{.Id}}'
docker run --rm --init --name "sz-test-openzim-$1" --user "$(id -u):$(id -g)" \
  --memory 16g --memory-swap 16g --cpu-shares 3072 \
  -v "$D/out:/output" -v "$D:/work" \
  streetzim:hosttest python /app/tools/measure_build.py --json /output/measure.json \
    --watch /work/tmp --watch /work/dl --watch /output --log /output/build.log -- \
  streetzim --name "$name" --title "$title" --description "$desc" \
    --include-poly "$poly" --output /output --stats-filename /output/task_progress.json \
    --tmp /work/tmp --dl /work/dl
echo "docker exit $?"; ls -l "$D/out"; echo "end $(date -Is)"
