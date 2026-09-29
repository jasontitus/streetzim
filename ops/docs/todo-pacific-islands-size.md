# Open question: pacific-islands got BIGGER on rebuild

Observed 2026-09-26. Not blocking — 3 of the first 4 rebuilt regions shrank
10–50% — but it is reproducible and unexplained, so it is written down.

## The numbers

| build | uncompressed | file |
|---|---|---|
| osm-pacific-islands-2026-09-20 | 3.38 GB | **1.91 GB** |
| osm-pacific-islands-2026-09-26 | 2.72 GB | **2.02 GB** |

660 MB **less** content, 110 MB **more** file. Reproduced twice (the second
run was the Suva-centre rebuild), so it is a property of the build, not noise.

## What has been ruled out

- **Wikidata filtering worked**: 624.0 MB → 1.0 MB, as intended.
- **Content is identical where it should be**: tiles 692.8 MB (3,905,639
  entries), satellite 762.0 MB (729,010), terrain 430.8 MB — byte-for-byte
  equal in both, verified by md5 on a sampled satellite tile
  (`satellite/12/3527/1991.avif`, d584cbaf22b5 in both).
- **Compression level is the same**: both logs say `zstd level 22`.
- **Entry counts match**: 4,922,473 vs 4,922,988.
- Every other section shrank or held: search-data 606.3 → 573.9,
  category-index 40.2 → 34.4.

## What it is NOT

The apparent "compression ratio got worse" is an artifact of measuring
`uncompressed/file`: the old build contained 624 MB of highly-compressible
Wikidata JSON, and removing the *most* compressible content necessarily
lowers the average ratio of what remains. That explains a ratio change, not
110 MB of extra bytes.

## What is left

Roughly 200 MB of compressed bytes are unaccounted for: the file should
have fallen by ~100 MB (the Wikidata's compressed footprint) and instead
rose by 110 MB. Leading hypothesis is cluster packing — how compressible and
`compress=False` items (satellite, terrain, 1.19 GB here) interleave into
2 MB clusters, and whether removing the Wikidata block changed that layout.
`libzim.reader.Archive` does not expose `cluster_count`, so this needs
`zimru`/`zimcheck` cluster output or a synthetic two-way build to settle.

## Why it probably only shows here

pacific-islands is unusually satellite-heavy: satellite + terrain are
1.19 GB of a 1.91 GB file, so the compressible remainder is a small slice
and any packing inefficiency in it is proportionally visible. colorado,
hawaii and washington-dc all shrank.

## Next regions to watch

Anything with a large satellite payload: alaska, iceland (already capped),
central-america-caribbean, southeast-asia. If they also grow, this becomes
worth fixing before the continent-scale regions at the end of the queue.
