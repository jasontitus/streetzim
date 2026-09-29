
// Last view, Home button and About panel.
//
// Last view: the camera (centre, zoom, and bearing/pitch when not 0) is kept
// in localStorage per map and reopened on the next visit -- unless the URL
// names a view or a place itself (#map=, #dest=, #origin=, #pin=,
// #find=results): a deep link always wins. Only a move the reader made
// (drag, pinch, wheel, keys, the zoom buttons; see initViewMemory) is
// saved; opening, deep links, Home, search and route framing are not, so merely opening the map never pins a view. The saved
// view also records map-config's centre and zoom and is dropped when they
// change: a rebuild that moves the opening view (repackage_zim.py
// --map-center, e.g. hawaii's mid-Pacific fix) keeps the name and bounds,
// and must not be masked by a view saved on the old build. The key is the map's name and
// bounds from map-config.json, not the page path: the /drive/ PWA serves
// every ZIM at the same /drive/viewer/, and a Kiwix book's path changes
// with each new release of the same region, whose saved view still fits.
// Storage can be missing or throw (Kiwix iOS's zim: scheme, private
// windows, a full quota, a sandboxed frame), so every access is guarded
// and the viewer behaves as before without it. Nothing is saved while
// driving mode moves the camera; the view it restores on exit is.
//
// Home returns to the opening view map-config.json ships (centre + zoom;
// see docs/todo-opening-view.md for why that is not a fit to the bounds).
// tests/viewer_ui_js.test.mjs runs this block against stubbed globals.
// BEGIN view-home-about

// The viewer's own release; tests/viewer_ui_js.test.mjs keeps it equal to
// streetzim/__about__.py. A ZIM's map-config.json names the release that
// built it (`generator`); the viewer is patched in later, so both are shown.
var SZ_VIEWER_VERSION = '1.0.0';
var SZ_VIEW_PREFIX = 'streetzim.view.';

function _szStorage() {
  try {
    var s = window.localStorage;
    if (!s) return null;
    s.setItem('streetzim.probe', '1');
    s.removeItem('streetzim.probe');
    return s;
  } catch (e) { return null; }
}

// Distance units. One setting for the whole viewer: the scale bar (click
// it to switch), search distances, Find cards and "Nearby", the place
// sheet, the routing panel and places.html, which reads it from
// localStorage ('streetzim.units'). Default imperial, as the scale bar
// always was.
var SZ_UNITS_KEY = 'streetzim.units';
function szReadUnit(storage) {
  try {
    var u = storage && storage.getItem(SZ_UNITS_KEY);
    return u === 'metric' || u === 'imperial' ? u : 'imperial';
  } catch (e) { return 'imperial'; }
}
function szWriteUnit(storage, unit) {
  try { if (storage) storage.setItem(SZ_UNITS_KEY, unit); } catch (e) {}
}
function szUnit() {
  var m = window.__szMap;
  return (m && m._streetzimUnit) || szReadUnit(_szStorage());
}
// 850 m / 1.4 km / 12 km, or 500 ft / 1.4 mi / 12 mi. Unit symbols lower case.
function szFormatDistance(meters, unit) {
  if (meters == null || !isFinite(meters)) return '';
  if (unit === 'imperial') {
    var feet = meters * 3.28084;
    if (feet < 1000) return Math.round(feet) + ' ft';
    var miles = meters / 1609.344;
    return (miles < 10 ? miles.toFixed(1) : Math.round(miles)) + ' mi';
  }
  if (meters < 1000) return Math.round(meters) + ' m';
  return (meters / 1000).toFixed(meters < 10000 ? 1 : 0) + ' km';
}

function _szViewKey(config) {
  var b = config && config.bounds;
  var id = (config && config.name) || '';
  if (b && b.length === 4) {
    id += '|' + b.map(function(v) { return Math.round(v * 1000) / 1000; }).join(',');
  }
  if (!id) id = location.pathname;
  return SZ_VIEW_PREFIX + id;
}

// Does the URL fragment choose the view or a place? Then it wins.
function _szHashSetsView(hash) {
  return /(?:^#|[#&?])(?:map|dest|origin|pin)=|find=results/.test(hash || '');
}

function _szNum(v, lo, hi) {
  return typeof v === 'number' && isFinite(v) && v >= lo && v <= hi;
}

// A stored view, checked: garbage, an old format or a view outside this
// map's bounds (a ZIM whose extent shrank) is ignored, not obeyed.
function _szReadView(config, storage) {
  if (!storage) return null;
  var v = null;
  try { v = JSON.parse(storage.getItem(_szViewKey(config)) || 'null'); } catch (e) { return null; }
  if (!v || !v.c || v.c.length !== 2) return null;
  if (v.h !== _szHomeFingerprint(config)) return null;   // opening view changed
  var lon = v.c[0], lat = v.c[1];
  if (!_szNum(lon, -180, 180) || !_szNum(lat, -90, 90)) return null;
  var minZ = (config && config.minZoom) || 0;
  if (!_szNum(v.z, minZ, 22)) return null;
  var b = config && config.bounds;
  if (b && b.length === 4 &&
      (lon < b[0] - 0.01 || lon > b[2] + 0.01 || lat < b[1] - 0.01 || lat > b[3] + 0.01)) return null;
  return {
    center: [lon, lat],
    zoom: v.z,
    bearing: _szNum(v.b, -360, 360) ? v.b : 0,
    pitch: _szNum(v.p, 0, 85) ? v.p : 0
  };
}

// map-config's opening view, as saved alongside a view.
function _szHomeFingerprint(config) {
  var c = (config && config.center) || [];
  return [c[0], c[1], config && config.zoom].join(',');
}

function _szWriteView(config, storage, cam) {
  if (!storage || !cam) return false;
  function r(x, n) { var f = Math.pow(10, n); return Math.round(x * f) / f; }
  var v = { c: [r(cam.lng, 5), r(cam.lat, 5)], z: r(cam.zoom, 2), h: _szHomeFingerprint(config) };
  if (Math.abs(cam.bearing || 0) >= 0.5) v.b = r(cam.bearing, 1);
  if ((cam.pitch || 0) >= 0.5) v.p = r(cam.pitch, 1);
  try { storage.setItem(_szViewKey(config), JSON.stringify(v)); return true; }
  catch (e) { return false; }
}

// The camera the map opens with: the saved view when the URL leaves the
// choice to us, else map-config.json's.
function _szOpeningCamera(config, hash, storage) {
  var home = { center: config.center, zoom: config.zoom, bearing: 0, pitch: 0 };
  if (_szHashSetsView(hash)) return home;
  return _szReadView(config, storage) || home;
}

// How far the reader can pan: exactly the built box (map-config.json's
// "bounds", which is also what tilemaker clipped every layer to). MapLibre
// keeps the whole viewport inside maxBounds, zooming in when it must, so no
// part of the screen can show the area outside the box, where there is no
// data at all -- not even the sea, whose polygons are clipped too. The
// viewer used to add 0.01 degrees on every side, and that strip was drawn as
// bare background: the sea stopped in a straight line at the edge of every
// coastal region (Monaco's east and south edges). Longitudes past 180
// (areas across the antimeridian) are passed through; MapLibre wraps them.
// A box as wide as the world (world ZIMs: -180,-85,180,85) gets none:
// MapLibre 5.23 throws in _calcMatrices on a 360-degree maxBounds ("Error
// loading map"), and there is no edge to hide anyway. Latitudes are kept
// inside Web Mercator's +-85.0511.
var SZ_MERC_LAT = 85.0511;
function _szMaxBounds(config) {
  var b = config && config.bounds;
  if (!b || b.length !== 4) return undefined;
  for (var i = 0; i < 4; i++) if (typeof b[i] !== 'number' || !isFinite(b[i])) return undefined;
  if (!(b[0] < b[2] && b[1] < b[3])) return undefined;
  if (b[2] - b[0] >= 359.9) return undefined;
  var s = Math.max(b[1], -SZ_MERC_LAT), n = Math.min(b[3], SZ_MERC_LAT);
  if (!(s < n)) return undefined;
  return [[b[0], s], [b[2], n]];
}

// A result near the box edge cannot be flown to the middle of the free map:
// maxBounds stops the camera at the edge, so flyTo's offset is lost and the
// pin can end up under the find strip, the place sheet or the search box
// (at the north edge MapLibre 5.23 even puts it above the screen). Moving
// the camera cannot help; zooming in can, because the pin's distance in
// pixels from the box edge doubles with each zoom level.
//
// y: the pin's y after the move; h: canvas height; top/bottom: px covered
// by chrome; yNorth/ySouth: the box's north and south edges projected at
// the same zoom (they may be off screen). Returns the zoom at which, centred
// on the pin with no offset, it clears the chrome by `margin` px, or null
// when it is clear already (or cannot be cleared).
function _szClearZoom(y, h, top, bottom, zoom, maxZoom, yNorth, ySouth, margin) {
  margin = margin || 40;
  if (y > top + margin / 2 && y < h - bottom - margin / 2) return null;
  var dTop = y - yNorth, dBottom = ySouth - y;   // px from each edge
  var d = Math.min(dTop, dBottom);
  var want = dBottom <= dTop ? bottom + margin : top + margin;
  if (d * 2 >= h) return zoom;                   // not at an edge: just recentre
  if (!(d > 1)) return null;
  var z = Math.min(maxZoom, zoom + Math.max(0, Math.log(want / d) / Math.LN2));
  return z;
}

// How much of the canvas the chrome covers: the search box at the top, and
// at the bottom whichever sheet is open (find strip, place details, the
// wiki panel when it is a bottom sheet).
function _szCoveredEdges(map) {
  var c = map.getCanvas().getBoundingClientRect(), top = 0, bottom = 0;
  function rect(id) {
    var el = document.getElementById(id);
    // Not offsetParent: it is null for the position:fixed sheets.
    if (!el || !el.getClientRects().length) return null;
    var r = el.getBoundingClientRect();
    return r.height > 0 ? r : null;
  }
  var sc = rect('search-container');
  if (sc) top = Math.max(0, sc.bottom - c.top);
  ['find-results-strip', 'place-detail', 'wiki-panel'].forEach(function(id) {
    var r = rect(id);
    if (r && r.top > c.top + c.height * 0.3) bottom = Math.max(bottom, c.bottom - r.top);
  });
  return { top: top, bottom: bottom };
}

// flyTo; then, if maxBounds left the target under the chrome, ease in on it.
function _szFlyToClear(map, opts) {
  var mb = map.getMaxBounds && map.getMaxBounds();
  if (!mb) { map.flyTo(opts); return; }
  // Listen first: a flyTo with duration 0 ends inside the call.
  map.once('moveend', function() {
    try {
      var c = maplibregl.LngLat.convert(opts.center);
      var h = map.getCanvas().clientHeight, e = _szCoveredEdges(map);
      var z = _szClearZoom(map.project(c).y, h, e.top, e.bottom, map.getZoom(),
                           map.getMaxZoom(), map.project([c.lng, mb.getNorth()]).y,
                           map.project([c.lng, mb.getSouth()]).y);
      if (z !== null) map.easeTo({ center: c, zoom: z, duration: 300 });
    } catch (err) {}
  });
  map.flyTo(opts);
}
window.__szFlyToClear = _szFlyToClear;   // for tests and probes

function _szDriving() {
  var hud = document.getElementById('drive-hud');
  return !!(hud && hud.classList && hud.classList.contains('visible'));
}

function initViewMemory(map, config) {
  var storage = _szStorage();
  if (!storage) return;
  var timer = null;
  // Whose move is it? MapLibre marks most gesture moves with originalEvent,
  // but not all: a plain mouse-wheel notch and the snap back to north after
  // a small rotate come without one. So the reader's own input on the map
  // (press, release, wheel, key) counts too while it is recent. A camera
  // call from code (flyTo, Home's easeTo, deep links, route fitting) is
  // caught at the call, so it is never saved, even when it cuts into a drag.
  var byUser = false;   // the move in progress is the reader's
  var prog = false;     // a camera call from code is running
  var held = false;     // a pointer or finger is down on the map
  var userAt = 0;       // time of the reader's last input on the map
  var ending = false;   // inside a reader move's moveend, from which
                        // MapLibre may snap to north as part of that move
  function recent() { return held || Date.now() - userAt < 350; }
  function save() {
    clearTimeout(timer);
    timer = null;
    if (_szDriving()) return;
    var c = map.getCenter();
    _szWriteView(config, storage, {
      lng: c.lng, lat: c.lat, zoom: map.getZoom(),
      bearing: map.getBearing(), pitch: map.getPitch()
    });
  }
  var el = map.getCanvasContainer && map.getCanvasContainer();
  if (el && el.addEventListener) {
    var press = function(e) {
      userAt = Date.now();
      if (e.type !== 'wheel' && e.type !== 'keydown') held = true;
    };
    ['pointerdown', 'mousedown', 'touchstart', 'wheel', 'keydown'].forEach(function(t) {
      el.addEventListener(t, press, { capture: true, passive: true });
    });
    var release = function() { if (held) { held = false; userAt = Date.now(); } };
    ['pointerup', 'mouseup', 'touchend', 'touchcancel', 'pointercancel'].forEach(function(t) {
      window.addEventListener(t, release, true);
    });
  }
  ['easeTo', 'flyTo', 'jumpTo'].forEach(function(name) {
    var orig = map[name];
    if (typeof orig !== 'function') return;
    map[name] = function(opts, eventData) {
      if (!(eventData && eventData.originalEvent) && !ending) {
        // Keep the reader's last view (a pending save) before code moves on.
        if (timer) save();
        prog = true;
        byUser = false;
      }
      var r = orig.apply(this, arguments);
      // A call that did not move (or already finished) leaves no moveend
      // to clear the flag with.
      if (prog && typeof map.isMoving === 'function' && !map.isMoving()) prog = false;
      return r;
    };
  });
  map.on('movestart', function(e) {
    clearTimeout(timer);
    timer = null;
    if (e && e.originalEvent) prog = false;   // the reader took over
    byUser = !prog && !!((e && e.originalEvent) || recent());
  });
  map.on('moveend', function(e) {
    var user = !prog && (byUser || !!(e && e.originalEvent) || recent());
    prog = false;
    byUser = false;
    clearTimeout(timer);
    timer = null;
    if (!user) return;
    ending = true;
    setTimeout(function() { ending = false; }, 0);
    // Read the camera once it has settled (after any snap to north).
    timer = setTimeout(save, 400);
  });
  // A reader closed within the debounce still keeps where it was.
  window.addEventListener('pagehide', function() { if (timer) save(); });
  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'hidden' && timer) save();
  });
}

function initHomeButton(map, config) {
  var ctrl = {
    onAdd: function() {
      var div = document.createElement('div');
      div.className = 'maplibregl-ctrl maplibregl-ctrl-group';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'sz-home-btn';
      btn.title = 'Show the whole map';
      btn.setAttribute('aria-label', 'Show the whole map');
      btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' +
        '<path d="M12 3 2 12h3v8h6v-6h2v6h6v-8h3z"/></svg>';
      btn.addEventListener('click', function() {
        // Driving mode's follow camera owns the view until EXIT.
        if (_szDriving()) return;
        map.easeTo({ center: config.center, zoom: config.zoom, bearing: 0, pitch: 0, duration: 800 });
      });
      div.appendChild(btn);
      return div;
    },
    onRemove: function() {}
  };
  map.addControl(ctrl, 'top-right');
}

// "July 2026" from "2026-07-14" / "2026/07"; anything else as given.
function _szMonth(d) {
  var m = /^(\d{4})[-/](\d{1,2})/.exec(String(d || ''));
  if (!m) return d ? String(d) : '';
  var names = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
               'August', 'September', 'October', 'November', 'December'];
  var i = parseInt(m[2], 10) - 1;
  return names[i] ? names[i] + ' ' + m[1] : String(d);
}

// Text for the About panel. Newer ZIMs carry title/description/
// generator in map-config.json (streetzim/zim_writer.py _add_map_config);
// older ones only name and buildDate, and every field is optional.
// The satellite imagery's credit (EOX requires it wherever the imagery is
// shown) and licence. map-config.json names them since the source became a
// choice (streetzim/satellite_sources.py); a ZIM from before carries the
// 2021 mosaic, CC BY-NC-SA 4.0. null without a satellite layer.
function _szSatellite(config) {
  if (!config || !config.hasSatellite) return null;
  if (!config.satelliteAttribution) {
    return { attribution: 'Sentinel-2 cloudless - https://s2maps.eu by EOX IT Services GmbH' +
             ' (Contains modified Copernicus Sentinel data 2021)',
             license: 'CC BY-NC-SA 4.0', licenseUrl: 'https://creativecommons.org/licenses/by-nc-sa/4.0/',
             nonCommercial: true };
  }
  return { attribution: String(config.satelliteAttribution),
           license: String(config.satelliteLicense || ''),
           licenseUrl: String(config.satelliteLicenseUrl || ''),
           year: String(config.satelliteYear || ''),
           nonCommercial: !!config.satelliteNonCommercial };
}

function _szEsc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// The short credit on the map itself while the imagery is shown (the full
// one is in About): links are fixed EOX addresses, and the licence link only
// a Creative Commons 4.0 URL; everything taken from map-config is escaped.
function _szSatelliteCreditHtml(config) {
  var sat = _szSatellite(config);
  if (!sat) return '';
  var year = sat.year || (config.satelliteAttribution ? '' : '2021');
  var lic = _szEsc(sat.license);
  if (/^https:\/\/creativecommons\.org\/licenses\/[a-z-]+\/4\.0\/$/.test(sat.licenseUrl)) {
    lic = '<a href="' + sat.licenseUrl + '" target="_blank" rel="noopener">' + lic + '</a>';
  }
  return '&copy; <a href="https://cloudless.eox.at" target="_blank" rel="noopener">EOxCloudless</a>' +
    (year ? ' ' + _szEsc(year) : '') +
    ' by <a href="https://eox.at" target="_blank" rel="noopener">EOX</a> &middot; ' + lic +
    (sat.nonCommercial ? ' (non-commercial)' : '');
}

function _szAboutText(config) {
  config = config || {};
  var meta = [];
  // buildDate is when the ZIM was built, not the OSM data's timestamp.
  var when = _szMonth(config.buildDate);
  if (when || config.generator) {
    meta.push('Built' + (when ? ' ' + when : '') + (config.generator ? ' with ' + config.generator : ''));
  }
  meta.push('viewer: streetzim ' + SZ_VIEWER_VERSION);
  var sat = _szSatellite(config);
  return {
    title: config.title || config.name || 'Offline OpenStreetMap',
    desc: config.description || '',
    meta: meta.join(' · '),
    // A ZIM with non-commercial imagery says so first thing in About.
    notice: sat && sat.nonCommercial ?
      'Restricted: the satellite imagery is licensed ' + sat.license +
      ' and may be used for non-commercial purposes only; the rest of this map is openly licensed.' : '',
    satCredit: sat ? sat.attribution : '',
    satLicense: sat ? sat.license + (sat.nonCommercial ? ', non-commercial use only' : '') +
      (sat.licenseUrl ? ' \u2014 ' + sat.licenseUrl.replace(/^https?:\/\//, '') : '') : ''
  };
}

function initAbout(config) {
  var t = _szAboutText(config);
  var set = function(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  set('about-title', t.title);
  set('about-desc', t.desc);
  set('about-meta', t.meta);
  set('about-notice', t.notice);
  var notice = document.getElementById('about-notice');
  if (notice) notice.style.display = t.notice ? '' : 'none';
  if (t.satCredit) {
    set('attr-satellite-by', t.satCredit);
    set('attr-satellite-license', t.satLicense);
  }
  // Focus moves into the dialog on open, Tab stays inside it, and focus
  // returns to the button however it closes (it is aria-modal); Escape
  // closes it without reaching the other Escape handlers (find results,
  // routing pickers) underneath.
  var overlay = document.getElementById('attr-overlay');
  var dialog = document.getElementById('attr-dialog');
  var btn = document.getElementById('attr-btn');
  var closeBtn = document.getElementById('attr-close');
  if (!overlay) return;
  var isOpen = function() { return overlay.style.display === 'block'; };
  var refocus = function() { if (btn && btn.focus) btn.focus(); };
  if (btn) btn.addEventListener('click', function() {
    // After 120's listener has shown the overlay.
    setTimeout(function() { if (closeBtn && closeBtn.focus && isOpen()) closeBtn.focus(); }, 0);
  });
  if (closeBtn) closeBtn.addEventListener('click', refocus);
  overlay.addEventListener('click', function(e) { if (e.target === overlay) refocus(); });
  document.addEventListener('keydown', function(e) {
    if (!isOpen()) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopImmediatePropagation();
      overlay.style.display = 'none';
      refocus();
    } else if (e.key === 'Tab' && dialog && dialog.querySelectorAll) {
      var f = Array.prototype.filter.call(
        dialog.querySelectorAll('button, a[href], [tabindex]:not([tabindex="-1"])'),
        function(x) { return x.offsetParent !== null; });
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1], at = document.activeElement;
      var inside = dialog.contains && dialog.contains(at);
      if (e.shiftKey && (at === first || !inside)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (at === last || !inside)) { e.preventDefault(); first.focus(); }
    }
  }, true);
}
// END view-home-about
