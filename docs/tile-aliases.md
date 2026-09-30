# Identical tiles stored once (ZIM aliases)

Since 2026-09, the libzim writer stores each distinct tile once. The first tile
with some bytes is written as an item. Every later tile with exactly the same
bytes becomes a ZIM **alias** (`Creator.add_alias`): a second directory entry
that points at the same cluster and blob. openzim/maps stores tiles the same
way. The code is in `streetzim/tile_alias.py`, and it covers:

- vector tiles (`tiles/`), after gzip decompression, in `create_zim`
  (`streetzim/zim_writer.py`);
- raster tiles (`satellite/`, `terrain/`) in `create_zim`. These sit in
  uncompressed clusters, so each repeat would otherwise cost its full size;
- `cloud/repackage_zim.py`. The reader API does not say which entries share
  a blob, so repackage finds the duplicates again by content and writes them
  as aliases.

An alias is not a redirect. libzim, kiwix-serve and the /drive PWA's reader
(`web/drive/zim-reader.js`) all return it as an ordinary item with the
target's mimetype and bytes, with no HTTP 302. `cloud/validate_zim.py` counts
aliases as tiles, because it skips only `is_redirect` entries.

- **Turning it off:** set `STREETZIM_TILE_ALIASES=0`.
- **Size limit:** only small tiles are remembered: vector tiles up to 128 B,
  satellite and terrain tiles up to 640 B (`MAX_ALIAS_BYTES` in
  `streetzim/tile_alias.py`, per path prefix; a prefix without a cap is an
  error). Only a tile with nothing specific to its position (open sea, the
  inside of one landcover polygon, flat terrain) can repeat, and those are
  small: every vector duplicate seen is 55–77 B; a one-colour raster tile is
  44–54 B as terrain, 198–338 B as a 256 px satellite tile and 354–560 B at
  512 px, and deep-ocean Sentinel-2 imagery is about 300–500 B.
- **Keys:** a vector tile is keyed on its bytes, so an alias always has
  identical bytes. A raster tile is keyed on an 88-bit BLAKE2b digest plus
  its length. The remaining risk is two raster tiles of the same length
  with the same digest; for 30 M remembered tiles the chance of any such
  pair is about 1e-12.
- **Memory:** the table remembers every *distinct* tile under the cap (the
  first copy has to be remembered before anyone knows it repeats). Measured
  with tracemalloc (CPython 3.11, 1 M distinct tiles): 114 B per raster
  entry and 171–235 B per vector entry of 64–128 B; the old table took
  162 B. A million distinct small raster tiles is ~115 MB. The caps keep
  the count low: Monaco has 108 distinct vector tiles, of which 2 are under
  128 B (the old 4 KiB cap let 81 in). On a synthetic stream of 1 M
  distinct tiles of 64–2,080 B, the table holds 46,875 entries (10.2 MB) as
  vector tiles or 296,875 (31.9 MB) as raster tiles; with the old cap and
  table it held all of them (162 MB). The build log's alias summary prints
  the count and the estimate.
- **Reproducible builds:** tiles are added in a fixed order (z, x, then TMS
  row from the MBTiles; sorted file names for raster caches). The first copy
  is therefore the same on every build.
- **Where aliases are not written:** `--zim-builder rust` (the path that
  `ops/build-region-fast.sh` uses) has no alias record in the manifest or in
  zimru, so it still writes every copy. `ops/cloud/swap_viewer_rust.py` also
  rewrites through that packer, so it turns aliases back into copies. The
  output is still correct, just without the saving.

## Is it a big saving? No

### Duplicates in the MBTiles

| MBTiles (Monaco) | tiles | distinct | duplicate tiles | duplicate bytes (decompressed) |
|---|---|---|---|---|
| `monaco.mbtiles` / OpenFreeMap cut | 246 | 108 | 138 (56%) | 7,745 of 723,833 B (1.1%) |
| tilemaker v3, full extract (`tm-full`) | 245 non-empty | 58 | 187 (76%) | 10,285 of 711,714 B (1.4%) |

By zoom level (tm-full): z0–z11 have no duplicates. z12 has 5 of 15, z13 has
36 of 50 and z14 has 146 of 162. Every duplicate is a 55–57 byte "sea only"
tile. The OpenFreeMap cut has the same 138 duplicates at 56–57 B. Counting the
stored (gzipped) bytes gives the same tile counts, with 2.2% and 3.6% of
stored bytes.

The CI build (`--area monaco`, clipped to the 7.40,43.72,7.44,43.76 bbox the preset had then) had
only 28 non-empty tiles and no duplicates.

### Effect on the ZIM file

Tiles only, same input and same packing (libzim 9.8.2, 2 MiB clusters, the
builder's "Tile z/x/y" titles), with and without aliases:

| input | entries | tile blobs | ZIM without → with aliases | saved | per duplicate |
|---|---|---|---|---|---|
| OpenFreeMap cut | 246 | 246 → 108 | 320,444 → 319,690 B | 754 B (0.24%) | 5.5 B |
| tilemaker tm-full | 245 | 245 → 58 | 341,203 → 340,860 B | 343 B (0.10%) | 1.8 B |

The saving is small because the alias still writes a dirent of the same size
as the item it replaces. What goes is a 4-byte blob offset, plus whatever zstd
spent on a 56-byte block it had already seen in the same cluster, which is
only a few bytes.

For full Monaco builds (viewer, search, Find chips), build-to-build noise is
larger than the effect. Two builds with aliases, from the same input with the
same flags, differed by 1.7 KB and 3.0 KB. The aliased and unaliased builds
differed by −0.2 KB (OpenFreeMap) and −5.2 / +2.8 KB (tm-full). In every pair,
all 245/246 tile paths returned identical bytes.

One side effect is worth having: zimcheck 3.8 `-R` reports a
"Redundant Data" warning for each duplicate pair. The OpenFreeMap Monaco ZIM
went from 139 of those warnings to 1 (the remaining one is a search chunk).
On a sea-heavy country, that removes hundreds of thousands of warning lines.

### Scaling to a sea-heavy country (an estimate, not a measurement)

No large MBTiles was available, so this is extrapolated from Monaco:

- **Vector:** the saving is about 2–6 B per duplicate tile. Suppose a country
  with a lot of sea or uniform land has 5 M tiles, 60% of them duplicates. It
  saves roughly 6–18 MB on a ZIM of several GB, well under 1%. Tiles are
  added in (z, x, y) order, so runs of sea tiles share clusters, and zstd
  already removes most of the repetition. That is why the per-duplicate
  figure should not grow. The ceiling, if zstd matched nothing at all, would
  be about 60 B per duplicate.
- **Raster (satellite/terrain):** this could be where a real saving is,
  because these clusters are uncompressed and a repeat costs its full size.
  It is not measured, because the local caches (11 terrain and 13 satellite
  tiles) had no duplicates. Satellite imagery over sea is noisy, so repeats
  are probably rare. Flat terrain-RGB over sea might repeat, if the terrain
  cache contains sea tiles at all.
- **What cannot be claimed:** any figure for a real country, the duplicate
  fraction for OpenFreeMap tiles at country scale, or any saving on
  production builds made with `--zim-builder rust`. Those builds do not
  alias.

### A bigger saving next door (not done here)

Every tile entry carries the title `Tile z/x/y`. Leaving the title empty (so
libzim uses the path) saved 4,112 B on the 246-tile Monaco set, about 17 B per
tile. That applies to every tile, not just the duplicates, so it is roughly
3–9× the alias saving per tile. Tiles are not front articles, so those titles
are not in Kiwix's suggestion index.
