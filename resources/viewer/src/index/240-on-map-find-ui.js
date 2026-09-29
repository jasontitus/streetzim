
var _findCatManifest = null;
var _findChipCache = new Map();    // chipId -> Promise<Array<record>>
// Geo-sharded chips: bounded LRU of shard fetches instead of whole chips.
var _chipShardCache = CHIP_SHARDS.makeCache(CHIP_SHARDS.budgetBytes());
window.addEventListener('pagehide', function() { _chipShardCache.clear(); });

// BEGIN chip-availability
// Which rail chips THIS ZIM can serve, read from category-index/manifest.json.
// Chip files are only written by --split-find-chips, and every build that
// writes them also lists them under the manifest's `chips` key, so:
//   - manifest unknown (network error)   -> keep every chip, try on tap;
//   - manifest has no `chips` (or no category index at all) -> no chip
//     data: hide the whole rail. It used to show all ten chips, and a tap
//     fetched chip-<id>.json, got a 404 and showed nothing;
//   - a chip whose manifest count is 0 (Parks / Gas on a tilemaker
//     build) -> hide it; the tap had nothing to show.
// Restaurants + Cafés became one "Food & Drink" chip on 2026-09-16, but
// ZIMs built before that ship the old pair and no chip-food.json, so the
// food button stands for either.
var _FIND_CHIP_ALT = { food: ['food', 'restaurants', 'cafes'] };
function _findChipHas(chips, id) {
  var c = chips && chips[id];
  if (!c) return false;
  return !(typeof c.count === 'number' && c.count <= 0);
}
// manifest: the parsed manifest; { _szUnknown: true } when it could not be
// read. ids: the rail's chip ids. -> { rail: bool, show: { id: bool } }
function _findChipsPlan(manifest, ids) {
  var show = {}, i;
  if (!manifest || manifest._szUnknown) {
    for (i = 0; i < ids.length; i++) show[ids[i]] = true;
    return { rail: true, show: show };
  }
  var chips = manifest.chips;
  var known = chips ? Object.keys(chips) : [];
  var shown = 0, recognised = 0;
  for (i = 0; i < ids.length; i++) {
    var alts = _FIND_CHIP_ALT[ids[i]] || [ids[i]];
    var ok = false;
    for (var a = 0; a < alts.length; a++) {
      if (chips && chips[alts[a]]) recognised++;
      if (_findChipHas(chips, alts[a])) ok = true;
    }
    show[ids[i]] = ok;
    if (ok) shown++;
  }
  if (known.length && !recognised) {
    // Unrecognised chip set (ids drifted from chip_rules.py): better all
    // than none — a tap then says what is missing.
    for (i = 0; i < ids.length; i++) show[ids[i]] = true;
    return { rail: true, show: show };
  }
  return { rail: shown > 0, show: show };
}
// END chip-availability

var _szChipSynth = 0;   // ts of the last tap-synthesised chip activation
// A MapLibre popup anchors ABOVE its marker, so on a phone it can open flush
// against — or overlapping — the search box + chip rail, even now that it
// paints above them (z-index 4). Nudge the camera by just the shortfall so
// the card keeps a small gap below the top chrome. Measured on iPhone: the
// card opened with its top edge and close X inside the chip row.
function _szPopupGap(map, popup) {
  if (!map || !popup || typeof popup.on !== 'function') return;
  popup.on('open', function () {
    var el = typeof popup.getElement === 'function' ? popup.getElement() : null;
    if (!el) return;
    // after layout, or getBoundingClientRect reads the pre-paint position
    requestAnimationFrame(function () {
      var sc = document.getElementById('search-container');
      var chromeBottom = (sc && getComputedStyle(sc).display !== 'none')
        ? sc.getBoundingClientRect().bottom : 0;
      var r = el.getBoundingClientRect();
      if (!r.height) return;
      var dy = (chromeBottom + 10) - r.top;   // 10 px of breathing room
      // panBy positive-y moves features UP, so negative pushes the card down.
      if (dy > 2) map.panBy([0, -dy], { duration: 250 });
    });
  });
}
// The rail is built before the category manifest is fetched, so hide what
// this ZIM cannot serve once it arrives (_findChipsPlan above).
function _findChipsReconcile(rail) {
  _findFetchCatManifest().then(function(m) {
    var btns = rail.querySelectorAll('.find-chip');
    var ids = [];
    for (var i = 0; i < btns.length; i++) ids.push(btns[i].dataset.chip);
    var plan = _findChipsPlan(m, ids);
    for (var j = 0; j < btns.length; j++) btns[j].hidden = !plan.show[ids[j]];
    rail.hidden = !plan.rail;
  }).catch(function() { /* keep the full rail */ });
}

function initFindChips(map) {
  var rail = document.getElementById('find-chips');
  if (!rail) return;
  // Build the chip buttons from EXPLORE_CHIPS (single source of
  // truth; mirrors chip_rules.py).
  for (var i = 0; i < EXPLORE_CHIPS.length; i++) {
    (function(c) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'find-chip';
      btn.dataset.chip = c.id;
      var emoji = document.createElement('span');
      emoji.setAttribute('aria-hidden', 'true');
      emoji.textContent = c.emoji;
      var label = document.createElement('span');
      label.textContent = c.label;
      btn.appendChild(emoji);
      btn.appendChild(label);
      // iOS cancels the click when a tap drifts (pointercancel), so the
      // action lives in a named function that BOTH the click listener and
      // the touch-tap fallback below can invoke. _szChipSynth suppresses
      // the double-fire when a real click arrives after a synthetic tap.
      var act = function() {
        loadChipOnMap(map, c).catch(function(err) {
          // A chip tapped before the rail reconciles (or one whose file this
          // ZIM lacks) used to leave the search box reading
          // "Loading food & drink…" for ever, with only a console warning.
          console.warn('[streetzim] chip load failed:', err);
          var si = document.getElementById('search-input');
          if (si && _chipOrigPlaceholder) si.placeholder = _chipOrigPlaceholder;
          _findChipPaintActive(null);
          _showFindToast('Couldn’t load ' + c.label.toLowerCase());
        });
      };
      btn._szAct = act;
      btn.addEventListener('click', function() {
        if (Date.now() - _szChipSynth < 500) return;   // already run by tap
        act();
      });
      rail.appendChild(btn);
    })(EXPLORE_CHIPS[i]);
  }
  // Touch-tap fallback: a tap that drifts a few px is cancelled by iOS
  // before click fires. Track the start point and, on touchend, treat a
  // small, short gesture as a tap on whichever chip it began over.
  // Threshold is deliberately generous (10 px / 600 ms) -- a real swipe
  // moves far further, and scrollLeft reached 590 in the same trace, so
  // this does not eat scrolling.
  (function() {
    var sx = 0, sy = 0, st = 0, startBtn = null;
    rail.addEventListener('touchstart', function(e) {
      var t = e.changedTouches && e.changedTouches[0];
      if (!t) return;
      sx = t.clientX; sy = t.clientY; st = Date.now();
      startBtn = e.target && e.target.closest ? e.target.closest('.find-chip') : null;
    }, { passive: true });
    rail.addEventListener('touchend', function(e) {
      var t = e.changedTouches && e.changedTouches[0];
      if (!t || !startBtn) { startBtn = null; return; }
      var moved = Math.abs(t.clientX - sx) + Math.abs(t.clientY - sy);
      var btn = startBtn; startBtn = null;
      if (moved > 10 || Date.now() - st > 600) return;   // a swipe, not a tap
      if (typeof btn._szAct === 'function') { _szChipSynth = Date.now(); btn._szAct(); }
    }, { passive: true });
  })();
  _findChipsReconcile(rail);
}

function _findChipPaintActive(chipId) {
  var rail = document.getElementById('find-chips');
  if (!rail) return;
  var btns = rail.querySelectorAll('.find-chip');
  for (var i = 0; i < btns.length; i++) {
    btns[i].classList.toggle('on', btns[i].dataset.chip === chipId);
  }
}

// Resolves the manifest; {} when the ZIM has none (HTTP error: no category
// index, so no chips); { _szUnknown: true } when it could not be read at
// all — that one is not cached, so a later tap tries again.
function _findFetchCatManifest() {
  if (_findCatManifest) return Promise.resolve(_findCatManifest);
  return fetch(baseUrl + 'category-index/manifest.json')
    .then(function(r) { return r.ok ? r.json() : {}; })
    .then(function(m) {
      _findCatManifest = (m && typeof m === 'object') ? m : {};
      return _findCatManifest;
    })
    .catch(function() { return { _szUnknown: true }; });
}

// Whole-chip arrays (pre-geo-shard ZIMs) run to 100 MB+ each; keeping
// every chip tapped in a session added up on a phone. Keep the last few.
var _FIND_CHIP_CACHE_MAX = (navigator.deviceMemory && navigator.deviceMemory <= 2) ? 1 : 3;
function _findFetchChipData(chipId) {
  if (_findChipCache.has(chipId)) {
    var hit = _findChipCache.get(chipId);
    _findChipCache.delete(chipId);        // refresh recency
    _findChipCache.set(chipId, hit);
    return hit;
  }
  var p = _findFetchCatManifest().then(function(m) {
    var meta = (m.chips && m.chips[chipId]) || {};
    var subs = Array.isArray(meta.sub_chunks) ? meta.sub_chunks : null;
    if (subs && subs.length) {
      // Fan-out fetch when the build sub-bucketed this chip
      // (cloud/repackage_zim.py --split-find-chips). Concatenate the
      // per-bucket arrays.
      return Promise.all(subs.map(function(suffix) {
        return fetch(baseUrl + 'category-index/chip-' + chipId
                     + '-' + suffix + '.json')
          .then(function(r) { return r.ok ? r.json() : []; })
          .catch(function() { return []; });
      })).then(function(blobs) {
        var out = [];
        for (var i = 0; i < blobs.length; i++) {
          for (var j = 0; j < blobs[i].length; j++) out.push(blobs[i][j]);
        }
        return out;
      });
    }
    return fetch(baseUrl + 'category-index/chip-' + chipId + '.json')
      .then(function(r) {
        if (!r.ok) throw new Error('chip-' + chipId + ' HTTP ' + r.status);
        return r.json();
      });
  });
  _findChipCache.set(chipId, p);
  // A failed load must not occupy a slot.
  p.catch(function() { if (_findChipCache.get(chipId) === p) _findChipCache.delete(chipId); });
  while (_findChipCache.size > _FIND_CHIP_CACHE_MAX) {
    _findChipCache.delete(_findChipCache.keys().next().value);
  }
  return p;
}

// Helper for the "no results" toast — pulled out because both
// `loadChipOnMap` (when called via the Search-this-area pill with
// requireInBounds) and the old `_searchAreaApply` in-memory path
// want the same look.
// Layer id the hillshade should be inserted before: the first layer
// that is neither the background nor a raster (satellite) layer. The
// old positional `layers[1]` broke once the satellite layer had been
// inserted at index 0 — hillshade then landed BELOW `background` and
// was invisible whenever satellite was toggled back off.
function _hillshadeBeforeId(map) {
  var layers = (map.getStyle() && map.getStyle().layers) || [];
  for (var i = 0; i < layers.length; i++) {
    var l = layers[i];
    if (l.type !== 'background' && l.type !== 'raster' && l.type !== 'hillshade') {
      return l.id;
    }
  }
  return undefined;
}

function _showFindToast(text, opts) {
  opts = opts || {};
  var d = document.createElement('div');
  d.style.cssText = (
    'position:fixed; left:50%; top:calc(14px + var(--top-inset, 0px)); transform:translateX(-50%);'
    + 'z-index:1600; padding:9px 18px;'
    // left:50% alone caps a shrink-to-fit box at half the screen, which
    // broke a one-line message into three on a phone.
    + 'width:max-content; max-width:calc(100vw - 32px); box-sizing:border-box;'
    + 'text-align:center;'
    + 'background:rgba(255,255,255,0.95);'
    + 'color:' + (opts.color || '#a33') + ';'
    + 'border:1px solid rgba(170,50,50,0.25); border-radius:999px;'
    + 'font:600 13px/1.2 -apple-system,system-ui,sans-serif;'
    + 'box-shadow:0 4px 14px rgba(0,0,0,0.18); pointer-events:none;'
  );
  d.textContent = text;
  document.body.appendChild(d);
  setTimeout(function() {
    if (d.parentNode) d.parentNode.removeChild(d);
  }, opts.ms || 1800);
}

// Generation counter: tap chip A (slow) then chip B (fast) used to
// leave A's "Loading …" placeholder stuck in the search box and let A's
// late result overwrite B's carousel.
var _chipLoadSeq = 0;
// The search box's ORIGINAL placeholder, captured once. Capturing it
// per call meant a second chip tap saw "Loading a…" as the value to
// restore, and left that text stuck after the first load was
// superseded.
var _chipOrigPlaceholder = null;

// Resolve a tapped chip against the chips THIS ZIM actually has. The rail is
// built from EXPLORE_CHIPS before the category manifest arrives —
// _findChipsReconcile() hides the absentees once it does — so on a slow
// source, notably a ZIM streamed over HTTP by the online preview, "Food &
// Drink" stays clickable for a second or two on a pre-2026-09-16 ZIM that
// ships chip-restaurants.json / chip-cafes.json and no chip-food.json.
// Tapping it then fetched a file that cannot exist and threw
// "chip-food HTTP 404" (seen on central-asia, 2026-09-18). Resolve first,
// across the merge in both directions, and bail quietly when the ZIM has
// nothing to show rather than requesting a 404.
// A manifest that lists no chips means the ZIM has no chip files at all
// (built without --split-find-chips), and a count-0 entry has nothing to
// show: both resolve to null too.
async function _findResolveChipDef(chipDef) {
  var m = await _findFetchCatManifest().catch(function() { return null; });
  if (!m || m._szUnknown) {   // unreadable manifest: try the file anyway
    return { id: chipDef.id, label: chipDef.label, sources: [chipDef.id] };
  }
  var chips = m.chips;
  if (!chips || !Object.keys(chips).length) return null;
  if (_findChipHas(chips, chipDef.id)) {
    return { id: chipDef.id, label: chipDef.label, sources: [chipDef.id] };
  }
  // Food & Drink merged Restaurants + Cafés on 2026-09-16, but a retrofit
  // reshards the chips a ZIM already has rather than re-deriving them from
  // chip_rules.py, so every region retrofitted this round still ships the
  // old pair. Present ONE chip either way: on those ZIMs "Food & Drink"
  // loads both files and merges them, so the catalog reads the same
  // whatever vintage the ZIM is.
  var alias = { food: ['restaurants', 'cafes'],
                restaurants: ['food'], cafes: ['food'] }[chipDef.id] || [];
  var have = [];
  for (var i = 0; i < alias.length; i++) {
    if (_findChipHas(chips, alias[i])) have.push(alias[i]);
  }
  if (!have.length) return null;
  return { id: chipDef.id, label: chipDef.label, sources: have };
}

async function loadChipOnMap(map, chipDef, opts) {
  opts = opts || {};
  var _resolved = await _findResolveChipDef(chipDef);
  if (!_resolved) {
    var _m = _findCatManifest || {};
    var _none = !_m.chips || !Object.keys(_m.chips).length;
    var _btn = document.querySelector('.find-chip[data-chip="' + chipDef.id + '"]');
    if (_btn) _btn.hidden = true;          // the reconcile is about to do this anyway
    if (_none) {
      var _rail = document.getElementById('find-chips');
      if (_rail) _rail.hidden = true;
    }
    _showFindToast(_none ? 'Category search is not available in this map'
                         : 'No ' + chipDef.label.toLowerCase() + ' in this map');
    _findChipPaintActive(null);
    return;
  }
  chipDef = _resolved;
  var mySeq = ++_chipLoadSeq;
  _findChipPaintActive(chipDef.id);
  // Stash the active chip on the find state so "Search this area"
  // (the pill that appears when the user pans the map) can re-fetch
  // the chip's FULL dataset instead of just re-filtering the
  // already-loaded result set. Without this, panning from SF to
  // LA and clicking the pill kept showing SF restaurants because
  // the items array only had SF-area data.
  _findResultsState.activeChip = chipDef;
  // Show progress in the search input as a placeholder so the user
  // sees "loading" for the chip they tapped without a separate
  // status surface.
  var searchInput = document.getElementById('search-input');
  // Refresh the remembered placeholder whenever the current one is a
  // real one (not our own "Loading …"): the search manifest rewrites it
  // asynchronously ("Search N places…"), so capturing only once could
  // pin the boot-time text forever.
  if (searchInput && !/^Loading /.test(searchInput.placeholder || '')) {
    _chipOrigPlaceholder = searchInput.placeholder || '';
  }
  if (searchInput) {
    searchInput.placeholder = 'Loading ' + chipDef.label.toLowerCase() + '…';
  }
  function restorePlaceholder() {
    if (searchInput && _chipOrigPlaceholder != null) {
      searchInput.placeholder = _chipOrigPlaceholder;
    }
  }
  var catManifest = await _findFetchCatManifest();
  if (mySeq !== _chipLoadSeq) return;
  var _sources = chipDef.sources || [chipDef.id];
  var items, expanded;
  var data = null;             // chip records for the shared downstream path
  var _mergedPath = false;     // true once a multi-source merge has filled `data`
  if (_sources.length > 1) {
    // Merged chip (Food & Drink over a pre-merge ZIM's restaurants+cafes).
    // Each source is queried through its own layout and the results are
    // concatenated, then the shared downstream code below filters to the
    // viewport and caps at 300. Note this is a merge of two k-nearest
    // queries, not one exact kNN over the union: where both sources are
    // dense, the true 300th-nearest can sit just past one source's cut.
    // Exact would mean one shard walk across both chips, which needs every
    // shard index mapped back to its origin chip inside fetchShard — more
    // ways to be silently wrong than this is worth.
    var _merged = [];
    var _mFailed = false, _mPartial = false;
    for (var _si = 0; _si < _sources.length; _si++) {
      var _sid = _sources[_si];
      var _sMeta = (catManifest.chips && catManifest.chips[_sid]) || null;
      try {
        if (CHIP_SHARDS.isGeo(_sMeta)) {
          var _vb = map.getBounds(), _ctr = map.getCenter().wrap();
          var _r = await CHIP_SHARDS.load(_sMeta, {
            fetchShard: (function(sid) {
              return function(suffix) {
                return fetch(baseUrl + 'category-index/chip-' + sid + '-' + suffix + '.json')
                  .then(function(r) {
                    if (!r.ok) throw new Error('chip shard HTTP ' + r.status);
                    return r.json();
                  });
              };
            })(_sid),
            cache: _chipShardCache,
            cacheKey: _sid,
            point: { lat: _ctr.lat, lon: _ctr.lng },
            bbox: { s: _vb.getSouth(), w: _vb.getWest(),
                    n: _vb.getNorth(), e: _vb.getEast() },
            k: 300,
            isCancelled: function() { return mySeq !== _chipLoadSeq; },
            fallbackToNearest: !opts.requireInBounds
          });
          if (mySeq !== _chipLoadSeq) return;
          if (_r) {
            _merged = _merged.concat(_r.records);
            if (_r.partial) _mPartial = true;
            if (_r.failed) _mFailed = true;
          }
        } else {
          var _d = await _findFetchChipData(_sid);
          if (mySeq !== _chipLoadSeq) return;
          if (Array.isArray(_d)) _merged = _merged.concat(_d);
        }
      } catch (_e) {
        console.warn('[streetzim] chip source failed:', _sid, _e);
        _mFailed = true;
      }
    }
    if (mySeq !== _chipLoadSeq) return;
    restorePlaceholder();
    if (!_merged.length) {
      _showFindToast(_mFailed
        ? 'Couldn\u2019t load ' + chipDef.label.toLowerCase()
        : 'No ' + chipDef.label.toLowerCase() + ' in this map');
      _findChipPaintActive(null);
      return;
    }
    if (_mPartial) {
      _showFindToast('Showing the nearest \u2014 zoom in for more',
                     { color: '#333', ms: 2600 });
    }
    data = _merged;
    _mergedPath = true;
  }
  var chipMeta = (catManifest.chips && catManifest.chips[chipDef.id]) || null;
  if (!_mergedPath && CHIP_SHARDS.isGeo(chipMeta)) {
    // Geographic shards: load only what the 300 nearest records around
    // the map centre need, scoped to the viewport.
    var vb = map.getBounds();
    var ctr = map.getCenter().wrap();
    var res;
    try {
    res = await CHIP_SHARDS.load(chipMeta, {
      fetchShard: function(suffix) {
        return fetch(baseUrl + 'category-index/chip-' + chipDef.id + '-' + suffix + '.json')
          .then(function(r) {
            if (!r.ok) throw new Error('chip shard HTTP ' + r.status);
            return r.json();
          });
      },
      cache: _chipShardCache,
      cacheKey: chipDef.id,
      point: { lat: ctr.lat, lon: ctr.lng },
      bbox: { s: vb.getSouth(), w: vb.getWest(), n: vb.getNorth(), e: vb.getEast() },
      k: 300,
      isCancelled: function() { return mySeq !== _chipLoadSeq; },
      fallbackToNearest: !opts.requireInBounds
    });
    } catch (err) {
      console.warn('[streetzim] chip shard load failed:', err);
      if (mySeq !== _chipLoadSeq) return;
      restorePlaceholder();
      _findChipPaintActive(null);
      _showFindToast('Couldn\u2019t load ' + chipDef.label.toLowerCase());
      return;
    }
    if (!res || mySeq !== _chipLoadSeq) return;  // superseded while loading
    restorePlaceholder();
    if (!res.records.length) {
      if (res.failed) {
        _showFindToast('Couldn’t load ' + chipDef.label.toLowerCase());
        if (!opts.requireInBounds) _findChipPaintActive(null);
      } else if (opts.requireInBounds) {
        _showFindToast('No ' + chipDef.label.toLowerCase() + ' in this area');
      } else {
        _showFindToast('No ' + chipDef.label.toLowerCase() + ' in this map');
        _findChipPaintActive(null);
      }
      return;
    }
    if (res.partial) {
      _showFindToast('Showing the nearest ' + res.records.length
        + ' — zoom in for more', { color: '#333', ms: 2600 });
    }
    items = res.records;
    expanded = res.fellBack;
  } else {
  try {
    // Already filled by the multi-source merge above — re-fetching here
    // would throw the merged records away and ask for chip-food.json,
    // which is the file this whole path exists to avoid.
    if (!_mergedPath) data = await _findFetchChipData(chipDef.id);
  } catch (err) {
    console.warn('[streetzim] chip fetch failed:', err);
    if (mySeq !== _chipLoadSeq) return;  // a newer chip owns the UI now
    restorePlaceholder();
    _findChipPaintActive(null);
    _showFindToast('Couldn\u2019t load ' + chipDef.label.toLowerCase());
    return;
  }
  if (mySeq !== _chipLoadSeq) return;  // superseded while loading
  restorePlaceholder();
  // An empty chip used to render an empty carousel with no word said.
  if (!Array.isArray(data) || !data.length) {
    _showFindToast('No ' + chipDef.label.toLowerCase()
      + (opts.requireInBounds ? ' in this area' : ' in this map'));
    if (!opts.requireInBounds) _findChipPaintActive(null);
    return;
  }

  // Filter to the current map viewport. If that wipes everything,
  // fall back to the unfiltered set (matches places.html behaviour
  // — better to surface what's around than show "no results").
  var bounds = map.getBounds();
  var n = bounds.getNorth(), s = bounds.getSouth();
  var e = bounds.getEast(),  w = bounds.getWest();
  // getBounds() returns unwrapped longitudes across the antimeridian
  // (west=170, east=190) while records store [-180, 180] — test the
  // record shifted by ±360° too, or a viewport over Fiji rejects
  // every record with a negative longitude.
  function lonInside(lon) {
    if (w <= e) {
      return (lon >= w && lon <= e)
          || (lon + 360 >= w && lon + 360 <= e)
          || (lon - 360 >= w && lon - 360 <= e);
    }
    return lon >= w || lon <= e;
  }
  var filtered = [];
  for (var i = 0; i < data.length; i++) {
    var r = data[i];
    if (typeof r.a !== 'number' || typeof r.o !== 'number') continue;
    if (r.a >= s && r.a <= n && lonInside(r.o)) filtered.push(r);
  }
  // Behaviour split:
  //   - Initial chip-rail tap: viewport often empty (user just opened
  //     the map; chip might have nothing in their immediate view).
  //     Auto-fall back to the full chip data so they SEE something.
  //   - Search-this-area pill: user explicitly asked for "search
  //     here". Empty viewport → toast + keep existing carousel.
  if (filtered.length === 0 && opts.requireInBounds) {
    _showFindToast('No ' + chipDef.label.toLowerCase() + ' in this area');
    return;
  }
  items = filtered.length ? filtered : data;
  // Cap at 300 to keep the carousel + pin-rendering responsive, keeping
  // the 300 NEAREST the map centre. A plain slice took the first 300 in
  // file order: zoomed into Aleksotas (Kaunas) with no food in view, the
  // fallback returned 300 places strewn across the Baltics and fitBounds
  // flew the camera out to zoom 5.8 (baltics 2026-09-20, legacy layout).
  // The geo-shard path already falls back to nearest; this matches it.
  if (items.length > 300) {
    var _c = map.getCenter().wrap();
    var _ranked = [];
    for (var ri = 0; ri < items.length; ri++) {
      var ir = items[ri];
      if (typeof ir.a !== 'number' || typeof ir.o !== 'number') continue;
      _ranked.push({ d: _haversineMetersStrip(_c.lat, _c.lng, ir.a, ir.o), r: ir });
    }
    _ranked.sort(function(x, y) { return x.d - y.d; });
    items = [];
    for (var rj = 0; rj < _ranked.length && rj < 300; rj++) items.push(_ranked[rj].r);
  }
  expanded = !filtered.length;
  }  // end legacy (single-file / name-hash bucket) layout

  // Use the cached GPS as origin so the carousel cards' distance
  // labels are populated. Stale/missing → no distance shown.
  var origin = null;
  var loc = window.__streetzimLastLoc;
  if (loc && Date.now() - (loc.ts || 0) < 30 * 60 * 1000) {
    origin = { lat: loc.lat, lon: loc.lon, label: 'Current location' };
  }
  var stash = {
    label: chipDef.label + (expanded ? ' · expanded' : ''),
    origin: origin,
    items: items,
    // Tag the stash with the chip id so the render path (which
    // calls clearFindResults — wiping activeChip — at start)
    // can re-bind activeChip on the new state. Without this, the
    // first "Search this area" click had activeChip already null
    // and fell through to the old in-memory filter.
    chipId: chipDef.id,
  };
  try {
    sessionStorage.setItem(FIND_RESULTS_STASH_KEY, JSON.stringify(stash));
  } catch (e2) {}
  renderFindResultsFromStash(map);
}

// initExplore was the corner ⊕ Explore FAB that opened a chip
// menu. It duplicated the on-map chip rail (#find-chips) and shared
// its name with the Wikipedia "Explore" button (#wiki-toggle), per
// user 2026-05-10: "we have that same functionality in the
// chicklets under search AND in the 'Find' section. We need to
// fold it down to one. And we already have a wikipedia based
// 'Explore' as well."
//
// The chip rail wins (always visible, single tap, no click-to-
// reveal). This is now a no-op kept so the call site doesn't
// have to change.
function initExplore(_map) {}

// "Nearby" inside the detail panel. Source is the loaded result set
// itself, sorted by distance from `r`. Capped to 5 rows + a 1500 m
// radius so out-of-area entries don't drift in. Tapping a row swaps
// the detail panel to that place (no carousel flick required).
function _populateNearby(slot, map, r, selfIdx) {
  if (!_findResultsState.items || _findResultsState.items.length < 2) return;
  var items = _findResultsState.items;
  var ranked = [];
  for (var i = 0; i < items.length; i++) {
    if (i === selfIdx) continue;
    var o = items[i];
    if (typeof o.a !== 'number' || typeof o.o !== 'number') continue;
    var d = _haversineMetersStrip(r.a, r.o, o.a, o.o);
    if (d > 1500) continue;
    ranked.push({ idx: i, item: o, dist: d });
  }
  if (ranked.length === 0) return;
  ranked.sort(function(a, b) { return a.dist - b.dist; });
  ranked = ranked.slice(0, 5);

  var head = document.createElement('div');
  head.style.cssText = (
    'font-size:11px; color:#888; text-transform:uppercase;'
    + 'letter-spacing:0.04em; margin:0 0 6px;'
  );
  head.textContent = 'Nearby (within 1.5 km)';
  slot.appendChild(head);
  var list = document.createElement('div');
  list.style.cssText = (
    'display:flex; flex-direction:column;'
    + 'border:1px solid #eee; border-radius:10px; overflow:hidden;'
  );
  for (var j = 0; j < ranked.length; j++) {
    list.appendChild(_nearbyRow(map, ranked[j], j === ranked.length - 1));
  }
  slot.appendChild(list);
}

function _nearbyRow(map, entry, isLast) {
  var row = document.createElement('button');
  row.type = 'button';
  row.style.cssText = (
    'display:flex; align-items:center; gap:10px; width:100%;'
    + 'padding:10px 12px; background:#fff; cursor:pointer;'
    + 'border:none;' + (isLast ? '' : 'border-bottom:1px solid #f0f0f0;')
    + 'text-align:left; font:inherit;'
  );
  row.addEventListener('mouseenter', function() {
    row.style.background = '#f5f9ff';
  });
  row.addEventListener('mouseleave', function() {
    row.style.background = '#fff';
  });

  var col = document.createElement('div');
  col.style.cssText = 'flex:1 1 auto; min-width:0;';
  var name = document.createElement('div');
  name.style.cssText = (
    'font-weight:600; font-size:14px;'
    + 'overflow:hidden; text-overflow:ellipsis; white-space:nowrap;'
  );
  name.textContent = entry.item.n || '(unnamed)';
  col.appendChild(name);
  var meta = document.createElement('div');
  meta.style.cssText = (
    'color:#777; font-size:12px;'
    + 'overflow:hidden; text-overflow:ellipsis; white-space:nowrap;'
  );
  var kind = entry.item.cat || entry.item.s || entry.item.t;
  var metaParts = [];
  if (kind) metaParts.push(String(kind).replace(/_/g, ' '));
  if (entry.item.brand) metaParts.push(entry.item.brand);
  meta.textContent = metaParts.join(' · ');
  col.appendChild(meta);
  row.appendChild(col);

  var dist = document.createElement('div');
  dist.style.cssText = (
    'flex:0 0 auto; color:#1a73e8; font-size:13px; font-weight:600;'
  );
  dist.textContent = _formatDistanceStrip(entry.dist);
  row.appendChild(dist);

  row.addEventListener('click', function() {
    // Swap the detail panel to the tapped neighbour. Re-using
    // _showPlaceDetail keeps a clean stack — _showPlaceDetail tears
    // down any prior panel before opening the new one.
    _setActiveResult(map, entry.idx, /*flyTo*/true);
    _showPlaceDetail(map, entry.item, entry.idx);
  });
  return row;
}

function _findResultCard(map, r, idx) {
  var card = document.createElement('div');
  card.className = 'find-result-card';
  card.dataset.idx = idx;
  card.style.cssText = (
    'flex:0 0 76vw; max-width:340px; min-width:240px;'
    + 'scroll-snap-align:center; scroll-snap-stop:always;'
    + 'background:#fff; border:1px solid #ddd; border-radius:12px;'
    + 'padding:10px 12px; cursor:pointer; transition:border-color .15s, box-shadow .15s;'
    + 'display:flex; flex-direction:column; gap:4px;'
  );

  var name = document.createElement('div');
  name.style.cssText = (
    'font-weight:600; font-size:14px;'
    + 'overflow:hidden; text-overflow:ellipsis; white-space:nowrap;'
  );
  name.textContent = r.n || '(unnamed)';
  card.appendChild(name);

  var sub = document.createElement('div');
  sub.style.cssText = (
    'color:#777; font-size:12px;'
    + 'overflow:hidden; text-overflow:ellipsis; white-space:nowrap;'
  );
  var subParts = [];
  var kind = r.cat || r.s || r.t;
  if (kind) subParts.push(String(kind).replace(/_/g, ' '));
  if (_findResultsState.origin && r.a != null && r.o != null) {
    var dm = _haversineMetersStrip(
      _findResultsState.origin.lat, _findResultsState.origin.lon,
      r.a, r.o);
    subParts.push(_formatDistanceStrip(dm));
  }
  if (r.l) subParts.push(r.l);
  if (subParts.length) sub.textContent = subParts.join(' · ');
  card.appendChild(sub);

  // Quick action: Directions to here. Same URL-fragment protocol as
  // the wiki/feature popup so the routing flow opens identically.
  var actions = document.createElement('div');
  actions.style.cssText = 'margin-top:6px; display:flex; gap:6px;';
  var dir = document.createElement('a');
  dir.href = 'javascript:void(0)';
  dir.textContent = 'Directions';
  dir.style.cssText = (
    'flex:1; text-align:center; padding:7px 8px; border-radius:8px;'
    + 'text-decoration:none; font-weight:600; font-size:12px;'
    + 'background:#1a73e8; color:#fff; border:1px solid #1a73e8;'
  );
  dir.addEventListener('click', function(e) {
    e.stopPropagation();
    if (window.streetzimRouting && window.streetzimRouting.open) {
      window.streetzimRouting.open();
      window.streetzimRouting.setDest(r.a, r.o, r.n || '');
      var cached = window.__streetzimLastLoc;
      var fresh = cached && (Date.now() - cached.ts) < 10 * 60 * 1000;
      if (fresh && window.streetzimRouting.setOrigin) {
        window.streetzimRouting.setOrigin(cached.lat, cached.lon, 'Current location');
      } else if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(function(pos) {
          window.__streetzimLastLoc = {
            lat: pos.coords.latitude, lon: pos.coords.longitude, ts: Date.now()
          };
          if (window.streetzimRouting.setOrigin) {
            window.streetzimRouting.setOrigin(
              pos.coords.latitude, pos.coords.longitude, 'Current location');
          }
        }, function() {}, { enableHighAccuracy: false, maximumAge: 60000, timeout: 8000 });
      }
    }
  });
  actions.appendChild(dir);
  // Tap-to-call + tap-to-website actions when the place has them.
  // Stop propagation so the card-level click handler doesn't also
  // re-centre the camera under the user's finger.
  function _miniBtn(emoji, title, href, target) {
    var a = document.createElement('a');
    a.href = href;
    a.title = title;
    if (target) { a.target = target; a.rel = 'noopener'; }
    a.textContent = emoji;
    a.style.cssText = (
      'flex:0 0 auto; display:inline-flex; align-items:center;'
      + 'justify-content:center; width:36px; padding:7px 0;'
      + 'border-radius:8px; text-decoration:none; font-size:14px;'
      + 'border:1px solid #ddd; background:#fff; color:#222;'
    );
    a.addEventListener('click', function(e) { e.stopPropagation(); });
    return a;
  }
  if (r.p) {
    actions.appendChild(_miniBtn('📞', r.p,
      'tel:' + String(r.p).replace(/\s+/g, '')));
  }
  if (r.ws) {
    actions.appendChild(_miniBtn('🌐', r.ws, r.ws, '_blank'));
  }
  card.appendChild(actions);

  // Tap (not on a sub-button) does two things:
  //   1) re-fly the camera to this result (helpful when the map has
  //      drifted away from the active pin)
  //   2) open the bottom-sheet place detail panel with the full
  //      enrichment — Wikipedia extract async, phone/website spelled
  //      out, distance from origin, fact grid, etc.
  // Sub-buttons (Directions / 📞 / 🌐) stop propagation so they
  // don't also open the panel.
  card.addEventListener('click', function() {
    _setActiveResult(map, idx, /*flyTo*/true);
    _showPlaceDetail(map, r, idx);
  });
  return card;
}

function _findCenteredCardIdx() {
  var track = _findResultsState.track;
  if (!track || !track.children.length) return -1;
  var rect = track.getBoundingClientRect();
  var center = rect.left + rect.width / 2;
  var bestIdx = -1, bestDist = Infinity;
  for (var i = 0; i < track.children.length; i++) {
    var cardRect = track.children[i].getBoundingClientRect();
    var c = cardRect.left + cardRect.width / 2;
    var d = Math.abs(c - center);
    if (d < bestDist) { bestDist = d; bestIdx = i; }
  }
  return bestIdx;
}

function _scrollCarouselTo(idx) {
  var track = _findResultsState.track;
  if (!track || idx < 0 || idx >= track.children.length) return;
  var card = track.children[idx];
  // Use scrollIntoView with center inline so scroll-snap settles cleanly.
  card.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
}

function _setActiveResult(map, idx, flyTo) {
  if (idx < 0 || !_findResultsState.items
      || idx >= _findResultsState.items.length) return;
  var prev = _findResultsState.active;
  _findResultsState.active = idx;
  // Card highlight.
  var track = _findResultsState.track;
  if (track) {
    if (prev >= 0 && track.children[prev]) {
      track.children[prev].style.borderColor = '#ddd';
      track.children[prev].style.boxShadow = '';
    }
    var active = track.children[idx];
    if (active) {
      active.style.borderColor = '#e74c3c';
      active.style.boxShadow = '0 0 0 2px rgba(231,76,60,0.25)';
    }
  }
  // Marker highlight — recolor the active pin and revert the previous.
  // maplibregl.Marker stores the SVG inside `getElement()`; the fill is
  // on a `<path>` we can update.
  if (prev >= 0 && _findResultsState.markers[prev]) {
    _markerSetColor(_findResultsState.markers[prev], '#e74c3c');
  }
  if (_findResultsState.markers[idx]) {
    _markerSetColor(_findResultsState.markers[idx], '#1a73e8');
    // Bring the active marker to the top of the marker stack.
    // MapLibre puts each marker in its own absolutely-positioned div
    // that's a child of the markers layer; with the same z-index,
    // DOM order decides what stacks on top. Re-appending puts the
    // active pin last in the parent → painted last → no longer
    // hidden behind another marker the user just panned past.
    try {
      var el = _findResultsState.markers[idx].getElement();
      if (el && el.parentNode) {
        el.parentNode.appendChild(el);
      }
    } catch (e2) {}
    // Close any popups on OTHER markers — common case is the user
    // tapped marker A (popup A opens), then swiped the carousel to
    // card B. Without this, popup A lingers on the map.
    for (var k = 0; k < _findResultsState.markers.length; k++) {
      if (k === idx) continue;
      var other = _findResultsState.markers[k];
      if (!other) continue;
      try {
        var pop = other.getPopup();
        if (pop && pop.isOpen()) other.togglePopup();
      } catch (e3) {}
    }
    // Also dismiss the Wikidata feature popup if any. Same
    // cross-system rationale as the marker click handler in
    // renderFindResultsFromStash.
    if (_findResultsState.map
        && typeof _findResultsState.map._closeWikidataPopup === 'function') {
      try { _findResultsState.map._closeWikidataPopup(); } catch (e4) {}
    }
  }
  if (flyTo) {
    var r = _findResultsState.items[idx];
    map.flyTo({
      center: [r.o, r.a],
      zoom: Math.max(map.getZoom(), 14),
      // Offset the camera up by the strip's height so the active
      // pin sits in the visible map area, not behind the carousel.
      offset: [0, -110],
      duration: 500,
    });
  }
}

function _markerSetColor(marker, color) {
  // MapLibre v5's default marker is an inline <svg> with the body
  // color spread across a few elements (a <g fill> wrapper plus a
  // few <path>s). Walk every fill-bearing node and recolor anything
  // that isn't the inner white highlight.
  try {
    var el = marker.getElement();
    var svg = el.querySelector('svg') || el;
    var nodes = svg.querySelectorAll('[fill]');
    for (var i = 0; i < nodes.length; i++) {
      var raw = (nodes[i].getAttribute('fill') || '').trim().toLowerCase();
      if (!raw || raw === 'none' || raw === 'transparent') continue;
      var noHash = raw.replace(/^#/, '');
      if (noHash === 'fff' || noHash === 'ffffff' || raw === 'white') continue;
      nodes[i].setAttribute('fill', color);
    }
  } catch (e) {}
}

function _haversineMetersStrip(lat1, lon1, lat2, lon2) {
  var R = 6371000;
  var toRad = Math.PI / 180;
  var dLat = (lat2 - lat1) * toRad;
  var dLon = (lon2 - lon1) * toRad;
  var a = Math.sin(dLat/2) * Math.sin(dLat/2)
        + Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad)
        * Math.sin(dLon/2) * Math.sin(dLon/2);
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

function _formatDistanceStrip(m) {
  if (m == null || isNaN(m)) return '';
  if (m < 1000) return Math.round(m) + ' m';
  return (m / 1000).toFixed(m < 10000 ? 1 : 0) + ' km';
}

function clearFindResults() {
  for (var i = 0; i < _findResultsState.markers.length; i++) {
    try { _findResultsState.markers[i].remove(); } catch (e) {}
  }
  _findResultsState.markers = [];
  if (_findResultsState.strip && _findResultsState.strip.parentNode) {
    _findResultsState.strip.parentNode.removeChild(_findResultsState.strip);
  }
  _findResultsState.strip = null;
  _findResultsState.track = null;
  _findResultsState.items = null;
  _findResultsState.origin = null;
  _findResultsState.active = -1;
  if (_findResultsState.scrollDebounce) {
    clearTimeout(_findResultsState.scrollDebounce);
    _findResultsState.scrollDebounce = null;
  }
  if (_findResultsState.map && _findResultsState.moveHandler) {
    try { _findResultsState.map.off('moveend', _findResultsState.moveHandler); }
    catch (e) {}
  }
  _findResultsState.moveHandler = null;
  _findResultsState.anchorCenter = null;
  _findResultsState.anchorZoom = null;
  if (_findResultsState.searchAreaBtn
      && _findResultsState.searchAreaBtn.parentNode) {
    _findResultsState.searchAreaBtn.parentNode.removeChild(
      _findResultsState.searchAreaBtn);
  }
  _findResultsState.searchAreaBtn = null;
  // Restore the main search if we hid it for the pill.
  if (_findResultsState._searchHidden) {
    var sc = document.getElementById('search-container');
    if (sc) sc.classList.remove('streetzim-hidden');
    _findResultsState._searchHidden = false;
  }
  _hidePlaceDetail();
  // Clear any active chip in the on-map find rail.
  if (typeof _findChipPaintActive === 'function') {
    _findChipPaintActive(null);
  }
  _findResultsState.activeChip = null;
  if (_findResultsState.map) {
    try { _findResultsState.map.keyboard.enable(); } catch (e) {}
  }
  _findResultsState.map = null;
  document.removeEventListener('keydown', _findResultsKeydown);
}

