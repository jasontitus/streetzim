  // A* pathfinding (binary graph).
  // Returns a Promise — findRoute must ensure the SZGM geom companion is
  // available before reconstructing path coords for v5 split ZIMs. For
  // v4 inline the Promise resolves synchronously on its first tick.
  // Spatial-graph A*. Mirrors the monolithic findRoute() but fetches
  // edges / geoms per node via the SpatialGraph façade — cells load
  // asynchronously as the frontier crosses them. Return shape matches
  // the monolithic version so the caller in computeAndDrawRoute sees a
  // uniform result regardless of layout.
  // findRouteSpatial — full-graph A* on the spatial graph. Sparse-state
  // implementation lives in findRouteSpatialFiltered (with
  // highwayOnly=false this is the same algorithm); delegating here
  // keeps the call sites for short-route fallback and the
  // findRouteSpatialTwoPass legs working on the same code path.
  // The dense typed-array version that lived here OOM'd on long
  // routes (Tokyo→Oita, Tehran→Baghdad) — see the sparse-state
  // comment on findRouteSpatialFiltered.
  async function findRouteSpatial(startNode, endNode) {
    return findRouteSpatialFiltered(startNode, endNode, false);
  }

  // Highway-tier ordinals (motorway, motorway_link, trunk, trunk_link,
  // primary, primary_link). Bit-encoded into class_access bits 0..4 by
  // create_osm_zim.CLASS_ORDINAL. Edges in this set are the cross-country
  // backbone — A* on just these is ~10× cheaper for long routes.
  function isHighwayClass(classAccess) {
    var ord = classAccess & 0x1F;
    return ord >= 1 && ord <= 6;
  }
  // class_access bit 9: no motor vehicles (footway / steps / private).
  // Never expanded by the driving profile — see routing-worker.js.
  var NO_MOTOR_BIT = 0x200;
  // Ordinals 16..20 (path/footway/cycleway/pedestrian/steps) are never
  // drivable either; ZIMs built before bit 9 existed still carry them
  // in the car graph, so the ordinal is checked alongside the bit.
  function isNoMotor(classAccess) {
    if (classAccess & NO_MOTOR_BIT) return true;
    var ord = classAccess & 0x1F;
    return ord >= 16 && ord <= 20;
  }

  // ?debug=1 overlay — shows pops/cells/heap during routing so we can
  // tell whether a long route is bottlenecked on cell cache, visited-
  // node Maps, or something else. No-op when the flag isn't set.
  // Per-page-load only — used to be sticky via localStorage but that
  // surprised users who'd left the URL flag on once and couldn't get
  // rid of it. The cleanup below also wipes any old sticky key.
  var DEBUG_ROUTING = (function() {
    try { localStorage.removeItem('streetzim_debug'); } catch (e) {}
    try {
      return new URLSearchParams(location.search).get('debug') === '1';
    } catch (e) { return false; }
  })();
  var __routeDebugEl = null;
  var __routeDebugStart = 0;
  var __routeDebugLabel = 'idle';
  var __routeDebugPops = 0;
  // Always-on tick paints idle stats every 2s when ?debug=1 is set,
  // even when no route is in flight. That way the overlay is a
  // continuous "is debug on?" indicator and lets you watch heap
  // creep, cell-cache fill, etc. while you're just panning.
  function __debugPaint() {
    if (!DEBUG_ROUTING || !__routeDebugEl) return;
    var lines = [__routeDebugLabel];
    if (__routeDebugStart) {
      lines.push('elapsed: ' +
        ((performance.now() - __routeDebugStart) / 1000).toFixed(1) + 's');
    }
    if (__routeDebugPops) {
      lines.push('pops: ' + __routeDebugPops);
    }
    var cellMB = 0;
    if (typeof graph !== 'undefined' && graph && graph._cells) {
      // Sum payload buffers. Typed arrays are views over these buffers.
      graph._cells.forEach(function(cell) {
        if (!cell) return;
        cellMB += cell.byteLength || 0;
      });
      cellMB = cellMB / (1024 * 1024);
      lines.push('cells: ' + graph._cells.size + ' = ' + cellMB.toFixed(0)
        + ' / ' + ((graph._maxResidentBytes || 0) / 1024 / 1024).toFixed(0)
        + ' MB budget');
    } else {
      lines.push('graph: not loaded');
    }
    // Estimated heap. Each Map entry in V8 is ~80 bytes (key + value
    // boxed in HashMap). 4 maps grown together ⇒ ~320 bytes/visited.
    // prevEdge stores 6-element JS arrays, each ~120 bytes → +120 byte.
    // 440 bytes/visited is a reasonable upper bound.
    var visitMB = (__routeDebugPops * 440) / (1024 * 1024);
    if (__routeDebugPops > 0) {
      lines.push('est. visited Maps: ' + visitMB.toFixed(0) + ' MB');
    }
    if (performance && performance.memory) {
      var mb = performance.memory.usedJSHeapSize / (1024 * 1024);
      var lim = performance.memory.jsHeapSizeLimit / (1024 * 1024);
      lines.push('JS heap: ' + mb.toFixed(0) + ' / ' + lim.toFixed(0) + ' MB');
    } else {
      // Best-effort total: cells + visited-state. Underestimates
      // (no MapLibre tile cache, no DOM, no place-search index).
      // Doubling gets closer to a reasonable upper bound on iOS
      // where Safari typically discards a tab around 1500 MB.
      var est = cellMB + visitMB;
      lines.push('est. heap (no Safari): ~' + (est * 2).toFixed(0) +
        ' MB (' + cellMB.toFixed(0) + ' cells + ' +
        visitMB.toFixed(0) + ' visit, ×2 overhead)');
    }
    __routeDebugEl.textContent = lines.join('\n');
  }
  if (DEBUG_ROUTING) {
    // Painter starts as soon as the body parses. The overlay div may
    // not exist yet if this script runs before the body — guard it.
    var __startDebug = function() {
      __routeDebugEl = document.getElementById('route-debug');
      if (!__routeDebugEl) return;
      __routeDebugEl.style.display = 'block';
      __routeDebugLabel = 'idle';
      __debugPaint();
      setInterval(__debugPaint, 2000);
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', __startDebug);
    } else {
      __startDebug();
    }
  }
  function debugInit() {
    if (!DEBUG_ROUTING) return;
    __routeDebugEl = __routeDebugEl || document.getElementById('route-debug');
    if (__routeDebugEl) __routeDebugEl.style.display = 'block';
    __routeDebugStart = performance.now();
    __routeDebugLabel = 'route start';
    __routeDebugPops = 0;
    __debugPaint();
  }
  function debugStats(label, pops) {
    __routeDebugLabel = label;
    __routeDebugPops = pops;
    // The continuous spinner/timer below picks up label+pops on its
    // own — no need to write the status text twice. We only paint
    // the on-screen ?debug=1 overlay here.
    if (DEBUG_ROUTING) __debugPaint();
  }

  // Continuous "still calculating" indicator. Without this, the
  // status panel updated only when the spatial A* paged in enough
  // edges to trigger a new debugStats() call — for 4-5 seconds at
  // the start of a Toronto→Montreal route the user saw "Calculating
  // route..." with no animation and no elapsed-time clock, and
  // assumed it had hung. Now the panel ticks every 100 ms with a
  // braille-spinner frame, the running pops/cells count, the
  // elapsed seconds, and (after 3s) a heuristic ETA derived from
  // the crow-fly distance. The timer is started by
  // computeAndDrawRoute and cleared once the result lands or
  // fails.
  var __routeStatusTimer = null;
  var __routeStatusStart = 0;
  var __routeStatusEtaSec = 0;
  var __ROUTE_SPINNER_FRAMES = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏'];
  // Route progress UI v2: clean stage label + slim animated bar
  // instead of the busy spinner/ETA/pops/cells string. The ETA
  // heuristic was wrong post-worker (a 1100 km route now finishes in
  // ~13 s but the heuristic still announced 55 s), so we drop the
  // number and let the bar carry "still working" semantics.
  // Stage transitions come from the worker's route-progress
  // messages (see _routingWorkerOnMessage). On main-thread fallback
  // we just show the indeterminate bar without label transitions.
  function startRouteProgressIndicator(crowKm) {
    if (__routeStatusTimer) clearInterval(__routeStatusTimer);
    __routeStatusStart = performance.now();
    var s = document.getElementById('routing-status');
    if (s) s.textContent = 'Loading map data…';
    var prog = document.getElementById('routing-progress');
    if (prog) {
      prog.classList.remove('determinate');
      prog.classList.add('visible');
      prog.style.removeProperty('--progress');
    }
    // Stage labels advance based on elapsed time as a simple
    // heuristic — most of the time is cell load + A* pops, in that
    // order. The indeterminate bar carries the "still working"
    // signal so we don't have to lie about percent complete.
    var phase = 0;  // 0=loading, 1=searching, 2=drawing
    __routeStatusTimer = setInterval(function() {
      var elapsed = (performance.now() - __routeStatusStart) / 1000;
      if (!s) return;
      var newPhase = phase;
      if (elapsed > 2.5 && phase < 1) newPhase = 1;
      // We never auto-advance to phase 2 — that transition is driven
      // by the actual route-done message in computeAndDrawRoute,
      // which immediately calls stopRouteProgressIndicator anyway.
      if (newPhase !== phase) {
        phase = newPhase;
        s.textContent = phase === 1
          ? 'Finding fastest route…'
          : 'Loading map data…';
      }
    }, 250);
  }
  function stopRouteProgressIndicator() {
    if (__routeStatusTimer) {
      clearInterval(__routeStatusTimer);
      __routeStatusTimer = null;
    }
    var prog = document.getElementById('routing-progress');
    if (prog) prog.classList.remove('visible');
  }

  // Two-pass routing: full-graph for first/last mile, highway-tier for
  // the long middle. Orchestrates three legs and stitches the result.
  // Falls back to single-pass full A* if either endpoint can't reach
  // a highway-adjacent node within `maxBfs`. See cloud/route_cli.py
  // for the same algorithm in Python (used as the differential test).
  async function findRouteSpatialTwoPass(startNode, endNode) {
    var hwSrc = await findNearestHighwayNode(startNode, 5000);
    var hwDst = await findNearestHighwayNode(endNode, 5000);
    if (hwSrc === null || hwDst === null) {
      return null;  // caller should fall back to full A*
    }

    var legA = (startNode === hwSrc)
      ? null
      : await findRouteSpatialFiltered(startNode, hwSrc, /*highwayOnly=*/false);
    if (legA === null && startNode !== hwSrc) return null;
    // Compact cells between phases. Phase A only needs the cells
    // around the start; Phase B will use mid-country cells. Holding
    // both is what blew Tokyo→Oita on iOS Safari.
    if (graph && typeof graph.compact === 'function') graph.compact(4);

    var legB = (hwSrc === hwDst)
      ? null
      : await findRouteSpatialFiltered(hwSrc, hwDst, /*highwayOnly=*/true);
    if (legB === null && hwSrc !== hwDst) return null;
    if (graph && typeof graph.compact === 'function') graph.compact(4);

    var legC = (hwDst === endNode)
      ? null
      : await findRouteSpatialFiltered(hwDst, endNode, /*highwayOnly=*/false);
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
      // Drop the first coord of continuation legs to avoid duplicate
      // join nodes (the join node ends one leg and starts the next).
      var startIdx = (coords.length > 0) ? 1 : 0;
      for (var k = startIdx; k < leg.coords.length; k++) coords.push(leg.coords[k]);
      for (var r = 0; r < leg.roads.length; r++) roads.push(leg.roads[r]);
      distance += leg.distance;
      time += leg.time;
    }
    return { coords: coords, distance: distance, time: time, roads: roads };
  }

  async function findNearestHighwayNode(seedNode, maxPops) {
    // Outgoing-edge BFS from `seedNode`. Returns the first node whose
    // outgoing-edge set contains a highway-tier edge — i.e. is a
    // highway "entry/exit" node. Bounded so a stranded seed doesn't
    // walk the full graph. Most populated places sit within a few
    // hops of motorway/primary; 5,000 pops covers ~10 km of urban
    // grid in the worst case.
    // Sparse Set instead of Uint8Array(numNodes) — BFS visits at most
    // maxPops nodes, so a Set holds maxPops entries (~few hundred KB)
    // vs 18 MB for the typed-array bitmap on Japan.
    var visited = new Set();
    visited.add(seedNode);
    var queue = [seedNode];
    var head = 0;  // index-based dequeue: shift() is O(n) per pop
    var pops = 0;
    while (head < queue.length && pops < maxPops) {
      var current = queue[head++];
      pops++;
      var edges = await graph.edgesOfNode(current);
      for (var k = 0; k < edges.length; k++) {
        if (isHighwayClass(edges[k][4])) {
          return current;
        }
      }
      for (var k = 0; k < edges.length; k++) {
        if (isNoMotor(edges[k][4])) continue;
        var target = edges[k][0];
        if (!visited.has(target)) {
          visited.add(target);
          queue.push(target);
        }
      }
    }
    return null;
  }

  // A* on the spatial graph with optional highway-tier edge filter.
  // Identical to findRouteSpatial when highwayOnly=false; with the
  // filter, only edges whose class_access ordinal is in 1..6 are
  // expanded — drops ~95% of edges in metro grids and keeps the
  // backbone for cross-country.
  //
  // Sparse-state implementation: g/prev/prevEdge/closed are Maps/Sets
  // that only allocate for visited nodes, not all numNodes. The dense
  // typed-array version blew the iOS heap on Tokyo→Oita (Japan has
  // 18M nodes ⇒ 234 MB just for g+prev+closed before any cell loaded).
  // For routes that visit ~100k–1M nodes, the Maps are 5-50 MB total.
  // Two-stage wrapper: try optimal (admissible heuristic) with a
  // tight pop budget first; on bail, retry with a greedy weight.
  // Easy routes get a guaranteed-optimal answer; hard routes still
  // get a suboptimal-but-found answer instead of crashing the page.
  async function findRouteSpatialFiltered(startNode, endNode, highwayOnly) {
    // Skip the optimal pass for very long highway legs. Toronto→Vancouver
    // (3360 km crow-fly) measured 134 s on highway-only optimal making
    // ~150 pops/sec before bailing at 50 k pops, then phase 2 (greedy)
    // found the route in 12 s. The optimal phase brought no useful
    // partial work — its frontier never reached the dst's hemisphere.
    // Threshold of 1500 km picked empirically: at 920 km (Tokyo→Oita)
    // optimal still bails fast (~1 s) and isn't pure waste; at 3360 km
    // it costs us 2 minutes of wall time. Crow distance scales much
    // faster than the optimal bail rate, so a hard skip is safer than
    // tuning the pop limit.
    var startCoords = await graph.nodeCoordsE7(startNode);
    var endCoords = await graph.nodeCoordsE7(endNode);
    var crowKm = haversine(
      startCoords[0] / 1e7,
      startCoords[1] / 1e7,
      endCoords[0] / 1e7,
      endCoords[1] / 1e7
    ) / 1000;
    // Skip the admissible pass beyond 200 km on the full graph and the
    // weighted pass beyond 800 km (matches the worker — see
    // routing-worker.js OPTIMAL_MAX_CROW_KM for the rationale).
    var skipOptimal = (highwayOnly && crowKm > 1500)
                   || (!highwayOnly && crowKm > 200);
    var skipWeighted = !highwayOnly && crowKm > 800;
    if (!skipOptimal) {
      var optimal = await findRouteSpatialAStar(
        startNode, endNode, highwayOnly,
        /*greedy*/ 1.0,
        /*popLimit*/ highwayOnly ? 50000 : 200000);
      if (optimal) return optimal;
      // Exhausted the open set without bailing: the destination is
      // unreachable and a weighted re-run can't change that (same rule
      // as the worker).
      if (!__mainThreadBailed) return null;
    }
    if (!highwayOnly) {
      // Same chain as the worker (routing-worker.js
      // findRouteSpatialFiltered): closed-pocket check, then weighted
      // A* ×1.25 (what the old 80 km/h heuristic effectively was, and
      // what converges on 200–500 km pairs), then greedy ×1.875. The
      // Map-based state here keeps the old budgets: 200k / 200k / 400k.
      if (await destComponentClosedMain(startNode, endNode)) {
        __mainThreadBailed = false;
        debugStats('destination pocket closed — unreachable', 0);
        return null;
      }
      if (!skipWeighted) {
        var weighted = await findRouteSpatialAStar(
          startNode, endNode, highwayOnly, /*greedy*/ 1.25, /*popLimit*/ 200000);
        if (weighted) return weighted;
        if (!__mainThreadBailed) return null;
      }
    }
    // Both bounded passes bailed (or the optimal pass was skipped) —
    // greedy. Routes here can be 10-20% suboptimal but the page
    // survives and the user gets directions in seconds.
    return findRouteSpatialAStar(
      startNode, endNode, highwayOnly,
      /*greedy*/ highwayOnly ? 2.0 : 1.875,
      /*popLimit*/ highwayOnly ? 100000 : 400000);
  }

  // Set when the most recent main-thread A* pass hit its pop budget
  // (as opposed to exhausting the open set). Mirrors ctx.bailed in
  // the worker so the fallback engine follows the same strategy chain.
  var __mainThreadBailed = false;

  // Mirrors routing-worker.js destComponentClosed exactly: bounded
  // forward BFS from the destination; if it exhausts before 20k nodes
  // without touching the origin, the pocket is "closed" only when no
  // drivable edge enters it from outside (scanning the cells the
  // pocket touches) — a one-way sink or parking loop has a tiny
  // forward component but is enterable and must still route.
  async function destComponentClosedMain(startNode, endNode) {
    var LIMIT = 20000;
    var seen = new Set([endNode]);
    var queue = [endNode], head = 0;
    var cids = new Set();
    while (head < queue.length) {
      if (seen.size >= LIMIT) return false;
      var cur = queue[head++];
      if (cur === startNode) return false;
      var cid = graph._index.cellForNode(cur);
      if (cid >= 0) cids.add(cid);
      var edges = await graph.edgesOfNode(cur);
      for (var i = 0; i < edges.length; i++) {
        var e = edges[i];
        if (isNoMotor(e[4]) || (e[1] >>> 24) === 0) continue;
        if (!seen.has(e[0])) {
          seen.add(e[0]); queue.push(e[0]);
          if (seen.size >= LIMIT) return false;
        }
      }
    }
    if (seen.has(startNode)) return false;
    var cidList = Array.from(cids);
    for (var ci = 0; ci < cidList.length; ci++) {
      var cell = await graph._ensureCell(cidList[ci]);
      var pEdges = cell.edges, pAdj = cell.cellAdj;
      var v2 = !!cell.nodesScaled;
      for (var pl = 0; pl < cell.nodeCount; pl++) {
        var src = v2 ? cell.baseNode + pl : cell.cellNodesGlobal[pl];
        if (seen.has(src)) continue;
        var pEnd = pAdj[pl + 1];
        for (var pe = pAdj[pl]; pe < pEnd; pe++) {
          if (!seen.has(pEdges[pe * 5])) continue;
          if (isNoMotor(pEdges[pe * 5 + 4]) || (pEdges[pe * 5 + 1] >>> 24) === 0) continue;
          return false;  // an entrance from outside the pocket
        }
      }
    }
    return true;
  }

  async function findRouteSpatialAStar(startNode, endNode, highwayOnly,
                                        GREEDY_WEIGHT, POP_LIMIT) {
    __mainThreadBailed = false;
    var endCoords = await graph.nodeCoordsE7(endNode);
    var endLat = endCoords[0] / 1e7;
    var endLon = endCoords[1] / 1e7;

    var g = new Map();         // node -> g-score (seconds)
    var prev = new Map();      // node -> predecessor node
    var prevEdge = new Map();  // node -> [src, tgt, speedDist, geomLocal, name, cls]
    var closed = new Set();
    g.set(startNode, 0);

    var open = new MinHeap();
    var startCoords = await graph.nodeCoordsE7(startNode);
    var h0 = haversine(
      startCoords[0] / 1e7,
      startCoords[1] / 1e7,
      endLat, endLon
    ) / HEURISTIC_MPS;
    open.push([h0, startNode]);
    var pops = 0;
    var weightTag = (GREEDY_WEIGHT > 1.0) ? ' greedy×' + GREEDY_WEIGHT : ' optimal';
    var label = (highwayOnly ? 'A* highway-only' : 'A* full') + weightTag;

    // Time-budget yielding decouples reporting (every 2k pops, so the
    // debug overlay updates predictably) from yielding (every 50 ms
    // wall-clock, so the event loop gets a turn for MapLibre repaints
    // / cancel handling). Replaces the count-based yield that paid
    // setTimeout-clamp overhead on every tick regardless of how slow
    // each pop ran. cf. project_routing_perf_canada.md.
    var lastReportPops = 0;
    var lastYield = performance.now();

    while (open.size() > 0) {
      var item = open.pop();
      var current = item[1];
      pops++;
      if (pops - lastReportPops >= 2000) {
        debugStats(label, pops);
        lastReportPops = pops;
      }
      if (performance.now() - lastYield > 50) {
        await new Promise(function(r) { setTimeout(r, 0); });
        lastYield = performance.now();
      }
      if (pops > POP_LIMIT) {
        debugStats(label + ' BAIL (pop limit ' + POP_LIMIT + ')', pops);
        __mainThreadBailed = true;
        return null;  // caller decides next step
      }
      if (current === endNode) break;
      if (closed.has(current)) continue;
      closed.add(current);
      var nodeEdges = await graph.edgesOfNode(current);
      var curG = g.get(current);
      for (var k = 0; k < nodeEdges.length; k++) {
        var e = nodeEdges[k];
        if (isNoMotor(e[4])) continue;
        if (highwayOnly && !isHighwayClass(e[4])) continue;
        var target = e[0];
        var speedDist = e[1];
        if (closed.has(target)) continue;
        var distM = (speedDist & 0xFFFFFF) / 10;
        var speed = speedDist >>> 24;
        if (speed === 0) continue;
        var cost = distM / (speed / 3.6);
        var newG = curG + cost;
        var oldG = g.has(target) ? g.get(target) : Infinity;
        if (newG < oldG) {
          g.set(target, newG);
          prev.set(target, current);
          prevEdge.set(target, [current, target, speedDist, e[2], e[3], e[4]]);
          var targetCoords = await graph.nodeCoordsE7(target);
          var tLat = targetCoords[0] / 1e7;
          var tLon = targetCoords[1] / 1e7;
          var h = haversine(tLat, tLon, endLat, endLon) / HEURISTIC_MPS * GREEDY_WEIGHT;
          open.push([newG + h, target]);
        }
      }
    }
    debugStats(label + ' done', pops);

    if (!g.has(endNode)) return null;

    // Path reconstruction.
    var path = [];
    var totalDist = 0;
    var totalTime = g.get(endNode);
    var n = endNode;
    var segRev = [];
    while (n !== startNode) {
      var pe = prevEdge.get(n);
      var sourceNode = pe[0];
      var speedDist = pe[2];
      var geomLocal = pe[3];
      var nameIdx = pe[4];
      var classAccess = pe[5];
      var distM = (speedDist & 0xFFFFFF) / 10;
      var isRound = ((classAccess >>> 8) & 1) !== 0;
      var cls = classAccess & 0x1F;
      var isLink = (cls === 2 || cls === 4 || cls === 6 || cls === 8 || cls === 10);
      var manFlags = (isRound ? 1 : 0) | (isLink ? 2 : 0);
      totalDist += distM;
      segRev.push({ nameIdx: nameIdx, distM: distM, flags: manFlags });

      var fromCoords = await graph.nodeCoordsE7(sourceNode);
      var toCoords = await graph.nodeCoordsE7(n);
      var fromLat = fromCoords[0] / 1e7;
      var fromLon = fromCoords[1] / 1e7;
      var toLat = toCoords[0] / 1e7;
      var toLon = toCoords[1] / 1e7;
      var segment = [[fromLon, fromLat]];
      if (geomLocal !== graph.NO_GEOM) {
        var pts = await graph.decodeGeomForEdge(sourceNode, geomLocal);
        if (pts) for (var j = 0; j < pts.length; j++) segment.push(pts[j]);
      }
      segment.push([toLon, toLat]);
      path.push(segment);

      n = prev.get(n);
    }
    // Segments were collected end→start; unshift-ing each one was
    // O(n²) on long routes (tens of thousands of segments).
    path.reverse();

    var roads = [];
    for (var si = segRev.length - 1; si >= 0; si--) {
      var s = segRev[si];
      if (roads.length > 0
          && roads[roads.length - 1].nameIdx === s.nameIdx
          && roads[roads.length - 1].flags === s.flags) {
        roads[roads.length - 1].distM += s.distM;
      } else {
        roads.push({ nameIdx: s.nameIdx, distM: s.distM, flags: s.flags });
      }
    }

    var coords = [];
    for (var sp = 0; sp < path.length; sp++) {
      var startI = (sp === 0) ? 0 : 1;
      for (var kk = startI; kk < path[sp].length; kk++) {
        coords.push(path[sp][kk]);
      }
    }
    return { coords: coords, distance: totalDist, time: totalTime, roads: roads };
  }

