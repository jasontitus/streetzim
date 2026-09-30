// BEGIN chip-shards
// Geographic Find-chip shards (cloud/chip_shards.py). Chips over ~2 MiB
// ship as spatially compact shard files; the category manifest lists
// each shard's bbox (`chips[id].layout === 'geo'`, `shards[i] =
// [s, w, n, e, count, bytes]`, w > e crossing ±180). CHIP_SHARDS.load
// fetches only the shards needed for the k nearest matching records
// around a point, optionally inside a viewport — never the whole chip,
// which on continents is hundreds of MB of JSON (iOS WebView jetsam).
// This block is byte-identical in index.html and places.html
// (tests/chip_shards_js.test.mjs enforces it); edit both together.
var CHIP_SHARDS = (function() {
  var EARTH_KM = 6371.0088;
  var D2R = Math.PI / 180;

  function hav(rad) { var s = Math.sin(rad / 2); return s * s; }
  function havKm(h) { return 2 * EARTH_KM * Math.asin(Math.sqrt(Math.min(1, Math.max(0, h)))); }
  function distKm(lat1, lon1, lat2, lon2) {
    return havKm(hav((lat2 - lat1) * D2R)
      + Math.cos(lat1 * D2R) * Math.cos(lat2 * D2R) * hav((lon2 - lon1) * D2R));
  }
  function mod360(x) { return ((x % 360) + 360) % 360; }
  function norm180(x) { return mod360(x + 180) - 180; }
  function lonIn(lon, w, e) {
    return w <= e ? (lon >= w && lon <= e) : (lon >= w || lon <= e);
  }
  // Degrees from `lon` to the circular interval [w, e]; 0 inside.
  function lonGap(lon, w, e) {
    // 180 and -180 are one meridian; norm180 maps both to -180.
    lon = norm180(lon); w = norm180(w); e = norm180(e);
    if (lonIn(lon, w, e)) return 0;
    return Math.min(mod360(w - lon), mod360(lon - e));
  }
  // Great-circle lower bound (km) from a point to anything inside a box:
  // hav(d) = hav(dLat) + cos(lat1)cos(lat2)hav(dLon), each term at its
  // minimum over the box, so the bound never exceeds a true distance.
  function boxLowerBoundKm(lat, lon, s, w, n, e) {
    var dLat = lat < s ? s - lat : (lat > n ? lat - n : 0);
    var dLon = Math.min(lonGap(lon, w, e), 180);
    var cosMin = Math.max(0, Math.min(Math.cos(s * D2R), Math.cos(n * D2R)));
    return havKm(hav(dLat * D2R)
      + Math.max(0, Math.cos(lat * D2R)) * cosMin * hav(dLon * D2R));
  }
  // Viewport {s, w, n, e} as MapLibre reports it (longitudes may be
  // unwrapped past ±180, or span 360+ at low zoom) → normalized form.
  function normBbox(b) {
    if (!b) return null;
    if (!(b.e - b.w < 360)) return { s: b.s, n: b.n, all: true, w: -180, e: 180 };
    return { s: b.s, n: b.n, all: false, w: norm180(b.w), e: norm180(b.e) };
  }
  function bboxHasPoint(nb, lat, lon) {
    return lat >= nb.s && lat <= nb.n && (nb.all || lonIn(norm180(lon), nb.w, nb.e));
  }
  function bboxMeetsShard(nb, row) {
    if (row[0] > nb.n || row[2] < nb.s) return false;
    if (nb.all) return true;
    // Two circular arcs intersect iff one contains the other's start.
    var sw = norm180(row[1]), se = norm180(row[3]);
    return lonIn(sw, nb.w, nb.e) || lonIn(nb.w, sw, se);
  }

  function isGeo(meta) {
    return !!(meta && meta.layout === 'geo' && Array.isArray(meta.shards)
      && Array.isArray(meta.sub_chunks) && meta.shards.length
      && meta.shards.length === meta.sub_chunks.length);
  }

  // JSON bytes kept parsed at once. Parsed heap is ~3-5x the JSON size.
  // WebKit has no navigator.deviceMemory, so iOS takes the fixed value.
  function budgetBytes() {
    var gb = (typeof navigator !== 'undefined') && navigator.deviceMemory;
    var mib = gb ? Math.min(32, Math.max(12, gb * 4)) : 16;
    return mib * 1024 * 1024;
  }

  // LRU of shard fetch promises, charged by the manifest's byte size
  // while still pending. Rejections are dropped, never cached.
  function makeCache(capBytes) {
    var entries = new Map();
    var total = 0;
    function drop(key) {
      var ent = entries.get(key);
      if (!ent) return;
      entries['delete'](key);
      total -= ent.bytes;
    }
    return {
      get: function(key, bytes, fetcher) {
        var ent = entries.get(key);
        if (ent) {
          entries['delete'](key);
          entries.set(key, ent);
          return ent.p;
        }
        var p = Promise.resolve().then(fetcher);
        ent = { p: p, bytes: bytes };
        entries.set(key, ent);
        total += bytes;
        p.then(null, function() { if (entries.get(key) === ent) drop(key); });
        var it = entries.keys();
        while (total > capBytes && entries.size > 1) {
          var oldest = it.next().value;
          if (oldest === key) break;
          drop(oldest);
        }
        return p;
      },
      clear: function() { entries.clear(); total = 0; }
    };
  }

  // opts: fetchShard(suffix) → Promise<Array>, cache (makeCache), cacheKey
  // (chip id), point {lat, lon} (required), bbox (optional viewport),
  // k (300), predicate(rec) (optional), inBox(rec) (optional: a further
  // test with the bbox, e.g. "on screen, not under the search box"; it
  // counts toward k like the bbox, so k records that pass it are found,
  // and the fallback without the bbox skips it), budgetBytes,
  // isCancelled(), fallbackToNearest (retry without bbox when nothing is
  // inside it).
  // Resolves null when cancelled, else {records (nearest first, <= k),
  // dists (km), partial, radiusKm, failed, fellBack, shardsLoaded,
  // bytesLoaded}. partial: a shard that could still hold one of the k
  // nearest was not loaded (budget or fetch failure); results are exact
  // within radiusKm. failed: at least one shard fetch failed.
  function load(meta, opts) {
    var k = opts.k || 300;
    var budget = opts.budgetBytes || budgetBytes();
    var plat = +opts.point.lat, plon = norm180(+opts.point.lon);
    var cancelled = opts.isCancelled || function() { return false; };
    function attempt(nb) {
      var cands = [];
      for (var i = 0; i < meta.shards.length; i++) {
        var row = meta.shards[i];
        if (!row || typeof row[0] !== 'number') continue;
        if (nb && !bboxMeetsShard(nb, row)) continue;
        cands.push({ i: i, bytes: +row[5] || 0,
                     lb: boxLowerBoundKm(plat, plon, row[0], row[1], row[2], row[3]) });
      }
      cands.sort(function(a, b) { return a.lb - b.lb || a.i - b.i; });
      var best = [];
      var next = 0, loadedBytes = 0, shardsLoaded = 0;
      var unresolvedKm = Infinity;
      var failed = false;
      function kthKm() { return best.length >= k ? best[k - 1].d : Infinity; }
      function finish() {
        var partial = unresolvedKm < Infinity && !(kthKm() <= unresolvedKm);
        return {
          records: best.map(function(x) { return x.r; }),
          dists: best.map(function(x) { return x.d; }),
          partial: partial, radiusKm: partial ? unresolvedKm : Infinity,
          failed: failed, fellBack: false, shardsLoaded: shardsLoaded, bytesLoaded: loadedBytes
        };
      }
      function step() {
        if (cancelled()) return null;
        var batch = [];
        // One shard first: in a dense city it alone often holds the k
        // nearest, and the stop rule is only checked between batches.
        var batchMax = next === 0 ? 1 : 4;
        while (next < cands.length && batch.length < batchMax) {
          var c = cands[next];
          if (c.lb >= kthKm()) { next = cands.length; break; }  // exact: done
          if (loadedBytes + c.bytes > budget && next > 0) {  // always try one
            unresolvedKm = Math.min(unresolvedKm, c.lb);
            next = cands.length;
            break;
          }
          batch.push(c);
          loadedBytes += c.bytes;
          next++;
        }
        if (!batch.length) return finish();
        return Promise.all(batch.map(function(c) {
          var suffix = meta.sub_chunks[c.i];
          var fetch1 = function() { return opts.fetchShard(suffix); };
          var p = opts.cache
            ? opts.cache.get((opts.cacheKey || '') + '/' + suffix, c.bytes, fetch1)
            : Promise.resolve().then(fetch1);
          return p.then(function(recs) { return Array.isArray(recs) ? recs : null; },
                        function() { return null; });
        })).then(function(arrays) {
          if (cancelled()) return null;
          for (var b = 0; b < batch.length; b++) {
            var recs = arrays[b];
            if (!recs) {
              failed = true;
              unresolvedKm = Math.min(unresolvedKm, batch[b].lb);
              continue;
            }
            shardsLoaded++;
            for (var j = 0; j < recs.length; j++) {
              var r = recs[j];
              if (!r || typeof r.a !== 'number' || typeof r.o !== 'number') continue;
              if (nb && !bboxHasPoint(nb, r.a, r.o)) continue;
              if (nb && opts.inBox && !opts.inBox(r)) continue;
              if (opts.predicate && !opts.predicate(r)) continue;
              best.push({ r: r, d: distKm(plat, plon, r.a, r.o) });
            }
          }
          best.sort(function(x, y) { return x.d - y.d; });
          if (best.length > k) best.length = k;
          return step();
        });
      }
      return Promise.resolve().then(step);
    }
    var nb0 = normBbox(opts.bbox);
    return attempt(nb0).then(function(res) {
      if (!res || !nb0 || res.records.length || !opts.fallbackToNearest || cancelled()) return res;
      return attempt(null).then(function(res2) {
        if (res2) res2.fellBack = true;
        return res2;
      });
    });
  }

  // Circular-mean centre of all geo shard boxes — last-resort origin
  // when there is no GPS, picked place, or map viewport.
  function centre(meta) {
    var x = 0, y = 0, lat = 0, n = 0;
    for (var i = 0; i < meta.shards.length; i++) {
      var r = meta.shards[i];
      if (!r || typeof r[0] !== 'number') continue;
      var mid = r[1] <= r[3] ? (r[1] + r[3]) / 2 : norm180((r[1] + r[3] + 360) / 2);
      var wgt = +r[4] || 1;
      x += wgt * Math.cos(mid * D2R);
      y += wgt * Math.sin(mid * D2R);
      lat += wgt * (r[0] + r[2]) / 2;
      n += wgt;
    }
    if (!n) return null;
    return { lat: lat / n, lon: Math.atan2(y, x) / D2R };
  }

  return {
    isGeo: isGeo, load: load, makeCache: makeCache, budgetBytes: budgetBytes,
    centre: centre, distKm: distKm,
    _test: { boxLowerBoundKm: boxLowerBoundKm, normBbox: normBbox,
             bboxHasPoint: bboxHasPoint, bboxMeetsShard: bboxMeetsShard }
  };
})();
// END chip-shards
