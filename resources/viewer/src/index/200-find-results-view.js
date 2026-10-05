// --- Find-results map view ---
// places.html stashes its current `state.results` in sessionStorage
// when the user clicks "Map view"; we read that here, drop one pin
// per record, fit bounds, and render a horizontal carousel pinned
// to the bottom of the viewport. As the user swipes (or scroll-
// snaps) between cards, the camera flies to the centered card's
// pin and that pin gets a visual highlight. v1 used a floating
// upper-right sidebar — covered most of the map on phones, replaced
// 2026-05-08 with this Google-Maps-style bottom strip.
//
// Schema (must match places.html `stashResultsForMap`):
//   { label: "<chip or query name>",
//     origin: { lat, lon, label } | null,
//     items:  [ { n, a, o, t?, s?, cat?, l?, ws?, p?, soc?, brand?, wd? }, … ] }
var FIND_RESULTS_STASH_KEY = 'streetzim_find_results';
var _findResultsState = {
  map:     null,   // maplibregl.Map — kept so teardown can re-enable keyboard
  markers: [],     // maplibregl.Marker[] — index aligns with stash.items
  strip:   null,   // root <div> of the bottom carousel
  track:   null,   // the scrollable inner element holding the cards
  items:   null,   // stash.items
  origin:  null,   // stash.origin (used for distance labels)
  active:  -1,     // index of the card currently centered
  searchAreaBtn: null,  // floating "Search this area" pill on the map
  anchorCenter:  null,  // map center once the results' camera settles
  anchorZoom:    null,  // map zoom once the results' camera settles
  moveHandler:   null,  // bound moveend handler (so we can detach on clear)
  renderSeq:     0,     // bumped per render; stale map.once('idle') callbacks bail
  detailPanel:   null,  // root <div> of the place detail bottom-sheet (or null)
  detailBackdrop: null, // semi-transparent backdrop behind detail panel
  scrollDebounce: null,
};

function renderFindResultsFromStash(map) {
  // A render that keeps the view must not let the previous render's
  // camera flight carry on (tap Gas, which flies to the nearest
  // stations, then Food & Drink: the food pins were left off screen).
  if (_findResultsState.fitting) {
    _findResultsState.fitting = false;
    try { map.stop(); } catch (e) {}
  }
  clearFindResults();
  var raw;
  try { raw = sessionStorage.getItem(FIND_RESULTS_STASH_KEY); }
  catch (e) { return; }
  if (!raw) return;
  var stash;
  try { stash = JSON.parse(raw); }
  catch (e) { return; }
  if (!stash || !Array.isArray(stash.items) || stash.items.length === 0) {
    return;
  }
  // Drop the stash after parsing so a refresh doesn't re-fire.
  // The data lives on `_findResultsState` for the lifetime of the
  // open carousel.
  try { sessionStorage.removeItem(FIND_RESULTS_STASH_KEY); }
  catch (e) {}
  _findResultsState.map    = map;
  // Sub-filter handling: the stash carries the FULL set of items
  // for the active chip plus an optional subFilter ("italian_-
  // restaurant", "bakery", …). We narrow stash.items in place so
  // every downstream loop (markers, carousel) sees only the filtered
  // subset, but keep allItems on state so the sub-filter row's
  // histogram reflects the full scope.
  _findResultsState.allItems = stash.items;
  _findResultsState.subFilter = stash.subFilter || null;
  _findResultsState.label = stash.label || '';
  if (stash.subFilter) {
    stash.items = stash.items.filter(function(r) {
      return (r.s || '') === stash.subFilter;
    });
  }
  _findResultsState.items  = stash.items;
  _findResultsState.origin = stash.origin || null;
  // Restore activeChip from the stash, paint the chip rail. Required
  // so subsequent "Search this area" clicks know to RE-FETCH the
  // chip's full dataset rather than just filter the in-memory items
  // array (which only contains the prior viewport's matches).
  if (stash.chipId && typeof EXPLORE_CHIPS !== 'undefined') {
    for (var ci = 0; ci < EXPLORE_CHIPS.length; ci++) {
      if (EXPLORE_CHIPS[ci].id === stash.chipId) {
        _findResultsState.activeChip = EXPLORE_CHIPS[ci];
        if (typeof _findChipPaintActive === 'function') {
          _findChipPaintActive(stash.chipId);
        }
        break;
      }
    }
  }
  // MapLibre's KeyboardHandler eats arrow keys for camera panning,
  // which fights the carousel's keyboard nav. Disable while the
  // carousel is open; clearFindResults re-enables.
  try { map.keyboard.disable(); } catch (e) {}

  // One marker per record. The "active" marker (whichever the
  // carousel currently highlights) gets a distinct color via DOM
  // class — see `_setActiveResult` for the swap.
  var bounds = null;
  for (var i = 0; i < stash.items.length; i++) {
    var r = stash.items[i];
    if (typeof r.a !== 'number' || typeof r.o !== 'number') {
      // Keep markers[] parallel to items[] — the carousel, pin click
      // handler and keyboard nav all index by position, and every
      // consumer already null-checks.
      _findResultsState.markers.push(null);
      continue;
    }
    var marker = new maplibregl.Marker({ color: '#e74c3c' })
      .setLngLat([r.o, r.a])
      .addTo(map);
    var popup = _findResultPopup(map, r);
    if (popup) marker.setPopup(popup);
    // Tap a pin → snap the carousel to the matching card AND close
    // any other popups that were open. MapLibre default
    // closeOnClick:false leaves earlier popups dangling when the
    // user clicks a different pin; before this we'd accumulate
    // multiple popups on the map.
    (function(idx) {
      var el = marker.getElement();
      el.style.cursor = 'pointer';
      el.addEventListener('click', function() {
        for (var k = 0; k < _findResultsState.markers.length; k++) {
          if (k === idx) continue;
          var other = _findResultsState.markers[k];
          if (!other) continue;
          try {
            var pop = other.getPopup();
            if (pop && pop.isOpen()) other.togglePopup();
          } catch (e2) {}
        }
        // Also dismiss the Wikidata feature popup if any — otherwise
        // tap on a feature opens its popup, then tap on a find-pin
        // accumulates a second popup. The two systems live in
        // separate closures; bridge via map._closeWikidataPopup.
        if (typeof map._closeWikidataPopup === 'function') {
          try { map._closeWikidataPopup(); } catch (e3) {}
        }
        _scrollCarouselTo(idx);
      });
    })(i);
    _findResultsState.markers.push(marker);
    // Longitudes the short way from the first result, so results either
    // side of the antimeridian frame the gap between them, not the globe.
    var ro = bounds ? r.o - 360 * Math.round((r.o - bounds.getWest()) / 360) : r.o;
    if (!bounds) bounds = new maplibregl.LngLatBounds([ro, r.a], [ro, r.a]);
    else         bounds.extend([ro, r.a]);
  }
  // Results found in what the reader can see (a chip tap that did not
  // fall back to the nearest places, "Search this area": stash.fromView)
  // leave the camera alone: re-framing them with maxZoom 14 pulled a
  // reader at z16 out to z14 on every tap, 16 times the area they had
  // chosen. Anything else (a name search handed over from places.html, a
  // chip that fell back) is framed as before.
  if (bounds && !stash.fromView) {
    // Leave room for the carousel at the bottom — extra bottom
    // padding so the camera doesn't park results behind the strip.
    _findResultsState.fitting = true;
    map.once('moveend', function() { _findResultsState.fitting = false; });
    map.fitBounds(bounds, {
      padding: { top: 60, right: 40, bottom: 200, left: 40 },
      maxZoom: 14, duration: 800,
    });
  }
  _renderFindResultsStrip(map, stash);
  _setActiveResult(map, 0, /*flyTo*/false);

  // Pre-warm routing cells for the visible results NOW, while the
  // user is reading the carousel. Each card is a candidate
  // "Directions to here" target — by the time they click, the
  // destination cell should be loaded (vs the 1.7 s wait we saw on
  // the user's profile). Cap at 12 unique cells (worker dedupes by
  // cell_id internally) so we don't blow the 12-cell LRU.
  if (typeof window.__streetzim_prewarmRoutingCells === 'function') {
    var coords = [];
    for (var pi = 0; pi < stash.items.length && coords.length < 12; pi++) {
      var r = stash.items[pi];
      if (typeof r.a === 'number' && typeof r.o === 'number') {
        coords.push({ lat: r.a, lon: r.o });
      }
    }
    if (coords.length) {
      window.__streetzim_prewarmRoutingCells(coords);
    }
  }

  // After fitBounds settles, capture the anchor view + start watching
  // for camera moves. Any subsequent move shows the "Search this area"
  // pill so the user can re-filter results to the new viewport.
  // Two renders before the map goes idle (rapid chip / sub-filter
  // taps) both used to reach this callback and each installed a
  // moveend handler; clearFindResults only detached the last one, so
  // the earlier handler leaked for the page lifetime.
  var renderToken = ++_findResultsState.renderSeq;
  map.once('idle', function() {
    if (!_findResultsState.strip) return;
    if (renderToken !== _findResultsState.renderSeq) return;
    if (_findResultsState.moveHandler) {
      try { map.off('moveend', _findResultsState.moveHandler); } catch (e) {}
    }
    _findResultsState.anchorCenter = map.getCenter();
    _findResultsState.anchorZoom   = map.getZoom();
    _findResultsState.moveHandler  = function() {
      if (!_findResultsState.strip) return;
      if (_searchAreaThresholdReached(map)) _showSearchAreaPill(map);
    };
    map.on('moveend', _findResultsState.moveHandler);
  });
  // 'idle' fires only after a render. When the camera stays put (the view
  // is kept) and nothing else redraws, it would never come and the pill
  // would never arm; ask for one frame so it does.
  map.triggerRepaint();
}

// The part of the map the reader can see, in canvas pixels, for a pin's
// tip: the screen less 16 px at the sides and a pin's body (FIND_PIN_H)
// at the top, since a pin is drawn upward from its tip; above the results
// strip at its tallest (200 px, or the strip on screen if that is taller:
// a strip without a sub-filter row is 34 px shorter than the next one may
// be); and not under the search box and chip rail, the "Search this area"
// pill, the layer buttons or MapLibre's top corner controls (holes: each
// element's own rectangle, extended down by a pin's body). Holes, not a
// band across the screen: on a landscape phone the search box covers only
// the middle of the width, and a band below it left 11 px of a 360 px
// screen.
var FIND_PIN_H = 44;
function _findVisibleRect(map) {
  var canvas = map.getCanvas();
  var cr = canvas.getBoundingClientRect();
  var W = canvas.clientWidth, H = canvas.clientHeight;
  var holes = [];
  var els = ['search-container', 'find-search-area-btn', 'controls']
    .map(function(id) { return document.getElementById(id); });
  var ctrls = document.querySelectorAll(
    '.maplibregl-ctrl-top-left > .maplibregl-ctrl, .maplibregl-ctrl-top-right > .maplibregl-ctrl');
  for (var c = 0; c < ctrls.length; c++) els.push(ctrls[c]);
  els.forEach(function(el) {
    if (!el) return;
    var r = el.getBoundingClientRect();
    if (r.height > 0 && r.width > 0) {
      holes.push({ x0: r.left - cr.left - 4, y0: r.top - cr.top - 4,
                   x1: r.right - cr.left + 4, y1: r.bottom - cr.top + FIND_PIN_H - 4 });
    }
  });
  var strip = document.getElementById('find-results-strip');
  var sr = strip ? strip.getBoundingClientRect() : null;
  var bottom = Math.min((sr && sr.height > 0) ? sr.top - cr.top : H, H - 200);
  return { x0: 16, y0: FIND_PIN_H, x1: W - 16,
           y1: Math.max(bottom - 8, FIND_PIN_H + 8), holes: holes };
}

// Whether a record's point is inside that area on screen. project() takes
// longitudes as given, so a record across the antimeridian from the view
// is tried shifted by ±360° too.
function _findRecordVisible(map, rect, r) {
  if (typeof r.a !== 'number' || typeof r.o !== 'number') return false;
  for (var k = -1; k <= 1; k++) {
    var p = map.project([r.o + 360 * k, r.a]);
    if (p.x < rect.x0 || p.x > rect.x1 || p.y < rect.y0 || p.y > rect.y1) continue;
    var covered = false;
    for (var h = 0; h < rect.holes.length; h++) {
      var o = rect.holes[h];
      if (p.x >= o.x0 && p.x <= o.x1 && p.y >= o.y0 && p.y <= o.y1) { covered = true; break; }
    }
    if (!covered) return true;
  }
  return false;
}

// A lon/lat box around that rectangle (its four corners unprojected), for
// queries that take a box. Wider than the rectangle on a rotated or tilted
// map; callers filter with _findRecordVisible.
function _findVisibleBox(map, rect) {
  var pts = [[rect.x0, rect.y0], [rect.x1, rect.y0], [rect.x0, rect.y1], [rect.x1, rect.y1]]
    .map(function(p) { return map.unproject(p); });
  var box = { s: 90, n: -90, w: Infinity, e: -Infinity };
  pts.forEach(function(ll) {
    box.s = Math.min(box.s, ll.lat); box.n = Math.max(box.n, ll.lat);
    box.w = Math.min(box.w, ll.lng); box.e = Math.max(box.e, ll.lng);
  });
  return box;
}

// Threshold: pill appears once the user has either zoomed by ≥ 0.5
// levels or panned the centre out of the original viewport. Tighter
// than "any move" so a small jitter doesn't flash the pill.
function _searchAreaThresholdReached(map) {
  var ac = _findResultsState.anchorCenter;
  var az = _findResultsState.anchorZoom;
  if (!ac || az == null) return false;
  if (Math.abs(map.getZoom() - az) >= 0.5) return true;
  var b = map.getBounds();
  // Outside the current viewport box → user has clearly panned away.
  return ac.lng < b.getWest() || ac.lng > b.getEast()
      || ac.lat < b.getSouth() || ac.lat > b.getNorth();
}

function _findResultPopup(map, r) {
  if (typeof map._buildWikiPopupDOM === 'function') {
    var wd = null;
    if (r.l) wd = { c: r.l };
    var lng = { lat: r.a, lng: r.o };
    var dom = map._buildWikiPopupDOM(r.n || szT('common.unnamed', '(unnamed)'), wd, lng,
                                     _wikiArticlePath(r.q, r.w));
    return new maplibregl.Popup({ offset: 28, closeButton: true,
                                  closeOnClick: false, maxWidth: '320px' })
      .setDOMContent(dom);
  }
  var box = document.createElement('div');
  box.style.cssText = 'font:14px/1.4 -apple-system,system-ui,sans-serif;';
  box.textContent = r.n || szT('common.unnamed', '(unnamed)');
  return new maplibregl.Popup({ offset: 28, closeButton: true,
                                closeOnClick: false })
    .setDOMContent(box);
}

// Sub-filter chip row above the carousel. Derived from the FULL
// item set (`_findResultsState.allItems`) so the histogram doesn't
// shrink as the user narrows; only the displayed cards/markers
// shrink. Tapping a chip swaps `_findResultsState.subFilter` and
// re-runs the render path (via session-storage stash) with the
// new filter applied.
function _renderSubfilterRow(map, stash) {
  var all = _findResultsState.allItems || stash.items || [];
  var counts = new Map();
  for (var i = 0; i < all.length; i++) {
    var s = (all[i] && all[i].s) ? String(all[i].s) : '';
    if (!s) continue;
    counts.set(s, (counts.get(s) || 0) + 1);
  }
  var subs = [];
  counts.forEach(function(n, key) {
    if (n >= 2) subs.push([key, n]);
  });
  subs.sort(function(a, b) { return b[1] - a[1]; });
  subs = subs.slice(0, 8);
  // Need at least 2 distinct subtypes to be useful — if everyone
  // is e.g. just "restaurant" there's nothing to narrow.
  if (subs.length < 2) return null;

  var row = document.createElement('div');
  row.style.cssText = (
    'display:flex; gap:6px; padding:0 14px 6px;'
    + 'overflow-x:auto; overflow-y:hidden;'
    + '-webkit-overflow-scrolling:touch; scrollbar-width:none;'
    + 'mask-image:linear-gradient(to right, transparent 0, black 14px,'
    + ' black calc(100% - 14px), transparent 100%);'
    + '-webkit-mask-image:linear-gradient(to right, transparent 0,'
    + ' black 14px, black calc(100% - 14px), transparent 100%);'
  );

  var current = _findResultsState.subFilter;

  function makeChip(label, sub, count) {
    var chip = document.createElement('button');
    chip.type = 'button';
    var active = sub === current;
    chip.style.cssText = (
      'flex:0 0 auto; padding:5px 11px; border-radius:999px;'
      + 'font:inherit; font-size:12px; font-weight:600;'
      + 'cursor:pointer; white-space:nowrap;'
      + 'transition:background .12s ease, color .12s ease,'
      + ' transform .08s ease;'
      + (active
        ? 'background:#1a73e8; color:#fff;'
          + 'border:1px solid #1a73e8;'
        : 'background:var(--szd-surface, #fff); color:var(--szd-fg, #222);'
          + 'border:1px solid var(--szd-line, #ddd);')
    );
    chip.textContent = (count != null) ? (label + ' · ' + count) : label;
    chip.addEventListener('click', function() {
      _applySubFilter(map, sub === current ? null : sub);
    });
    chip.addEventListener('pointerdown', function() {
      chip.style.transform = 'scale(0.97)';
    });
    chip.addEventListener('pointerup', function() {
      chip.style.transform = '';
    });
    chip.addEventListener('pointerleave', function() {
      chip.style.transform = '';
    });
    return chip;
  }

  if (current) {
    row.appendChild(makeChip(szT('find.all', 'All'), null, null));
  }
  for (var k = 0; k < subs.length; k++) {
    row.appendChild(makeChip(_humanSubtype(subs[k][0]), subs[k][0], subs[k][1]));
  }
  return row;
}

function _humanSubtype(s) {
  if (!s) return '';
  return szPlaceType(s).replace(/^./, function(c) {
    return c.toUpperCase();
  });
}

function _applySubFilter(map, sub) {
  var all = _findResultsState.allItems;
  if (!all) return;
  // Build a fresh stash with the SAME full item set + new subFilter.
  // renderFindResultsFromStash will narrow stash.items in place
  // before drawing markers / carousel cards, but keep allItems
  // visible to the sub-row builder so the histogram is stable.
  var chip = _findResultsState.activeChip;
  var origin = _findResultsState.origin;
  var label = _findResultsState.label || (chip ? szChipLabel(chip.id, chip.label) : szT('find.results', 'Results'));
  var stash = {
    label: label, origin: origin, items: all,
    chipId: chip ? chip.id : undefined,
    subFilter: sub || undefined,
  };
  try {
    sessionStorage.setItem(FIND_RESULTS_STASH_KEY, JSON.stringify(stash));
  } catch (e) {}
  renderFindResultsFromStash(map);
}

function _renderFindResultsStrip(map, stash) {
  // Bottom-pinned, full-width carousel. Each card snaps to center
  // via CSS scroll-snap; an IntersectionObserver tracks which card
  // is centered and drives the active-result update.
  var strip = document.createElement('div');
  strip.id = 'find-results-strip';
  // Which chip, for the gates: the title is a translatable label.
  if (stash.chipId) strip.setAttribute('data-chip', stash.chipId);
  strip.style.cssText = (
    'position:fixed; left:0; right:0; bottom:var(--bottom-inset, 0px); z-index:1500;'
    + 'background:var(--szd-surface-a, rgba(255,255,255,0.96)); border-top:1px solid var(--szd-line, #ccc);'
    + 'box-shadow:0 -4px 16px rgba(0,0,0,0.12);'
    + 'font:13px/1.35 -apple-system,system-ui,sans-serif; color:var(--szd-fg, #222);'
    + 'padding-top:6px;'
    // Clear the dead touch band next to the app's bottom toolbar (see the
    // locate-button rule): without this the cards' lower half renders fine
    // but taps/swipes there are swallowed by native chrome gestures.
    + 'padding-bottom:max(8px, min(var(--bottom-inset, 0px), 64px));'
  );

  var header = document.createElement('div');
  header.style.cssText = (
    'padding:2px 14px 6px; display:flex; align-items:center; gap:8px;'
    + 'font-size:12px; color:var(--szd-fg-2, #555);'
  );
  var title = document.createElement('span');
  title.style.cssText = (
    'flex:1 1 auto; min-width:0; overflow:hidden;'
    + 'text-overflow:ellipsis; white-space:nowrap;'
  );
  title.textContent = (stash.label || szT('find.results', 'Results')) + ' · ' + stash.items.length;
  header.appendChild(title);
  var closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.title = szT('find.clear_results', 'Clear results');
  closeBtn.textContent = '×';
  closeBtn.style.cssText = (
    'flex:0 0 auto; background:transparent; border:none;'
    + 'font-size:18px; line-height:1; cursor:pointer; color:var(--szd-fg-3, #666);'
    + 'padding:2px 6px;'
  );
  closeBtn.addEventListener('click', clearFindResults);
  header.appendChild(closeBtn);
  strip.appendChild(header);

  // Sub-filter chip row — same idea as places.html's
  // _renderSubfilterChips. Derives top-N subtypes from the active
  // result set's r.s histogram. Tapping narrows the carousel + pins
  // to that subtype; tapping again or "All" clears.
  var subRow = _renderSubfilterRow(map, stash);
  if (subRow) strip.appendChild(subRow);

  var track = document.createElement('div');
  track.id = 'find-results-strip-track';
  track.style.cssText = (
    'display:flex; gap:8px; overflow-x:auto; overflow-y:hidden;'
    + 'scroll-snap-type:x mandatory;'
    + '-webkit-overflow-scrolling:touch;'
    + 'scrollbar-width:none;'
    + 'padding:4px 12vw 8px;'  // big side-padding so the first/last card can center
  );
  // Hide scrollbar across browsers — purely visual on a touch UI.
  track.style.setProperty('scrollbar-width', 'none');
  for (var i = 0; i < stash.items.length; i++) {
    track.appendChild(_findResultCard(map, stash.items[i], i));
  }
  strip.appendChild(track);
  // Swipe UP on a card expands it into the full place-detail sheet — the
  // natural bottom-sheet gesture, and sturdier than a tap when the card's
  // lower edge brushes the app's dead touch band. Fires the card's own click
  // handler so it always opens exactly the card the gesture started on
  // (falling back to the centered card). Horizontal carousel swipes are
  // untouched: the gesture must be clearly vertical (1.5x dominance, 40px).
  (function addSwipeUpExpand() {
    var sx = 0, sy = 0, startCard = null, armed = false;
    track.addEventListener('touchstart', function(e) {
      if (!e.touches || e.touches.length !== 1) { armed = false; return; }
      sx = e.touches[0].clientX; sy = e.touches[0].clientY;
      startCard = (e.target && e.target.closest)
        ? e.target.closest('.find-result-card') : null;
      armed = true;
    }, { passive: true });
    track.addEventListener('touchmove', function(e) {
      if (!armed || !e.touches || e.touches.length !== 1) return;
      var dx = e.touches[0].clientX - sx, dy = e.touches[0].clientY - sy;
      if (dy < -40 && Math.abs(dy) > Math.abs(dx) * 1.5) {
        armed = false;
        var card = startCard;
        if (!card) {
          var di = _findCenteredCardIdx();
          if (di >= 0 && track.children[di]) card = track.children[di];
        }
        if (card) card.click();
      }
    }, { passive: true });
    track.addEventListener('touchend', function() { armed = false; }, { passive: true });
  })();
  document.body.appendChild(strip);

  // Watch for the centered card. IntersectionObserver with a tall
  // narrow root margin would work, but the simpler signal is just
  // the scroll position — debounced so a fast swipe doesn't fire
  // a flyTo for every transitional card.
  track.addEventListener('scroll', function() {
    if (_findResultsState.scrollDebounce) {
      clearTimeout(_findResultsState.scrollDebounce);
    }
    _findResultsState.scrollDebounce = setTimeout(function() {
      var idx = _findCenteredCardIdx();
      if (idx >= 0 && idx !== _findResultsState.active) {
        _setActiveResult(map, idx, /*flyTo*/true);
      }
    }, 120);
  });

  _findResultsState.strip = strip;
  _findResultsState.track = track;

  // Keyboard nav for the carousel:
  //   ←/↑ → previous card   →/↓ → next card
  //   Enter → toggle active marker's popup (same as tapping the pin)
  //   Esc   → close the carousel
  // Skip when the user is typing in an input — they want to type, not
  // scroll cards.
  document.addEventListener('keydown', _findResultsKeydown);
}

function _showSearchAreaPill(map) {
  if (_findResultsState.searchAreaBtn) return; // already on screen
  // While the pill is on screen it pretty much owns the top-center
  // band — hide the main search input so the two affordances aren't
  // crowding each other / drawing the user's eye away from the
  // result-set-on-map flow they're actively in. Class-based so the
  // !important rule beats any stray inline display (and so the
  // hide survives if some other code rewrites style.display).
  var searchContainer = document.getElementById('search-container');
  if (searchContainer) {
    searchContainer.classList.add('streetzim-hidden');
    _findResultsState._searchHidden = true;
  }
  var btn = document.createElement('button');
  btn.id = 'find-search-area-btn';
  btn.type = 'button';
  btn.textContent = szT('find.search_this_area', 'Search this area');
  btn.style.cssText = (
    'position:fixed; left:50%; top:calc(14px + var(--top-inset, 0px)); transform:translateX(-50%);'
    + 'z-index:1600; padding:9px 18px;'
    + 'background:var(--szd-surface-a, rgba(255,255,255,0.92)); color:var(--szd-fg, #222);'
    + 'border:1px solid rgba(0,0,0,0.12); border-radius:999px;'
    + 'font:600 13px/1.2 -apple-system,system-ui,sans-serif;'
    + 'box-shadow:0 4px 14px rgba(0,0,0,0.18);'
    + 'cursor:pointer; opacity:0;'
    + 'transition:opacity .25s ease, transform .25s ease, background .15s;'
    + 'pointer-events:auto;'
  );
  btn.addEventListener('mouseenter', function() {
    btn.style.background = 'var(--szd-surface-hover, rgba(255,255,255,1))';
  });
  btn.addEventListener('mouseleave', function() {
    btn.style.background = 'var(--szd-surface-a, rgba(255,255,255,0.92))';
  });
  btn.addEventListener('click', function() {
    _searchAreaApply(map);
  });
  document.body.appendChild(btn);
  _findResultsState.searchAreaBtn = btn;
  // Two RAFs so the initial style flush takes the opacity-0 starting
  // point; otherwise the transition skips and the button pops in.
  requestAnimationFrame(function() {
    requestAnimationFrame(function() {
      if (!_findResultsState.searchAreaBtn) return;
      _findResultsState.searchAreaBtn.style.opacity = '1';
    });
  });
}

function _hideSearchAreaPill() {
  var btn = _findResultsState.searchAreaBtn;
  if (!btn) return;
  _findResultsState.searchAreaBtn = null;
  btn.style.opacity = '0';
  setTimeout(function() {
    if (btn.parentNode) btn.parentNode.removeChild(btn);
  }, 250);
  // Restore the main search container.
  if (_findResultsState._searchHidden) {
    var searchContainer = document.getElementById('search-container');
    if (searchContainer) {
      searchContainer.classList.remove('streetzim-hidden');
    }
    _findResultsState._searchHidden = false;
  }
}

function _searchAreaApply(map) {
  // If the carousel was populated by a chip-rail tap, re-fetch the
  // chip's full dataset and filter to the new viewport — that way
  // panning SF→LA and tapping "Search this area" surfaces LA
  // restaurants instead of (no SF restaurants in LA, so nothing).
  // For non-chip stashes (e.g. a hand-off from places.html name
  // search), fall back to filtering the existing items array.
  var activeChip = _findResultsState.activeChip;
  if (activeChip && typeof loadChipOnMap === 'function') {
    _hideSearchAreaPill();
    loadChipOnMap(map, activeChip, { requireInBounds: true })
      .catch(function(err) {
        console.warn('[streetzim] search-this-area chip refetch failed:', err);
      });
    return;
  }
  if (!_findResultsState.items || !_findResultsState.items.length) return;
  // What the reader can see, not getBounds() (see _findVisibleRect). The
  // pill hides the search box and chip rail while it is up; put them back
  // first, or the rectangle would reach under them.
  _hideSearchAreaPill();
  var rect = _findVisibleRect(map);
  var filtered = _findResultsState.items.filter(function(r) {
    return _findRecordVisible(map, rect, r);
  });
  if (filtered.length === 0) {
    // Nothing in view — keep current state, just flash a hint.
    _hideSearchAreaPill();
    var btn = document.createElement('div');
    btn.style.cssText = (
      'position:fixed; left:50%; top:calc(14px + var(--top-inset, 0px)); transform:translateX(-50%);'
      + 'z-index:1600; padding:9px 18px;'
      + 'background:var(--szd-surface-a, rgba(255,255,255,0.95)); color:var(--szd-warn, #a33);'
      + 'border:1px solid rgba(170,50,50,0.25); border-radius:999px;'
      + 'font:600 13px/1.2 -apple-system,system-ui,sans-serif;'
      + 'box-shadow:0 4px 14px rgba(0,0,0,0.18); pointer-events:none;'
    );
    btn.textContent = szT('find.no_results_in_area', 'No results in this area');
    document.body.appendChild(btn);
    setTimeout(function() {
      if (btn.parentNode) btn.parentNode.removeChild(btn);
    }, 1800);
    return;
  }
  // Tear down the old visuals (markers + strip) but keep the
  // session: stash a fresh stash with the filtered items and
  // re-enter the render path so all the wiring is consistent.
  var origin = _findResultsState.origin;
  // The strip's title without its count, which is what this read off the
  // strip's text before it was translatable.
  var label = _findResultsState.label || szT('find.results', 'Results');
  clearFindResults();
  try {
    sessionStorage.setItem(FIND_RESULTS_STASH_KEY, JSON.stringify({
      label: label, origin: origin, items: filtered, fromView: true,
    }));
  } catch (e2) {}
  renderFindResultsFromStash(map);
}

function _findResultsKeydown(e) {
  if (!_findResultsState.strip) return;
  var t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
            || t.tagName === 'SELECT' || t.isContentEditable)) {
    return;
  }
  var n = _findResultsState.items ? _findResultsState.items.length : 0;
  if (n === 0) return;
  var active = _findResultsState.active;
  // Escape closes the detail panel before teardown of the carousel
  // so the user can step out of the panel without losing their browse.
  if (e.key === 'Escape') {
    e.preventDefault();
    if (_findResultsState.detailPanel) {
      _hidePlaceDetail();
    } else {
      clearFindResults();
    }
    return;
  }
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
    e.preventDefault();
    _scrollCarouselTo(Math.min(n - 1, (active < 0 ? 0 : active) + 1));
  } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
    e.preventDefault();
    _scrollCarouselTo(Math.max(0, (active < 0 ? 0 : active) - 1));
  } else if (e.key === 'Enter') {
    if (active < 0) return;
    e.preventDefault();
    // Enter on a focused carousel opens the detail panel rather than
    // a small popup — less re-clicking to read the full info.
    var r = _findResultsState.items[active];
    if (r) _showPlaceDetail(_findResultsState.map, r, active);
  }
}

