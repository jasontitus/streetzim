  // --- Off-main-thread routing bridge ---
  // The worker owns the spatial graph cell cache, so its A* runs
  // flat-out without competing with
  // MapLibre repaints. Falls back to the main-thread engine below if
  // the worker fails to come up (e.g. host CSP, ZIM-baked viewer
  // pre-dating routing-worker.js, etc.). cf. project_routing_perf_canada.md.
  var __routingWorker = null;
  var __routingCellsIndexForWorker = null;  // ArrayBuffer copy handed to the worker on init
  var __routingWorkerReady = null;       // Promise<boolean> | null
  var __routingWorkerSeq = 0;
  var __routingWorkerInflight = new Map();

  // Settle every pending route/snap promise when the worker dies.
  // Without this a crash mid-route left findRoute awaiting forever
  // and the panel stuck on "Finding fastest route…" with no fallback.
  function _failRoutingWorkerInflight(err) {
    var pending = Array.from(__routingWorkerInflight.values());
    __routingWorkerInflight.clear();
    pending.forEach(function(p) {
      try { p.reject(err); } catch (e) {}
    });
  }

  function initRoutingWorker() {
    if (__routingWorkerReady) return __routingWorkerReady;
    if (typeof Worker !== 'function') {
      __routingCellsIndexForWorker = null;
      __routingWorkerReady = Promise.resolve(false);
      return __routingWorkerReady;
    }
    // Spawn from a blob: URL rather than a same-origin URL. Same-origin
    // worker construction was producing opaque error events with no
    // message/filename/lineno (likely a CSP/CORP/COEP edge case on
    // Firebase Hosting). Fetching the worker source and wrapping it
    // in a Blob sidesteps all of that — blob: URLs are same-origin to
    // the page that created them and aren't subject to host CSP rules.
    //
    // Returns a Promise<bool> — true once the worker has loaded its
    // graph data, false on any failure. findRoute awaits this so a
    // route requested before the worker is ready waits up briefly
    // rather than silently falling back to the main thread.
    var workerSrcUrl = baseUrl + 'routing-worker.js';
    __routingWorkerReady = fetch(workerSrcUrl)
      .then(function(r) {
        if (!r.ok) throw new Error('routing-worker.js HTTP ' + r.status);
        return r.text();
      })
      .then(function(src) {
        var blob = new Blob([src], { type: 'application/javascript' });
        var blobUrl = URL.createObjectURL(blob);
        __routingWorker = new Worker(blobUrl);
        return _installRoutingWorker();
      })
      .catch(function(err) {
        console.warn('[streetzim] routing worker init failed:', err);
        __routingCellsIndexForWorker = null;
        if (__routingWorker) {
          try { __routingWorker.terminate(); } catch (e) {}
          __routingWorker = null;
        }
        return false;
      });
    return __routingWorkerReady;
  }

  function _installRoutingWorker() {
    if (!__routingWorker) return Promise.resolve(false);
    return new Promise(function(resolve) {
      __routingWorker.addEventListener('message', function onMsg(e) {
        var m = e.data || {};
        if (m.type === 'ready') {
          // console.warn so the smoke harness and devtools both
          // surface the moment the worker comes up — important for
          // distinguishing "worker is doing the routing" from
          // "silently fell back to main thread".
          console.warn('[streetzim] routing worker ready', JSON.stringify(m));
          window.__streetzim_routingWorker = __routingWorker;
          window.__streetzim_routingWorkerReady = true;
          __routingWorker.removeEventListener('message', onMsg);
          __routingWorker.addEventListener('message', _routingWorkerOnMessage);
          // Replay any GPS that fired before the worker was ready —
          // the geolocate handler tries to prewarm but a no-op'd if
          // the bridge wasn't installed yet.
          var loc = window.__streetzimLastLoc;
          if (loc && typeof loc.lat === 'number'
              && typeof window.__streetzim_prewarmRoutingCells === 'function') {
            window.__streetzim_prewarmRoutingCells([{ lat: loc.lat, lon: loc.lon }]);
          }
          resolve(true);
        } else if (m.type === 'init-error') {
          console.warn('[streetzim] routing worker init failed:', m.error);
          __routingWorker.terminate();
          __routingWorker = null;
          resolve(false);
        }
      });
      __routingWorker.addEventListener('error', function(err) {
        console.warn('[streetzim] routing worker error:'
          + ' msg=' + (err.message || '<none>')
          + ' file=' + (err.filename || '<none>')
          + ' line=' + (err.lineno || '?')
          + ' col=' + (err.colno || '?')
          + ' type=' + (err.type || '?')
        );
        if (__routingWorker) {
          try { __routingWorker.terminate(); } catch (e) {}
          __routingWorker = null;
        }
        // Reject in-flight requests so their callers fall back to the
        // main-thread engine, and pin the ready state to "failed" so
        // later routes don't await a promise that already resolved
        // true for a worker that no longer exists.
        _failRoutingWorkerInflight(new Error('routing worker crashed: '
          + (err.message || err.type || 'unknown')));
        __routingWorkerReady = Promise.resolve(false);
        window.__streetzim_routingWorkerReady = false;
        resolve(false);
      });
      var workerBaseUrl;
      try {
        workerBaseUrl = new URL(baseUrl, location.href).href;
      } catch (e) {
        workerBaseUrl = baseUrl;
      }
      // Hand the worker the cells index the main thread already fetched
      // (a copy, transferred) instead of letting it fetch its own —
      // 150 MB on Japan through the service worker, twice, at startup.
      var idxBuf = null;
      try { if (__routingCellsIndexForWorker) idxBuf = __routingCellsIndexForWorker.slice(0); } catch (e) {}
      __routingCellsIndexForWorker = null;
      if (idxBuf) {
        __routingWorker.postMessage({ cmd: 'init', baseUrl: workerBaseUrl, cellsIndex: idxBuf }, [idxBuf]);
      } else {
        __routingWorker.postMessage({ cmd: 'init', baseUrl: workerBaseUrl });
      }
    });
  }

  function _routingWorkerOnMessage(e) {
    var m = e.data || {};
    if (m.type === 'route-profile') {
      // Format the worker's profile summary into a single console.warn
      // line so smoke harness + devtools both surface it consistently.
      var mb = (m.cellBytes || 0) / 1024 / 1024;
      var line = '[route-profile] ' + (m.ok ? 'OK' : 'FAIL')
        + ' total=' + m.totalMs.toFixed(0) + 'ms'
        + ' crow=' + (m.crowKm || 0).toFixed(0) + 'km'
        + ' coords=' + (m.coords || 0)
        + ' cells=' + m.cellHits + 'h/' + m.cellMisses + 'm'
        + ' (' + mb.toFixed(1) + 'MB,'
        + ' http=' + (m.cellHttpMs || 0).toFixed(0) + 'ms'
        + ' body=' + (m.cellBodyMs || 0).toFixed(0) + 'ms'
        + ' parse=' + (m.cellParseMs || 0).toFixed(0) + 'ms)'
        + ' prewarm=' + (m.prewarmCells || 0)
        + '/' + (m.prewarmMs || 0).toFixed(0) + 'ms'
        + ' edgeReqs=' + m.edgeReqs
        + ' yields=' + m.yields + '/' + (m.yieldMs || 0).toFixed(0) + 'ms';
      for (var pi = 0; pi < (m.phases || []).length; pi++) {
        var p = m.phases[pi];
        var pps = (p.pops > 0 && p.ms > 0)
          ? Math.round(p.pops / (p.ms / 1000)) : 0;
        line += '\n  phase[' + pi + '] ' + p.label
              + ' pops=' + p.pops
              + ' ms=' + p.ms.toFixed(0)
              + (pps > 0 ? ' (' + pps + '/s)' : '')
              + ' bail=' + (p.bailed ? 'yes' : 'no');
      }
      console.warn(line);
      return;
    }
    var inflight = __routingWorkerInflight.get(m.id);
    if (!inflight) return;
    if (m.type === 'route-progress') {
      __routeDebugLabel = m.label || __routeDebugLabel;
      __routeDebugPops = m.pops || 0;
      if (DEBUG_ROUTING) __debugPaint();
    } else if (m.type === 'route-done') {
      __routingWorkerInflight.delete(m.id);
      if (m.cancelled) {
        // Resolve with a sentinel so findRoute can return null
        // without triggering the main-thread fallback (the user
        // explicitly asked us to stop).
        inflight.resolve({ _cancelled: true });
      } else if (m.ok && m.result) inflight.resolve(m.result);
      else if (m.ok)               inflight.resolve(null);
      else                         inflight.reject(new Error(m.error || 'route failed'));
    } else if (m.type === 'snap-done') {
      __routingWorkerInflight.delete(m.id);
      if (m.error) inflight.reject(new Error(m.error));
      else inflight.resolve({ node: m.node, lat: m.lat, lon: m.lon });
    }
  }

  // Cancel any worker routes still in flight. Hooked into origin /
  // destination input focus so a route auto-fired by "Directions to
  // here" doesn't keep grinding while the user is changing one of
  // the endpoints. The worker checks ctx.cancelled() at every yield
  // and exits the inner loop early; the bridge then resolves the
  // route promise with a {_cancelled:true} sentinel that findRoute
  // recognises and turns into a no-op (no UI update, no fallback).
  function cancelInFlightRoute() {
    if (!__routingWorker || __routingWorkerInflight.size === 0) return;
    __routingWorkerInflight.forEach(function(_v, id) {
      try { __routingWorker.postMessage({ cmd: 'cancel', id: id }); }
      catch (e) {}
    });
  }
  // Keep on window for any host (drive mode, popup helpers) that
  // wants to short-circuit a stale route without restarting search.
  window.__streetzim_cancelInFlightRoute = cancelInFlightRoute;

  // Pre-warm worker-side cells around a list of lat/lon points. Used
  // at page-load (cell at user's GPS) so the FIRST route doesn't pay
  // the ~1.7 s cold-fetch penalty on its starting cell. Safe to call
  // multiple times; the worker's _ensureCell de-dupes via _inFlight.
  function prewarmRoutingCells(coords) {
    if (!__routingWorker) return;
    if (!coords || !coords.length) return;
    try {
      __routingWorker.postMessage({ cmd: 'prewarmCells', coords: coords });
    } catch (e) {}
  }
  window.__streetzim_prewarmRoutingCells = prewarmRoutingCells;

  function findRouteViaWorker(startNode, endNode, travel) {
    if (!__routingWorker) return Promise.reject(new Error('worker not ready'));
    // Only the newest route matters: an origin/dest change while a
    // route is still computing used to leave the old search grinding
    // in the worker alongside the new one (both slowed down, both
    // competed for the cell budget). computeAndDrawRoute already
    // ignores the stale result via routeSeq, so cancelling is safe.
    cancelInFlightRoute();
    // ?route=full / ?route=two-pass test override — the main-thread
    // engine honoured it but the worker was always sent options:{}.
    var override = null;
    try { override = new URLSearchParams(location.search).get('route'); }
    catch (e) {}
    var id = ++__routingWorkerSeq;
    return new Promise(function(resolve, reject) {
      __routingWorkerInflight.set(id, { resolve: resolve, reject: reject });
      __routingWorker.postMessage({
        cmd: 'route', id: id,
        start: startNode, end: endNode,
        options: override ? { route: override, travel: travel } : { travel: travel },
      });
    });
  }

  function snapViaWorker(lat, lon, mode, travel) {
    if (!__routingWorker) return Promise.reject(new Error('worker not ready'));
    var id = ++__routingWorkerSeq;
    return new Promise(function(resolve, reject) {
      __routingWorkerInflight.set(id, { resolve: resolve, reject: reject });
      __routingWorker.postMessage({ cmd: 'snap', id: id, lat: lat, lon: lon,
                                    mode: mode === 'dest' ? 'dest' : 'origin',
                                    travel: travel || 'drive' });
    });
  }

  async function findRoute(startNode, endNode, travel) {
    travel = travel || 'drive';
    // Prefer the worker on spatial graphs. initRoutingWorker is
    // idempotent and returns a Promise<bool> resolving once init
    // completes (true → ready, false → failed, fall back). Awaiting
    // here means a route requested before init finishes waits up
    // briefly rather than silently bypassing the worker.
    if (graph && graph.isSpatial) {
      var ready = await initRoutingWorker();
      if (ready && __routingWorker) {
        try {
          var t0 = performance.now();
          console.warn('[streetzim] routing via worker (start=' + startNode
                       + ' end=' + endNode + ')');
          var workerResult = await findRouteViaWorker(startNode, endNode, travel);
          // Cancellation: caller asked us to stop (likely via the
          // origin/dest focus hook). Don't fall back to main thread —
          // they're about to fire a new route. Return null so
          // computeAndDrawRoute treats it as "no route" silently.
          if (workerResult && workerResult._cancelled) {
            console.warn('[streetzim] worker route cancelled');
            // Hand the sentinel up: computeAndDrawRoute must neither
            // draw nor say "No route found" — the user cancelled by
            // focusing an endpoint field, and whatever route was
            // drawn before should stay until a new one lands.
            return { _cancelled: true };
          }
          console.warn('[streetzim] worker route done in '
                       + (performance.now() - t0).toFixed(0) + ' ms');
          return workerResult;
        } catch (err) {
          console.warn('[streetzim] worker route failed, falling back:', err);
        }
      } else {
        console.warn('[streetzim] routing on main thread (worker not ready)');
      }
    }
    // The main-thread engine is the old-ZIM fallback and routes cars
    // only: never show its car route as a walking or cycling one.
    if (travel !== 'drive') {
      throw new Error(travel + ' routing needs the routing worker');
    }
    return findRouteMainThread(startNode, endNode);
  }

  async function findRouteMainThread(startNode, endNode) {
    // Spatial graphs route via the async-cell-loading path below; the
    // inline monolithic A* beneath only works with a single typed array
    // of edges (graph.edges is undefined on spatial graphs).
    if (graph.isSpatial) {
      // Long-distance routing wins big from the highway-tier two-pass:
      // Tehran→Baghdad (~700 km) was 4-5 min single-pass full A* in
      // Kiwix Desktop; two-pass measured ~10× fewer pops in the Python
      // prototype (cloud/route_cli.py). For short routes the full A*
      // is already fast and the two-pass overhead (BFS + leg stitching)
      // would actually slow it down, so gate by crow-fly distance.
      // Threshold of 100 km picked empirically: at 50 km the two-pass
      // wins barely; at 100 km it dominates; at 200 km it's huge.
      // Override via URL ?route=full or ?route=two-pass for testing.
      debugInit();
      var startCoords = await graph.nodeCoordsE7(startNode);
      var endCoords = await graph.nodeCoordsE7(endNode);
      var startLat = startCoords[0] / 1e7;
      var startLon = startCoords[1] / 1e7;
      var endLat = endCoords[0] / 1e7;
      var endLon = endCoords[1] / 1e7;
      var crow = haversine(startLat, startLon, endLat, endLon);
      var override = new URLSearchParams(location.search).get('route');
      var useTwoPass = override === 'two-pass'
        || (override !== 'full' && crow > 100000);
      // Long-route pre-cleanup. Drops all cached cells + yields to
      // the event loop so the browser has a chance to GC before we
      // start allocating again. Without this, an immediately-prior
      // route (Kyoto→Oita) leaves the heap close to the limit, and
      // the next long route (Tokyo→Oita) spills over and iOS Safari
      // discards the page mid-A*. Threshold matches the two-pass
      // gate above so it's only invoked when actually needed.
      if (useTwoPass && graph && typeof graph.compact === 'function') {
        graph.compact(0);
        // Three short awaits give the browser real GC opportunities;
        // a single 0ms timeout often isn't enough on Safari.
        for (var i = 0; i < 3; i++) {
          await new Promise(function(r) { setTimeout(r, 50); });
        }
        if (DEBUG_ROUTING) {
          __routeDebugLabel = 'pre-route cleanup done (cells dropped)';
          __debugPaint();
        }
      }
      // Strategy chain: try the cheapest most-accurate option first,
      // fall back only on failure.
      //   1. Full A*  — most accurate; findRouteSpatialFiltered is
      //      already two-stage internally (admissible → greedy).
      //   2. Two-pass — only when full bails on a really long route.
      // The 100-km threshold for forcing two-pass that we had earlier
      // made cross-country routes WORSE in both time and accuracy
      // when full A* would have finished. Always try full first.
      var routeResult = null;
      var t1 = performance.now();
      __mainThreadBailed = false;
      if (override !== 'two-pass') {
        routeResult = await findRouteSpatial(startNode, endNode);
      }
      if (routeResult) {
        console.log('[route] full crow=' + (crow/1000).toFixed(0) +
                    'km took ' + (performance.now() - t1).toFixed(0) + 'ms');
      } else if (override === 'two-pass' || __mainThreadBailed) {
        // Two-pass only when the full search ran out of budget (any
        // distance) — same gate as the worker; an exhausted open set
        // or a closed destination pocket means genuinely unreachable
        // and is not retried.
        console.log('[route] full bailed, trying two-pass...');
        var t0 = performance.now();
        routeResult = await findRouteSpatialTwoPass(startNode, endNode);
        if (routeResult) {
          console.log('[route] two-pass crow=' + (crow/1000).toFixed(0) +
                      'km took ' + (performance.now() - t0).toFixed(0) + 'ms');
        }
      }
      // Free up cell cache after EVERY route. iOS Safari discards
      // the page when heap stays near the limit even during idle
      // browsing (clicking an input, panning the map). Dropping
      // cells back to ~8 retained gives the rest of the page room.
      // Cells re-fetch cheaply if the user routes again.
      if (graph && typeof graph.compact === 'function') graph.compact(8);
      return routeResult;
    }
    var nodesScaled = graph.nodesScaled;
    var adjOffsets = graph.adjOffsets;
    var numNodes = graph.numNodes;

    var endLat = nodesScaled[endNode * 2] / 1e7;
    var endLon = nodesScaled[endNode * 2 + 1] / 1e7;

    // g[node] = best known cost to reach node (in seconds)
    var g = new Float64Array(numNodes);
    g.fill(Infinity);
    g[startNode] = 0;

    // Track predecessors for path reconstruction
    var prev = new Int32Array(numNodes);
    prev.fill(-1);
    // prevEdge[n] = absolute edge index in `edges` that leads to n
    var prevEdge = new Int32Array(numNodes);
    prevEdge.fill(-1);

    var closed = new Uint8Array(numNodes);

    var open = new MinHeap();
    var h0 = haversine(
      nodesScaled[startNode * 2] / 1e7,
      nodesScaled[startNode * 2 + 1] / 1e7,
      endLat, endLon
    ) / HEURISTIC_MPS;
    open.push([h0, startNode]);

    while (open.size() > 0) {
      var item = open.pop();
      var current = item[1];

      if (current === endNode) break;
      if (closed[current]) continue;
      closed[current] = 1;

      var eStart = adjOffsets[current];
      var eEnd = adjOffsets[current + 1];

      for (var i = eStart; i < eEnd; i++) {
        if (isNoMotor(graph.edgeClassAccess(i))) continue;
        var target = graph.edgeTarget(i);
        var distM = graph.edgeDistMeters(i);
        var speed = graph.edgeSpeed(i);

        if (closed[target]) continue;

        var cost = distM / (speed / 3.6);           // time in seconds
        var newG = g[current] + cost;

        if (newG < g[target]) {
          g[target] = newG;
          prev[target] = current;
          prevEdge[target] = i;
          var tLat = nodesScaled[target * 2] / 1e7;
          var tLon = nodesScaled[target * 2 + 1] / 1e7;
          var h = haversine(tLat, tLon, endLat, endLon) / HEURISTIC_MPS;
          open.push([newG + h, target]);
        }
      }
    }

    if (g[endNode] === Infinity) return null;

    // Reconstruct path. On v5 split ZIMs geomBlob isn't resident yet —
    // block the first route-draw on the SZGM fetch so decodeGeom() can
    // produce real polylines instead of straight-line fallbacks.
    if (graph.version === 5 && !graph.hasGeoms()) {
      await loadGraphGeoms();
    }
    var path = [];
    var totalDist = 0;
    var totalTime = g[endNode];
    var n = endNode;

    // Accumulate segments in reverse (endNode→startNode); flipped later.
    var segRev = [];  // [{nameIdx, distM}, ...]
    while (n !== startNode) {
      var p = prev[n];
      var ei = prevEdge[n];
      var distM = graph.edgeDistMeters(ei);
      var geomIdx = graph.edgeGeomIdx(ei);
      var nameIdx = graph.edgeNameIdx(ei);
      var isRound = graph.edgeIsRoundabout(ei);
      var isLink  = graph.edgeIsLink(ei);
      var manFlags = (isRound ? 1 : 0) | (isLink ? 2 : 0);
      totalDist += distM;
      segRev.push({ nameIdx: nameIdx, distM: distM, flags: manFlags });

      var fromLat = nodesScaled[p * 2] / 1e7;
      var fromLon = nodesScaled[p * 2 + 1] / 1e7;
      var toLat = nodesScaled[n * 2] / 1e7;
      var toLon = nodesScaled[n * 2 + 1] / 1e7;
      var segment = [[fromLon, fromLat]];
      if (geomIdx !== graph.NO_GEOM) {
        var pts = graph.decodeGeom(geomIdx);
        if (pts) {
          for (var j = 0; j < pts.length; j++) segment.push(pts[j]);
        }
      }
      segment.push([toLon, toLat]);
      path.push(segment);

      n = p;
    }
    path.reverse();  // collected end→start

    // Coalesce consecutive same-name segments into named roads. We also
    // coalesce by maneuver flags so that e.g. a multi-segment unnamed
    // roundabout collapses into one "roundabout" road entry (was merging
    // by name alone, which dropped the maneuver cue on unnamed pieces).
    var roads = [];
    for (var si = segRev.length - 1; si >= 0; si--) {
      var s = segRev[si];
      if (roads.length > 0 &&
          roads[roads.length - 1].nameIdx === s.nameIdx &&
          roads[roads.length - 1].flags === s.flags) {
        roads[roads.length - 1].distM += s.distM;
      } else {
        roads.push({ nameIdx: s.nameIdx, distM: s.distM, flags: s.flags });
      }
    }

    // Flatten path segments into single coordinate array
    var coords = [];
    for (var s = 0; s < path.length; s++) {
      var start = (s === 0) ? 0 : 1;  // skip duplicate junction points
      for (var k = start; k < path[s].length; k++) {
        coords.push(path[s][k]);
      }
    }

    return {
      coords: coords,
      distance: totalDist,
      time: totalTime,
      roads: roads  // [{nameIdx, distM}, ...] in travel order, consecutive same-name coalesced
    };
  }

  // A route across the antimeridian steps from 179.99 to -179.99; drawn
  // as given, that step is a line round the whole globe. Continue the
  // longitude past +-180 instead, which MapLibre draws where it belongs.
  function unwrapLngs(coords) {
    var out = [];
    var prev = null;
    for (var i = 0; i < coords.length; i++) {
      var lng = coords[i][0];
      if (prev !== null) lng -= 360 * Math.round((lng - prev) / 360);
      out.push([lng, coords[i][1]]);
      prev = lng;
    }
    return out;
  }

  function drawRoute(coords) {
    if (routeDrawn) {
      map.getSource('route').setData({
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: coords }
      });
    } else {
      map.addSource('route', {
        type: 'geojson',
        data: {
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: coords }
        }
      });
      map.addLayer({
        id: 'route-outline',
        type: 'line',
        source: 'route',
        paint: {
          'line-color': '#1e40af',
          'line-width': 8,
          'line-opacity': 0.4
        }
      });
      map.addLayer({
        id: 'route-line',
        type: 'line',
        source: 'route',
        paint: {
          'line-color': '#2563eb',
          'line-width': 4,
          'line-opacity': 0.9
        }
      });
      routeDrawn = true;
    }
  }

  function formatDistance(meters) {
    return szFormatDistance(meters, szUnit());
  }

  function formatTime(seconds) {
    if (seconds < 60) return Math.round(seconds) + ' sec';
    var mins = Math.round(seconds / 60);
    if (mins < 60) return mins + ' min';
    var hrs = Math.floor(mins / 60);
    var rem = mins % 60;
    return hrs + ' hr ' + rem + ' min';
  }

  function renderRoads(roads) {
    if (!roadsEl) return;
    while (roadsEl.firstChild) roadsEl.removeChild(roadsEl.firstChild);
    if (!roads || !roads.length) return;
    for (var i = 0; i < roads.length; i++) {
      var r = roads[i];
      var name = graph.getName ? graph.getName(r.nameIdx) : '';
      var row = document.createElement('div');
      row.className = 'road-row';
      var nameSpan = document.createElement('span');
      nameSpan.className = 'road-name' + (name ? '' : ' unnamed');
      nameSpan.textContent = name || 'unnamed road';
      var distSpan = document.createElement('span');
      distSpan.className = 'road-dist';
      distSpan.textContent = formatDistance(r.distM);
      row.appendChild(nameSpan);
      row.appendChild(distSpan);
      roadsEl.appendChild(row);
    }
  }

  function coordLabel(lat, lon) {
    return lat.toFixed(4) + ', ' + lon.toFixed(4);
  }

  function makeMarkerEl(cls) {
    var el = document.createElement('div');
    el.className = 'routing-marker';
    var inner = document.createElement('div');
    inner.className = cls;
    el.appendChild(inner);
    return el;
  }

