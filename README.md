# StreetZim — offline OpenStreetMap in a ZIM file

[![CI](https://github.com/jasontitus/streetzim/actions/workflows/ci.yml/badge.svg)](https://github.com/jasontitus/streetzim/actions/workflows/ci.yml)

StreetZim packages OpenStreetMap data into a single [ZIM](https://wiki.openzim.org/wiki/ZIM_file_format)
file that opens in [Kiwix](https://kiwix.org) (iOS, Android, desktop) or in a
browser, with no network at all. A ZIM contains:

- **Vector map** rendered on the device by MapLibre GL JS (OpenMapTiles schema, z0–14, overzoomed beyond).
- **Search** over places, streets, addresses, POIs, peaks, parks and water, plus Kiwix's own title/full-text search and one detail page per feature.
- **Find page** with category chips (Food & Drink, Bars, Hotels, Museums, Parks, Health, Shops, Gas…) and distance sorting.
- **Offline routing** (drive / walk / bike) in a Web Worker, with a GPS turn-by-turn HUD.
- Optional **terrain** (hillshade / 3D from Copernicus DEM), **satellite imagery** (see the licence note below), **Wikidata** facts and bundled **Wikipedia** articles.
- Optional **Overture Maps** addresses and place details (websites, phones, brands).

Published ZIMs are listed at <https://streetzim.web.app>. That site also has a
PWA at `/drive/` that opens a local ZIM, or streams one from archive.org, in
the browser.

## Quick start (build Monaco in about a minute)

CI runs these steps on every push (`.github/workflows/ci.yml`, job
`monaco-e2e`, which also adds the production routing and chip flags), so
they are known to work on Ubuntu 24.04.

**1. System tools.** You need Python ≥ 3.10, `osmium-tool`, and
**tilemaker 3.x**. Ubuntu's `apt install tilemaker` (2.4) is too old for
`resources/tilemaker/process-openmaptiles.lua`, so build it from source:

```bash
sudo apt install osmium-tool unzip build-essential cmake libboost-dev \
  libboost-filesystem-dev libboost-iostreams-dev libboost-program-options-dev \
  libboost-system-dev liblua5.1-0-dev libshp-dev libsqlite3-dev rapidjson-dev zlib1g-dev
git clone --depth 1 --branch v3.0.0 https://github.com/systemed/tilemaker.git /tmp/tilemaker
cmake -S /tmp/tilemaker -B /tmp/tilemaker/build -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/tilemaker/build -j"$(nproc)" && sudo cp /tmp/tilemaker/build/tilemaker /usr/local/bin/
```

On macOS: `brew install osmium-tool`, then build tilemaker the same way
(`brew install boost lua rapidjson shapelib sqlite cmake` for its
dependencies) or use Homebrew's `tilemaker` if it is 3.0 or newer.

**2. Python environment.**

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pulls stock `libzim` (python-libzim), which includes the
libzim fixes StreetZim contributed upstream; the old patches in `patches/`
are kept for history only.

**3. Coastline and Natural Earth shapefiles.** tilemaker reads them relative to
the directory you build in. Without them the build still succeeds but has no
ocean, and `create_osm_zim.py` prints a warning.

```bash
scripts/fetch-shapefiles.sh        # ~900 MB download, once
```

**4. Build, validate, view.**

```bash
python create_osm_zim.py --area monaco --routing -o osm-monaco.zim
python cloud/validate_zim.py osm-monaco.zim     # the pre-upload gate
kiwix-serve --port 8888 osm-monaco.zim          # from kiwix-tools; or:
python cloud/serve_zim_entries.py osm-monaco.zim --port 8888
```

**StreetZim uses the standard openZIM tools.** It writes ZIMs with
[python-libzim](https://github.com/openzim/python-libzim) (libzim), checks
them with `zimcheck`, and serves them with `kiwix-serve`. Nothing else is
needed. On every push, CI builds, rewrites and serves a ZIM with exactly
this stack.

`cloud/validate_zim.py` also runs `zimcheck` if it is on your PATH. Use
zim-tools 3.8 or newer from <https://download.openzim.org/release/zim-tools/>;
Ubuntu's 3.2 misreads ZIMs written by current libzim.

### Or with Docker

The image contains tilemaker 3, osmium and the Python stack; CI builds it
and makes a ZIM inside it on every push.

```bash
docker build -t streetzim .
mkdir -p out
docker run --rm -v "$PWD/out:/output" streetzim scripts/fetch-shapefiles.sh /output   # once
docker run --rm -v "$PWD/out:/output" streetzim \
    create_osm_zim.py --area monaco --routing -o /output/osm-monaco.zim
```

### Or the openZIM way: `streetzim`

`streetzim` is the same builder behind openZIM's scraper conventions:
maps2zim's flag names, ZIM metadata flags checked before any download,
`{name}_{period}.zim` in an output folder, and a Zimfarm progress file. Its
flags are described for Zimfarm in `offliner-definition.json`;
[docs/zimfarm.md](docs/zimfarm.md) covers the default profile and what a
build costs.

```bash
pip install -e .              # or use the Docker image, which has it
streetzim --name osm_en_monaco --title Monaco \
    --description "Offline map of Monaco with search and routing" \
    --area monaco --output out --stats-filename out/task_progress.json
```

## Choosing what goes in

Input, one of:

| flag | input |
|---|---|
| `--area NAME` | a preset: `monaco`, `liechtenstein`, `dc`, `manhattan`, `san-francisco`, `austin`, `portland`, `virginia`, `colorado`, `california`, `iran`, `united-states` |
| `--geofabrik europe/liechtenstein --name …` | any [Geofabrik](https://download.geofabrik.de/) extract |
| `--pbf file.osm.pbf --name …` | a local PBF; add `--bbox minlon,minlat,maxlon,maxlat` to cut it |
| `--mbtiles tiles.mbtiles` | skip tilemaker and reuse existing tiles (with `--pbf` for search/routing) |

Main feature flags (all off by default; `python create_osm_zim.py --help` lists all ~50):

| flag | adds | needs |
|---|---|---|
| `--routing` | routing graph (SZRG v4) | — |
| `--spatial-chunk-scale 10` | routing split into 0.1° cells loaded on demand (what production ships; needed for large regions on phones) | `--routing` |
| `--split-find-chips` | one file per Find chip instead of the whole POI list | — |
| `--terrain` | hillshade / 3D terrain tiles to z12 | GDAL (via `rasterio`), network to AWS S3 |
| `--satellite` | Sentinel-2 imagery, **non-commercial licence** | network to EOX |
| `--wikidata` | population, descriptions, Wikipedia extracts | network to Wikidata/Wikipedia |
| `--bundle-wiki-articles --wiki-articles-source enwiki.zim` | full Wikipedia articles, read from a local Wikipedia ZIM | a Wikipedia ZIM |
| `--overture-addresses/--overture-places PARQUET` | Overture data, from `download_overture_data.py` | DuckDB, network to S3 |

Size and memory: a city builds in minutes on a laptop. Country and continent
builds need tens to ~100 GB of RAM and are run with the production wrappers
described in [MAINTAINING.md](MAINTAINING.md).

## How it works

```
Geofabrik / planet PBF
   │  tilemaker (OpenMapTiles profile, resources/tilemaker/)
   ▼
MBTiles ──► tiles/{z}/{x}/{y}.pbf ──┐
PBF ──► search records, addresses ──┤  (+ Overture, Wikidata, terrain, satellite)
PBF ──► routing graph (pyosmium) ───┤
resources/viewer/ (MapLibre app) ───┤
                                    ▼
                         create_osm_zim.py → osm-<name>-<date>.zim
                                    │
             Kiwix / the /drive/ PWA serve entries to the viewer, which renders
             tiles, searches the sharded JSON and routes in a Web Worker.
```

- Every byte format and ZIM path is specified in [docs/formats.md](docs/formats.md); search records and category files in [docs/search-records.md](docs/search-records.md).
- Tile sources: StreetZim's tilemaker tiles give +31% street names and about 2x named places compared with OpenFreeMap's ([docs/tile-sources.md](docs/tile-sources.md)).
- Working with openzim/maps: StreetZim also builds from the OpenFreeMap tiles maps2zim uses (`scripts/fetch-openfreemap-mbtiles.py`, then `--mbtiles`); [docs/openzim-integration.md](docs/openzim-integration.md) is the plan for porting features into maps2zim.
- Routing internals: [docs/routing.md](docs/routing.md). Search sharding: [docs/search-prefix-locality.md](docs/search-prefix-locality.md).
- Kiwix reader quirks the design works around (service-worker request drops, cluster size limits, zstd windows): [docs/zim-packaging-gotchas.md](docs/zim-packaging-gotchas.md).
- The in-ZIM apps (`places.html`, detail pages, the `#dest=` deep-link protocol): [docs/in-zim-apps.md](docs/in-zim-apps.md).

## Repository map

| path | what |
|---|---|
| `create_osm_zim.py` | the builder's command line |
| `streetzim/cli.py` | `streetzim`, the openZIM-style command (maps2zim's flags; `offliner-definition.json`) |
| `resources/viewer/` | the viewer shipped inside every ZIM (`index.html`, built from `src/index/` by `tools/build_viewer.py`; `places.html`; `routing-worker.js`) |
| `resources/vendor/`, `resources/viewer-assets.lock.json` | vendored MapLibre GL JS and the sha256 pins for it and the font glyphs (`tools/pin_viewer_assets.py`, [docs/viewer-supply-chain.md](docs/viewer-supply-chain.md)) |
| `resources/tilemaker/` | tilemaker config and Lua profile |
| `cloud/` | Python modules the builder imports (`chip_rules`, `search_shards`, `viewer_slots`, …) and the ZIM tools (`validate_zim`, `repackage_zim`, `patch_viewer_inplace`, …) |
| `streetzim/` | the builder's modules: `tiles`, `terrain`, `satellite`, `addresses`, `search_extract`, `zim_writer` (one function per ZIM phase), `zim_metadata` (openZIM metadata rules), `progress` (Zimfarm progress file), `common`, and `routing/` (graph build, formats, reference routers) |
| `tests/` | pytest and Node tests |
| `web/` | streetzim.web.app catalogue and the `/drive/` PWA (operations; moves to `ops/` in stage 2) |
| `preview-proxy/` | archive.org range proxy for online previews |
| `ops/` | StreetZim's own operations (build host, uploads, rollouts); not needed to build a ZIM. The old paths are symlinks into it for now: [ops/README.md](ops/README.md) |

[MAINTAINING.md](MAINTAINING.md) explains which parts are core and which are
the author's hosting/operations tooling, how releases are built and shipped,
and the known technical debt.

## Licence

**Code:** MIT (see [LICENSE](LICENSE)).

**Data in the ZIM files:**

| source | licence | attribution |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | ODbL 1.0 | © OpenStreetMap contributors |
| [OpenMapTiles](https://openmaptiles.org/) schema | CC BY 4.0 | © OpenMapTiles |
| OSM [water polygons](https://osmdata.openstreetmap.de/data/water-polygons.html) | ODbL 1.0 | © OpenStreetMap contributors |
| [Natural Earth](https://www.naturalearthdata.com/) | public domain | — |
| [Copernicus GLO-30 / GLO-90 DEM](https://dataspace.copernicus.eu) (`--terrain`) | Copernicus free & open | © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the EU and ESA |
| [Sentinel-2 cloudless 2021](https://s2maps.eu) by EOX (`--satellite`) | **CC BY-NC-SA 4.0** | Sentinel-2 cloudless by EOX (contains modified Copernicus Sentinel data 2021) |
| [Overture Maps](https://overturemaps.org/) (`--overture-*`) | CDLA-Permissive-2.0 and others per source; written to `overture-sources.json` | per source |
| [Wikidata](https://www.wikidata.org/) | CC0 | — |
| [Wikipedia](https://en.wikipedia.org/) text | CC BY-SA 4.0 | Wikipedia contributors |

**Bundled software:** [MapLibre GL JS](https://maplibre.org/) (BSD-3-Clause);
[mapbox-gl-rtl-text](https://github.com/mapbox/mapbox-gl-rtl-text) 0.3.0
(BSD-2-Clause, with ICU under the Unicode licence; vendored in
`resources/vendor/mapbox-gl-rtl-text/`, shaping Arabic/Hebrew labels);
[Maki](https://github.com/mapbox/maki) 8.2.0 POI icons (CC0 1.0, inlined in the viewer);
label glyphs from [Open Sans](https://github.com/openmaptiles/fonts) (Apache 2.0) and,
for Arabic, Armenian, Georgian, Hebrew, Lao and Thai labels when a map has them,
[Noto Sans](https://github.com/protomaps/basemaps-assets) (SIL Open Font License 1.1,
shipped in the ZIM as `fonts/NotoSans/OFL.txt`; pinned in
`resources/viewer-assets.lock.json`, licence in `resources/vendor/noto-sans/`).

> **Satellite imagery is non-commercial.** The Sentinel-2 cloudless 2021 layer
> is CC BY-NC-SA 4.0. `create_osm_zim.py` only includes it with `--satellite`,
> but the production wrappers pass that flag, so most **published** StreetZim
> ZIMs contain it and may only be redistributed non-commercially. Build without
> `--satellite` (or use a variant with `satellite=no` in
> `cloud/region-variants.tsv`) for a ZIM with no non-commercial data.

## Optional accelerators for very large builds

The author's production host builds whole continents. It uses two optional
speed-ups, both switched off by default:

- `--zim-builder rust` swaps libzim's writer for a separate packer.
- `--xapian builder` uses an external indexer.

Both produce ordinary ZIMs, the same as libzim's. Neither is needed for
anything in this README. [docs/zim-builder-rust.md](docs/zim-builder-rust.md)
explains when they help.

## Legacy: raster (Leaflet) variant

`create_osm_zim_leaflet.py` is the original experiment that renders the same
vector tiles to PNG with Pillow and shows them with Leaflet, for readers
without WebGL. It is not used by any build, has far fewer features, and is
kept only for comparison.
