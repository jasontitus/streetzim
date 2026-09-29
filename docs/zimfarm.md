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

The default is `--profile full` ([Profiles](#profiles)); `--profile basic`
turns off everything below that says "full".

| | default | how |
|---|---|---|
| Vector tiles | built with tilemaker from the OSM extract | or `--mbtiles` for ready-made OpenMapTiles (e.g. OpenFreeMap) |
| Search over every named feature, Find chips, places list | on | always |
| Offline routing (drive / walk / bike) | on, spatial layout (SZCI v3) | `--no-routing` |
| Overture Maps addresses and place details | on with full | `--overture` / `--no-overture` (reads Overture's public bucket) |
| Wikipedia articles (text) | on with full | `--wikipedia` / `--no-wikipedia` (Wikipedia API; images with `--wikipedia-zim-url`) |
| Wikidata place details | on with full | `--wikidata` / `--no-wikidata` (queries Wikidata) |
| Terrain / hillshade | off | `--terrain` (downloads Copernicus DEM tiles) |
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

## Profiles

`--profile` picks the content; any feature flag given explicitly wins over
it, in either direction (`--profile basic --wikidata`, or
`--no-wikipedia` on the default `full`). Routing is on in both (`--no-routing`
turns it off).

| feature | `full` (default) | `basic` |
|---|---|---|
| Wikidata place details | on | off |
| Wikipedia articles (text from the API; images only with `--wikipedia-zim-url`) | on | off |
| Overture Maps addresses and places | on | off |
| Terrain / hillshade | on | off |
| Satellite imagery | never: opt-in only, and labelled | never |
| Search, Find chips, places page, routing, fonts, dark style, icons | on | on |

`full` is everything StreetZim's own builds ship that openZIM can ship too;
`basic` fetches nothing besides the OSM extract and the shapefiles, and is
the cheapest (the "What a build costs" tables below are `basic` builds).
Satellite is in neither: its imagery is CC BY-NC-SA, so it is only ever
turned on explicitly.

Terrain is on in `full` only once `--terrain` can also be turned off
(`--no-terrain`, from the terrain branch). A profile sets only features the
command line can switch both ways (`streetzim/cli.py`, `PROFILES` and
`switchable`), so until then `--terrain` keeps its old default (off) under
either profile, and a recipe adds it explicitly.

On Zimfarm, `profile` is a string-enum flag (`full`, `basic`, default
`full`). Zimfarm passes a boolean only when it is ticked, so every feature a
profile sets is offered both ways (`wikidata` and `no_wikidata`,
`wikipedia` and `no_wikipedia`, `overture` and `no_overture`, and
`terrain`/`no_terrain` once terrain has both); ticking both is refused
before anything is downloaded. `tests/test_offliner_definition.py` runs
recipes through Zimfarm's own models and `compute_flags` and checks the
features `streetzim` then builds.

Recipe flags (the `offliner` part of `POST /v2/recipes`, dash form):

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with search and routing",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly",
 "profile": "full"}
```

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg-basic", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with search and routing",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly",
 "profile": "basic"}
```

and the same on the command line:

```sh
streetzim --name osm_en_luxembourg --title Luxembourg \
  --description "Offline map of Luxembourg with search and routing" \
  --include-poly https://download.geofabrik.de/europe/luxembourg.poly \
  --profile full --output /output          # or --profile basic
streetzim ... --profile basic --wikidata    # basic plus Wikidata
streetzim ... --no-overture                 # full without Overture
```

### Wikipedia articles on Zimfarm: the API, not a Wikipedia ZIM

StreetZim's own builds read articles and images from a local copy of the
English Wikipedia maxi ZIM (`--wiki-articles-source`, 124 GB, kept on the
build host). A Zimfarm task has no persistent storage, so it would have to
download one per task. The candidates on download.kiwix.org (2026-09):

| Kiwix ZIM | size | articles | images |
|---|---|---|---|
| `wikipedia_en_all_maxi_2026-08` | 119 GB | all | yes |
| `wikipedia_en_all_nopic_2026-06` | 49 GB | all | no |
| `wikipedia_en_all_mini_2026-09` | 13 GB | all, lead section only | no |
| `wikipedia_en_top_maxi_2026-09` | 6.5 GB | the most read ones only | yes |

The full ZIMs cost 50 to 120 GB of disk and download per task (about 35 to
80 minutes at 25 MB/s) for what is, for a region, a few hundred to ten
thousand articles (California: 11,613); the `top` ZIMs miss most of the
long-tail places a map links to. So **the default is the API**
(`action=parse`, one request per article, cached in `--dl` for the task),
which gives the article text but no images. `--wikipedia-zim-url` takes
any of the ZIMs above (or a `file://` URL on a worker that has one) and
then also bundles images (`--wikipedia-images`, default `all`, as
production), for workers with the disk to spare.

The API is rate limited. Until `topic-wiki-429` is merged (a shared
`cloud/wikimedia_http.py` that honours `Retry-After` and never caches a 429
as a missing article), an article that is still rate limited after four
tries is left out, and the build carries on without it. On Zimfarm that
loss is per task (the cache dies with the container), so the next run asks
again. From this sandbox's shared IP the Wikimedia APIs were rate
limiting on 2026-09-29, so the `full` Monaco build measured below got 16 of
its 53 articles; that is the sandbox's IP, not the flag.

## Feature parity with StreetZim's own builds

The production builds are `ops/build-region-fast.sh` (which
`ops/ship-region.sh` runs after fetching the Overture parquets) and the
older `cloud/build_region.sh`. Both call `create_osm_zim.py`
directly. "streetzim" below is `streetzim/cli.py` and
`offliner-definition.json` on this branch.

| feature | production | `streetzim` | default | licence | data source | works in a Zimfarm task? |
|---|---|---|---|---|---|---|
| Vector map (OpenMapTiles schema, z0-14) | planet tiles (`--mbtiles`) | tilemaker from the extract, or `--mbtiles` (not offered on Zimfarm) | on | ODbL (OSM), CC BY 4.0 (OpenMapTiles schema) | OSM extract (Geofabrik, or `--pbf-url`) | yes |
| Ocean, glaciers, urban areas at low zoom | yes | yes | on | ODbL (water polygons), public domain (Natural Earth) | osmdata.openstreetmap.de, naturalearthdata.com shapefiles | yes, about 900 MB download per task ([Disk](#disk-for-a-zimfarm-recipe)) |
| Low-zoom lakes | yes | yes | on | public domain (Natural Earth, inlined in the viewer) | none at build time | yes |
| Search (places, streets, addresses, POIs; Kiwix title and full-text) | yes | yes | on | ODbL | the extract | yes |
| OSM addresses | yes | yes | on | ODbL | the extract | yes |
| Find chips, places page, detail pages | yes (`--split-find-chips`) | yes | on | ODbL | the extract | yes |
| Large search chunks split for iOS (`--split-hot-search-chunks-mb 10`) | yes | **was missing; now always** | on | - | - | yes |
| No bulk `category-index/{addr,poi,street}.json` (`--no-llm-bundle`) | yes | **was missing (ZIMs carried them); now always** | on | - | - | yes |
| Routing, drive / walk / bike, 0.1° cells | yes | yes | on | ODbL | the extract | yes |
| Wikidata facts (population, description, Wikipedia extract) | yes | `--wikidata` existed, off; **now on in `full`**, `--no-wikidata` added | full: on | CC0 (Wikidata), CC BY-SA 4.0 (extracts) | query.wikidata.org SPARQL, en.wikipedia.org API | yes: network, no credentials; anonymous rate limits (backs off on 429) |
| Q-ID to Wikipedia title backfill (`--resolve-wikidata-titles`) | yes | **was missing; now part of `--wikipedia`** | full: on | CC0 | www.wikidata.org API | yes, as above |
| Wikipedia articles bundled (`--bundle-wiki-articles`) | yes, from the enwiki maxi ZIM | **was missing; now `--wikipedia`**, text from the API | full: on | CC BY-SA 4.0 (each page keeps its source link and licence) | en.wikipedia.org `action=parse` | yes, rate limited ([above](#wikipedia-articles-on-zimfarm-the-api-not-a-wikipedia-zim)) |
| Wikipedia images (`--wiki-images all`) | yes | **was missing; now `--wikipedia-zim-url` + `--wikipedia-images`** | off (no URL): the only source is a Wikipedia ZIM, 6.5 to 119 GB per task | per image, as in the Kiwix ZIM | download.kiwix.org | only with the disk and time for the download |
| Overture addresses (`--overture-addresses`) | yes | **was missing; now `--overture`** | full: on | per source, listed in the ZIM's `overture-sources.json` (CDLA-Permissive-2.0, CC0, CC BY, ODbL, ...) | overturemaps-us-west-2 S3 over HTTPS, STAC catalog | yes: anonymous HTTPS, `latest` resolved via STAC; tested here |
| Overture places: websites, phones, brands, categories (`--overture-places`) | yes | **was missing; now `--overture`** | full: on | CDLA-Permissive-2.0 | as above | yes, as above |
| Dead-website filter for Overture places (`--url-cache`) | yes | no | - | - | a crawl of every Overture website, kept on StreetZim's build host | no: the crawl result is not published anywhere a task could fetch it; without it, Overture places keep websites that may be dead |
| Terrain / hillshade / 3D terrain (`--terrain`, `--low-zoom-world-vrt`) | yes | `--terrain` off; on by default in `topic-terrain-openzim` | full: on (once `--no-terrain` exists) | Copernicus DEM licence (free, attribution) | Copernicus DEM on AWS S3 | yes (that branch) |
| Satellite imagery (`--satellite`) | yes, except `satellite=no` variants | not offered; opt-in with licence labelling in `topic-satellite-optin` | off, in no profile | **CC BY-NC-SA 4.0** | EOX Sentinel-2 cloudless | that branch |
| 3D buildings | no (the viewer has no building extrusion; "3D" is terrain) | no | - | - | - | - |
| Fonts: Open Sans, Noto Sans for Arabic, Hebrew, Armenian, Georgian, Lao, Thai; RTL shaping | yes | yes | on | Apache 2.0, OFL 1.1, BSD-2-Clause (RTL plugin) | glyphs pinned by sha256; in the Docker image, fetched otherwise | yes |
| Dark map style, POI icons (Maki) | yes | yes | on | CC0 (Maki) | inlined in the viewer | yes |
| GPS, driving HUD, `#dest=` deep links | yes | yes | on | - | the viewer | yes |
| Offline PWA (`/drive/`: install, service worker, streaming from archive.org) | the website's, not in the ZIM | no | - | - | - | not applicable: Kiwix ignores in-ZIM manifests (docs/in-zim-apps.md) |
| Max-zoom variants (`cloud/region-variants.tsv`) | yes | `--max-zoom` | 14 | - | - | yes |
| Rust packer, external Xapian builder | yes (speed only; same ZIM content) | no | - | - | local binaries | not needed |

All gaps that can work on Zimfarm are closed except the dead-website filter,
which has no public source. Terrain and satellite are left to their branches.

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
`basic` profile (tilemaker, search, chips, routing), OSM extracts from
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

### Per profile, and the resources a recipe needs

All the measurements above are `basic` builds (then called the default
profile: tiles, search, chips, routing), made before `streetzim` passed
`--no-llm-bundle` and `--split-hot-search-chunks-mb 10` as production does;
the first only leaves files out and the second only splits chunks over
10 MB, so they are if anything upper bounds for the ZIM size. Monaco was measured for both
profiles on 2026-09-29 with `tools/measure_build.py`, `streetzim` from this
branch, outside Docker (Python 3.11), on the same 4-core, 15 GB machine,
shared with other jobs (load average about 36), so the wall times are upper
bounds. Each run started with an empty `--dl`, as a Zimfarm task does, so
it includes downloading the extract (from openstreetmap.fr: Geofabrik is
blocked here), the font glyphs and, for `full`, the Overture data; the
shapefiles were given with `--shapefiles`. Terrain and satellite are not on
this branch (`topic-terrain-openzim`, `topic-satellite-optin`), so `full`
here is full minus terrain: Wikidata, Wikipedia articles and Overture on
top of `basic`. Disk is the temp, output and download folders.

| Monaco | wall time | CPU time | peak memory | peak disk | ZIM |
|---|---|---|---|---|---|
| `basic` | 1.2 min | 0.9 min | 1.9 GB (1.9) | 0.01 GB | 2.9 MB |
| `full` minus terrain | 7.7 min | 1.4 min | 2.8 GB (2.8) | 0.02 GB | 3.3 MB |

`full` spent its extra time waiting, not computing: the Overture download
(latest release resolved through STAC, 1 of 64 address files and 1 of 16
place files read, 4,087 and 3,181 rows) took about 13 s, and the rest was
Wikimedia's rate limiting. Every Wikimedia API answered this sandbox's
shared IP with 429 that day: Wikidata SPARQL backed off for 2 minutes, the
Q-ID to title backfill resolved 0 of 95 Q-IDs, and 16 of 53 articles got
through (the other 37 were still rate limited after four tries). An earlier
run the same day, identical but for a bug since fixed in the title cache
path, peaked at 3.3 GB and took 7.1 min. The ZIM passed
`tools/check_openzim_output.py --routing` and `cloud/validate_zim.py`
(zimcheck included), with `hasOvertureAddresses`, `hasWikidata` and
`hasWikiArticles` set and the Overture and Wikipedia licences in `License`.

What to give a recipe (`resources` in `POST /v2/recipes`). The `basic`
rows follow from the measurements above; the `full` rows are **estimates**
from them, since only Monaco was measured with `full`:

| extract size (example) | profile | cpu | memory | disk |
|---|---|---|---|---|
| up to about 60 MB (Monaco, Luxembourg, Rhode Island) | `basic` | 2 | 6 GiB (measured 1.9 to 4.1 GB) | 4 GiB (measured 3.5 GiB on Zimfarm for Monaco) |
| | `full` | 2 | 6 GiB (Monaco measured 2.8 to 3.3 GB) | 4 GiB |
| about 700 MB (Switzerland) | `basic` | 4 | 8 GiB (measured 4.6 GB) | 10 GiB (4 + 4.4 measured + extract) |
| | `full` | 4 | 10 GiB (estimated) | 12 GiB (estimated: Overture parquets and article cache on top) |
| about 1.6 GB (the Netherlands) | `basic` | 4 | 12 GiB (measured 10.8 GB) | 20 GiB (4 + 11.0 measured + extract; less failed with ENOSPC) |
| | `full` | 4 | 14 GiB (estimated) | 22 GiB (estimated) |

- Memory in `full` grows with the Overture merge (DuckDB, and more search
  records to write) and the Wikidata cache: Monaco's peak went from 1.9 to
  2.8 GB. For larger regions the added records are a larger share of the
  search step (Overture addresses are dense where national registries feed
  them, as in the Netherlands), hence the extra 2 GiB estimated.
- Time in `full` is dominated by the Wikimedia APIs: one SPARQL request per
  40 Q-IDs with a 1 s pause, and one request per article with at least
  0.1 s between them (California links 11,613 articles: over 20 minutes
  before any rate limiting). A recipe for a large region should allow hours
  on top of `basic`'s time, not minutes.
- Terrain (on in `full` once merged) adds the Copernicus DEM tiles and the
  hillshade tiles to z12, and satellite (opt-in) the imagery; their
  branches measure them. Add their disk and time to the `full` rows.

### Downloads per task

A fresh Zimfarm container downloads, besides the OSM extract:
- the coastline and Natural Earth shapefiles when tiles are built with
  tilemaker (see below). Building from `--mbtiles` avoids this;
- nothing for the viewer: MapLibre GL JS is vendored and the Docker image
  carries the pinned font glyphs ([viewer-supply-chain.md](viewer-supply-chain.md));
- with `--profile full` (or the flags): Overture's addresses and places for
  the box (DuckDB reads only the files whose STAC bbox meets it, and only
  the row groups inside it: 0.1 and 0.3 MB for Monaco), Wikidata facts by
  SPARQL, and one Wikipedia API request per article. The DuckDB `spatial`
  and `httpfs` extensions are installed in the image; outside it, DuckDB
  fetches them (about 100 MB) from extensions.duckdb.org on first use;
- with `--wikipedia-zim-url`, the whole Wikipedia ZIM;
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

Monaco on the local Zimfarm below (what is now `--profile basic`, recipe resources cpu 2,
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
