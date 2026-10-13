# Language-ready builds

A language-ready ZIM is today's region ZIM plus what a later language
switch or language pack needs ([language-packs.md](language-packs.md),
[language-matrix.md](language-matrix.md)): its tiles carry `name:xx` for
the matrix languages, and its `map-config.json` records which ones, and the
region's own languages. Labels, search and the viewer do not change.

## The builder

| flag | effect |
|---|---|
| `--tile-languages ar,de,…` | The `name:xx` the tiles carry besides English (`name:latin`). With `--mbtiles` the caller vouches for them; without, the build's own tilemaker run writes them. Sets `STREETZIM_TILE_LANGUAGES` for the build (the fallback glyphs then cover their scripts, [formats.md](formats.md)) and records `tileLanguages` (sorted) in `map-config.json`. `en` is refused: English is always there. |
| `--region-languages el` | The region's own languages, most used first. Recorded as `regionLanguages` in `map-config.json`; nothing else reads it yet. |

A build that passes neither is byte for byte what it was.

**Names in the tiles** (`resources/tilemaker/process-openmaptiles.lua`,
`SetNameAttributes`): an OSM name tag can list several names separated by
`;` (`name:zh=洛杉矶;洛杉磯`, Simplified;Traditional), and the label drew
them all. Every name the profile writes (`name:latin`, `name_int`,
`name:xx`) keeps the first non-empty one, trimmed, and `name:zh` comes
from `name:zh-Hans` when OSM has it. This applies to every tilemaker run
with this profile; the world tiles change only when they are rebuilt.
`tests/test_tile_profile_names.py` runs the profile's Lua (5.1, through
`lupa`).
`streetzim.languages.MATRIX_TILE_LANGUAGES` is the list (21: ar de el es fa
fr he hi id it ja ko nl pl pt ru th tr uk vi zh).
`cloud/region-languages.tsv` holds every registry region's own languages;
`tests/test_region_languages.py` keeps it complete.

## Production: `STREETZIM_ML_TILES=1`

The world tiles carry no `name:xx`, so `build-region-fast.sh` (and the
rebuild driver, which runs it) cuts a region's tiles itself when
`STREETZIM_ML_TILES=1`:

1. `ops/region-ml-tiles.py --src-id <parent> -- <id> <bbox>` runs tilemaker
   on `world-data/regions/<parent>.osm.pbf` and the registry bbox, and
   writes `world-data/regions/<parent>.ml.mbtiles` with sidecars `.bbox`,
   `.langs`, `.profile` and `.pbf`. `.profile` is what decides the tiles in
   an image: the `resources/tilemaker` tree, the `Dockerfile` blob and its
   `TILEMAKER_REF`. A file whose sidecars all match is reused without
   asking the registry or pulling; any change (bbox, languages, profile, a
   new extract) makes it again. A new commit that leaves the profile alone
   does not.
2. **The image** is one CI published for main
   (`ghcr.io/jasontitus/streetzim:sha-<12 hex>`), with the tile profile
   inside it. CI publishes one only for each main tip it tests, so the step
   walks main's first-parent line from the newest commit that is the
   checkout's HEAD or below it (the host's own commits, such as torrents,
   reach main only as a merge's second parent) back to the first one with
   the checkout's tile inputs and a published image. Tile inputs changed
   in the checkout but not committed are refused. `STREETZIM_ML_COMMIT`
   names the commit instead, and must have the same inputs.
3. The build uses the tiles as `--mbtiles`, with `--tile-languages` from
   `.langs` and `--region-languages` from the table.
4. Any failure stops the build: no image with these tile inputs, no table
   row, an extract cut for another bbox, a box across the antimeridian
   (alaska; not supported yet), or tiles that fail the check (an MBTiles
   file, zooms 0-14, bounds = bbox, `name:xx` for the asked languages
   only). It never falls back to the world tiles.

The driver's marker check then requires `tileLanguages` and
`regionLanguages` in the ZIM.

**On the shared host:** one tilemaker at a time (a lock in
`tmp/.ml-tiles.lock`), `--memory 24g --memory-swap 24g --cpus 6`, our own
uid, nice 19 (`STREETZIM_ML_IO=idle` adds the idle I/O class, which is
much slower on a busy disk), the store in `tmp/ml-store-<id>-<pid>`
(deleted after), and the image tag removed again if the run pulled it.
tilemaker runs under `timeout` inside the container: an hour plus half an
hour per GB of extract (`STREETZIM_ML_TIMEOUT` overrides; measured about 2
minutes per GB on extracts up to 1.65 GB). A killed run's container, store
and part folder are removed by the next run, under the lock. A signal to
`build-region-fast.sh` reaches the step, which removes its container
before the wrapper ends.

**What differs from the world tiles** (Hispaniola, measured 2026-10-13):
the world tiles were made by tilemaker 3.2 (master `facdf93`, the same
code as tag v3.2.0), and so is the image now (`Dockerfile`
`TILEMAKER_REF`; it built v3.0.0 before). With 3.2, a region's tiles
match the world tiles' feature counts within 0.7% per layer at zoom
11-14, and the 21 languages add 0.1% of the tile bytes. Lower down, the
tiles reach past the box, where a region's extract has nothing: at zoom
8-10 landcover has 6-15% fewer features and water 12% fewer at zoom 8
(the same with the world tiles' own tilemaker build), and below zoom 8 the
neighbours are missing altogether. The viewer never shows beyond the box.

With 3.0 the content changed too: a polygon's label (`LayerAsCentroid`)
was copied into every tile under the polygon (the island name
"Hispaniola", with its 21 translations, sat in 32,000 zoom-14 tiles;
"Iceland" in 170,000; fixed upstream in #874), points were not merged into
multipoints (#725) or decluttered at low zoom (#926), and landuse thinned
out at zoom 8-12; tile bytes grew 13.7% on Hispaniola (7.7% of it the
languages) and 32% on Iceland.
