# TODO: switzerland-light needs its own rebuild recipe

Dropped from `rebuild-old.list` on 2026-09-26.

`cloud/rebuild_old_regions.sh` calls `build-region-fast.sh "$ID" "$BBOX" "$NAME"`
with no per-region flags, and that wrapper hardcodes `--satellite
--satellite-download-zoom 12` and leaves `--max-zoom` at its default of 14.

But `switzerland-light` is defined (cloud/regions.tsv) as "no satellite
imagery, vector tiles capped at z13 (--max-zoom 13). 1.40 GB vs 2.10 GB
full", and web/generate.py advertises it as "without satellite imagery and
with vector map detail capped one zoom level shallower (z13 instead of z14).
1.4 GB instead of 2.1 GB".

So a plain rebuild would produce a ~2.1 GB full satellite build, upload it to
`streetzim-switzerland-light`, and the site would keep describing it as the
1.4 GB light option. **No gate catches this** -- validate, markers, overlap,
device matrix, render and kiwix all pass on a perfectly good full build.

It also has no PBF of its own (`world-data/regions/switzerland-light.osm.pbf`
is absent; it shares switzerland's bbox), so a queue run would additionally
burn a full planet pass producing a byte-duplicate of switzerland.osm.pbf.

## What it needs

Either
  * a per-region flags column in the queue's data file, so this row can carry
    `--no-satellite --max-zoom 13`; or
  * build it by hand the way `.light-himalaya-queue3.sh` did, from the
    `-nosat-z13` variant.

The same applies to any other "light" variant added later. `africa-light` is
already handled separately and is correctly absent from the rebuild list.
