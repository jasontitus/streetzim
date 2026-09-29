# Runbook: the first Alaska build across the antimeridian

This is a one-time runbook for a person or a Claude Code session on the
build host, after the host pulls the change that moved alaska's row in
`cloud/regions.tsv` from `-180.0,51.0,-130.0,72.0` to
`172.0,51.0,-130.0,72.0` (the western Aleutians, 172E–180, were cut off).

**Why it is needed:** region files are named by region id, not by bbox.
The alaska files on the host were cut for the old bbox, so a build that
reused them would ship without the 172E side. The ops scripts now write a
`<file>.bbox` sidecar next to each extract, Overture parquet and derived
slice, and treat a file whose sidecar names another bbox as stale
(`ops/region-bbox.sh`). The files made before sidecars existed have none.
For a normal row such a file is trusted and its sidecar written. For a row
across the antimeridian the queue treats it as stale and logs a
`WARNING: … has no .bbox sidecar` line. This runbook replaces the files
deliberately, so nothing depends on that fallback.

**Rules for the session:**
- Run only the commands below, from `/storage/streetzim`, one step at a
  time.
- Do not run it while `build-refresh-queue.sh` or `extract-region-pbfs.sh`
  is running (both hold the regions lock; step 1 checks it).
- **Stop and report at any `STOP`.** Don't work around it.

## 1. Check the row and that nothing else is extracting

```sh
cd /storage/streetzim
awk -F'\t' '$1=="alaska"{print NF, $3}' cloud/regions.tsv
pgrep -a -f 'extract-region-pbfs|build-refresh-queue' || echo "idle"
```

Expect `8 172.0,51.0,-130.0,72.0` and `idle`. Otherwise: STOP.

## 2. Re-extract the PBF (both sides of 180)

```sh
./extract-region-pbfs.sh --only alaska --force
cat world-data/regions/alaska.osm.pbf.bbox
osmium fileinfo -e world-data/regions/alaska.osm.pbf | grep -A1 'Bounding box'
```

Expect the sidecar to read `172.0,51.0,-130.0,72.0`, and the data bounding
box to run from -180 to 180 in longitude (both sides of the antimeridian
are present). If the extract fails or the box stops at -180 or 180 on
one side only: STOP.

## 3. Remove the two alaska Overture parquets

```sh
ls -l overture_cache/*-alaska-*.parquet
rm -f overture_cache/addresses-alaska-*.parquet overture_cache/places-alaska-*.parquet
```

The queue downloads them again, for both sides, on alaska's next run and
writes their sidecars.

## 4. Re-derive the tile and search slices

```sh
./derive-region-mbtiles.py --only alaska --src "$WORLD_MBTILES"
./derive-region-search.py  --only alaska --src "$WORLD_SEARCH"
cat world-data/regions/alaska.mbtiles.bbox world-data/regions/alaska.search.jsonl.bbox
```

Use the same world files the queue is given (`WORLD_MBTILES`,
`WORLD_SEARCH`). Expect both sidecars to read `172.0,51.0,-130.0,72.0`.
Otherwise: STOP.

## 5. Build

Run the queue for alaska as usual (`./build-refresh-queue.sh --only alaska
…`). In its log, expect no `no .bbox sidecar` warning for alaska, an
Overture download for both themes, and, from the build,
`Joined N road(s) split at the antimeridian` and
`opening centre: [-149.9003, 61.2181] from registry anchor (Alaska)`.
