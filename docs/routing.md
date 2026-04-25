# Routing in the StreetZim Drive PWA

This document describes how driving directions are computed in the
`streetzim.web.app/drive/` PWA, and how the implementation was tuned
to work on iOS Safari with its hard ~1.5 GB JS-heap ceiling.

## Algorithm chain

`findRoute()` in `resources/viewer/index.html` runs a **strategy
chain**: try the cheapest, most accurate option first, fall back only
when one fails to converge inside its budget. As of 2026-04-25 the
chain is:

1. **Full A*** — single-source A* over the entire spatial graph,
   sparse-state. Internally itself two-stage:
   1. *Optimal pass*: admissible heuristic (haversine ÷ 80 km/h),
      pop limit 200,000. Returns the guaranteed-shortest route.
   2. *Greedy fallback*: heuristic × 1.5 (no longer admissible —
      may overshoot), pop limit 400,000. Routes here are 5–15%
      longer than optimal but the page survives.
2. **Two-pass** — only invoked when full A* exhausts both stages.
   Picks a "highway entry" near each endpoint via outgoing-edge BFS,
   then runs three legs: src → hw_src on the full graph (admissible
   then greedy), hw_src → hw_dst on a highway-tier-only filter, and
   hw_dst → dst back on the full graph. Per-phase compaction drops
   cells between legs.

The chain is biased toward correctness: an admissible-A* answer is
returned whenever it fits in the budget. We only degrade to greedy
or two-pass when the optimal pass would have crashed the page.

### Highway-tier filter

Edges carry a `class_access` u32 from `CLASS_ORDINAL` in
`create_osm_zim.py` (bits 0..4). The two-pass middle leg only
expands edges whose ordinal is in {1..6} (motorway, trunk, primary,
and the corresponding `*_link` variants). On Japan this collapses
the search from ~18M nodes to a few hundred thousand highway
nodes — enough that even a 800 km cross-country route fits in
the budget.

### Suboptimality of greedy mode

Multiplying the heuristic by `w > 1` makes A* "weighted" — it
prefers expanding nodes closer to the goal. Routes are bounded by
`w × optimal` in worst case. With `w = 2.0` on the highway filter,
the worst case is twice the optimal distance, but in practice on
real road networks the overshoot is single-digit percent.

The differential test harness (see below) confirms this empirically:
on every route where full A* (`w=1.0`) returns a path, the chain's
default mode returns the same path byte-for-byte. Greedy only
appears when full A* doesn't converge.

## Memory budget

iOS Safari discards a tab when its JS heap stays near the limit
(~1.5 GB on iPhone Pro, less on older devices). The router has to
stay well under that. Per-route memory budget at peak:

| Component | Size | Comment |
|---|---|---|
| Cell cache (cap = 4) | ~140 MB | Each Japan 1° cell is ~36 MB JS-decoded. |
| Visited-node Maps | up to ~300 MB | `g`/`prev`/`prevEdge`/`closed`. ~440 B per visited node × pop limit. |
| MapLibre tiles + DOM | ~100 MB | Constant-ish. |
| **Routing peak** | **~500–600 MB** | Measured on Tokyo→Oita with the harness. |

Knobs in `resources/viewer/index.html`:

* `SpatialGraph` constructor: `cacheLimit = 4`. Drops cells aggressively
  during long-distance routing. Re-fetches are cheap if the user
  routes again across the same corridor.
* Per-phase compaction: `graph.compact(4)` between two-pass legs;
  `graph.compact(0)` before any route with crow-fly > 100 km
  (the "pre-route cleanup" pause + GC yields).
* Pop limits: 200k optimal / 400k greedy on full A*; 50k optimal /
  100k greedy on the highway-only middle leg.
* Sparse-state algorithm: `Map<int, X>` instead of typed arrays
  sized for `numNodes`. Eliminates the ~370 MB up-front allocation
  the old code paid even on a 1.5 km route.

## Debug instrumentation

### `?debug=1`

Drops a fixed-position green overlay in the bottom-right corner of
the viewer. Updates every 2 seconds when idle and on every 2,000
A* expansions during a route. Lines:

```
A* highway-only greedy×2 · 174,000 nodes · 4 cells
elapsed: 89.4s
pops: 174000
cells: 4 / cap 4 = 144 MB
est. visited Maps: 73 MB
est. heap (no Safari): ~832 MB (144 cells + 73 visit, ×2 overhead)
```

The flag is sticky via `localStorage` — set it once on any URL
(`?debug=1`) and it survives the picker → viewer redirect. Turn
off with `?debug=0`.

### Build stamps

Every screen shows the deploy stamp:

* **Marketing site** (`streetzim.web.app/`) — at the bottom of
  the "Last updated" line.
* **PWA picker** (`/drive/`) — in the footer.
* **Viewer** (`/drive/viewer/`) — green badge in the top-left
  corner. Click to copy.

The stamp is `<git-short>[-d<HHMMSS>]` from
`cloud/deploy_pwa.sh`. The `-dHHMMSS` suffix appears whenever the
working tree is dirty, so back-to-back deploys from an uncommitted
state always produce a new `SHELL_CACHE` key (otherwise the SW
silently re-served stale viewer JS — that bug ate hours on
2026-04-25 before we noticed).

### Service worker is network-first

`web/drive/sw.js` runs `fetch(req, { cache: 'no-store' })` for all
shell requests when online. The cache only kicks in when the
network fails. This means a successful Firebase deploy is
immediately visible — no `?bust=1` dance — at the cost of one
network round-trip per asset. Worth it for development; for a
production-only release we could revert to cache-first.

### Routing status panel

The Directions panel shows live progress during long routes:

```
A* full optimal · 174,000 nodes · 4 cells
```

Updates every 2,000 pops alongside the debug overlay. Saves you
from staring at "Calculating route..." for 30+ seconds with no
signal of activity.

## Headless test harness

Two files in `cloud/`:

* `route_browser_test.mjs` — drives a single mode (default / full /
  two-pass) through a labelled set of routes. Captures peak heap,
  peak cells, and timing per route.
* `route_compare.mjs` — runs the harness twice (default + full)
  and prints a side-by-side delta table.

Setup (one-time):

```bash
npm install puppeteer
```

Run:

```bash
# Pick any region (route sets are in route_browser_test.mjs ROUTE_SETS):
ZIM_URL=http://localhost:8765/osm-japan-chips-v2.zim ROUTES=japan \
  node cloud/route_compare.mjs
```

Prereqs:

1. `cloud/serve_zims.py` running on port 8765 (or any HTTP server
   that serves the .zim with byte-range support).
2. System Chrome installed at the standard macOS path. Override
   with `CHROME_PATH=/path/to/Chrome`. (The puppeteer-bundled
   Chromium is sandboxed off the network in some CI shells; system
   Chrome inherits the user's network policy.)

The harness loads the picker, fetches the ZIM, posts it to the SW
via the same `set-zim` message the picker uses, then navigates to
the viewer with `?debug=1` and runs each route via
`window.streetzimRouting.setOrigin/setDest`.

### Test results (2026-04-25)

24/24 routes pass across 5 large maps. Default mode matches optimal
full-A* exactly on every route where full converges; degrades to
greedy/two-pass only when full bails:

| Region | Routes | Default = Full | Notable |
|---|---|---|---|
| Japan | 5/5 | 4 exact + 1 graceful fallback | Oita→Tokyo: full bails @ 200k pops; default's two-pass returns 1248 km in 12 s |
| Texas | 5/5 | 5 exact | El Paso→Houston: 1225 km, 31 s |
| Central-US | 4/4 | 4 exact | SLC→Albuquerque: 965 km, 2.4 s |
| West-Asia | 5/5 | 5 exact | Tehran→Baghdad cross-border: 847 km, 8 s |
| Australia-NZ | 5/5 | 5 exact | Brisbane→Cairns: 1697 km, 5 s |

Peak heap on the longest route in each region stayed under 600 MB,
well below the iOS Safari ceiling.

## Files

* `resources/viewer/index.html` — viewer JS, including
  `findRouteSpatial`, `findRouteSpatialFiltered`,
  `findRouteSpatialTwoPass`, `findNearestHighwayNode`,
  `SpatialGraph.compact()`.
* `web/drive/sw.js` — service worker, network-first.
* `cloud/route_cli.py` — Python prototype that mirrors the JS
  algorithm. Used as a differential reference while iterating
  on the JS port.
* `cloud/route_browser_test.mjs` — Puppeteer harness.
* `cloud/route_compare.mjs` — default-vs-full diff runner.
* `cloud/deploy_pwa.sh` — bumps `SHELL_CACHE` (with a dirty-tree
  marker) and runs `firebase deploy`.
