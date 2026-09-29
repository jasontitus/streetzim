# StreetZim data formats (frozen reference)

This is the contract between the builder (`create_osm_zim.py` and the
`cloud/` tools that write ZIMs) and every reader: the viewer
(`resources/viewer/index.html`, `routing-worker.js`), the PWA at
`/drive/` (which serves the *current* viewer against *any* ZIM a user
opens), and the Python reference readers used by tests.

Rule for maintainers: **a change to any layout below is a new version
number, never an edit in place.** Readers must keep accepting every
version that is still in a published ZIM (see
[Version support](#version-support-and-retirement)). Update this file in
the same commit as the writer.

All byte layouts were derived from the writer code; line references are
to the functions, which are the source of truth if this file ever
disagrees.

## Conventions

- Every integer is **little-endian**. Magics are 4 ASCII bytes (JS checks
  them as a big-endian u32, e.g. `SZRG` = `0x535A5247`).
- Every typed section starts **4-byte aligned**, because the JS readers
  build `Int32Array` / `Uint32Array` views directly on the buffer.
- Coordinates are `round(degrees * 1e7)` stored as i32. Node arrays are
  **(lat, lon)** pairs; geometry blobs are **(lon, lat)**.

### Geometry blob encoding

`_encode_geom` in `extract_routing_graph` (`create_osm_zim.py`):

- First point: `<ii` = `lon_e7, lat_e7`.
- Each later point: `varint(zigzag32(dlon))`, `varint(zigzag32(dlat))`.
  varint is LEB128 (7 bits per byte, `0x80` = continue);
  `zigzag32(n) = ((n << 1) ^ (n >> 31)) & 0xFFFFFFFF`.
- A longitude delta over 180° is wrapped the short way, so a decoder keeps
  a running longitude that may pass ±180°.
- A geometry holds **interior points only**; its endpoints are the edge's
  two nodes.
- Geometries are deduplicated by exact encoded bytes; the reverse
  direction of a two-way road is a separate geometry.
- Geometry `k` is `blob[geom_offsets[k] : geom_offsets[k+1]]`. Once the
  blob reaches `0xFFFF0000` bytes, later edges get no geometry.

### Edge record (20 bytes, 5 × u32)

| word | field |
|---|---|
| 0 | `target` node id |
| 1 | `dist_speed = (speed_kmh & 0xFF) << 24 \| min(dist_dm, 0xFFFFFF)`; `dist_dm` is the haversine length in decimetres |
| 2 | `geom_idx`, `0xFFFFFFFF` = none |
| 3 | `name_idx` (0 = no name) |
| 4 | `class_access`, below |

`class_access` bits:

| bits | meaning |
|---|---|
| 0–4 | road class ordinal (`CLASS_ORDINAL`): motorway 1, motorway_link 2, trunk 3, trunk_link 4, primary 5, primary_link 6, secondary 7, secondary_link 8, tertiary 9, tertiary_link 10, residential 11, living_street 12, unclassified 13, service 14, track 15, path 16, footway 17, cycleway 18, pedestrian 19, steps 20; anything else 0 |
| 5 | `foot=no` |
| 6 | `bicycle=no` |
| 7 | one-way (set for both `oneway=yes` and `oneway=-1`, and implied for roundabouts and motorways) |
| 8 | roundabout / circular / mini_roundabout |
| 9 | no motor vehicles (OSM access hierarchy, or a class in `NO_MOTOR_HIGHWAY`); the car profile must skip these edges |
| 10–31 | zero (reserved) |

Speeds come from the `SPEED` table (`DEFAULT_SPEED` = 30 km/h).

### Nodes and names

- Nodes are junctions (used by 2+ ways, or a way endpoint), numbered by
  ascending OSM node id.
- Edges are stably sorted by source node; `adj_offsets` is CSR, u32[N+1]:
  node `n`'s out-edges are `adj[n] .. adj[n+1]`.
- Names: index 0 is `""`. A label is `"name (ref)"` when both tags exist,
  otherwise whichever exists. UTF-8; `name_offsets` is u32[M+1].

## Routing graph formats

### SZRG v4 — `routing-data/graph.bin` (default for `--routing`)

Writer: `extract_routing_graph`, `create_osm_zim.py`.

| offset | type | field |
|---|---|---|
| 0 | char[4] | `SZRG` |
| 4 | u32 | version = 4 |
| 8 | u32 | N nodes |
| 12 | u32 | E edges |
| 16 | u32 | G geometries |
| 20 | u32 | B geometry bytes (includes 0–3 zero pad bytes; `geom_offsets[G]` is the unpadded end) |
| 24 | u32 | M names |
| 28 | u32 | S name bytes |

Then: nodes i32[2N] · adj_offsets u32[N+1] · edges u32[5E] ·
geom_offsets u32[G+1] · geom_blob u8[B] · name_offsets u32[M+1] ·
names u8[S].

### SZRG v5 + SZGM v1 — split graph (read-only; writer retired 2026-09)

`graph.bin` has the v4 header with `version = 5` and **B = 0** (G is kept
so `geom_idx` still means something) and omits the geometry sections.
Geometries move to `routing-data/graph-geoms.bin`:

| offset | type | field |
|---|---|---|
| 0 | char[4] | `SZGM` |
| 4 | u32 | version = 1 |
| 8 | u32 | G (must equal the SZRG header's G) |
| 12 | u32 | B |
| 16 | u32[G+1] | geom_offsets |
| 16 + 4(G+1) | u8[B] | geom_blob |

No writer remains: `--split-graph` (builder and `cloud/repackage_zim.py`)
was not used by the production wrappers and now fails with a message.
Readers keep v5 support in case a published file carries one;
`cloud/validate_zim.py` flags any that does.

### SZCI v3 + SZRC v2 — spatial cells (`--spatial-chunk-scale N`, what production ships)

Writer: `build_spatial` in `streetzim/routing/spatial.py` (called by
`create_osm_zim.py` and `cloud/repackage_zim.py`; see
[Known debt](#known-debt)). The SZRG v4 file is only an intermediate
input in this mode.

`routing-data/graph-cells-index.bin`:

| offset | type | field |
|---|---|---|
| 0 | char[4] | `SZCI` |
| 4 | u32 | version = 3 |
| 8 | u32 | N |
| 12 | u32 | E |
| 16 | u32 | M names |
| 20 | u32 | S name bytes |
| 24 | u32 | C cells |
| 28 | i32 | cell_scale (10 ⇒ 0.1° cells) |
| 32 | 24 B × C | per cell: i32 lat_cell, i32 lon_cell, u32 base_node, u32 node_count, u32 edge_count, u32 geom_count |
| 32 + 24C | u32[M+1] | name_offsets |
| … | u8[S] | names |

Cell rules: `cell = (floor(lat_e7 * scale / 1e7), floor(lon_e7 * scale / 1e7))`;
cells sorted ascending by (lat_cell, lon_cell); empty cells omitted;
nodes renumbered cell-major, so `base_node[i+1] = base_node[i] + node_count[i]`
and a node's cell is found by binary search on `base_node`.

`routing-data/graph-cell-NNNNN.bin` (5-digit cell id):

| offset | type | field |
|---|---|---|
| 0 | char[4] | `SZRC` |
| 4 | u32 | version = 2 |
| 8 | u32 | cell_id |
| 12 | u32 | n nodes |
| 16 | u32 | e edges |
| 20 | u32 | g geometries |
| 24 | u32 | b geometry bytes |
| 28 | i32[2n] | coords (lat, lon); local node `i` is global `base_node + i` |
| … | u32[n+1] | local CSR into this cell's edges |
| … | u32[5e] | edges (`target` is a global cell-major id, `geom_idx` is cell-local) |
| … | u32[g+1] | geom_offsets |
| … | u8[b] | geom_blob (last, unpadded) |

A geometry used by edges in two cells is copied into both. SZRC v2 is only
valid with SZCI v3 (it needs `base_node`).

### Legacy spatial versions (read-only; no writer remains)

- **SZCI v1**: v3 header with version 1, then `nodes_scaled` i32[2N] in
  original node order, then 20-byte cell records (no `base_node`), then
  names.
- **SZCI v2** (written by `build_spatial` from 2026-05-03 to 2026-06-02, and by `cloud/upgrade_spatial_zim.py`, retired in `056261e` and removed in `a436aa8`): 40-byte header (the seven
  v1 fields, then u32 `num_node_shards`, u32 `nodes_per_shard`), no inline
  nodes; coordinates live in `routing-data/nodes-scaled-NNN.bin` (raw i32
  lat/lon pairs).
- **SZRC v1**: 28-byte header with version 1, then u32 global node ids[n]
  (no coordinates; they come from `nodes_scaled`), then the CSR, edges
  (global targets), geom_offsets and blob as in v2. Pairs with SZCI v1/v2.

### SZRG v2 / v3 (read-only, pre-April 2026)

Same header as v4. v3 edges are 4 words (no `class_access`); v2 edges are
`[target, dist_dm, speed << 24 | geom_idx24, name_idx]`.

### Chunked graphs — `routing-data/graph-chunk-manifest.json`

Written by `chunk_graph_file` when `--chunk-graph-mb > 0` on a non-spatial
build, **in addition to** `graph.bin`:

```json
{"schema": 1, "total_bytes": 123, "sha256": "<hex of the whole file>",
 "chunks": [{"path": "graph-chunk-0000.bin", "bytes": 123}]}
```

Chunk paths are relative to the manifest. v5 adds
`graph-geoms-chunk-manifest.json` the same way. The JS reader checks sizes
but not the sha256; `streetzim/routing/reader.py` checks both.

### Compression

Routing entries of 200 MB or more are stored in uncompressed clusters:
the PWA's zstd decoder fails on clusters over about 500 MB, and Kiwix
WebViews time out decompressing them. See `docs/zim-packaging-gotchas.md`.

## Version support and retirement

**The builder writes only the current versions**: SZRG v4 for plain
`--routing`, SZCI v3 + SZRC v2 with `--spatial-chunk-scale` (what the
`streetzim` command and every production wrapper use).
`tests/test_current_formats.py` checks this. Older versions are read-only:
they exist in StreetZim's already-published ZIMs, which get the current
viewer (in-place patches, the PWA), so the viewer keeps reading them. A ZIM
built today, by StreetZim or by openZIM, never needs a legacy branch.

| format | writer | readers | status |
|---|---|---|---|
| SZCI v3 + SZRC v2 | `build_spatial` (`--spatial-chunk-scale`) | JS, Python | canonical |
| SZRG v4 (+ chunk manifest) | `extract_routing_graph` (plain `--routing`) | JS, Python | current (small regions) |
| SZRG v5 + SZGM v1 | none (retired 2026-09) | JS, Python | drop the readers once `validate_zim` finds no `routing-data/graph-geoms*` in the catalog |
| SZCI v1/v2, SZRC v1 | none (`upgrade_spatial_zim.py` removed in `a436aa8`) | JS, Python | in continent ZIMs built before 2026-06-02; drop after those are rebuilt |
| SZRG v2/v3 | none | JS, Python | pre-April 2026 files only; first candidate for removal |

### Where the legacy read branches are

Everything to delete when a format above is dropped. The viewer's are what
matter for users; the Python ones are reference readers for tests and
`cloud/validate_zim.py`.

| format | viewer (`resources/viewer/`) | Python |
|---|---|---|
| SZRG v2/v3 | `src/index/510-routing-graph-formats.js`: `edgeStride` (4-word edges) and the `version === 2` edge decoding in the graph parser | `streetzim/routing/reader.py`: the `version == 2` branches of `SZRG.edge_*`, `no_geom`, and the 4-word stride in `parse_szrg_bytes` |
| SZRG v5 + SZGM | `510-…`: `attachGeoms` and the "v5 without companion" path; `520-routing-graph-load-and-snap.js`: the v5-split fallback; `540-routing-worker-bridge.js`: waiting for SZGM before drawing | `reader.py`: `parse_szgm_bytes`, `SZRG.attach_geoms`, the v5 branches of `load_from_zim` / `load_from_file`; `tests/test_szrg_v5_split.py` + `tests/v4_to_v5_convert.py` |
| SZCI v1/v2, SZRC v1 | `510-…`: the `version === 1` / `version === 2` branches of the spatial index and cell parsers, `loadNodeShards` (`nodes-scaled-NNN.bin`); `520-…`: the node-shard fetch; `routing-worker.js`: the same branches in its SZCI/SZRC parsers and its shard loader | `streetzim/routing/spatial.py`: the v1/v2 branches of `parse_szci`, the v1 branch of `parse_szrc`, `SZRCCell.cell_nodes_global`, and the non-v3 paths of `SZCIIndex.cell_for_node` / `SpatialGraph.node_coords_e7` / `nodes_scaled` |

`cloud/validate_zim.py` reports which layout a ZIM carries, and warns on
the legacy ones ("legacy spatial SZCI vN", "legacy monolithic SZRG vN"; v5
also by its `graph-geoms` companion), so run it over the live catalog before
removing a reader branch. The PWA serves the current viewer to old files, so a
reader branch is only dead when no published ZIM needs it.

## Other ZIM entries

| path | content |
|---|---|
| `index.html`, `places.html`, `routing-worker.js` | viewer, padded into fixed uncompressed slots with an `SZVSLOT1` marker so `cloud/patch_viewer_inplace.py` can replace them in a published ZIM (`docs/viewer-slots.md`) |
| `maplibre-gl.js`, `maplibre-gl.css` | MapLibre GL JS (vendored; version in `resources/viewer-assets.lock.json`) |
| `mapbox-gl-rtl-text.js` | MapLibre's RTL text plugin (Arabic/Hebrew shaping), vendored and pinned the same way, behind a comment carrying its licence; the viewer loads it only once a tile has RTL text. Older ZIMs lack it; `cloud/repackage_zim.py` adds it with the viewer swap |
| `map-config.json` | name, center, zoom, minZoom, maxZoom, buildDate, bounds, `hasSatellite`/`satelliteMaxZoom`/`satelliteFormat`/`satelliteTileSize`, `satelliteSource`/`satelliteYear`/`satelliteLicense`/`satelliteLicenseUrl`/`satelliteAttribution`/`satelliteNonCommercial` (which EOX mosaic and the credit it needs, `streetzim/satellite_sources.py`; absent in older ZIMs, which all carry the 2021 mosaic, CC BY-NC-SA 4.0), `hasTerrain`/`terrainMaxZoom`, `hasWikidata`, `hasRouting`, `hasOvertureAddresses`, `hasWikiArticles`, `rtlTextPlugin` (the RTL plugin's entry path, `mapbox-gl-rtl-text.js`; absent in older ZIMs, whose viewer then leaves RTL labels unshaped); `title`, `description` (the ZIM's Title/Description metadata) and `generator` (`streetzim <version>`) for the viewer's About panel |
| `streetzim-meta.json` | build metadata for other consumers. `routingGraph.version` is the SZRG version of the intermediate graph (4 or 5), even when the ZIM ships SZCI v3 cells |
| `tiles/{z}/{x}/{y}.pbf` | OpenMapTiles-schema MVT; empty tiles are dropped; repeats of an identical tile are ZIM aliases of the first (`docs/tile-aliases.md`) |
| `satellite/{z}/{x}/{y}.{avif,webp}` | optional, uncompressed; repeats of an identical tile are ZIM aliases of the first, as for `tiles/` |
| `terrain/{z}/{x}/{y}.webp` | optional, Mapbox terrain-RGB; repeats are aliased too |
| `fonts/{Font}/{start}-{end}.pbf` | SDF glyphs (Open Sans Regular/Bold/Italic) |
| `search-data/manifest.json`, `search-data/{prefix}.json` | prefix-sharded search records `{n, t, s, a, o, l, …}`; see `docs/search-prefix-locality.md` |
| `category-index/manifest.json`, `category-index/{cat}.json`, `category-index/chip-{id}[…].json` | Find page data; chip ids come from `cloud/chip_rules.py`, shard layout from `cloud/chip_shards.py` |
| `wikidata/manifest.json`, `wikidata/{NN}.json` | optional Wikidata facts, bucketed by the first two digits of the Q-number |
| `wiki-article/{Title}`, `wiki-image/{sha1}.{ext}` | optional bundled Wikipedia (`cloud/wiki_articles.py`) |
| `wiki-geo-index.json` | `{title: [lat, lon, type]}` |
| `search/{slug}.html` | per-feature detail pages (libzim Xapian mode only) |
| `overture-sources.json` | Overture attribution, when Overture data was merged |

### Areas across the antimeridian

An area is one box. One across the antimeridian (Fiji, Chukotka,
Kiribati) is kept **unwrapped**: `minLon` in [-180, 180) and `maxLon`
past 180, so `minLon < maxLon` still holds; Fiji is
`[172.84, -23.12, 183.47, -11.24]`. `map-config.json` `bounds` and
`streetzim-meta.json` `bbox` carry it that way, and a reader that tests a
longitude against them also tests it plus 360°. `center` stays in
[-180, 180]. A box whose `maxLon` is at most 180 does not cross, and every
such ZIM is built exactly as before. `streetzim/area.py` holds the rules:
tools limited to [-180, 180] (osmium, tilemaker, MapLibre's source
`bounds`) get the two sides as separate boxes. Tile, search-record and
graph coordinates are always in [-180, 180]; a route or way geometry that
steps across ±180° is continued the short way (see geometry blobs above).

## Known debt

- `map-config.json` has no routing format field; readers probe for
  `graph-cells-index.bin`, then the chunk manifest, then `graph.bin`.
