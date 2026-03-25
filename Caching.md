# StreetZim Caching

All caches live alongside `create_osm_zim.py` in the project root. They are designed for incremental reuse across builds — a world-scale cache is automatically reused when building regional extracts (US, DC, etc.).

## Cache Summary

| Cache | Size | Entries | Reuse Scope |
|-------|------|---------|-------------|
| `satellite_cache_sources/` | ~38 GB | JPEG source tiles | All satellite builds (any format/size) |
| `satellite_cache_{fmt}_{size}/` | 6–23 GB | Encoded tiles | Builds with matching format+size |
| `terrain_cache/` | ~633 GB | Terrain-RGB + DEM sources | All terrain builds |
| `wikidata_cache/` | ~1 GB | 3.15M Q-IDs | All builds with `--wikidata` |
| `search_cache/` | ~16 GB | World search JSONL | All builds with `--search-cache` |
| `world-data/` | ~307 GB | Planet PBF + MBTiles | World/regional builds via `--pbf`/`--mbtiles` |

Total footprint: ~1 TB.

---

## Satellite Tile Cache

**Two-tier architecture:** raw JPEG sources are downloaded once, then transcoded to the target format (WebP or AVIF) per-build.

### Source Cache (`satellite_cache_sources/`)

- **Contents:** Raw JPEG tiles from EOX Sentinel-2 WMTS (`tiles.maps.eox.at`)
- **Structure:** `{z}/{x}/{y}.jpg` (zoom 0–14)
- **Population:** `download_satellite_tiles()` with 32 parallel threads
- **Reuse:** Any satellite build checks here before re-downloading
- **Invalidation:** None — Sentinel-2 cloudless mosaics are static yearly composites
- **Size:** ~38 GB (full US coverage at z0–14)

### Format-Specific Caches (`satellite_cache_{format}_{tilesize}/`)

- **Examples:** `satellite_cache_avif_256/`, `satellite_cache_webp_512/`
- **Contents:** Tiles transcoded from source JPEGs
- **Structure:** `{z}/{x}/{y}.{webp|avif}`
- **Population:** Automatic during `download_satellite_tiles()` — transcodes from source cache
- **CLI controls:**
  - `--satellite` — enable satellite imagery
  - `--satellite-format {webp|avif}` — output format (default: avif)
  - `--satellite-quality N` — compression quality (default: 40 for AVIF, 65 for WebP)
  - `--satellite-tile-size {256|512}` — tile size (512 stitches 4 source tiles)
  - `--satellite-zoom N` — max zoom (default: same as `--max-zoom`)
- **Invalidation:** Different format/size/quality creates a new cache directory; old caches are not deleted
- **Size:** 6–23 GB depending on format and coverage

---

## Terrain Tile Cache

### Terrain Tiles (`terrain_cache/`)

- **Contents:** Terrain-RGB WebP tiles (lossless) derived from Copernicus GLO-30 DEM
- **Structure:** `{z}/{x}/{y}.webp` (zoom 0–12 typical)
- **Population:** `generate_terrain_tiles()` — downloads DEM sources, builds VRT mosaic, generates terrain-RGB
- **CLI controls:**
  - `--terrain` — enable terrain tiles
  - `--terrain-zoom N` — max zoom (default: 12)
  - `--terrain-dir PATH` — cache directory (default: `terrain_cache/`)
- **Reuse detection:** Samples 10 random tiles at max zoom; if all present, skips regeneration
- **Invalidation:** None — Copernicus DEM data is static
- **Size:** ~633 GB (includes DEM sources)

### DEM Source Sub-Cache (`terrain_cache/dem_sources/`)

- **Contents:** Copernicus GLO-30 GeoTIFF files (1-degree tiles, ~40–50 MB each)
- **Naming:** `dem_{N|S}{lat:02d}_{E|W}{lon:03d}.tif` (e.g., `dem_N38_W077.tif`)
- **VRT:** `mosaic_4326.vrt` — virtual raster index for efficient multi-tile reads
- **Reuse:** Tiles checked before downloading; corrupted files (< 1000 bytes) are re-fetched
- **Size:** ~547 GB (26,000+ tiles for global coverage)

---

## Wikidata Cache

### Cache Directory (`wikidata_cache/`)

- **Contents:** Wikidata properties (population, area, description, Wikipedia extract, etc.) for OSM features with `wikidata=Q*` tags
- **Structure:** Bucketed JSON files — `{prefix}.json` where prefix = first 2 digits of Q-ID number
  - Example: `Q123456` → bucket `12.json`
  - `manifest.json` — metadata (total entries, bucket count, timestamp)
- **Population:** `wikidata_cache.py` or automatically via `--wikidata` flag during build
  1. Extract Q-IDs from PBF (preferred) or MBTiles
  2. Load existing cache
  3. Fetch only missing Q-IDs from Wikidata SPARQL API (~40/batch, 1 req/sec rate limit)
  4. Fetch Wikipedia extracts (optional, ~500 chars each)
  5. Save incrementally every 10K entries (crash-safe)
- **CLI controls:**
  - `--wikidata` — enable Wikidata enrichment
  - `--wikidata-cache PATH` — cache directory (default: `wikidata_cache/`)
  - `--wikidata-no-extracts` — skip Wikipedia text (faster, smaller)
- **Reuse:** Fully incremental — only new Q-IDs are fetched. A world cache (3.15M entries) covers all regional builds.
- **Invalidation:** None — properties assumed stable. Manually delete entries or bucket files to force re-fetch.
- **Size:** ~1 GB (3,154,441 entries across 90 buckets)

### Fields Stored Per Entry

| Field | Key in ZIM | Description |
|-------|-----------|-------------|
| `label` | `l` | Wikidata label |
| `description` | `d` | Short description |
| `population` | `p` | Population count |
| `area_km2` | `a` | Area in km² |
| `elevation_m` | `e` | Elevation in meters |
| `country` | `c` | Country name |
| `capital` | `cap` | Capital city |
| `extract` | `x` | Wikipedia intro (~500 chars) |
| `instance_of` | `i` | Entity type |
| `timezone` | `tz` | Timezone |

---

## Search Features Cache

### Cache File (`search_cache/world.jsonl`)

- **Contents:** All named features extracted from z14 vector tiles (places, POIs, streets, water, parks, peaks, airports)
- **Format:** JSONL — one JSON object per line
  ```json
  {"name":"Washington","type":"place","subtype":"city","lat":38.89,"lon":-77.03}
  ```
- **Population:** `extract_searchable_features()` — parallel extraction from MBTiles z14 tiles
  - Layers scanned: `place`, `poi`, `transportation_name`, `water_name`, `park`, `mountain_peak`, `aerodrome_label`
  - Cross-worker deduplication via temp JSONL files
- **CLI control:** `--search-cache PATH` — use pre-built cache instead of extracting from tiles
- **Reuse:** When `--search-cache` is provided with a bbox, features are filtered to the bounding box at runtime without modifying the cache file
- **Invalidation:** None — tied to the planet snapshot used to generate it. Rebuild when updating planet data.
- **Size:** ~16 GB (world.jsonl, ~121M features)

---

## World Data (Long-Term Storage)

### Directory (`world-data/`)

- **Contents:**
  - `planet-{date}.osm.pbf` — full OpenStreetMap planet extract (~91 GB)
  - `world-tiles-v2.mbtiles` — pre-built vector tiles (~120 GB)
  - `world-tiles.mbtiles` — older vector tiles (~117 GB)
- **Population:** Manual — download planet PBF from Geofabrik, run tilemaker
- **Reuse:** Pass via `--pbf` or `--mbtiles` to skip download/tile generation
- **Invalidation:** Manual — user decides when to update planet data (versioned by date in filename)
- **Size:** ~307 GB

---

## Cache Reuse Across Builds

A typical regional build (e.g., US) reuses caches built from world data:

```
World Build                          US Build
───────────                          ────────
planet.osm.pbf ──→ world MBTiles     us-latest.osm.pbf ──→ us MBTiles

satellite_cache_sources/ ────────────→ same (bbox-filtered at download)
satellite_cache_avif_256/ ───────────→ same (bbox-filtered at download)
terrain_cache/dem_sources/ ──────────→ same (bbox-filtered tiles)
terrain_cache/{z}/{x}/{y}.webp ──────→ same (sampling detects existing)
wikidata_cache/ (3.15M Q-IDs) ───────→ same (only 459 new Q-IDs fetched)
search_cache/world.jsonl ────────────→ same (filtered to US bbox at runtime)
```

## CLI Quick Reference

```bash
# Full build reusing all caches
python3 create_osm_zim.py \
  --area "united-states" \
  --mbtiles us-tiles.mbtiles \
  --pbf us-latest.osm.pbf \
  --satellite --satellite-format avif \
  --terrain --terrain-zoom 12 \
  --wikidata \
  --search-cache search_cache/world.jsonl \
  --keep-temp

# Just rebuild wikidata cache
python3 wikidata_cache.py --pbf us-latest.osm.pbf --stats

# Show wikidata cache stats
python3 wikidata_cache.py --stats
```
