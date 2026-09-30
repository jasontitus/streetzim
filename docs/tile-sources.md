# Tile sources: StreetZim's tilemaker tiles vs OpenFreeMap

StreetZim can build from two kinds of OpenMapTiles vector tiles:

- **tilemaker**: our own tiles, made with tilemaker and
  `resources/tilemaker/process-openmaptiles.lua` from an OSM PBF. This is
  what production uses.
- **OpenFreeMap**: the Planetiler-built tiles openzim/maps ships, used with
  `--mbtiles`, or `--mbtiles-url` on Zimfarm (see
  `scripts/fetch-openfreemap-mbtiles.py` and [zimfarm.md](zimfarm.md#building-from-ready-made-tiles---mbtiles-url)).

Both render with the same viewer style, and routing, addresses and Wikidata
come from the PBF either way. The difference is **how much there is to
search and label**. This page measures it.

## Method

- **Area**: Monaco, the only small area OpenFreeMap publishes.
- **Same OSM data**: OpenFreeMap build `20260928_033001` and our tiles from
  the openstreetmap.fr Monaco extract, **both from OSM data of 2026-09-27**.
- **Same extent**: tilemaker was run over OpenFreeMap's exact bounds
  (`7.40858,43.48382,7.59567,43.75293`).
- **Same tiles**: only z14 tiles present in both files are compared, which
  here is all 162 of them.
- **Tool**: `tools/compare_tile_sources.py ours.mbtiles theirs.mbtiles`. It
  also runs StreetZim's real search extraction on the shared tiles, so you
  can re-run it for any area you have both tile sets for.

## Results

| what users can find | tilemaker | OpenFreeMap | tilemaker advantage |
|---|---|---|---|
| **Street names** (distinct) | 387 | 295 | **+31%** |
| **Named places, all kinds** (distinct POI-layer names) | 1,682 | 829 | **+103%** |
| … of which shops, amenities, transport etc. (POIs with a class) | 892 | 829 | +8% |
| … of which named buildings and other named objects without a POI class | 790 | 0 | only tilemaker |
| Settlements and localities (`place` names) | 12 | 10 | +20% |
| Water, peak names | 3 / 1 | 3 / 1 | same |
| Park names | 0 | 1 | OpenFreeMap +1 |
| House-number labels on the map | 566 | 158 | +258% |
| Building footprints at z14 | 4,717 | 126 (merged) | individual outlines |

What the differences are:

- **The +31% street names** are mostly footpaths and stairways (57, for
  example "Escalier du Carnier"), service roads (19) and minor streets (16).
  OpenFreeMap's z14 street-name layer leaves most of these out. In a city of
  stairs and passages, they are how people give directions.
- **The +103% named places** break down as follows:
  - About 790 are **named buildings and residences** ("Villa Theodora",
    "Palais de la Mer", "Castel Lorraine"). Our profile's catch-all writes
    any named building or object to the POI layer; OpenFreeMap's doesn't.
    They are real search targets, since in Monaco addresses are often given
    by building name, but they are not shops or services.
  - For classed POIs the gain is **+8%**: bus stops (51), shops (26),
    amenities (20).
  - OpenFreeMap has 79 names we lack, mostly **offices** (42: companies,
    embassies).
- **House numbers and buildings** only affect how the map looks. Address
  *search* comes from the PBF with either source.

Where OpenFreeMap is ahead:

- **About 12% fewer tile bytes** in the shared tiles.
- **Native-script `name`** is present. Our tiles carry only `name:latin` and
  `name_int`, which matters if we add native-script search.
- **Offices** are included as POIs.
- **No tilemaker run**: no multi-day planet build, no tilemaker or shapefile
  setup, and fresh tiles every week.

## What this means

- **For search, tilemaker is clearly better.** On Monaco it gives 31% more
  street names and about twice as many named places. The extra places are
  mainly named buildings, which OpenFreeMap has none of.
- **For the map picture, the two are nearly identical.** The screenshots
  at z13, z16 and z17 differ only in which POIs get labels and in merged
  building outlines.
- **StreetZim keeps tilemaker as its default and supports OpenFreeMap as
  an input.** CI builds both on every push.
- **For openzim/maps, which stays on OpenFreeMap**, most of the gap can be
  closed without changing tile source. Named buildings, footpath names and
  offices are all in the OSM PBF, which a routing-enabled maps2zim would
  download anyway. StreetZim already takes addresses from the PBF the same
  way.

Monaco is small and unusual: dense, with many named buildings. The ratios
will differ elsewhere. Run `tools/compare_tile_sources.py` on a larger area
cut from both planets before deciding anything that depends on the exact
numbers.
