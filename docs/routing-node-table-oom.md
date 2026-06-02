# Routing node-table OOM — diagnosis & fix options

## Symptom

Computing a route in the viewer — even a short *local* one (e.g. ~2 km within
Palo Alto) — takes **20+ seconds "loading routing data"** and then the tab
**OOMs and reloads**.

## Root cause

The routing graph loads the **global node-coordinate table** the first time a
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

## Status (2026-06-02)

- **Worker half: done** — `f2edb21` "routing(worker): index node coords from
  shards, drop the 60 MB combined copy". `loadNodeShards` now keeps the shard
  buffers separate and indexes the owning shard via `nodeLatE7`/`nodeLonE7`
  accessors (math cross-checked against the ZIM). Removes one of the two 60 MB
  allocations. **Committed + pushed; not yet deployed to web or baked into a
  ZIM.** Note: `swap_viewer_rust.py` only swaps `index.html`/`places.html`, so
  the routing-worker fix reaches a ZIM only via a deploy (web) and a rebuild or
  an extended swap (ZIM).
- **Main thread: not started** — `index.html` still builds the combined array
  and has ~30 flat-access sites across two graph types (monolithic + spatial).

## Fix options

### Option 1 — finish the main-thread half (recommended now)

Apply the same shard-accessor change to the main-thread graph in `index.html`
(mirrors the worker fix): add `nodeLatE7`/`nodeLonE7` to the spatial parser +
`SpatialGraph`, stop building the combined array in `loadNodeShards`, and
rewrite the ~30 `nodesScaled[n*2]` access sites to the accessors (with a
fallback so the monolithic/non-spatial graph path keeps working).

- **Effect:** removes the second 60 MB allocation; peak ~240 MB → ~120 MB.
  Stops the OOM.
- **Does NOT fix** the ~20 s load — both contexts still download the full 60 MB
  node table.
- **Risk:** edits to live navigation code; the browser harness can confirm the
  graph *loads* cleanly but not that a route is *correct*, so a human spot-check
  of a real route is needed before shipping.
- **Verify:** Playwright harness clean-load + grep for zero remaining flat
  `nodesScaled[` in the spatial path; then a manual Palo Alto route on the web
  viewer before baking into the ZIM.

### Option 2 — ship the worker fix only (already committed)

Leave the main thread as-is.

- **Effect:** peak ~240 MB → ~180 MB. Lower risk, no further changes.
- **Downside:** the main-thread 60 MB array remains, so it may still OOM on the
  tightest devices.

### Option 3 — the proper fix: per-cell node coordinates

Re-bundle node coordinates **per routing cell** (each `graph-cell-NNNNN.bin`
carries its own nodes' coordinates) instead of a global `nodes-scaled` table, so
a local route loads only the handful of cells it touches (a few hundred KB),
never the whole 60 MB table.

- **Effect:** fixes **both** the 20 s first-route load **and** the OOM, and
  scales to every region — local routes become cheap everywhere.
- **Cost:** a build-format change in `create_osm_zim.py` (+ the routing graph
  emitter) and a full **rebuild** of each region's ZIM; worker + main-thread
  readers updated to read coords from cells.

## Recommendation

Do **Option 1** now to stop the crash (with a human route spot-check), then
schedule **Option 3** as the real load-time fix for the next rebuild cycle.
