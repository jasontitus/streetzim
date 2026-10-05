  // Binary graph format (SZRG v2 / v3 / v4):
  //   Header (32 B): "SZRG" magic, u32 version, u32 numNodes, u32 numEdges,
  //                  u32 numGeoms, u32 geomBytes, u32 numNames, u32 namesBytes
  //   Nodes:         Int32Array [lat_e7, lon_e7, ...] length numNodes*2
  //   Adj offsets:   Uint32Array length numNodes+1
  //   Edges (v2):    Uint32Array [target, dist_dm, speed_geom24, name_idx, ...]
  //                  speed_geom24 = (speed << 24) | geomIdx24  (0xFFFFFF = no geom)
  //                  CAVEAT: geomIdx limited to 24 bits — breaks at >16.78M geoms.
  //   Edges (v3):    Uint32Array [target, dist_speed, geom_idx, name_idx, ...]
  //                  dist_speed = (speed << 24) | dist_dm24
  //                  geom_idx is full u32; 0xFFFFFFFF = no geom
  //   Geom offsets:  Uint32Array length numGeoms+1 (byte offsets into geomBlob)
  //   Geom blob:     Uint8Array length geomBytes — per geom: i32 lon0, i32 lat0
  //                  then zigzag-varint (dlon, dlat) pairs until next geom's offset.
  //   Name offsets:  Uint32Array length numNames+1 (byte offsets into namesBlob)
  //   Names blob:    Uint8Array length namesBytes (concatenated UTF-8)
  function parseRoutingGraphBinary(buffer) {
    var view = new DataView(buffer);
    // Magic "SZRG" as big-endian bytes = 0x535A5247
    if (view.getUint32(0, false) !== 0x535A5247) {
      throw new Error('Invalid routing graph magic');
    }
    var version = view.getUint32(4, true);
    if (version !== 2 && version !== 3 && version !== 4 && version !== 5) {
      throw new Error('Unsupported graph version: ' + version);
    }
    // v4/v5 insert a trailing class_access u32 per edge (road class +
    // access flags). v5 additionally hoists the geom_offsets + geom_blob
    // out of this buffer into a separate routing-data/graph-geoms.bin
    // companion file so the PWA can avoid allocating a multi-GB single
    // buffer on iOS Safari startup.
    var edgeStride = (version === 4 || version === 5) ? 5 : 4;
    var numNodes = view.getUint32(8, true);
    var numEdges = view.getUint32(12, true);
    var numGeoms = view.getUint32(16, true);
    var geomBytes = view.getUint32(20, true);
    var numNames = view.getUint32(24, true);
    var namesBytes = view.getUint32(28, true);

    var offset = 32;
    var nodesScaled = new Int32Array(buffer, offset, numNodes * 2);
    offset += numNodes * 2 * 4;
    var adjOffsets = new Uint32Array(buffer, offset, numNodes + 1);
    offset += (numNodes + 1) * 4;
    var edges = new Uint32Array(buffer, offset, numEdges * edgeStride);
    offset += numEdges * edgeStride * 4;

    var geomOffsets = null;
    var geomBlob = null;
    var geomView = null;              // DataView over geomBlob for absolute int32 reads
    var geomBlobByteStart = 0;
    var hasGeoms = false;
    if (version !== 5) {
      geomOffsets = new Uint32Array(buffer, offset, numGeoms + 1);
      offset += (numGeoms + 1) * 4;
      geomBlob = new Uint8Array(buffer, offset, geomBytes);
      geomBlobByteStart = offset;
      geomView = view;
      hasGeoms = true;
      offset += geomBytes;
    }

    var nameOffsets = new Uint32Array(buffer, offset, numNames + 1);
    offset += (numNames + 1) * 4;
    var namesBlob = new Uint8Array(buffer, offset, namesBytes);

    var textDecoder = new TextDecoder('utf-8');

    // Decode geom index `gi` to a [[lon, lat], ...] array (on-demand).
    // First 8 bytes are absolute int32 (lon_e7, lat_e7); remaining bytes
    // are zigzag-varint (dlon, dlat) pairs until the next geom's offset.
    // Returns null when the graph is v5 and the SZGM companion hasn't
    // been attached yet — callers must ensure loadGraphGeoms() resolved.
    function decodeGeom(gi) {
      if (!hasGeoms) return null;
      var start = geomOffsets[gi];
      var end = geomOffsets[gi + 1];
      if (end <= start + 8) {
        var lon0 = geomView.getInt32(geomBlobByteStart + start, true);
        var lat0 = geomView.getInt32(geomBlobByteStart + start + 4, true);
        return [[lon0 / 1e7, lat0 / 1e7]];
      }
      var lon = geomView.getInt32(geomBlobByteStart + start, true);
      var lat = geomView.getInt32(geomBlobByteStart + start + 4, true);
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

    // attachGeoms(szgmBuffer) — wire up the SZGM companion buffer so
    // decodeGeom() can return real polylines. Validates magic + version
    // + numGeoms agreement with the main buffer.
    function attachGeoms(szgmBuffer) {
      var gv = new DataView(szgmBuffer);
      if (gv.getUint32(0, false) !== 0x535A474D /* "SZGM" */) {
        throw new Error('Invalid graph-geoms magic');
      }
      var ver = gv.getUint32(4, true);
      if (ver !== 1) throw new Error('Unsupported SZGM version: ' + ver);
      var gNumGeoms = gv.getUint32(8, true);
      var gBytes = gv.getUint32(12, true);
      if (gNumGeoms !== numGeoms) {
        throw new Error('SZGM numGeoms (' + gNumGeoms + ') != SZRG numGeoms (' + numGeoms + ')');
      }
      var goff = 16;
      geomOffsets = new Uint32Array(szgmBuffer, goff, numGeoms + 1);
      goff += (numGeoms + 1) * 4;
      geomBlob = new Uint8Array(szgmBuffer, goff, gBytes);
      geomBlobByteStart = goff;
      geomView = gv;
      hasGeoms = true;
    }

    // Version-aware edge field accessors — v2 packs (dist, speed+geom24),
    // v3 packs (dist+speed, geomFull). Keep the rest of the routing code
    // free of version checks by funnelling through these three helpers.
    var edgeDistMeters, edgeSpeed, edgeGeomIdx;
    var NO_GEOM;
    if (version === 2) {
      NO_GEOM = 0xFFFFFF;
      edgeDistMeters = function(i) { return edges[i * edgeStride + 1] / 10; };
      edgeSpeed = function(i) { return edges[i * edgeStride + 2] >>> 24; };
      edgeGeomIdx = function(i) { return edges[i * edgeStride + 2] & 0xFFFFFF; };
    } else { // v3 / v4 / v5 share the packed speed+dist / full geom_idx layout
      NO_GEOM = 0xFFFFFFFF;
      edgeDistMeters = function(i) { return (edges[i * edgeStride + 1] & 0xFFFFFF) / 10; };
      edgeSpeed = function(i) { return edges[i * edgeStride + 1] >>> 24; };
      edgeGeomIdx = function(i) { return edges[i * edgeStride + 2]; };
    }

    var hasClassAccess = (version === 4 || version === 5);
    return {
      version: version,
      numNodes: numNodes,
      numEdges: numEdges,
      numGeoms: numGeoms,
      numNames: numNames,
      nodesScaled: nodesScaled,
      adjOffsets: adjOffsets,
      edges: edges,
      NO_GEOM: NO_GEOM,
      hasGeoms: function() { return hasGeoms; },
      attachGeoms: attachGeoms,
      edgeTarget: function(i) { return edges[i * edgeStride]; },
      edgeDistMeters: edgeDistMeters,
      edgeSpeed: edgeSpeed,
      edgeGeomIdx: edgeGeomIdx,
      edgeNameIdx: function(i) { return edges[i * edgeStride + 3]; },
      // v4/v5 class_access — bit layout in docs/driving-mode-road-class-warnings.md:
      //   bits 0..4 : road-class ordinal
      //   bit 5..7  : no-foot / no-bicycle / oneway
      //   bit 8     : junction=roundabout
      // v2/v3 ZIMs pre-dated this field, so we return 0 and the HUD
      // falls back to geometric turn arrows — no roundabout/ramp cues.
      edgeClassAccess: hasClassAccess
        ? function(i) { return edges[i * edgeStride + 4]; }
        : function() { return 0; },
      edgeIsRoundabout: hasClassAccess
        ? function(i) { return ((edges[i * edgeStride + 4] >>> 8) & 1) !== 0; }
        : function() { return false; },
      // Link / ramp ordinals: motorway_link=2, trunk_link=4, primary_link=6,
      // secondary_link=8, tertiary_link=10.
      edgeIsLink: hasClassAccess
        ? function(i) {
            var cls = edges[i * edgeStride + 4] & 0x1F;
            return cls === 2 || cls === 4 || cls === 6 || cls === 8 || cls === 10;
          }
        : function() { return false; },
      decodeGeom: decodeGeom,
      getName: function(nameIdx) {
        if (nameIdx <= 0 || nameIdx >= numNames) return '';
        var start = nameOffsets[nameIdx];
        var end = nameOffsets[nameIdx + 1];
        if (end === start) return '';
        return textDecoder.decode(namesBlob.subarray(start, end));
      },
    };
  }

  // Fetch either a single-file blob or a chunked one, depending on which
  // layout the ZIM uses. primaryPath is the monolithic file name;
  // manifestPath points at a graph-chunk-manifest.json. On 404 of either
  // side we fall through; both missing → reject.
  //
  // Each chunk is an independent fetch so libzim streams them in parallel
  // and the browser never needs to hold a >500 MB single cluster in memory.
  function fetchSzrgBlob(primaryPath, manifestPath) {
    // Try the manifest first. If it's present, prefer chunked — a ZIM
    // that has both layouts stored is probably in flight between formats,
    // and the manifest is authoritative.
    return fetch(baseUrl + manifestPath)
      .then(function(r) {
        // SW returns 200 + X-Streetzim-Absent for absent optional
        // probes (e.g. graph-chunk-manifest only exists in v10+
        // chunked builds). Drain the body before throwing —
        // Chrome flags an undrained response as net::ERR_ABORTED.
        var absent = r.headers.get('X-Streetzim-Absent') === '1';
        if (!r.ok) throw new Error('manifest HTTP ' + r.status);
        if (absent) {
          return r.text().then(function() {
            throw new Error('manifest HTTP absent');
          });
        }
        return r.json();
      })
      .then(function(manifest) {
        // manifest.chunks paths are relative to the manifest's directory.
        var dir = manifestPath.indexOf('/') >= 0
          ? manifestPath.slice(0, manifestPath.lastIndexOf('/') + 1)
          : '';
        var total = manifest.total_bytes;
        var parts = new Array(manifest.chunks.length);
        return Promise.all(manifest.chunks.map(function(ch, i) {
          return fetch(baseUrl + dir + ch.path)
            .then(function(r) {
              if (!r.ok) throw new Error('chunk HTTP ' + r.status);
              return r.arrayBuffer();
            })
            .then(function(buf) {
              if (buf.byteLength !== ch.bytes) {
                throw new Error('chunk ' + ch.path + ' size '
                                + buf.byteLength + ' != manifest ' + ch.bytes);
              }
              parts[i] = new Uint8Array(buf);
            });
        })).then(function() {
          var out = new Uint8Array(total);
          var off = 0;
          for (var i = 0; i < parts.length; i++) {
            out.set(parts[i], off);
            off += parts[i].byteLength;
          }
          if (off !== total) {
            throw new Error('chunked blob total ' + off
                            + ' != manifest ' + total);
          }
          return out.buffer;
        });
      })
      .catch(function(err) {
        // Manifest missing → fall back to the single-file layout. This
        // also handles every pre-chunking ZIM.
        if (/manifest HTTP/.test(String(err && err.message))) {
          return fetch(baseUrl + primaryPath)
            .then(function(r) {
              if (!r.ok) throw new Error('HTTP ' + r.status);
              var absent = r.headers.get('X-Streetzim-Absent') === '1';
              return r.arrayBuffer().then(function(buf) {
                if (absent || !buf || buf.byteLength === 0) {
                  throw new Error(absent ? 'HTTP absent' : 'empty');
                }
                return buf;
              });
            });
        }
        throw err;
      });
  }

  // Kick off graph-geoms.bin fetch the first time the viewer needs to
  // draw a route. Returns a promise that resolves once graph.hasGeoms()
  // is true. Idempotent: subsequent callers get the same in-flight
  // promise, then a fulfilled one once the SZGM companion is attached.
  var _geomsPromise = null;
  function loadGraphGeoms() {
    if (!graph) return Promise.reject(new Error('graph not loaded'));
    if (graph.version !== 5 || graph.hasGeoms()) return Promise.resolve();
    if (_geomsPromise) return _geomsPromise;
    _geomsPromise = fetchSzrgBlob(
        'routing-data/graph-geoms.bin',
        'routing-data/graph-geoms-chunk-manifest.json')
      .then(function(buf) {
        graph.attachGeoms(buf);
        console.log('[streetzim] routing geoms attached', {
          bytes: buf.byteLength,
          numGeoms: graph.numGeoms,
        });
      });
    return _geomsPromise;
  }

  // Parse the SZCI (spatial cells index) buffer. Eager-loaded at
  // startup — contains the name table and compact per-cell metadata.
  // Each actual cell's coords/edges/geoms live in a separate ZIM
  // entry fetched on demand.
  //
  // Three layouts:
  //   v1 — nodes_blob is INLINE in this buffer right after the header.
  //        Header is 32 bytes (4 magic + 7 × u32). Used for small ZIMs
  //        where total nodes_scaled fits comfortably in one entry.
  //   v2 — nodes_blob is SHARDED into routing-data/nodes-scaled-NNN.bin
  //        files. Header is 40 bytes (4 magic + 9 × u32, the extra two
  //        being num_node_shards and nodes_per_shard). The continent-
  //        scale path: ~10 M+ nodes ⇒ 80 MB+ of node data ⇒ would blow
  //        the 200 MB per-entry cap if inline. Caller must run
  //        loadNodeShards(idx, baseUrl) before constructing
  //        SpatialGraph; nodesScaled is null until then.
  //   v3 — coordinates live in each SZRC v2 cell. The index stores one
  //        contiguous global-node range per cell, so startup is index-only.
  function parseRoutingCellsIndex(buffer) {
    var view = new DataView(buffer);
    if (view.getUint32(0, false) !== 0x53_5A_43_49 /* "SZCI" */) {
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
    var cellScale = view.getInt32(28, true);  // signed: 1 ⇒ 1°, 10 ⇒ 0.1°

    var offset, numNodeShards = 0, nodesPerShard = 0, nodesScaled = null;
    if (version === 1) {
      offset = 32;
      nodesScaled = new Int32Array(buffer, offset, numNodes * 2);
      offset += numNodes * 2 * 4;
    } else if (version === 2) {
      // v2: two extra header u32s, then NO inline nodes — fetched from
      // routing-data/nodes-scaled-NNN.bin shards by loadNodeShards.
      numNodeShards = view.getUint32(32, true);
      nodesPerShard = view.getUint32(36, true);
      offset = 40;
    } else {
      offset = 32;
    }
    // Cell metadata: num_cells × 20 bytes each
    var cellLatIdx = new Int32Array(numCells);
    var cellLonIdx = new Int32Array(numCells);
    var cellBaseNode = new Uint32Array(numCells);
    var cellNodeCount = new Uint32Array(numCells);
    var cellEdgeCount = new Uint32Array(numCells);
    var cellGeomCount = new Uint32Array(numCells);
    var cellKeyToId = new Map();  // "lat,lon" → cell_id
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
      cellKeyToId.set(cellLatIdx[i] + ',' + cellLonIdx[i], i);
    }
    var nameOffsets = new Uint32Array(buffer, offset, numNames + 1);
    offset += (numNames + 1) * 4;
    var namesBlob = new Uint8Array(buffer, offset, namesBytes);

    var textDecoder = new TextDecoder('utf-8');

    // cell_of(lat_e7, lon_e7, scale) — must match streetzim/routing/spatial.cell_of
    // EXACTLY (floor semantics on negatives, which JS's Math.floor does
    // correctly unlike the // operator).
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
      cellBaseNode: cellBaseNode,
      cellNodeCount: cellNodeCount,
      cellEdgeCount: cellEdgeCount,
      cellGeomCount: cellGeomCount,
      getName: getName,
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
      var ns = idx.nodesScaled;
      if (!ns) return -1;
      var latE7 = ns[nodeIdx * 2];
      var lonE7 = ns[nodeIdx * 2 + 1];
      var k = cellOf(latE7, lonE7);
      var id = cellKeyToId.get(k.lat + ',' + k.lon);
      return id === undefined ? -1 : id;
    };
    idx.cellForCoords = function(latE7, lonE7) {
      var k = cellOf(latE7, lonE7);
      var id = cellKeyToId.get(k.lat + ',' + k.lon);
      return id === undefined ? -1 : id;
    };
    return idx;
  }

  // Fetch all SZCI v2 nodes-scaled-NNN.bin shards in parallel and
  // assemble idx.nodesScaled. No-op for v1 (already inline). Resolves
  // when every shard has been read into the contiguous Int32Array.
  function loadNodeShards(idx) {
    if (idx.version !== 2) return Promise.resolve();
    var combined = new Int32Array(idx.numNodes * 2);
    var pad3 = function(n) {
      return n < 10 ? '00' + n : n < 100 ? '0' + n : '' + n;
    };
    var fetches = [];
    for (var i = 0; i < idx.numNodeShards; i++) {
      (function(shardIdx) {
        var path = baseUrl + 'routing-data/nodes-scaled-' + pad3(shardIdx) + '.bin';
        fetches.push(
          fetch(path).then(function(r) {
            if (!r.ok || r.headers.get('X-Streetzim-Absent') === '1') {
              throw new Error('node shard ' + shardIdx + ' missing');
            }
            return r.arrayBuffer();
          }).then(function(buf) {
            // Each shard holds up to nodes_per_shard nodes × 8 B.
            // Source order: shardIdx * nodes_per_shard * 2 i32 elements
            // into the combined array.
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

  // Parse one SZRC cell buffer — owns the edges + geoms for a subset of
  // graph nodes. Returns a record with binary-searchable node list,
  // local adjacency, edges (stride 5 u32), and geom offset/blob views.
  function parseRoutingCell(index, cid, buffer) {
    var view = new DataView(buffer);
    if (view.getUint32(0, false) !== 0x53_5A_52_43 /* "SZRC" */) {
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

    // Binary search for a global node idx → local position in this cell.
    // Returns -1 if not present (shouldn't happen when the index is
    // consistent — cells are authoritative for their nodes).
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

    // Decode a cell-local geom index → [[lon, lat], ...]. Identical
    // zigzag-varint layout to SZRG/SZGM polylines.
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
    };
  }

  // Spatial graph façade: holds SZCI index + an LRU-capped map of parsed
  // cell buffers. ``edgesOfNode`` + ``decodeGeomForEdge`` are the only
  // entry points the routing A* needs. Cells are fetched via
  // ``routing-data/graph-cell-NNNNN.bin`` (5-digit zero-pad) — must
  // match the writer in cloud/repackage_zim.py.
  function SpatialGraph(index, maxResidentBytes) {
    this._index = index;
    this._cells = new Map();     // cell_id → parsed cell
    this._inFlight = new Map();  // cell_id → Promise<parsed cell>
    this._lru = [];
    this._residentBytes = 0;
    this._maxResidentBytes = maxResidentBytes || 64 * 1024 * 1024;
    // Expose SZRG-like surface so call sites don't need a version switch.
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

  // Aggressively drop cached cells when the page needs the memory
  // back. Called after each route completes — the cells stick around
  // in case the user routes again, but if iOS Safari is under
  // memory pressure (typeahead, MapLibre re-render, etc.) we'd
  // rather give up the cache than have the page killed.
  SpatialGraph.prototype.compact = function(keep) {
    keep = (keep === undefined) ? 4 : keep;
    while (this._lru.length > keep) {
      var cid = this._lru.shift();
      var cell = this._cells.get(cid);
      if (cell) this._residentBytes -= cell.byteLength;
      this._cells.delete(cid);
    }
  };

  SpatialGraph.prototype._ensureCell = function(cid) {
    var self = this;
    if (self._cells.has(cid)) {
      // LRU touch.
      var idx = self._lru.indexOf(cid);
      if (idx >= 0) self._lru.splice(idx, 1);
      self._lru.push(cid);
      return Promise.resolve(self._cells.get(cid));
    }
    if (self._inFlight.has(cid)) return self._inFlight.get(cid);
    var p = fetch(baseUrl + self._cellPath(cid))
      .then(function(r) {
        if (!r.ok) throw new Error('cell HTTP ' + r.status + ' for ' + cid);
        return r.arrayBuffer();
      })
      .then(function(buf) {
        var cell = parseRoutingCell(self._index, cid, buf);
        self._cells.set(cid, cell);
        self._lru.push(cid);
        self._residentBytes += cell.byteLength;
        self._inFlight.delete(cid);
        while (self._residentBytes > self._maxResidentBytes
               && self._lru.length > 1) {
          var evict = self._lru.shift();
          var evicted = self._cells.get(evict);
          if (evicted) self._residentBytes -= evicted.byteLength;
          self._cells.delete(evict);
        }
        return cell;
      });
    self._inFlight.set(cid, p);
    return p;
  };

  SpatialGraph.prototype.nodeCoordsE7 = function(globalNodeIdx) {
    var self = this;
    var cid = self._index.cellForNode(globalNodeIdx);
    if (cid < 0) return Promise.reject(new Error('node out of range: ' + globalNodeIdx));
    if (self._index.version !== 3) {
      return Promise.resolve([
        self._index.nodesScaled[globalNodeIdx * 2],
        self._index.nodesScaled[globalNodeIdx * 2 + 1],
      ]);
    }
    return self._ensureCell(cid).then(function(cell) {
      var local = globalNodeIdx - cell.baseNode;
      return [cell.nodesScaled[local * 2], cell.nodesScaled[local * 2 + 1]];
    });
  };

  // Shortlist snap (mirrors routing-worker.js SNAP_*): the nearest vertex
  // may be a footpath vertex or a tiny disconnected fragment (pier,
  // private drive) the car A* can never leave. Keep the nearest few
  // car-ok vertices and return the first whose forward component
  // reaches SNAP_MIN_REACH nodes, provided it is at most
  // SNAP_MAX_EXTRA_M further away than the nearest.
  var SNAP_CANDIDATES = 6, SNAP_MIN_REACH = 32, SNAP_MAX_EXTRA_M = 1000;
  // `mode` 'origin' (default) | 'dest': for a destination an edgeless
  // vertex with a drivable incoming edge in its own cell (end of a
  // one-way spur) is accepted too — the forward reach test would
  // otherwise displace it up to SNAP_MAX_EXTRA_M. Mirrors the worker.
  SpatialGraph.prototype.snapNearestNode = async function(latE7, lonE7, mode) {
    var forDest = (mode === 'dest');
    var scale = this._index.cellScale;
    // Longitude degrees shrink with latitude — compare in a locally
    // metric space or the snap picks the wrong node at high latitudes.
    var cosLat = Math.max(0.05, Math.cos(latE7 / 1e7 * Math.PI / 180));
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
    var best = [], worstKept = Infinity;
    for (var i = 0; i < candidates.length && candidates[i][0] <= worstKept; i++) {
      var cell = await this._ensureCell(candidates[i][1]);
      for (var local = 0; local < cell.nodeCount; local++) {
        var nlat = cell.nodesScaled[local * 2];
        var nlon = cell.nodesScaled[local * 2 + 1];
        var ndlat = nlat - latE7;
        var ndlon = (nlon - lonE7) * cosLat;
        var dist = ndlat * ndlat + ndlon * ndlon;
        if (dist >= worstKept) continue;
        // Skip a node whose outgoing edges are all car-prohibited
        // (footpath vertex); edgeless sinks stay eligible.
        // A speed-0 out-edge (walk/bike against a one-way) counts as
        // absent: a one-way's end stays a sink.
        var eS = cell.cellAdj[local], eE = cell.cellAdj[local + 1];
        var real = 0, carOk = false;
        for (var ei = eS; ei < eE; ei++) {
          if ((cell.edges[ei * 5 + 1] >>> 24) === 0) continue;
          real++;
          if (!isNoMotor(cell.edges[ei * 5 + 4])) { carOk = true; break; }
        }
        if (real === 0) carOk = true;
        if (!carOk) continue;
        var k = best.length;
        while (k > 0 && best[k - 1].dist > dist) k--;
        best.splice(k, 0, { dist: dist, node: cell.baseNode + local, lat: nlat, lon: nlon,
                            edgeless: (real === 0) });
        if (best.length > SNAP_CANDIDATES) best.pop();
        if (best.length === SNAP_CANDIDATES) worstKept = best[best.length - 1].dist;
      }
    }
    if (best.length === 0) throw new Error('no routing nodes');
    var limitR = Math.sqrt(best[0].dist) + SNAP_MAX_EXTRA_M * 90;  // ~90 e7/m
    var pick = best[0];
    for (var c = 0; c < best.length; c++) {
      if (Math.sqrt(best[c].dist) > limitR) break;
      if (await this._reachesAtLeast(best[c].node, SNAP_MIN_REACH)) { pick = best[c]; break; }
      if (forDest && best[c].edgeless
          && await this._hasDrivableIncomingInOwnCell(best[c].node)) { pick = best[c]; break; }
    }
    return { node: pick.node, lat: pick.lat / 1e7, lon: pick.lon / 1e7 };
  };

  // True when an edge in `node`'s own cell targets it and is drivable
  // (mirrors the worker; own cell only so the result is deterministic).
  SpatialGraph.prototype._hasDrivableIncomingInOwnCell = async function(node) {
    var cid = this._index.cellForNode(node);
    if (cid < 0) return false;
    var cell = await this._ensureCell(cid);
    var edges = cell.edges;
    for (var ei = 0, n = cell.edgeCount; ei < n; ei++) {
      if (edges[ei * 5] !== node) continue;
      if (isNoMotor(edges[ei * 5 + 4]) || (edges[ei * 5 + 1] >>> 24) === 0) continue;
      return true;
    }
    return false;
  };

  // Bounded forward BFS over drivable edges: true once `limit` distinct
  // nodes are reachable from `node`.
  SpatialGraph.prototype._reachesAtLeast = async function(node, limit) {
    var seen = new Set([node]);
    var queue = [node], head = 0;
    while (head < queue.length) {
      if (seen.size >= limit) return true;
      var edges = await this.edgesOfNode(queue[head++]);
      for (var i = 0; i < edges.length; i++) {
        var e = edges[i];
        if (isNoMotor(e[4]) || (e[1] >>> 24) === 0) continue;
        if (!seen.has(e[0])) { seen.add(e[0]); queue.push(e[0]); if (seen.size >= limit) return true; }
      }
    }
    return seen.size >= limit;
  };

  // Returns a promise resolving to an array of edges for the given
  // global node: [[target, speedDist, geomLocal, nameIdx, classAccess], ...]
  // Empty array if the node has no outbound edges.
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

