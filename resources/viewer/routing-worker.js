// routing-worker.js — runs in a Web Worker context.
//
// Owns the spatial-v2 routing graph + cell cache + A* multi-phase
// engine. Frees the main thread from competing with MapLibre repaints
// during the long-route plateau (cf. project_routing_perf_canada.md).
//
// Message protocol (main → worker):
//   {cmd:'init',  baseUrl}                      → {type:'ready'} | {type:'init-error',error}
//   {cmd:'route', id, start, end, options?}     → {type:'route-progress', id, label, pops}*
//                                                 {type:'route-done', id, ok, result?, error?}
//   {cmd:'snap',  id, lat, lon, mode?}          → {type:'snap-done', id, node?, error?}
//   {cmd:'getCoords', id, node}                 → {type:'coords-done', id, lat, lon}
//   {cmd:'compact', keep?}                      → no reply
//   {cmd:'cancel', id}                          → cooperatively stops the matching route
//
// This worker does NOT include the legacy monolithic v1 binary format
// — only spatial v2 (SZCI / SZRC). Continent-scale ZIMs all use v2;
// older ZIMs fall back to the main-thread implementation.

'use strict';

var BASE_URL = '';
var graph = null;
var cancelledRoutes = new Set();

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
    edgeReqs: 0,
  };
}
function _profileEmit(routeOk, totalCoords) {
  if (!_profile) return;
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
  _profile = null;
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
  }
};

function handleInit(msg) {
  BASE_URL = msg.baseUrl || '';
  fetch(BASE_URL + 'routing-data/graph-cells-index.bin')
    .then(function(r) {
      if (!r.ok) throw new Error('cells-index HTTP ' + r.status);
      return r.arrayBuffer();
    })
    .then(function(buf) {
      var idx = parseRoutingCellsIndex(buf);
      return loadNodeShards(idx).then(function() { return idx; });
    })
    .then(function(idx) {
      graph = new SpatialGraph(idx, /*cacheLimit*/ 12);
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
  cancelledRoutes.delete(msg.id);
  _profileReset();
  var ctx = {
    id: msg.id,
    options: msg.options || {},
    cancelled: function() { return cancelledRoutes.has(msg.id); },
  };
  findRoute(msg.start, msg.end, ctx)
    .then(function(result) {
      // Capture the cancellation state BEFORE clearing — the bridge
      // uses this to distinguish "user clicked origin field while
      // routing" (don't fall back, don't update UI) from "no route
      // found" (do fall back to main thread once).
      var wasCancelled = cancelledRoutes.has(msg.id);
      cancelledRoutes.delete(msg.id);
      _profileEmit(!!result, result && result.coords ? result.coords.length : 0);
      self.postMessage({
        type: 'route-done', id: msg.id,
        ok: !!result, cancelled: wasCancelled,
        result: result,
      });
    })
    .catch(function(err) {
      var wasCancelled = cancelledRoutes.has(msg.id);
      cancelledRoutes.delete(msg.id);
      _profileEmit(false, 0);
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
  // Main thread does the actual snap-to-nearest using its own
  // graph copy today; this hook is here for a future refactor.
  self.postMessage({
    type: 'snap-done', id: msg.id, error: 'not implemented',
  });
}

function handleGetCoords(msg) {
  if (!graph || !graph.nodesScaled) {
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
  self.postMessage({
    type: 'coords-done', id: msg.id,
    lat: graph.nodesScaled[n * 2] / 1e7,
    lon: graph.nodesScaled[n * 2 + 1] / 1e7,
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
// Routing engine (ported from main thread; spatial v2 only).
// =============================================================

function haversine(lat1, lon1, lat2, lon2) {
  var R = 6371000;
  var dLat = (lat2 - lat1) * Math.PI / 180;
  var dLon = (lon2 - lon1) * Math.PI / 180;
  var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
          Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
          Math.sin(dLon / 2) * Math.sin(dLon / 2);
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// Highway-tier filter — matches the OSM class_access ordinal scheme.
// 1: motorway, 2: motorway_link, 3: trunk, 4: trunk_link,
// 5: primary,  6: primary_link.
function isHighwayClass(classAccess) {
  var cls = classAccess & 0x1F;
  return cls >= 1 && cls <= 6;
}

function MinHeap() { this.data = []; }
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

function parseRoutingCellsIndex(buffer) {
  var view = new DataView(buffer);
  if (view.getUint32(0, false) !== 0x53_5A_43_49) {
    throw new Error('Invalid cells-index magic');
  }
  var version = view.getUint32(4, true);
  if (version !== 1 && version !== 2) {
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
  } else {
    numNodeShards = view.getUint32(32, true);
    nodesPerShard = view.getUint32(36, true);
    offset = 40;
  }
  var cellLatIdx = new Int32Array(numCells);
  var cellLonIdx = new Int32Array(numCells);
  var cellNodeCount = new Uint32Array(numCells);
  var cellEdgeCount = new Uint32Array(numCells);
  var cellGeomCount = new Uint32Array(numCells);
  var cellKeyToId = new Map();
  for (var i = 0; i < numCells; i++) {
    cellLatIdx[i]    = view.getInt32(offset,      true);
    cellLonIdx[i]    = view.getInt32(offset +  4, true);
    cellNodeCount[i] = view.getUint32(offset +  8, true);
    cellEdgeCount[i] = view.getUint32(offset + 12, true);
    cellGeomCount[i] = view.getUint32(offset + 16, true);
    cellKeyToId.set(cellLatIdx[i] + ',' + cellLonIdx[i], i);
    offset += 20;
  }
  var nameOffsets = new Uint32Array(buffer, offset, numNames + 1);
  offset += (numNames + 1) * 4;
  var namesBlob = new Uint8Array(buffer, offset, namesBytes);
  var textDecoder = new TextDecoder('utf-8');

  function cellOf(latE7, lonE7) {
    var latMult = Math.floor((latE7 * cellScale) / 10_000_000);
    var lonMult = Math.floor((lonE7 * cellScale) / 10_000_000);
    return { lat: latMult, lon: lonMult };
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
    cellNodeCount: cellNodeCount,
    cellEdgeCount: cellEdgeCount,
    cellGeomCount: cellGeomCount,
    getName: getName,
  };
  idx.cellForNode = function(nodeIdx) {
    var ns = idx.nodesScaled;
    if (!ns) return -1;
    var latE7 = ns[nodeIdx * 2];
    var lonE7 = ns[nodeIdx * 2 + 1];
    var k = cellOf(latE7, lonE7);
    var id = cellKeyToId.get(k.lat + ',' + k.lon);
    return id === undefined ? -1 : id;
  };
  // Used by the corridor pre-warm — sample arbitrary lat/lon, find
  // the cell that owns that point, prefetch in parallel before A*.
  idx.cellForCoords = function(latE7, lonE7) {
    var k = cellOf(latE7, lonE7);
    var id = cellKeyToId.get(k.lat + ',' + k.lon);
    return id === undefined ? -1 : id;
  };
  return idx;
}

function loadNodeShards(idx) {
  if (idx.version !== 2) return Promise.resolve();
  var combined = new Int32Array(idx.numNodes * 2);
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
          var shardArr = new Int32Array(buf);
          combined.set(shardArr, shardIdx * idx.nodesPerShard * 2);
        })
      );
    })(i);
  }
  return Promise.all(fetches).then(function() {
    idx.nodesScaled = combined;
  });
}

function parseRoutingCell(buffer) {
  var view = new DataView(buffer);
  if (view.getUint32(0, false) !== 0x53_5A_52_43) {
    throw new Error('Invalid SZRC magic');
  }
  var version = view.getUint32(4, true);
  if (version !== 1) throw new Error('Unsupported SZRC version: ' + version);
  var cellId = view.getUint32(8, true);
  var nodeCount = view.getUint32(12, true);
  var edgeCount = view.getUint32(16, true);
  var geomCount = view.getUint32(20, true);
  var geomBytes = view.getUint32(24, true);

  var off = 28;
  var cellNodesGlobal = new Uint32Array(buffer, off, nodeCount);
  off += nodeCount * 4;
  var cellAdj = new Uint32Array(buffer, off, nodeCount + 1);
  off += (nodeCount + 1) * 4;
  var edges = new Uint32Array(buffer, off, edgeCount * 5);
  off += edgeCount * 5 * 4;
  var geomOffsets = new Uint32Array(buffer, off, geomCount + 1);
  off += (geomCount + 1) * 4;
  var geomBlob = new Uint8Array(buffer, off, geomBytes);
  var geomBlobByteStart = off;

  function localIdxFor(globalIdx) {
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
    nodeCount: nodeCount,
    edgeCount: edgeCount,
    geomCount: geomCount,
    cellNodesGlobal: cellNodesGlobal,
    cellAdj: cellAdj,
    edges: edges,
    localIdxFor: localIdxFor,
    decodeGeomLocal: decodeGeomLocal,
  };
}

function SpatialGraph(index, cacheLimit) {
  this._index = index;
  this._cells = new Map();
  this._inFlight = new Map();
  this._lru = [];
  this._cacheLimit = cacheLimit || 12;
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
  keep = (keep === undefined) ? Math.min(8, this._cacheLimit) : keep;
  while (this._lru.length > keep) {
    var cid = this._lru.shift();
    this._cells.delete(cid);
  }
};
SpatialGraph.prototype._ensureCell = function(cid) {
  var self = this;
  if (self._cells.has(cid)) {
    if (_profile) _profile.cellHits++;
    var idx = self._lru.indexOf(cid);
    if (idx >= 0) self._lru.splice(idx, 1);
    self._lru.push(cid);
    return Promise.resolve(self._cells.get(cid));
  }
  if (self._inFlight.has(cid)) {
    // Same cell already in flight — count as a near-miss; the actual
    // wait time gets attributed via the original fetch's timing.
    if (_profile) _profile.cellHits++;
    return self._inFlight.get(cid);
  }
  if (_profile) _profile.cellMisses++;
  var fetchT0 = nowMs();
  var bodyT0 = 0;
  var p = fetch(BASE_URL + self._cellPath(cid))
    .then(function(r) {
      if (!r.ok) throw new Error('cell HTTP ' + r.status + ' for ' + cid);
      // HTTP-response time: from fetch() to receiving headers. The
      // body still has to stream in via arrayBuffer(); time that
      // separately so we can tell SW round-trip from blob-slice cost.
      if (_profile) _profile.cellHttpMs += nowMs() - fetchT0;
      bodyT0 = nowMs();
      return r.arrayBuffer();
    })
    .then(function(buf) {
      if (_profile) _profile.cellBodyMs += nowMs() - bodyT0;
      if (_profile) _profile.cellFetchMs += nowMs() - fetchT0;
      if (_profile) _profile.cellBytes += buf.byteLength;
      var parseT0 = nowMs();
      var cell = parseRoutingCell(buf);
      if (_profile) _profile.cellParseMs += nowMs() - parseT0;
      self._cells.set(cid, cell);
      self._lru.push(cid);
      self._inFlight.delete(cid);
      if (self._cells.size > self._cacheLimit) {
        var evict = self._lru.shift();
        self._cells.delete(evict);
      }
      return cell;
    });
  self._inFlight.set(cid, p);
  return p;
};
SpatialGraph.prototype.edgesOfNode = function(globalNodeIdx) {
  var cid = this._index.cellForNode(globalNodeIdx);
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
  var cid = this._index.cellForNode(sourceNodeIdx);
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
  var nodesScaled = graph.nodesScaled;
  var crowKm = haversine(
    nodesScaled[startNode * 2] / 1e7,
    nodesScaled[startNode * 2 + 1] / 1e7,
    nodesScaled[endNode * 2] / 1e7,
    nodesScaled[endNode * 2 + 1] / 1e7
  ) / 1000;
  // Skip optimal pass for very long highway legs (Toronto→Vancouver
  // pattern). cf. project_routing_perf_canada.md.
  // Skip the optimal pass on long routes:
  //   - highway-only > 1500 km (Toronto→Vancouver pattern; established
  //     threshold from project_routing_perf_canada.md)
  //   - full-graph    > 800 km (SD→Arcata pattern; SD→Arcata at
  //     1095 km consistently bails the 200 k-pop full-optimal at ~12 s
  //     and falls back to greedy anyway, so the optimal pass produces
  //     zero value. SF→LA at 559 km succeeds in optimal — calibrate
  //     between if we hit a region where 800 km is too tight.)
  var skipOptimal = (highwayOnly && crowKm > 1500)
                 || (!highwayOnly && crowKm > 800);
  if (!skipOptimal) {
    var optimal = await findRouteSpatialAStar(
      startNode, endNode, highwayOnly,
      /*greedy*/ 1.0,
      /*popLimit*/ highwayOnly ? 50000 : 200000,
      ctx);
    if (optimal) return optimal;
    if (ctx && ctx.cancelled && ctx.cancelled()) return null;
  }
  return findRouteSpatialAStar(
    startNode, endNode, highwayOnly,
    /*greedy*/ highwayOnly ? 2.0 : 1.5,
    /*popLimit*/ highwayOnly ? 100000 : 400000,
    ctx);
}

async function findRouteSpatialAStar(startNode, endNode, highwayOnly,
                                      GREEDY_WEIGHT, POP_LIMIT, ctx) {
  var nodesScaled = graph.nodesScaled;
  var endLat = nodesScaled[endNode * 2] / 1e7;
  var endLon = nodesScaled[endNode * 2 + 1] / 1e7;

  var g = new Map();
  var prev = new Map();
  var prevEdge = new Map();
  var closed = new Set();
  g.set(startNode, 0);

  var open = new MinHeap();
  var h0 = haversine(
    nodesScaled[startNode * 2] / 1e7,
    nodesScaled[startNode * 2 + 1] / 1e7,
    endLat, endLon
  ) / (80 / 3.6);
  open.push([h0, startNode]);
  var pops = 0;
  var weightTag = (GREEDY_WEIGHT > 1.0) ? ' greedy×' + GREEDY_WEIGHT : ' optimal';
  var label = (highwayOnly ? 'A* highway-only' : 'A* full') + weightTag;
  var phaseT0 = nowMs();
  if (_profile) _profile.edgeReqs++; // we'll add per-pop below

  // #1: time-budget yielding. Decoupled from progress reporting:
  // - report every 2k pops (cheap; main thread can re-render the
  //   progress indicator at most that often)
  // - yield to the worker event loop every 50 ms of wall time so
  //   cancel/compact messages from the main thread can be processed
  //   without burning hundreds of ms before each yield
  var lastReportPops = 0;
  var lastYield = nowMs();

  while (open.size() > 0) {
    var item = open.pop();
    var current = item[1];
    pops++;
    if (pops - lastReportPops >= 2000) {
      debugStats(ctx, label, pops);
      lastReportPops = pops;
    }
    if (nowMs() - lastYield > 50) {
      var yT0 = nowMs();
      await new Promise(function(r) { setTimeout(r, 0); });
      if (_profile) {
        _profile.yields++;
        _profile.yieldMs += nowMs() - yT0;
      }
      lastYield = nowMs();
      if (ctx && ctx.cancelled && ctx.cancelled()) {
        if (_profile) _profile.phases.push({
          label: label, pops: pops, ms: nowMs() - phaseT0,
          bailed: true, cancelled: true,
        });
        return null;
      }
    }
    if (pops > POP_LIMIT) {
      debugStats(ctx, label + ' BAIL (pop limit ' + POP_LIMIT + ')', pops);
      if (_profile) _profile.phases.push({
        label: label, pops: pops, ms: nowMs() - phaseT0,
        bailed: true,
      });
      return null;
    }
    if (current === endNode) break;
    if (closed.has(current)) continue;
    closed.add(current);
    if (_profile) _profile.edgeReqs++;
    var nodeEdges = await graph.edgesOfNode(current);
    var curG = g.get(current);
    for (var k = 0; k < nodeEdges.length; k++) {
      var e = nodeEdges[k];
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
        var tLat = nodesScaled[target * 2] / 1e7;
        var tLon = nodesScaled[target * 2 + 1] / 1e7;
        var h = haversine(tLat, tLon, endLat, endLon) / (80 / 3.6) * GREEDY_WEIGHT;
        open.push([newG + h, target]);
      }
    }
  }
  debugStats(ctx, label + ' done', pops);
  if (_profile) _profile.phases.push({
    label: label, pops: pops, ms: nowMs() - phaseT0,
    bailed: false,
  });

  if (!g.has(endNode)) return null;

  // Path reconstruction (same shape as main-thread implementation).
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

    var fromLat = nodesScaled[sourceNode * 2] / 1e7;
    var fromLon = nodesScaled[sourceNode * 2 + 1] / 1e7;
    var toLat = nodesScaled[n * 2] / 1e7;
    var toLon = nodesScaled[n * 2 + 1] / 1e7;
    var segment = [[fromLon, fromLat]];
    if (geomLocal !== graph.NO_GEOM) {
      var pts = await graph.decodeGeomForEdge(sourceNode, geomLocal);
      if (pts) for (var j = 0; j < pts.length; j++) segment.push(pts[j]);
    }
    segment.push([toLon, toLat]);
    path.unshift(segment);

    n = prev.get(n);
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
  for (var sp = 0; sp < path.length; sp++) {
    var startI = (sp === 0) ? 0 : 1;
    for (var kk = startI; kk < path[sp].length; kk++) {
      coords.push(path[sp][kk]);
    }
  }
  return { coords: coords, distance: totalDist, time: totalTime, roads: roads };
}

async function findNearestHighwayNode(seedNode, maxPops) {
  var visited = new Set();
  visited.add(seedNode);
  var queue = [seedNode];
  var pops = 0;
  while (queue.length > 0 && pops < maxPops) {
    var current = queue.shift();
    pops++;
    var edges = await graph.edgesOfNode(current);
    for (var k = 0; k < edges.length; k++) {
      if (isHighwayClass(edges[k][4])) {
        return current;
      }
    }
    for (var k2 = 0; k2 < edges.length; k2++) {
      var target = edges[k2][0];
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
    for (var r = 0; r < leg.roads.length; r++) roads.push(leg.roads[r]);
    distance += leg.distance;
    time += leg.time;
  }
  return { coords: coords, distance: distance, time: time, roads: roads };
}

async function findRoute(startNode, endNode, ctx) {
  if (!graph.isSpatial) {
    throw new Error('worker only handles spatial-v2 graphs');
  }
  var nodesScaled = graph.nodesScaled;
  var startLat = nodesScaled[startNode * 2] / 1e7;
  var startLon = nodesScaled[startNode * 2 + 1] / 1e7;
  var endLat = nodesScaled[endNode * 2] / 1e7;
  var endLon = nodesScaled[endNode * 2 + 1] / 1e7;
  var crow = haversine(startLat, startLon, endLat, endLon);
  if (_profile) _profile.crowKm = crow / 1000;
  var override = ctx && ctx.options && ctx.options.route;
  var useTwoPass = override === 'two-pass'
    || (override !== 'full' && crow > 100000);

  if (useTwoPass) {
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

  var routeResult = await findRouteSpatial(startNode, endNode, ctx);
  if (!routeResult && useTwoPass) {
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
    var lon = startLon + (endLon - startLon) * t;
    var latE7 = Math.round(lat * 1e7);
    var lonE7 = Math.round(lon * 1e7);
    var cid = graph._index.cellForCoords(latE7, lonE7);
    if (cid < 0 || seen.has(cid)) continue;
    seen.add(cid);
    if (graph._cells.has(cid)) continue;
    // Kick off the fetch — A* awaits via _inFlight when it gets there.
    graph._ensureCell(cid).catch(function() { /* ignore, A* will retry */ });
  }
  if (_profile) {
    _profile.prewarmCells = seen.size;
    _profile.prewarmMs = nowMs() - prewarmT0;
  }
}
