# Routing node-table OOM — diagnosis & fix options

## Symptom

Computing a route in the viewer — even a short *local* one (e.g. ~2 km within
Palo Alto) — takes **20+ seconds "loading routing data"** and then the tab
**OOMs and reloads**.

## Root cause

Legacy routing graphs load the **global node-coordinate table** the first time a
route runs, regardless of how local the route is. For a spatial-v2 ZIM the node
coordinates live in sharded files `routing-data/nodes-scaled-NNN.bin`, and
`loadNodeShards()` does two memory-heavy things:

1. downloads **all** shards (California: `nodes-scaled-000.bin` 38 MB +
   `-001.bin` 22 MB = **~60 MB**), and
2. copies them into one `Int32Array(numNodes * 2)` — California has
   **7,887,092 nodes**, so the combined array alone is **~60 MB**.

Peak during the copy is the shards **plus** the combined array ≈ **~120 MB**,
and this happens in **two** places independently:

- the **routing Web Worker** (`routing-worker.js`), and
- the **main thread** (`index.html`, used for snap-to-nearest + the
  non-spatial fallback).

So a single route can transiently allocate **~240 MB** of node-coordinate data
across the two contexts. On a memory-limited tab (phones, and especially the
Kiwix iOS/macOS WKWebView) that OOMs and force-reloads. Because it's the
*global* table, route locality doesn't matter — a 2 km Palo Alto route pays the
same as San Diego → Eureka. The ~60 MB read from a 3 GB ZIM via the service
worker is the "20+ seconds."

This is **pre-existing routing-architecture behaviour**, not caused by the
Wikipedia / locate-button / marker viewer work (none of which touches routing).
It is the flip side of the `nodes_scaled` sharding: the shards were moved out of
the cells-index, but the worker/main-thread still pull all of them into one
full-size array.

## Resolution (2026-06-02)

New spatial builds use **SZCI v3 + SZRC v2**:

- Nodes are reindexed into cell-major order during packing.
- Each cell payload carries its own node coordinates, adjacency, edges, and
  geometries.
- The small SZCI index stores `base_node + node_count` for each cell. Readers
  resolve node ownership with a binary search over those ranges.
- Edges are rewritten to the cell-major node IDs during packing.
- Endpoint snapping orders cells by geometric lower bound and stops loading
  once no unloaded cell can beat the best candidate.
- The worker cache is bounded by resident bytes (`64 MB`) rather than a fixed
  number of cells, and cell I/O is capped at four concurrent requests.

There is no `routing-data/nodes-scaled-NNN.bin` table in v3. A local route no
longer downloads or allocates coordinates for the entire region.

The embedded viewer now bundles `routing-worker.js` inside every new ZIM.
Normal Kiwix use routes and snaps in that worker. The main thread retains a
lazy v3 fallback for WebViews where worker startup fails.

Legacy SZCI v1/v2 readers remain for existing ZIMs, but new region rebuilds
should use `--spatial-chunk-scale 10`.

## Verification

- `tests/test_spatial_chunking.py` covers cell-local coordinates, edge
  remapping, lazy routes, cache behavior, and boundary snapping.
- `tests/test_routing_worker_v3.py` runs the actual worker message protocol
  against a generated v3 graph and checks snap + route results.
- `cloud/validate_zim.py` identifies the SZCI version and warns on legacy
  eager-coordinate layouts.
- `cloud/validate_platforms.py` models legacy node shards separately from the
  v3 bounded cell cache.
