// Dark map theme. makeStyle() builds the light style; when the reader
// prefers a dark colour scheme, _szThemeStyle() overwrites the colours
// below layer by layer. Only paint values change -- layer ids, filters,
// order and zooms are the light style's, so search, chips, satellite and
// driving mode (which address layers by id) see the same map either way.
// tests/viewer_style_js.test.mjs fails if a light layer gains a colour
// this table does not darken, or the table names a layer that is gone.
//
// Chosen by, in order: ?theme=dark|light in the URL (for that page load);
// the reader's choice from the theme button (szThemeButton, under Home:
// Auto -> Light -> Dark), kept in localStorage 'streetzim.theme'; then
// Auto: light when the host already inverts the page (Kiwix JS's "invert"
// dark mode filters the article frame, so a dark map would come out
// light), else prefers-color-scheme, followed live when it changes.
// places.html reads the same key for its own colours.
// BEGIN map-theme
var _SZ_DARK_HALO = 'rgba(0,0,0,0.75)';
var _SZ_DARK = {
  'background': { 'background-color': '#1b1e23' },
  'landcover-grass': { 'fill-color': '#243325' },
  'landcover-wood': { 'fill-color': '#1f3524' },
  'landcover-farmland': { 'fill-color': '#262a22' },
  'landuse-residential': { 'fill-color': '#34373d' },
  'landuse-commercial': { 'fill-color': '#3b2c2e' },
  'landuse-park': { 'fill-color': '#1f3d26' },
  'water': { 'fill-color': '#1c3a50' },
  'water-lowzoom': { 'fill-color': '#1c3a50' },
  'waterway': { 'line-color': '#25506c' },
  'building': { 'fill-color': '#2d3037' },
  'building-outline': { 'line-color': '#3d4149' },
  'boundary-country': { 'line-color': '#77748c' },
  'boundary-state': { 'line-color': '#5d5b6d' },
  'aeroway-runway': { 'line-color': '#43464d' },
  'road-motorway-casing': { 'line-color': '#6e4524' },
  'road-trunk-casing': { 'line-color': '#6e4524' },
  'road-primary-casing': { 'line-color': '#5f4526' },
  'road-secondary-casing': { 'line-color': '#4d4527' },
  'road-motorway': { 'line-color': '#b8703a' },
  'road-trunk': { 'line-color': '#b8703a' },
  'road-primary': { 'line-color': '#937145' },
  'road-secondary': { 'line-color': '#77714a' },
  'road-tertiary': { 'line-color': '#50535b' },
  'road-minor': { 'line-color': '#43464d' },
  'road-track': { 'line-color': '#8f7250' },
  'road-path-paved': { 'line-color': '#8c776a' },
  'road-path': { 'line-color': '#a06e55' },
  'rail': { 'line-color': '#5c5f66' },
  'road-label': { 'text-color': '#c8c8c8', 'text-halo-color': _SZ_DARK_HALO },
  'water-label': { 'text-color': ['case', ['has', 'wikidata'], '#9dbbe6', '#7ea3cc'],
                   'text-halo-color': _SZ_DARK_HALO },
  'place-country': { 'text-color': ['case', ['has', 'wikidata'], '#c3d2ee', '#d6d6de'],
                     'text-halo-color': _SZ_DARK_HALO },
  'place-state': { 'text-color': ['case', ['has', 'wikidata'], '#b3c4e4', '#b4b4bc'],
                   'text-halo-color': _SZ_DARK_HALO },
  'place-city': { 'text-color': ['case', ['has', 'wikidata'], '#cddcf5', '#ececec'],
                  'text-halo-color': _SZ_DARK_HALO },
  'place-town': { 'text-color': ['case', ['has', 'wikidata'], '#bccdea', '#dcdcdc'],
                  'text-halo-color': _SZ_DARK_HALO },
  'place-village': { 'text-color': ['case', ['has', 'wikidata'], '#b3c6e6', '#cdcdcd'],
                     'text-halo-color': _SZ_DARK_HALO },
  'place-suburb': { 'text-color': ['case', ['has', 'wikidata'], '#a7bde0', '#b8b8b8'],
                    'text-halo-color': _SZ_DARK_HALO },
  'poi-label': { 'text-color': ['case', ['has', 'wikidata'], '#a7bde0', '#bdbdbd'],
                 'text-halo-color': _SZ_DARK_HALO }
};

function _szHostInvertsContent() {
  function inverted(el) {
    try { return !!el && /invert\(\s*(1|100%)/.test(getComputedStyle(el).filter || ''); }
    catch (e) { return false; }
  }
  var fe = null;
  try { fe = window.frameElement; } catch (e) {}   // cross-origin parent
  return inverted(fe) || inverted(document.documentElement);
}

// The reader's choice. Storage can be missing or throw (Kiwix iOS's zim:
// scheme, private windows, a sandboxed frame); anything unreadable or
// unknown is Auto, and a failed write only loses the memory, not the switch.
var SZ_THEME_KEY = 'streetzim.theme';
var SZ_THEME_MODES = ['auto', 'light', 'dark'];
function szReadThemeMode(storage) {
  try {
    var v = storage && storage.getItem(SZ_THEME_KEY);
    if (v === 'light' || v === 'dark') return v;
  } catch (e) {}
  return 'auto';
}
function szWriteThemeMode(storage, mode) {
  try {
    if (!storage) return;
    if (mode === 'light' || mode === 'dark') storage.setItem(SZ_THEME_KEY, mode);
    else storage.removeItem(SZ_THEME_KEY);
  } catch (e) {}
}
function szNextThemeMode(mode) {
  return SZ_THEME_MODES[(SZ_THEME_MODES.indexOf(mode) + 1) % SZ_THEME_MODES.length];
}
function _szThemeStore() {
  try { return window.localStorage || null; } catch (e) { return null; }
}
// 'dark' / 'light' from ?theme=, until the reader taps the theme button.
var _szThemeUrlOff = false;
function _szUrlTheme() {
  if (_szThemeUrlOff) return null;
  var m = /[?&]theme=(dark|light)(?:&|$)/.exec(location.search || '');
  return m ? m[1] : null;
}
var _szThemeMode = szReadThemeMode(_szThemeStore());
// What the button shows: the URL's theme while it rules, else the choice.
function szThemeMode() { return _szUrlTheme() || _szThemeMode; }

// Whether to build the DARK style. Decide what the reader should SEE, then
// flip it when the host inverts the page (Kiwix JS's dark mode puts
// invert(1) hue-rotate(180deg) on the frame): there the light style is what
// looks dark. Light, Dark and ?theme= all mean what the reader sees. Auto
// under an inverting host keeps its old look -- the host is in its dark
// mode, so the reader sees dark (light style, inverted); else the OS scheme.
function _szPrefersDark() {
  var inverts = _szHostInvertsContent();
  var mode = szThemeMode();
  var want;
  if (mode !== 'auto') want = mode === 'dark';
  else if (inverts) want = true;
  else {
    try {
      want = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    } catch (e) { want = false; }
  }
  return inverts ? !want : want;
}

// The page chrome follows the map: html.sz-dark switches the search box,
// chips, panels and MapLibre's controls to their dark colours (010-styles).
// Applied once as the script loads, before the map exists, so the chrome
// does not flash light, and again on every live scheme change.
function _szApplyUiTheme(dark) {
  try {
    var root = document.documentElement;
    if (dark) root.classList.add('sz-dark');
    else root.classList.remove('sz-dark');
  } catch (e) {}
}
_szApplyUiTheme(_szPrefersDark());

function _szThemeStyle(style, dark) {
  if (!dark) return style;
  style.layers.forEach(function(layer) {
    var o = _SZ_DARK[layer.id];
    if (!o) return;
    layer.paint = layer.paint || {};
    Object.keys(o).forEach(function(k) { layer.paint[k] = o[k]; });
  });
  style.name = (style.name || '') + ' (dark)';
  return style;
}

// Restyle the running map when the theme changes -- a live change of the
// OS/browser scheme (evening auto-dark) in Auto, or a tap on the theme
// button -- without setStyle(), which would drop every runtime layer
// (satellite, hillshade, wiki dots, routes, search and find pins).
// Repaints the base layers from a fresh makeStyle(), then fires
// 'streetzim.theme' so satellite mode can re-apply its overrides.
//
// A tap (or scheme change) before the style has loaded: the chrome switches
// at once, the map is repainted when its base layers exist ('styledata'),
// and `current` -- the theme the map is painted in -- only changes once the
// paint has happened, so nothing is lost and the next tap is not confused.
var _szThemeRefresh = function() { _szApplyUiTheme(_szPrefersDark()); };
function initMapTheme(map, config) {
  var current = _szPrefersDark();
  var waiting = false;
  function styleReady() {
    try { return !!map.getLayer('background'); } catch (e) { return false; }
  }
  function refresh() {
    var dark = _szPrefersDark();
    _szApplyUiTheme(dark);
    if (dark === current) return;
    if (!styleReady()) {
      if (!waiting) {
        waiting = true;
        // 'styledata' can come before the layers exist; refresh() then
        // simply waits again.
        var again = function() { waiting = false; refresh(); };
        map.once('styledata', again);
      }
      return;
    }
    current = dark;
    var style = makeStyle(config);
    style.layers.forEach(function(layer) {
      if (!layer.paint || !map.getLayer(layer.id)) return;
      Object.keys(layer.paint).forEach(function(k) {
        try { map.setPaintProperty(layer.id, k, layer.paint[k]); }
        catch (e) { dbg('theme paint failed', layer.id, k, describeError(e)); }
      });
    });
    map.fire('streetzim.theme', { dark: dark });
    map.triggerRepaint();
  }
  _szThemeRefresh = refresh;
  // Another tab (or places.html) changed the choice: follow it here too.
  try {
    window.addEventListener('storage', function(e) {
      if (e && e.key !== SZ_THEME_KEY && e.key !== null) return;
      _szThemeMode = szReadThemeMode(_szThemeStore());
      _szThemeButtons.forEach(_szThemeButtonShow);
      refresh();
    });
  } catch (e) {}
  var mq = null;
  try { mq = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)'); } catch (e) {}
  if (!mq) return;
  if (mq.addEventListener) mq.addEventListener('change', refresh);
  else if (mq.addListener) mq.addListener(refresh);   // Safari < 14
}

// Choose a mode (the button). A tap ends the URL's ?theme= for this page:
// the reader asked for something else.
function szSetThemeMode(mode) {
  _szThemeUrlOff = true;
  _szThemeMode = (mode === 'light' || mode === 'dark') ? mode : 'auto';
  szWriteThemeMode(_szThemeStore(), _szThemeMode);
  _szThemeRefresh();
}

// The switch: one square in the Home button's group (initHomeButton, 140),
// so it adds a button's height, not another group, to the top-right column.
// The icon shows the current mode: half-filled circle (auto), sun, moon.
var _SZ_THEME_ICON = {
  auto: '<path fill-rule="evenodd" d="M12 2a10 10 0 1 0 0 20 10 10 0 1 0 0-20zm0 2v16a8 8 0 1 1 0-16z"/>',
  light: '<circle cx="12" cy="12" r="4.5"/>' + [0, 45, 90, 135, 180, 225, 270, 315].map(function(a) {
    return '<rect x="11" y="1" width="2" height="3.5" rx="1" transform="rotate(' + a + ' 12 12)"/>';
  }).join(''),
  dark: '<path d="M20.5 14.6A8.6 8.6 0 0 1 9.4 3.5a8.6 8.6 0 1 0 11.1 11.1z"/>'
};
var _SZ_THEME_LABEL = {
  auto: 'Map theme: auto (follows the system)',
  light: 'Map theme: light',
  dark: 'Map theme: dark'
};
function _szThemeButtonShow(btn) {
  var mode = szThemeMode();
  btn.setAttribute('data-mode', mode);
  btn.title = _SZ_THEME_LABEL[mode];
  btn.setAttribute('aria-label', _SZ_THEME_LABEL[mode]);
  btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + _SZ_THEME_ICON[mode] + '</svg>';
}
var _szThemeButtons = [];
function szThemeButton() {
  var btn = document.createElement('button');
  _szThemeButtons.push(btn);
  btn.type = 'button';
  btn.className = 'sz-theme-btn';
  _szThemeButtonShow(btn);
  btn.addEventListener('click', function() {
    szSetThemeMode(szNextThemeMode(szThemeMode()));
    _szThemeButtonShow(btn);
  });
  return btn;
}
// END map-theme
