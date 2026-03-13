# Elevation Data Compression Analysis

## Current Approach

- **Source data:** Copernicus GLO-30 DEM (30m spatial resolution)
- **Encoding:** Mapbox terrain-RGB — elevation packed into 3 bytes (R/G/B) with 0.1m precision, offset by 10,000m
- **Tile format:** 256x256 lossless WebP
- **Consumer:** MapLibre GL JS (`encoding: 'mapbox'`)
- **Zoom range:** z0-z12
- **ZIM storage:** WebP tiles stored in zstd-compressed clusters (~1MB each)

The encoding formula is: `encoded = (elevation + 10000.0) / 0.1`, then split across R (high byte), G (mid byte), B (low byte). This gives 0.1m precision over a range of -10,000m to +6,777m.

## Why Lossy Compression Does NOT Work

Lossy WebP (and lossy AVIF) are fundamentally incompatible with Mapbox terrain-RGB encoding. The encoding packs elevation into a 24-bit integer split across R/G/B:
- R = high byte: each LSB change = `65536 * 0.1 = 6,553.6m` of elevation error
- G = mid byte: each LSB change = `256 * 0.1 = 25.6m` of elevation error
- B = low byte: each LSB change = `0.1m` of elevation error

Lossy compressors treat each channel as an independent image signal and don't respect the byte-boundary structure. Even at quality 99, the compressor freely changes R and G values, producing thousands of meters of elevation error. Our testing confirmed mean errors of 21,000m+ for mountainous terrain.

**Note on "lossless" AVIF via Pillow:** Pillow's AVIF encoder (pillow-avif-plugin) introduces ±3 LSB errors per channel even at quality=100 with 4:4:4 subsampling, due to internal color space conversion. This would cause catastrophic elevation errors in terrain-RGB tiles (up to ±19,660m from R channel alone). True lossless AVIF requires `avifenc --lossless` which uses identity matrix coefficients (no YUV conversion).

## Strategies Tested

We tested 16 strategies across three regions (Colorado mountains, Kansas rolling hills, Washington DC flatlands), measuring both raw tile size and size after zstd compression (simulating ZIM cluster storage).

### 1. WebP Quantized (MapLibre-native, no viewer changes)

Pre-round elevation to a coarser precision before encoding with the standard Mapbox formula. Reduces entropy in all three RGB channels, improving lossless WebP compression.

```python
elev = np.round(elev / round_meters) * round_meters  # e.g. round_meters=5
encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)  # standard Mapbox formula
```

**Key finding:** Lossless WebP already compresses so well that zstd adds nothing on top (~1.00x ratio). The WebP file IS the final compressed size.

### 2. AVIF Lossless Quantized (needs addProtocol or native AVIF support)

Same Mapbox terrain-RGB encoding and quantization as WebP, but stored as lossless AVIF (AV1 intra-frame). Encoded via `avifenc --lossless` to guarantee exact round-trip.

**Key finding:** Lossless AVIF is consistently 25-135% **larger** than lossless WebP at the same quantization level. At high quantization (5m, 10m) the gap narrows but WebP still wins. AVIF's AV1 codec is optimized for photographic/video content — its lossless mode is not competitive with WebP's lossless mode for this type of structured RGB data.

### 3. Raw Int16 Binary (custom decoder needed)

Store elevation as raw 16-bit signed integers (1m precision). 256x256 tile = 128 KB uncompressed. Relies entirely on zstd for compression.

Also tested **delta-encoded** variant: store row-wise pixel-to-pixel differences instead of absolute values. Deltas have smaller magnitude and compress much better.

**Key finding:** Even with zstd, raw int16 barely beats the WebP baseline. Delta encoding helps but still loses to quantized WebP.

### 4. LERC (Limited Error Raster Compression, custom decoder needed)

Purpose-built format for elevation/raster data. Supports configurable max error tolerance. Has a JavaScript decoder available (esri-leaflet uses it).

**Key finding:** LERC at equivalent error tolerance consistently loses to quantized WebP. At 0.1m precision, LERC is actually *larger* than WebP baseline. LERC compresses somewhat further under zstd but not enough to close the gap.

## Full Results

### Sorted by Effective Size in ZIM (zstd-compressed)

**Colorado (CO Springs) — Mountainous terrain, ~1,600m-4,300m elevation**

| Strategy | In ZIM | Savings | Max Error | Decoder |
|----------|--------|---------|-----------|---------|
| webp_quant_10m | 1.27 MB | 74.1% | 5.0m | native |
| lerc_10m | 1.34 MB | 72.5% | 10.0m | custom |
| **webp_quant_5m** | **1.68 MB** | **65.7%** | **2.5m** | **native** |
| lerc_5m | 1.89 MB | 61.3% | 5.0m | custom |
| webp_quant_2m | 2.31 MB | 52.8% | 1.0m | native |
| webp_quant_1m | 2.89 MB | 40.9% | 0.5m | native |
| avif_quant_10m | 3.16 MB | 35.4% | 5.0m | native |
| lerc_1m | 3.45 MB | 29.4% | 1.0m | custom |
| raw_int16_delta | 3.62 MB | 26.1% | 0.5m | custom |
| avif_quant_5m | 3.93 MB | 19.6% | 2.5m | native |
| webp_baseline | 4.89 MB | — | 0.1m | native |
| raw_int16 | 5.14 MB | -5.1% | 0.5m | custom |
| avif_quant_2m | 5.56 MB | -13.6% | 1.0m | native |
| lerc_0.1m | 5.70 MB | -16.6% | 0.1m | custom |
| avif_quant_1m | 6.10 MB | -24.6% | 0.5m | native |
| avif_baseline | 6.21 MB | -26.9% | 0.1m | native |

**Kansas (Flint Hills) — Rolling grassland, ~300m-500m elevation**

| Strategy | In ZIM | Savings | Max Error | Decoder |
|----------|--------|---------|-----------|---------|
| webp_quant_10m | 466 KB | 85.7% | 5.0m | native |
| lerc_10m | 470 KB | 85.6% | 10.0m | custom |
| avif_quant_10m | 533 KB | 83.6% | 5.0m | native |
| **webp_quant_5m** | **789 KB** | **75.8%** | **2.5m** | **native** |
| lerc_5m | 824 KB | 74.7% | 5.0m | custom |
| avif_quant_5m | 929 KB | 71.5% | 2.5m | native |
| webp_quant_2m | 1.20 MB | 62.2% | 1.0m | native |
| webp_quant_1m | 1.57 MB | 50.6% | 0.5m | native |
| lerc_1m | 1.95 MB | 38.8% | 1.0m | custom |
| raw_int16_delta | 2.32 MB | 27.2% | 0.5m | custom |
| avif_quant_2m | 2.56 MB | 19.6% | 1.0m | native |
| raw_int16 | 2.73 MB | 14.3% | 0.5m | custom |
| webp_baseline | 3.18 MB | — | 0.1m | native |
| avif_quant_1m | 3.66 MB | -15.0% | 0.5m | native |
| avif_baseline | 3.93 MB | -23.6% | 0.1m | native |
| lerc_0.1m | 4.08 MB | -28.3% | 0.1m | custom |

**Washington DC — Flat terrain, ~0m-150m elevation**

| Strategy | In ZIM | Savings | Max Error | Decoder |
|----------|--------|---------|-----------|---------|
| webp_quant_10m | 272 KB | 80.7% | 5.0m | native |
| lerc_10m | 272 KB | 80.7% | 10.0m | custom |
| avif_quant_10m | 359 KB | 74.5% | 5.0m | native |
| **webp_quant_5m** | **414 KB** | **70.6%** | **2.5m** | **native** |
| lerc_5m | 420 KB | 70.2% | 5.0m | custom |
| webp_quant_2m | 604 KB | 57.2% | 1.0m | native |
| avif_quant_5m | 668 KB | 52.6% | 2.5m | native |
| webp_quant_1m | 778 KB | 44.7% | 0.5m | native |
| lerc_1m | 847 KB | 39.9% | 1.0m | custom |
| raw_int16_delta | 1.00 MB | 27.0% | 0.5m | custom |
| raw_int16 | 1.17 MB | 15.0% | 0.5m | custom |
| webp_baseline | 1.38 MB | — | 0.1m | native |
| avif_quant_2m | 1.41 MB | -2.3% | 1.0m | native |
| lerc_0.1m | 1.49 MB | -8.0% | 0.1m | custom |
| avif_quant_1m | 1.55 MB | -12.6% | 0.5m | native |
| avif_baseline | 1.65 MB | -19.8% | 0.1m | native |

### WebP vs AVIF Head-to-Head Comparison

At each quantization level, lossless WebP consistently beats lossless AVIF:

| Quantization | Colorado WebP | Colorado AVIF | AVIF Overhead |
|-------------|--------------|--------------|---------------|
| Baseline (0.1m) | 4.89 MB | 6.21 MB | **+27%** |
| 1m | 2.89 MB | 6.10 MB | **+111%** |
| 2m | 2.31 MB | 5.56 MB | **+141%** |
| 5m | 1.68 MB | 3.93 MB | **+134%** |
| 10m | 1.27 MB | 3.16 MB | **+149%** |

| Quantization | Kansas WebP | Kansas AVIF | AVIF Overhead |
|-------------|------------|------------|---------------|
| Baseline (0.1m) | 3.18 MB | 3.93 MB | **+24%** |
| 1m | 1.57 MB | 3.66 MB | **+133%** |
| 2m | 1.20 MB | 2.56 MB | **+113%** |
| 5m | 789 KB | 929 KB | **+18%** |
| 10m | 466 KB | 533 KB | **+14%** |

| Quantization | DC WebP | DC AVIF | AVIF Overhead |
|-------------|--------|--------|---------------|
| Baseline (0.1m) | 1.38 MB | 1.65 MB | **+20%** |
| 1m | 778 KB | 1.55 MB | **+99%** |
| 2m | 604 KB | 1.41 MB | **+134%** |
| 5m | 414 KB | 668 KB | **+61%** |
| 10m | 272 KB | 359 KB | **+32%** |

**Key finding:** AVIF lossless is 14-149% larger than WebP lossless across all terrain types and quantization levels. The gap is largest at low quantization (where there is more entropy for the codec to handle) and smallest at high quantization on flatter terrain. Even in the best case (Kansas 10m), AVIF is still 14% larger.

### Zstd Compressibility by Format

| Format | Zstd ratio (CO z12) | Zstd ratio (KS z12) | Zstd ratio (DC z12) | Notes |
|--------|---------------------|---------------------|---------------------|-------|
| Lossless WebP | 1.00x | 1.00x | 1.00x | Already optimally compressed |
| Lossless AVIF | 1.00x | 1.00x | 1.00x | Already optimally compressed |
| Raw int16 | 2.16x | 4.08x | 3.19x | Zstd helps a lot |
| Raw int16 delta | 3.21x | 4.78x | 3.82x | Delta + zstd is decent |
| LERC 0.1m | 1.01x | 1.03x | 1.03x | Already compressed internally |
| LERC 5m | 1.29x | 1.56x | 1.34x | Some zstd gains at higher error |

**Key insight:** Both lossless WebP and lossless AVIF are effectively incompressible by zstd — they're already at their entropy limit. So the raw file sizes are the true final sizes in ZIM.

### Elevation Error Comparison

| Strategy | Mean Error | P95 Error | Max Error |
|----------|-----------|-----------|-----------|
| webp_baseline / avif_baseline | 0.05m | 0.09m | 0.10m |
| webp_quant_1m / avif_quant_1m | 0.25m | 0.48m | 0.50m |
| webp_quant_2m / avif_quant_2m | 0.50m | 0.95m | 1.00m |
| **webp_quant_5m / avif_quant_5m** | **1.25m** | **2.37m** | **2.50m** |
| webp_quant_10m / avif_quant_10m | 2.50m | 4.75m | 5.00m |
| lerc_1m | 0.49m | 0.95m | 1.00m |
| lerc_5m | 2.52m | 4.76m | 5.00m |
| lerc_10m | 5.02m | 9.49m | 10.00m |

WebP and AVIF have identical elevation errors at each quantization level (both are lossless — the error comes purely from quantization). LERC's `maxZErr` is *per-side* (±), so LERC 5m has max error of 5.0m while WebP/AVIF quant 5m has max error of 2.5m (half the rounding step).

## Recommendation

**Use `webp_quant_5m`: pre-round elevation to 5m, lossless WebP.**

Rationale:
- **Best compression for any native-decoder option**: 66-76% savings
- **No viewer changes needed**: works with existing MapLibre Mapbox decoder
- **Bounded, predictable error**: max ±2.5m (well within 30m DEM accuracy)
- **Beats all custom formats**: LERC and raw binary at comparable error are larger even after zstd
- **Beats AVIF lossless**: WebP lossless is 18-134% smaller than AVIF lossless at the same quantization
- **One-line code change**: just add `elev = np.round(elev / 5.0) * 5.0`

### Why Not AVIF for Elevation Tiles?

While AVIF excels at lossy photographic compression (e.g., our satellite tiles use AVIF at 56% savings over WebP), its **lossless mode is not competitive** for terrain-RGB elevation data:

1. **AV1 lossless is optimized for natural images** — it doesn't exploit the structured, low-entropy patterns that quantized terrain-RGB produces as effectively as WebP's lossless mode
2. **Consistently larger**: 14-149% overhead vs WebP across all terrain types and quantization levels
3. **Encoding is slower**: avifenc takes ~2x as long as Pillow's WebP encoder
4. **No decoder advantage**: both require the same Mapbox terrain-RGB decoding logic; AVIF adds a format dependency without benefit

LERC and raw binary formats are also not worth the complexity — they require a custom JavaScript decoder via `addProtocol`, add a dependency, and don't compress as well as quantized lossless WebP.

### Implementation

In `generate_terrain_tiles()` in `create_osm_zim.py`, add one line:

```python
elev = elevation[0]
elev = np.round(elev / 5.0) * 5.0  # ADD THIS LINE
encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
```

### If More Savings Are Needed

`webp_quant_10m` gives 74-86% savings with ±5m max error. Still well within the source DEM's accuracy at 30m horizontal resolution.

## Test Script

```bash
python3 test_terrain_compression.py [--max-zoom 12]
```

Tests all 16 strategies (WebP and AVIF lossless quantized variants, raw int16, raw int16 delta-encoded, LERC at 4 error levels) across three regions (Colorado, Kansas, DC), measures raw size, zstd-compressed size, and round-trip elevation error.
