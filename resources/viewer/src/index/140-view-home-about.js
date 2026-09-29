
// Last view, Home button and About panel.
//
// Last view: the camera (centre, zoom, and bearing/pitch when not 0) is kept
// in localStorage per map and reopened on the next visit -- unless the URL
// names a view or a place itself (#map=, #dest=, #origin=, #pin=,
// #find=results): a deep link always wins. The key is the map's name and
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

function _szWriteView(config, storage, cam) {
  if (!storage || !cam) return false;
  function r(x, n) { var f = Math.pow(10, n); return Math.round(x * f) / f; }
  var v = { c: [r(cam.lng, 5), r(cam.lat, 5)], z: r(cam.zoom, 2) };
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

function _szDriving() {
  var hud = document.getElementById('drive-hud');
  return !!(hud && hud.classList && hud.classList.contains('visible'));
}

function initViewMemory(map, config) {
  var storage = _szStorage();
  if (!storage) return;
  var timer = null;
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
  map.on('moveend', function() {
    clearTimeout(timer);
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

// Text for the About panel. Newer ZIMs carry title/description/date/
// generator in map-config.json (streetzim/zim_writer.py _add_map_config);
// older ones only name and buildDate, and every field is optional.
function _szAboutText(config) {
  config = config || {};
  var meta = [];
  var when = _szMonth(config.date || config.buildDate);
  if (when) meta.push('Map data from ' + when);
  if (config.generator) meta.push('built with ' + config.generator);
  meta.push('viewer: streetzim ' + SZ_VIEWER_VERSION);
  return {
    title: config.title || config.name || 'Offline OpenStreetMap',
    desc: config.description || '',
    meta: meta.join(' · ')
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
  var overlay = document.getElementById('attr-overlay');
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && overlay && overlay.style.display === 'block') overlay.style.display = 'none';
  });
}
// END view-home-about
