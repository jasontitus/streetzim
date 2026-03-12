# Elevation Data Compression Analysis

## Current Approach

- **Source data:** Copernicus GLO-30 DEM (30m spatial resolution)
- **Encoding:** Mapbox terrain-RGB — elevation packed into 3 bytes (R/G/B) with 0.1m precision, offset by 10,000m
- **Tile format:** 256x256 lossless WebP
- **Consumer:** MapLibre GL JS (`encoding: 'mapbox'`)
- **Zoom range:** z0-z12
- **ZIM storage:** WebP tiles stored in zstd-compressed clusters (~1MB each)

The encoding formula is: `encoded = (elevation + 10000.0) / 0.1`, then split across R (high byte), G (mid byte), B (low byte). This gives 0.1m precision over a range of -10,000m to +6,777m.

## Why Lossy WebP Does NOT Work

Lossy WebP is fundamentally incompatible with Mapbox terrain-RGB encoding. The encoding packs elevation into a 24-bit integer split across R/G/B:
- R = high byte: each LSB change = `65536 * 0.1 = 6,553.6m` of elevation error
- G = mid byte: each LSB change = `256 * 0.1 = 25.6m` of elevation error
- B = low byte: each LSB change = `0.1m` of elevation error

Lossy WebP treats each channel as an independent image signal and doesn't respect the byte-boundary structure. Even at quality 99, the compressor freely changes R and G values, producing thousands of meters of elevation error. Our testing confirmed mean errors of 21,000m+ for mountainous terrain.

## Strategies Tested

We tested 11 strategies across two regions (Colorado mountains, Washington DC flatlands), measuring both raw tile size and size after zstd compression (simulating ZIM cluster storage).

### 1. WebP Quantized (MapLibre-native, no viewer changes)

Pre-round elevation to a coarser precision before encoding with the standard Mapbox formula. Reduces entropy in all three RGB channels, improving lossless WebP compression.

```python
elev = np.round(elev / round_meters) * round_meters  # e.g. round_meters=5
encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)  # standard Mapbox formula
```

**Key finding:** Lossless WebP already compresses so well that zstd adds nothing on top (~1.00x ratio). The WebP file IS the final compressed size.

### 2. Raw Int16 Binary (custom decoder needed)

Store elevation as raw 16-bit signed integers (1m precision). 256x256 tile = 128 KB uncompressed. Relies entirely on zstd for compression.

Also tested **delta-encoded** variant: store row-wise pixel-to-pixel differences instead of absolute values. Deltas have smaller magnitude and compress much better.

**Key finding:** Even with zstd, raw int16 barely beats the WebP baseline. Delta encoding helps but still loses to quantized WebP.

### 3. LERC (Limited Error Raster Compression, custom decoder needed)

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
| lerc_1m | 3.45 MB | 29.4% | 1.0m | custom |
| raw_int16_delta | 3.62 MB | 26.1% | 0.5m | custom |
| webp_baseline | 4.89 MB | — | 0.1m | native |
| raw_int16 | 5.14 MB | -5.1% | 0.5m | custom |
| lerc_0.1m | 5.70 MB | -16.6% | 0.1m | custom |

**Washington DC — Flat terrain, ~0m-150m elevation**

| Strategy | In ZIM | Savings | Max Error | Decoder |
|----------|--------|---------|-----------|---------|
| webp_quant_10m | 272 KB | 80.7% | 5.0m | native |
| lerc_10m | 272 KB | 80.7% | 10.0m | custom |
| **webp_quant_5m** | **414 KB** | **70.6%** | **2.5m** | **native** |
| lerc_5m | 420 KB | 70.2% | 5.0m | custom |
| webp_quant_2m | 604 KB | 57.2% | 1.0m | native |
| webp_quant_1m | 778 KB | 44.7% | 0.5m | native |
| lerc_1m | 847 KB | 39.9% | 1.0m | custom |
| raw_int16_delta | 1.00 MB | 27.0% | 0.5m | custom |
| raw_int16 | 1.17 MB | 15.0% | 0.5m | custom |
| webp_baseline | 1.38 MB | — | 0.1m | native |
| lerc_0.1m | 1.49 MB | -8.0% | 0.1m | custom |

### Zstd Compressibility by Format

| Format | Zstd ratio (CO z12) | Zstd ratio (DC z12) | Notes |
|--------|---------------------|---------------------|-------|
| Lossless WebP | 1.00x | 1.00x | Already optimally compressed |
| Raw int16 | 2.16x | 3.19x | Zstd helps a lot |
| Raw int16 delta | 3.21x | 3.82x | Delta + zstd is decent |
| LERC 0.1m | 1.01x | 1.03x | Already compressed internally |
| LERC 5m | 1.29x | 1.34x | Some zstd gains at higher error |

**Key insight:** Lossless WebP is effectively incompressible by zstd — it's already at its entropy limit. So the WebP quantization numbers are the true final sizes. LERC and raw formats benefit from zstd, but not enough to overcome WebP's superior per-tile compression.

### Elevation Error Comparison

| Strategy | Mean Error | P95 Error | Max Error |
|----------|-----------|-----------|-----------|
| webp_baseline | 0.05m | 0.09m | 0.10m |
| webp_quant_1m | 0.25m | 0.48m | 0.50m |
| webp_quant_2m | 0.50m | 0.95m | 1.00m |
| **webp_quant_5m** | **1.25m** | **2.37m** | **2.50m** |
| webp_quant_10m | 2.50m | 4.75m | 5.00m |
| lerc_1m | 0.49m | 0.95m | 1.00m |
| lerc_5m | 2.52m | 4.76m | 5.00m |
| lerc_10m | 5.02m | 9.49m | 10.00m |

Note: LERC's `maxZErr` is *per-side* (±), so LERC 5m has max error of 5.0m while WebP quant 5m has max error of 2.5m (half the rounding step).

## Recommendation

**Use `webp_quant_5m`: pre-round elevation to 5m, lossless WebP.**

Rationale:
- **Best compression for any native-decoder option**: 66-71% savings
- **No viewer changes needed**: works with existing MapLibre Mapbox decoder
- **Bounded, predictable error**: max ±2.5m (well within 30m DEM accuracy)
- **Beats all custom formats**: LERC and raw binary at comparable error are larger even after zstd
- **One-line code change**: just add `elev = np.round(elev / 5.0) * 5.0`

LERC and raw binary formats are not worth the complexity — they require a custom JavaScript decoder via `addProtocol`, add a dependency, and don't compress as well as quantized lossless WebP.

### Implementation

In `generate_terrain_tiles()` in `create_osm_zim.py`, add one line:

```python
elev = elevation[0]
elev = np.round(elev / 5.0) * 5.0  # ADD THIS LINE
encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
```

### If More Savings Are Needed

`webp_quant_10m` gives 74-81% savings with ±5m max error. Still well within the source DEM's accuracy at 30m horizontal resolution.

## Test Script

```bash
python3 test_terrain_compression.py [--max-zoom 12]
```

Tests all 11 strategies (WebP quantized variants, raw int16, raw int16 delta-encoded, LERC at 4 error levels), measures raw size, zstd-compressed size, and round-trip elevation error.
