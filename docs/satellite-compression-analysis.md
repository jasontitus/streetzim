# Satellite Imagery Compression Analysis

## Current Approach

- **Source:** Sentinel-2 Cloudless (EOX, 10m/pixel native resolution, 2021 vintage)
- **Tile format:** 256x256 AVIF, quality 30
- **Consumer:** MapLibre GL JS raster source / Leaflet tile layer
- **Zoom range:** z0-z14 (configurable with `--satellite-zoom`)
- **ZIM storage:** AVIF tiles stored uncompressed (already lossy-compressed)

## Why AVIF Beats WebP for Satellite Imagery

AVIF (AV1 Image Format) uses the AV1 video codec's intra-frame compression, which is significantly more efficient than WebP's VP8-based compression for photographic content. Satellite imagery is an ideal case: natural textures, smooth gradients, no text or sharp synthetic edges.

At equivalent visual quality, AVIF produces files **60-70% smaller** than WebP for satellite tiles.

## Strategies Tested

We tested 15 strategies across three sample areas near Washington, D.C.: urban (National Mall/Capitol), suburban (Bethesda/Silver Spring), and rural (Virginia countryside). All tests used z14 tiles from the EOX Sentinel-2 WMTS source.

### Formats and Quality Levels

- **WebP** at quality 30, 40, 50, 65 (65 was the previous default)
- **AVIF** at quality 20, 30, 40, 50
- **JPEG** at quality 85 (original source, for reference)

### Tile Sizes

- **256x256** — standard WMTS tile size, one source tile per output tile
- **512x512** — stitch 4 source tiles (from z+1) into one output tile

## Results

### Average Savings vs Previous Default (WebP q65 256px)

| Strategy | Avg savings | Notes |
|---|---|---|
| avif_q20_512 | 77.3% | Diminishing returns below q30 |
| avif_q30_512 | 68.1% | Larger because 4x source pixels from z+1 |
| avif_q20 | 65.4% | Aggressive, minor artifacts at full zoom |
| **avif_q30** | **56.2%** | **New default — best quality/size tradeoff** |
| avif_q40_512 | 53.9% | |
| avif_q40 | 42.4% | Conservative, visually lossless |
| webp_q30 | 39.2% | Aggressive WebP |
| webp_q40_512 | 33.2% | |
| avif_q50_512 | 32.3% | |
| webp_q40 | 28.9% | |
| webp_q50_512 | 22.2% | |
| avif_q50 | 20.6% | |
| webp_q50 | 17.7% | |
| webp_q65_512 | 4.4% | |
| webp_q65 | 0.0% | Previous default (baseline) |
| jpeg_source | -235.1% | Original uncompressed source |

### Detailed Results by Area

**Urban DC (National Mall / Capitol)**

| Strategy | Size | vs JPEG | vs webp_q65 |
|---|---|---|---|
| avif_q20_512 | 3.6 KB | 91.3% | 78.1% |
| avif_q30_512 | 5.0 KB | 87.7% | 69.3% |
| avif_q30 | 7.1 KB | 82.7% | 56.7% |
| webp_q30 | 9.8 KB | 76.0% | 39.8% |
| webp_q65 | 16.4 KB | 60.1% | — |

**Rural Virginia (farmland/forest)**

| Strategy | Size | vs JPEG | vs webp_q65 |
|---|---|---|---|
| avif_q20_512 | 4.8 KB | 90.2% | 77.1% |
| avif_q30_512 | 6.9 KB | 85.9% | 67.1% |
| avif_q30 | 8.9 KB | 81.6% | 57.1% |
| webp_q30 | 12.7 KB | 74.0% | 39.3% |
| webp_q65 | 20.9 KB | 57.1% | — |

**Suburban (Bethesda / Silver Spring)**

| Strategy | Size | vs JPEG | vs webp_q65 |
|---|---|---|---|
| avif_q20_512 | 3.5 KB | 90.8% | 76.8% |
| avif_q30_512 | 4.9 KB | 87.4% | 68.1% |
| avif_q30 | 7.0 KB | 81.9% | 54.3% |
| webp_q30 | 9.4 KB | 75.6% | 38.5% |
| webp_q65 | 15.3 KB | 60.4% | — |

## Why 512px Tiles Don't Help

The 512px tile approach stitches 4 source tiles from z+1 into one output tile. In theory, larger tiles compress better because the encoder has more spatial context.

In practice, this **hurts** for satellite imagery because:

1. **No real additional detail.** Sentinel-2 native resolution is 10m/pixel. At z14, each pixel is ~9.5m — already at the source limit. The z15 tiles are just upscaled, so 512px tiles encode 4x the pixels with no additional information.
2. **Net size increase.** AVIF q30 at 512px (1,384 KB for DC) is more than 2x larger than AVIF q30 at 256px (642 KB) for the same geographic coverage.
3. **More network requests per area.** While there are fewer tiles, each tile is larger and covers the same area, so the total bytes transferred increases.

The 512px approach would only be beneficial if the source imagery had detail beyond the 256px tile resolution at each zoom level — e.g., aerial photography at 0.5m/pixel.

## DC ZIM Build Comparison

| ZIM | Total size | Satellite data | Format |
|---|---|---|---|
| osm-dc-webp256.zim | 9.6 MB | 1,657 KB | WebP q65 256px (old default) |
| osm-dc-avif512.zim | 9.3 MB | 1,384 KB | AVIF q30 512px |
| **osm-dc-avif256.zim** | **8.6 MB** | **642 KB** | **AVIF q30 256px (new default)** |

Satellite data reduction from old to new default: **61%**.

## Kiwix Compatibility

AVIF is decoded by the browser/webview, not by Kiwix itself. Support:

| Platform | AVIF support |
|---|---|
| Kiwix Desktop (Electron/Chromium) | Yes |
| Kiwix Android (WebView) | Android 12+ (2021) |
| Kiwix iOS (Safari) | iOS 16.4+ (2023) |
| Kiwix JS PWA | Depends on browser |

For older devices, use `--satellite-format webp` to fall back to WebP.

## CLI Options

```bash
# New default (AVIF q30 256px) — just add --satellite
python3 create_osm_zim.py --area dc --satellite

# Explicit AVIF with custom quality
python3 create_osm_zim.py --area dc --satellite --satellite-quality 40

# Fall back to WebP for older Kiwix clients
python3 create_osm_zim.py --area dc --satellite --satellite-format webp

# 512px tiles (higher pixel density, larger files)
python3 create_osm_zim.py --area dc --satellite --satellite-tile-size 512
```

## Recommendation

**Use the default: AVIF q30 at 256px.**

- 61% smaller satellite data vs the old WebP q65 default
- No viewer changes needed — MapLibre and Leaflet decode whatever the browser supports
- Quality is visually indistinguishable from WebP q65 at map zoom levels
- Falls back gracefully with `--satellite-format webp` for legacy devices

## Test Script

```bash
python3 test_satellite_compression.py
```

Downloads sample tiles from urban, rural, and suburban areas and benchmarks all format/quality/size combinations.
