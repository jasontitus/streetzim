// Dark map theme. makeStyle() builds the light style; when the reader
// prefers a dark colour scheme, _szThemeStyle() overwrites the colours
// below layer by layer. Only paint values change -- layer ids, filters,
// order and zooms are the light style's, so search, chips, satellite and
// driving mode (which address layers by id) see the same map either way.
// tests/viewer_style_js.test.mjs fails if a light layer gains a colour
// this table does not darken, or the table names a layer that is gone.
//
// Chosen by, in order: ?theme=dark|light in the URL; light when the host
// already inverts the page (Kiwix JS's "invert" dark mode filters the
// article frame, so a dark map would come out light); else
// prefers-color-scheme, followed live when it changes.
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

function _szPrefersDark() {
  var forced = /[?&]theme=(dark|light)(?:&|$)/.exec(location.search || '');
  if (forced) return forced[1] === 'dark';
  if (_szHostInvertsContent()) return false;
  try {
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  } catch (e) { return false; }
}

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

// Follow a live change of the OS/browser scheme (evening auto-dark) without
// setStyle(), which would drop every runtime layer (satellite, hillshade,
// routes, find pins). Repaints the base layers from a fresh makeStyle(),
// then fires 'streetzim.theme' so satellite mode can re-apply its overrides.
function initMapTheme(map, config) {
  if (/[?&]theme=(dark|light)(?:&|$)/.test(location.search || '')) return;
  var mq = null;
  try { mq = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)'); } catch (e) {}
  if (!mq) return;
  var current = _szPrefersDark();
  function onChange() {
    var dark = _szPrefersDark();
    if (dark === current) return;
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
  if (mq.addEventListener) mq.addEventListener('change', onChange);
  else if (mq.addListener) mq.addListener(onChange);   // Safari < 14
}
// END map-theme
