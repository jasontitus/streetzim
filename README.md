# StreetZIM - Offline OSM Maps in ZIM Files

A prototype tool that packages OpenStreetMap vector tiles into ZIM files for
offline viewing in the [Kiwix](https://kiwix.org) app (iOS, Android, desktop).
Maps are rendered client-side using MapLibre GL JS — no tile server needed.

## The Idea

**Problem:** Offline map apps typically store pre-rendered raster tiles, which
are enormous. A full planet render at z0-18 is ~54 TB. Even a single city at
useful zoom levels can be multiple gigabytes.

**Solution:** Store compact vector tiles in a ZIM file and render them on-the-fly
in the browser using MapLibre GL JS. This gives you:

- **50-200x smaller** files compared to raster tiles
- **Arbitrary resolution** — vector tiles scale perfectly to any display density
- **Runs in Kiwix** — the world's most popular offline content reader
- **No server needed** — everything renders client-side in JavaScript

## Size Comparisons

| Area | OSM PBF | Vector Tiles | ZIM File | Raster z0-18 (est.) |
|------|---------|--------------|----------|---------------------|
| Monaco | 0.6 MB | 0.3 MB | 0.7 MB | ~50 MB |
| Washington, D.C. | 19.6 MB | 8.6 MB | 17.4 MB | ~2-5 GB |
| US State (typical) | 200-600 MB | 50-200 MB | 60-250 MB | ~50-200 GB |
| Entire Planet | ~85 GB | ~80-120 GB | ~100-150 GB | ~54 TB |

The ZIM file includes MapLibre GL JS (~800 KB) and font glyphs, so it's slightly
larger than raw vector tiles, but still dramatically smaller than raster.

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

```bash
# Python packages
pip install libzim osmium

# System tools
apt install tilemaker osmium-tool
```

### Quick Start

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

- **Renderer:** MapLibre GL JS v4.7.1
- **Style:** Custom lightweight style embedded in ZIM
- **Fonts:** Minimal SDF (Signed Distance Field) font PBFs for labels
- **No sprites/icons** in this prototype (text-only labels)

### Known Limitations

1. **No coastline/ocean data** — tilemaker needs separate shapefiles for ocean polygons
   (Natural Earth data). The prototype skips these, so oceans appear as background color.
2. **Minimal fonts** — the prototype includes placeholder SDF fonts. For production,
   bundle real font PBFs from [openmaptiles/fonts](https://github.com/openmaptiles/fonts).
3. **No search** — the prototype is view-only. A future version could add geocoding.
4. **iOS Kiwix JS support** — Kiwix iOS uses WKWebView which supports WebGL (required
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

- Tool code: MIT
- Map data: [OpenStreetMap](https://www.openstreetmap.org/copyright) (ODbL)
- Tile schema: [OpenMapTiles](https://openmaptiles.org/) (CC-BY 4.0)
- MapLibre GL JS: BSD-3-Clause
