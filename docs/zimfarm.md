# Running StreetZim on Zimfarm

`streetzim` (`streetzim/cli.py`) is the builder's openZIM-style command. Its
flags follow maps2zim's, and `offliner-definition.json` describes them for
Zimfarm. This page covers what it builds by default, what Zimfarm needs on
its side before it can run it, and what a build costs.

```sh
docker run --rm -v "$PWD/out:/output" streetzim \
  streetzim --name osm_en_luxembourg --title Luxembourg \
    --description "Offline map of Luxembourg with search and routing" \
    --include-poly https://download.geofabrik.de/europe/luxembourg.poly \
    --output /output --stats-filename /output/task_progress.json
```

The image is Python 3.14 on Debian trixie, like openZIM's scrapers, so
zimscraperlib is installed and `streetzim` uses it for the metadata rules,
the illustration (PNG, JPEG, WebP or SVG, cropped to 48x48), downloads and
the output-folder check. The ZIM itself is written with python-libzim.

## The default profile

| | default | how |
|---|---|---|
| Vector tiles | built with tilemaker from the OSM extract | or `--mbtiles` for ready-made OpenMapTiles (e.g. OpenFreeMap) |
| Search over every named feature, Find chips, places list | on | always |
| Offline routing (drive / walk / bike) | on, spatial layout (SZCI v3) | `--no-routing` |
| Wikidata place details | off | `--wikidata` (queries Wikidata) |
| Terrain / hillshade | off | `--terrain` (downloads Copernicus DEM tiles) |
| Satellite imagery | **never** | not offered: the imagery is CC BY-NC-SA |

The area is exactly one of `--area` (a preset), `--include-poly` (a `.poly`
URL; a Geofabrik one also selects its extract) or `--bbox`. Areas are
bounding boxes: the extract and the tiles are cut to the box, not the
polygon, and an area that reaches the antimeridian (Russia, Fiji) is refused
rather than turned into a band around the world.

`License` metadata and the viewer's credits list only the sources a ZIM
actually contains.

## What Zimfarm needs on its side

Checked against openzim/zimfarm at `917d7bc`:

1. **Docker image name.** Zimfarm pulls `ghcr.io/<name>` and only accepts
   names in `DockerImageName` (`backend/src/zimfarm_backend/common/enums.py`).
   StreetZim's image would have to be published under a name added there
   (for example `openzim/streetzim` if the repository moved to openZIM).
2. **Progress bar.** The worker reads `task_progress.json` only for
   offliners listed in `PROGRESS_CAPABLE_OFFLINERS`
   (`worker/.../common/constants.py`); `stdStats: true` has no effect until
   `streetzim` is added there.
3. **Definition upload.** maps2zim publishes its definition with
   `.github/workflows/update-zim-offliner-definition.yaml`, which calls
   `openzim/overview`'s reusable workflow with a `ZIMFARM_CI_SECRET`. The
   same workflow works here once openZIM provides the secret. The offliner
   is registered with `base_model: DashModel` (flags are passed as
   `--flag-name value`).

`offliner-definition.json` itself validates against Zimfarm's
`OfflinerSpecSchema`, and the command line Zimfarm generates from it is
accepted by `streetzim` (`tests/test_offliner_definition.py` checks the
second part in CI).

## What a build costs

Measured with `tools/measure_build.py` on a 4-core, 15 GB machine,
default profile (tilemaker, search, chips, routing), OSM extracts from
2026-09-28. Memory is PSS summed over every process of the build; disk is
the temp and output folders (the downloaded extract is not counted).

| region | OSM extract | wall time | CPU time | peak memory | peak disk | ZIM |
|---|---|---|---|---|---|---|
| Luxembourg | 56 MB | 3.2 min | 6.7 min | 4.0 GB | 0.35 GB | 55 MB |

Switzerland and the Netherlands are being measured; this table will be
completed.

### Downloads per task

A fresh Zimfarm container downloads, besides the OSM extract:
- the coastline and Natural Earth shapefiles (about 900 MB, unzipped
  about 1.2 GB) when tiles are built with tilemaker. Baking them into the
  image, or building from `--mbtiles`, avoids this;
- MapLibre GL JS and the font glyphs (a few MB);
- with `--terrain`, Copernicus DEM tiles for the area.
