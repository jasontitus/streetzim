# Routing in StreetZim

This document describes how driving directions are computed in the
embedded Kiwix viewer and `streetzim.web.app/drive/` PWA, and how the
implementation is tuned for memory-constrained mobile WebViews.

## Algorithm chain

`findRoute()` in `resources/viewer/index.html` runs a **strategy
chain**: try the cheapest, most accurate option first, fall back only
when one fails to converge inside its budget. As of 2026-04-25 the
chain is:

1. **Full A*** — single-source A* over the entire spatial graph,
   sparse-state. Internally a three-stage chain, each stage bounded
   by a pop budget (worker numbers; the main-thread fallback keeps
   smaller ones, see *Memory budget*):
   1. *Optimal pass* — admissible heuristic (haversine ÷ 100 km/h,
      the fastest edge speed `create_osm_zim.SPEED` emits, so the
      estimate can never exceed the true remaining time), 500,000
      pops, only for crow-fly ≤ 200 km. Returns the guaranteed-
      shortest route. Every Silicon Valley metro pair that the
      reference A* can route finishes here (20 of 25 random pairs up
      to 110 km; the other five are unreachable by car or need more
      than 500k expansions).
      (Until 2026-09 both JS engines divided by 80 km/h. That
      over-estimates remaining time on any motorway leg, so the
      "optimal" pass silently returned routes a few percent slower
      than the true optimum — but it also behaved like weighted A*
      at ×1.25, which is why it converged on 200–500 km pairs; see
      the next stage.)
   2. *Weighted pass* — heuristic × 1.25, 400,000 pops. Bounded to
      1.25 × optimal in theory; in practice it returns the exact
      optimum on Asheville → Wilmington (442 km, 137k pops, 2.0 s)
      and 6 % over on Raleigh → Charlotte (210 km, 381k pops). This
      is the stage the old 80 km/h heuristic really was; the first
      2026-09 revision went straight from the admissible pass to
      ×1.875 and returned routes 20 % slower than optimal on those
      pairs. Skipped above 800 km crow-fly (SD → Arcata pattern: no
      bounded pass finishes; greedy converges in ~60k pops).
   3. *Greedy fallback* — heuristic × 1.875 (the old engine's 1.5 on
      an 80 km/h heuristic re-expressed against the admissible one),
      500,000 pops. Two Silicon Valley hill pairs need 343k / 467k
      pops here and cannot use two-pass (no highway-tier edge within
      5,000 BFS pops), which is why the budget is not smaller. Routes
      here are 4–10 % longer than optimal.

   Each stage only runs when the previous one *bailed* on its pop
   budget. A stage that exhausts the open set has proved the
   destination unreachable and the chain stops. After the first
   bail (or before the weighted pass when the admissible one is
   skipped), `destComponentClosed` runs a bounded forward BFS from
   the destination. If it exhausts before 20,000 nodes without
   touching the origin, the cells the pocket touches are scanned for
   a drivable edge that enters the pocket from outside; only a pocket
   with no entrance is closed (ferry-only island, road cut at the
   extract boundary) and stops the chain. A one-way sink or parking
   loop has a one-node forward component but an entrance, so it
   routes normally (verified on ten real Silicon Valley sinks with
   the admissible pass forced to bail). Charlotte → Ocracoke Island
   on the Carolinas ZIM answers "no route" in 0.3 s; before this
   check it spent 43 s re-fetching 1.1 GB of cells through the
   two-pass fallback.
2. **Two-pass** — only invoked when the full chain bailed on its
   budget (any distance — it used to be gated on crow-fly > 100 km,
   which left dense-metro routes just under that with "No route
   found"). Picks a "highway entry" near each endpoint via
   outgoing-edge BFS, then runs three legs: src → hw_src on the full
   graph, hw_src → hw_dst on a highway-tier-only filter (optimal
   150k pops then greedy ×2 at 300k), and hw_dst → dst back on the
   full graph. Per-phase compaction drops cells between legs. Note
   that on a state-sized graph the highway-only leg touches cells
   across the whole state and thrashes the 64 MB cell budget; it is
   reached only when the destination is reachable-but-hard or in a
   large disconnected component.

The chain is biased toward correctness: an admissible-A* answer is
returned whenever it fits in the budget. We only degrade to weighted,
greedy or two-pass when the optimal pass would have crashed the page
or taken tens of seconds.

### Worker ↔ main-thread contract

* `route-done` carries `ok:true, result:null` when the search ran to
  completion without a route. `ok:false` means the engine threw, and
  only then does the bridge fall back to the main-thread engine.
  (Reporting "no route" as `ok:false` used to trigger a full
  main-thread re-run of the identical search — twice the wall time
  and iOS heap to reach the same "No route found".)
* A new `route` command cancels any route still running in the
  worker, and the bridge sends a `cancel` for its in-flight ids
  before posting a new one. Only the newest request ever matters
  (`computeAndDrawRoute` drops stale results via `routeSeq`).
* If the worker dies mid-route the bridge rejects every pending
  route/snap promise (callers fall back to the main thread) and pins
  the ready state to `false`; previously those promises hung forever
  and the panel stayed on "Finding fastest route…".
* `?route=full` / `?route=two-pass` reach the worker via
  `options.route`.

### Snapping and non-drivable edges

Both snappers (`snapNearestNode` in the worker and in
`index.html`) rank vertices by planar distance with longitude scaled
by cos(lat), and they never return a vertex the car A* could not
leave:

* an edge is *non-drivable* when `class_access` has bit 9 set
  (builders from 2026-09 on) **or** its class ordinal is 16..20
  (path, footway, cycleway, pedestrian, steps). Older ZIMs carry
  those ways in the car graph at 3–5 km/h, which used to route cars
  down staircases and — more often — snap the origin to a pier or
  footpath vertex and report "No route found". `isNoMotor()` is the
  single predicate every A*, BFS and snapper uses.
* the snap keeps the six nearest car-ok vertices and returns the
  first from which a bounded forward BFS reaches 32 nodes, provided
  it is no more than 1 km further away than the nearest. A
  four-node private-drive fragment or a footpath vertex right next
  to the road no longer wins the snap.
* the bridge tells the snapper which endpoint it is resolving
  (`mode: 'origin' | 'dest'`). The forward-reach test is the right
  question for an origin (can the car leave?) but the wrong one for
  a destination: the end of a one-way spur, a drop-off lane or a
  one-way road cut at the extract boundary has a drivable incoming
  edge and no outgoing one. For `dest` such an edgeless vertex is
  accepted when an edge in its own cell drives into it. On Silicon
  Valley, 234 vertices are like that; snapping their own
  coordinates as a destination now lands on all 40 sampled (the
  origin rule displaced 37 of them by 3–178 m).
* ties in distance are broken by scan order (nearer cell first,
  ascending local index) in both the JS and the Python snapper, so
  equal-distance candidates across a cell boundary resolve the same
  way. `tests/szrg_spatial.SpatialGraph.nearest_node` mirrors all of
  this and is what `cloud/route_cli.py` calls, so the differential
  harness snaps exactly like the viewer. Its `raw=True` form is a
  plain nearest-vertex lookup for harnesses that replay recorded
  vertex pairs by coordinate.

On the Silicon Valley ZIM the car-only rule moved random metro pairs
into the optimal pass (footways were inflating the search) and fixed
one pair that snapped onto a pier and returned no route.

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
| Cell cache (byte budget) | ≤64 MB | Scale-10 SZCI v3 cells are fetched lazily. |
| Visited-node table + heap (worker) | ~90–130 MB peak at the 500k budgets (each pass frees its table before the next allocates) | `NodeTable` in `routing-worker.js`: open-addressing hash on node id with typed-array columns for g / prev / prev-edge / closed, 21 B per slot, power-of-two capacity at ≤ 50 % load, ~1.4 entries per pop (inserted on relaxation) ⇒ a 4M-slot table (~88 MB, ~130 MB while it doubles) plus a `NodeHeap` at 12 B per push. The old `Map`-based state cost ~440 B per node, i.e. the same envelope at 400k pops. The main-thread fallback still uses `Map`s. |
| MapLibre tiles + DOM | ~100 MB | Constant-ish. |
| **Routing peak** | **~500–600 MB** | Measured on Tokyo→Oita with the harness. |

Knobs in `resources/viewer/index.html`:

* `SpatialGraph` constructor: `maxResidentBytes = 64 MB`. Drops cells
  aggressively during long-distance routing. Cell I/O is capped at four
  concurrent requests; route-critical fetches jump ahead of prewarm work.
* Per-phase compaction: `graph.compact(4)` between two-pass legs;
  `graph.compact(0)` before any route with crow-fly > 100 km in the worker (the main-thread fallback does the same)
  (the "pre-route cleanup" pause + GC yields).
* Pop limits (worker): 500k optimal / 400k weighted ×1.25 / 500k
  greedy ×1.875 on full A*; 150k optimal / 300k greedy ×2 on the
  highway-only middle leg. The main-thread fallback runs the same
  chain with the old Map-based state and budgets 200k / 200k /
  400k, and 50k / 100k on the highway-only leg. Both engines skip
  the admissible pass above 200 km crow-fly and the weighted pass
  above 800 km (`OPTIMAL_MAX_CROW_KM`). `options.popLimits` on the
  worker's `route` command shrinks the budgets for tests.
* Sparse-state algorithm: state only for visited nodes instead of
  typed arrays sized for `numNodes`. Eliminates the ~370 MB up-front
  allocation the old code paid even on a 1.5 km route. In the worker
  the state is a `NodeTable` (see above) plus a `NodeHeap` (parallel
  `Float64Array`/`Int32Array` binary heap), so the inner loop does no
  per-edge allocation.
* Synchronous fast path: the worker's A* reads edges and coordinates
  straight out of the resident cell's typed arrays and only `await`s
  on a genuine cell miss. The previous engine awaited a fresh promise
  (and allocated an edge array) on every pop and every relaxation,
  which dominated the per-pop cost. A one-entry "last cell" cache in
  front of the node→cell binary search covers the common same-cell
  case. On a 62 k-node synthetic grid the search phase got ~10×
  faster (`tests/test_routing_worker_v3.py` has the differential
  harness; see git history for the benchmark numbers).
* SZCI v3: node coordinates live inside cell payloads. Startup loads only
  compact cell metadata and names; there is no region-wide coordinate table.

## Debug instrumentation

### `?debug=1`

Drops a fixed-position green overlay in the bottom-right corner of
the viewer. Updates every 2 seconds when idle and on every 2,000
A* expansions during a route. Lines:

```
A* highway-only greedy×2 · 174,000 nodes · 4 cells
elapsed: 89.4s
pops: 174000
cells: 18 = 42 / 64 MB budget
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

`web/drive/sw.js` runs `fetch(req, { cache: 'no-cache' })` for
shell requests when online — revalidate with the origin, reuse the
HTTP cache on a 304 (the 2026-09 review changed this from
`'no-store'`; the offline fallback page still uses `'no-store'`).
The service-worker cache only kicks in when the network fails. This means a successful Firebase deploy is
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

### Test results (2026-09-03) — real ZIMs, node driver

Same 20 random 4–56 km pairs on the Silicon Valley ZIM, worker as of
2026-08 (`557f971`) versus this branch, Python reference A* as the
oracle (`tests/` differential harness):

| | old worker | new worker |
|---|---|---|
| Total wall for 20 routes | 15.6 s | 4.1 s |
| Median / max per route | 675 ms / 2.6 s | 144 ms / 0.74 s |
| Expansions per second | ~230 k | ~900 k |
| Routes that finished in the "optimal" pass | 11 / 20 (80 km/h heuristic, so not actually guaranteed optimal) | 19 / 20 |
| Routes matching the Python optimum | n/a — 7 of 20 origins snapped onto footway vertices the car-only reference cannot route from, and the other routes drive over footways | 20 / 20 |

The nine old greedy fallbacks were 2–16 % slower than the new routes
for the same pairs (e.g. 3,699 s vs 3,234 s). Excluding footway
ordinals also shrank the search: the same pairs need ~30 % fewer
expansions.

California ZIM (7.9 M nodes, 20 M edges, 7,568 cells), San Diego ⇄
Eureka (1,093 km crow-fly, so both engines skip the optimal pass):

| | old worker | new worker | Python optimum |
|---|---|---|---|
| SD → Eureka | 49,724 s / 1,312 km in 0.6 s | 49,321 s / 1,311 km in 2.0 s | 46,307 s / 1,229 km |
| Eureka → SD | 51,420 s / 1,309 km in 0.6 s | 50,955 s / 1,320 km in 0.5 s | 46,295 s / 1,230 km |

With the greedy factor at ×1.5 the new engine returned routes 6.5 % /
10 % over the optimum after 667 k / 152 k pops; at the shipped ×1.875
it returns the same routes as the old engine after ~60 k / 49 k
pops. Ten random pairs up to 400 km (seed 7), old vs new, wall
per route in node: 372 km pair 2.3 s → 1.7 s (and a 4 % better
route, 20,004 s vs 20,872 s); 320 km 2.3 s → 1.2 s; 331 km 1.8 s →
0.7 s (now finishes in the optimal pass); two unreachable ~400 km
pairs 3.2 s / 2.7 s → 1.7 s / 1.4 s; 10-route total 13.8 s → 7.6 s.
Twenty random pairs up to 60 km (seed 42), back to back on an idle
machine: 4.0 s → 3.0 s wall for the whole run including init, median
per route 24 ms → 13 ms. Two pairs snap both ends to the same node
(both points at sea) and one is unreachable by car (the old engine
"routed" it over a footway; the Python reference confirms no
drivable path) — the remaining 17 all finish in the optimal pass
and match the Python optimum exactly. Two observations for future
work, not changed on this branch:

* The true optimum needs 5.7 M expansions on SD → Eureka and the
  64 MB cell budget then thrashes (700 k cell misses, 349 s in node);
  greedy ×1.25 gets within 3.7 % in 6 s. A contracted highway-tier
  graph in the ZIM is the real fix for 1,000 km routes.
* The two-pass fallback is *worse* on California: its highway-only
  optimal leg touches cells across the whole state, evicts them at
  64 MB and re-fetches 46 GB (24 s, then bails at 150 k pops and
  returns a route 7.9 % over the optimum). It is only reached when
  the full greedy pass bails, which none of the tested routes did.

### Test results (2026-09-04) — after the adversarial review fixes

Same harness as above (node driver against the worker, Python
reference as the oracle), `557f971` = main before the branch,
"branch" = the first 2026-09 revision (admissible → ×1.875 chain),
"final" = the shipped chain (admissible ≤ 200 km → ×1.25 → ×1.875,
closed-pocket check, destination snap rule).

Carolinas ZIM (6.6 M nodes, 3,275 cells):

| pair | main (`557f971`) | branch | final | Python optimum |
|---|---|---|---|---|
| Asheville → Wilmington, 442 km | 20,127 s in 2.3 s (optimal pass, 156k pops) | 24,181 s (+20 %) in 15.0 s | **20,124 s (exact) in 2.0 s**, weighted pass 137k pops | 20,124 s |
| Raleigh → Charlotte, 210 km | 10,632 s (+7.8 %) in 1.8 s | 10,608 s (+7.5 %) in 2.6 s | **10,460 s (+6.0 %) in 2.6 s**, weighted pass 381k pops | 9,867 s |
| Charlotte → Ocracoke Island (ferry only) | no route, 4.8 s | no route, **43 s** (1.1 GB of cell re-fetch in two-pass) | no route, **0.3 s** (closed-pocket check) | none |
| same point | 0 | 0 | 0 | 0 |

Silicon Valley ZIM, 25 random pairs 4–110 km (seed 2024): final vs
branch — 20 / 20 provably optimal (identical routes), 0 pairs worse,
total wall 20.8 s vs 22.6 s (main: 34.8 s, and it "routed" one pair
over footways that the Python reference says is unreachable by car).
The three pairs that bail the admissible pass are 3.7 %, 3.9 % and
19 % over the optimum (the last one via two-pass; its greedy pass
needs more than 500k pops — a contracted highway graph is the real
fix, see above). Synthetic 200 × 200 grid (40k nodes, table grows
through two doublings): 14 / 14 pairs bit-exact with the reference.

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
