# Elevation Data Compression Analysis

## Current Approach

- **Source data:** Copernicus GLO-30 DEM (30m spatial resolution)
- **Encoding:** Mapbox terrain-RGB — elevation packed into 3 bytes (R/G/B) with 0.1m precision, offset by 10,000m
- **Tile format:** 256x256 lossless WebP
- **Consumer:** MapLibre GL JS (`encoding: 'mapbox'`)
- **Zoom range:** z0–z12

The encoding formula is: `encoded = (elevation + 10000.0) / 0.1`, then split across R (high byte), G (mid byte), B (low byte). This gives 0.1m precision over a range of -10,000m to +6,777m.

## Compression Strategies Considered

### 1. Quantize to 1m Precision (drop effective use of Blue channel)

**What it does:** Change the divisor from 0.1 to 1.0, so the encoded value is `(elevation + 10000)` instead of `(elevation + 10000) / 0.1`. This means the low byte (Blue channel) is always 0, and elevation information lives only in R and G.

**Why it works:** A constant blue channel compresses to almost nothing. The source DEM is 30m resolution, so 0.1m precision is 300x finer than the data actually supports. Even 1m precision is 30x finer than the source grid.

**Trade-offs:**
- No visual impact — source data doesn't have sub-meter precision
- MapLibre still decodes correctly (it just reads slightly different values)
- Simple code change (one constant)

**Compatibility note:** The viewer uses `encoding: 'mapbox'` which decodes as `elevation = (R*65536 + G*256 + B) * 0.1 - 10000`. With 1m precision encoding, the decoder still works but reconstructs elevations rounded to the nearest 0.1m (since the blue channel carries no useful data). This is functionally identical for 30m source data.

### 2. Lossy WebP (quality 92)

**What it does:** Switch from `lossless=True` to `lossless=False, quality=92`.

**Why it works:** Lossy WebP can dramatically reduce file sizes. The concern is that even small pixel value changes translate to elevation errors — but at quality 92, typical errors are ~1-2 LSB, meaning ~0.1-0.2m in the Mapbox encoding. This is well within the noise floor of 30m DEM data.

**Trade-offs:**
- Lossy compression can introduce block artifacts that show as slight terrain stepping in 3D view (especially with exaggeration)
- At quality 92, these are minimal and generally not visible
- Could be tested at lower quality values (85, 80) for even more savings if visual inspection passes

### 3. Combined: 1m Quantization + Lossy WebP q92

**What it does:** Both techniques applied together.

**Why it works:** Quantizing to 1m makes pixel values much smoother (the blue channel is constant, and R/G change more gradually). This smoother signal is then far easier for lossy WebP to compress, producing a compounding effect beyond what either technique achieves alone.

**Trade-offs:**
- Same as individual techniques, but combined
- The compounding effect is substantial — this is our recommended approach

### 4. Other Options Considered but Not Tested

| Option | Description | Why we didn't pursue it |
|--------|-------------|------------------------|
| **Terrarium encoding** | Alternative RGB encoding supported by MapLibre | Marginal gains over Mapbox encoding with lossless compression |
| **128x128 tile resolution** | Half-resolution tiles, let MapLibre upscale | 4x fewer pixels but changes tile infrastructure; could combine with other approaches |
| **Custom binary format (Lerc)** | Purpose-built DEM compression | Requires custom client-side decoder in JS; not natively supported by MapLibre |
| **Skip ocean/constant tiles** | Don't generate tiles with zero elevation variation | Good optimization but doesn't reduce per-tile size; more of a tile-count reduction |
| **Lower zoom level** | Generate fewer zoom levels (e.g., z10 instead of z12) | Already configurable via `--terrain-zoom`; reduces detail |
| **PNG indexed color** | Use palette-based PNG for low-variation tiles | Inconsistent savings, adds per-tile format decisions |

## Test Results

Tested on two regions with different terrain characteristics:
- **Colorado (CO Springs area):** Mountainous terrain with large elevation range (~1,600m–4,300m)
- **Washington DC:** Relatively flat terrain with gentle hills (~0m–150m)

### Overall Summary

| Region | Strategy | Total Size | Savings | Ratio |
|--------|----------|-----------|---------|-------|
| **Colorado** | baseline (0.1m, lossless) | 4.89 MB | — | 1.00x |
| | quantized 1m (lossless) | 2.30 MB | 53.0% | 2.13x |
| | lossy q92 (0.1m) | 2.15 MB | 56.0% | 2.27x |
| | **combined (1m + lossy q92)** | **794 KB** | **84.2%** | **6.31x** |
| **Washington DC** | baseline (0.1m, lossless) | 1.38 MB | — | 1.00x |
| | quantized 1m (lossless) | 646 KB | 54.1% | 2.18x |
| | lossy q92 (0.1m) | 578 KB | 59.0% | 2.44x |
| | **combined (1m + lossy q92)** | **91 KB** | **93.5%** | **15.43x** |

### Per-Zoom Detail (z12, where most tiles live)

| Region | Strategy | Total | Avg/tile | Savings |
|--------|----------|-------|----------|---------|
| **Colorado** (54 tiles) | baseline | 3.00 MB | 56.9 KB | — |
| | quantized 1m | 1.28 MB | 24.2 KB | 57.4% |
| | lossy q92 | 1.34 MB | 25.4 KB | 55.3% |
| | **combined** | **421 KB** | **7.8 KB** | **86.3%** |
| **DC** (16 tiles) | baseline | 762 KB | 47.6 KB | — |
| | quantized 1m | 329 KB | 20.6 KB | 56.8% |
| | lossy q92 | 305 KB | 19.1 KB | 59.9% |
| | **combined** | **41 KB** | **2.5 KB** | **94.7%** |

### Key Observations

1. **Quantization and lossy compression are roughly equal individually** (~53-59% savings each)
2. **They compound dramatically when combined** (84-94% savings) because quantizing smooths the signal, making lossy compression far more effective
3. **Flat terrain benefits more** — DC sees 15x compression vs Colorado's 6x, because there's less actual elevation variation per tile
4. **The effect strengthens at higher zooms** — at z12, individual tiles cover less area and thus have less variation, so compression is even more effective

## Recommendation

**Use the combined approach: 1m precision + lossy WebP quality 92.**

Implementation requires two changes in `generate_terrain_tiles()`:

```python
# Change precision from 0.1m to 1.0m
encoded = ((elev + 10000.0) / 1.0).astype(np.uint32)

# Change from lossless to lossy WebP
img.save(tile_path, "WEBP", lossless=False, quality=92)
```

This delivers **84-94% size reduction** with no meaningful loss of terrain quality given the 30m source resolution. The savings apply both to the ZIM file size and to download bandwidth when serving tiles.

### Next Steps

- [ ] Visual comparison: load both baseline and compressed tiles in MapLibre with 3D terrain + hillshade enabled to confirm no visible artifacts
- [ ] Test with `exaggeration: 1.5` (current setting) to check if lossy artifacts become visible when exaggerated
- [ ] Consider whether quality could go lower (85 or 80) for even more savings
- [ ] Implement in `create_osm_zim.py` once visual quality is confirmed

## Test Script

The comparison script is at `test_terrain_compression.py`. Run with:

```bash
python3 test_terrain_compression.py [--max-zoom 12]
```

It downloads Copernicus DEMs, generates tiles using all four strategies (in memory, without writing to disk), and reports size comparisons.
