#!/usr/bin/env bash
# run-cpu-count-ab2.sh <tag> <cpu_count|default> : Luxembourg, basic profile, the
# openZIM recipe, with os.cpu_count() left alone or forced by PYTHON_CPU_COUNT.
# Measures how the build's peak memory depends on the worker count.
SZT=/storage/streetzim/sz-tests; tag=$1; n=$2; D=$SZT/cpuab-luxembourg
mkdir -p "$D/dl" "$D/tmp-$tag" "$D/out-$tag"
env=(); [ "$n" != default ] && env=(-e "PYTHON_CPU_COUNT=$n")
echo "start $tag $(date -Is)"
docker run --rm --init --name "sz-test-cpuab-$tag" --user "$(id -u):$(id -g)" \
  --memory 16g --memory-swap 16g --cpu-shares 3072 "${env[@]}" \
  -v "$D:/work" streetzim:hosttest python /app/tools/measure_build.py --json /work/out-$tag/measure.json \
    --watch /work/tmp-$tag --watch /work/out-$tag --log /work/out-$tag/build.log -- \
  streetzim --name osm_en_luxembourg --title Luxembourg --description "Offline map of Luxembourg" \
    --include-poly https://download.geofabrik.de/europe/luxembourg.poly --profile basic \
    --output /work/out-$tag --tmp /work/tmp-$tag --dl /work/dl
echo "docker exit $? $tag $(date -Is)"
