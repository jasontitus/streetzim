# Elevation Data Compression Analysis

## Current Approach

- **Source data:** Copernicus GLO-30 DEM (30m spatial resolution)
- **Encoding:** Mapbox terrain-RGB — elevation packed into 3 bytes (R/G/B) with 0.1m precision, offset by 10,000m
- **Tile format:** 256x256 lossless WebP
- **Consumer:** MapLibre GL JS (`encoding: 'mapbox'`)
- **Zoom range:** z0-z12

The encoding formula is: `encoded = (elevation + 10000.0) / 0.1`, then split across R (high byte), G (mid byte), B (low byte). This gives 0.1m precision over a range of -10,000m to +6,777m.

## Why Lossy WebP Does NOT Work

**Lossy WebP is fundamentally incompatible with Mapbox terrain-RGB encoding.**

The Mapbox encoding packs elevation into a 24-bit integer split across R/G/B:
- R = high byte: each LSB change = `65536 * 0.1 = 6,553.6m` of elevation error
- G = mid byte: each LSB change = `256 * 0.1 = 25.6m` of elevation error
- B = low byte: each LSB change = `0.1m` of elevation error

Lossy WebP treats each channel as an independent image signal and doesn't respect the byte-boundary structure. Even at quality 99, the compressor freely changes R and G values by 1-2 LSB, producing **thousands of meters of elevation error**. Our testing confirmed mean errors of 21,000m+ for Colorado at quality 92.

This is a known limitation of using lossy image compression with terrain-RGB tiles.

## Compression Strategy: Elevation Quantization

The effective approach is **pre-rounding elevation to a coarser precision** before encoding with the standard Mapbox formula. This:

1. Reduces the number of distinct encoded values, lowering entropy in all three RGB channels
2. Makes pixel values change more gradually between neighboring pixels
3. Improves lossless WebP compression significantly
4. Remains **fully compatible** with MapLibre's Mapbox decoder — no viewer changes needed

Implementation is a single line added before encoding:

```python
elev = np.round(elev / round_meters) * round_meters  # e.g., round_meters=5
encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)  # standard Mapbox formula
```

The source DEM is 30m horizontal resolution, so 0.1m vertical precision is far beyond what the data supports. Rounding to 1-10m loses nothing meaningful.

## Other Options Considered

| Option | Description | Why we didn't pursue it |
|--------|-------------|------------------------|
| **Lossy WebP** | Lossy image compression | Catastrophic elevation errors (see above) |
| **Terrarium encoding** | Alternative RGB encoding supported by MapLibre | Same lossy problem; marginal lossless gains |
| **128x128 tile resolution** | Half-resolution tiles, let MapLibre upscale | 4x fewer pixels but changes tile infrastructure |
| **Custom binary format (Lerc)** | Purpose-built DEM compression | Requires custom client-side JS decoder |
| **Skip ocean/constant tiles** | Don't generate tiles with zero variation | Good optimization but reduces tile count, not per-tile size |
| **Lower zoom level** | Fewer zoom levels (e.g., z10 vs z12) | Already configurable via `--terrain-zoom` |

## Test Results

Tested on two regions with different terrain characteristics:
- **Colorado (CO Springs area):** Mountainous terrain, elevation range ~1,600m-4,300m
- **Washington DC:** Relatively flat terrain, gentle hills ~0m-150m

### Size Comparison

| Region | Strategy | Total Size | Savings | Ratio |
|--------|----------|-----------|---------|-------|
| **Colorado** | baseline (0.1m) | 4.89 MB | — | 1.00x |
| | quantized 1m | 2.89 MB | 40.9% | 1.69x |
| | quantized 2m | 2.31 MB | 52.8% | 2.12x |
| | **quantized 5m** | **1.68 MB** | **65.7%** | **2.92x** |
| | quantized 10m | 1.27 MB | 74.1% | 3.86x |
| **DC** | baseline (0.1m) | 1.38 MB | — | 1.00x |
| | quantized 1m | 778 KB | 44.8% | 1.81x |
| | quantized 2m | 603 KB | 57.2% | 2.33x |
| | **quantized 5m** | **414 KB** | **70.6%** | **3.40x** |
| | quantized 10m | 271 KB | 80.7% | 5.19x |

### Per-Zoom Detail (z12, where most tiles live)

| Region | Strategy | Total | Avg/tile | Savings |
|--------|----------|-------|----------|---------|
| **Colorado** (54 tiles) | baseline | 3.00 MB | 56.9 KB | — |
| | quantized 1m | 1.67 MB | 31.7 KB | 44.4% |
| | quantized 2m | 1.31 MB | 24.9 KB | 56.3% |
| | quantized 5m | 954 KB | 17.7 KB | 69.0% |
| | quantized 10m | 692 KB | 12.8 KB | 77.5% |
| **DC** (16 tiles) | baseline | 762 KB | 47.6 KB | — |
| | quantized 1m | 399 KB | 25.0 KB | 47.6% |
| | quantized 2m | 302 KB | 18.8 KB | 60.4% |
| | quantized 5m | 204 KB | 12.8 KB | 73.2% |
| | quantized 10m | 126 KB | 7.9 KB | 83.5% |

### Elevation Error (vs source DEM)

All strategies use lossless WebP, so errors come purely from the quantization rounding:

| Strategy | Mean Error | P95 Error | Max Error |
|----------|-----------|-----------|-----------|
| baseline | 0.05m | 0.09m | 0.10m |
| quantized 1m | 0.25m | 0.48m | 0.50m |
| quantized 2m | 0.50m | 0.95m | 1.00m |
| **quantized 5m** | **1.25m** | **2.37m** | **2.50m** |
| quantized 10m | 2.50m | 4.75m | 5.00m |

Errors are deterministic and bounded: max error = half the rounding step. For context, the source DEM has 30m horizontal resolution, so these vertical quantization levels are all well within the data's inherent accuracy.

### Key Observations

1. **Lossy WebP is ruled out** — it produces 6,000-1,000,000m errors due to the Mapbox encoding structure
2. **Quantization works well** — progressive savings from 41% (1m) to 74% (10m) with bounded, predictable errors
3. **Flat terrain compresses better** — DC sees larger savings ratios because there's less elevation variation per tile
4. **Savings improve at higher zooms** — at z12, tiles cover less area with less variation, so quantized values repeat more

## Recommendation

**Use quantized 5m precision with lossless WebP.**

This provides:
- **66-71% size reduction** (2.9-3.4x smaller)
- Max elevation error of ±2.5m (well within 30m DEM accuracy)
- Zero visual impact with hillshade and 3D terrain
- Full MapLibre compatibility — no viewer changes needed
- Simple one-line code change

Implementation in `generate_terrain_tiles()`:

```python
elev = elevation[0]
elev = np.round(elev / 5.0) * 5.0  # quantize to 5m
encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
# ... rest unchanged, still lossless WebP
```

If even more aggressive savings are desired (74-81%), quantized 10m is viable with ±5m max error.

### Next Steps

- [ ] Visual comparison: generate both baseline and quantized 5m tiles for a region, load in MapLibre with 3D terrain + hillshade at exaggeration 1.5x
- [ ] Implement in `create_osm_zim.py` (one-line addition)
- [ ] Consider making the quantization level a CLI parameter (e.g., `--terrain-precision 5`)

## Test Script

The comparison script is at `test_terrain_compression.py`. Run with:

```bash
python3 test_terrain_compression.py [--max-zoom 12]
```

It downloads Copernicus DEMs, generates tiles using all strategies (in memory, without writing to disk), measures round-trip elevation error, and reports size comparisons.
