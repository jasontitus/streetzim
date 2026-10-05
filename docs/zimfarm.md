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
| Vector tiles | built with tilemaker from the OSM extract | or `--mbtiles-url` for a ready-made OpenMapTiles MBTiles, e.g. OpenFreeMap's ([below](#building-from-ready-made-tiles---mbtiles-url)); `--mbtiles` for a local file (command line only) |
| Search over every named feature, Find chips, places list | on | always |
| Offline routing (drive / walk / bike) | on, spatial layout (SZCI v3) | `--no-routing` |
| Overture Maps addresses and place details | on with full | `--overture` / `--no-overture` (reads Overture's public bucket) |
| Wikipedia articles (text) | on with full | `--wikipedia` / `--no-wikipedia` (Wikipedia API; images with `--wikipedia-zim-url`) |
| Wikidata place details | on with full | `--wikidata` / `--no-wikidata` (queries Wikidata) |
| Terrain (hillshade and 3D) | on with full, from just below the lowest zoom the viewer can show to z12 | `--terrain` / `--no-terrain` (downloads Copernicus DEM tiles; [cost](#terrain-cost)) |
| POIs in Kiwix's own search | on with full (without it, Kiwix's full-text search covers places, parks, peaks, water, airports and administrative areas) | `--kiwix-poi-pages` / `--kiwix-poi-pages=off` ([below](#pois-in-kiwixs-own-search---kiwix-poi-pages)) |
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
(`search/<slug>.html`): places, parks, peaks, water, airports and
administrative areas ([search-records.md](search-records.md#administrative-areas-t-admin)). Without
the flag, "Casino" on the Monaco ZIM finds the Fontaine du Casino and not
the Casino, the shops or the bus stops named after it.
`--kiwix-poi-pages` gives every named POI a page too, which Kiwix's
full-text search and its title suggestions both find. It costs about 440 B
per POI: roughly 40 B of compressed page, 80 B of directory entry and
pointers, 125 B of full-text index and 195 B of title index. Measured on
2026-09-29 with what is now `--profile basic`:

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

`--profile full` turns it on, so a full-profile ZIM carries the cost above
(about +12% to +19% for a country, +28% for Monaco); `--profile basic`
leaves it off. Like every profile feature it is an on/off choice
(`--kiwix-poi-pages=off`; an enum in offliner-definition.json), unset
meaning "as the profile says".

### Kiwix's search in each `--xapian` mode

The pages above, and so Kiwix's title suggestions and full-text search, are
the same with `--xapian libzim` (libzim's indexer, the default with the
libzim writer) and `--xapian builder` (the external `xapianbuilder`, with
the manifest writer; what StreetZim's own build host uses): both write
`search/<slug>-<i>.html` for every record of a page type and the admin
areas' redirects, and in builder mode xapianbuilder indexes those pages,
their redirect titles and the bundled Wikipedia articles, as libzim would.
`--xapian none` writes no page and no index. From 2026-05-08 to 2026-10-02
a builder build wrote no page and indexed `s/<n>` paths that did not exist,
so none of its Kiwix results opened; `cloud/validate_zim.py` and
`tools/check_openzim_output.py` now fail such a ZIM, and
`ops/cloud/swap_viewer_rust.py --rebuild-search --rebuild-xapian` mends the
published ones ([search-prefix-locality.md](search-prefix-locality.md#kiwixs-own-search---rebuild-xapian)).
Writing the pages costs a builder build what it costs a libzim one: measured
on 2026-10-02, two builds each, Luxembourg `--profile basic` (8,166 pages,
144 redirects): 52.8 -> 54.2 MB (+1.4 MB, +2.7%), 149 and 144 s -> 154 and
147 s wall (218 and 214 s -> 222 and 218 s CPU; about +3%, the size of the
spread between runs); a libzim build of the same: 54.4 MB, 145 s. Monaco
`--profile full` (5,114 pages, POIs included): 4.64 -> 5.87 MB (+27%, the
POI pages' cost above); libzim 5.97 MB.

## Profiles

`--profile` picks the content; any feature flag given explicitly wins over
it, in either direction (`--profile basic --wikidata`, or
`--wikipedia=off` on the default `full`). Routing is on in both (`--no-routing`
turns it off).

| feature | `full` (default) | `basic` |
|---|---|---|
| Wikidata place details | on | off |
| Wikipedia articles (text from the API; images only with `--wikipedia-zim-url`) | on | off |
| Overture Maps addresses and places | on | off |
| Terrain / hillshade | on | off |
| Every named POI in Kiwix's own search (`--kiwix-poi-pages`) | on | off |
| Satellite imagery | off: opt-in (`--satellite`); the 2021 source is non-commercial | off |
| Search, Find chips, places page, routing, fonts, dark style, icons | on | on |

`full` is everything StreetZim's own builds ship that openZIM can ship too;
`basic` fetches nothing besides the OSM extract and the shapefiles, and is
the cheapest (the "What a build costs" tables below are `basic` builds).
Satellite is in neither, by choice: it stays off for openZIM's main
distribution and is one flag to turn on (the default source is CC BY 4.0;
the sharper 2021 one is non-commercial and needs an explicit
acknowledgement).

Each feature is one flag with three states: on (`--x`, `--x=on`), off
(`--x=off`, `--no-x`), or not given, when the profile decides. Saying both
on and off is refused before anything is downloaded, and flag names cannot
be abbreviated. `--kiwix-poi-pages` and `--terrain` are defined as plain
flags in the parser's "Content" group (`--terrain` with `--no-terrain`);
`add_profile_arguments` in `streetzim/cli.py` turns each into the same
three-state flag.

On Zimfarm, `profile` is a **required** string-enum (`full`, `basic`), so
every recipe states its profile next to the resources it is given, and each
feature is one optional string-enum (`on`, `off`; unset: as the profile
says). A recipe therefore cannot switch a feature both ways.
`tests/test_offliner_definition.py` runs recipes through Zimfarm's own
models and `compute_flags` and checks the features `streetzim` then builds.

Recipe flags (the `offliner` part of `POST /v2/recipes`, dash form):

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with search and routing",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly",
 "profile": "full", "cpus": 2}
```

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg-basic", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with search and routing",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly",
 "profile": "basic", "wikidata": "on"}
```

(the second is `basic` plus Wikidata), and the same on the command line:

```sh
streetzim --name osm_en_luxembourg --title Luxembourg \
  --description "Offline map of Luxembourg with search and routing" \
  --include-poly https://download.geofabrik.de/europe/luxembourg.poly \
  --profile full --cpus 2 --output /output # or --profile basic
streetzim ... --profile basic --wikidata    # basic plus Wikidata
streetzim ... --overture=off                # full without Overture (or --no-overture)
```

### Failure policy

- **Overture** failing is fatal: if the release cannot be resolved, the
  files listed or read (network, DuckDB), the task fails with a message
  naming the source (`--overture=off` builds without it). A ZIM that says
  it has Overture data has it.
- **Wikidata** and **Wikipedia** degrade: the builder retries (honouring
  `Retry-After`, within a wait budget, `cloud/wikimedia_http.py`) and then
  leaves out what the APIs did not answer. It never caches a rate limit as
  "no article", so the next run asks again. The ZIM's `hasWikidata` /
  `hasWikiArticles` flags and its `License` describe what it holds.
  `STREETZIM_REQUIRE_WIKI=1` makes a missing article fatal instead.
- `streetzim` ends with one line saying what each source delivered, e.g.
  `streetzim: sources: Overture addresses 4087 rows (4012 added); Overture
  places 36 enriched, 3028 added; Wikipedia titles 0/95 Q-IDs resolved;
  Wikidata 45 entries; Wikipedia articles 16/53 (37 not fetched, 37
  rate-limited)` (`streetzim/source_report.py`).

### Wikimedia API etiquette for periodic recipes

A `full` recipe queries Wikimedia on every run, and Zimfarm runs recipes
periodically, so:
- requests carry a descriptive User-Agent naming the project and its issue
  tracker (`cloud/wikimedia_http.py`, per Wikimedia's User-Agent policy);
  an operator can add a contact address with `STREETZIM_WIKI_CONTACT` (set
  it on the worker, not in the recipe or the repository);
- one run makes one SPARQL request per 40 Q-IDs (1 s apart), one
  extracts request per 20 articles, one `wbgetentities` request per 50
  Q-IDs for titles, and one `action=parse` request per article. For
  Monaco that is about 60 requests; for California (11,613 linked
  articles) about 12,000. Schedule large-region `full` recipes no more
  often than their data changes (monthly), not in parallel with each
  other from one worker IP;
- the caches live in the task's `--dl` and die with it, so every run asks
  again. A worker with persistent storage could keep `--dl` between runs;
  Zimfarm has no such mount today.

What Wikimedia asks (read 2026-09-30):
- [API:Etiquette](https://www.mediawiki.org/wiki/API:Etiquette): "There
  is no hard speed limit on read requests, but be considerate and try not
  to take a site down." and "Making your requests in series rather than
  in parallel, by waiting for one request to finish before sending a new
  request, should result in a safe request rate." On `ratelimited`: "you
  may retry that request, however you should increase the time between
  subsequent requests." `maxlag` is for non-interactive tasks: "Higher
  values mean more aggressive behaviour, lower values are nicer."
- [Wikimedia APIs/Rate limits](https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits)
  (new in 2026, "subject to experimentation and change"): "Unauthenticated
  bot requests with a compliant User-Agent header" get **200 requests a
  minute**; the limits "apply across all sites and platforms, including
  requests to the Action API and REST APIs, and are enforced per user",
  counted "per-minute". Clients should "limit the number of concurrent
  requests to 3 or fewer" and "respect the Retry-After header provided
  with a 429 Too Many Requests status code"; when a 429 or 503 carries
  none, "clients should wait at least five seconds, or implement
  exponential back-off".
- [Robot policy](https://wikitech.wikimedia.org/wiki/Robot_policy), Action
  API: "If unauthenticated, keep the concurrency of your requests to 1 at
  a time, and below 5 requests per second overall", "if your request takes
  more than 1 second to serve, please wait 5 seconds before making another
  request", "Where supported, use batch requests", and "Always request
  content with an `Accept-Encoding: gzip` HTTP header". It also says
  "Avoid using the action API for HTML content of pages. Use the website
  and/or the REST API instead."
- [User-Agent policy](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy):
  `<client name>/<version> (<contact information>) <library>/<version>`;
  a generic agent (`Python-urllib` alone) gets HTTP 403.

How the requests follow it (`cloud/wikimedia_http.py`: `polite_pacer`,
`Pacer`, `get_json`). Wikipedia articles (`action=parse`), Wikipedia
extracts and Wikidata title lookups (`wbgetentities`) each run one
serial loop with a `polite_pacer`:
- one request at a time; the pause runs from the end of each response,
  0.1 s by default (`STREETZIM_WIKI_GAP`);
- at most 120 request starts a minute (`STREETZIM_WIKI_MAX_PER_MIN`), so a
  run of fast answers stays well under the 200 a minute. With 0.15 s
  answers and only the 0.1 s gap a loop would make ~240 a minute. The cap
  assumes **one Wikimedia client per worker IP**: the loops run one after
  another, but two builds side by side from one IP would each take the
  full rate, so lower `STREETZIM_WIKI_MAX_PER_MIN` for a worker that runs
  several `full` tasks at once;
- 5 s after an answer that took over 1 s;
- a 429, maxlag, or 5xx with `Retry-After` doubles the gap between
  requests, up to 30 s, and each success eases it 10% back. The retry of
  the refused request itself waits the whole `Retry-After` (at least 5 s
  after a 429 or 503 without one); a `Retry-After` over 120 s stops the loop for
  this run instead of retrying before the server allows it. The Wikidata
  title backfill retries a 429/maxlag/ratelimited answer until the wait
  budget below is spent rather than five times, since replication lag
  lasts minutes; every other request keeps five tries;
- `STREETZIM_WIKI_WAIT_BUDGET` (default 900 s) bounds the waiting spent on
  rate limits and retries **per step**, not per run: each of the Wikidata
  title backfill (`--resolve-wikidata-titles`), the Wikidata SPARQL
  properties, the SPARQL name lookups (`--mbtiles` input only), the
  Wikipedia extracts and the Wikipedia articles has its own, so a hard
  throttle can cost a `full` run up to four or five budgets. The pauses
  above are not charged to it;
- an extract or article the API did not answer (a rate limit, 5xx, a
  stopped step) is never cached as missing: the next build asks again. A
  page that has no extract is recorded as such (`no_extract`) and not
  asked again;
- gzip is requested and decoded.
The Wikidata SPARQL queries (query.wikidata.org, a separate service) keep
their 1 s (properties) and 0.5 s (name lookup) pauses, now from the end of
each response, widened by 429s and bounded by the same budget.

Until 2026-09 the article fetch paused a fixed 1 s after every answer. In
the D.C. `full` comparison (1,308 articles, no 429) that was 1,522 to
1,541 s of a 39 minute build, 1.17 s an article. **Estimates, not
measured** (this sandbox's IP was rate limited when we tried): at 120 a
minute the same 1,308 take at least 10.9 minutes, about 11 to 13 minutes
with slower answers and the 5 s pauses, so the build would take about 25
to 27 minutes instead of 39. For Switzerland the title count is an
extrapolation: it has 10,639 distinct `wikipedia=` values and 37,271
`wikidata=` values in OSM (taginfo.geofabrik.de, 2026-09-30), where D.C.
has 1,085 and 1,992 and bundles 1,308 titles, which suggests 13,000 to
16,000 titles. At 120 a minute that is at least about 2 hours of article
fetching (1.8 to 2.2 hours), against an estimated 4.2 to 5.2 hours at the
old 1.17 s an article.

Why one `action=parse` per article and not something batched:
- `action=parse` takes one page per request.
- `prop=extracts` batches up to 20 titles, but "Multiple extracts can only
  be returned if exintro is set to true" (lead sections only), so whole
  articles are still one request each.
- The REST API (`/api/rest_v1/page/html/<title>`) is one request per page
  too; it is CDN-cached and what the robot policy prefers for HTML, but
  its Parsoid HTML does not clean to the same page: on 50 D.C. articles
  42 came out different and 11 leaked `data-mw` JSON and wikitext (the
  geohack coordinate template) into the text, because
  `clean_article_html` strips tags with a regex that a `>` inside an
  attribute defeats. Switching would need a new cleaner and a separate
  cache (the cache stores the raw parse HTML), so the fetch stays on
  `action=parse`. The parse HTML is also what the builder uses: text
  structure only online (links are unwrapped, images come from an
  offline Wikipedia ZIM).

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

The API is rate limited. `cloud/wikimedia_http.py` honours `Retry-After`
within a wait budget and never caches a 429 as a missing article; an
article still unanswered after that is left out and the build carries on
([Failure policy](#failure-policy)). From this sandbox's shared IP the
Wikimedia APIs were rate limiting on 2026-09-29, so the `full` Monaco
builds measured below got only part of their articles; that is the
sandbox's IP, not the flag.

### What CI checks

- Per push, the `docker` job builds Monaco with `--profile full` inside the
  image, as a non-root user (`--user`) and with `--network none`, from
  `tests/fixtures/monaco-full`: the extract, Overture parquets of a pinned
  release, and the Wikidata and Wikipedia caches of a live run
  (`SOURCES.txt` there says how to refresh them). So it tests the code and
  the image (DuckDB extensions in `/opt/duckdb-ext`), not the APIs, and
  takes about 20 s outside Docker. `tools/check_full_profile.py` then
  checks that `map-config.json` has `hasOvertureAddresses`, `hasWikidata`
  and `hasWikiArticles` and that `License` credits Overture and Wikipedia.
- Weekly and on demand, `live-full` builds the same with the live sources
  and runs the same check with `--soft-wikimedia`: Overture must work,
  Wikidata and Wikipedia only warn.
- `monaco-e2e` builds `--profile basic --terrain` from Geofabrik, and the Zimfarm
  schema step runs recipes for both profiles through Zimfarm's own models.

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

Satellite is outside both profiles, so a recipe adds it to either; the
recipes without it are in [Profiles](#profiles). `full` plus the freely
licensed imagery (one flag):

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with satellite imagery",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly",
 "profile": "full", "satellite": true}
```

Full, restricted: the 2021 imagery, labelled non-commercial (two flags for
the imagery; `satellite-source` implies `satellite`):

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with satellite imagery",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly",
 "profile": "full", "satellite-source": "s2cloudless-2021",
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
| Wikidata facts (population, description, Wikipedia extract) | yes | `--wikidata` existed, off; **now on in `full`**, `--wikidata=off` / `--no-wikidata` added | full: on | CC0 (Wikidata), CC BY-SA 4.0 (extracts) | query.wikidata.org SPARQL, en.wikipedia.org API | yes: network, no credentials; anonymous rate limits (backs off on 429) |
| Q-ID to Wikipedia title backfill (`--resolve-wikidata-titles`) | yes | **was missing; now part of `--wikipedia`** | full: on | CC0 | www.wikidata.org API | yes, as above |
| Wikipedia articles bundled (`--bundle-wiki-articles`) | yes, from the enwiki maxi ZIM | **was missing; now `--wikipedia`**, text from the API | full: on | CC BY-SA 4.0 (each page keeps its source link and licence) | en.wikipedia.org `action=parse` | yes, rate limited ([above](#wikipedia-articles-on-zimfarm-the-api-not-a-wikipedia-zim)) |
| Wikipedia images (`--wiki-images all`) | yes | **was missing; now `--wikipedia-zim-url` + `--wikipedia-images`** | off (no URL): the only source is a Wikipedia ZIM, 6.5 to 119 GB per task | per image, as in the Kiwix ZIM | download.kiwix.org | only with the disk and time for the download |
| Overture addresses (`--overture-addresses`) | yes | **was missing; now `--overture`** | full: on | per source, listed in the ZIM's `overture-sources.json` (CDLA-Permissive-2.0, CC0, CC BY, ODbL, ...) | overturemaps-us-west-2 S3 over HTTPS, STAC catalog | yes: anonymous HTTPS, `latest` resolved via STAC; tested here |
| Overture places: websites, phones, brands, categories (`--overture-places`) | yes | **was missing; now `--overture`** | full: on | CDLA-Permissive-2.0 | as above | yes, as above |
| Dead-website filter for Overture places (`--url-cache`) | yes | no | - | - | a crawl of every Overture website, kept on StreetZim's build host | no: the crawl result is not published anywhere a task could fetch it; without it, Overture places keep websites that may be dead |
| Terrain / hillshade / 3D terrain (`--terrain`, `--low-zoom-world-vrt`) | yes | yes: `--terrain` / `--no-terrain` | full: on | Copernicus DEM licence (free, attribution) | Copernicus DEM on AWS S3 | yes: measured on Monaco and Luxembourg ([Terrain cost](#terrain-cost)) |
| Satellite imagery (`--satellite`) | yes (the 2021 mosaic), except `satellite=no` variants | opt-in, labelled ([Satellite imagery](#satellite-imagery)) | off, in no profile | 2016 mosaic (the default source): CC BY 4.0; 2021 mosaic: CC BY-NC-SA 4.0, non-commercial | EOX Sentinel-2 cloudless | yes, about 15 s for Monaco |
| Every named POI in Kiwix's own search (`--kiwix-poi-pages`) | no | yes | full: on | ODbL | the extract | yes (+12 to 28% ZIM size, [above](#pois-in-kiwixs-own-search---kiwix-poi-pages)) |
| 3D buildings | no (the viewer has no building extrusion; "3D" is terrain) | no | - | - | - | - |
| Fonts: Open Sans, Noto Sans for Arabic, Hebrew, Armenian, Georgian, Lao, Thai; RTL shaping | yes | yes | on | Apache 2.0, OFL 1.1, BSD-2-Clause (RTL plugin) | glyphs pinned by sha256; in the Docker image, fetched otherwise | yes |
| Dark map style (Auto follows the system; in-map Auto/Light/Dark switch under Home, kept per browser; `?theme=` overrides), POI icons (Maki) | yes | yes | on | CC0 (Maki) | inlined in the viewer | yes |
| GPS, driving HUD, `#dest=` deep links | yes | yes | on | - | the viewer | yes |
| Offline PWA (`/drive/`: install, service worker, streaming from archive.org) | the website's, not in the ZIM | no | - | - | - | not applicable: Kiwix ignores in-ZIM manifests (docs/in-zim-apps.md) |
| Max-zoom variants (`cloud/region-variants.tsv`) | yes | `--max-zoom` | 14 | - | - | yes |
| Rust packer, external Xapian builder | yes (speed only; same ZIM content since 2026-10-02: before, a `--xapian=builder` ZIM's Kiwix results did not open, [above](#kiwixs-search-in-each---xapian-mode)) | no | - | - | local binaries | not needed |

All gaps that can work on Zimfarm are closed except the dead-website filter,
which has no public source. Satellite is opt-in by choice, outside both
profiles.

## What Zimfarm needs on its side

Checked against openzim/zimfarm at `917d7bc`:

1. **Docker image name.** The image is published today as
   `ghcr.io/jasontitus/streetzim` (linux/amd64; `dev` from main, `X.Y.Z`
   and `latest` from release tags; `.github/workflows/docker-publish.yml`).
   Publication uses the exact image saved after CI's full-profile Docker
   validation, checking its checksum, image ID, platform and tested commit.
   Image artifacts are retained for 14 days; rerun all CI jobs if they expire.
   Historical commits whose CI predates image export cannot be published
   through this workflow simply by rerunning their old CI definition.
   A failed-job rerun can reuse the latest successful Docker artifact from the
   same run and commit. Keep release tags immutable: only the highest stable
   Git tag gets `latest`, even if a higher tag's CI has not passed yet; older
   releases still get their version tag. Publication is serialized with
   GitHub's queued concurrency so an older CI rerun cannot evict a newer
   pending publisher.
   Zimfarm pulls `ghcr.io/<name>` and only accepts
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

These runs predate several changes, and the memory figures above are not
what the current code does. The Netherlands `basic` again on 2026-09-30,
with the `streetzim` command in its Docker image, on a 36-core machine
(before `--cpus`; the search processes held to 4 with `PYTHON_CPU_COUNT=4`,
tilemaker at every core): 83 min wall, 135 CPU minutes, a 1.13 GB ZIM, and
memory (PSS) peaking at 12.6 GB in tilemaker's 36 threads. Each of the four
`osmium extract` cuts took about 3.8 GB (5.1 GB with the Python process),
the routing graph 4.7 GB, and writing the ZIM stayed near 2 GB: the 10.8 GB
above came from code since replaced. [CPUs and memory](#cpus-and-memory---cpus)
covers what `--cpus` and cutting the extract once do to these numbers.

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
10 MB, so they are if anything upper bounds for the ZIM size. Monaco was
measured for both profiles on 2026-09-29 with `tools/measure_build.py` and
`streetzim` from this branch, outside Docker (Python 3.11), on the same
4-core, 15 GB machine, shared with other jobs (load average about 36), so
the wall times are upper bounds. Each run started with an empty `--dl`, as
a Zimfarm task does, so it includes downloading the extract (from
openstreetmap.fr: Geofabrik is blocked here), the font glyphs and, for
`full`, the Overture data; the shapefiles were given with `--shapefiles`.
Terrain had not joined `full` yet ([Terrain cost](#terrain-cost) measures
it) and satellite is in no profile, so `full` here is Wikidata, Wikipedia
articles and Overture on top of `basic`; these runs predate `--kiwix-poi-pages` joining `full`
(+28% ZIM size on Monaco, [above](#pois-in-kiwixs-own-search---kiwix-poi-pages)).
They used the monaco preset's box before `next` widened it. Disk is the
temp, output and download folders.

| Monaco | wall time | CPU time | peak memory | peak disk | ZIM | sources |
|---|---|---|---|---|---|---|
| `basic` | 1.0 min | 0.9 min | 2.1 GB (2.1) | 0.03 GB | 2.9 MB | none besides OSM |
| `full` minus terrain | 25.2 min | 1.1 min | 3.4 GB (3.4) | 0.02 GB | 3.3 MB | Overture addresses 4,087 rows, places 3,181 (36 enriched, 3,028 added); Wikidata 207 entries; titles 0/95; articles 8/53 |
| `full`, earlier the same day, before the 429 fix merged | 7.1 and 7.7 min | 1.4 min | 2.8 to 3.3 GB | 0.02 GB | 3.3 MB | articles 16/53 |

`full` spends its extra time waiting, not computing: the Overture download
(latest release resolved through STAC, 1 of 64 address files and 1 of 16
place files read) takes about 13 s. The rest is Wikimedia rate limiting
this sandbox's shared IP: with the 429 fix, each source waits
out `Retry-After` up to its budget (`STREETZIM_WIKI_WAIT_BUDGET`, 15 min),
so the 25 minutes are the title backfill (2 min, then it stops asking),
Wikidata SPARQL (6 min) and the articles (15 min, the budget, then "not
requesting the rest"). That is the worst case the budgets allow per
source, not a normal run; from an IP that is not rate limited, Monaco's
~60 requests take about a minute. The ZIMs passed
`tools/check_openzim_output.py --routing`, `cloud/validate_zim.py`
(zimcheck included) and `tools/check_full_profile.py`.

What to give a recipe (`resources` in `POST /v2/recipes`). The `basic`
rows follow from the measurements above; the `full` rows are **estimates**
from them, since only Monaco was measured with `full`. Every recipe should
also pass the flag `cpus` equal to its `cpu`
([CPUs and memory](#cpus-and-memory---cpus)); the memory figures assume it:

| extract size (example) | profile | cpu | memory | disk |
|---|---|---|---|---|
| up to about 60 MB (Monaco, Luxembourg, Rhode Island) | `basic` | 2 | 6 GiB (measured 1.9 to 4.1 GB) | 4 GiB (measured 3.5 GiB on Zimfarm for Monaco) |
| | `full` | 2 | 6 GiB (Monaco measured 2.8 to 3.4 GB) | 4 GiB |
| about 700 MB (Switzerland) | `basic` | 4 | 8 GiB (measured 4.6 GB) | 10 GiB (4 + 4.4 measured + extract) |
| | `full` | 4 | 10 GiB (estimated) | 12 GiB (estimated: Overture parquets and article cache on top) |
| about 1.4 to 1.6 GB (the Netherlands: 1.40 GB from Geofabrik, 1.63 GB from openstreetmap.fr) | `basic` | 4 | 12 GiB (measured 10.8 GB with older code; see below the first table) | 20 GiB (4 + 11.0 measured + extract; less failed with ENOSPC) |
| | `full` | 4 | 14 GiB (estimated) | 22 GiB (estimated) |

- Memory in `full` grows with the Overture merge (DuckDB, and more search
  records to write) and the Wikidata cache: Monaco's peak went from 1.9 to
  2.8 to 3.4 GB. For larger regions the added records are a larger share of the
  search step (Overture addresses are dense where national registries feed
  them, as in the Netherlands), hence the extra 2 GiB estimated.
- Time in `full` is dominated by the Wikimedia APIs: one SPARQL request per
  40 Q-IDs with a 1 s pause, and one request per article at no more than
  120 a minute (California links 11,613 articles: over an hour and a half
  before any rate limiting, an estimate), plus up to 15 minutes of rate-limit
  waiting per step (`STREETZIM_WIKI_WAIT_BUDGET`; the steps are listed under
  [Wikimedia API etiquette](#wikimedia-api-etiquette-for-periodic-recipes)). A recipe for a large region
  should allow hours on top of `basic`'s time, not minutes.
- Terrain (on in `full`) adds the Copernicus DEM download and the
  hillshade tiles ([Terrain cost](#terrain-cost): 0.3 GB for Luxembourg,
  about 0.8 GB for Switzerland), and satellite (opt-in) the imagery. Add
  their disk and time to the `full` rows.

### CPUs and memory (`--cpus`)

Zimfarm gives a task its CPUs as a CPU share (`docker run --cpu-shares`),
which hides none of the machine's cores: inside the container Python's
`os.cpu_count()` and tilemaker both see every core of the worker. Before
`--cpus`, a build started a tilemaker thread and a search process per core
it saw, so its memory grew with the machine it landed on, not with the map.
The measurements above were made on a 4-core machine and do not show it.
tilemaker alone, on a 36-core machine (peak RSS, and time with the machine
otherwise shared):

| region | 1 thread | 4 | 8 | 16 | 36 (every core) |
|---|---|---|---|---|---|
| Luxembourg | 0.70 GB, 28 s | 1.42 GB, 10.7 s | 2.48 GB, 8.3 s | 4.07 GB, 8.4 s | 7.96 GB, 10.0 s |
| Switzerland | 1.88 GB, 332 s | 2.62 GB, 102 s | 3.76 GB, 58 s | 5.95 GB, 71 s | 11.4 GB, 36 s |
| the Netherlands | | 3.78 GB, 185 s | 4.74 GB, 106 s | 7.11 GB, 69 s | 11.9 GB, 60 s |

Each thread costs about 0.25 GB whatever the region; the fixed part grows
with it. tilemaker's own detection ignores `docker run --cpus` and
`--cpuset-cpus` (it picked 36 under both), and with every core Luxembourg
was killed in a 4 GB container. At about 50 cores the Netherlands' tiles
alone would pass 16 GiB. Fewer threads cost time on a large region (the
Netherlands' tiles: 60 s with 36, 106 s with 8, 185 s with 4), minutes on
a build of an hour or more.

`streetzim/cpus.py` decides the count once: `--cpus N` when given,
otherwise the cores the process may run on, capped by a CPU quota
(`cpu.max`, as `docker run --cpus` sets) and by the memory limit
(`memory.max`, as `--memory` sets, rounded to whole GiB) at one core per
2 GiB. tilemaker's threads and the search and terrain processes use it.
libzim's compression threads (at most 20) and the tile decompression threads
leave the memory rule out: they cost about 43 MB each, and capping them costs
time (4 compression threads took 71 s where 20 took 17 s). `--cpus` and a CPU
quota do limit them: a recipe that passes `cpus` gets that many. A CPU share is
relative to the other containers and says nothing about a core count, so it
is not read. cgroup v1 limits are read when the process has no cgroup v2
hierarchy of its own (a v1 or hybrid machine).
Outside a container, with no limits, the count is every core, as before.

**A recipe should pass `--cpus` equal to its `cpu` resource** (`"cpus": 4`
next to `"resources": {"cpu": 4, ...}`), so the build uses what the task
pays for whatever machine it lands on. Without it the memory rule decides:
the recipes in the table above get 3 to 7 cores, a 16 GiB task 8.

The address, wiki-tag and routing steps also no longer cut the extract to
the area again: each did so with `osmium extract` on the file the tile step
had already cut to the same box (the result was byte-identical), and each
took about 3.8 GB, even for a 1 km box (2.3 GB): osmium's ID sets span the
planet's ID range, not the extract's. One cut remains, before the tiles.

Luxembourg `basic` in a container limited like a Zimfarm task
(`--memory 16g --cpu-shares 3072`) on the 36-core machine:

| | peak memory | wall time | CPU time |
|---|---|---|---|
| before (36 tilemaker threads, 32 to 36 search processes, 20 compression threads) | 8.4 GB | 3.0 min | 5.8 min |
| 4 cores for everything (an earlier rule: one per 4 GiB) | 4.0 GB | 3.3 min | 5.7 min |
| 4 cores, and the extract cut once | 3.9 GB | 2.5 min | 4.0 min |

Peak memory here is PSS sampled every 2 s (`tools/measure_build.py`), which
can miss a spike of a few seconds. At this size the peak is the one
remaining `osmium extract` (3.7 GB), a fixed cost of about 4 GB per build.

#### tilemaker's store (`--tilemaker-store`)

tilemaker keeps the extract's nodes, ways and relations in memory unless it
is given a store folder, where it maps them to files instead (and deletes
them when it exits). In memory, that is most of what grows with the region:
the Netherlands' tiles took 4.0 GB, and China's 6.5 GB extract would take
about 15 GB, too close to a 16 GiB task's limit with the rest of the build
alongside. tilemaker alone (v3.0.0, 4 threads, a `--memory 16g` container on
the 36-core machine; anonymous memory at its peak, sampled every second from
the container's cgroup, and wall time; 2026-10-02). The extracts are
Geofabrik's of 2026-09-30, smaller than the openstreetmap.fr ones in the
first table above (the Netherlands: 1.40 GB against 1.63 GB):

| region (extract) | memory store | disk store | store files at their largest |
|---|---|---|---|
| Luxembourg (48 MB) | 1.48 GB, 12 s | 0.46 GB, 12 s | |
| Switzerland (547 MB) | 2.74 GB, 96 s | 0.67 GB, 90 s | |
| the Netherlands (1.40 GB) | 4.02 GB, 194 s | 0.65 GB, 184 s (twice) | 2.5 GB |
| China (6.48 GB cut, in a whole `full` build, 8 threads) | | 2.85 GB, 22.3 min | at least 14 GB (one sample, mid-run) |

The disk store was no slower here (on a hard disk, with the machine's page
cache free to help), and what it saves grows with the region. It costs
disk while tilemaker runs: 1.8 times what tilemaker read for the
Netherlands, at least 2.2 times for China. The files are sparse: tilemaker
reports their full length ("Store size 66G" for China), far more than they
take. The container's
cgroup also counts the store's file pages it has touched (4.0 GB at the
Netherlands' peak), but those are page cache, which the kernel writes back
and drops under the limit rather than killing the task.

`--tilemaker-store` (on Zimfarm `tilemaker_store`) is `auto` by default:
the disk store when the file tilemaker reads (the extract cut to the area)
is over 1 GiB, or when tilemaker's estimated memory in memory mode is over
half the container's memory limit (`memory.max`); otherwise memory. The
estimate is 0.5 GB, plus 0.25 GB per thread (the table above), plus 2.2
times what it reads, which is within 15% of the three regions measured on
their own (over, for the Netherlands). The other half of the limit is for
the Python process that runs tilemaker, the page cache and the estimate's
error. The builder decides just before tilemaker runs, after the cut, and
logs the choice and why (`tilemaker store: ...`;
`streetzim/tilemaker_store.py`).

Disk is chosen only with 3 times what tilemaker reads free on the store's
filesystem: running out is fatal and unhelpful, as tilemaker maps the
files into memory and dies of SIGBUS on the first page it cannot write
(the builder then says that the store ran out of disk). Without that much
free, `auto` falls back to memory with a warning and `disk` fails before
tilemaker starts. `memory` forces memory. The store is a folder in the
build's workspace under `--tmp` (`streetzim-build-*/tilemaker-store`),
removed when the build ends, even with `--debug`, as tilemaker leaves its
files behind when it is killed. A killed build's whole workspace is removed
by a later build on the same `--tmp`, on the same running kernel, once its
lock is free and it has not changed for 6 hours, also when it is named for
the later build's own PID (in a container every run may get the same one,
1 when `streetzim` is the entrypoint).

The tiles are the same either way, as far as tilemaker's tiles are ever the
same: with 4 threads two runs of the same mode differ from each other. On
Luxembourg, decoding every tile and comparing each layer's features by
attributes and geometry (polygons unioned and normalized, so a ring's
starting point or a merge's order does not count): every feature with its
attributes is in both modes, and the points and lines (roads, POIs, places,
house numbers, labels) are identical. What differs is the outline of some
merged polygons at z14 (landcover, landuse, a few buildings and water), in
147 of 2,081 tiles between memory and disk, against 140 between two memory
runs. With 1 thread each mode is byte-for-byte repeatable, and memory
against disk differs in 143 tiles, the same polygon layers: the store
changes the order in which tilemaker assembles multipolygons, not what it
reads.

#### The routing graph

The routing step (`streetzim/routing/build.py`, `extract_routing_graph`)
used to be what a large region could not fit. China's cut (6.48 GB, 870 M
nodes, 26.1 M highway ways, 43.6 M junctions) in a `full` build in a
`--memory 16g` container reached Pass 2 with 12.6 GB of anonymous memory
and thrashed there for hours (CPU 8%, 464 GB read, 3.6 M of 26.1 M ways
done): a Python dict of every junction id (about 4-5 GB), a dict of every
geometry's bytes for the dedup (about 85 bytes each), Pass 1's set of way
ends, and a location index of every node in the cut, a 13.9 GB file read at
random through the 3 GB of page cache left. Everything before it had
peaked at 7.3 GB.

For a large extract the builder now first filters it to the highway ways
and their nodes (`osmium tags-filter`; China: 2.47 GB, 333 M nodes). The
filter is a process of its own whose ID sets span the planet's node-ID
range, so it costs 1.4 to 2.2 GB of anonymous memory whatever the extract's
size (1.45 GB for Monaco's 1 MB fixture, 2.1 GB for Luxembourg, 2.2 GB for
the Netherlands). A small extract is therefore read whole, with a file
index of every node in it, as before: auto filters only when that index,
estimated at 2.2 bytes per byte of PBF (China: 13.9 GB for 6.48 GB), would
pass 3 GiB or a quarter of the memory limit (`memory.max`); so of the
regions below only China is filtered (the Netherlands' 1.40 GB, an
estimated 3.1 GB, measured 1.9 GB, is read whole).
`STREETZIM_ROUTING_HIGHWAY_FILTER=on|off` forces either, and the log says
which and why.

Either way the builder keeps junction ids in a sorted array, spills way
refs and the encoded geometries to scratch files beside the graph and
deduplicates the geometries afterwards (by length and a 64-bit hash, every
match confirmed byte for byte), and writes the edges a block at a time.
With the filter, the node-location index is in memory up to an estimated
1 GiB (three times 16 bytes per highway node, for the vector's growth) or
a tenth of the memory limit, else in a file; without it, always in a file.
The file goes in `STREETZIM_NODE_LOC_DIR` when that is a writable folder,
else the build's temporary folder. Its default, `/data`, is not writable
on the current build host (a root-owned folder on the root filesystem)
and does not exist in the image, so both use the build's folder.
`STREETZIM_ROUTING_NODE_INDEX=memory|file` forces either, and the log
says which and why (`Node locations ...`). The graph is byte-identical to
the old builder's (Monaco, Luxembourg, Switzerland, the Netherlands,
China; random networks in `tests/test_routing_build_memory.py` against
the old builder, with and without the filter).

The routing step alone (`extract_routing_graph` on the cut extract,
2026-10-03, on the shared 36-core machine). Peak memory is of the whole
process tree, the osmium filter included, sampled every 50 ms: anonymous
memory, then RSS, which also counts a file index's mapped pages (page
cache, which the kernel can drop):

| region (extract) | before | after (auto) | after, filter forced on |
|---|---|---|---|
| Monaco fixture (1.1 MB) | 0.04 / 0.06 GB, 0.8 s | 0.03 / 0.06 GB, 0.6 s | 1.46 / 1.48 GB, 1.2 s |
| Luxembourg (47 MB) | 0.27 / 0.35 GB, 12 s | 0.19 / 0.27 GB, 15 s | 2.11 / 2.13 GB, 14 s |
| Switzerland (547 MB) | 1.84 / 2.56 GB, 192 s | 1.13 / 1.67 GB, 234 s | 2.18 / 2.20 GB, 211 s |
| the Netherlands (1.40 GB) | 2.04 / 3.99 GB, 291 s | 1.06 / 3.00 GB, 323 s | 2.22 / 2.25 GB, 254 s |
| China (6.48 GB cut), `--memory 16g` container | did not finish (thrashed in Pass 2) | 8.6 / 10.4 GB, 49 min (filtered) | |
| China, outside a container (no limit) | 23.4 / 33.6 GB, 48 min | | |

These are isolated routing-stage measurements, not full-build limits. A
2026-10-04 full China run at `29ec7da` with a production-seeded shared
Wikidata cache peaked at **15.08 GB container anonymous memory** in spatial
conversion. Loading all 3.34 million cached facts retained about 3.8 GB
unnecessarily beside the routing child; China requested only 198,840 IDs.
Cold-cache and shared-cache runs therefore were not memory-equivalent.
The [October memory follow-up](build-review.md#china-memory-follow-up--2026-10-04)
records regional fact selection, mapped spatial input, measured synthetic
reductions and the remaining need for a full-country rerun. Its successful
9 GiB spatial capacity test is not a measured full-China result.

The isolated China routing-stage peak is after Pass 2, deduplicating 59.8 M geometry
candidates and sorting 105 M edges on top of 2.5 GB of edge columns; its
filter ran first, alone, at about 2.4 GB (the container's anonymous
memory, sampled every 10 s). The run sat CPU-bound throughout (one core,
about 104%, reads under 1 MB/s in Pass 2, no memory pressure); the
container's total memory reached its limit only with page cache (the
highway extract, the 5.0 GB index file and the geometry spill), which the
kernel dropped as needed. Wall times are on a shared machine, within
about 20% run to run. Disk: the highway extract (0.38 times the cut for
China), the index file (16 bytes per node indexed) and the geometry spill
(2.2 GB for China), all removed when the step ends. If the build is
killed instead, they stay in create_osm_zim's temporary folder
(`osm_zim_*`), which `streetzim` keeps in its workspace under `--tmp`
(`streetzim-build-*`), so a later build sweeps it with the workspace.

### Terrain cost

Terrain is on with `--profile full`, the default: hillshade and 3D are
part of the StreetZim experience, and the Copernicus DEM is free and open (attribution only;
`License` and the viewer's credits name it whenever a ZIM has terrain).
`--no-terrain` (on Zimfarm: `terrain` off) or `--profile basic` leaves it
out. The numbers below are what it costs.

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
- tiles start just below the lowest zoom the viewer can show. The viewer
  keeps the view inside the area's box (maxBounds, no margin), so on a
  320-px screen Monaco never shows below map zoom 11.7 and Luxembourg below
  8.1. Terrain starts two levels lower (z9 and z6): MapLibre's 3D terrain
  reads DEM tiles one level below the ones it draws (its deltaZoom of 1),
  and the far side of a tilted view is drawn from coarser tiles still. A
  320x440 view of Monaco, fully zoomed out and tilted, reads real elevation
  there. map-config.json says where terrain starts (`terrainMinZoom`) and
  the viewer asks for nothing lower;
- every tile is filled over its whole square: z10 and above from GLO-30
  (30 m), z9 and below from a 90 m mosaic of GLO-30 and GLO-90 cells. At
  z9 a 256-px tile has 10-arc-second pixels, so GLO-90 loses nothing, and a
  GLO-90 cell is about an eighth of the size of a GLO-30 one;
- GLO-30 is fetched only for the cells under the area's z10 tiles;
- for a very large area the low-zoom mosaic is capped: it may span at most
  64 one-degree cells, or three times the cells under the area's z10 tiles
  if that is more, counting every cell the tile squares cover (sea cells,
  which cost a 404, and cells shared with GLO-30 included). Past the cap it
  covers the full squares of the lowest zoom that fits, and the zooms below
  that are filled over that part and are 0 m beyond it, far outside the
  area;
- the health check has two parts that do not trust each other. Every
  one-degree cell under every tile's square must be on disk or known sea
  (a 404 from every source); it asks the disk, not the plan. And every
  tile at z9 and below, and every tile on the edge of the area, is compared
  with the mosaic for its zoom at 25 points: a tile reading 0 m where the
  DEM is more than 15 m from 0 (land, or ground below sea level) fails the
  build, whatever its file size. Interior tiles above z9 keep the size-based
  blank check. Missing tiles are made again first. Sea reads 0 m in both
  and passes; no `TERRAIN_BLANK_TOLERATE` is needed;
- a DEM download that fails for any reason but a 404 stops the build at
  that cell, requests time out after 30 s, and all downloads together
  have a 30-minute budget (`TERRAIN_DOWNLOAD_BUDGET_S`); the error names
  `--no-terrain` (on Zimfarm, `terrain` set to `off`).

Builds given a world DEM (production) keep their layout and check; a
`--low-zoom-world-vrt` that is not a file is refused, and the production
wrappers stop when theirs is missing.

**Measured** on 2026-09-29 with `tools/measure_build.py`, through the
`streetzim` command, each run with an empty `--dl` so the DEM was
downloaded; extracts as `file://` URLs (Monaco from openstreetmap.fr, the
preset box 7.39,43.715,7.46,43.765; Luxembourg the same extract as above),
4-core 15 GB machine shared with other jobs (1-minute load about 5 to 10),
so wall times are upper bounds and even CPU time varies by a few percent
between runs (Luxembourg's build with terrain used less CPU than the one
without). Peak disk counts `--dl`'s cache (where the DEM goes) as well as
the temp and output folders.

| region | terrain | wall time | CPU time | terrain phase | peak memory | peak disk | DEM downloaded | ZIM |
|---|---|---|---|---|---|---|---|---|
| Monaco | off | 42 s | 79 s | | 3.09 GB | 0.01 GB | | 2,933,725 B |
| Monaco | on | 61 s | 99 s | 3.3 s | 2.98 GB | 0.03 GB | 17.8 MB (1 GLO-30 + 1 GLO-90) | 3,002,043 B (+68 kB: 4 tiles, z9-z12) |
| Luxembourg | off | 3.0 min | 5.6 min | | 3.98 GB | 0.35 GB | | 56.7 MB |
| Luxembourg | on | 4.5 min | 5.5 min | 66 s | 3.99 GB | 0.66 GB | 283 MB (4 GLO-30 + 31 GLO-90) | 59.6 MB (+2.85 MB, +5%: 208 tiles, z6-z12, 2.82 MB of entries) |

Luxembourg's terrain phase alone, with the DEM already on disk, takes about
12 s (tiles, then the health check), 0.2 CPU minutes and 0.6 GB of memory
(measured with the z7 start used before the min zoom moved one level
lower; one tile more now); the rest of the 66 s was the download, about
5 MB/s through this machine's proxy. The build's peak memory is unchanged,
as terrain is not the step where it peaks. Before this change the same two
areas fetched 178 MB (the old Monaco box, bbox + 1 degree: 9 cells) and
608 MB (Luxembourg, 16).

**Switzerland and the Netherlands**, extrapolated, not built:
- DEM bytes are exact: the cells each area's plan needs, sized with an HTTP
  HEAD on the S3 objects (a 404 is sea);
- tile counts are exact (the plan's zooms over the area);
- ZIM bytes and CPU time per tile come from making every z10-z12 tile of
  sample GLO-30 cells: Alps (N46E007) and Plateau (N47E008) for
  Switzerland, inland (N52E005) and coast (N53E006) for the Netherlands.
  Luxembourg's own cell (N49E006) gave 10.8 kB and 36 ms per z12 tile,
  against 13.5 kB per tile over all zooms in its ZIM;
- download time at 25 MB/s, the rate [Disk for a Zimfarm recipe](#disk-for-a-zimfarm-recipe)
  assumes for the shapefiles.

| region | zooms | DEM to download | tiles | ZIM bytes | CPU | download | vs. the build (tables above) |
|---|---|---|---|---|---|---|---|
| Switzerland | z4-z12 | 815 MB (18 GLO-30 + 10 GLO-90 cells) | 2,535 | about 45 MB | about 2 min | about 35 s | ZIM +7%, disk +0.9 GB, time +1-2% |
| Netherlands | z3-z12 | 598 MB (20 GLO-30 + 20 GLO-90 land cells) | 3,896 | about 7 MB (flat; sea tiles are aliased) | about 1.5 min | about 25 s | ZIM +0.6%, disk +0.6 GB, time +1% |

Both are capped: the low-zoom mosaic spans the z6 squares for Switzerland
(28 cells; the z5 squares would be 88, over its budget of 64) and the z7
squares for the Netherlands (49 cells; budget 72). Their z4-z5 (z3-z6)
tiles are filled over those squares and are 0 m beyond them: far outside
the country, in the part of a tilted view nearest the horizon. Luxembourg is
not capped: its z6 squares are 35 cells, within the minimum budget of 64,
which is why its GLO-90 share is larger than Switzerland's.
Before this change Switzerland would have fetched 1.71 GB and the
Netherlands 0.95 GB.

So terrain costs a Zimfarm task a DEM download about the size of the OSM
extract (1.2 times it for Switzerland, 0.4 times for the Netherlands, 5
times for Luxembourg, whose extract is small), under a minute of CPU for
countries of this size, and a few percent of the ZIM at most (hilly
areas); peak memory does not change. Add the DEM to the recipe's disk.

A recipe that leaves terrain out of `full` sets **terrain** to `off`,
which Zimfarm passes as `--terrain=off`:

```json
{"offliner_id": "streetzim", "name": "osm_en_luxembourg", "title": "Luxembourg",
 "description": "Offline map of Luxembourg with search and routing",
 "include-poly": "https://download.geofabrik.de/europe/luxembourg.poly",
 "profile": "full", "terrain": "off"}
```

### Downloads per task

A fresh Zimfarm container downloads, besides the OSM extract:
- the coastline and Natural Earth shapefiles when tiles are built with
  tilemaker (see below). Building from `--mbtiles-url` avoids this;
- nothing for the viewer: MapLibre GL JS is vendored and the Docker image
  carries the pinned font glyphs ([viewer-supply-chain.md](viewer-supply-chain.md));
- with `--profile full` (or the flags): Overture's addresses and places for
  the box (DuckDB reads only the files whose STAC bbox meets it, and only
  the row groups inside it: 0.1 and 0.3 MB for Monaco), Wikidata facts by
  SPARQL, and one Wikipedia API request per article. The DuckDB `spatial`
  and `httpfs` extensions are installed in the image; outside it, DuckDB
  fetches them (about 100 MB) from extensions.duckdb.org on first use;
- with `--wikipedia-zim-url`, the whole Wikipedia ZIM;
- with terrain (`--profile full`, or `--terrain`): Copernicus DEM tiles
  for the area, from the public `copernicus-dem-30m` and
  `copernicus-dem-90m` buckets on AWS S3, over HTTPS with no credentials,
  into `--dl` (on Zimfarm the default `/tmp/streetzim/dl`, never the
  output folder). 17.8 MB for Monaco, 283 MB for Luxembourg; see
  [Terrain cost](#terrain-cost).

### Shared caches: ownership and permissions

A Zimfarm task's `--dl` is its own, so none of this applies there. It
applies when several builds, or several users, share one cache: the
production host's Wikidata cache (`wikidata_cache/`, or
`--wikidata-cache`), or a persistent `--dl` reused between runs.

How the Wikidata cache is written (`wikidata_cache.py`,
`streetzim/cache_permissions.py`):
- one writer at a time, under `manifest.json.lock`; a build that fetched
  nothing new reads without the lock;
- each bucket file (`NN.json`) and `manifest.json` is written to a
  staging file beside it and renamed over the old one, so a reader never
  sees half a file and a killed build leaves the old file whole;
- the replacement keeps the old file's mode, and its group and ACL as
  far as the writing user can, so whoever could read or write it before
  still can. Its owner becomes the writing user unless that is the old
  owner (or root, who keeps the old owner).

**When a build stops instead.** Keeping a file's group needs the
building user to be in that group. If it is not, and the file's group
access differs from everyone else's (for example mode `664` with group
`staff`: the group may write, others may not), a replacement would hand
the file to the builder's own group and change who may write it. The
build stops with an error naming the file, its group and mode, the
builder's user and groups, and these fixes, any of:
- run the build as a user in that group. In Docker, `--user uid:gid`
  gives the process no other groups; add the cache's group with
  `--group-add <gid>` (the error gives the number);
- give the cache files a group the builder is in (`chgrp`), or make the
  group's access the same as everyone else's (`chmod`), when that is
  what you want;
- give the build a cache of its own (`--wikidata-cache DIR`, or `--dl`
  for the `streetzim` command).

With mode `644` (group and others alike) a different group changes
nothing, so the file is replaced, with the builder's group.

A file with an ACL keeps it only with the same owner, which only that
user (or root) can give the replacement, so another user's build stops
too. Its error gives these fixes instead: run the build as the file's
owner (or as root), remove the ACL if it is not wanted (`setfacl -b`),
or give the build a cache of its own.

**Other failures are warnings.** A bucket that cannot be read is
reported and left alone. If the build does not add to it, that is all:
only the manifest's totals count it, and nothing builds from those. If
the build adds to it, the build stops when it saves, saying why: merging
into a file it cannot read would drop every other region's entries in
it. The warning says what to do: a bucket that is not valid JSON (cut
short) can be deleted, and its Q-IDs are fetched again by the builds
that use them; one this user may not read is checked for owner and mode
instead, since it may be another user's valid bucket.

**What killed builds leave.** A build killed while publishing leaves a
staging file (`.NN.json.<id>.tmp`, `.manifest.json.<id>.tmp`) or a
private staging folder holding one. The next build that finds any takes
the lock and removes them (staging only happens under the lock, so none
is a live writer's), as does every save. At the start it only tries
the lock: if another build holds it, that build cleans up when it saves,
so nothing waits. A lock's own staging file
(`.manifest.json.lock.<id>.tmp`) is made before the lock exists, so it
is removed only once it is an hour old. Only names this code makes are
removed. One this user cannot remove, or a cache it cannot lock
(read-only), is a warning.

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
