// --- Search functionality ---
// Uses chunked index: search-data/manifest.json lists available prefix chunks,
// and search-data/{prefix}.json files are loaded on demand. This keeps RAM
// bounded even for world-scale datasets (millions of features).
var searchMarker = null;

// BEGIN proximity-label
// The distance after a search result ("Casino  grocery · Jardin Exotique ·
// 1 mi"), in the units the scale bar shows (map._streetzimUnit, switched by
// clicking the scale). Unit symbols are lower case -- mi, km -- and the
// line is no longer text-transform: capitalize (010), which printed "1 Mi".
function _szProximityLabel(miles, unit) {
  if (!(miles >= 0)) return '';
  if (unit === 'metric') {
    var km = miles * 1.609344;
    if (km < 0.8) return szT('search.nearby', 'nearby');
    if (km < 8) return Math.round(km) + ' km';
    if (km < 80) return Math.round(km / 5) * 5 + ' km';
    if (km < 800) return Math.round(km / 10) * 10 + ' km';
    return Math.round(km / 100) * 100 + ' km';
  }
  if (miles < 0.5) return szT('search.nearby', 'nearby');
  if (miles < 5) return Math.round(miles) + ' mi';
  if (miles < 50) return Math.round(miles / 5) * 5 + ' mi';
  if (miles < 500) return Math.round(miles / 10) * 10 + ' mi';
  return Math.round(miles / 100) * 100 + ' mi';
}
// END proximity-label

// BEGIN admin-search
// Administrative areas (records with t: 'admin', docs/search-records.md):
// countries, states, counties, cities, wards, from OSM boundary relations.
// They answer to their other names (`alt`) and to their name with their type
// ("Alexandria city", "City of Alexandria"), rank above a POI or place of the
// same name by admin_level, and a pick fits the area's box (`bb`).
// Pure functions, so tests/viewer_search_js.test.mjs runs them as they ship.
var SZ_ADMIN_CONNECTORS = { of: 1, de: 1, du: 1, des: 1, del: 1, di: 1, la: 1, le: 1, the: 1 };
var SZ_ADMIN_LEVEL_BONUS = { 2: 200, 3: 150, 4: 120, 5: 100, 6: 80, 7: 60, 8: 40, 9: 15, 10: 10 };
var SZ_ADMIN_ZOOM = { 2: 5, 3: 6, 4: 7, 5: 8, 6: 10, 7: 11, 8: 12, 9: 13, 10: 14 };
var SZ_TYPE_BONUS = { admin: 25, place: 20, airport: 15, peak: 10, park: 10, water: 5, poi: 5, building: 3, street: 0 };
// Cities and counties carry far more public relevance than a same-named
// neighbourhood or hamlet, but they're often FAR from the user's current
// view (San Diego when typing "san" in SF) and would otherwise be crushed by
// the proximity multiplier. A sizable subtype bonus lifts them above local
// non-place hits even at 1x prox.
var SZ_PLACE_SUB_BONUS = { city: 200, county: 80, region: 60, town: 30,
                           suburb: 10, village: 8, neighbourhood: 4 };

// A record's names (cloud/search_shards.record_names): its name, its name
// in its own script `nn` ("北京大学" beside "Peking University"), and an
// admin area's other names.
function _szRecordNames(rec) {
  var names = [rec.n];
  if (typeof rec.nn === 'string' && rec.nn) names.push(rec.nn);
  // A build in another language: the English / Latin name too.
  if (typeof rec.nl === 'string' && rec.nl) names.push(rec.nl);
  return Array.isArray(rec.alt) ? names.concat(rec.alt) : names;
}

// The normalized texts a record answers to: its names; for an admin area
// also "<name> <type>" and "<type> of <name>".
function _szSearchForms(rec, norm) {
  if (rec.t !== 'admin') {
    var plain = [norm(String(rec.n || ''))];
    if (typeof rec.nn === 'string' && rec.nn) plain.push(norm(rec.nn));
    if (typeof rec.nl === 'string' && rec.nl) plain.push(norm(rec.nl));
    return plain;
  }
  var names = _szRecordNames(rec);
  var label = rec.s ? norm(String(rec.s)) : '';
  var out = [];
  for (var i = 0; i < names.length; i++) {
    var h = norm(String(names[i] || ''));
    if (!h) continue;
    out.push(h);
    if (label) { out.push(h + ' ' + label); out.push(label + ' of ' + h); }
  }
  return out;
}

// Score of the query words against one form, or -1: every word must
// appear (+11 at a word start, +1 inside one). An admin area may skip the
// connector words, but must match at least one other word.
function _szWordsScore(name, words, admin) {
  var textScore = 0, real = 0;
  for (var w = 0; w < words.length; w++) {
    var pos = name.indexOf(words[w]);
    if (pos === -1) {
      if (admin && SZ_ADMIN_CONNECTORS[words[w]]) continue;
      return -1;
    }
    if (!(admin && SZ_ADMIN_CONNECTORS[words[w]])) real++;
    if (pos === 0 || name[pos - 1] === ' ') textScore += 10;
    textScore += 1;
  }
  return (admin && !real) ? -1 : textScore;
}

// The streaming filter: could the record match? Other records: the name
// contains the whole query (as before). An admin area: _szTextScore's own
// rule on any of its forms, so the filter never drops what it would rank.
function _szFormsContain(rec, qNorm, norm) {
  if (!qNorm) return true;
  if (rec.t !== 'admin') {
    var plainForms = _szSearchForms(rec, norm);
    for (var pf = 0; pf < plainForms.length; pf++) {
      if (plainForms[pf].indexOf(qNorm) >= 0) return true;
    }
    return false;
  }
  var words = qNorm.split(/\s+/).filter(function(w) { return w; });
  var forms = _szSearchForms(rec, norm);
  for (var i = 0; i < forms.length; i++) if (_szWordsScore(forms[i], words, true) >= 0) return true;
  return false;
}

// Text score before proximity, or -1 when the record does not match: every
// query word must appear in the name (an admin area may skip "of", "de", ...).
function _szTextScore(item, q, words, norm) {
  var forms = _szSearchForms(item, norm);
  var admin = item.t === 'admin';
  var best = -1;
  for (var f = 0; f < forms.length; f++) {
    var name = forms[f];
    var textScore = _szWordsScore(name, words, admin);
    if (textScore < 0) continue;
    if (name === q) textScore += 50;
    if (name.indexOf(q) === 0) textScore += 25;
    if (textScore > best) best = textScore;
  }
  if (best < 0) return -1;
  best += SZ_TYPE_BONUS[item.t] || 0;
  if (item.t === 'place' && item.s) best += SZ_PLACE_SUB_BONUS[item.s] || 0;
  if (admin) best += SZ_ADMIN_LEVEL_BONUS[item.al] || 0;
  return best;
}

// Where a pick of an admin area goes: its box when it has a sane one, else
// its point at a zoom for its level.
function _szAdminCamera(item) {
  var bb = item && item.bb;
  if (Array.isArray(bb) && bb.length === 4) {
    var ok = true;
    for (var i = 0; i < 4; i++) if (typeof bb[i] !== 'number' || !isFinite(bb[i])) ok = false;
    if (ok && bb[0] <= bb[2] && bb[1] <= bb[3] && bb[1] >= -90 && bb[3] <= 90 &&
        bb[0] >= -180 && bb[2] <= 540) {
      return { bounds: [[bb[0], bb[1]], [bb[2], bb[3]]] };
    }
  }
  return { zoom: SZ_ADMIN_ZOOM[item && item.al] || 12 };
}

// The name|lat|lon keys of the administrative-area records among
// `entries`. An area is often placed at its label node, which is also a
// place record with the same name and point (Monaco the country and
// Monaco the place): the search keeps the area's record, with its box,
// and drops the place record with the same key, whichever comes first.
function _szAdminKeys(entries) {
  var keys = {};
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    if (e && e.t === 'admin') keys[e.n + '|' + e.a + '|' + e.o] = true;
  }
  return keys;
}
// END admin-search

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
  var box = _szPlacePopupDOM(lat, lon, name, enrich);
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

// The body of a place popup: name, brand, category, contact links and
// "Directions to here" (when the ZIM has routing). Used by search-result
// pins and by labels tapped on the map (initWikidataPopups).
function _szPlacePopupDOM(lat, lon, name, enrich) {
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
    ct.textContent = szPlaceType(cat);
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
  if (Array.isArray(enrich.soc)) {
    enrich.soc.forEach(function(s) {
      // Same rule as the website: http(s) links only.
      if (typeof s !== 'string' || !/^https?:\/\//i.test(s.trim())) return;
      s = s.trim();
      var host = s.toLowerCase();
      var g = /facebook/.test(host) ? 'f' :
              /instagram/.test(host) ? 'IG' :
              /twitter|x\.com/.test(host) ? '𝕏' :
              /tiktok/.test(host) ? 'TT' : '•';
      addLink(s, g, s);
    });
  }
  if (any) box.appendChild(contact);
  if (!window.streetzimRouting ||
      typeof window.streetzimRouting.open !== 'function') {
    return box;
  }

  // NOTE: see project_directions_button_duplicated.md memory.
  // This is the SEARCH-PIN copy of the Directions button. There's
  // a near-identical sibling in initWikidataPopups
  // (~line 4382). Any click-handler change MUST be applied to
  // BOTH or only one popup-source gets the new behaviour.
  var btn = document.createElement('button');
  btn.className = 'pin-directions';
  btn.textContent = szT('popup.directions_to_here', 'Directions to here');
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
      window.streetzimRouting.setOrigin(cached.lat, cached.lon, SZ_HERE);
    } else if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        function(pos) {
          window.__streetzimLastLoc = {
            lat: pos.coords.latitude, lon: pos.coords.longitude, ts: Date.now()
          };
          window.streetzimRouting.setOrigin(
            pos.coords.latitude, pos.coords.longitude, SZ_HERE);
        },
        function() {},
        { enableHighAccuracy: false, maximumAge: 60000, timeout: 8000 });
    }
  });
  box.appendChild(btn);
  return box;
}

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
      input.placeholder = szTn('search.placeholder_count', data.total,
        { one: 'Search {n} places...', other: 'Search {n} places...' }, { n: szLocaleNum(data.total) });
      // State for the smoke gates, which must not read the (translatable)
      // placeholder: ready / unavailable.
      input.setAttribute('data-sz-search', 'ready');
    })
    .catch(function() {
      input.placeholder = szT('search.unavailable', 'Search unavailable');
      input.setAttribute('data-sz-search', 'unavailable');
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

  // Lowercase + strip diacritics — so "cafe" matches "Café" and "sao" matches
  // "São" — exactly as the writer folded the index (SEARCH_SHARDS.fold below).
  function normalizeText(s) {
    return SEARCH_SHARDS.fold(s);
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

  // The search fold: NFKD, drop every character whose canonical combining
  // class is not 0, lowercase -- exactly cloud/search_shards.py `norm`, which
  // wrote every published index. Names, queries and the prefix keys and
  // leaf paths computed from them all go through it. NOT /\p{M}/: that also
  // drops Indic vowel signs and Thai vowels (marks of class 0), so
  // "कोलकाता" became "कलकत" here and "कोलकाता" in the index, and the reader
  // asked for leaves the writer never wrote.
  // BEGIN combining-marks (generated by tools/gen_combining_marks.py; do not edit)
  // Unicode 16.0.0: every code point whose canonical combining class
  // is not 0, as [first, last] pairs -- what the writer's
  // unicodedata.combining() drops.
  var CCC_RANGES = [
    0x0300, 0x034E, 0x0350, 0x036F, 0x0483, 0x0487, 0x0591, 0x05BD, 0x05BF, 0x05BF, 0x05C1, 0x05C2,
    0x05C4, 0x05C5, 0x05C7, 0x05C7, 0x0610, 0x061A, 0x064B, 0x065F, 0x0670, 0x0670, 0x06D6, 0x06DC,
    0x06DF, 0x06E4, 0x06E7, 0x06E8, 0x06EA, 0x06ED, 0x0711, 0x0711, 0x0730, 0x074A, 0x07EB, 0x07F3,
    0x07FD, 0x07FD, 0x0816, 0x0819, 0x081B, 0x0823, 0x0825, 0x0827, 0x0829, 0x082D, 0x0859, 0x085B,
    0x0897, 0x089F, 0x08CA, 0x08E1, 0x08E3, 0x08FF, 0x093C, 0x093C, 0x094D, 0x094D, 0x0951, 0x0954,
    0x09BC, 0x09BC, 0x09CD, 0x09CD, 0x09FE, 0x09FE, 0x0A3C, 0x0A3C, 0x0A4D, 0x0A4D, 0x0ABC, 0x0ABC,
    0x0ACD, 0x0ACD, 0x0B3C, 0x0B3C, 0x0B4D, 0x0B4D, 0x0BCD, 0x0BCD, 0x0C3C, 0x0C3C, 0x0C4D, 0x0C4D,
    0x0C55, 0x0C56, 0x0CBC, 0x0CBC, 0x0CCD, 0x0CCD, 0x0D3B, 0x0D3C, 0x0D4D, 0x0D4D, 0x0DCA, 0x0DCA,
    0x0E38, 0x0E3A, 0x0E48, 0x0E4B, 0x0EB8, 0x0EBA, 0x0EC8, 0x0ECB, 0x0F18, 0x0F19, 0x0F35, 0x0F35,
    0x0F37, 0x0F37, 0x0F39, 0x0F39, 0x0F71, 0x0F72, 0x0F74, 0x0F74, 0x0F7A, 0x0F7D, 0x0F80, 0x0F80,
    0x0F82, 0x0F84, 0x0F86, 0x0F87, 0x0FC6, 0x0FC6, 0x1037, 0x1037, 0x1039, 0x103A, 0x108D, 0x108D,
    0x135D, 0x135F, 0x1714, 0x1715, 0x1734, 0x1734, 0x17D2, 0x17D2, 0x17DD, 0x17DD, 0x18A9, 0x18A9,
    0x1939, 0x193B, 0x1A17, 0x1A18, 0x1A60, 0x1A60, 0x1A75, 0x1A7C, 0x1A7F, 0x1A7F, 0x1AB0, 0x1ABD,
    0x1ABF, 0x1ACE, 0x1B34, 0x1B34, 0x1B44, 0x1B44, 0x1B6B, 0x1B73, 0x1BAA, 0x1BAB, 0x1BE6, 0x1BE6,
    0x1BF2, 0x1BF3, 0x1C37, 0x1C37, 0x1CD0, 0x1CD2, 0x1CD4, 0x1CE0, 0x1CE2, 0x1CE8, 0x1CED, 0x1CED,
    0x1CF4, 0x1CF4, 0x1CF8, 0x1CF9, 0x1DC0, 0x1DFF, 0x20D0, 0x20DC, 0x20E1, 0x20E1, 0x20E5, 0x20F0,
    0x2CEF, 0x2CF1, 0x2D7F, 0x2D7F, 0x2DE0, 0x2DFF, 0x302A, 0x302F, 0x3099, 0x309A, 0xA66F, 0xA66F,
    0xA674, 0xA67D, 0xA69E, 0xA69F, 0xA6F0, 0xA6F1, 0xA806, 0xA806, 0xA82C, 0xA82C, 0xA8C4, 0xA8C4,
    0xA8E0, 0xA8F1, 0xA92B, 0xA92D, 0xA953, 0xA953, 0xA9B3, 0xA9B3, 0xA9C0, 0xA9C0, 0xAAB0, 0xAAB0,
    0xAAB2, 0xAAB4, 0xAAB7, 0xAAB8, 0xAABE, 0xAABF, 0xAAC1, 0xAAC1, 0xAAF6, 0xAAF6, 0xABED, 0xABED,
    0xFB1E, 0xFB1E, 0xFE20, 0xFE2F, 0x101FD, 0x101FD, 0x102E0, 0x102E0, 0x10376, 0x1037A, 0x10A0D, 0x10A0D,
    0x10A0F, 0x10A0F, 0x10A38, 0x10A3A, 0x10A3F, 0x10A3F, 0x10AE5, 0x10AE6, 0x10D24, 0x10D27, 0x10D69, 0x10D6D,
    0x10EAB, 0x10EAC, 0x10EFD, 0x10EFF, 0x10F46, 0x10F50, 0x10F82, 0x10F85, 0x11046, 0x11046, 0x11070, 0x11070,
    0x1107F, 0x1107F, 0x110B9, 0x110BA, 0x11100, 0x11102, 0x11133, 0x11134, 0x11173, 0x11173, 0x111C0, 0x111C0,
    0x111CA, 0x111CA, 0x11235, 0x11236, 0x112E9, 0x112EA, 0x1133B, 0x1133C, 0x1134D, 0x1134D, 0x11366, 0x1136C,
    0x11370, 0x11374, 0x113CE, 0x113D0, 0x11442, 0x11442, 0x11446, 0x11446, 0x1145E, 0x1145E, 0x114C2, 0x114C3,
    0x115BF, 0x115C0, 0x1163F, 0x1163F, 0x116B6, 0x116B7, 0x1172B, 0x1172B, 0x11839, 0x1183A, 0x1193D, 0x1193E,
    0x11943, 0x11943, 0x119E0, 0x119E0, 0x11A34, 0x11A34, 0x11A47, 0x11A47, 0x11A99, 0x11A99, 0x11C3F, 0x11C3F,
    0x11D42, 0x11D42, 0x11D44, 0x11D45, 0x11D97, 0x11D97, 0x11F41, 0x11F42, 0x1612F, 0x1612F, 0x16AF0, 0x16AF4,
    0x16B30, 0x16B36, 0x16FF0, 0x16FF1, 0x1BC9E, 0x1BC9E, 0x1D165, 0x1D169, 0x1D16D, 0x1D172, 0x1D17B, 0x1D182,
    0x1D185, 0x1D18B, 0x1D1AA, 0x1D1AD, 0x1D242, 0x1D244, 0x1E000, 0x1E006, 0x1E008, 0x1E018, 0x1E01B, 0x1E021,
    0x1E023, 0x1E024, 0x1E026, 0x1E02A, 0x1E08F, 0x1E08F, 0x1E130, 0x1E136, 0x1E2AE, 0x1E2AE, 0x1E2EC, 0x1E2EF,
    0x1E4EC, 0x1E4EF, 0x1E5EE, 0x1E5EF, 0x1E8D0, 0x1E8D6, 0x1E944, 0x1E94A
  ];
  // END combining-marks
  var COMBINING = (function () {
    var cls = '';
    for (var i = 0; i < CCC_RANGES.length; i += 2) {
      cls += '\\u{' + CCC_RANGES[i].toString(16) + '}-\\u{' + CCC_RANGES[i + 1].toString(16) + '}';
    }
    try { return new RegExp('[' + cls + ']', 'gu'); } catch (_) { return null; }
  })();

  function fold(s) {
    var t = String(s == null ? '' : s);
    if (t.normalize && COMBINING) {
      try { t = t.normalize('NFKD').replace(COMBINING, ''); } catch (_) {}
    }
    return t.toLowerCase();
  }

  // The word rule: how the writer split a folded name into the words it
  // keyed and pathed (cloud/search_shards.py `words`), from the manifest's
  // "word_rule"; absent means rule 1, which every older ZIM was written with.
  //   1: a word is a run of letters and digits; any mark ends it, so
  //      "कोलकाता" was indexed as "क" "लक" "त" -- kept exactly so those
  //      ZIMs find what they hold.
  //   2: marks (\p{M}: Indic vowel signs, Thai vowels) continue a word but
  //      never start one. /[\p{L}\p{M}\p{N}]/u is the writer's
  //      isalnum()-or-category-M with "_" excluded, code point for code point
  //      (tests/search_word_rule_js.test.mjs).
  // Returns the words of 2+ characters, the ones the writer indexed; rule 2
  // counts code points as Python does.
  function wordRule(manifest) {
    return (manifest && manifest.word_rule === 2) ? 2 : 1;
  }
  function words(folded, manifest) {
    var t = String(folded == null ? '' : folded);
    if (wordRule(manifest) !== 2) {
      return t.split(/[^\p{L}\p{N}]+/u).filter(function (w) { return w.length >= 2; });
    }
    var parts = t.split(/[^\p{L}\p{M}\p{N}]+/u);
    var out = [];
    for (var i = 0; i < parts.length; i++) {
      var w = parts[i].replace(/^\p{M}+/u, '');
      if (Array.from(w).length >= 2) out.push(w);
    }
    return out;
  }

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

  // The code point a typed token stands for ('u<hex>', or one ASCII
  // character); null for the terminal.
  function tokenCp(tok) {
    if (tok.length === 1) return tok.charCodeAt(0);
    if (tok.charAt(0) === 'u') {
      var n = parseInt(tok.slice(1), 16);
      return isNaN(n) ? null : n;
    }
    return null;
  }

  // A declared token covers a typed one when they are equal, or when it is
  // a range 'r<lo>.<hi>' (hex code points) holding the typed token's code
  // point: small siblings share one leaf (cloud/search_shards.py
  // GROUP_BYTES), listed under the manifest's char_ranges.
  // Exactly cloud/search_shards.py RANGE_RE (lowercase hex only, so
  // parseInt never reads a prefix of a malformed bound).
  var RANGE_RE = /^r([0-9a-f]+)\.([0-9a-f]+)$/;
  function tokenMatches(decl, typed) {
    if (decl === typed) return true;
    var m = RANGE_RE.exec(decl);
    if (!m) return false;
    var c = tokenCp(typed);
    if (c === null) return false;
    return parseInt(m[1], 16) <= c && c <= parseInt(m[2], 16);
  }

  // a, b: token arrays; a's tokens are declared ones when declaredFirst,
  // else b's are.
  function isPrefixOf(a, b, declaredFirst) {
    if (a.length > b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (!(declaredFirst ? tokenMatches(a[i], b[i]) : tokenMatches(b[i], a[i]))) return false;
    }
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
      if (isPrefixOf(p, typed, true) || isPrefixOf(typed, p, false)) out.push(declared[i]);
    }
    return out;
  }

  // Leaf names for one prefix, tier by tier, in fetch order. Returns null
  // when this ZIM has no character split for the prefix (caller keeps the
  // old whole-prefix behaviour), or [] when the typed characters exist in
  // no path at all — which means no records, not "fetch everything".
  // The declared paths of a character-split prefix: char_split, or
  // char_ranges when its plan groups siblings (an older viewer ignores that
  // key and reads the prefix whole -- slow, never wrong).
  function splitPaths(manifest, prefix) {
    if (!manifest) return null;
    return (manifest.char_split && manifest.char_split[prefix])
      || (manifest.char_ranges && manifest.char_ranges[prefix]) || null;
  }

  function leavesFor(manifest, prefix, word, query, resolve) {
    var cs = splitPaths(manifest, prefix);
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
    fold: fold,
    wordRule: wordRule,
    words: words,
    tokenFor: tokenFor,
    pathTokens: pathTokens,
    pathsFor: pathsFor,
    splitPaths: splitPaths,
    tokenMatches: tokenMatches,
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
                  if (qNorm && !_szFormsContain(rec, qNorm, normalizeText)) continue;
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
    // The words the writer keyed, split by the ZIM's word rule: >= 2 chars,
    // as the writer (`prefixes_for` skips shorter words) and places.html.
    // Keeping 1-char words made "a coffee" expand the whole "a_" prefix for
    // nothing.
    var words = SEARCH_SHARDS.words(q, manifest);

    function keyFor(word) {
      // Latin-leading words keep the 2-char ASCII-alnum prefix. Non-ASCII
      // first chars (CJK, Cyrillic, Arabic, Thai, …) bucket into
      // 'u' + lowercase hex of their codepoint. Must match the writer
      // ``prefix_key`` in cloud/search_shards.py and the Swift
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

    if (!(opts && opts.wholeNameOnly)) {
      for (var i = 0; i < words.length; i++) addFor(words[i]);
    }
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

    var adminKeys = _szAdminKeys(entries);   // an area beats a place at its point
    for (var i = 0; i < entries.length; i++) {
      var item = entries[i];
      // Dedup: same entry may appear in multiple word-keyed chunks.
      var dedupKey = item.n + '|' + item.a + '|' + item.o;
      if (seenKeys[dedupKey]) continue;
      if (item.t !== 'admin' && adminKeys[dedupKey]) continue;
      seenKeys[dedupKey] = true;

      var textScore = _szTextScore(item, q, words, normalizeText);
      if (textScore < 0) continue;

      // Proximity is the PRIMARY signal — multiply text score by proximity factor
      // This means a mediocre text match nearby always beats a perfect match far away
      var proximityLabel = '';
      var score = textScore; // fallback if no coordinates
      if (item.a !== undefined && item.o !== undefined) {
        var dlat = item.a - clat;
        // Scale longitude by cos(lat) so the degree-space distance is
        // isotropic; raw Δlng overstated E–W distances 2x at 60°N.
        // The short way round, for maps across the antimeridian.
        var dlng0 = item.o - clng;
        dlng0 -= 360 * Math.round(dlng0 / 360);
        var dlng = dlng0 * Math.cos(clat * Math.PI / 180);
        var dist = Math.sqrt(dlat * dlat + dlng * dlng);
        var viewSpan = Math.max(
          bounds.getNorth() - bounds.getSouth(),
          bounds.getEast() - bounds.getWest()
        );
        var ratio = dist / (viewSpan || 1);

        // Proximity multiplier: on-screen = 10x, few viewports = 5x, far = 1x
        var proxMultiplier = 1 + 9 * Math.exp(-ratio * 0.5);
        score = textScore * proxMultiplier;

        // Human-readable distance, in the scale bar's units.
        proximityLabel = _szProximityLabel(dist * 69, map._streetzimUnit);
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
      resultsEl.innerHTML = '<div class="search-no-results"></div>';
      resultsEl.firstChild.textContent = szT('search.no_results', 'No results found');
      resultsEl.style.display = 'block';
      return;
    }
    var html = '';
    for (var j = 0; j < matches.length; j++) {
      var m = matches[j].item;
      // Display label: Overture's clean category (`cat`) when present,
      // otherwise fall back to the OMT subtype.
      var rawLabel = m.cat || m.s || m.t;
      var label = rawLabel ? szPlaceType(rawLabel) : '';
      var loc = m.l ? ' &middot; ' + escapeHtml(m.l) : '';
      var distLabel = matches[j].dist ? ' &middot; ' + escapeHtml(matches[j].dist) : '';
      html += '<div class="search-result" data-idx="' + j +
        '" data-lat="' + m.a + '" data-lon="' + m.o + '" data-type="' + m.t +
        '" data-name="' + escapeHtml(m.n) + '">' +
        '<span class="search-result-name">' + escapeHtml(m.n) +
        (m.nn ? ' <span class="search-result-native">' + escapeHtml(m.nn) + '</span>' : '') +
        '</span>' +
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
      loadingRow.setAttribute('data-sz-pending', '1');  // not a result (gates)
      loadingRow.style.pointerEvents = 'none';
      loadingRow.style.opacity = '0.7';
      loadingRow.textContent = szT('search.searching', 'Searching…');
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
    var adminCam = (item && item.t === 'admin') ? _szAdminCamera(item) : null;
    if (adminCam && adminCam.zoom) zoom = adminCam.zoom;
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
    if (adminCam && adminCam.bounds) {
      // An area: show all of it, clear of the search box.
      map.fitBounds(adminCam.bounds, { duration: 1500, maxZoom: 16,
        padding: { top: _topChrome + 20, bottom: 30, left: 30, right: 30 } });
    } else {
      _szFlyToClear(map, { center: [lon, lat], zoom: zoom, duration: 1500, offset: [0, _dy] });
    }
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

  // The search record for a place labelled on the map: the record with the
  // same name nearest (lat, lon), within 300 m. Tapped labels use it for
  // the details a search result shows (category, website, phone). Resolves
  // to null when the index has no such record.
  window.__streetzimLookupPlace = function(name, lat, lon) {
    if (!manifest || !name || name.length < 2) return Promise.resolve(null);
    // Only the leaves for the whole name (the writer always indexes it),
    // and no lookup when that is not a targeted read: a name like "De
    // Observant" read 176 leaves (97 MB of JSON) through its words.
    var prefixes = getPrefixes(name, { wholeNameOnly: true });
    if (!prefixes.length || (!prefixes.targeted && prefixes.length > 8)) {
      return Promise.resolve(null);
    }
    var want = normalizeText(name);
    return streamFilterChunks(prefixes, name).then(function(recs) {
      var best = null, bestD = 300;
      for (var i = 0; i < recs.length; i++) {
        var r = recs[i];
        if (!r || typeof r.a !== 'number' || typeof r.o !== 'number') continue;
        if (normalizeText(r.n || '') !== want
            && normalizeText(r.nn || '') !== want
            && normalizeText(r.nl || '') !== want) continue;
        var d = _haversineMetersStrip(lat, lon, r.a, r.o);
        if (d < bestD) { bestD = d; best = r; }
      }
      return best;
    }, function() { return null; });
  };

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

