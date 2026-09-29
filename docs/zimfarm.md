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
| Vector tiles | built with tilemaker from the OSM extract | or `--mbtiles-url` for a ready-made OpenMapTiles MBTiles, e.g. OpenFreeMap's ([below](#building-from-ready-made-tiles---mbtiles-url)); `--mbtiles` for a local file (command line only) |
| Search over every named feature, Find chips, places list | on | always |
| Offline routing (drive / walk / bike) | on, spatial layout (SZCI v3) | `--no-routing` |
| Wikidata place details | off | `--wikidata` (queries Wikidata) |
| Terrain / hillshade | off | `--terrain` (downloads Copernicus DEM tiles) |
| POIs in Kiwix's own search | off: Kiwix's full-text search covers places, parks, peaks, water and airports | `--kiwix-poi-pages` |
| Satellite imagery | **off**; opt-in | `--satellite`: EOX Sentinel-2 cloudless 2016, **CC BY 4.0**. The 2021 mosaic, **CC BY-NC-SA 4.0 (non-commercial)**, only with `--satellite-source s2cloudless-2021 --satellite-accept-noncommercial`, as a variant labelled restricted ([below](#satellite-imagery)) |

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

### POIs in Kiwix's own search (`--kiwix-poi-pages`)

The map's own search (the search box, Find chips, places list) covers every
named feature. Kiwix's search, the one in the Kiwix app's bar and on
kiwix-serve, only sees the features that have a detail page
(`search/<slug>.html`): places, parks, peaks, water and airports. Without
the flag, "Casino" on the Monaco ZIM finds the Fontaine du Casino and not
the Casino, the shops or the bus stops named after it.
`--kiwix-poi-pages` gives every named POI a page too, which Kiwix's
full-text search and its title suggestions both find. It costs about 440 B
per POI: roughly 40 B of compressed page, 80 B of directory entry and
pointers, 125 B of full-text index and 195 B of title index. Measured on
2026-09-29 with the default profile:

| area | POI pages | ZIM | build time |
|---|---|---|---|
| Monaco (`--area monaco`) | +1,812 | 2.90 -> 3.70 MB (+28%) | within noise |
| Luxembourg | +21,214 | 56.7 -> 66.0 MB (+16%) | within noise |
| Switzerland (from its POI count) | +274,806 | about +121 MB on 624 MB (+19%) | |
| Netherlands (from its POI count) | +324,793 | about +143 MB on 1,164 MB (+12%) | |

Without the flag the pages that exist anyway (places, parks, peaks, water,
airports) are in the title index too; that costs Monaco 15 KB (+0.5%) and
Luxembourg 1.2 MB for 7,984 pages (+2.2%).

The search pages are front articles (that is what puts them in the title
index), so they count as the ZIM's articles: the "articles" number in the
Kiwix library and the ZIM's article count are the main page plus one per
search page. Monaco: 1 before this change, 18 without the flag, 1,830 with
it; Luxembourg: 1, 7,985 and 29,199. `M/Counter` (entries by MIME type) and
zimcheck's output do not change. Kiwix's "random article" can now open a
search page (a place's detail page with "Directions to here" and "View on
map").

It is off by default for now. The planned `--profile full` is meant to turn
it on, and so a full-profile ZIM carries the cost above (about +12% to +19%
for a country, +28% for Monaco); `--profile basic` leaves it off. That
branch will also make the option an on/off choice (an enum) in
offliner-definition.json rather than the boolean it is here.

## Satellite imagery

Off by default, and one flag to turn on. The imagery is EOX's Sentinel-2
cloudless mosaic ("EOxCloudless"), which EOX licenses **per year**:

| `--satellite-source` | EOX WMTS layer | licence | use | flag(s) |
|---|---|---|---|---|
| `s2cloudless-2016` (default) | `s2cloudless_3857` | **CC BY 4.0** | any, with attribution | `--satellite` |
| `s2cloudless-2021` | `s2cloudless-2021_3857` | **CC BY-NC-SA 4.0** | non-commercial only | `--satellite-source s2cloudless-2021 --satellite-accept-noncommercial` |

Why the 2021 mosaic stays off in openZIM's main distribution: NC-SA forbids
commercial use of the imagery and of anything adapted from it, and passes
that on to everyone downstream. openZIM's ZIMs are mirrored, bundled and
resold by others (device makers, library projects, app stores), so a ZIM with
NC imagery cannot go wherever the rest of openZIM's catalogue goes. That is a
reason to keep it out of the default and to label it clearly when it is in,
not a reason to make it unavailable: for non-commercial users it is the better
imagery. A freely licensed source (2016) has no such restriction and needs no
acknowledgement.

### The licences, from EOX's own pages

Checked on 2026-09-29 (copies of the pages were kept with the evidence for
this change):

- <https://cloudless.eox.at/license-non-commercial> (EOX's "License
  Non-Commercial" page):
  - "The conditions for use are the attribution when publishing any imagery
    or content from EOxCloudless WM(T)S layers as well as the non-commercial
    use for the 2018 - 2025 data."
  - "For the years 2018 to 2025, EOxCloudless WM(T)S layers is licensed under
    the Creative Commons Attribution-NonCommercial-ShareAlike 4.0
    International License."
  - "For the year 2016, EOxCloudless is licensed under the Creative Commons
    Attribution 4.0 International License."
  - Required attribution, 2016: "EOxCloudless https://cloudless.eox.at by EOX
    IT Services GmbH (Contains modified Copernicus Sentinel data 2016 &
    2017)"; 2021: "… (Contains modified Copernicus Sentinel data 2021)"; 2018:
    "… (Contains modified Copernicus Sentinel data 2017 & 2018)"; the other
    years name their own year.
  - "The attribution shall be displayed legibly and in proximity to the usage".
- <https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml>, the layer
  abstracts:
  - `s2cloudless_3857` ("Sentinel-2 cloudless layer for 2016 by EOX"):
    "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains
    modified Copernicus Sentinel data 2016) released under Creative Commons
    Attribution 4.0 International License."
  - `s2cloudless-2021_3857`: "… (Contains modified Copernicus Sentinel data
    2021) released under Creative Commons Attribution-NonCommercial-ShareAlike
    4.0 International License. For commercial usage please see
    https://cloudless.eox.at"; 2018 to 2025 read the same.
  - the service's AccessConstraints: "Proper attribution is required for any
    usage. … Additional restrictions may apply for individual layers as
    indicated in the respective abstract."
- <https://cloudless.eox.at/documentation/license> ("License Summary"):
  "Attribution must be clearly visible wherever the imagery is displayed. For
  interactive maps, the credit should appear in the map interface. In cases
  where direct display is not possible, the attribution should be included
  under credits, data sources, or as a part of metadata." It describes the
  non-commercial CC BY-NC-SA terms and EOX's commercial licence, and does not
  mention the 2016 layer; its sub-licensing limits are stated for those two.

`s2maps.eu` now redirects to <https://cloudless.eox.at/preview>.

Notes:
- The 2016 attribution differs between the two sources ("2016 & 2017" on the
  licence page, "2016" in the WMTS abstract). StreetZim uses the licence
  page's, which covers both.
- The WMTS also serves `s2cloudless-2017_3857`, whose abstract says CC BY 4.0,
  but the licence page lists no 2017 layer, and the layer has holes (no
  imagery around Singapore at z10). It is not offered.
- The 2016 layer is served today as `s2cloudless_3857` (and `s2cloudless`
  in EPSG:4326), the only yearly layer without a year in its id, in the same
  `GoogleMapsCompatible` grid as the 2021 layer; Monaco's tiles came back at
  every zoom to z15. How long EOX has kept that id could not be checked (the
  Web Archive was not reachable from the build machine), and EOX could
  rename or retire it: the build log would then warn about every tile it
  failed to download, and no other year's tiles are used in their place,
  since each source has its own cache.
- **Before openZIM publishes 2016-satellite ZIMs widely, get written
  confirmation from EOX (cloudless@eox.at)** that redistributing the 2016
  imagery inside ZIMs under CC BY 4.0 is fine. The licence page and the WMTS
  abstract both say CC BY 4.0 for 2016, but EOX's License Summary page does
  not carve 2016 out of its general terms (which limit sub-licensing and
  redistribution), so a short written answer removes the doubt.

### Which is the default, and the quality difference

The default is the most permissive source, 2016. It is usable but older and
softer: at the viewer's deepest zoom, buildings and streets that are distinct
in 2021 are blurred, colours are lighter with bluer water, and some tile
seams show (a straight edge across Monaco at z13-14, patchy sea off Iceland
at z6). Cloud cover was similar in the places compared (Monaco, Edinburgh,
Bergen, Singapore, northern Iceland): both are cloud-free composites, with
snow and glaciers where expected. The 2021 mosaic is sharper, darker and more
saturated.

### What a satellite ZIM carries

| | `--satellite` (2016, CC BY 4.0) | 2021, CC BY-NC-SA 4.0 (restricted) |
|---|---|---|
| Flavour | `satellite` | `satellite-nc` |
| Tags (added) | `satellite` | `satellite;non-commercial` |
| File name (default) | `{name}_satellite_{period}.zim` | `{name}_satellite-nc_{period}.zim` |
| LongDescription | unchanged | ends with "Restricted: the satellite imagery (…) is licensed CC BY-NC-SA 4.0 and may be used for non-commercial purposes only; the rest of this map is openly licensed." (after `--long-description`, or after the Description when there is none) |
| License | adds "Satellite imagery: CC BY 4.0, <licence URL> (<attribution>)" | opens with "Non-commercial use only: the satellite imagery is CC BY-NC-SA 4.0" and adds "Satellite imagery: CC BY-NC-SA 4.0, non-commercial use only, <licence URL> (<attribution>)" |
| viewer | Satellite button; while imagery shows, a short linked credit on the map ("© EOxCloudless 2016 by EOX · CC BY 4.0"); EOX's full attribution under Data Sources in About | the same, the map credit ending "(non-commercial)", plus a "Restricted: …" notice at the top of About |
| `map-config.json` | `satelliteSource`, `satelliteLicense`, `satelliteAttribution`, `satelliteNonCommercial: false` | the same, `satelliteNonCommercial: true` |

Kiwix identifies a book by Name and Flavour, so the variants of one area are
separate books under the same Name, and a recipe or a library filter can
pick the restricted ones out by Flavour `satellite-nc` or the tag
`non-commercial`. Without satellite imagery, Flavour stays `maxi` as before.
`--file-name` also takes `{flavour}`. `--satellite-max-zoom` caps the imagery
(default: `--max-zoom`, and z13 for areas centred 45° or more from the
equator, where Sentinel-2's 10 m pixels make z14 an upscale); like
`--satellite-source`, it turns the imagery on. `--satellite-accept-noncommercial`
on its own is refused. A `--long-description` too long to take the
restricted note is shortened (ending in "…") so the note always fits
openZIM's 4000 characters. The builder refuses a `--flavour` that
contradicts its imagery (for example `satellite` with the 2021 layer).

`tools/check_openzim_output.py --satellite SOURCE` checks all of this on a
built ZIM.

### Recipes

The flags as a Zimfarm recipe's offliner config (dash form, as in step 5 of
the local run below):

Basic (the default profile: no satellite imagery):

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with search and routing",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly"}
```

Full: every optional layer, with the freely licensed imagery (one flag for
the imagery; terrain as in the default profile table above):

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with satellite imagery",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly",
 "wikidata": true, "satellite": true}
```

Full, restricted: the 2021 imagery, labelled non-commercial (two flags for
the imagery; `satellite-source` implies `satellite`):

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with satellite imagery",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly",
 "wikidata": true, "satellite-source": "s2cloudless-2021",
 "satellite-accept-noncommercial": true}
```

Without `satellite-accept-noncommercial` that last recipe fails at once,
before any download, with a message naming the licence, so NC imagery cannot
end up in a ZIM by accident. The same on the command line:

```sh
streetzim --name osm_en_monaco --title Monaco --description "Offline map of Monaco" \
  --area monaco --output out --satellite                       # 2016, CC BY 4.0
streetzim --name osm_en_monaco --title Monaco --description "Offline map of Monaco" \
  --area monaco --output out --satellite-source s2cloudless-2021 \
  --satellite-accept-noncommercial                             # restricted variant
```

Satellite tiles are downloaded from EOX during the build: Monaco at z0-14
is 29 tiles and about 15 s. They are cached under `--dl`, per source.

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
default profile (tilemaker, search, chips, routing), OSM extracts from
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

### Downloads per task

A fresh Zimfarm container downloads, besides the OSM extract:
- the coastline and Natural Earth shapefiles when tiles are built with
  tilemaker (see below). Building from `--mbtiles-url` avoids this;
- nothing for the viewer: MapLibre GL JS is vendored and the Docker image
  carries the pinned font glyphs ([viewer-supply-chain.md](viewer-supply-chain.md));
- with `--terrain`, Copernicus DEM tiles for the area.

### Disk for a Zimfarm recipe

`--shapefiles` and `--dl` are not offliner flags, so on Zimfarm the
shapefiles go to the default `/tmp/streetzim/dl/shapefiles`, in the
container's writable layer, and every task downloads them again: the
water polygons zip is 864 MB (plus three small Natural Earth zips), about
1.2 GB unzipped, and about 2.1 GB at peak while the zip is being unpacked.
At about 25 MB/s that is 35 to 50 s per task. Zimfarm counts the image and
the writable layer toward the task's disk, so **give every recipe at least
4 GiB of disk, even for tiny areas**, and add the build's own peak disk
(the tables above) for larger ones.

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

## Building from ready-made tiles (`--mbtiles-url`)

`--mbtiles-url` (a Zimfarm `url` flag) builds from an OpenMapTiles MBTiles
instead of running tilemaker, as maps2zim builds from OpenFreeMap's. On the
command line, `--mbtiles` takes a local file instead.

**On Zimfarm today, only a downloaded MBTiles works, and in practice only a
regional one.** A Zimfarm worker starts the scraper with a single bind
mount, the task's own work folder at `/output`
(`worker/src/zimfarm_worker/common/docker.py`, `start_scraper`), created
empty for each task and counted in the task's disk. So:
- every task downloads its MBTiles again; nothing is shared between tasks,
  and the reuse, resume and pre-seeding below only help a retry inside the
  same container;
- a `file://` URL can only name a file inside the container: nothing kept
  on the worker is visible, so `file://` is for the build host and direct
  command-line runs, not for Zimfarm recipes;
- the planet (about 103 GB) would be downloaded per task and needs that
  much task disk, so a recipe needs a regional MBTiles hosted somewhere.
Sharing one planet between tasks would need an openZIM change: a
worker-side, read-only volume (say, a `ZIMFARM_SHARED_DATA` folder on the
worker mounted into scrapers at a fixed path) that `start_scraper` adds to
the scraper's mounts, a recipe-level way to ask for it, and a way to fill
and refresh it on each worker; or a cache that the worker manager keeps
across tasks. Neither exists at `917d7bc`.

How the flag behaves:
- **http(s)://** URLs are downloaded into `<dl>/mbtiles/` with
  zimscraperlib's retrying session where it is installed (urllib
  otherwise), logging progress every 5% (`streetzim/download.py`).
  - An interrupted download (`.part`) is resumed only when the server
    takes ranges, the file has an ETag or Last-Modified, and it is
    unchanged upstream (same ETag, Last-Modified and size as when it
    started). The request carries `If-Range`; the answer must be a 206
    starting at the offset. A 200 is the whole file again and is written
    from the start, without a second request. A `.part` already complete
    is renamed without downloading.
  - A finished download is reused while unchanged upstream. A file
    already in place with the upstream size and no record of its version
    (a pre-seeded download folder) is used; for OpenFreeMap only if it
    also has the published SHA-256. Offline, what is there is used.
  - OpenFreeMap downloads (`https://*.openfreemap.com/areas/<area>/
    <version>/tiles.mbtiles`) are checked against that version's
    `SHA256SUMS`; a mismatch is an error. Once a new version is in place,
    the other versions of that area in `<dl>/mbtiles/` are deleted (and
    logged), since each is as large as the new one; one in use by another
    task is kept.
  - When HEAD is refused, a one-byte ranged GET gives the headers
    instead. Two tasks sharing a download folder take turns on
    `<file>.lock`.
- **file://** URLs are used in place, never copied (see above for where
  that works). Zimfarm's `url` type accepts `file://` URLs, not bare paths.
- The file is refused early if it is not an MBTiles: a download stops at
  its first bytes unless they are an SQLite header, and the file must have
  `tiles` and `metadata` tables (a `tiles` view, as OpenFreeMap's, counts).
- Its metadata goes into the build log (`MBTiles: OpenFreeMap, version
  3.16.0, planetiler 0.10.3-SNAPSHOT, OSM data 2026-09-27, bounds …`) and
  into the ZIM: `map-config.json` gets `tileSource` (name, version, OSM
  date, generator, homepage and the source: an http(s) URL without user
  name, password or query, or for `file://` only the file name), the
  viewer's About dialog shows it under "Vector Tiles", and `License`
  credits the tiles ("Vector tiles: OpenFreeMap (https://openfreemap.org)",
  the name cleaned of control characters and cut at 80 characters).
- **Only the area's tiles go into the ZIM.** The file is always cut to the
  tiles that touch the area's box, z0 to z14 (its `bounds` metadata is not
  trusted): maps2zim's `TileFilter` rule, edges included; a box across the
  antimeridian keeps both sides. `--max-zoom` then caps the tiles stored,
  as for tilemaker builds; the cut keeps z14, which search reads. The cut
  goes to `<tmp>/mbtiles-cut/`, which is cleared at start and removed at
  the end, also when the build fails or is stopped with SIGTERM.
  - It is one index search per tile column on the tiles'
    `(zoom_level, tile_column, tile_row)` primary key (`SEARCH
    tiles_shallow USING PRIMARY KEY (zoom_level=? AND tile_column=? AND
    tile_row>? AND tile_row<?)`), then each distinct tile's data by its id
    (`SEARCH tiles_data USING INTEGER PRIMARY KEY`). It never counts or
    scans the source, so its cost follows the area: Switzerland is 426
    seeks and 37,326 tiles, Germany 849 seeks and 318,186 tiles, whatever
    the size of the file.
  - OpenFreeMap's layout is kept: each distinct tile is stored once (its
    ocean and land tiles repeat). Other files get a plain tiles table.
  - Measured on a synthetic planet-shaped file in OpenFreeMap's layout
    (26.3 million tiles, 1.7 GB: every tile to z12 drawn from 1,000
    shared blobs, z13-14 unique over Europe), after dropping the page
    cache: Switzerland 0.19 s (12.0 MB, 13% smaller than without the
    deduplication), Germany 0.87 s (100 MB, 15% smaller), Fiji, which is
    all shared ocean-like tiles, 0.04 s (0.7 MB instead of 8.5 MB); peak
    memory 18 MB. The whole of `monaco.mbtiles` cut this way is 9%
    smaller than a plain copy. On the real planet the copy dominates: it
    reads the area's own tile data (a few GB for Germany), at disk speed.

`--mbtiles` on the command line (a local file) now goes through the same
check, cut and record as `--mbtiles-url`; before, it passed every tile of
the file to the ZIM and recorded nothing. A missing file is an error before
anything is downloaded. `create_osm_zim.py --mbtiles`, which production
uses, is unchanged: it records the source only with `--record-tile-source`.

The trade-off:
- **No tilemaker and no shapefiles.** The task skips the 864 MB shapefile
  download and the tilemaker run, so the 4 GiB disk floor above does not
  apply; disk is the MBTiles, the cut and the ZIM.
- **But OpenFreeMap publishes only `planet` (about 103 GB, some 276
  million tiles) and `monaco`** (`scripts/fetch-openfreemap-mbtiles.py`
  finds the newest). On Zimfarm, a real recipe therefore needs a regional
  MBTiles hosted somewhere (cut from the planet with this same code:
  `streetzim.mbtiles.cut`). The build host can use the planet in place
  with `file://`.
- **The tiles are OpenFreeMap's, not ours**: a different feature mix, with
  fewer named places and streets to search (on Monaco our tilemaker tiles
  give 31% more street names and about twice as many named places; see
  [tile-sources.md](tile-sources.md)).
  Search, addresses and routing still come from the OSM extract when there
  is one (`--pbf-url`, or the Geofabrik extract of the area); without one,
  give `--no-routing`.

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
