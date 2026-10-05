# StreetZim: how it works and how it is built

A primer for product and partner readers (no coding background needed), as of
October 2026. It describes the current code; engineering detail lives in the
documents listed under [Further reading](#further-reading).

StreetZim packs an interactive offline map of a region — streets, search,
turn-by-turn routing, terrain and place information — into a single .zim file
that opens in any Kiwix reader. This primer explains what is in the file, how
each feature works on the device, and how the file is built.

## Being worked on

Three improvements are in progress; the sections below describe the product as
it ships today.

- **Real walking and cycling routes.** Today the Walk and Bike modes show the car
  route with a walking or cycling time. The new routing uses footpaths, steps,
  pedestrian streets and cycle tracks, lets pedestrians walk against one-way
  streets, follows bike rules (one-way exemptions, push-your-bike sections), and
  keeps published files working.
- **Search in every script.** Today a place is searchable by one name: its English name when OpenStreetMap has one, otherwise its local name. So 北京 may not find Beijing, which is stored as "Beijing". Local-script names (北京, Москва, Αθήνα, 東京) will be searchable alongside the English ones, including part of a Chinese or Japanese name, which has no spaces between words.
- **StreetZims in other languages.** Today every file is English. A build will be
  able to choose a language: place names on the map and in search, Wikidata facts,
  Wikipedia articles and summaries, and the file's metadata in that language, with
  local names kept alongside.

## At a glance

```
Public data, downloaded during the build
  OpenStreetMap (roads, places) · Overture Maps (addresses) · Copernicus DEM (elevation)
  · Wikipedia (facts, articles) · EOX imagery (optional)
        │ downloaded
        ▼
Build: the streetzim command, one Docker run (Zimfarm recipe)
  tilemaker (map tiles) · search index (places, chips) · routing graph (in 0.1° cells)
  · terrain (elevation tiles) · libzim (writes the ZIM)
        │ written into one file
        ▼
One .zim file (China: 21 GB)
  map tiles (vector, z0–14) · terrain (WebP, z0–12) · routing cells (binary graph)
  · search + chips (JSON files) · app + articles (HTML, JavaScript)
        │ opened offline
        ▼
On the device: the map app runs inside any Kiwix reader
  map (MapLibre draws) · search box (loads few files) · Find chips (300 nearest)
  · directions (A* on cells) · Kiwix search (a page per place)
```

The build runs once per region and needs the internet; everything after it,
from opening the file to routing, works offline inside Kiwix.

## What is inside a StreetZim file

A StreetZim file is an ordinary ZIM whose main page is a map app. Everything the
app needs — its code, the map, search, routing and place information — is stored
as ZIM entries and read on the device; it needs no internet connection. (A few links, such as
Wikipedia in other languages, open online pages only if tapped.)

Shares below are from the published China file (21.3 GB, 11.7 million entries). It was made
by StreetZim's own production pipeline, not by the openZIM `streetzim` command, and it carries
satellite imagery; files from the command differ in details (for example their Find chips and
metadata).

| Part | Entries in the ZIM | Format | China: size, share |
| --- | --- | --- | --- |
| Map | `tiles/{z}/{x}/{y}.pbf` | Vector tiles (OpenMapTiles schema), zoom 0–14 | 5.1 GB, 24% |
| Terrain | `terrain/{z}/{x}/{y}.webp` | Elevation encoded as colours, lossless WebP, zoom up to 12 | 5.0 GB, 24% |
| Satellite (optional) | `satellite/{z}/{x}/{y}.avif` or `.webp` | Photo tiles, 256 px | 4.6 GB, 21% |
| Routing | `routing-data/graph-cells-index.bin` + `graph-cell-NNNNN.bin` | Custom binary road graph, split into 0.1° map cells | 3.0 GB, 14% |
| App search | `search-data/*.json` | JSON lists of places, streets and addresses, split by name prefix | 1.5 GB, 7% |
| Kiwix search | `search/*.html` + the Xapian full-text index | One small HTML page per place (and per POI in the full profile) | 0.8 GB, 4% |
| Wikipedia | `wiki-article/*`, `wiki-image/*` | Article HTML and images, English | 0.25 GB, 1% |
| Find chips and categories | `category-index/*.json` | JSON, cut into geographic shards when large | 0.15 GB, 0.7% |
| Place facts | `wikidata/*.json` | Wikidata facts (population, elevation…) | 5 MB |
| The app itself | `index.html` (map), `places.html` (Find), `routing-worker.js`, MapLibre GL JS, fonts | HTML/JS, glyph files | about 4 MB |

Map tiles, terrain and satellite are most of the size; terrain and satellite are
layers a build can leave out. The app files sit in fixed-size slots so the viewer
in an already-published ZIM can be updated in place without rebuilding.

## The map

The map is drawn on the device from vector tiles, not shipped as pictures. Each
tile holds roads, buildings, water, labels and points of interest as geometry;
MapLibre GL JS (an open-source map renderer, version 5.23, bundled in the file)
draws them in the reader's browser engine, so labels stay sharp and the style can
change (light or dark) without new data.

- **Tiles.** OpenMapTiles schema, made from OpenStreetMap by tilemaker. Detail is
  stored up to zoom 14 (about street level); the app zooms on to 20 by scaling the
  zoom-14 data. Empty tiles are left out, and identical tiles (open sea) are
  stored once and pointed to.
- **Inside Kiwix.** The page reads every tile, font and image from the ZIM by
  relative address, through a small loader that retries and keeps at most six
  requests in flight, because Kiwix's request handling drops requests under load.
- **Labels.** Fonts are glyph files in the ZIM: Open Sans, with Noto Sans merged
  in for scripts Open Sans lacks; right-to-left text (Arabic, Hebrew) is shaped by
  a bundled plugin.
- **What the user can do.** Pan and zoom, tap any labelled place for its
  information card, switch light/dark/auto theme, toggle terrain and satellite
  layers when present, and share a position as a link inside the file
  (`index.html#dest=…`).
- **Same app on the web.** streetzim.web.app hosts the same viewer as a web app
  that opens a downloaded ZIM or streams one, which is how viewer fixes reach
  published files fastest.

## Search and Find chips

There are two searches: the app's own search box, which covers every place,
street, address and point of interest, and Kiwix's built-in search, which covers
named places through small pages written for it.

**The app's search box.** Every searchable thing is a short record: name, kind
(place, street, address, shop…), position, and a location label ("Lyon, France").
The records are split into small JSON files by the first two letters of each word
of the name, plus an index file. Typing "rue de" loads only the files for those
letters, so a search touches a few files, not the whole region.

- Matching ignores accents and case: names are reduced to plain letters before
  indexing, the same way in the build and in the app.
- Busy prefixes ("st", "ca") are split further, aiming at files of about 4 MB. A
  one-letter query, or a query in Chinese or Japanese, reads more files than a typical one.
- Addresses come from OpenStreetMap plus Overture Maps (an open dataset of
  addresses and places), merged where OSM has none. Administrative areas
  (countries, regions, cities) are searchable with their alternate names.
- Known gap, being worked on: each place is indexed under one name, its English name
  when OpenStreetMap has one and otherwise its local name. So the local name of a place that
  also has an English name is not searchable (北京 may not find Beijing). Map labels follow the
  same rule. Indexing local names as well is in progress.

**Kiwix's own search.** Kiwix's search bar and suggestions use the ZIM's title
list and its Xapian full-text index, which only see pages. So the build writes a
small page for each named place, park, peak, lake, airport and administrative
area, and in the full profile for every named point of interest. Each page has
"Directions to here" and "View on map" buttons that open the map app. A
point-of-interest page costs about 440 bytes in the file.

**Find chips.** Under the search box is a row of category chips: Food & Drink,
Bars, Hotels, Museums, Landmarks, Parks, Libraries, Health, Shops and Gas. A tap shows the 300 matches nearest the map's centre as pins with a scrolling card list,
with distances from the user's GPS position when it is known. The Find page
(`places.html`) shows the same as a list with sorting and filters.

- Each chip is decided at build time by fixed rules on the OpenStreetMap category
  (for example Shops = shop, supermarket, mall, marketplace…), and stored as its
  own file.
- A chip bigger than 2 MB is cut into geographic shards of 1–2 MB each, so a phone
  loads only the shards around it. Loading stops at a memory budget (12–32 MB
  depending on the device) and says when results are partial.

## Offline routing

Directions are computed on the device, from a road graph stored in the file, by a
search that runs in the background of the page. No routing server is involved.

**The road graph.** At build time every OpenStreetMap road becomes edges between
junctions. Each edge stores its length, a speed for its road class (motorway
100 km/h, primary 60, residential 30, track 15, footway 5…), its road name, its
shape for drawing, and flags: one-way, roundabout, no cars, no bikes, no
pedestrians. Roads under construction, proposed or abandoned are left out;
private and destination-only roads stay in.

**Split into map cells.** The graph is cut into cells of 0.1° latitude by 0.1°
longitude (about 11 km by 8 km in Europe); each cell is its own small file with
its roads and their positions, plus one index file listing the cells. A route loads the cells its search explores, plus a corridor along the straight line fetched
in advance, and keeps 64–384 MB of cells in memory depending on the device (192 MB when the
device does not say). One long Japanese route measured about 500–600 MB of memory in total.

**The search.** The app uses A* (a standard shortest-path search guided by
straight-line distance). It tries an exact search first (up to 200 km straight-line distance), then progressively
faster approximate ones, and finally a two-stage route: local roads at each end and only
major roads (motorway, trunk, primary) in between. Each pass explores a fixed number of
junctions, so routes over 200 km are good but not guaranteed to be the shortest. It answers
"no route" quickly when the destination is in a small unreachable area such as an island
without ferries.

**Turn-by-turn.** Driving mode shows the next turn and its distance and the arrival time,
follows GPS, and keeps the screen on. After 60 m off the route it shows "Off route" with a
Re-route button. A turn is announced where the road name changes, or on entering or leaving
a roundabout or a ramp. There is no voice guidance. Speeds come from the road's class, not
from posted speed limits, and OpenStreetMap turn restrictions (no left turn and the like) are
not applied.

**Limitation to know: walk and bike.** The Walk and Bike buttons change the time remaining during
navigation (1.4 m/s walking, 4.5 m/s cycling) and the map view, but the route itself, and the
time shown when it is planned, are for a car: it avoids footpaths and cycle tracks and
follows one-way streets. Real walking and cycling routes are being worked on:
footpaths, cycle tracks and pedestrian streets for walking and cycling,
against-one-way walking, and bike-specific road rules; the graph already records
the footpath and cycleway flags they need.

## Elevation and imagery

**Terrain.** Elevation comes from the Copernicus digital elevation model, a free
global dataset from the European Space Agency: 30 m resolution (GLO-30) for close
zooms, and a coarser 90 m mosaic for country-wide views. The build turns it into
terrain tiles: each 256-pixel tile stores the height of every pixel as a colour
(the common "terrain-RGB" encoding), rounded to 10 m, in lossless WebP. Rounding to
10 m cuts the terrain's size by about three quarters with no visible change.

- Tiles go up to zoom 12 (about 38 m per pixel at the equator, finer towards the
  poles), starting two zoom levels below the widest view of the region.
- In the app, a terrain button cycles off → hillshading → 3D relief.
- The build checks itself: every elevation file under every tile must be present
  (or known sea), and tiles are sampled against the source; a tile that reads sea
  level over land fails the build instead of shipping blank mountains.
- Cost: small for small areas (Luxembourg +2.9 MB, +5%), about a quarter of the
  file for a big country (China 5.0 GB of 21.3 GB).

**Satellite imagery (optional).** EOX's Sentinel-2 cloudless mosaic, a cloud-free
picture of the Earth assembled from European Sentinel-2 satellite images, as photo
tiles up to zoom 14 (zoom 13 when the region's centre is at 45° latitude or more, unless the
build sets the zoom). It is in no profile; a build must
ask for it.

- Licence: the 2016 mosaic is CC BY 4.0 and is the default; the newer 2021 mosaic
  is non-commercial (CC BY-NC-SA 4.0) and has to be accepted explicitly, which
  marks the ZIM non-commercial in its metadata and in the app.
- Open item: written confirmation from EOX before publishing satellite ZIMs widely.

## Place information

Tapping a city, museum or mountain opens a card with facts and, when one exists,
its Wikipedia article, all from the file.

- **Wikidata facts.** For every place tagged with a Wikidata ID in OpenStreetMap:
  name, short description, population, area, elevation, country, capital, time
  zone, website and type, plus a three-sentence summary from English Wikipedia.
  Stored as small JSON files grouped by ID; about 5 MB for China.
- **Wikipedia articles.** Full English articles for the places that link one (from a
  few hundred to tens of thousands per region; the China file has 24,620). A
  build can take them from a Kiwix Wikipedia ZIM given to it, with images, or fetch
  text-only versions from the Wikipedia API at Wikimedia's polite rate of 120 per
  minute. Articles whose OSM tag is in another language are matched to the English
  article through Wikidata.
- **Nearby Wikipedia.** An index of where each bundled article is lets the app
  list and pin nearby articles at any zoom.
- **English only today.** Facts, summaries, articles and the ZIM's Language
  metadata are English.

## How a file is built

One command, `streetzim`, builds a ZIM from public data in a single run inside a
Docker image, the way openZIM's other scrapers do. It is published as
`ghcr.io/jasontitus/streetzim` (Linux x86-64) and described by a generated
Zimfarm offliner definition.

**Inputs, all public:** an OpenStreetMap extract for the region (Geofabrik, or any
URL), coastline and Natural Earth shapefiles, Overture Maps addresses and places,
Copernicus elevation, Wikidata and Wikipedia (the APIs, or a Wikipedia ZIM), and
EOX imagery if satellite is requested.

**The steps, in order:**

1. **Get the OSM data** and cut it to the region.
2. **Make the map tiles** with tilemaker (an open-source tile generator), zoom
   0–14. Large regions use its disk store to save memory.
3. **Prepare the tiles**: drop empty ones, share identical ones.
4. **Build the search index**: read every named thing from the tiles, add
   addresses (OSM and Overture), administrative areas and Overture places, and
   write the search files and Find-chip categories.
5. **Collect Wikidata facts** for every tagged place.
6. **Build the routing graph** from the OSM roads, then split it into map cells.
7. **Make terrain tiles** from Copernicus elevation (and satellite tiles, if
   requested); then check every terrain tile.
8. **Check the bundled map library** (MapLibre) against its pinned version.
9. **Write the ZIM** with libzim (openZIM's library): the app, tiles, search,
   chips, routing, the Wikipedia articles (fetched in this step), the Kiwix search pages and the
   full-text index, with standard metadata.

**Profiles.** A recipe picks one of two, and can switch single features on or off:

| Profile | Map, search, chips, routing | Wikidata and Wikipedia | Overture addresses | Terrain | Every POI in Kiwix search |
| --- | --- | --- | --- | --- | --- |
| `full` (default) | yes | yes | yes | yes | yes |
| `basic` | yes | no | no | no | no |

Satellite is in neither profile and must be requested. By convention the recipe names the ZIM
`osm_en_<area>` (the build takes the name it is given); flavour `maxi`, or `satellite` /
`satellite-nc` with imagery; tagged `maps;osm;offline`.

**Checks.** Every change to the code is tested automatically, including a full-profile
build of Monaco in Docker with no network (from pinned, cached inputs, terrain off), validated
with openZIM's `zimcheck` and StreetZim's own checks; a build from live sources runs weekly. The build command itself
does not run zimcheck.

## Cost to build

A country builds in minutes to a couple of hours on 4 cores; China, among the
largest regions, has built in about 12 hours inside a 16 GB container, the size Zimfarm
offers, with little memory to spare so far. The `full` profile adds hours of waiting on Wikimedia's rate limits
for large regions.

| Region | OSM extract | Profile, machine | Time | Peak memory | ZIM |
| --- | --- | --- | --- | --- | --- |
| Luxembourg | 56 MB | basic, 4 cores | 3 min | 4.0 GB | 55 MB |
| Switzerland | 679 MB | basic, 4 cores | 70 min | 4.6 GB | 654 MB |
| Netherlands | 1.6 GB | basic, 4 cores | 92 min | 10.8 GB (older code) | 1.2 GB |
| China | 6.5 GB | full, 16 GB container | 11 h 38 min | 15.6 GB (16 GiB limit) | 22.8 GB |

- **China in 16 GB.** The first complete build passed on 4 October 2026, peaking at
  15.6 GB, about 0.4 GB under the limit, after changes that each removed a memory peak: routing extraction and its map-cell
  split run in separate short-lived processes, terrain workers cap their image
  cache, and the Find chips are built one at a time. Two further changes since,
  measured on China's data: the map-cell split reads the road graph from disk
  instead of copying it into memory (8.7 GB to 0.8 GB), and only the region's own
  Wikidata facts are loaded (4.4 GB to 1.2 GB). A full China run on that final code, which should
  leave a wide margin, has not finished yet.
- **Europe** (a 42 GB extract, 6.5 times China) is building in a 16 GB container as
  the next test.
- **Recommended recipe sizes** (Zimfarm): up to 60 MB extract, 2 cores and 6 GiB;
  around 700 MB, 4 cores and 8–10 GiB; around 1.5 GB, 4 cores and 12–14 GiB. The
  build sizes its own parallel work to one core per 2 GiB of memory, and switches
  tilemaker to its disk store for large extracts.
- **Disk:** 6 to 11 times the extract size while building (Switzerland 6.5×, China
  71 GB for a 6.5 GB extract), mostly temporary.
- **Downloads per task:** the extract, about 900 MB of shapefiles, Overture for the
  region's box, and with terrain an elevation download of 0.4 to 5 times the
  extract size.

## Points for a Kiwix decision

The proposal is that openZIM adopts StreetZim as its map scraper after a pilot on
Zimfarm; the alternative, porting StreetZim's features into maps2zim, is written up
as a nine-step plan in the repository.

**Status of what openZIM's review asked for:**

| Ask | Status |
| --- | --- |
| Separate builder and viewer, modular code | Done |
| Operations scripts out of the scraper | Done in the tree; separate repository pending |
| libzim instead of custom writers | Done; the faster custom writers are optional |
| Binary formats frozen and documented | Done, with readers for older published files kept |
| openZIM conventions: CLI, metadata, progress file, offliner definition, Docker, CI | Done |
| tilemaker, osmium, GDAL in the image, costs measured | Done |
| Non-commercial satellite imagery | Opt-in only; CC BY default |

**Still open:** splitting the repository; registering on Zimfarm (an `openzim/`
image name, workers, the definition upload); moving the ZIM writer to
zimscraperlib's Creator; a side-by-side comparison with real Wikipedia articles.

**Licences in every file**, listed in its License metadata from the layers present:
OpenStreetMap (ODbL), OpenMapTiles schema (CC BY 4.0), Copernicus elevation,
Wikidata (CC0), Wikipedia (CC BY-SA 4.0), Overture (per-source, credits in the
file), satellite (CC BY 4.0, or marked non-commercial), fonts (Apache 2.0 / OFL), icons (CC0), MapLibre GL JS (BSD-3-Clause), the right-to-left
text plugin (BSD-2-Clause), StreetZim's code (MIT).

**Limitations to weigh:**

- Walking and cycling routes use the car route today; real walking and cycling
  routing is being worked on (see [Offline routing](#offline-routing)).
- English only: facts, articles and metadata (StreetZims in other languages are being worked
  on).
- Search finds a place by one name, English when OpenStreetMap has one, otherwise local;
  searching by the local name as well is being worked on.
- The app needs a Kiwix reader that runs JavaScript, and keeps its requests gentle
  for Kiwix's request handling.
- Satellite imagery needs EOX's written confirmation before wide publication. Files made by
  StreetZim's own production pipeline, such as the published China file, carry the
  non-commercial 2021 imagery without being labelled non-commercial; the openZIM command labels
  it, and those files need relabelling or rebuilding.
- Updating the viewer inside an already-published file keeps the file's ID while its content
  changes. Kiwix's library recognises files by that ID, and mirrors check files by checksum, so
  openZIM would need a rule for it (for example a new ID with each viewer update).
- Routing quality: speeds come from road class, not posted limits; turn restrictions are not
  applied; routes over 200 km are not guaranteed shortest; re-routing is a button, not
  automatic.
- Very large regions need care on a 16 GB worker: China has fitted with 0.4 GB to spare, with
  fixes since to widen that; Europe is being tested.

## Further reading

These documents go deeper, written for engineers:

| Document | Covers |
| --- | --- |
| [zimfarm.md](zimfarm.md) | Running StreetZim on Zimfarm: profiles, flags, costs, satellite licences, Wikimedia etiquette |
| [adoption-plan.md](adoption-plan.md) | What openZIM's review asked for and the status of each item |
| [openzim-integration.md](openzim-integration.md) | The adoption proposal, and the plan to port into maps2zim instead |
| [in-zim-apps.md](in-zim-apps.md) | The map and Find pages, chips, deep links, Kiwix search pages |
| [formats.md](formats.md) | Every entry in the ZIM and the binary routing formats |
| [routing.md](routing.md) | The routing search, its fallbacks and its memory budget |
| [search-records.md](search-records.md) | Search records and how the search files are split |
| [find-chip-shards.md](find-chip-shards.md) | How large Find chips are cut into geographic shards and loaded |
