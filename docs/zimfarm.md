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
The image installs only the runtime dependencies (`requirements.txt`): no
test tools and no upload client.

## The default profile

| | default | how |
|---|---|---|
| Vector tiles | built with tilemaker from the OSM extract | or `--mbtiles` for ready-made OpenMapTiles (e.g. OpenFreeMap) |
| Search over every named feature, Find chips, places list | on | always |
| Offline routing (drive / walk / bike) | on, spatial layout (SZCI v3) | `--no-routing` |
| Wikidata place details | off | `--wikidata` (queries Wikidata) |
| Terrain (hillshade and 3D) | on, from the lowest zoom the viewer can show to z12 | `--no-terrain`; downloads Copernicus DEM tiles ([cost](#terrain-cost)) |
| Satellite imagery | **never** | not offered: the imagery is CC BY-NC-SA |

The area is exactly one of `--area` (a preset), `--include-poly` (a `.poly`
URL; a Geofabrik one also selects its extract) or `--bbox`. Areas are
bounding boxes: the extract and the tiles are cut to the box, not the
polygon. So:
- an area across the antimeridian (Fiji, Chukotka, Kiribati) is supported.
  A polygon whose parts sit either side of ±180° (rings split there, as
  Geofabrik's are, or one ring drawn across it) becomes one box across the
  antimeridian, never a band around the world. With `--bbox`, write such a
  box with minlon > maxlon (Fiji: `172.8,-23.2,-176.5,-11.2`); a box that
  would then be wider than 180° is refused as swapped coordinates. The
  extract, the tiles (tilemaker runs once per side) and the map bounds
  cover both sides, and search and routing work across it
  ([formats](formats.md#areas-across-the-antimeridian)). The production
  queue takes such regions too (`cloud/regions.tsv` alaska; see
  `ops/docs/new-region-setup.md`);
- a polygon whose parts are far apart is built from the part with the most
  land, and the log names the parts left out. Parts are kept together while
  they are less than 1° apart or their shared box is at most 3 times their
  own boxes: Spain keeps the Balearics and the Canaries, but the
  Netherlands leaves out its Caribbean islands and Portugal the Azores and
  Madeira. Build those separately with `--bbox`;
- a single ring that spans open sea (Norway's includes Svalbard and
  Jan Mayen) gives a large, mostly empty box; prefer a smaller polygon or
  `--bbox`.

`License` metadata and the viewer's credits list only the sources a ZIM
actually contains.

## What Zimfarm needs on its side

Checked against openzim/zimfarm at `917d7bc`:

1. **Docker image name.** Zimfarm pulls `ghcr.io/<name>` and only accepts
   names in `DockerImageName` (`backend/src/zimfarm_backend/common/enums.py`).
   StreetZim's image would have to be published under a name added there
   (for example `openzim/streetzim` if the repository moved to openZIM).
2. **Worker offliner list.** A worker advertises only the offliners in
   `ALL_OFFLINERS` (`worker/src/zimfarm_worker/common/constants.py`;
   `SUPPORTED_OFFLINERS` filters on it), and Zimfarm will not request a
   task for a worker that does not advertise the recipe's offliner
   ("Worker '…' offliners do not match the offliner for recipe '…'").
   `streetzim` has to be added there. Existing workers pick it up only when
   they update their task-worker image; workers that set
   `ZIMFARM_OFFLINERS` explicitly also have to add it to that list.
3. **Progress bar.** The worker reads `task_progress.json` only for
   offliners listed in `PROGRESS_CAPABLE_OFFLINERS` (same file);
   `stdStats: true` has no effect until `streetzim` is added there.
4. **Definition upload.** maps2zim publishes its definition with
   `.github/workflows/update-zim-offliner-definition.yaml`, which calls
   `openzim/overview`'s reusable workflow with a `ZIMFARM_CI_SECRET`. The
   same workflow works here once openZIM provides the secret. The offliner
   is registered with `base_model: DashModel`: Zimfarm passes flags as
   `--flag-name=value` (its `compute_flags`), which also keeps a value
   starting with `-`, such as a western `--bbox`, from being read as a flag.

`offliner-definition.json` validates against Zimfarm's own
`OfflinerSpecSchema` (`tools/check_zimfarm_schema.py`, run in CI against a
pinned Zimfarm commit), and the command line Zimfarm generates from it is
accepted by `streetzim` (`tests/test_offliner_definition.py`).

The patch this amounts to on openZIM's side, as used in the local run below:
- `backend/src/zimfarm_backend/common/enums.py`: `streetzim =
  "openzim/streetzim"` in `DockerImageName`, and `cls.streetzim` in its
  `all()` set;
- `worker/src/zimfarm_worker/common/constants.py`: `OFFLINER_STREETZIM =
  "streetzim"`, added to both `ALL_OFFLINERS` and
  `PROGRESS_CAPABLE_OFFLINERS`;
- optional: `streetzim` appended to `ZIMFARM_OFFLINERS` in
  `worker/contrib/zimfarm.config.example`, and a `streetzim` entry
  (`DashModel`, image `openzim/streetzim`, command `streetzim`) in the
  offliner config map of `dev/contrib/create-offliners.sh`. That script
  fetches each definition from `openzim/<id>` on GitHub, so `streetzim` can
  go in its fetch list only once the repository is there.

Zimfarm pulls the image from `ghcr.io/openzim/streetzim:<tag>`: the
`ghcr.io` prefix is the default and `openzim/` comes from
`DockerImageName`, so the image (or at least the package) has to be
published under the openzim organisation, or openZIM has to add a
different name to the enum.

## What a build costs

Measured with `tools/measure_build.py` on a 4-core, 15 GB machine,
default profile as it was before terrain became the default (tilemaker,
search, chips, routing; add [Terrain cost](#terrain-cost)), OSM extracts from
openstreetmap.fr on 2026-09-28 (the Netherlands: 2026-09-29), Python
3.11 outside Docker. Memory is PSS
summed over every process of the build (RSS, which counts shared pages
once per worker, in brackets); disk is the temp and output folders (the
downloaded extract is not counted).

| region | OSM extract | wall time | CPU time | peak memory | peak disk | ZIM |
|---|---|---|---|---|---|---|
| Luxembourg | 56 MB | 3.2 min | 6.7 min | 4.0 GB (4.0) | 0.35 GB | 55 MB |
| Switzerland | 679 MB | 70 min | 195 min | 4.6 GB (6.3) | 4.4 GB | 654 MB |
| Netherlands | 1.63 GB | 92 min | 162 min | 10.8 GB (11.2) | 11.0 GB | 1.22 GB |

The Switzerland run shared the machine with low-priority test builds, so
its wall time is an upper bound. 49 of its 70 minutes are the search step
(feature extraction from the tiles, location labels for every named
feature, and addresses from the extract); the next largest are writing
the ZIM (11 min) and the routing graph (5 min).

The Netherlands had the machine to itself. Its time splits between the
search step (40 min) and writing the ZIM (34 min); tiles take 8 min and
routing 7. Writing the ZIM is also where memory and disk peak (10.8 GB
PSS, 11.0 GB): the search step peaks at 5.4 GB PSS (11.2 GB RSS, shared
pages counted per worker) and 8.1 GB of disk. Two earlier runs with less
free disk failed with ENOSPC while writing the ZIM's search index, so for
a region this size
give the task at least 12 GB of RAM and 15 GB of disk, besides the extract
and shapefiles.

A US region and a Docker comparison, measured the same way on 2026-09-29
with the code as of that day and the inputs given as `file://` URLs (so no
download time): the US states from Geofabrik (2026-09-28 extracts),
Luxembourg the same openstreetmap.fr extract as above. The machine was shared
with other jobs this time, so wall times are upper bounds: the mean 1-minute
load over the run was 15.4 for Rhode Island and 14.2 for Massachusetts (on 4
cores), and was not recorded for the Luxembourg runs. CPU time varies too:
Luxembourg took 6.7 CPU minutes in the run above (older code) and 5.1 and
4.7 in the two here.

| region | where | OSM extract | wall time | CPU time | peak memory | peak disk | ZIM |
|---|---|---|---|---|---|---|---|
| Luxembourg | Docker image (Python 3.14.7) | 56 MB | 3.2 min | 4.7 min | 4.1 GB (4.1) | 0.34 GB | 55 MB |
| Luxembourg | outside Docker (Python 3.11.15) | 56 MB | 4.3 min | 5.1 min | 4.0 GB (4.0) | 0.36 GB | 55 MB |
| Rhode Island | outside Docker (Python 3.11.15) | 52 MB | 4.2 min | 4.7 min | 4.0 GB (4.0) | 0.28 GB | 52 MB |

Inside Docker, `tools/measure_build.py` ran in the container, around the
`streetzim` process, as the image runs it. The two Luxembourg runs agree on
memory, disk and the ZIM. Of the 1.1 min wall-time gap, 31 s is the font
glyphs, which the image carries and a fresh `--dl` outside it downloads,
and most of the rest is tile generation (47 s against 14 s, the same
tilemaker v3.0.0 release build), which these two runs alone cannot
attribute to the container rather than the shared machine. Rhode Island, a
US state of Luxembourg's size, costs the same: about 4 GB of memory for
both small regions measured.

Massachusetts (a 310 MB extract, between Luxembourg and Switzerland) was
stopped by the measuring script 15 minutes in, during the ZIM step, when
the machine's free disk (shared with the other jobs) reached 2 GB; its
temporary and output folders held 2.1 GB at that point. Tiles had taken
2.3 min, the search step 7.8 min and the routing graph 2.7 min. No peak
memory was recorded, as the measuring script writes it at the end.

### Terrain cost

Terrain is on by default: hillshade and 3D are part of the StreetZim
experience, and the Copernicus DEM is free and open (attribution only;
`License` and the viewer's credits name it whenever a ZIM has terrain).
`--no-terrain` leaves it out. The numbers below are what it costs.

**What a build fetches.** A terrain tile covers its whole square, and at
low zoom that square is far larger than a small region (Luxembourg's z7
tile spans 2.8 degrees). The builder used to fetch GLO-30 for the bbox
plus one degree and leave the rest of each low-zoom tile at 0 m: a cliff
where the DEM stopped, visible in a tilted 3D view, and a health check that
passed only because it read the same short DEM. On a fresh machine it also
fetched far more GLO-30 than the region needed. Without a world DEM
(`--low-zoom-world-vrt`, which only the production host has, so on every
`streetzim` build and every Zimfarm task) it now works like this
(`streetzim/terrain.py`):
- tiles start at the lowest zoom the viewer can show. The viewer keeps the
  view inside the area, so on a 320-px screen Monaco never shows below map
  zoom 12.5 and Luxembourg below 8.1; terrain starts one zoom below the 3D
  view's (z11 and z7). map-config.json says so (`terrainMinZoom`) and the
  viewer asks for nothing lower;
- every tile is filled over its whole square: z10 and above from GLO-30
  (30 m), z9 and below from a 90 m mosaic of GLO-30 and GLO-90 cells. At
  z9 a 256-px tile has 10-arc-second pixels, so GLO-90 loses nothing, and a
  GLO-90 cell is about an eighth of the size of a GLO-30 one;
- GLO-30 is fetched only for the cells under the area's z10 tiles;
- for a very large area the GLO-90 ring is capped (64 cells, or three times
  the GLO-30 cells); the lowest zooms are then filled over the part the cap
  covers, and are 0 m beyond it, far outside the area;
- the health check compares every tile at z9 and below, and every tile on
  the edge of the area, with the mosaic it was made from (25 points each):
  a tile reading 0 m where that mosaic has land fails the build, whatever
  its file size, and so does a missing tile the repair pass could not
  make. Sea reads 0 m in both and passes; no `TERRAIN_BLANK_TOLERATE` is
  needed. Builds given a world DEM (production) keep their layout and check.

**Measured** on 2026-09-29 with `tools/measure_build.py`, through the
`streetzim` command, each run with an empty `--dl` so the DEM was
downloaded; extracts as `file://` URLs (Monaco from openstreetmap.fr,
Luxembourg the same extract as above), 4-core 15 GB machine shared with
other jobs (1-minute load 13 to 50, median 29), so wall times are upper
bounds and CPU time is the steadier figure. Peak disk counts `--dl`'s cache
(where the DEM goes) as well as the temp and output folders.

| region | terrain | wall time | CPU time | terrain phase | peak memory | peak disk | DEM downloaded | ZIM |
|---|---|---|---|---|---|---|---|---|
| Monaco | off | 58 s | 52 s | | 3.23 GB | 0.01 GB | | 2,906,595 B |
| Monaco | on | 54 s | 59 s | 1.9 s | 3.19 GB | 0.02 GB | 12.6 MB (1 GLO-30 cell) | 2,934,867 B (+28 kB: 2 tiles, z11-z12) |
| Luxembourg | off | 4.8 min | 5.4 min | | 3.99 GB | 0.36 GB | | 55.6 MB |
| Luxembourg | on | 6.3 min | 5.6 min | 33 s | 3.99 GB | 0.56 GB | 189 MB (4 GLO-30 + 8 GLO-90) | 58.3 MB (+2.75 MB, +5%: 207 tiles, z7-z12, 2.78 MB of entries) |

The terrain phase alone, with the DEM already on disk, took 12 s for
Luxembourg (11 s to make the tiles, 1 s for the health check), 0.2 CPU
minutes and 0.6 GB of memory: the other 21 s of the phase were the
download (189 MB at about 9 MB/s through this machine's proxy). The build's
peak memory is unchanged, as terrain is not the step where it peaks. Before
this change the same two areas fetched 178 MB (Monaco, bbox + 1 degree: 9
cells) and 608 MB (Luxembourg, 16).

**Switzerland and the Netherlands**, extrapolated, not built:
- DEM bytes are exact: the cells each area's plan needs, sized with an HTTP
  HEAD on the S3 objects (a 404 is sea);
- tile counts are exact (the plan's zooms over the area);
- ZIM bytes and CPU time per tile come from making every z10-z12 tile of
  sample GLO-30 cells: Alps (N46E007) and Plateau (N47E008) for
  Switzerland, inland (N52E005) and coast (N53E006) for the Netherlands.
  Luxembourg's own cell (N49E006) gave 10.8 kB and 36 ms per z12 tile,
  against 13.4 kB per tile over all zooms in its ZIM;
- download time at 25 MB/s, the rate [Disk for a Zimfarm recipe](#disk-for-a-zimfarm-recipe)
  assumes for the shapefiles.

| region | zooms | DEM to download | tiles | ZIM bytes | CPU | download | vs. the build (tables above) |
|---|---|---|---|---|---|---|---|
| Switzerland | z5-z12 | 815 MB (18 GLO-30 + 10 GLO-90 cells) | 2,534 | about 45 MB | about 2 min | about 35 s | ZIM +7%, disk +0.9 GB, time +1-2% |
| Netherlands | z4-z12 | 598 MB (20 GLO-30 + 20 GLO-90 land cells) | 3,895 | about 7 MB (flat; sea tiles are aliased) | about 1.5 min | about 25 s | ZIM +0.6%, disk +0.6 GB, time +1% |

For the Netherlands the GLO-90 ring is capped at the z7 squares (49 cells),
so its z4-z6 tiles are filled over those squares and are 0 m beyond them:
far outside the country, in the part of a tilted view nearest the horizon.
Before this change Switzerland would have fetched 1.71 GB and the
Netherlands 0.95 GB.

So terrain costs a Zimfarm task a DEM download about the size of the OSM
extract (1.2 times it for Switzerland, 0.4 times for the Netherlands, 3.4
times for Luxembourg, whose extract is small), under a minute of CPU for
countries of this size, and a few percent of the ZIM at most (hilly
areas); peak memory does not change. Add the DEM to the recipe's disk.

A recipe that leaves terrain out ticks **No terrain**, which Zimfarm passes
as `--no-terrain`:

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with search and routing",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly",
 "no-terrain": true}
```

### Downloads per task

A fresh Zimfarm container downloads, besides the OSM extract:
- the coastline and Natural Earth shapefiles when tiles are built with
  tilemaker (see below). Building from `--mbtiles` avoids this;
- nothing for the viewer: MapLibre GL JS is vendored and the Docker image
  carries the pinned font glyphs ([viewer-supply-chain.md](viewer-supply-chain.md));
- Copernicus DEM tiles for the area, unless `--no-terrain`: from the
  public `copernicus-dem-30m` and `copernicus-dem-90m` buckets on AWS S3,
  over HTTPS with no credentials, into `--dl` (on Zimfarm the default
  `/tmp/streetzim/dl`, never the output folder). 12.6 MB for Monaco,
  189 MB for Luxembourg; see [Terrain cost](#terrain-cost).

### Disk for a Zimfarm recipe

`--shapefiles` and `--dl` are not offliner flags, so on Zimfarm the
shapefiles go to the default `/tmp/streetzim/dl/shapefiles`, in the
container's writable layer, and every task downloads them again: the
water polygons zip is 864 MB (plus three small Natural Earth zips), about
1.2 GB unzipped, and about 2.1 GB at peak while the zip is being unpacked.
At about 25 MB/s that is 35 to 50 s per task. Zimfarm counts the image and
the writable layer toward the task's disk, so **give every recipe at least
4 GiB of disk, even for tiny areas**, and add the build's own peak disk
(the tables above) and the DEM ([Terrain cost](#terrain-cost)) for larger
ones.

Monaco on the local Zimfarm below (default profile, recipe resources cpu 2,
memory 6 GiB, disk 4 GiB), as Zimfarm reported it: memory max 3.49 GiB
(Docker's usage figure, which includes page cache), disk max 3.51 GiB
(image 1.54 GB, shapefiles, a 2.8 MB ZIM), and 72 s of scraper time, about
50 s of it the shapefile download.

Ways to cut this, none taken yet:
- **Bake the shapefiles into the image.** No download per task, and the
  layer is shared by every task on a worker, but the image grows by about
  1.2 GB for every pull, including `--mbtiles` builds that do not need
  them.
- **Unzip while downloading.** Piping the download into a streaming
  extractor (`bsdtar -xf -`, from libarchive-tools, which the image does
  not have) would drop the 864 MB zip from the peak (about 1.2 GB instead
  of 2.1 GB), but curl's `--retry` cannot resume into a pipe, so a dropped
  connection would need its own retry around the whole pipe. Deleting the
  zip right after unzipping it does not lower the peak, which is reached
  while both exist; `fetch-shapefiles.sh` already removes it when it
  finishes.

## Tested on a local Zimfarm

On 2026-09-29 a Monaco recipe ran end to end on a local Zimfarm at
openzim/zimfarm `917d7bc` with the patch above: the worker pulled the image,
ran `streetzim`, Zimfarm showed its progress (7/7), and the uploaded ZIM
passed Zimfarm's zimcheck, `tools/check_openzim_output.py --routing` and
`cloud/validate_zim.py`. To reproduce:

1. Build the image and push it to a local registry
   (`docker run -d -p 127.0.0.1:5000:5000 registry:2`, then
   `docker build -t localhost:5000/openzim/streetzim:dev . && docker push …`).
2. Apply the patch above to a zimfarm checkout and start its dev compose
   stack (`dev/docker-compose.yml`: postgres and backend, then the
   `worker` profile's receiver, worker manager and task worker). On the backend, set
   `DOCKER_REGISTRY_openzim/streetzim=localhost:5000` (the key contains the
   image name's slash): the backend builds the pull reference as
   `getenv("DOCKER_REGISTRY_<image name>", "ghcr.io")/<image name>:<tag>`,
   so no code change is needed for a local registry. With that variable
   set, `PATCH /recipes/<name>` with an `image` fails with "Image name must
   match selected offliner", as the check reads two different keys; create
   recipes with the image instead. The upstream `minio/minio` image the
   dev stack uses for logs is gone from Docker Hub; uploading logs and
   zimcheck results to the receiver over SFTP works instead.
3. Register the offliner as `dev/contrib/create-offliners.sh` does for
   the others, but with the definition from this repository:
   `POST /v2/offliners` with `{"offliner_id": "streetzim", "base_model":
   "DashModel", "docker_image_name": "openzim/streetzim", "command_name":
   "streetzim", "ci_secret_hash": …}`, then
   `POST /v2/offliners/streetzim/versions` with `{"version": "dev",
   "ci_secret": …, "spec": <offliner-definition.json>}`.
4. Create the worker with `POST /v2/workers {"name": "test-worker",
   "ssh_key": {"key": "<public key>"}}`. `dev/contrib/create_worker.sh` is stale at
   this commit (it posts to `/v2/users`, and worker accounts can no longer
   be created through `/v2/accounts`), and worker names must match
   `^[a-z0-9-]+$`.
5. `POST /v2/recipes` with warehouse path `/maps`, platform `maps`, image
   `openzim/streetzim:dev`, resources as above and offliner flags in their
   dash form (`"offliner_id": "streetzim", "name": …, "title": …,
   "description": …, "illustration-url": …, "area": "monaco"`), then
   `POST /v2/requested-tasks {"recipe_names": [...], "worker":
   "test-worker"}` once the worker manager has checked in.
