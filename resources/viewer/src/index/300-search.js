// --- Search functionality ---
// Uses chunked index: search-data/manifest.json lists available prefix chunks,
// and search-data/{prefix}.json files are loaded on demand. This keeps RAM
// bounded even for world-scale datasets (millions of features).
var searchMarker = null;

// Drop a red marker at (lat, lon) with a "Directions to here" popup.
// Shared by the search-result picker and the #pin= deep-link path
// (Find-page "Map" button). Replaces any existing search marker.
// `map` is the module-scope map instance; callers ensure it's ready.
function placeSearchPin(map, lat, lon, name, enrich) {
  if (searchMarker) { searchMarker.remove(); searchMarker = null; }
  searchMarker = new maplibregl.Marker({ color: '#e74c3c' })
    .setLngLat([lon, lat])
    .addTo(map);
  if (!window.streetzimRouting ||
      typeof window.streetzimRouting.open !== 'function') {
    return searchMarker;
  }
  enrich = enrich || {};
  var box = document.createElement('div');
  box.className = 'pin-popup';
  if (name) {
    var nm = document.createElement('div');
    nm.className = 'pin-name';
    nm.textContent = name;
    box.appendChild(nm);
  }
  // Brand subline — shows chain name ("Holiday Inn", "Starbucks") when
  // the place's own name doesn't already contain it.
  var brand = enrich.brand;
  if (brand && (!name || name.toLowerCase().indexOf(brand.toLowerCase()) === -1)) {
    var br = document.createElement('div');
    br.className = 'pin-brand';
    br.textContent = brand;
    box.appendChild(br);
  }
  // Category label from Overture's normalized taxonomy
  // (museum / hotel / vietnamese_restaurant / …).
  var cat = enrich.cat || enrich.subtype;
  if (cat) {
    var ct = document.createElement('div');
    ct.className = 'pin-cat';
    ct.textContent = String(cat).replace(/_/g, ' ');
    box.appendChild(ct);
  }
  // Contact row — icon links for website, phone, socials.
  var contact = document.createElement('div');
  contact.className = 'pin-contact';
  var any = false;
  function addLink(href, glyph, title) {
    var a = document.createElement('a');
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener';
    a.className = 'pin-link';
    a.textContent = glyph;
    a.title = title;
    contact.appendChild(a);
    any = true;
  }
  // Only http(s) links may come out of index data (a `javascript:`
  // website value would otherwise execute on tap).
  var wsHref = (typeof enrich.ws === 'string' && /^https?:\/\//i.test(enrich.ws.trim()))
    ? enrich.ws.trim() : null;
  if (wsHref) addLink(wsHref, '🌐', wsHref);
  if (enrich.p)  addLink('tel:' + String(enrich.p).replace(/\s+/g, ''), '📞', String(enrich.p));
  if (enrich.soc && enrich.soc.length) {
    enrich.soc.forEach(function(s) {
      var host = s.toLowerCase();
      var g = /facebook/.test(host) ? 'f' :
              /instagram/.test(host) ? 'IG' :
              /twitter|x\.com/.test(host) ? '𝕏' :
              /tiktok/.test(host) ? 'TT' : '•';
      addLink(s, g, s);
    });
  }
  if (any) box.appendChild(contact);

  // NOTE: see project_directions_button_duplicated.md memory.
  // This is the SEARCH-PIN copy of the Directions button. There's
  // a near-identical sibling in initWikidataPopups
  // (~line 4382). Any click-handler change MUST be applied to
  // BOTH or only one popup-source gets the new behaviour.
  var btn = document.createElement('button');
  btn.className = 'pin-directions';
  btn.textContent = 'Directions to here';
  btn.addEventListener('click', function(ev) {
    // Close the parent popup before firing the route — otherwise it
    // sits on top of the drawn route asking the user to choose
    // "Directions to here" again. Cf. the same fix in
    // initWikidataPopups.buildDirectionsButton — there are TWO copies
    // of this button-construction code in the file (this one is the
    // search-pin path; the other is the Wikidata feature-popup path).
    try {
      var popEl = ev && ev.target && ev.target.closest
        ? ev.target.closest('.maplibregl-popup') : null;
      if (popEl) {
        var closer = popEl.querySelector('.maplibregl-popup-close-button');
        if (closer) closer.click();
      }
      if (window.__streetzim_map
          && typeof window.__streetzim_map._closeWikidataPopup === 'function') {
        window.__streetzim_map._closeWikidataPopup();
      }
      if (typeof _findResultsState !== 'undefined'
          && _findResultsState.markers) {
        for (var __i = 0; __i < _findResultsState.markers.length; __i++) {
          var __mm = _findResultsState.markers[__i];
          if (!__mm) continue;
          try {
            var __pp = __mm.getPopup();
            if (__pp && __pp.isOpen()) __mm.togglePopup();
          } catch (e2) {}
        }
      }
    } catch (e3) {}
    window.streetzimRouting.open();
    window.streetzimRouting.setDest(lat, lon, name || undefined);
    var cached = window.__streetzimLastLoc;
    var fresh = cached && (Date.now() - cached.ts) < 10 * 60 * 1000;
    if (fresh) {
      window.streetzimRouting.setOrigin(cached.lat, cached.lon, 'Current location');
    } else if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        function(pos) {
          window.__streetzimLastLoc = {
            lat: pos.coords.latitude, lon: pos.coords.longitude, ts: Date.now()
          };
          window.streetzimRouting.setOrigin(
            pos.coords.latitude, pos.coords.longitude, 'Current location');
        },
        function() {},
        { enableHighAccuracy: false, maximumAge: 60000, timeout: 8000 });
    }
  });
  box.appendChild(btn);
  var popup = new maplibregl.Popup({ offset: 28, closeButton: true,
                                     closeOnClick: false,
                                     maxWidth: '320px' })
    .setDOMContent(box);
  searchMarker.setPopup(popup);
  _szPopupGap(map, popup);
  // Auto-open once the camera settles — or right away when no camera
  // move is in flight (`#pin=` without `map=`), otherwise the popup
  // used to pop on whatever unrelated pan came next. A pending
  // listener from an earlier pin is detached so two rapid pins can't
  // toggle the newer popup open and then closed again.
  if (_searchPinOpenHandler) {
    try { map.off('moveend', _searchPinOpenHandler); } catch (e) {}
    _searchPinOpenHandler = null;
  }
  var thisMarker = searchMarker;
  if (map.isMoving && map.isMoving()) {
    _searchPinOpenHandler = function() {
      _searchPinOpenHandler = null;
      if (searchMarker === thisMarker && !thisMarker.getPopup().isOpen()) {
        thisMarker.togglePopup();
      }
    };
    map.once('moveend', _searchPinOpenHandler);
  } else {
    thisMarker.togglePopup();
  }
  return searchMarker;
}
var _searchPinOpenHandler = null;

function initSearch(map) {
  var input = document.getElementById('search-input');
  var resultsEl = document.getElementById('search-results');
  var clearBtn = document.getElementById('search-clear');
  var activeIdx = -1;

  var manifest = null;       // {total: N, chunks: {prefix: count, ...}}
  var chunkCache = {};       // prefix -> array of entries
  var chunkCacheBytes = {};  // prefix -> approximate bytes held
  var chunkCacheOrder = [];  // prefixes, least recently used first
  var chunkCacheTotal = 0;
  function touchChunk(prefix) {
    var i = chunkCacheOrder.indexOf(prefix);
    if (i >= 0) chunkCacheOrder.splice(i, 1);
    chunkCacheOrder.push(prefix);
  }
  // Parsed chunks stay referenced here after the streaming filter is done
  // with them, so this cache — not the filter — set the page's peak: 20
  // chunks of 15 MB JSON on Japan was ~300 MB of live objects, the range
  // that gets a WebView killed on a phone. Bound it by bytes instead
  // (the SW / kiwix-serve send Content-Length; else estimate from the
  // record count), smaller where the device reports ≤ 2 GB.
  var CHUNK_CACHE_BUDGET = ((navigator.deviceMemory && navigator.deviceMemory <= 2) ? 12 : 32) * 1024 * 1024;
  var pendingFetches = {};   // prefix -> Promise

  // Load manifest
  fetch(baseUrl + 'search-data/manifest.json')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      manifest = data;
      input.placeholder = 'Search ' + data.total.toLocaleString() + ' places...';
    })
    .catch(function() {
      input.placeholder = 'Search unavailable';
      input.disabled = true;
    });

  // Fetch a chunk by prefix, with caching
  function fetchChunk(prefix) {
    if (chunkCache[prefix]) { touchChunk(prefix); return Promise.resolve(chunkCache[prefix]); }
    if (pendingFetches[prefix]) return pendingFetches[prefix];

    var p = fetch(baseUrl + 'search-data/' + encodeURIComponent(prefix) + '.json')
      .then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        var len = parseInt(r.headers.get('Content-Length') || '0', 10) || 0;
        return r.json().then(function(data) { return { data: data, bytes: len }; });
      })
      .then(function(o) {
        var data = o.data;
        // Parsed JSON weighs a few times its text; ~160 B per record when
        // the length is unknown.
        var bytes = o.bytes ? o.bytes * 3 : (Array.isArray(data) ? data.length * 160 : 0);
        delete pendingFetches[prefix];
        // Always keep the newest chunk, even one over the whole budget: a
        // hot 15 MB chunk takes seconds to parse on a phone and the next
        // keystroke needs it again. Everything else goes, least recently
        // used first, until under budget. (Object.keys() would list
        // digit prefixes like "12" first whatever their age — hence the
        // explicit order.)
        chunkCacheTotal += bytes - (chunkCacheBytes[prefix] || 0);
        chunkCache[prefix] = data;
        chunkCacheBytes[prefix] = bytes;
        touchChunk(prefix);
        while (chunkCacheOrder.length > 1 && chunkCacheTotal > CHUNK_CACHE_BUDGET) {
          var k = chunkCacheOrder.shift();
          chunkCacheTotal -= chunkCacheBytes[k] || 0;
          delete chunkCache[k];
          delete chunkCacheBytes[k];
        }
        return data;
      })
      .catch(function() {
        delete pendingFetches[prefix];
        return [];
      });
    pendingFetches[prefix] = p;
    return p;
  }

  // Lowercase + strip diacritics — so "cafe" matches "Café" and "sao" matches "São".
  function normalizeText(s) {
    return s.normalize ? s.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
                       : s.toLowerCase();
  }

// BEGIN search-shards
// Character-path + tier targeting for search-data leaves. Mirrors
// cloud/search_shards.py — if the two drift, a reader asks for leaves the
// writer never wrote. See docs/search-prefix-locality.md.
//
// Old layout: a hot prefix was fanned out by FNV hash, so a query had to
// fetch every leaf ("Caracas" on south-america: 736 files, 320 MB, 99 s).
// Here the manifest's char_split names the character paths that exist, and
// the leaf holding what was typed is computed directly.
var SEARCH_SHARDS = (function () {
  'use strict';
  var SEP = '~';
  var TERMINAL = '_e';          // "the word ends here" (never a character)
  var TIERS = ['c', 'p', 's'];  // place-like, POI, street; 'a' is gated below

  function tokenFor(ch) {
    var code = ch.codePointAt(0);
    if (code >= 128) return 'u' + code.toString(16);
    var alnum = (code >= 48 && code <= 57) || (code >= 97 && code <= 122);
    return (alnum || ch === '_') ? ch : '_';
  }

  // Tokens of the characters following the prefix, as the writer computed
  // them: a 'u<hex>' prefix consumed one code point, an ASCII one consumed
  // two. Code points, not UTF-16 units, or astral names desync.
  function pathTokens(prefix, word) {
    var chars = Array.from(word || '');
    var rest = chars.slice(prefix.indexOf('u') === 0 ? 1 : 2);
    var out = [];
    for (var i = 0; i < rest.length; i++) out.push(tokenFor(rest[i]));
    return out;
  }

  function isPrefixOf(a, b) {          // a, b: token arrays
    if (a.length > b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  // Which declared paths cover what was typed. Both directions matter:
  // typing "car" must read every leaf under "r" (including the terminal
  // "r~_e"), and typing "cara" must still read the shallower leaf "r" when
  // that node was never split.
  function pathsFor(declared, typed) {
    var out = [];
    for (var i = 0; i < declared.length; i++) {
      var p = declared[i] ? declared[i].split(SEP) : [];
      if (isPrefixOf(p, typed) || isPrefixOf(typed, p)) out.push(declared[i]);
    }
    return out;
  }

  // Leaf names for one prefix, tier by tier, in fetch order. Returns null
  // when this ZIM has no character split for the prefix (caller keeps the
  // old whole-prefix behaviour), or [] when the typed characters exist in
  // no path at all — which means no records, not "fetch everything".
  function leavesFor(manifest, prefix, word, query, resolve) {
    var cs = manifest && manifest.char_split && manifest.char_split[prefix];
    if (!cs || !cs.length) return null;
    var typed = pathTokens(prefix, word);
    if (!typed.length) return null;            // 1-2 chars: whole prefix
    var paths = pathsFor(cs, typed);
    if (!paths.length) return [];
    // Addresses are ~86% of a hot prefix and are only what the user wants
    // once they type a house number; the map viewer already discards them
    // for non-digit queries, after downloading them.
    var tiers = TIERS.slice();
    if (/\d/.test(query || '') && String(query || '').replace(/\W/g, '').length >= 4) {
      tiers.push('a');
    }
    var out = [];
    var seen = {};
    for (var t = 0; t < tiers.length; t++) {
      for (var i = 0; i < paths.length; i++) {
        var name = prefix + SEP + paths[i] + SEP + tiers[t];
        // Through expandPrefix, never a direct fetch: a later repackage
        // hash-splits an oversized leaf into name-0…name-f and copies
        // char_split through verbatim.
        var leaves = resolve(name);
        for (var j = 0; j < leaves.length; j++) {
          if (!seen[leaves[j]]) { seen[leaves[j]] = 1; out.push(leaves[j]); }
        }
      }
    }
    return out;
  }

  return {
    tokenFor: tokenFor,
    pathTokens: pathTokens,
    pathsFor: pathsFor,
    leavesFor: leavesFor,
    TERMINAL: TERMINAL,
    _test: { isPrefixOf: isPrefixOf }
  };
})();
// END search-shards

  // Stream-fetch + per-chunk filter for large prefix expansions.
  //
  // Hot prefixes like "sa" (the leading letters for "San Francisco",
  // "Sacramento", thousands of others) are recursive-split into 256
  // hash-bucketed sub-chunks (`sa-0-0` … `sa-f-f`) each ~1.5 MB on
  // disk. Naively `Promise.all(prefixes.map(fetchChunk))` would
  // download AND keep parsed all 256 (~1.5 GB of JS heap) — iOS
  // Safari OOMs and the page reloads. cf. user report on "san "
  // crashing the iPhone.
  //
  // This helper:
  //   • caps concurrent in-flight fetches at MAX_CONCURRENT (peak
  //     ~9 MB of parsed JSON live at any moment)
  //   • filters records on parse — only those whose normalized name
  //     contains the normalized query are retained, so the original
  //     chunk array goes straight to GC after extraction
  //   • caps the accumulated matches (very generous — we just want
  //     to bound runaway worst case)
  //
  // Build-side fix is queued: prefix-based recursive split (use the
  // next character of the most common word, not FNV hash) would let
  // "san" target a single sub-chunk instead of all 256.
  function streamFilterChunks(prefixes, query) {
    if (!prefixes || !prefixes.length) return Promise.resolve([]);
    var qNorm = normalizeText(query || '');
    // Skip address records unless the query contains a digit. Each
    // sa-X-Y chunk is ~94 % addresses (11.7k of 12.5k), so without
    // this filter the 20k MAX_RESULTS cap fills with addresses long
    // before we reach the chunk that holds the actual city of San
    // Diego (sa-d-b). For typeahead queries like "san" the user
    // wants cities/POIs/streets, not "1023 10th Avenue, San Diego" —
    // and once they type a street number ("1023 san jose ave") we
    // re-include addresses.
    var includeAddrs = /\d/.test(qNorm);
    var matches = [];
    var MAX_CONCURRENT = 6;
    var MAX_RESULTS = 20000;  // soft cap; scorer slices top ~20 anyway
    var i = 0;
    return new Promise(function(resolve) {
      var inFlight = 0;
      var done = false;
      function pump() {
        if (done) return;
        if (matches.length >= MAX_RESULTS && inFlight === 0) {
          done = true; return resolve(matches);
        }
        if (i >= prefixes.length && inFlight === 0) {
          done = true; return resolve(matches);
        }
        while (inFlight < MAX_CONCURRENT
               && i < prefixes.length
               && matches.length < MAX_RESULTS) {
          (function(prefix) {
            inFlight++;
            fetchChunk(prefix).then(function(chunk) {
              if (chunk && chunk.length) {
                for (var j = 0; j < chunk.length; j++) {
                  var rec = chunk[j];
                  var name = rec && rec.n;
                  if (!name) continue;
                  if (!includeAddrs && rec.t === 'addr') continue;
                  if (qNorm && normalizeText(name).indexOf(qNorm) < 0) continue;
                  matches.push(rec);
                  if (matches.length >= MAX_RESULTS) break;
                }
              }
              // chunk goes out of scope here — eligible for GC.
            }).catch(function() {
              // Swallow individual chunk failures; partial results
              // are better than no results.
            }).then(function() {
              inFlight--;
              pump();
            });
          })(prefixes[i++]);
        }
      }
      pump();
    });
  }

  // Determine which chunk prefixes are needed for a query. With multi-word
  // indexing on the server side, entries live in chunks keyed by each of
  // their words' 2-char prefixes — so we fetch one chunk per query word to
  // hit substring matches like "cathedral" → "Washington National Cathedral".
  function getPrefixes(query, opts) {
    var q = normalizeText(query);
    // >= 2 chars, matching the writer (`_prefixes_for` skips shorter words)
    // and places.html. Keeping 1-char words made "a coffee" expand the whole
    // "a_" prefix for nothing.
    var words = q.split(/[^\p{L}\p{N}]+/u).filter(function(w) { return w.length >= 2; });

    function keyFor(word) {
      // Latin-leading words keep the 2-char ASCII-alnum prefix. Non-ASCII
      // first chars (CJK, Cyrillic, Arabic, Thai, …) bucket into
      // 'u' + lowercase hex of their codepoint. Must match the writer
      // ``_prefix_key`` in create_osm_zim.py and the Swift
      // ``Geocoder.normalizePrefix``.
      if (!word) return '__';
      var c0 = word[0];
      var c0code = c0.charCodeAt(0);
      // Non-ASCII → codepoint hex bucket. codePointAt handles surrogate
      // pairs correctly; no zero-padding so keys stay variable-length.
      if (c0code >= 128) {
        return 'u' + word.codePointAt(0).toString(16);
      }
      // ASCII path. First char: alnum or '_' kept, else '_'.
      function asciiNorm(ch) {
        var code = ch.charCodeAt(0);
        if (ch === '_') return '_';
        if (code >= 48 && code <= 57) return ch;    // 0-9
        if (code >= 97 && code <= 122) return ch;   // a-z
        return '_';
      }
      var k0 = asciiNorm(c0);
      var k1 = '_';
      if (word.length >= 2) {
        var c1 = word[1];
        // 2nd char non-ASCII → collapse to '_' so the bucket is keyed
        // by c0 alone; avoids splitting a single-leading-char search
        // across two chunks.
        k1 = (c1.charCodeAt(0) >= 128) ? '_' : asciiNorm(c1);
      }
      return k0 + k1;
    }

    // Hot-chunk sub-split: when a prefix in the old scheme exceeded the
    // size threshold during repackage, it was fanned out into N hashed
    // sub-chunks (e.g. ``av`` → ``av-0``…``av-f``). The manifest records
    // these in ``sub_chunks``. We can't predict which sub-bucket a
    // record lives in from the query alone, so we fetch ALL of them and
    // let the scorer filter. Manifest schema: sub_chunks = {prefix: [sub…]}.
    // If ``sub_chunks`` is absent, we're on a pre-split ZIM — look up
    // the plain prefix as before.
    var subChunks = (manifest && manifest.sub_chunks) || {};

    // Which names have hash children (`ca~r~c-0`…), computed once per
    // manifest instead of re-scanning every chunk key per candidate leaf.
    function _hashParents() {
      if (manifest.__hashParents) return manifest.__hashParents;
      var parents = {};
      for (var k in manifest.chunks) {
        var cut = k.lastIndexOf('-');
        if (cut > 0) parents[k.slice(0, cut)] = 1;
      }
      for (var sk in subChunks) parents[sk] = 1;
      manifest.__hashParents = parents;
      return parents;
    }
    function expandPrefix(k, seen, out) {
      if (seen[k]) return;
      seen[k] = true;
      // Direct chunk hit — done.
      if (manifest.chunks[k] !== undefined) { out.push(k); return; }
      // Recursive fan-out. Canada has prefixes that were split TWICE
      // (e.g. "to" → "to-0".."to-f" → "to-0-0", "to-1-1", …) and
      // ``sub_chunks['to']`` was emitted as an empty list by an
      // earlier writer — the real first-level relationship was
      // dropped, but the second-level (``sub_chunks['to-0']``) is
      // present. So when we don't find ``k`` directly:
      //   1. Try its declared sub-chunks (one level).
      //   2. Otherwise scan ``sub_chunks`` for any entry whose key
      //      starts with ``k + '-'`` (covers the case where the
      //      parent's list was empty but children exist).
      //   3. Otherwise scan ``chunks`` for any leaf starting with
      //      ``k + '-'`` (final fallback for non-recursive splits).
      var direct = subChunks[k];
      if (direct && direct.length) {
        for (var j = 0; j < direct.length; j++) expandPrefix(direct[j], seen, out);
        return;
      }
      // Both separators: '-' for hash buckets, '~' for the character +
      // tier leaves (docs/search-prefix-locality.md). A prefix whose
      // sub_chunks list shipped empty — 143 of them on united-states —
      // is reachable ONLY through these scans, so a scan that knew just
      // '-' would return zero leaves on a character-split ZIM.
      var seps = ['-', '~'];
      var found = false;
      for (var s = 0; s < seps.length; s++) {
        var prefix = k + seps[s];
        for (var sk in subChunks) {
          if (sk.indexOf(prefix) === 0) { found = true; expandPrefix(sk, seen, out); }
        }
      }
      if (found) return;
      for (var s2 = 0; s2 < seps.length; s2++) {
        var cprefix = k + seps[s2];
        for (var ck in manifest.chunks) {
          if (ck.indexOf(cprefix) === 0 && !seen[ck]) {
            seen[ck] = true; out.push(ck);
          }
        }
      }
    }

    var seen = {};
    var prefixes = [];
    var targeted = false;

    // Resolve one leaf name through the same expansion (hash children,
    // empty sub_chunks lists, …) rather than fetching it blind.
    //
    // Fast path first: expandPrefix's miss branch scans sub_chunks and
    // chunks four times over, and a CJK prefix asks about ~950 candidate
    // leaves per keystroke — 1.06 s on korea-mongolia's 10,855 chunks, and
    // united-states has ten times as many.
    function resolveLeaf(name) {
      if (manifest.chunks[name] !== undefined) return [name];
      if (!_hashParents()[name]) return [];
      var s2 = {}, o2 = [];
      expandPrefix(name, s2, o2);
      return o2;
    }

    // On a character-split ZIM, read the leaf holding what was typed
    // instead of every leaf under the prefix (docs/search-prefix-locality.md).
    function addFor(word) {
      var key = keyFor(word);
      var leaves = (opts && opts.noTargeting) ? null
        : SEARCH_SHARDS.leavesFor(manifest, key, word, q, resolveLeaf);
      if (leaves === null) { expandPrefix(key, seen, prefixes); return; }
      targeted = true;   // [] means "no records for those characters"
      for (var j = 0; j < leaves.length; j++) {
        if (!seen[leaves[j]]) { seen[leaves[j]] = true; prefixes.push(leaves[j]); }
      }
    }

    for (var i = 0; i < words.length; i++) addFor(words[i]);
    // The whole name is indexed under its own first two characters, with
    // spaces folded — "45 Broadway" is findable by typing "45 b".
    addFor(q.replace(/\s/g, '_'));
    prefixes.targeted = targeted;
    // Single-char: fetch every chunk starting with that char (skip
    // hashed sub-chunks, whose name is {prefix}-{hex} — the parent
    // prefix has already been expanded above if it was 1-char).
    if (q.length < 2) {
      var c = q[0] || '';
      for (var kk in manifest.chunks) {
        if (kk[0] === c && !seen[kk]) { seen[kk] = true; prefixes.push(kk); }
      }
    }
    return prefixes;
  }

  function scoreResults(entries, query) {
    var q = normalizeText(query);
    var words = q.split(/\s+/).filter(function(w) { return w; });
    var matches = [];
    var seenKeys = {};  // dedup across chunks (entry may be in multiple)

    // Get current map center and bounds for proximity scoring
    var center = map.getCenter();
    var clat = center.lat;
    var clng = center.lng;
    var bounds = map.getBounds();

    for (var i = 0; i < entries.length; i++) {
      var item = entries[i];
      // Dedup: same entry may appear in multiple word-keyed chunks.
      var dedupKey = item.n + '|' + item.a + '|' + item.o;
      if (seenKeys[dedupKey]) continue;
      seenKeys[dedupKey] = true;

      var name = normalizeText(item.n);

      // All words must appear in the name
      var allMatch = true;
      var textScore = 0;
      for (var w = 0; w < words.length; w++) {
        var pos = name.indexOf(words[w]);
        if (pos === -1) { allMatch = false; break; }
        if (pos === 0 || name[pos - 1] === ' ') textScore += 10;
        textScore += 1;
      }
      if (!allMatch) continue;

      if (name === q) textScore += 50;
      if (name.indexOf(q) === 0) textScore += 25;
      var typeBonus = {place: 20, airport: 15, peak: 10, park: 10, water: 5, poi: 5, building: 3, street: 0};
      textScore += typeBonus[item.t] || 0;
      // Subtype boost — cities and counties carry far more public
      // relevance than a same-named neighbourhood or hamlet, but
      // they're often FAR from the user's current view (San Diego
      // when typing "san" in SF) and would otherwise be crushed by
      // the proximity multiplier. Adding a sizable subtype bonus
      // lifts them above local non-place hits even at 1× prox.
      var placeSubBonus = {
        city: 200, county: 80, region: 60, town: 30,
        suburb: 10, village: 8, neighbourhood: 4,
      };
      if (item.t === 'place' && item.s) {
        textScore += placeSubBonus[item.s] || 0;
      }

      // Proximity is the PRIMARY signal — multiply text score by proximity factor
      // This means a mediocre text match nearby always beats a perfect match far away
      var proximityLabel = '';
      var score = textScore; // fallback if no coordinates
      if (item.a !== undefined && item.o !== undefined) {
        var dlat = item.a - clat;
        // Scale longitude by cos(lat) so the degree-space distance is
        // isotropic; raw Δlng overstated E–W distances 2x at 60°N.
        var dlng = (item.o - clng) * Math.cos(clat * Math.PI / 180);
        var dist = Math.sqrt(dlat * dlat + dlng * dlng);
        var viewSpan = Math.max(
          bounds.getNorth() - bounds.getSouth(),
          bounds.getEast() - bounds.getWest()
        );
        var ratio = dist / (viewSpan || 1);

        // Proximity multiplier: on-screen = 10x, few viewports = 5x, far = 1x
        var proxMultiplier = 1 + 9 * Math.exp(-ratio * 0.5);
        score = textScore * proxMultiplier;

        // Generate human-readable distance for display
        var miles = dist * 69;
        if (miles < 0.5) proximityLabel = 'nearby';
        else if (miles < 5) proximityLabel = Math.round(miles) + ' mi';
        else if (miles < 50) proximityLabel = Math.round(miles / 5) * 5 + ' mi';
        else if (miles < 500) proximityLabel = Math.round(miles / 10) * 10 + ' mi';
        else proximityLabel = Math.round(miles / 100) * 100 + ' mi';
      }

      matches.push({ item: item, score: score, dist: proximityLabel });
    }

    matches.sort(function(a, b) { return b.score - a.score; });
    return matches.slice(0, 15);
  }

  // Stash the rendered match set so selectResult can look up the full
  // record (including Overture enrichment fields) by index — cleaner
  // than mirroring every optional field as a data-attribute.
  var lastSearchMatches = [];

  function renderResults(matches) {
    activeIdx = -1;
    lastSearchMatches = matches;
    if (matches.length === 0) {
      resultsEl.innerHTML = '<div class="search-no-results">No results found</div>';
      resultsEl.style.display = 'block';
      return;
    }
    var html = '';
    for (var j = 0; j < matches.length; j++) {
      var m = matches[j].item;
      // Display label: Overture's clean category (`cat`) when present,
      // otherwise fall back to the OMT subtype.
      var rawLabel = m.cat || m.s || m.t;
      var label = rawLabel ? String(rawLabel).replace(/_/g, ' ') : '';
      var loc = m.l ? ' &middot; ' + escapeHtml(m.l) : '';
      var distLabel = matches[j].dist ? ' &middot; ' + escapeHtml(matches[j].dist) : '';
      html += '<div class="search-result" data-idx="' + j +
        '" data-lat="' + m.a + '" data-lon="' + m.o + '" data-type="' + m.t +
        '" data-name="' + escapeHtml(m.n) + '">' +
        '<span class="search-result-name">' + escapeHtml(m.n) + '</span>' +
        '<span class="search-result-type">' + escapeHtml(label) + loc + distLabel + '</span></div>';
    }
    resultsEl.innerHTML = html;
    resultsEl.style.display = 'block';
  }

  var searchSeq = 0;  // Sequence counter to discard stale results

  function doSearch(query) {
    if (!manifest || query.length < 2) {
      resultsEl.style.display = 'none';
      return;
    }

    var seq = ++searchSeq;
    var prefixes = getPrefixes(query);
    if (prefixes.length === 0) {
      renderResults([]);
      return;
    }

    // Show a "Searching…" row immediately. On Japan a prefix like `sh`
    // can land on a 15 MB JSON chunk which takes 20-40s to fetch+parse
    // over Kiwix's SW on iOS; without feedback the input feels hung.
    // Skip the placeholder if any needed chunk is already cached —
    // the response will be synchronous-enough that a flicker is worse
    // than no indicator.
    var anyMissing = false;
    for (var pi = 0; pi < prefixes.length; pi++) {
      if (!chunkCache[prefixes[pi]]) { anyMissing = true; break; }
    }
    if (anyMissing) {
      while (resultsEl.firstChild) resultsEl.removeChild(resultsEl.firstChild);
      var loadingRow = document.createElement('div');
      loadingRow.className = 'search-result';
      loadingRow.style.pointerEvents = 'none';
      loadingRow.style.opacity = '0.7';
      loadingRow.textContent = 'Searching…';
      resultsEl.appendChild(loadingRow);
      resultsEl.style.display = 'block';
    }

    // Stream-fetch with per-chunk filter — bounds peak heap on hot
    // prefixes (e.g. "sa" → 256 sub-chunks). See streamFilterChunks.
    streamFilterChunks(prefixes, query).then(function(matches) {
      if (seq !== searchSeq) return;  // Stale query, discard
      var scored = scoreResults(matches, query);
      // Targeting reads the leaf for what was typed, so it misses matches
      // inside a word ("Caiçara" for "car" — measured ~2% of hits, nearly
      // all street names). When that would leave the user with almost
      // nothing, pay for the old whole-prefix scan instead.
      if (scored.length < 5 && prefixes.targeted) {
        streamFilterChunks(getPrefixes(query, { noTargeting: true }), query)
          .then(function(more) {
            if (seq !== searchSeq) return;
            renderResults(scoreResults(more, query));
          })
          .catch(function() {
            // Show what targeting did find rather than leaving "Searching…".
            if (seq === searchSeq) renderResults(scored);
          });
        return;
      }
      renderResults(scored);
    });
  }

  function escapeHtml(s) {
    // Quotes too — the result is also placed inside data-* attributes,
    // where an unescaped `"` in a place name broke out of the attribute.
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function selectResult(el) {
    var idx = parseInt(el.getAttribute('data-idx'), 10);
    var match = (isFinite(idx) && lastSearchMatches[idx]) || null;
    var item = match && match.item ? match.item : null;
    var lat = parseFloat(el.getAttribute('data-lat'));
    var lon = parseFloat(el.getAttribute('data-lon'));
    var type = el.getAttribute('data-type');
    var name = el.getAttribute('data-name') || (item && item.n) || '';
    var zoom = {place: 14, airport: 14, peak: 15, park: 15, water: 14, poi: 17, street: 16}[type] || 15;
    // Pull Overture enrichment fields when the result has them
    // (websites, phones, socials, brand, normalized category).
    var enrich = null;
    if (item) {
      enrich = {
        cat:   item.cat,
        subtype: item.s,
        // `ws` (website) not `w` — `w` is reserved for the Wikipedia
        // tag that OSM POIs carry in the same record; see commit
        // fa6208b's collision notes.
        ws:    item.ws,
        p:     item.p,
        soc:   item.soc,
        brand: item.brand,
        wd:    item.wd,
      };
    }
    // Start the camera move BEFORE dropping the pin: placeSearchPin
    // opens the popup immediately when no move is in flight, and
    // defers to moveend when one is — we want the latter here.
    // Bias the camera DOWN so the pin's popup clears the top chrome. The
    // popup opens above the pin, and with the pin centred it lands under the
    // search box + chip rail: a "basel" search put the chips and the
    // "Directions to here" CTA behind the search field on a 390px phone.
    // Positive offset moves the map content down, i.e. the pin sits below
    // centre. Zoom-independent pixels, same reasoning as the wiki detail
    // sheet at ~6222: a percentage-of-latitude-span shift flies kilometres
    // off target when zoomed out.
    var _topChrome = (function() {
      var sc = document.getElementById('search-container');
      var r = sc ? sc.getBoundingClientRect() : null;
      // chips sit inside #search-container; its bottom is the real edge
      return r && r.bottom ? Math.round(r.bottom) : 110;
    })();
    var _h = map.getCanvas().clientHeight || 600;
    // Just enough to clear the chrome and the popup's own height (~120px),
    // not more: 0.28*h put the pin 74% down an 844px viewport, which reads
    // as "the map jumped past my result". 0.18*h capped at chrome+60 lands
    // it around 60% with the popup clear of the search box.
    var _dy = Math.min(Math.round(_h * 0.18), _topChrome + 60);
    map.flyTo({ center: [lon, lat], zoom: zoom, duration: 1500, offset: [0, _dy] });
    placeSearchPin(map, lat, lon, name, enrich);
    resultsEl.style.display = 'none';
    input.blur();
  }

  // Input handler with debounce
  var debounceTimer;
  input.addEventListener('input', function() {
    clearTimeout(debounceTimer);
    var v = input.value.trim();
    clearBtn.style.display = v ? 'block' : 'none';
    debounceTimer = setTimeout(function() { doSearch(v); }, 200);
  });

  // Keyboard navigation
  input.addEventListener('keydown', function(e) {
    var items = resultsEl.querySelectorAll('.search-result');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      activeIdx = Math.min(activeIdx + 1, items.length - 1);
      updateActive(items);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      activeIdx = Math.max(activeIdx - 1, 0);
      updateActive(items);
    } else if (e.key === 'Enter' && activeIdx >= 0 && items[activeIdx]) {
      e.preventDefault();
      selectResult(items[activeIdx]);
    } else if (e.key === 'Escape') {
      resultsEl.style.display = 'none';
      input.blur();
    }
  });

  function updateActive(items) {
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle('active', i === activeIdx);
    }
    if (items[activeIdx]) items[activeIdx].scrollIntoView({ block: 'nearest' });
  }

  // Prevent input blur when tapping/clicking results so the keyboard
  // doesn't dismiss before the selection registers.
  resultsEl.addEventListener('mousedown', function(e) { e.preventDefault(); });

  // Touch handling: previously this preventDefault'd touchstart to
  // suppress synthetic mouse events, which had the side effect of
  // BLOCKING SCROLL inside the dropdown — every touch was treated as
  // a selection, so trying to scroll the result list immediately
  // picked an item. Replaced with a touchmove-distance check: if the
  // finger moved more than the slop threshold between touchstart and
  // touchend, treat it as a scroll and don't select. Listeners are
  // passive so iOS can run the scroll on the compositor.
  var __touchStartY = null;
  var __touchScrolled = false;
  resultsEl.addEventListener('touchstart', function(e) {
    if (e.touches.length === 1) {
      __touchStartY = e.touches[0].clientY;
      __touchScrolled = false;
    }
  }, { passive: true });
  resultsEl.addEventListener('touchmove', function(e) {
    if (__touchStartY != null && e.touches.length === 1) {
      var dy = Math.abs(e.touches[0].clientY - __touchStartY);
      if (dy > 8) __touchScrolled = true; // 8 px slop
    }
  }, { passive: true });

  resultsEl.addEventListener('click', function(e) {
    if (__touchScrolled) {
      __touchScrolled = false;
      return;
    }
    var el = e.target.closest('.search-result');
    if (el) selectResult(el);
  });

  clearBtn.addEventListener('click', function() {
    input.value = '';
    clearBtn.style.display = 'none';
    resultsEl.style.display = 'none';
    if (searchMarker) { searchMarker.remove(); searchMarker = null; }
  });

  map.on('click', function() {
    resultsEl.style.display = 'none';
  });

  // Expose map globally for DevTools diagnostics. Safe to leave in shipping
  // builds — it's just a property on window.
  try { window.__streetzim_map = map; } catch (e) {}

  // Stash the current viewport in sessionStorage so the Find page can
  // offer a "Limit to map area" filter when the user just zoomed in
  // somewhere. Debounced so a fast pan doesn't pound the storage. The
  // shape matches what places.html's bbox filter reads.
  var __vpDebounce = null;
  function __stashViewport() {
    try {
      var b = map.getBounds();
      var v = {
        n: b.getNorth(), s: b.getSouth(),
        e: b.getEast(),  w: b.getWest(),
        z: map.getZoom(),
        ts: Date.now(),
      };
      sessionStorage.setItem('streetzim_last_viewport', JSON.stringify(v));
    } catch (e) {}
  }
  map.on('moveend', function() {
    clearTimeout(__vpDebounce);
    __vpDebounce = setTimeout(__stashViewport, 200);
  });
  // Also stash once at load — covers the case where the user opens
  // the find page without ever moving the map.
  map.once('idle', __stashViewport);

  // Shared search API — used by routing panel's typeahead inputs so we
  // don't duplicate the manifest/chunk/scoring infrastructure. Returns
  // a Promise resolving to scored matches: [{item:{n,t,a,o,...}, score, dist}].
  map._queryPlaces = function(query) {
    if (!manifest || !query || query.length < 2) return Promise.resolve([]);
    var prefixes = getPrefixes(query);
    if (!prefixes.length) return Promise.resolve([]);
    // Stream + filter — same OOM-avoidance as the inline typeahead
    // above. Used by the routing-panel typeahead.
    return streamFilterChunks(prefixes, query).then(function(matches) {
      var scored = scoreResults(matches, query);
      // Same few-results fallback as the main search box: targeting can
      // miss mid-word matches, and a typeahead that finds nothing is worse
      // than one that takes a moment longer.
      if (scored.length < 5 && prefixes.targeted) {
        return streamFilterChunks(getPrefixes(query, { noTargeting: true }), query)
          .then(function(more) { return scoreResults(more, query); })
          // The routing typeahead awaits this; a rejection must not take the
          // panel down, so fall back to what targeting found.
          .catch(function() { return scored; });
      }
      return scored;
    });
  };
}

