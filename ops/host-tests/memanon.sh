#!/usr/bin/env bash
# memanon.sh <container> <out.tsv> : every second, the container's anonymous
# memory (what a memory limit kills for; page cache is reclaimed first) plus
# shmem and kernel memory, from its cgroup's memory.stat. Writes the running
# peak too, so a spike between samples of a slower sampler is not missed.
C=$1; OUT=$2
id=$(docker inspect -f '{{.Id}}' "$C" 2>/dev/null) || exit 1
P=/sys/fs/cgroup/system.slice/docker-$id.scope
printf 't_s\tanon_gb\tshmem_gb\tkernel_gb\tpeak_anon_gb\n' > "$OUT"; t0=$(date +%s); peak=0
while [ -r "$P/memory.stat" ]; do
  read -r a s k < <(awk '/^anon /{a=$2} /^shmem /{s=$2} /^kernel /{k=$2} END{print a+0, s+0, k+0}' "$P/memory.stat" 2>/dev/null) || break
  [ "$a" -gt "$peak" ] && peak=$a
  awk -v t=$(( $(date +%s)-t0 )) -v a=$a -v s=$s -v k=$k -v p=$peak 'BEGIN{printf "%d\t%.2f\t%.2f\t%.2f\t%.2f\n", t, a/1e9, s/1e9, k/1e9, p/1e9}' >> "$OUT"
  sleep 1
done
