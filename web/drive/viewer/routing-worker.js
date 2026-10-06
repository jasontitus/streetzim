// routing-worker.js — runs in a Web Worker context.
//
// Owns the spatial routing graph + cell cache + A* multi-phase
// engine. Frees the main thread from competing with MapLibre repaints
// during the long-route plateau (cf. project_routing_perf_canada.md).
//
// Message protocol (main → worker):
//   {cmd:'init',  baseUrl}                      → {type:'ready'} | {type:'init-error',error}
//   {cmd:'route', id, start, end, options?}     → {type:'route-progress', id, label, pops}*
//                                                 options.travel: 'drive' (default) |
//                                                 'walk' | 'bike' — see edgeCostWB.
//                                                 {type:'route-done', id, ok, result?, error?}
//                                                 ok:true + result:null means the search
//                                                 completed and found no route; ok:false
//                                                 means the engine threw.
//   {cmd:'snap',  id, lat, lon, mode?, travel?} → {type:'snap-done', id, node?, error?}
//                                                 mode 'origin' (default) | 'dest' —
//                                                 see snapNearestNode.
//   options.popLimits (route)                     test hook, see findRouteSpatialFiltered
//   {cmd:'getCoords', id, node}                 → {type:'coords-done', id, lat, lon}
//   {cmd:'compact', keep?}                      → no reply
//   {cmd:'cancel', id}                          → cooperatively stops the matching route
//
// Posting a new 'route' cancels any route still running in this
// worker — the main thread only ever wants the newest one, and two
// concurrent A*s would otherwise share the yield loop and the cell
// budget and both crawl.
//
// This worker does NOT include the legacy monolithic v1 binary format
// — only spatial layouts (SZCI / SZRC). New ZIMs use SZCI v3;
// older ZIMs fall back to the main-thread implementation.

'use strict';

var BASE_URL = '';
var graph = null;
var cancelledRoutes = new Set();
var activeRoutes = new Set();

// A* heuristic speed. MUST be >= the fastest edge speed the builder
// emits (SPEED in streetzim/routing/build.py: motorway = 100 km/h) or the heuristic
// over-estimates remaining time and the "optimal" pass silently
// returns suboptimal routes. Matches tests/szrg_astar.HEURISTIC_SPEED_KPH
// and cloud/route_cli.py, which are the differential references.
var HEURISTIC_SPEED_KMH = 100;
var HEURISTIC_MPS = HEURISTIC_SPEED_KMH / 3.6;

// Pop budgets per phase. Visited state is the NodeTable (21 B/slot,
// power-of-two capacity at ≤ 50 % load, entries inserted on relaxation
// so ~1.4 entries per pop) plus the heap (12 B/push, ~2 pushes per
// pop). At the 1M greedy budget that is a 4M-slot table (~88 MB, ~130
// MB transiently while it doubles) plus ~25-50 MB of heap: ~150-180 MB
// peak, versus ~440 B per visited node × 400k under the old Map-based
// state (~175 MB) — same envelope, 2.5× the search budget.
// Measured on the Silicon Valley ZIM (894k nodes): at 200k the optimal
// pass bailed on 7 of 16 random 5-60 km metro pairs and one pair fell
// through greedy to two-pass with a 70 % longer route; at these
// budgets every pair completes in the optimal pass. ~800k pops/s on a
// desktop core; budget the worst case at a few seconds on a phone.
var POP_LIMIT_FULL_OPTIMAL = 500000;
// Second pass: weighted A* at ×1.25. The pre-2026-09 engine divided
// its heuristic by 80 km/h, which is inadmissible but behaves exactly
// like weighted A* at 100/80 = 1.25 — and that is what made it converge
// on 200–500 km pairs (Carolinas Asheville→Wilmington: 156k pops, the
// true optimum). Going straight from the admissible pass to ×1.875
// lost that: the same pair bailed at 500k and greedy returned a route
// 20 % slower in 15 s. ×1.25 gets it in 137k pops / 3 s, bit-exact
// with the Python optimum; Raleigh→Charlotte 6 % over instead of 7.5 %.
// Worst case is bounded by 1.25 × optimal.
var WEIGHT_FULL_WEIGHTED = 1.25;
// Raleigh→Charlotte (210 km) needs 381k weighted pops for a route 6 %
// over the optimum; at 300k it fell through to greedy (7.5 % over).
var POP_LIMIT_FULL_WEIGHTED = 400000;
// The admissible pass is skipped above this crow-fly distance and the
// chain starts at the weighted pass: on state-sized graphs the optimal
// pass never finishes 500k pops past ~200 km (Raleigh→Charlotte bails;
// every 4–110 km Silicon Valley pair that finishes at all does so well
// under it) and each failed attempt costs seconds of cell thrash —
// Asheville→Wilmington (442 km) took 15 s through a bailing admissible
// pass versus 3 s going straight to ×1.25 for the same, exact-optimum
// route. Silicon Valley metro pairs are all under this and keep their
// optimality guarantee.
var OPTIMAL_MAX_CROW_KM = 200;
// Third pass: the old effective greedy weight (1.5 on an 80 km/h
// heuristic = 1.875 against the admissible one) for routes even the
// ×1.25 pass could not finish. Two Silicon Valley hill pairs need 343k
// and 467k greedy pops (the two-pass fallback cannot help them: no
// highway-tier edge within 5000 BFS pops), so the budget stays at 500k.
// Worst case is now 1.4M pops, but only for a destination that is
// unreachable *and* not in a small closed pocket (destComponentClosed
// ends the common island / cut-boundary case after the first pass).
var GREEDY_WEIGHT_FULL = 1.875;
var POP_LIMIT_FULL_GREEDY = 500000;
// Walking and cycling have no two-pass fallback (it rides the highway
// tier), so their last pass gets a larger budget: Silicon Valley bike
// pairs of 40-55 km bailed every 500k pass (San Jose -> Pescadero, 101
// km by bike, needs 500k-1M even optimal). Walking's tight 5 km/h
// heuristic keeps it far below (a 30 km walk: ~250k pops).
var POP_LIMIT_WB_GREEDY = 1500000;
// And room for the exact pass to finish: San Jose -> Pescadero by bike
// (100 km) needs 677k pops; at the car's 500k it bailed into the weighted
// and greedy passes (1.45M pops, 1.8 s, a route 2.5 % slower). With 1.2M
// it is exact in 1.2 s. Worst case: a pass visiting over 1M states grows
// the NodeTable to 4M slots (88 MB, 132 MB while growing) plus 24-48 MB of
// heap, ~130-180 MB, reached only on long rides (a 30 km ride: ~160k pops).
// A destination in a sealed pocket is caught before the first pass.
var POP_LIMIT_WB_OPTIMAL = 1200000;
var POP_LIMIT_WB_WEIGHTED = 1000000;
var POP_LIMIT_HW_OPTIMAL = 150000;
var POP_LIMIT_HW_GREEDY = 300000;
// Unreachable-destination short-circuit (see destComponentClosed):
// a bounded forward BFS from the destination that exhausts before this
// many nodes without reaching the origin means the destination sits in
// a closed pocket (ferry-only island, extract cut) — no pass can reach
// it, so the remaining passes and the two-pass fallback are skipped.
var DEST_COMPONENT_LIMIT = 20000;

// Profiling — reset at route start, emitted as a single console.warn
// on completion so the user (or smoke harness) gets one consolidated
// line per route showing where wall-clock went. Always-on; cheap.
var _profile = null;
function nowMs() {
  return (typeof performance !== 'undefined' && performance.now)
    ? performance.now() : Date.now();
}
function _profileReset() {
  _profile = {
    routeT0: nowMs(),
    crowKm: 0,
    phases: [],
    cellHits: 0,
    cellMisses: 0,
    cellHttpMs: 0,    // fetch() → headers
    cellBodyMs: 0,    // headers → arrayBuffer() complete
    cellFetchMs: 0,   // total fetch + body (sum of two above; kept for compat)
    cellParseMs: 0,   // parseRoutingCell time (typed-array views)
    cellBytes: 0,     // total bytes loaded across misses
    prewarmCells: 0,  // # of cells fired off by corridor pre-warm
    prewarmMs: 0,     // wall time spent kicking off the prefetches
    yields: 0,
    yieldMs: 0,
    edgeReqs: 0,      // node expansions (pops that actually scanned edges)
  };
}
// Emits `prof` (a specific route's profile object). Routes overlap
// briefly when a new one cancels its predecessor, so the predecessor
// must only clear the global slot if it still owns it — otherwise the
// successor's profile went missing and the predecessor's phases were
// pushed into the successor's record.
function _profileEmit(prof, routeOk, totalCoords) {
  if (!prof) return;
  var _profileWas = _profile;
  _profile = prof;
  var total = nowMs() - _profile.routeT0;
  // Pack into a flat object the main thread can format/log. Worker
  // console messages aren't captured by puppeteer's page.on('console')
  // hook (different target), so the main-thread log is what the smoke
  // harness sees. The worker ALSO console.warn's locally for devtools.
  var summary = {
    type: 'route-profile',
    ok: routeOk,
    totalMs: total,
    crowKm: _profile.crowKm,
    coords: totalCoords || 0,
    cellHits: _profile.cellHits,
    cellMisses: _profile.cellMisses,
    cellFetchMs: _profile.cellFetchMs,
    cellHttpMs: _profile.cellHttpMs,
    cellBodyMs: _profile.cellBodyMs,
    cellParseMs: _profile.cellParseMs,
    cellBytes: _profile.cellBytes,
    prewarmCells: _profile.prewarmCells,
    prewarmMs: _profile.prewarmMs,
    edgeReqs: _profile.edgeReqs,
    yields: _profile.yields,
    yieldMs: _profile.yieldMs,
    phases: _profile.phases,
  };
  console.warn('[route-profile worker]', JSON.stringify(summary));
  try { self.postMessage(summary); } catch (e) {}
  // Restore whichever profile was current (the successor's, if this is
  // a cancelled predecessor winding down); clear only if it was ours.
  _profile = (_profileWas === prof) ? null : _profileWas;
}

self.onmessage = function(e) {
  var msg = e.data;
  switch (msg.cmd) {
    case 'init':      handleInit(msg);      break;
    case 'route':     handleRoute(msg);     break;
    case 'snap':      handleSnap(msg);      break;
    case 'getCoords': handleGetCoords(msg); break;
    case 'compact':   handleCompact(msg);   break;
    case 'cancel':    cancelledRoutes.add(msg.id); break;
    case 'prewarmCells': handlePrewarmCells(msg); break;
  }
};

// Prewarm a list of cells covering caller-supplied lat/lon points.
// Used at page-load time to warm the cells around the user's GPS
// location so the first route doesn't pay the ~1.7s cold-fetch
// penalty on its starting cell. _ensureCell de-dupes via _inFlight,
// so this is safe to call repeatedly (and idempotent w.r.t. the
// corridor pre-warm fired at route start).
function handlePrewarmCells(msg) {
  if (!graph || !graph._index || !graph._index.cellForCoords) return;
  var coords = msg.coords || [];
  var seen = new Set();
  var fired = 0;
  for (var i = 0; i < coords.length; i++) {
    var c = coords[i];
    if (!c || typeof c.lat !== 'number' || typeof c.lon !== 'number') continue;
    var latE7 = Math.round(c.lat * 1e7);
    var lonE7 = Math.round(c.lon * 1e7);
    var cid = graph._index.cellForCoords(latE7, lonE7);
    if (cid < 0 || seen.has(cid)) continue;
    seen.add(cid);
    if (graph._cells.has(cid)) continue;
    graph._ensureCell(cid, /*priority=*/false).catch(function() {}); // best-effort
    fired++;
  }
  if (fired > 0) {
    console.warn('[routing-worker] prewarmed ' + fired + ' cells around '
                 + coords.length + ' coords');
  }
}

// Resident routing-cell budget.
//
// A flat 64 MB held only ~33 of east-coast-us's cells, but a NYC->Philadelphia
// route needs ~62 resident at once (route_cli loads 62 and keeps them). The
// A* frontier then evicted cells it was about to need again: measured on
// 2026-09-10, misses climbed linearly at ~21 cells/s with resident pinned at
// 33 cells / 63 MB and the route never finished — the 360 s browser-gate
// timeout east-coast-us kept failing while the same route took 5.9 s
// natively. The working set has to fit or the search never terminates.
//
// deviceMemory is Chrome-only and capped at 8; where it is missing (Safari,
// Firefox) assume a mid-range device rather than the old 64 MB floor, since
// that floor is precisely what breaks. Small regions never approach any of
// these numbers — the cache only fills when the route is long.
function residentCellBudget() {
  var gb = (self.navigator && self.navigator.deviceMemory) || 0;
  var mb = gb ? Math.min(384, Math.max(64, gb * 48)) : 192;
  return mb * 1024 * 1024;
}

function handleInit(msg) {
  BASE_URL = msg.baseUrl || '';
  // The main thread has already fetched the cells index and hands over a
  // copy (transferred); fetching it a second time through the service
  // worker was the other half of a 300 MB startup on Japan.
  var bufP = (msg.cellsIndex instanceof ArrayBuffer)
    ? Promise.resolve(msg.cellsIndex)
    : fetch(BASE_URL + 'routing-data/graph-cells-index.bin')
        .then(function(r) {
          if (!r.ok) throw new Error('cells-index HTTP ' + r.status);
          return r.arrayBuffer();
        });
  bufP
    .then(function(buf) {
      var idx = parseRoutingCellsIndex(buf);
      return loadNodeShards(idx).then(function() { return idx; });
    })
    .then(function(idx) {
      graph = new SpatialGraph(idx, residentCellBudget());
      self.postMessage({
        type: 'ready',
        format: 'spatial-v' + idx.version,
        numNodes: idx.numNodes,
        numEdges: idx.numEdges,
        numCells: idx.numCells,
      });
    })
    .catch(function(err) {
      self.postMessage({ type: 'init-error', error: String(err) });
    });
}

function handleRoute(msg) {
  if (!graph) {
    return self.postMessage({
      type: 'route-done', id: msg.id, ok: false,
      error: 'graph not loaded',
    });
  }
  // Only the newest route matters — cancel anything still running so
  // it stops burning pops and cell budget at the next yield.
  activeRoutes.forEach(function(otherId) {
    if (otherId !== msg.id) cancelledRoutes.add(otherId);
  });
  activeRoutes.add(msg.id);
  cancelledRoutes.delete(msg.id);
  _profileReset();
  var ctx = {
    id: msg.id,
    options: msg.options || {},
    bailed: false,
    profile: _profile,
    cancelled: function() { return cancelledRoutes.has(msg.id); },
  };
  findRoute(msg.start, msg.end, ctx)
    .then(function(result) {
      // Capture the cancellation state BEFORE clearing — the bridge
      // uses this to distinguish "user clicked origin field while
      // routing" (don't update UI) from "no route found".
      var wasCancelled = cancelledRoutes.has(msg.id);
      cancelledRoutes.delete(msg.id);
      activeRoutes.delete(msg.id);
      _profileEmit(ctx.profile, !!result, result && result.coords ? result.coords.length : 0);
      // ok:true even when result is null: the search ran to completion
      // and there is no route (or every phase exhausted its budget).
      // Reporting ok:false here made the bridge treat it as an engine
      // failure and re-run the identical search on the main thread —
      // twice the wall time (and iOS heap) to reach the same answer.
      self.postMessage({
        type: 'route-done', id: msg.id,
        ok: true, cancelled: wasCancelled,
        result: result || null,
      });
    })
    .catch(function(err) {
      var wasCancelled = cancelledRoutes.has(msg.id);
      cancelledRoutes.delete(msg.id);
      activeRoutes.delete(msg.id);
      _profileEmit(ctx.profile, false, 0);
      self.postMessage({
        type: 'route-done', id: msg.id, ok: false,
        cancelled: wasCancelled,
        error: String(err && err.stack ? err.stack : err),
      });
    });
}

function handleSnap(msg) {
  if (!graph) {
    return self.postMessage({
      type: 'snap-done', id: msg.id, error: 'graph not loaded',
    });
  }
  if (typeof msg.lat !== 'number' || typeof msg.lon !== 'number'
      || !isFinite(msg.lat) || !isFinite(msg.lon)) {
    return self.postMessage({
      type: 'snap-done', id: msg.id, error: 'snap: lat/lon not finite numbers',
    });
  }
  graph.snapNearestNode(Math.round(msg.lat * 1e7), Math.round(msg.lon * 1e7),
                        msg.mode === 'dest' ? 'dest' : 'origin',
                        travelMode(msg.travel))
    .then(function(result) {
      self.postMessage({
        type: 'snap-done', id: msg.id,
        node: result.node, lat: result.lat, lon: result.lon,
      });
    })
    .catch(function(err) {
      self.postMessage({
        type: 'snap-done', id: msg.id, error: String(err),
      });
    })
    .then(function() {
      // The bridge's cancel sweep covers snap ids too; drop ours so the
      // set doesn't grow by one entry per snap over a long session.
      cancelledRoutes.delete(msg.id);
    });
}

function handleGetCoords(msg) {
  if (!graph) {
    return self.postMessage({
      type: 'coords-done', id: msg.id, error: 'graph not loaded',
    });
  }
  var n = msg.node | 0;
  if (n < 0 || n >= graph.numNodes) {
    return self.postMessage({
      type: 'coords-done', id: msg.id, error: 'node out of range',
    });
  }
  graph.nodeCoordsE7(n).then(function(coords) {
    self.postMessage({
      type: 'coords-done', id: msg.id,
      lat: coords[0] / 1e7,
      lon: coords[1] / 1e7,
    });
  }).catch(function(err) {
    self.postMessage({
      type: 'coords-done', id: msg.id, error: String(err),
    });
  });
}

function handleCompact(msg) {
  if (graph && typeof graph.compact === 'function') {
    graph.compact(msg.keep != null ? msg.keep : 8);
  }
}

function debugStats(ctx, label, pops) {
  self.postMessage({
    type: 'route-progress', id: ctx.id, label: label, pops: pops,
  });
}

// =============================================================
// Routing engine (spatial layouts only).
// =============================================================

var DEG2RAD = Math.PI / 180;
var EARTH_R2 = 2 * 6371000;

function haversine(lat1, lon1, lat2, lon2) {
  var dLat = (lat2 - lat1) * DEG2RAD;
  var dLon = (lon2 - lon1) * DEG2RAD;
  var sLat = Math.sin(dLat / 2);
  var sLon = Math.sin(dLon / 2);
  var a = sLat * sLat +
          Math.cos(lat1 * DEG2RAD) * Math.cos(lat2 * DEG2RAD) * sLon * sLon;
  a = Math.min(1, Math.max(0, a));
  return EARTH_R2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// Highway-tier filter — matches the OSM class_access ordinal scheme.
// 1: motorway, 2: motorway_link, 3: trunk, 4: trunk_link,
// 5: primary,  6: primary_link.
function isHighwayClass(classAccess) {
  var cls = classAccess & 0x1F;
  return cls >= 1 && cls <= 6;
}
// class_access bit 9: no motor vehicles (footway / path / steps /
// cycleway / access=private …). Set by create_osm_zim.py ≥ 2026-09;
// older ZIMs have it clear everywhere, so this is a no-op on them.
// The driving profile must never expand such an edge — before the
// builder marked them, a 20 m staircase at 3 km/h out-scored any road
// detour over ~200 m and cars were routed down stairs.
var NO_MOTOR_BIT = 0x200;
// Class ordinals 16..20 (path, footway, cycleway, pedestrian, steps —
// CLASS_ORDINAL in extract_routing_graph, streetzim/routing/build.py)
// are never drivable either. ZIMs built before the builder set bit 9
// still carry these ways in the car graph
// with 3–5 km/h speeds, so the ordinal is checked alongside the bit.
var NO_MOTOR_ORD_MIN = 16, NO_MOTOR_ORD_MAX = 20;
function isNoMotor(classAccess) {
  if (classAccess & NO_MOTOR_BIT) return true;
  var ord = classAccess & 0x1F;
  return ord >= NO_MOTOR_ORD_MIN && ord <= NO_MOTOR_ORD_MAX;
}

// --- Travel modes -----------------------------------------------------
// Mirror of streetzim/routing/modes.py (tests compare the two edge by
// edge): what walking and cycling may use and what it costs. Bits are
// in docs/formats.md. The search minimises cost; the route reports
// time, so edgeCostWB returns the cost and leaves the time in _wbTime.
// Every speed is at most the mode's heuristic speed and every
// multiplier is >= 1, so the straight-line heuristic stays admissible.
var TRAVEL_HEURISTIC_KMH = { drive: HEURISTIC_SPEED_KMH, walk: 5, bike: 18 };
var WALK_KPH = 5, WALK_STEPS_KPH = 2.5, WALK_ROUGH_KPH = 4.5;
var BIKE_PAVED_KPH = 18, BIKE_FIRM_KPH = 14, BIKE_ROUGH_KPH = 8;
var BIKE_TRACK_KPH = 12, BIKE_PUSH_KPH = 4;
var BIKE_PUSH_PENALTY = 1.5, BIKE_STEPS_PENALTY = 3.0;
var WALK_SEPARATE_SIDEWALK = 1.2, PRIVATE_PENALTY = 3.0;
var GEOM_REVERSED_BIT = 0x800;
var _wbTime = 0;

function travelMode(t) {
  return (t === 'walk' || t === 'bike') ? t : 'drive';
}

// Cost in seconds of a walk/bike edge, or -1 if the mode may not use
// it; the plain travel time is left in _wbTime.
function edgeCostWB(mode, speedDist, ca) {
  var distM = (speedDist & 0xFFFFFF) / 10;
  var ord = ca & 0x1F;
  var surface = (ca >>> 15) & 3;
  var t, mult;
  if (mode === 'walk') {
    if ((ca & 0x20) || (ca & 0x40000) || ord === 1 || ord === 2) return -1;
    var kph = (ord === 20) ? WALK_STEPS_KPH
            : (surface === 2 || surface === 3) ? WALK_ROUGH_KPH : WALK_KPH;
    t = distM / (kph / 3.6);
    mult = 1.0;
    if (ord === 3 || ord === 4) mult = 1.5;
    if ((ca & 0x20000) && ord >= 3 && ord <= 8) mult *= 1.5;
    if ((ca & 0x80000) && ord >= 1 && ord <= 14) mult *= WALK_SEPARATE_SIDEWALK;
    if (ca & 0x100000) mult *= PRIVATE_PENALTY;
    _wbTime = t;
    return t * mult;
  }
  // bike
  if ((ca & 0x40) || ord === 1 || ord === 2) return -1;
  var priv = (ca & 0x200000) ? PRIVATE_PENALTY : 1.0;
  if ((ca & 0x1000) || ord === 20) {
    t = distM / (BIKE_PUSH_KPH / 3.6);
    _wbTime = t;
    return t * priv * (ord === 20 ? BIKE_STEPS_PENALTY : BIKE_PUSH_PENALTY);
  }
  var bkph;
  if (surface === 3) bkph = BIKE_ROUGH_KPH;
  else if (surface === 2) bkph = BIKE_FIRM_KPH;
  else if (surface === 0 && (ord === 15 || ord === 16)) bkph = BIKE_TRACK_KPH;
  else bkph = BIKE_PAVED_KPH;
  t = distM / (bkph / 3.6);
  mult = 1.0;
  if (!(ca & 0x6000)) {
    if (ord === 3 || ord === 4) mult = 1.6;
    else if (ord === 5 || ord === 6) mult = 1.4;
    else if (ord === 7 || ord === 8) mult = 1.2;
  }
  _wbTime = t;
  return t * mult * priv;
}

// May `travel` use this edge at all?
function edgeUsable(travel, speedDist, ca) {
  if (travel === 'drive') return !isNoMotor(ca) && (speedDist >>> 24) !== 0;
  return edgeCostWB(travel, speedDist, ca) >= 0;
}

// --- Sparse visited-node state -------------------------------------
// Open-addressing hash table keyed by global node id with parallel
// typed-array columns for the A* state (g, predecessor node,
// predecessor edge slot, closed flag). Replaces four Map/Set objects
// holding boxed doubles and a 6-element array per visited node
// (~440 B/node) with ~21 B per slot at <=50% load, i.e. ~42 B per
// visited node — and no per-relaxation allocation at all.
function NodeTable(initialCapacity) {
  var cap = 1024;
  while (cap < initialCapacity) cap <<= 1;
  this._alloc(cap);
}
NodeTable.prototype._alloc = function(cap) {
  this.cap = cap;
  this.mask = cap - 1;
  this.size = 0;
  this.keys = new Int32Array(cap);
  this.keys.fill(-1);
  this.g = new Float64Array(cap);
  this.prev = new Int32Array(cap);
  this.prevEi = new Int32Array(cap);
  this.closed = new Uint8Array(cap);
};
NodeTable.prototype._slotFor = function(key) {
  // Fibonacci hashing spreads consecutive node ids (cell-major, so
  // neighbours are usually adjacent ids) across the table.
  return (Math.imul(key, 0x9E3779B1) >>> 0) >>> 0 & this.mask;
};
// Slot holding `key`, or -1.
NodeTable.prototype.find = function(key) {
  var keys = this.keys, mask = this.mask;
  var i = this._slotFor(key);
  while (true) {
    var k = keys[i];
    if (k === key) return i;
    if (k === -1) return -1;
    i = (i + 1) & mask;
  }
};
// Slot holding `key`, inserting it (g=Inf, no predecessor) if absent.
// May grow the table — any slot index obtained earlier is stale after
// this returns.
NodeTable.prototype.insert = function(key) {
  if ((this.size + 1) * 2 > this.cap) this._grow();
  var keys = this.keys, mask = this.mask;
  var i = this._slotFor(key);
  while (true) {
    var k = keys[i];
    if (k === key) return i;
    if (k === -1) {
      keys[i] = key;
      this.g[i] = Infinity;
      this.prev[i] = -1;
      this.prevEi[i] = -1;
      this.closed[i] = 0;
      this.size++;
      return i;
    }
    i = (i + 1) & mask;
  }
};
NodeTable.prototype._grow = function() {
  var oldKeys = this.keys, oldG = this.g, oldPrev = this.prev;
  var oldEi = this.prevEi, oldClosed = this.closed, oldCap = this.cap;
  this._alloc(oldCap * 2);
  for (var i = 0; i < oldCap; i++) {
    var k = oldKeys[i];
    if (k === -1) continue;
    var j = this._slotFor(k);
    while (this.keys[j] !== -1) j = (j + 1) & this.mask;
    this.keys[j] = k;
    this.g[j] = oldG[i];
    this.prev[j] = oldPrev[i];
    this.prevEi[j] = oldEi[i];
    this.closed[j] = oldClosed[i];
    this.size++;
  }
};
// Approximate resident bytes (for the debug overlay / profile).
NodeTable.prototype.bytes = function() {
  return this.cap * (4 + 8 + 4 + 4 + 1);
};

// --- Binary min-heap on parallel typed arrays ----------------------
// (f-score, node) pairs without allocating a 2-element array per push.
function NodeHeap(initialCapacity) {
  var cap = Math.max(1024, initialCapacity | 0);
  this.keys = new Float64Array(cap);
  this.vals = new Int32Array(cap);
  this.n = 0;
  this.topKey = 0;
}
NodeHeap.prototype.push = function(key, val) {
  if (this.n === this.keys.length) {
    var nk = new Float64Array(this.keys.length * 2);
    nk.set(this.keys);
    var nv = new Int32Array(this.vals.length * 2);
    nv.set(this.vals);
    this.keys = nk;
    this.vals = nv;
  }
  var keys = this.keys, vals = this.vals;
  var i = this.n++;
  while (i > 0) {
    var p = (i - 1) >> 1;
    var pk = keys[p];
    if (pk <= key) break;
    keys[i] = pk;
    vals[i] = vals[p];
    i = p;
  }
  keys[i] = key;
  vals[i] = val;
};
// Returns the node with the smallest key; the key is left in
// `this.topKey`. Caller must check `n > 0` first.
NodeHeap.prototype.pop = function() {
  var keys = this.keys, vals = this.vals;
  var topVal = vals[0];
  this.topKey = keys[0];
  var n = --this.n;
  if (n > 0) {
    var key = keys[n];
    var val = vals[n];
    var i = 0;
    var half = n >> 1;
    while (i < half) {
      var l = 2 * i + 1;
      var r = l + 1;
      var c = (r < n && keys[r] < keys[l]) ? r : l;
      if (keys[c] >= key) break;
      keys[i] = keys[c];
      vals[i] = vals[c];
      i = c;
    }
    keys[i] = key;
    vals[i] = val;
  }
  return topVal;
};
NodeHeap.prototype.size = function() { return this.n; };

function parseRoutingCellsIndex(buffer) {
  var view = new DataView(buffer);
  if (view.getUint32(0, false) !== 0x53_5A_43_49) {
    throw new Error('Invalid cells-index magic');
  }
  var version = view.getUint32(4, true);
  if (version !== 1 && version !== 2 && version !== 3) {
    throw new Error('Unsupported SZCI version: ' + version);
  }
  var numNodes = view.getUint32(8, true);
  var numEdges = view.getUint32(12, true);
  var numNames = view.getUint32(16, true);
  var namesBytes = view.getUint32(20, true);
  var numCells = view.getUint32(24, true);
  var cellScale = view.getInt32(28, true);

  var offset, numNodeShards = 0, nodesPerShard = 0, nodesScaled = null;
  if (version === 1) {
    offset = 32;
    nodesScaled = new Int32Array(buffer, offset, numNodes * 2);
    offset += numNodes * 2 * 4;
  } else if (version === 2) {
    numNodeShards = view.getUint32(32, true);
    nodesPerShard = view.getUint32(36, true);
    offset = 40;
  } else {
    offset = 32;
  }
  var cellLatIdx = new Int32Array(numCells);
  var cellLonIdx = new Int32Array(numCells);
  var cellBaseNode = new Uint32Array(numCells);
  var cellNodeCount = new Uint32Array(numCells);
  var cellEdgeCount = new Uint32Array(numCells);
  var cellGeomCount = new Uint32Array(numCells);
  // Numeric cell key instead of "lat,lon" strings — cellForCoords is
  // called from the snap + prewarm hot paths.
  var cellKeyToId = new Map();
  function cellKey(latMult, lonMult) {
    return latMult * 1048576 + lonMult;  // |lonMult| < 2^20 for any sane scale
  }
  for (var i = 0; i < numCells; i++) {
    cellLatIdx[i]    = view.getInt32(offset,      true);
    cellLonIdx[i]    = view.getInt32(offset +  4, true);
    if (version === 3) {
      cellBaseNode[i]  = view.getUint32(offset +  8, true);
      cellNodeCount[i] = view.getUint32(offset + 12, true);
      cellEdgeCount[i] = view.getUint32(offset + 16, true);
      cellGeomCount[i] = view.getUint32(offset + 20, true);
      offset += 24;
    } else {
      cellNodeCount[i] = view.getUint32(offset +  8, true);
      cellEdgeCount[i] = view.getUint32(offset + 12, true);
      cellGeomCount[i] = view.getUint32(offset + 16, true);
      offset += 20;
    }
    cellKeyToId.set(cellKey(cellLatIdx[i], cellLonIdx[i]), i);
  }
  var nameOffsets = new Uint32Array(buffer, offset, numNames + 1);
  offset += (numNames + 1) * 4;
  var namesBlob = new Uint8Array(buffer, offset, namesBytes);
  var textDecoder = new TextDecoder('utf-8');

  // cell_of(lat_e7, lon_e7, scale) — must match streetzim/routing/spatial.cell_of
  // EXACTLY (floor semantics on negatives).
  function cellIdFor(latE7, lonE7) {
    var latMult = Math.floor((latE7 * cellScale) / 10_000_000);
    var lonMult = Math.floor((lonE7 * cellScale) / 10_000_000);
    var id = cellKeyToId.get(cellKey(latMult, lonMult));
    return id === undefined ? -1 : id;
  }
  function getName(nameIdx) {
    if (nameIdx <= 0 || nameIdx >= numNames) return '';
    var s = nameOffsets[nameIdx];
    var e = nameOffsets[nameIdx + 1];
    if (s === e) return '';
    return textDecoder.decode(namesBlob.subarray(s, e));
  }

  var idx = {
    version: version,
    numNodes: numNodes,
    numEdges: numEdges,
    numNames: numNames,
    numCells: numCells,
    cellScale: cellScale,
    numNodeShards: numNodeShards,
    nodesPerShard: nodesPerShard,
    nodesScaled: nodesScaled,
    cellLatIdx: cellLatIdx,
    cellLonIdx: cellLonIdx,
    cellBaseNode: cellBaseNode,
    cellNodeCount: cellNodeCount,
    cellEdgeCount: cellEdgeCount,
    cellGeomCount: cellGeomCount,
    getName: getName,
  };
  // Node-coordinate accessors — work for v1 (inline flat array) and v2
  // (separate shard buffers, never combined into one full-size array, which
  // doubled peak memory and OOM'd dense graphs like California's 7.9M nodes).
  idx.hasNodes = function() {
    return idx.version === 3 || !!(idx.nodeShards || idx.nodesScaled);
  };
  idx.nodeLatE7 = function(n) {
    if (idx.nodeShards) {
      var s = (n / idx.nodesPerShard) | 0;
      return idx.nodeShards[s][(n - s * idx.nodesPerShard) * 2];
    }
    return idx.nodesScaled[n * 2];
  };
  idx.nodeLonE7 = function(n) {
    if (idx.nodeShards) {
      var s = (n / idx.nodesPerShard) | 0;
      return idx.nodeShards[s][(n - s * idx.nodesPerShard) * 2 + 1];
    }
    return idx.nodesScaled[n * 2 + 1];
  };
  idx.cellForNode = function(nodeIdx) {
    if (idx.version === 3) {
      var lo = 0, hi = idx.numCells;
      while (lo < hi) {
        var mid = (lo + hi) >>> 1;
        if (idx.cellBaseNode[mid] <= nodeIdx) lo = mid + 1;
        else hi = mid;
      }
      var cid = lo - 1;
      return cid >= 0
        && nodeIdx < idx.cellBaseNode[cid] + idx.cellNodeCount[cid]
        ? cid : -1;
    }
    if (!idx.hasNodes()) return -1;
    return cellIdFor(idx.nodeLatE7(nodeIdx), idx.nodeLonE7(nodeIdx));
  };
  // Used by the corridor pre-warm — sample arbitrary lat/lon, find
  // the cell that owns that point, prefetch in parallel before A*.
  idx.cellForCoords = cellIdFor;
  return idx;
}

function loadNodeShards(idx) {
  if (idx.version !== 2) return Promise.resolve();
  // Keep each shard buffer separate and index into the owning shard on
  // demand (see idx.nodeLatE7/nodeLonE7). Building one combined
  // Int32Array(numNodes*2) doubled peak memory — the shard buffers PLUS the
  // full-size copy — and OOM'd dense graphs (California: 7.9M nodes, the
  // copy alone was ~60 MB on top of the 60 MB of shards). Now peak is just
  // the shards.
  var shards = new Array(idx.numNodeShards);
  function pad3(n) {
    return n < 10 ? '00' + n : n < 100 ? '0' + n : '' + n;
  }
  var fetches = [];
  for (var i = 0; i < idx.numNodeShards; i++) {
    (function(shardIdx) {
      var path = BASE_URL + 'routing-data/nodes-scaled-' + pad3(shardIdx) + '.bin';
      fetches.push(
        fetch(path).then(function(r) {
          if (!r.ok || r.headers.get('X-Streetzim-Absent') === '1') {
            throw new Error('node shard ' + shardIdx + ' missing');
          }
          return r.arrayBuffer();
        }).then(function(buf) {
          shards[shardIdx] = new Int32Array(buf);
        })
      );
    })(i);
  }
  return Promise.all(fetches).then(function() {
    idx.nodeShards = shards;
  });
}

function parseRoutingCell(index, cid, buffer) {
  var view = new DataView(buffer);
  if (view.getUint32(0, false) !== 0x53_5A_52_43) {
    throw new Error('Invalid SZRC magic');
  }
  var version = view.getUint32(4, true);
  if (version !== 1 && version !== 2) {
    throw new Error('Unsupported SZRC version: ' + version);
  }
  var cellId = view.getUint32(8, true);
  var nodeCount = view.getUint32(12, true);
  var edgeCount = view.getUint32(16, true);
  var geomCount = view.getUint32(20, true);
  var geomBytes = view.getUint32(24, true);

  var off = 28;
  var baseNode = version === 2 ? index.cellBaseNode[cid] : 0;
  var cellNodesGlobal = null;
  var nodesScaled = null;
  if (version === 1) {
    cellNodesGlobal = new Uint32Array(buffer, off, nodeCount);
    off += nodeCount * 4;
  } else {
    nodesScaled = new Int32Array(buffer, off, nodeCount * 2);
    off += nodeCount * 2 * 4;
  }
  var cellAdj = new Uint32Array(buffer, off, nodeCount + 1);
  off += (nodeCount + 1) * 4;
  var edges = new Uint32Array(buffer, off, edgeCount * 5);
  off += edgeCount * 5 * 4;
  var geomOffsets = new Uint32Array(buffer, off, geomCount + 1);
  off += (geomCount + 1) * 4;
  var geomBlob = new Uint8Array(buffer, off, geomBytes);
  var geomBlobByteStart = off;
  var turns = parseTurnTrailer(buffer, view, off + geomBytes);

  function localIdxFor(globalIdx) {
    if (version === 2) {
      var local = globalIdx - baseNode;
      return local >= 0 && local < nodeCount ? local : -1;
    }
    var lo = 0, hi = nodeCount;
    while (lo < hi) {
      var mid = (lo + hi) >>> 1;
      var v = cellNodesGlobal[mid];
      if (v < globalIdx) lo = mid + 1;
      else if (v > globalIdx) hi = mid;
      else return mid;
    }
    return -1;
  }

  function decodeGeomLocal(gi) {
    var start = geomOffsets[gi];
    var end = geomOffsets[gi + 1];
    if (end <= start + 8) {
      var lon0 = view.getInt32(geomBlobByteStart + start, true);
      var lat0 = view.getInt32(geomBlobByteStart + start + 4, true);
      return [[lon0 / 1e7, lat0 / 1e7]];
    }
    var lon = view.getInt32(geomBlobByteStart + start, true);
    var lat = view.getInt32(geomBlobByteStart + start + 4, true);
    var coords = [[lon / 1e7, lat / 1e7]];
    var i = start + 8;
    while (i < end) {
      var raw = 0, shift = 0, b;
      do {
        b = geomBlob[i++];
        raw |= (b & 0x7F) << shift;
        shift += 7;
      } while (b & 0x80);
      var dlon = (raw >>> 1) ^ -(raw & 1);
      raw = 0; shift = 0;
      do {
        b = geomBlob[i++];
        raw |= (b & 0x7F) << shift;
        shift += 7;
      } while (b & 0x80);
      var dlat = (raw >>> 1) ^ -(raw & 1);
      lon += dlon;
      lat += dlat;
      coords.push([lon / 1e7, lat / 1e7]);
    }
    return coords;
  }

  return {
    cellId: cellId,
    version: version,
    byteLength: buffer.byteLength,
    baseNode: baseNode,
    nodeCount: nodeCount,
    edgeCount: edgeCount,
    geomCount: geomCount,
    cellNodesGlobal: cellNodesGlobal,
    nodesScaled: nodesScaled,
    cellAdj: cellAdj,
    edges: edges,
    localIdxFor: localIdxFor,
    decodeGeomLocal: decodeGeomLocal,
    turns: turns,
  };
}

// Turn restrictions held by a cell: the SZTR trailer after its geometry
// blob (streetzim/routing/restrictions.py has the layout). Records are
// node paths [u, v, ..., w] (global ids) whose v is in this cell.
// Returns null when there is none, else {recs: [{flags, path, id}],
// roots: Map("u,v" -> [rec])}. The trailer need not be 4-byte aligned,
// so its arrays are copied out.
function parseTurnTrailer(buffer, view, off) {
  if (buffer.byteLength < off + 16) return null;
  if (view.getUint32(off, false) !== 0x535A5452) return null;    // 'SZTR'
  if (view.getUint32(off + 4, true) !== 1) return null;
  var r = view.getUint32(off + 8, true), p = view.getUint32(off + 12, true);
  var o = off + 16;
  var offs = new Uint32Array(buffer.slice(o, o + (r + 1) * 4));
  o += (r + 1) * 4;
  var flags = new Uint8Array(buffer.slice(o, o + r));
  o += r + ((4 - r % 4) % 4);
  var pool = new Uint32Array(buffer.slice(o, o + p * 4));
  // byVia: path[1] -> records; the router checks path[0] itself (a number
  // key, no string per relaxed edge).
  var recs = [], byVia = new Map();
  for (var i = 0; i < r; i++) {
    var path = Array.prototype.slice.call(pool, offs[i], offs[i + 1]);
    var rec = { flags: flags[i], path: path, id: i };
    recs.push(rec);
    var lst = byVia.get(path[1]);
    if (lst) lst.push(rec); else byVia.set(path[1], [rec]);
  }
  return { recs: recs, byVia: byVia };
}

// Turn-restriction flags (restrictions.py) and the U-turn penalty that
// steers searches around a banned turn the long way rather than by
// turning straight back (not counted in the reported time).
var TURN_ONLY = 1, TURN_CAR = 2, TURN_BIKE = 4;
var U_TURN_PENALTY_S = { drive: 45, bike: 20, walk: 0 };

function SpatialGraph(index, maxResidentBytes) {
  this._index = index;
  this._cells = new Map();
  this._inFlight = new Map();
  this._lru = new Map();
  this._residentBytes = 0;
  this._maxResidentBytes = maxResidentBytes || 64 * 1024 * 1024;
  this._maxConcurrent = 4;
  this._activeFetches = 0;
  this._queue = [];
  // Last cell looked up by node id — consecutive A* pops are almost
  // always in the same (cell-major numbered) cell, so this turns the
  // per-pop binary search over numCells into one range check.
  this._lastCid = -1;
  this._lastBase = 0;
  this._lastEnd = 0;
  this._lastTouched = -1;
  this.isSpatial = true;
  this.numNodes = index.numNodes;
  this.numEdges = index.numEdges;
  this.numNames = index.numNames;
  this.nodesScaled = index.nodesScaled;
  this.NO_GEOM = 0xFFFFFFFF;
  this.getName = function(idx) { return index.getName(idx); };
}
SpatialGraph.prototype._cellPath = function(cid) {
  var s = String(cid);
  while (s.length < 5) s = '0' + s;
  return 'routing-data/graph-cell-' + s + '.bin';
};
SpatialGraph.prototype.compact = function(keep) {
  keep = (keep === undefined) ? 4 : keep;
  while (this._lru.size > keep) {
    var cid = this._lru.keys().next().value;
    this._lru.delete(cid);
    var cell = this._cells.get(cid);
    if (cell) this._residentBytes -= cell.byteLength;
    this._cells.delete(cid);
  }
  this._lastTouched = -1;
};
SpatialGraph.prototype._touch = function(cid) {
  if (cid === this._lastTouched) return;
  this._lru.delete(cid);
  this._lru.set(cid, true);
  this._lastTouched = cid;
};
SpatialGraph.prototype._evictToBudget = function(protectCid) {
  while (this._residentBytes > this._maxResidentBytes && this._lru.size > 1) {
    var cid = this._lru.keys().next().value;
    this._lru.delete(cid);
    if (cid === protectCid) {
      this._lru.set(cid, true);
      continue;
    }
    var cell = this._cells.get(cid);
    if (cell) this._residentBytes -= cell.byteLength;
    this._cells.delete(cid);
  }
  this._lastTouched = -1;
};
SpatialGraph.prototype._drainQueue = function() {
  var self = this;
  while (self._activeFetches < self._maxConcurrent && self._queue.length) {
    (function(task) {
      self._activeFetches++;
      var fetchT0 = nowMs();
      var bodyT0 = 0;
      fetch(BASE_URL + self._cellPath(task.cid))
        .then(function(r) {
          if (!r.ok) throw new Error('cell HTTP ' + r.status + ' for ' + task.cid);
          if (_profile) _profile.cellHttpMs += nowMs() - fetchT0;
          bodyT0 = nowMs();
          return r.arrayBuffer();
        })
        .then(function(buf) {
          if (_profile) {
            _profile.cellBodyMs += nowMs() - bodyT0;
            _profile.cellFetchMs += nowMs() - fetchT0;
            _profile.cellBytes += buf.byteLength;
          }
          var parseT0 = nowMs();
          var cell = parseRoutingCell(self._index, task.cid, buf);
          if (_profile) _profile.cellParseMs += nowMs() - parseT0;
          self._cells.set(task.cid, cell);
          self._residentBytes += cell.byteLength;
          self._touch(task.cid);
          self._evictToBudget(task.cid);
          task.resolve(cell);
        })
        .catch(task.reject)
        .then(function() {
          self._inFlight.delete(task.cid);
          self._activeFetches--;
          self._drainQueue();
        });
    })(self._queue.shift());
  }
};
// Resident cell or null — no promise, no fetch. The A* inner loop
// uses this first and only falls back to the async _ensureCell on a
// miss, which keeps the hot path free of per-pop promise churn.
SpatialGraph.prototype.cellIfResident = function(cid) {
  var cell = this._cells.get(cid);
  if (cell === undefined) return null;
  if (_profile) _profile.cellHits++;
  this._touch(cid);
  return cell;
};
SpatialGraph.prototype._ensureCell = function(cid, priority) {
  var self = this;
  if (self._cells.has(cid)) {
    if (_profile) _profile.cellHits++;
    self._touch(cid);
    return Promise.resolve(self._cells.get(cid));
  }
  if (self._inFlight.has(cid)) {
    // Same cell already in flight — count as a near-miss; the actual
    // wait time gets attributed via the original fetch's timing.
    if (_profile) _profile.cellHits++;
    return self._inFlight.get(cid);
  }
  if (_profile) _profile.cellMisses++;
  var resolveTask, rejectTask;
  var p = new Promise(function(resolve, reject) {
    resolveTask = resolve;
    rejectTask = reject;
  });
  self._inFlight.set(cid, p);
  var task = { cid: cid, resolve: resolveTask, reject: rejectTask };
  if (priority === false) self._queue.push(task);
  else self._queue.unshift(task);
  self._drainQueue();
  return p;
};
// Cell id owning a node, with a one-entry cache in front of the
// binary search (v3) / coordinate hash (v1, v2).
SpatialGraph.prototype.cellForNode = function(n) {
  if (this._index.version === 3) {
    if (n >= this._lastBase && n < this._lastEnd) return this._lastCid;
    var cid = this._index.cellForNode(n);
    if (cid >= 0) {
      this._lastCid = cid;
      this._lastBase = this._index.cellBaseNode[cid];
      this._lastEnd = this._lastBase + this._index.cellNodeCount[cid];
    }
    return cid;
  }
  return this._index.cellForNode(n);
};
// Cell a node's coordinates live in when resident, else null. For
// v1/v2 indexes coordinates come from the index and no cell is needed
// (returns the index object as a truthy sentinel).
SpatialGraph.prototype._coordCellSync = function(n, cid) {
  if (this._index.version !== 3) return this._index;
  return this.cellIfResident(cid);
};
// Latitude / longitude (E7) of node `n` given the resident cell (or the
// index sentinel from _coordCellSync).
SpatialGraph.prototype._latE7 = function(n, holder) {
  if (holder === this._index) return this._index.nodeLatE7(n);
  return holder.nodesScaled[(n - holder.baseNode) * 2];
};
SpatialGraph.prototype._lonE7 = function(n, holder) {
  if (holder === this._index) return this._index.nodeLonE7(n);
  return holder.nodesScaled[(n - holder.baseNode) * 2 + 1];
};
SpatialGraph.prototype.nodeCoordsE7 = function(globalNodeIdx) {
  var self = this;
  var cid = self.cellForNode(globalNodeIdx);
  if (cid < 0) return Promise.reject(new Error('node out of range: ' + globalNodeIdx));
  if (self._index.version !== 3) {
    return Promise.resolve([
      self._index.nodeLatE7(globalNodeIdx),
      self._index.nodeLonE7(globalNodeIdx),
    ]);
  }
  return self._ensureCell(cid).then(function(cell) {
    var local = globalNodeIdx - cell.baseNode;
    return [cell.nodesScaled[local * 2], cell.nodesScaled[local * 2 + 1]];
  });
};
// Snap candidates are ranked by planar distance (longitude scaled by
// cos(lat)), but the nearest vertex is not always a usable one: a
// footpath vertex (all out-edges no-motor) or a four-node pier / private
// drive fragment that never reconnects to the road network. The car A*
// can't leave such a node, so a snap there guarantees "No route found"
// even though a perfectly good road is a few metres further. We keep the
// SNAP_CANDIDATES nearest car-ok vertices and return the first one from
// which a bounded forward BFS reaches SNAP_MIN_REACH nodes; if none does
// (tiny island ZIM), the nearest is returned as before. A candidate is
// only preferred over a nearer one when it is at most SNAP_MAX_EXTRA_M
// further away, so a legitimate one-way dead end right under the tap
// still wins over a road a kilometre off.
var SNAP_CANDIDATES = 6;
var SNAP_CANDIDATES_WIDE = 48;  // second pass, when none of the first reaches
var SNAP_WIDE_EXTRA_M = 150;     // ...and only this far past the nearest (not an island hop)
var SNAP_MIN_REACH = 32;
var SNAP_MAX_EXTRA_M = 1000;

// `mode` is 'origin' (default) or 'dest'. The reach test is a *forward*
// BFS, which is the right question for an origin (can the car leave?)
// but the wrong one for a destination: the end of a one-way spur, a
// drop-off lane or a one-way road cut at the extract boundary has a
// drivable incoming edge and no outgoing one, so its forward reach is
// 1 and it would lose to any road vertex within SNAP_MAX_EXTRA_M even
// though the router can reach it (Silicon Valley: 37 of 40 such sinks
// were displaced 3–178 m). For 'dest' an edgeless vertex with a
// drivable incoming edge in its own cell is accepted as well.
//
// `travel` 'walk' / 'bike' keeps only vertices with an out-edge the mode
// may use (edgeCostWB), records against a one-way included; there is no
// edgeless case — a walk/bike graph gives every vertex they can reach a
// way out, so a vertex without one is a motorway end or similar.
SpatialGraph.prototype.snapNearestNode = async function(latE7, lonE7, mode, travel, exclude) {
  var forDest = (mode === 'dest');
  travel = travelMode(travel);
  var drive = (travel === 'drive');
  var scale = this._index.cellScale;
  // Longitude degrees shrink with latitude — without this the snap
  // picks the nearest node in degree space, which at 60°N can be a
  // node 2x further away on the ground than the true nearest.
  var cosLat = Math.max(0.05, Math.cos(latE7 / 1e7 * DEG2RAD));
  var candidates = [];
  for (var cid = 0; cid < this._index.numCells; cid++) {
    var la = this._index.cellLatIdx[cid];
    var lo = this._index.cellLonIdx[cid];
    var latMin = la * 10_000_000 / scale;
    var latMax = (la + 1) * 10_000_000 / scale;
    var lonMin = lo * 10_000_000 / scale;
    var lonMax = (lo + 1) * 10_000_000 / scale;
    var dlat = latE7 < latMin ? latMin - latE7 : latE7 > latMax ? latE7 - latMax : 0;
    var dlon = lonE7 < lonMin ? lonMin - lonE7 : lonE7 > lonMax ? lonE7 - lonMax : 0;
    dlon *= cosLat;
    candidates.push([dlat * dlat + dlon * dlon, cid]);
  }
  candidates.sort(function(a, b) { return a[0] - b[0]; });
  var self = this;
  // The `width` nearest usable vertices, ascending (ties: scan order).
  async function shortlist(width, bound) {
    // best[] holds up to width {dist, node, lat, lon}, ascending.
    var best = [];
    var worstKept = bound || Infinity;  // dist of best[width-1] once full
    for (var i = 0; i < candidates.length && candidates[i][0] <= worstKept; i++) {
      var cell = await self._ensureCell(candidates[i][1]);
      var v2 = !!cell.nodesScaled;
      var cellAdj = cell.cellAdj, edges = cell.edges;
      for (var local = 0; local < cell.nodeCount; local++) {
        var globalNode = v2 ? cell.baseNode + local : cell.cellNodesGlobal[local];
        var nlat = v2 ? cell.nodesScaled[local * 2] : self._index.nodeLatE7(globalNode);
        var nlon = v2 ? cell.nodesScaled[local * 2 + 1] : self._index.nodeLonE7(globalNode);
        var ndlat = nlat - latE7;
        var ndlon = (nlon - lonE7) * cosLat;
        var dist = ndlat * ndlat + ndlon * ndlon;
        if (dist >= worstKept) continue;
        if (exclude && exclude.has(globalNode)) continue;
        // Skip nodes whose outgoing edges are ALL no-motor-vehicle (a
        // footpath vertex next to the road). A node with no outgoing
        // edges at all stays eligible — it's the end of a one-way and a
        // perfectly good destination. Checked lazily, only when a node
        // would make the shortlist, so the scan's per-node cost stays tiny.
        // A speed-0 out-edge (the walk/bike record for travel against a
        // one-way) counts as absent: a one-way's end stays a sink.
        var eStart = cellAdj[local], eEnd = cellAdj[local + 1];
        var real = 0, carOk = false;
        if (drive) {
          for (var ei = eStart; ei < eEnd; ei++) {
            if ((edges[ei * 5 + 1] >>> 24) === 0) continue;
            real++;
            if (!isNoMotor(edges[ei * 5 + 4])) { carOk = true; break; }
          }
          if (real === 0) carOk = true;
        } else {
          real = 1;
          for (var wi = eStart; wi < eEnd; wi++) {
            if (edgeCostWB(travel, edges[wi * 5 + 1], edges[wi * 5 + 4]) >= 0) {
              carOk = true; break;
            }
          }
        }
        if (!carOk) continue;
        var k = best.length;
        while (k > 0 && best[k - 1].dist > dist) k--;
        best.splice(k, 0, { dist: dist, node: globalNode, lat: nlat, lon: nlon,
                            edgeless: (real === 0) });
        if (best.length > width) best.pop();
        if (best.length === width) worstKept = best[best.length - 1].dist;
      }
    }
    return best;
  }
  // The nearest SNAP_CANDIDATES first; only when none of them reaches the
  // network, the nearest SNAP_CANDIDATES_WIDE (a big station: Zurich HB's
  // dozen nearest walk vertices are all two-node platform and escalator
  // fragments), and then only SNAP_WIDE_EXTRA_M past the nearest: a tap on
  // a small island stays there (the re-snap decides about islands). The
  // fallback stays the nearest vertex.
  var fallback = null;
  var passes = [[SNAP_CANDIDATES, SNAP_MAX_EXTRA_M], [SNAP_CANDIDATES_WIDE, SNAP_WIDE_EXTRA_M]];
  var prev = null;
  for (var w = 0; w < passes.length; w++) {
    // The wide pass: pass 1's candidates are its first ones (same order)
    // and all failed, so start after them; and scan only vertices inside
    // its distance limit (nothing past it can be picked).
    var start = 0, best;
    if (prev) {
      var lim2 = Math.sqrt(prev[0].dist) + passes[w][1] * 90;
      if (prev.length < SNAP_CANDIDATES || Math.sqrt(prev[prev.length - 1].dist) > lim2) break;
      best = await shortlist(passes[w][0], lim2 * lim2 * (1 + 1e-9) + 1);
      start = prev.length;
    } else {
      best = await shortlist(passes[w][0]);
    }
    prev = best;
    if (best.length === 0) throw new Error('no routing nodes');
    if (fallback === null) fallback = best[0];
    // 1 m ≈ 90 e7-units of latitude (and of cos-scaled longitude).
    var limitR = Math.sqrt(best[0].dist) + passes[w][1] * 90;
    for (var c = start; c < best.length; c++) {
      if (Math.sqrt(best[c].dist) > limitR) break;
      if (await this._reachesAtLeast(best[c].node, SNAP_MIN_REACH, travel)) {
        return { node: best[c].node, lat: best[c].lat / 1e7, lon: best[c].lon / 1e7 };
      }
      if (forDest && best[c].edgeless
          && await this._hasDrivableIncomingInOwnCell(best[c].node, travel)) {
        return { node: best[c].node, lat: best[c].lat / 1e7, lon: best[c].lon / 1e7 };
      }
    }
  }
  return { node: fallback.node, lat: fallback.lat / 1e7, lon: fallback.lon / 1e7 };
};

// True when some edge in `node`'s own cell targets it and is drivable.
// A one-way sink's predecessor is almost always in the same cell (the
// spur is a few hundred metres long); a predecessor in a neighbouring
// cell is missed and the vertex then falls back to the origin rule.
// Deterministic (never depends on which other cells happen to be
// resident) so the Python reference can mirror it exactly.
SpatialGraph.prototype._hasDrivableIncomingInOwnCell = async function(node, travel) {
  travel = travelMode(travel);
  var cid = this.cellForNode(node);
  if (cid < 0) return false;
  var cell = this.cellIfResident(cid);
  if (cell === null) cell = await this._ensureCell(cid);
  var edges = cell.edges;
  for (var ei = 0, n = cell.edgeCount; ei < n; ei++) {
    if (edges[ei * 5] !== node) continue;
    if (!edgeUsable(travel, edges[ei * 5 + 1], edges[ei * 5 + 4])) continue;
    return true;
  }
  return false;
};

// Bounded forward BFS over edges `travel` may use (driving when
// omitted): true once `limit` distinct
// nodes are reachable from `node` (itself included). Cheap — a real road
// vertex hits the limit within a few hops — and it only ever touches
// the one or two cells around the snap point.
SpatialGraph.prototype._reachesAtLeast = async function(node, limit, travel) {
  travel = travelMode(travel);
  var seen = new Set([node]);
  var queue = [node];
  var head = 0;
  while (head < queue.length) {
    if (seen.size >= limit) return true;
    var cur = queue[head++];
    var cid = this.cellForNode(cur);
    if (cid < 0) continue;
    var cell = this.cellIfResident(cid);
    if (cell === null) cell = await this._ensureCell(cid);
    var local = cell.localIdxFor(cur);
    if (local < 0) continue;
    var edges = cell.edges;
    var eEnd = cell.cellAdj[local + 1];
    for (var ei = cell.cellAdj[local]; ei < eEnd; ei++) {
      if (!edgeUsable(travel, edges[ei * 5 + 1], edges[ei * 5 + 4])) continue;
      var t = edges[ei * 5];
      if (!seen.has(t)) { seen.add(t); queue.push(t); if (seen.size >= limit) return true; }
    }
  }
  return seen.size >= limit;
};
// Promise-returning edge list (kept for tooling / debugging; the A*
// loops read the cell's typed arrays directly).
SpatialGraph.prototype.edgesOfNode = function(globalNodeIdx) {
  var cid = this.cellForNode(globalNodeIdx);
  if (cid < 0) return Promise.resolve([]);
  return this._ensureCell(cid).then(function(cell) {
    var local = cell.localIdxFor(globalNodeIdx);
    if (local < 0) return [];
    var eStart = cell.cellAdj[local];
    var eEnd = cell.cellAdj[local + 1];
    var out = [];
    for (var ei = eStart; ei < eEnd; ei++) {
      var base = ei * 5;
      out.push([
        cell.edges[base],
        cell.edges[base + 1],
        cell.edges[base + 2],
        cell.edges[base + 3],
        cell.edges[base + 4],
      ]);
    }
    return out;
  });
};
SpatialGraph.prototype.decodeGeomForEdge = function(sourceNodeIdx, geomLocal) {
  if (geomLocal === this.NO_GEOM) return Promise.resolve(null);
  var cid = this.cellForNode(sourceNodeIdx);
  if (cid < 0) return Promise.resolve(null);
  return this._ensureCell(cid).then(function(cell) {
    return cell.decodeGeomLocal(geomLocal);
  });
};

// === A* phase functions ===

async function findRouteSpatial(startNode, endNode, ctx) {
  return findRouteSpatialFiltered(startNode, endNode, false, ctx);
}

async function findRouteSpatialFiltered(startNode, endNode, highwayOnly, ctx) {
  var startCoords = await graph.nodeCoordsE7(startNode);
  var endCoords = await graph.nodeCoordsE7(endNode);
  var crowKm = haversine(
    startCoords[0] / 1e7,
    startCoords[1] / 1e7,
    endCoords[0] / 1e7,
    endCoords[1] / 1e7
  ) / 1000;
  // Skip the optimal pass on long routes:
  //   - highway-only > 1500 km (Toronto→Vancouver pattern; established
  //     threshold from project_routing_perf_canada.md)
  //   - full-graph    > 800 km (SD→Arcata pattern; SD→Arcata at
  //     1095 km consistently bails the 200 k-pop full-optimal at ~12 s
  //     and falls back to greedy anyway, so the optimal pass produces
  //     zero value. SF→LA at 559 km succeeds in optimal — calibrate
  //     between if we hit a region where 800 km is too tight.)
  // Admissible pass only up to OPTIMAL_MAX_CROW_KM on the full graph
  // (see the constant); highway-only legs keep the 1500 km rule from
  // project_routing_perf_canada.md. Beyond 800 km on the full graph the
  // weighted pass is skipped as well (SD→Arcata pattern: 1095 km bailed
  // every bounded pass; greedy ×1.875 converges in ~60k pops).
  var skipOptimal = (highwayOnly && crowKm > 1500)
                 || (!highwayOnly && crowKm > OPTIMAL_MAX_CROW_KM);
  var skipWeighted = !highwayOnly && crowKm > 800;
  // Test hook: options.popLimits = {fullOptimal, fullWeighted, fullGreedy,
  // hwOptimal, hwGreedy} shrinks the budgets so a unit-test-sized graph
  // can exercise the bail → weighted → greedy chain.
  var lim = (ctx && ctx.options && ctx.options.popLimits) || {};
  var wbTravel = travelMode(ctx && ctx.options && ctx.options.travel) !== 'drive';
  var limOptimal  = highwayOnly ? (lim.hwOptimal || POP_LIMIT_HW_OPTIMAL)
                                : (lim.fullOptimal
                                   || (wbTravel ? POP_LIMIT_WB_OPTIMAL : POP_LIMIT_FULL_OPTIMAL));
  var limWeighted = lim.fullWeighted
                    || (wbTravel ? POP_LIMIT_WB_WEIGHTED : POP_LIMIT_FULL_WEIGHTED);
  var limGreedy   = highwayOnly ? (lim.hwGreedy || POP_LIMIT_HW_GREEDY)
                                : (lim.fullGreedy
                                   || (wbTravel ? POP_LIMIT_WB_GREEDY : POP_LIMIT_FULL_GREEDY));
  // Walking and cycling search large budgets (POP_LIMIT_WB_*): a
  // destination in a sealed pocket would cost the whole first pass
  // (1.2M pops) before the check below ran. Check first: it is a bounded
  // BFS (DEST_COMPONENT_LIMIT nodes) around the destination.
  if (wbTravel && !highwayOnly && await destComponentClosed(startNode, endNode, ctx)) {
    if (ctx) { ctx.bailed = false; ctx.unreachable = true; }
    debugStats(ctx, 'destination pocket closed — unreachable', 0);
    return null;
  }
  if (!skipOptimal) {
    var optimal = await findRouteSpatialAStar(
      startNode, endNode, highwayOnly, /*greedy*/ 1.0, limOptimal, ctx);
    if (optimal) return optimal;
    if (ctx && ctx.cancelled && ctx.cancelled()) return null;
    // Optimal search exhausted the open set without bailing: the
    // destination is unreachable. No weighted pass can do better.
    if (ctx && !ctx.bailed) return null;
  }
  if (!highwayOnly) {
    // The budget ran out. Before spending another 600k pops (and, on
    // a state-sized graph, tens of seconds of cell thrash in two-pass)
    // check whether the destination is in a closed pocket that no
    // search can enter: ferry-only islands, roads cut at the extract
    // boundary. Cheap — one bounded BFS over the cells around the
    // destination, which the corridor pre-warm already fetched.
    if (await destComponentClosed(startNode, endNode, ctx)) {
      if (ctx) { ctx.bailed = false; ctx.unreachable = true; }
      debugStats(ctx, 'destination pocket closed — unreachable', 0);
      return null;
    }
    if (!skipWeighted) {
      var weighted = await findRouteSpatialAStar(
        startNode, endNode, highwayOnly, WEIGHT_FULL_WEIGHTED, limWeighted, ctx);
      if (weighted) return weighted;
      if (ctx && ctx.cancelled && ctx.cancelled()) return null;
      // Weighted A* expands the same edge set; an exhausted open set is
      // still a proof of unreachability.
      if (ctx && !ctx.bailed) return null;
    }
  }
  return findRouteSpatialAStar(
    startNode, endNode, highwayOnly,
    /*greedy*/ highwayOnly ? 2.0 : GREEDY_WEIGHT_FULL, limGreedy, ctx);
}

// Is the destination in a pocket no search can enter? Two steps:
//  1. bounded forward BFS from `endNode` over drivable edges. If it
//     reaches DEST_COMPONENT_LIMIT nodes or touches `startNode` the
//     destination is not in a small pocket — return false.
//  2. the BFS exhausted: `seen` is the pocket. "Closed" must mean no
//     drivable edge ENTERS it from outside — a one-way sink (drop-off
//     spur, road cut at the extract edge; 234 such vertices on Silicon
//     Valley), a one-way parking loop or a service spur has a forward
//     component of one or a few nodes yet is perfectly reachable, and
//     the snapper deliberately accepts such vertices as destinations.
//     So scan the edge lists of every cell the pocket touches (already
//     resident from step 1) for an edge whose source is outside the
//     pocket and whose target is inside; any such entrance → false.
// Only a pocket with no entrance in those cells is closed (ferry-only
// island: Charlotte → Ocracoke took 43 s of two-pass cell thrash before
// this check, 0.3 s after). An entrance whose source vertex lives in a
// neighbouring cell that the pocket itself does not touch is missed;
// that needs a pocket straddling a cell boundary whose only entrance
// comes from a third cell — accepted, and the consequence is just the
// old behaviour (full budget spent, "No route found").
async function destComponentClosed(startNode, endNode, ctx) {
  var travel = travelMode(ctx && ctx.options && ctx.options.travel);
  var seen = new Set([endNode]);
  var queue = [endNode];
  var head = 0;
  var cids = new Set();
  while (head < queue.length) {
    if (seen.size >= DEST_COMPONENT_LIMIT) return false;
    if (ctx && ctx.cancelled && ctx.cancelled()) return false;
    var cur = queue[head++];
    if (cur === startNode) return false;
    var cid = graph.cellForNode(cur);
    if (cid < 0) continue;
    cids.add(cid);
    var cell = graph.cellIfResident(cid);
    if (cell === null) cell = await graph._ensureCell(cid);
    var local = cell.localIdxFor(cur);
    if (local < 0) continue;
    var edges = cell.edges;
    var eEnd = cell.cellAdj[local + 1];
    for (var ei = cell.cellAdj[local]; ei < eEnd; ei++) {
      if (!edgeUsable(travel, edges[ei * 5 + 1], edges[ei * 5 + 4])) continue;
      var t = edges[ei * 5];
      if (!seen.has(t)) {
        seen.add(t);
        queue.push(t);
        if (seen.size >= DEST_COMPONENT_LIMIT) return false;
      }
    }
  }
  if (seen.has(startNode)) return false;
  // Step 2: any drivable edge from outside the pocket into it? An edge
  // lives in its SOURCE node's cell, so an entrance from across a cell
  // boundary is in a neighbouring cell: scan those too.
  var idx = graph._index;
  if (idx.cellForCoords && idx.cellScale > 0) {
    Array.from(cids).forEach(function(c) {
      for (var dy = -1; dy <= 1; dy++) {
        for (var dx = -1; dx <= 1; dx++) {
          var la = idx.cellLatIdx[c] + dy, lo = idx.cellLonIdx[c] + dx;
          var nid = idx.cellForCoords(Math.round((la + 0.5) * 1e7 / idx.cellScale),
                                      Math.round((lo + 0.5) * 1e7 / idx.cellScale));
          if (nid >= 0) cids.add(nid);
        }
      }
    });
  }
  // Fetch the cells not yet resident together (the fetch queue runs a
  // few at a time) rather than one round-trip after another: a pocket's
  // neighbours are 6-11 cells. They land in the same LRU cell cache
  // (64 MB budget) a one-by-one fetch would fill.
  var cidList = Array.from(cids);
  var pcells = await Promise.all(cidList.map(function(c) {
    var rc = graph.cellIfResident(c);
    return rc !== null ? rc : graph._ensureCell(c);
  }));
  for (var ci = 0; ci < pcells.length; ci++) {
    var pcell = pcells[ci];
    var pEdges = pcell.edges, pAdj = pcell.cellAdj;
    var v2 = !!pcell.nodesScaled;
    for (var pl = 0; pl < pcell.nodeCount; pl++) {
      var src = v2 ? pcell.baseNode + pl : pcell.cellNodesGlobal[pl];
      if (seen.has(src)) continue;
      var pEnd = pAdj[pl + 1];
      for (var pe = pAdj[pl]; pe < pEnd; pe++) {
        if (!seen.has(pEdges[pe * 5])) continue;
        if (!edgeUsable(travel, pEdges[pe * 5 + 1], pEdges[pe * 5 + 4])) continue;
        return false;  // entrance from outside: the pocket is enterable
      }
    }
  }
  if (ctx) ctx.pocket = seen;   // for findRoute's re-snap
  return true;
}

// The nodes a route from `startNode` can reach, when they are a small
// sealed set (DEST_COMPONENT_LIMIT) without `endNode`: a start snapped
// into a pocket (a road stub the extract cut off). Else null.
async function startPocket(startNode, endNode, travel) {
  var seen = new Set([startNode]);
  var queue = [startNode];
  var head = 0;
  while (head < queue.length) {
    if (seen.size >= DEST_COMPONENT_LIMIT) return null;
    var cur = queue[head++];
    if (cur === endNode) return null;
    var cid = graph.cellForNode(cur);
    if (cid < 0) continue;
    var cell = graph.cellIfResident(cid);
    if (cell === null) cell = await graph._ensureCell(cid);
    var local = cell.localIdxFor(cur);
    if (local < 0) continue;
    var edges = cell.edges;
    var eEnd = cell.cellAdj[local + 1];
    for (var ei = cell.cellAdj[local]; ei < eEnd; ei++) {
      if (!edgeUsable(travel, edges[ei * 5 + 1], edges[ei * 5 + 4])) continue;
      var t = edges[ei * 5];
      if (!seen.has(t)) { seen.add(t); queue.push(t); }
    }
  }
  return seen.has(endNode) ? null : seen;
}

// Snap the query point again, outside `pocket`, when the new vertex is
// about as close to it as the old one was (so a tap on a ferry-only
// island stays "no route" instead of becoming a route to the mainland).
async function resnapOutside(query, mode, travel, pocket, oldNode) {
  if (!query || typeof query.lat !== 'number' || typeof query.lon !== 'number') return null;
  var oc = await graph.nodeCoordsE7(oldNode);
  var d0 = haversine(query.lat, query.lon, oc[0] / 1e7, oc[1] / 1e7);
  var alt = await graph.snapNearestNode(Math.round(query.lat * 1e7), Math.round(query.lon * 1e7),
                                        mode, travel, pocket);
  if (!alt || pocket.has(alt.node)) return null;
  var d1 = haversine(query.lat, query.lon, alt.lat, alt.lon);
  return d1 <= d0 * 1.5 + RESNAP_EXTRA_M ? alt : null;
}
var RESNAP_EXTRA_M = 500;

async function findRouteSpatialAStar(startNode, endNode, highwayOnly,
                                      GREEDY_WEIGHT, POP_LIMIT, ctx) {
  var endCoords = await graph.nodeCoordsE7(endNode);
  var endLat = endCoords[0] / 1e7;
  var endLon = endCoords[1] / 1e7;
  var startCoords = await graph.nodeCoordsE7(startNode);
  var startLat = startCoords[0] / 1e7;
  var startLon = startCoords[1] / 1e7;
  var travel = travelMode(ctx && ctx.options && ctx.options.travel);
  var drive = (travel === 'drive');
  var hScale = GREEDY_WEIGHT / (drive ? HEURISTIC_MPS : TRAVEL_HEURISTIC_KMH[travel] / 3.6);
  var isV3 = graph._index.version === 3;

  if (ctx) ctx.bailed = false;

  // Turn restrictions (cars and bikes; spatial_astar.find_route_spatial
  // is the reference). A search state is a node, or a virtual state:
  // a node plus the restriction paths the route is part-way along,
  // numbered from numNodes up (vNode / vMatch); and second arrivals, ALT_BASE
// + node: the U-turn penalty depends on where a node was reached from, so
// a node keeps, beside its best arrival, its best arrival from another
// neighbour while that is within one penalty of the best; expanded, it
// only goes back toward the best arrival's neighbour (the one move the
// best pays the penalty for). spatial_astar has the same rule.
// options.turnRestrictions
  // === false (or ?turns=off) ignores them.
  var turnsOn = !(ctx && ctx.options && ctx.options.turnRestrictions === false);
  var mbit = !turnsOn ? 0 : drive ? TURN_CAR : travel === 'bike' ? TURN_BIKE : 0;
  var uturn = turnsOn ? U_TURN_PENALTY_S[travel] : 0;
  var numNodes = graph._index.numNodes;
  var vNode = [], vMatch = [], vKey = new Map();
  var penOf = new Map();       // state -> U-turn penalty paid to reach it
  var ALT_BASE = 0x40000000;
  function nodeOf(sid) {
    if (sid >= ALT_BASE) return sid - ALT_BASE;
    return sid < numNodes ? sid : vNode[sid - numNodes];
  }
  function mainBack(node) {
    var ms = table.find(node);
    return (ms >= 0 && table.prev[ms] >= 0) ? nodeOf(table.prev[ms]) : -1;
  }
  // A second arrival can only matter after a restriction forced a detour:
  // offered only when the best arrival's chain passes a virtual state
  // within TAINT_HOPS steps (spatial_astar.tainted).
  // taintOf: state -> steps since a virtual state (1..TAINT_HOPS), set
  // when its predecessor is; a state is expanded only once closed, so its
  // chain no longer changes and this equals walking it.
  var TAINT_HOPS = 16;
  var taintOf = new Map();
  function isVirtual(sid) { return sid >= numNodes && sid < ALT_BASE; }
  function tainted(sid) { return sid >= 0 && (isVirtual(sid) || taintOf.has(sid)); }
  function setTaint(sid, from) {
    var t = isVirtual(from) ? 1 : (taintOf.get(from) || 0) && taintOf.get(from) + 1;
    if (t && t < TAINT_HOPS) taintOf.set(sid, t);
    else if (taintOf.size) taintOf.delete(sid);
  }
  async function hOf(node) {
    var c = await graph.nodeCoordsE7(node);
    return haversine(c[0] / 1e7, c[1] / 1e7, endLat, endLon) * hScale;
  }
  // Offer a second arrival at `node` (see above); pushes it when kept.
  async function offerAlt(node, g, prevSid, prevEi, pen, mb) {
    var aid = ALT_BASE + node;
    var as = table.find(aid);
    if (as >= 0 && table.closed[as]) return;
    if (as >= 0 && !(g < table.g[as]
                     || (table.prev[as] >= 0 && nodeOf(table.prev[as]) === mb))) return;
    if (as < 0) as = table.insert(aid);
    table.g[as] = g;
    table.prev[as] = prevSid;
    table.prevEi[as] = prevEi;
    setTaint(aid, prevSid);
    if (pen) penOf.set(aid, pen); else if (penOf.size) penOf.delete(aid);
    open.push(g + await hOf(node), aid);
  }
  var goalSid = -1;

  var table = new NodeTable(1 << 14);
  var startSlot = table.insert(startNode);
  table.g[startSlot] = 0;

  var open = new NodeHeap(1 << 14);
  open.push(haversine(startLat, startLon, endLat, endLon) * hScale, startNode);

  var pops = 0;
  var weightTag = (GREEDY_WEIGHT > 1.0) ? ' greedy×' + GREEDY_WEIGHT : ' optimal';
  var label = (highwayOnly ? 'A* highway-only' : 'A* full') + weightTag
            + (drive ? '' : ' ' + travel);
  var phaseT0 = nowMs();
  // Per-route profile: phases must land in THIS route's record even if
  // a newer route has since replaced the global `_profile`.
  var prof = (ctx && ctx.profile) || _profile;

  // Time-budget yielding. Decoupled from progress reporting:
  // - report every 2k pops (cheap; main thread can re-render the
  //   progress indicator at most that often)
  // - yield to the worker event loop every 50 ms of wall time so
  //   cancel/compact messages from the main thread can be processed
  //   without burning hundreds of ms before each yield
  var lastReportPops = 0;
  var lastYield = nowMs();
  var found = false;

  while (open.n > 0) {
    var curSid = open.pop();
    var current = nodeOf(curSid);
    pops++;
    if (pops - lastReportPops >= 2000) {
      debugStats(ctx, label, pops);
      lastReportPops = pops;
    }
    if ((pops & 63) === 0 && nowMs() - lastYield > 50) {
      var yT0 = nowMs();
      await new Promise(function(r) { setTimeout(r, 0); });
      if (prof) {
        prof.yields++;
        prof.yieldMs += nowMs() - yT0;
      }
      lastYield = nowMs();
      if (ctx && ctx.cancelled && ctx.cancelled()) {
        if (prof) prof.phases.push({
          label: label, pops: pops, ms: nowMs() - phaseT0,
          bailed: true, cancelled: true,
        });
        return null;
      }
    }
    if (pops > POP_LIMIT) {
      debugStats(ctx, label + ' BAIL (pop limit ' + POP_LIMIT + ')', pops);
      if (prof) prof.phases.push({
        label: label, pops: pops, ms: nowMs() - phaseT0,
        bailed: true,
      });
      if (ctx) ctx.bailed = true;
      return null;
    }
    if (current === endNode) { found = true; goalSid = curSid; break; }
    var cs = table.find(curSid);
    if (table.closed[cs]) continue;
    var altTo = -1;
    if (curSid >= ALT_BASE) {
      altTo = mainBack(current);
      if (altTo < 0 || altTo === nodeOf(table.prev[cs])) continue;
    } else if (curSid >= numNodes) {
      // A closed plain state here dominates this virtual one.
      // ... when it came the same way (the U-turn penalty depends on it).
      var plainSlot = table.find(current);
      if (plainSlot >= 0 && table.closed[plainSlot]
          && mainBack(current) === nodeOf(table.prev[cs])) continue;
    }
    table.closed[cs] = 1;
    var curG = table.g[cs];

    var cid = graph.cellForNode(current);
    if (cid < 0) continue;  // dangling reference; treat as dead end
    var cell = graph.cellIfResident(cid);
    if (cell === null) cell = await graph._ensureCell(cid);
    var local = cell.localIdxFor(current);
    if (local < 0) continue;
    if (_profile) _profile.edgeReqs++;
    var cellAdj = cell.cellAdj;
    var edges = cell.edges;
    var eEnd = cellAdj[local + 1];
    // Restrictions in force here: bans / only from paths ending at this
    // node, `cont` for paths that go on.
    var bans = null, only = null, cont = null;
    if (curSid >= numNodes && curSid < ALT_BASE) {
      var match = vMatch[curSid - numNodes];
      for (var mi = 0; mi < match.length; mi++) {
        var mm = match[mi], mp = mm.rec.path;
        if (mm.pos === mp.length - 2) {
          var last = mp[mp.length - 1];
          if (mm.rec.flags & TURN_ONLY) {
            if (only === null) only = [last];
            else only = only.filter(function(x) { return x === last; });
          } else {
            if (bans === null) bans = [];
            bans.push(last);
          }
        } else {
          if (cont === null) cont = new Map();
          var nx = mp[mm.pos + 1];
          var cl = cont.get(nx);
          var ent = { rec: mm.rec, cid: mm.cid, pos: mm.pos + 1 };
          if (cl) cl.push(ent); else cont.set(nx, [ent]);
        }
      }
      // An only_* whose turn this mode cannot take does not apply.
      if (only !== null) {
        var onlyOk = false;
        for (var oe = cellAdj[local]; oe < eEnd && !onlyOk; oe++) {
          if (only.indexOf(edges[oe * 5]) >= 0
              && edgeUsable(travel, edges[oe * 5 + 1], edges[oe * 5 + 4])
              && (!highwayOnly || isHighwayClass(edges[oe * 5 + 4]))) onlyOk = true;
        }
        if (!onlyOk) only = null;
      }
    }
    var backSlotPrev = table.prev[cs];
    var back = backSlotPrev >= 0 ? nodeOf(backSlotPrev) : -1;
    var otherExit = -1;   // computed on first U-turn candidate
    for (var ei = cellAdj[local]; ei < eEnd; ei++) {
      var base = ei * 5;
      var classAccess = edges[base + 4];
      var speedDist = edges[base + 1];
      var edgeCost;
      if (drive) {
        if (isNoMotor(classAccess)) continue;
        if (highwayOnly && !isHighwayClass(classAccess)) continue;
        var speed = speedDist >>> 24;
        if (speed === 0) continue;
        edgeCost = ((speedDist & 0xFFFFFF) / 10) / (speed / 3.6);
      } else {
        edgeCost = edgeCostWB(travel, speedDist, classAccess);
        if (edgeCost < 0) continue;
      }
      var target = edges[base];
      if (altTo >= 0 && target !== altTo) continue;
      if (bans !== null && bans.indexOf(target) >= 0) continue;
      if (only !== null && only.indexOf(target) < 0) continue;
      var pen = 0;
      if (uturn && target === back) {
        if (otherExit < 0) {
          otherExit = 0;
          for (var xe = cellAdj[local]; xe < eEnd; xe++) {
            if (edges[xe * 5] !== back
                && edgeUsable(travel, edges[xe * 5 + 1], edges[xe * 5 + 4])
                && (!highwayOnly || isHighwayClass(edges[xe * 5 + 4]))) { otherExit = 1; break; }
          }
        }
        if (otherExit) pen = uturn;
      }
      // The state reached: plain, or virtual when a restriction path
      // goes on or starts with this step.
      var tSid = target;
      var m2 = cont !== null ? cont.get(target) : undefined;
      if (mbit) {
        // Same cell as the source for most edges: no lookup then.
        var rcid, rcell;
        if (isV3 && target - cell.baseNode >= 0 && target - cell.baseNode < cell.nodeCount) {
          rcid = cid; rcell = cell;
        } else {
          rcid = graph.cellForNode(target);
          rcell = (rcid === cid) ? cell : graph.cellIfResident(rcid);
          if (rcell === null) rcell = await graph._ensureCell(rcid);
        }
        if (rcell.turns) {
          var roots = rcell.turns.byVia.get(target);
          if (roots) {
            for (var ri = 0; ri < roots.length; ri++) {
              if (roots[ri].path[0] !== current || !(roots[ri].flags & mbit)) continue;
              m2 = (m2 || []).concat([{ rec: roots[ri], cid: rcid, pos: 1 }]);
            }
          }
        }
      }
      if (m2 && m2.length) {
        var plainT = table.find(target);
        if (plainT >= 0 && table.closed[plainT] && mainBack(target) === current) continue;   // dominated
        var ids = m2.map(function(m) { return m.cid + ':' + m.rec.id + ':' + m.pos; }).sort();
        var vk = target + '|' + ids.join(',');
        tSid = vKey.get(vk);
        if (tSid === undefined) {
          tSid = numNodes + vNode.length;
          vKey.set(vk, tSid);
          vNode.push(target);
          vMatch.push(m2);
        }
      }
      var pslot = (tSid === target && uturn) ? table.find(target) : -1;
      if (pslot >= 0 && table.prev[pslot] >= 0
          && (tainted(table.prev[pslot]) || tainted(curSid))) {
        // A plain target: keep a second arrival (offerAlt) when this one
        // does not win, or when it replaces one from another neighbour.
        var newGp = curG + edgeCost + pen;
        var mb = (pslot >= 0 && table.prev[pslot] >= 0) ? nodeOf(table.prev[pslot]) : -1;
        if (pslot >= 0 && (table.closed[pslot] || newGp >= table.g[pslot])) {
          if (current !== mb && newGp < table.g[pslot] + uturn) {
            await offerAlt(target, newGp, curSid, ei, pen, mb);
          }
          continue;
        }
        if (pslot >= 0 && table.g[pslot] < Infinity && mb !== current) {
          await offerAlt(target, table.g[pslot], table.prev[pslot], table.prevEi[pslot],
                         penOf.get(target) || 0, current);
        }
      }
      var ts = table.find(tSid);
      if (ts >= 0 && table.closed[ts]) continue;
      var newG = curG + edgeCost + pen;
      if (ts < 0) ts = table.insert(tSid);
      if (newG < table.g[ts]) {
        table.g[ts] = newG;
        table.prev[ts] = curSid;
        table.prevEi[ts] = ei;
        if (uturn) setTaint(tSid, curSid);
        if (pen) penOf.set(tSid, pen); else if (penOf.size) penOf.delete(tSid);
        // Target coordinates for the heuristic. Same cell as the
        // source for the vast majority of edges; a neighbouring cell
        // is usually already resident (corridor prewarm) — only a
        // true miss pays a fetch.
        var tLat, tLon;
        if (!isV3) {
          tLat = graph._index.nodeLatE7(target) / 1e7;
          tLon = graph._index.nodeLonE7(target) / 1e7;
        } else {
          var tcid = graph.cellForNode(target);
          var tcell = (tcid === cid) ? cell : graph.cellIfResident(tcid);
          if (tcell === null) {
            tcell = await graph._ensureCell(tcid);
            // The await may have grown/rehashed nothing (only this
            // loop inserts) but `ts` is still valid; re-read defensively
            // in case a future edit adds inserts across the await.
            ts = table.find(tSid);
          }
          var tl = (target - tcell.baseNode) * 2;
          tLat = tcell.nodesScaled[tl] / 1e7;
          tLon = tcell.nodesScaled[tl + 1] / 1e7;
        }
        open.push(newG + haversine(tLat, tLon, endLat, endLon) * hScale, tSid);
      }
    }
  }
  debugStats(ctx, label + ' done', pops);
  if (prof) prof.phases.push({
    label: label, pops: pops, ms: nowMs() - phaseT0,
    bailed: false, visited: table.size, tableBytes: table.bytes(),
  });

  var endSlot = found ? table.find(goalSid) : -1;
  if (!found || endSlot < 0 || table.g[endSlot] === Infinity) return null;

  // Path reconstruction: walk predecessor links back to the start,
  // re-reading each edge from its (cell-local) slot. Cells may have
  // been evicted mid-search, so this path is async again — it runs
  // once per route, over the route's own cells only.
  // Driving: cost is time. Walking / cycling: the cost penalises busy
  // roads, so add up each edge's plain time instead.
  var totalTime = drive ? table.g[endSlot] : 0;
  var totalDist = 0;
  var segRev = [];   // per-edge {nameIdx, distM, flags}, end → start
  var pathRev = [];  // per-edge [[lon,lat], ...], end → start
  var n = endNode;
  var sid = goalSid;
  var slot = endSlot;
  while (sid !== startNode) {
    var sourceSid = table.prev[slot];
    var sourceEi = table.prevEi[slot];
    if (sourceSid < 0 || sourceEi < 0) return null;  // corrupt link
    if (drive && penOf.size) totalTime -= (penOf.get(sid) || 0);
    var sourceNode = nodeOf(sourceSid);
    var scid = graph.cellForNode(sourceNode);
    var scell = await graph._ensureCell(scid);
    var sb = sourceEi * 5;
    var sSpeedDist = scell.edges[sb + 1];
    var geomLocal = scell.edges[sb + 2];
    var nameIdx = scell.edges[sb + 3];
    var classAccess = scell.edges[sb + 4];
    var distM = (sSpeedDist & 0xFFFFFF) / 10;
    if (!drive) {
      edgeCostWB(travel, sSpeedDist, classAccess);
      totalTime += _wbTime;
    }
    var isRound = ((classAccess >>> 8) & 1) !== 0;
    var cls = classAccess & 0x1F;
    var isLink = (cls === 2 || cls === 4 || cls === 6 || cls === 8 || cls === 10);
    var manFlags = (isRound ? 1 : 0) | (isLink ? 2 : 0);
    totalDist += distM;
    segRev.push({ nameIdx: nameIdx, distM: distM, flags: manFlags });

    var fromCoords = await graph.nodeCoordsE7(sourceNode);
    var toCoords = await graph.nodeCoordsE7(n);
    var segment = [[fromCoords[1] / 1e7, fromCoords[0] / 1e7]];
    if (geomLocal !== graph.NO_GEOM) {
      var pts = scell.decodeGeomLocal(geomLocal);
      // Bit 11: the geometry is stored for the other direction (a walk/
      // bike record against a one-way shares the one-way's geometry).
      if (pts && (classAccess & GEOM_REVERSED_BIT)) pts = pts.slice().reverse();
      if (pts) for (var j = 0; j < pts.length; j++) segment.push(pts[j]);
    }
    segment.push([toCoords[1] / 1e7, toCoords[0] / 1e7]);
    pathRev.push(segment);

    n = sourceNode;
    sid = sourceSid;
    slot = table.find(sid);
    if (slot < 0) return null;
  }

  // Resolve road names so the main thread doesn't need access to the
  // names blob (which lives in the worker's index).
  var roads = [];
  for (var si = segRev.length - 1; si >= 0; si--) {
    var s = segRev[si];
    if (roads.length > 0
        && roads[roads.length - 1].nameIdx === s.nameIdx
        && roads[roads.length - 1].flags === s.flags) {
      roads[roads.length - 1].distM += s.distM;
    } else {
      roads.push({
        nameIdx: s.nameIdx,
        name: graph.getName(s.nameIdx),
        distM: s.distM,
        flags: s.flags,
      });
    }
  }

  var coords = [];
  for (var sp = pathRev.length - 1; sp >= 0; sp--) {
    var seg = pathRev[sp];
    var startI = (coords.length === 0) ? 0 : 1;
    for (var kk = startI; kk < seg.length; kk++) coords.push(seg[kk]);
  }
  if (coords.length === 0) {
    // start === end: a zero-length route. Give the caller a
    // degenerate but well-formed LineString so drawing/fitBounds
    // don't trip over an empty coordinate list.
    var pt = [startLon, startLat];
    coords.push(pt, pt.slice());
  }
  return { coords: coords, distance: totalDist, time: totalTime, roads: roads };
}

// Outgoing-edge BFS from `seedNode`; returns the first node that has a
// highway-tier outgoing edge, or null within `maxPops`.
async function findNearestHighwayNode(seedNode, maxPops) {
  var visited = new Set();
  visited.add(seedNode);
  var queue = [seedNode];
  var head = 0;
  var pops = 0;
  while (head < queue.length && pops < maxPops) {
    var current = queue[head++];
    pops++;
    var cid = graph.cellForNode(current);
    if (cid < 0) continue;
    var cell = graph.cellIfResident(cid);
    if (cell === null) cell = await graph._ensureCell(cid);
    var local = cell.localIdxFor(current);
    if (local < 0) continue;
    var edges = cell.edges;
    var eStart = cell.cellAdj[local];
    var eEnd = cell.cellAdj[local + 1];
    for (var ei = eStart; ei < eEnd; ei++) {
      // A highway edge a car can drive: not a no-motor or speed-0
      // (against-one-way) record, or leg B could not leave the node.
      var hca = edges[ei * 5 + 4];
      if (isHighwayClass(hca) && !isNoMotor(hca)
          && (edges[ei * 5 + 1] >>> 24) !== 0) return current;
    }
    for (var e2 = eStart; e2 < eEnd; e2++) {
      if (isNoMotor(edges[e2 * 5 + 4])) continue;
      var target = edges[e2 * 5];
      if (!visited.has(target)) {
        visited.add(target);
        queue.push(target);
      }
    }
  }
  return null;
}

async function findRouteSpatialTwoPass(startNode, endNode, ctx) {
  var hwSrc = await findNearestHighwayNode(startNode, 5000);
  var hwDst = await findNearestHighwayNode(endNode, 5000);
  if (hwSrc === null || hwDst === null) return null;

  var legA = (startNode === hwSrc)
    ? null
    : await findRouteSpatialFiltered(startNode, hwSrc, /*highwayOnly=*/false, ctx);
  if (legA === null && startNode !== hwSrc) return null;
  graph.compact(4);

  var legB = (hwSrc === hwDst)
    ? null
    : await findRouteSpatialFiltered(hwSrc, hwDst, /*highwayOnly=*/true, ctx);
  if (legB === null && hwSrc !== hwDst) return null;
  graph.compact(4);

  var legC = (hwDst === endNode)
    ? null
    : await findRouteSpatialFiltered(hwDst, endNode, /*highwayOnly=*/false, ctx);
  if (legC === null && hwDst !== endNode) return null;

  return concatenateLegs([legA, legB, legC]);
}

function concatenateLegs(legs) {
  var coords = [];
  var roads = [];
  var distance = 0;
  var time = 0;
  for (var i = 0; i < legs.length; i++) {
    var leg = legs[i];
    if (!leg) continue;
    var startIdx = (coords.length > 0) ? 1 : 0;
    for (var k = startIdx; k < leg.coords.length; k++) coords.push(leg.coords[k]);
    for (var r = 0; r < leg.roads.length; r++) {
      var road = leg.roads[r];
      var last = roads.length ? roads[roads.length - 1] : null;
      // Merge the join road across the leg boundary so the turn list
      // doesn't announce "continue on X" onto the road it's already on.
      if (last && last.nameIdx === road.nameIdx && last.flags === road.flags) {
        last.distM += road.distM;
      } else {
        roads.push({ nameIdx: road.nameIdx, name: road.name,
                     distM: road.distM, flags: road.flags });
      }
    }
    distance += leg.distance;
    time += leg.time;
  }
  return { coords: coords, distance: distance, time: time, roads: roads };
}

// A route; when there is none because an end was snapped into a sealed
// pocket (a road stub the extract cut off: Silicon Valley's southern edge
// near Pescadero gave a car destination 32 junctions joined to nothing),
// that end is snapped again outside the pocket (options.originQuery /
// destQuery: the points the user picked) and the route says where it now
// starts or ends (startMoved / endMoved: {node, lat, lon}).
async function findRoute(startNode, endNode, ctx) {
  var r = await findRouteCore(startNode, endNode, ctx);
  var opts = (ctx && ctx.options) || {};
  if (r || (ctx && ctx.cancelled && ctx.cancelled())
      || !(opts.destQuery || opts.originQuery)) return r;
  var travel = travelMode(opts.travel);
  var movedEnd = null;
  if (opts.destQuery) {
    // The destination's own pocket, found again: ctx.pocket may be one
    // a two-pass leg found. The cells are resident from the search.
    var pocket = await destComponentClosed(startNode, endNode, ctx) ? ctx.pocket : null;
    var altEnd = pocket && await resnapPast(opts.destQuery, 'dest', travel, pocket, endNode,
      function(n) { return destComponentClosed(startNode, n, ctx).then(function(c) {
        return c ? ctx.pocket : null; }); });
    if (altEnd) {
      debugStats(ctx, 'destination moved out of a closed pocket', 0);
      ctx.noTwoPass = true;
      r = await findRouteCore(startNode, altEnd.node, ctx);
      if (r) { r.endMoved = altEnd; return r; }
      endNode = altEnd.node;
      movedEnd = altEnd;
    }
  }
  if (opts.originQuery && !(ctx && ctx.cancelled && ctx.cancelled())) {
    var sp = await startPocket(startNode, endNode, travel);
    var altStart = sp && await resnapPast(opts.originQuery, 'origin', travel, sp, startNode,
      function(n) { return startPocket(n, endNode, travel); });
    if (altStart) {
      debugStats(ctx, 'start moved out of a closed pocket', 0);
      ctx.noTwoPass = true;
      r = await findRouteCore(altStart.node, endNode, ctx);
      if (r) {
        r.startMoved = altStart;
        if (movedEnd) r.endMoved = movedEnd;
      }
    }
  }
  return r;
}

// resnapOutside repeatedly (RESNAP_TRIES): a re-snap that lands in
// another sealed pocket (`pocketOf(node)` returns it, else null) excludes
// that one too and tries again. A vertex outside every pocket found, or null.
async function resnapPast(query, mode, travel, pocket, oldNode, pocketOf) {
  var excluded = new Set(pocket);
  for (var k = 0; k < RESNAP_TRIES; k++) {
    var alt = await resnapOutside(query, mode, travel, excluded, oldNode);
    if (!alt) return null;
    var p2 = await pocketOf(alt.node);
    if (!p2) return alt;
    p2.forEach(function(n) { excluded.add(n); });
  }
  return null;
}
var RESNAP_TRIES = 3;

async function findRouteCore(startNode, endNode, ctx) {
  if (ctx) { ctx.pocket = null; ctx.unreachable = false; }
  if (!graph.isSpatial) {
    throw new Error('worker only handles spatial graphs');
  }
  var startCoords = await graph.nodeCoordsE7(startNode);
  var endCoords = await graph.nodeCoordsE7(endNode);
  var startLat = startCoords[0] / 1e7;
  var startLon = startCoords[1] / 1e7;
  var endLat = endCoords[0] / 1e7;
  var endLon = endCoords[1] / 1e7;
  var crow = haversine(startLat, startLon, endLat, endLon);
  if (ctx && ctx.profile) ctx.profile.crowKm = crow / 1000;
  var override = ctx && ctx.options && ctx.options.route;
  // Two-pass rides the highway tier: driving only.
  var travel = travelMode(ctx && ctx.options && ctx.options.travel);
  if (travel !== 'drive' && override === 'two-pass') override = null;
  var isLong = crow > 100000;

  if (isLong && override !== 'full') {
    // Long-route pre-cleanup: drop cached cells and give the engine a
    // few turns to GC before allocating the next search's state.
    graph.compact(0);
    for (var i = 0; i < 3; i++) {
      await new Promise(function(r) { setTimeout(r, 50); });
    }
  }

  // Pre-warm the corridor: sample points along the great-circle line
  // and fire the cell fetches in parallel BEFORE A* starts. _ensureCell
  // de-dupes via _inFlight, so when A* later expands into one of these
  // cells it awaits the existing fetch promise instead of starting a
  // sequential round-trip. On a cold cache this collapses ~N×720ms of
  // per-cell I/O into one parallel ~720ms wave.
  _prewarmCorridor(startLat, startLon, endLat, endLon, crow);

  var routeResult = null;
  if (override !== 'two-pass') {
    routeResult = await findRouteSpatial(startNode, endNode, ctx);
  }
  // Strategy chain: full A* first (optimal → greedy). Two-pass only
  // when full A* ran out of budget — an exhausted open set means the
  // destination is genuinely unreachable and two-pass can't fix that.
  var cancelled = !!(ctx && ctx.cancelled && ctx.cancelled());
  if (!routeResult && !cancelled && travel === 'drive' && !(ctx && ctx.noTwoPass)
      && (override === 'two-pass' || (ctx && ctx.bailed))) {
    routeResult = await findRouteSpatialTwoPass(startNode, endNode, ctx);
  }
  graph.compact(8);
  return routeResult;
}

function _prewarmCorridor(startLat, startLon, endLat, endLon, crowM) {
  if (!graph || !graph._index || !graph._index.cellForCoords) return;
  var prewarmT0 = nowMs();
  // Sample density: one sample per ~25 km along the line, with a
  // floor of 20 samples for short routes. Dedupe by cell_id, so the
  // actual fetch count caps at the number of cells the line crosses.
  var samples = Math.max(20, Math.ceil((crowM / 1000) / 25));
  var seen = new Set();
  for (var i = 0; i <= samples; i++) {
    var t = i / samples;
    var lat = startLat + (endLat - startLat) * t;
    // The short way round across the antimeridian, back into [-180, 180].
    var dLon = endLon - startLon;
    dLon -= 360 * Math.round(dLon / 360);
    var lon = startLon + dLon * t;
    lon -= 360 * Math.round(lon / 360);
    var latE7 = Math.round(lat * 1e7);
    var lonE7 = Math.round(lon * 1e7);
    var cid = graph._index.cellForCoords(latE7, lonE7);
    if (cid < 0 || seen.has(cid)) continue;
    seen.add(cid);
    if (graph._cells.has(cid)) continue;
    // Kick off the fetch — A* awaits via _inFlight when it gets there.
    graph._ensureCell(cid, /*priority=*/false).catch(function() { /* ignore, A* will retry */ });
  }
  if (_profile) {
    _profile.prewarmCells = seen.size;
    _profile.prewarmMs = nowMs() - prewarmT0;
  }
}
