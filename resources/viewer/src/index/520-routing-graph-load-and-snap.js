  // Load routing graph (lazy). Probes for a spatial-chunked layout first
  // (SZCI index) — if present, sets ``graph`` to a SpatialGraph instance
  // and routing paths switch to the async spatial A*. Otherwise falls
  // back to the monolithic/v5-split path.
  //
  // Guarded against re-entrance: applyHash + queueGraphPick (origin) +
  // queueGraphPick (dest) can each call loadGraph in the same tick, and
  // before this guard each call would race-parse the same SZCI buffer
  // and the loser would clobber `graph` after the winner already
  // populated it. The .catch() on the loser then fires the fallback
  // path, which on a chunked-only Canada ZIM mis-parses graph.bin and
  // logs "Failed to load routing graph". Single in-flight promise
  // means the catch only fires once if the underlying load truly
  // fails.
  var loadGraphInflight = null;
  function loadGraph() {
    if (graph) return;
    if (loadGraphInflight) return;
    statusEl.textContent = 'Loading routing data...';
    // Try the spatial index first (tiny file, ~150 MB on Japan but only
    // ~10-100 KB on small regions). If it's not there, fall through to
    // the monolithic loader.
    loadGraphInflight = fetch(baseUrl + 'routing-data/graph-cells-index.bin')
      .then(function(r) {
        // SW returns 200 + X-Streetzim-Absent: 1 for ZIM layouts that
        // skipped the spatial index (v8/v9 monolithic). Chrome flags
        // the response as net::ERR_ABORTED if the body stream isn't
        // drained, so call arrayBuffer() unconditionally and check
        // the header alongside the byteLength.
        var absent = r.headers.get('X-Streetzim-Absent') === '1';
        if (!r.ok) throw new Error('no-spatial');
        return r.arrayBuffer().then(function(buf) {
          return { absent: absent, buf: buf };
        });
      })
      .then(function(o) {
        if (o.absent || !o.buf || o.buf.byteLength === 0) {
          throw new Error('no-spatial');
        }
        var idx = parseRoutingCellsIndex(o.buf);
        // Kick off the worker init NOW, in parallel with the main
        // thread's loadNodeShards, and let it take a copy of the index
        // buffer to transfer (it used to fetch its own: the same
        // 150 MB on Japan through the service worker a second time).
        // Only a reference is kept here: the copy is made at hand-off,
        // so a platform without a worker (Kiwix's WKWebView) never holds
        // two. The main thread keeps `o.buf` — v1 indexes view into it.
        __routingCellsIndexForWorker = o.buf;
        try { initRoutingWorker(); } catch (e) {
          console.warn('[streetzim] routing worker init skipped:', e);
        }
        // SZCI v2 shards nodes_scaled into separate ZIM entries — fetch
        // them in parallel before constructing SpatialGraph. v1 has
        // nodes inline, so loadNodeShards is a synchronous no-op.
        return loadNodeShards(idx).then(function() {
          // Cache by bytes rather than cell count: legacy 1° cells vary
          // wildly in density, while v3 scale-10 cells stay small.
          // Same budget as routing-worker.js. This main-thread fallback is
          // what runs where worker startup fails — Kiwix's iOS/macOS
          // WKWebView — so leaving it at a flat 64 MB meant exactly those
          // clients kept thrashing: ~33 of east-coast-us's cells resident
          // when a 130 km route needs ~62, and the route never terminates.
          var _gb = (navigator && navigator.deviceMemory) || 0;
          var _mb = _gb ? Math.min(384, Math.max(64, _gb * 48)) : 192;
          graph = new SpatialGraph(idx, _mb * 1024 * 1024);
          window.__streetzim_graph = graph;
          console.log('[streetzim] spatial graph loaded', {
            version: idx.version,
            numNodes: idx.numNodes,
            numEdges: idx.numEdges,
            numCells: idx.numCells,
            cellScale: idx.cellScale,
            indexBytes: o.buf.byteLength,
            numNodeShards: idx.numNodeShards,
          });
          // Worker init was kicked off above in parallel with this
          // loadNodeShards — see comment at parseRoutingCellsIndex.
          statusEl.textContent = 'Enter start and destination';
        });
      })
      .catch(function(_spatialErr) {
        // Fall back to monolithic / v5 split / byte-chunked layouts.
        // Returned so the in-flight latch below stays set until this
        // fallback settles too — otherwise a second loadGraph() call
        // during the (multi-second) monolithic fetch started a
        // duplicate download and double-parsed the graph.
        return fetchSzrgBlob(
            'routing-data/graph.bin',
            'routing-data/graph-chunk-manifest.json')
          .then(function(buffer) {
            graph = parseRoutingGraphBinary(buffer);
            window.__streetzim_graph = graph;
            var g0 = graph.hasGeoms() ? graph.decodeGeom(0) : null;
            var gLast = (graph.hasGeoms() && graph.numGeoms > 0)
              ? graph.decodeGeom(graph.numGeoms - 1) : null;
            console.log('[streetzim] graph loaded (monolithic)', {
              version: graph.version,
              bufferBytes: buffer.byteLength,
              geomsInline: graph.hasGeoms(),
              numNodes: graph.numNodes,
              numEdges: graph.numEdges,
              numGeoms: graph.numGeoms,
              geom0Sample: g0 ? g0[0] : null,
              geomLastSample: gLast ? gLast[0] : null,
            });
            statusEl.textContent = 'Enter start and destination';
          })
          .catch(function(err) {
            statusEl.textContent = 'Routing data unavailable';
            console.error('Failed to load routing graph:',
              (err && err.message) || err);
          });
      });
    // Clear the in-flight latch once the chain settles (success or
    // failure) so a future loadGraph() call after a transient error
    // can retry. If `graph` is set, future calls bail at the top.
    loadGraphInflight.finally && loadGraphInflight.finally(function() {
      loadGraphInflight = null;
    });
  }

  // Find nearest graph node to a lat/lon (operates on int32-scaled coords)
  // `mode` is 'origin' or 'dest' — the destination rule accepts one-way
  // sinks (see SpatialGraph.prototype.snapNearestNode).
  async function nearestNode(lat, lon, mode) {
    var latE7 = Math.round(lat * 1e7);
    var lonE7 = Math.round(lon * 1e7);
    if (graph.isSpatial && graph._index.version === 3) {
      var workerReady = await initRoutingWorker();
      if (workerReady && __routingWorker) {
        try { return await snapViaWorker(lat, lon, mode); }
        catch (err) {
          console.warn('[streetzim] worker snap failed, falling back:', err);
        }
      }
      return graph.snapNearestNode(latE7, lonE7, mode);
    }
    var nodes = graph.nodesScaled;
    var best = -1;
    var bestDist = Infinity;
    var cosLatM = Math.max(0.05, Math.cos(lat * Math.PI / 180));
    var adj = graph.adjOffsets;
    for (var i = 0; i < nodes.length; i += 2) {
      // Cast to float before squaring — int32 products can overflow
      var dlat = (nodes[i] - latE7);
      var dlon = (nodes[i + 1] - lonE7) * cosLatM;
      var d = dlat * dlat + dlon * dlon;
      if (d < bestDist) {
        // Skip footpath-only vertices (all out-edges car-prohibited);
        // spatial graphs go through snapNearestNode, this is the
        // monolithic-graph path. Edgeless sinks stay eligible.
        var n = i / 2;
        if (adj) {
          var eS = adj[n], eE = adj[n + 1];
          var carOk = (eS === eE);
          for (var ei = eS; ei < eE; ei++) {
            if (!isNoMotor(graph.edgeClassAccess(ei))) { carOk = true; break; }
          }
          if (!carOk) continue;
        }
        bestDist = d;
        best = n;
      }
    }
    return {
      node: best,
      lat: nodes[best * 2] / 1e7,
      lon: nodes[best * 2 + 1] / 1e7,
    };
  }

  // Haversine distance in meters
  function haversine(lat1, lon1, lat2, lon2) {
    var R = 6371000;
    var dLat = (lat2 - lat1) * Math.PI / 180;
    var dLon = (lon2 - lon1) * Math.PI / 180;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  // A* heuristic speed. MUST be >= the fastest edge speed the builder
  // emits (create_osm_zim.SPEED: motorway = 100 km/h) or the heuristic
  // over-estimates remaining time and the "optimal" pass silently
  // returns suboptimal routes. Matches routing-worker.js and the
  // Python references (streetzim/routing/astar.py, cloud/route_cli.py).
  var HEURISTIC_SPEED_KMH = 100;
  var HEURISTIC_MPS = HEURISTIC_SPEED_KMH / 3.6;

  // Binary min-heap for A* priority queue
  function MinHeap() {
    this.data = [];
  }
  MinHeap.prototype.push = function(item) {
    this.data.push(item);
    var i = this.data.length - 1;
    while (i > 0) {
      var p = (i - 1) >> 1;
      if (this.data[p][0] <= this.data[i][0]) break;
      var tmp = this.data[p];
      this.data[p] = this.data[i];
      this.data[i] = tmp;
      i = p;
    }
  };
  MinHeap.prototype.pop = function() {
    var top = this.data[0];
    var last = this.data.pop();
    if (this.data.length > 0) {
      this.data[0] = last;
      var i = 0;
      while (true) {
        var l = 2 * i + 1, r = 2 * i + 2, smallest = i;
        if (l < this.data.length && this.data[l][0] < this.data[smallest][0]) smallest = l;
        if (r < this.data.length && this.data[r][0] < this.data[smallest][0]) smallest = r;
        if (smallest === i) break;
        var tmp = this.data[i];
        this.data[i] = this.data[smallest];
        this.data[smallest] = tmp;
        i = smallest;
      }
    }
    return top;
  };
  MinHeap.prototype.size = function() { return this.data.length; };

