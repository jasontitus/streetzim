# StreetZIM - Offline OSM Maps in ZIM Files

A prototype tool that packages OpenStreetMap data into ZIM files for offline
viewing in the [Kiwix](https://kiwix.org) app (iOS, Android, desktop). Two
approaches are provided for comparison:

1. **Vector tiles + MapLibre GL JS** — small files, client-side rendering
2. **Raster tiles + Leaflet** — larger files, pre-rendered, wider compatibility

## The Idea

**Problem:** Offline map apps typically store pre-rendered raster tiles, which
are enormous. A full planet render at z0-18 is ~54 TB. Even a single city at
useful zoom levels can be multiple gigabytes.

**Solution:** Store compact vector tiles in a ZIM file and render them on-the-fly
in the browser using MapLibre GL JS. This gives you:

- **50-200x smaller** files compared to full raster tiles
- **Arbitrary resolution** — vector tiles scale perfectly to any display density
- **Runs in Kiwix** — the world's most popular offline content reader
- **No server needed** — everything renders client-side in JavaScript

A Leaflet/raster alternative is also provided for comparison and for environments
where WebGL (required by MapLibre) is not available.

## Size Comparisons

### Vector (MapLibre GL JS) vs Raster (Leaflet) ZIM Files

| Area | Vector ZIM | Raster ZIM | Ratio |
|------|-----------|-----------|-------|
| Monaco | 778 KB | 355 KB | 2.2x |
| Washington, D.C. | 7.8 MB | 6.9 MB | 1.1x |

**Why are the raster ZIMs still smaller?** See the [resolution comparison](#raster-vs-vector-tile-resolution) section below.

### Vector ZIM vs Full Raster (production-quality, all zoom levels)

| Area | OSM PBF | Vector Tiles | Vector ZIM | Full Raster z0-18 (est.) |
|------|---------|--------------|----------|---------------------|
| Monaco | 0.6 MB | 0.3 MB | 0.7 MB | ~50 MB |
| Washington, D.C. | 19.6 MB | 8.6 MB | 17.4 MB | ~2-5 GB |
| US State (typical) | 200-600 MB | 50-200 MB | 60-250 MB | ~50-200 GB |
| Entire Planet | ~85 GB | ~80-120 GB | ~100-150 GB | ~54 TB |

The vector ZIM file includes MapLibre GL JS (~800 KB) and font glyphs, so it's
slightly larger than raw vector tiles, but dramatically smaller than
production-quality raster.

## Raster vs Vector Tile Resolution

**The raster prototype ZIMs are currently smaller than the vector ZIMs, but this
is misleading.** Here's why:

### Why the raster ZIMs appear smaller

1. **Lighter JS library and simpler fonts.** The vector ZIM bundles MapLibre GL JS
   (~800 KB), CSS, and SDF font glyph PBFs for text rendering. The raster ZIM
   bundles Leaflet (~150 KB) and pre-renders labels into the tile images using
   system fonts — no font data needs to be shipped.

2. **Simplified rendering.** Our Python-based raster renderer (Pillow +
   mapbox-vector-tile) draws basic shapes with flat colors — no anti-aliasing,
   no road casings, no icons, no patterns. A production raster renderer (Mapnik,
   TileMill) would produce much larger, higher-quality PNGs.

3. **Same zoom levels, same tile count.** Both approaches generate tiles at
   z0-14 from the same vector tile data. The raster tiles are 256x256 PNG images
   rendered from those vector tiles. Since the rendering is so simple, the PNGs
   compress well.

4. **PNG compression favors simple images.** Our flat-color, label-free tiles
   compress to very small PNGs. Production raster tiles with full cartography
   (text, gradients, anti-aliasing) would be 10-50x larger per tile.

### What production raster tiles would look like

A production raster tile pipeline (like the official openzim/maps project or
OpenStreetMap's tile servers) would:

- Render at **256x256 or 512x512 pixels** per tile with full anti-aliasing
- Include **text labels** for streets, places, water features, POIs
- Render **icons and symbols** (hospital, parking, bus stop, etc.)
- Use **complex cartographic styling** (road casings, halos, gradients)
- Typically cover **z0-18** (vs our z0-14), adding 16x more tiles per extra zoom level
- Result in files that are **50-200x larger** than the vector approach

### The real tradeoff

| Aspect | Vector (MapLibre) | Raster (Leaflet) |
|--------|------------------|-----------------|
| **File size** | Small (compact PBF data) | Large (pre-rendered pixels) |
| **Resolution** | Infinite (vector scales to any DPI) | Fixed (256px per tile) |
| **Retina displays** | Perfect — renders at device DPI | Blurry on 2x/3x screens |
| **Text labels** | Yes (SDF font rendering) | Only if pre-rendered |
| **Interactivity** | Can query features, hover, click | Static image only |
| **Client requirements** | WebGL required | Basic JS only |
| **Rendering cost** | GPU-intensive on client | Pre-computed, fast to display |
| **Style flexibility** | Can change colors/styles at runtime | Baked into the image |

**Bottom line:** Vector tiles are the superior approach for quality and file size.
Raster tiles win only on compatibility (no WebGL needed) and rendering
simplicity (no client-side GPU work). In a production comparison at equivalent
quality and zoom levels, vector ZIMs would be 50-200x smaller than raster.

## How It Works

### Architecture

```
┌─────────────────────────────────────────────────┐
│                   ZIM File                       │
│                                                  │
│  index.html ─── MapLibre GL JS map viewer        │
│  maplibre-gl.js ─── Client-side renderer         │
│  maplibre-gl.css ─── Map UI styles               │
│  map-config.json ─── Center, zoom, bounds        │
│  tiles/{z}/{x}/{y}.pbf ─── Vector tiles (MVT)    │
│  fonts/{name}/{range}.pbf ─── SDF font glyphs    │
│                                                  │
└─────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│              Kiwix App (iOS/Android/Desktop)      │
│                                                  │
│  Opens ZIM → serves content via internal server   │
│  MapLibre GL JS requests tiles via relative URLs  │
│  Kiwix intercepts requests → serves from ZIM      │
│  Vector tiles rendered to canvas in real-time     │
│                                                  │
└─────────────────────────────────────────────────┘
```

### Pipeline

1. **Download** OSM data extract (PBF format) from Geofabrik
2. **Generate** vector tiles using [tilemaker](https://tilemaker.org/) (OpenMapTiles schema)
3. **Extract** individual tiles from MBTiles (SQLite) container
4. **Package** tiles + MapLibre GL JS + styles + fonts into a ZIM file using [python-libzim](https://github.com/openzim/python-libzim)

### Why This Approach Works

- **ZIM files** are compressed archives of web content (HTML, JS, CSS, images)
- **Kiwix** serves ZIM content like a local web server — JS apps work normally
- **MapLibre GL JS** is a pure-JavaScript vector tile renderer that runs in any modern browser
- **Vector tiles** (MVT/PBF format) are 50-200x smaller than raster tiles
- **OpenMapTiles schema** is the standard for vector tile layers (roads, buildings, water, etc.)

## Usage

### Prerequisites

**libzim 9.6+ required for large builds.** Two fixes that StreetZim contributed
upstream — the compressor infinite-loop fix and the spin-loop → condition-variable
rewrite of the writer pipeline — are needed when building US-scale (and larger)
ZIMs. Both landed in stock libzim by 9.6, so a current `pip install libzim` is
sufficient. The historical patches are kept in `patches/` for reference.

**System tools:**

```bash
# macOS (Homebrew)
brew install osmium-tool meson ninja

# tilemaker must be built from source on macOS:
git clone https://github.com/systemed/tilemaker.git /tmp/tilemaker
brew install boost lua rapidjson shapelib sqlite
cd /tmp/tilemaker && mkdir build && cd build && cmake .. && make -j$(sysctl -n hw.ncpu)
cp /tmp/tilemaker/build/tilemaker /opt/homebrew/bin/

# Linux (Debian/Ubuntu)
apt install tilemaker osmium-tool meson ninja-build
```

**Python environment:**

```bash
python3 -m venv venv312
source venv312/bin/activate

pip install -r requirements.txt
# Then install patched python-libzim (see patches/README.md)
```

### Quick Start — Vector Tiles (MapLibre GL JS)

```bash
# Create a ZIM for a well-known area
python3 create_osm_zim.py --area monaco
python3 create_osm_zim.py --area dc
python3 create_osm_zim.py --area san-francisco
python3 create_osm_zim.py --area manhattan

# Use a Geofabrik path for any region
python3 create_osm_zim.py --geofabrik europe/liechtenstein --name "Liechtenstein"

# Extract a specific area from a larger download
python3 create_osm_zim.py --geofabrik north-america/us/texas \
    --bbox "-97.95,30.10,-97.55,30.50" --name "Austin, TX"

# Use a local PBF file
python3 create_osm_zim.py --pbf mydata.osm.pbf --name "My Area" \
    --bbox "-97.9,30.1,-97.5,30.5"
```

### Quick Start — Raster Tiles (Leaflet)

```bash
# Create raster-tile ZIMs for comparison
python3 create_osm_zim_leaflet.py --area monaco
python3 create_osm_zim_leaflet.py --area dc

# Same options as the vector script
python3 create_osm_zim_leaflet.py --geofabrik europe/liechtenstein --name "Liechtenstein"
```

The Leaflet script uses the same tilemaker pipeline to generate vector tiles,
then renders them to 256x256 PNG raster images using Python (Pillow +
mapbox-vector-tile). Output files are named `osm-<area>-leaflet.zim`.

### Available Areas

| Name | Description |
|------|-------------|
| `monaco` | Monaco (tiny, good for testing) |
| `dc` | Washington, D.C. |
| `manhattan` | Manhattan, New York |
| `san-francisco` | San Francisco, CA |
| `austin` | Austin, TX |
| `portland` | Portland, OR |
| `liechtenstein` | Liechtenstein (small country) |

### Pre-built ZIM Files

The repository includes pre-built ZIM files for quick testing:

| File | Approach | Size |
|------|----------|------|
| `osm-monaco.zim` | Vector (MapLibre) | 778 KB |
| `osm-monaco-leaflet.zim` | Raster (Leaflet) | 355 KB |
| `osm-washington-dc.zim` | Vector (MapLibre) | 7.8 MB |
| `osm-washington-dc-leaflet.zim` | Raster (Leaflet) | 6.9 MB |

### Testing with kiwix-serve

```bash
# Install kiwix-tools
apt install kiwix-tools

# Serve the ZIM file locally
kiwix-serve --port 8888 osm-monaco.zim

# Open in browser: http://localhost:8888
```

### Using on iOS

1. Transfer the `.zim` file to your iOS device (AirDrop, Files app, etc.)
2. Open the Kiwix app
3. Import the ZIM file
4. The map opens with full pan/zoom, rendered entirely offline

## In-ZIM Apps

The ZIM ships two LLM-free, network-free browser apps in addition
to the main map viewer:

- `search/<slug>.html` — per-feature detail pages with **Directions
  to here** + **View on map** CTAs.
- `places.html` — search-and-browse mini-app. Search box, category
  chips (Restaurants, Cafés, Bars, Museums, Parks, Libraries,
  Shops, Gas), optional GPS-distance sort.

Both compose into the main viewer through a tiny URL-fragment
protocol (`index.html#dest=lat,lon&label=…`). Full reference in
[`docs/in-zim-apps.md`](docs/in-zim-apps.md).

## Technical Details

### OSM Data Pipeline

- **Source format:** OSM PBF (Protocol Buffer Format) — binary, compact
- **Tile generation:** tilemaker with OpenMapTiles Lua processing script
- **Tile format:** MVT (Mapbox Vector Tiles) in Protocol Buffer encoding
- **Zoom levels:** 0-14 (configurable with `--max-zoom`)
- **Intermediate:** MBTiles (SQLite database of tiles)
- **Final output:** ZIM file with individual tile entries

### Vector Tile Layers

The OpenMapTiles schema includes these layers:

| Layer | Min Zoom | Content |
|-------|----------|---------|
| `place` | 0 | Cities, towns, villages |
| `boundary` | 0 | Administrative boundaries |
| `transportation` | 4 | Roads, railways, paths |
| `transportation_name` | 8 | Road names |
| `water` | 6 | Water bodies |
| `waterway` | 8 | Rivers, streams |
| `landcover` | 0 | Forests, grass, farmland |
| `landuse` | 4 | Residential, commercial areas |
| `building` | 13 | Building footprints |
| `poi` | 12 | Points of interest |
| `housenumber` | 14 | House numbers |
| `park` | 11 | Parks and nature reserves |
| `aeroway` | 11 | Airport runways, taxiways |
| `mountain_peak` | 11 | Mountain peaks |

### Map Rendering

- **Renderer:** MapLibre GL JS v5.23.0
- **Style:** Custom lightweight style embedded in ZIM
- **Fonts:** Minimal SDF (Signed Distance Field) font PBFs for labels
- **No sprites/icons** in this prototype (text-only labels)

### Known Limitations

1. **No coastline/ocean data** — tilemaker needs separate shapefiles for ocean polygons
   (Natural Earth data). The prototype skips these, so oceans appear as background color.
2. **Minimal fonts** — the prototype includes placeholder SDF fonts. For production,
   bundle real font PBFs from [openmaptiles/fonts](https://github.com/openmaptiles/fonts).
3. **iOS Kiwix JS support** — Kiwix iOS uses WKWebView which supports WebGL (required
   by MapLibre GL JS). Initial testing indicates this works, but complex map interactions
   may vary by iOS version.

## OSM Data Sources & Sizes

### Where to get OSM data

- **[Geofabrik](https://download.geofabrik.de/)** — Pre-built extracts by country/state, updated daily
- **[BBBike](https://extract.bbbike.org/)** — Custom city-level extracts
- **[planet.openstreetmap.org](https://planet.openstreetmap.org/)** — Full planet file (~85 GB PBF)

### Typical PBF sizes

| Region | PBF Size |
|--------|----------|
| Monaco | 0.6 MB |
| Washington, D.C. | 19.6 MB |
| New York State | ~463 MB |
| California | ~1.2 GB |
| Texas | ~652 MB |
| United States | ~11 GB |
| Entire Planet | ~85 GB |

## License

**Tool code:** MIT (see [LICENSE](LICENSE))

**Data in ZIM files:**

| Source | License | Attribution Required |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | ODbL 1.0 | (c) OpenStreetMap contributors |
| [Sentinel-2 Cloudless](https://s2maps.eu) (satellite) | **CC BY-NC-SA 4.0** | Sentinel-2 cloudless by EOX (Contains modified Copernicus Sentinel data 2021) |
| [Copernicus GLO-30 DEM](https://dataspace.copernicus.eu) (elevation) | Copernicus free & open | (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by EU and ESA |
| [OpenMapTiles](https://openmaptiles.org/) (tile schema) | CC BY 4.0 | (c) OpenMapTiles contributors |
| [Wikidata](https://www.wikidata.org/) (place info) | CC0 1.0 | None required |
| [Wikipedia](https://en.wikipedia.org/) (extracts) | CC BY-SA 3.0 | Wikipedia contributors |

**Bundled software:** [MapLibre GL JS](https://maplibre.org/) (BSD-3-Clause), [Leaflet](https://leafletjs.com/) (BSD-2-Clause)

> **Note on commercial use:** The Sentinel-2 cloudless 2021 satellite imagery
> is licensed CC BY-NC-SA 4.0, which **restricts commercial use**. ZIM files
> containing satellite tiles may only be distributed for non-commercial
> purposes. To distribute commercially, simply omit the `--satellite`
> flag when building, or substitute imagery with a permissive license.
> All other data sources permit commercial redistribution with proper
> attribution.
