// The map style the viewer ships: the dark theme (135-map-theme.js), the POI
// icons (136-poi-icons.js) and the lazy RTL text plugin (137-rtl-text.js).
// Runs the code straight out of resources/viewer/index.html (the copy ZIMs
// and the in-place patcher ship) against stubbed browser globals.
//
//   node tests/viewer_style_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const HTML = fs.readFileSync(`${REPO}/resources/viewer/index.html`, 'utf8');
const LUA = fs.readFileSync(`${REPO}/resources/tilemaker/process-openmaptiles.lua`, 'utf8');

let pass = 0;
async function ok(name, fn) {
  try { await fn(); console.log('ok   ', name); pass++; }
  catch (e) { console.error('FAIL ', name, '\n      ', e.stack || e.message); process.exitCode = 1; }
}

// makeStyle (130) through the RTL block (137) are contiguous in index.html.
const a = HTML.indexOf('var _SZ_LAKES');
const b = HTML.indexOf('// END rtl-text');
assert.ok(a >= 0 && b > a, 'style/theme/icon/rtl blocks not found in index.html');
const SRC = HTML.slice(a, b);

// A fresh copy of the code with its own globals. `env` stubs what the
// browser would provide.
function load(env = {}) {
  const search = env.search || '';
  const dark = !!env.dark;
  const listeners = [];
  const mq = {
    get matches() { return env.darkNow ? env.darkNow() : dark; },
    addEventListener: (t, f) => listeners.push(f),
  };
  const window = {
    matchMedia: () => mq,
    frameElement: env.frameElement || null,
    devicePixelRatio: env.dpr || 1,
    __szFetchWithRetry: env.fetcher,
  };
  const document = {
    documentElement: { tag: 'html' },
    createElement: env.createElement || (() => ({ getContext: () => null })),
  };
  const getComputedStyle = (el) => ({ filter: (env.filters || {})[el && el.tag] || 'none' });
  const logs = [];
  const fn = new Function('window', 'document', 'location', 'getComputedStyle',
    'baseUrl', 'dbg', 'describeError', 'Path2D', 'fetch',
    SRC + '\nreturn { makeStyle, _SZ_DARK, _SZ_MAKI, _SZ_POI_ICON, _SZ_POI_GROUP,' +
    ' _szPoiIconExpr, _szPrefersDark, _szThemeStyle, initMapTheme, initPoiIcons,' +
    ' _szRenderPoiIcon, initRtlText };');
  const api = fn(window, document, { search }, getComputedStyle, 'http://zim/C/',
    (...x) => logs.push(x), (e) => String(e && e.message || e), env.Path2D, env.fetch);
  return { ...api, window, mq, listeners, logs };
}
const CONFIG = { minZoom: 0, maxZoom: 14 };

function layerMap(style) { return Object.fromEntries(style.layers.map(l => [l.id, l])); }

// Evaluate the subset of style expressions these layers use.
function evalExpr(e, props, zoom = 16) {
  if (!Array.isArray(e)) return e;
  const [op, ...args] = e;
  switch (op) {
    case 'literal': return args[0];
    case 'get': return props[args[0]] ?? null;
    case 'has': return args[0] in props;
    case 'zoom': return zoom;
    case '==': return evalExpr(args[0], props, zoom) === evalExpr(args[1], props, zoom);
    case '<=': return evalExpr(args[0], props, zoom) <= evalExpr(args[1], props, zoom);
    case '>=': return evalExpr(args[0], props, zoom) >= evalExpr(args[1], props, zoom);
    case 'all': return args.every(x => evalExpr(x, props, zoom));
    case 'any': return args.some(x => evalExpr(x, props, zoom));
    case 'case': {
      for (let i = 0; i + 1 < args.length; i += 2) if (evalExpr(args[i], props, zoom)) return evalExpr(args[i + 1], props, zoom);
      return evalExpr(args[args.length - 1], props, zoom);
    }
    case 'match': {
      const v = evalExpr(args[0], props, zoom);
      for (let i = 1; i + 1 < args.length; i += 2) {
        const labels = Array.isArray(args[i]) ? args[i] : [args[i]];
        if (labels.includes(v)) return evalExpr(args[i + 1], props, zoom);
      }
      return evalExpr(args[args.length - 1], props, zoom);
    }
    case 'step': {
      const x = evalExpr(args[0], props, zoom);
      let out = evalExpr(args[1], props, zoom);
      for (let i = 2; i + 1 < args.length; i += 2) if (x >= args[i]) out = evalExpr(args[i + 1], props, zoom);
      return out;
    }
    default: throw new Error('unsupported op in test evaluator: ' + op);
  }
}

// ---- WCAG contrast --------------------------------------------------------
function rgb(c) {
  let m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(c);
  if (m) {
    let h = m[1]; if (h.length === 3) h = h.replace(/./g, x => x + x);
    return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16));
  }
  m = /^rgba?\(([^)]+)\)$/.exec(c);
  if (m) return m[1].split(',').slice(0, 3).map(Number);
  throw new Error('colour? ' + c);
}
function lum(c) {
  return rgb(c).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; })
    .reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0);
}
function contrast(x, y) { const [h, l] = [lum(x), lum(y)].sort((p, q) => q - p); return (h + 0.05) / (l + 0.05); }
function colours(v) {   // every literal colour a paint value can produce
  if (typeof v === 'string') return [v];
  if (Array.isArray(v) && v[0] === 'case') return v.slice(1).filter((_, i, arr) => i % 2 === 1 || i === arr.length - 1).flatMap(colours);
  return [];
}

// ========================================================================
// 1. Dark theme
// ========================================================================
await ok('theme: light by default, dark when the scheme is dark', () => {
  assert.strictEqual(load().makeStyle(CONFIG).layers[0].paint['background-color'], '#f8f4f0');
  const d = load({ dark: true });
  assert.strictEqual(d.makeStyle(CONFIG).layers[0].paint['background-color'], d._SZ_DARK.background['background-color']);
});

await ok('theme: ?theme= forces either way; an inverting host (Kiwix JS) gets light', () => {
  assert.strictEqual(load({ dark: true, search: '?theme=light' })._szPrefersDark(), false);
  assert.strictEqual(load({ dark: false, search: '?debug=1&theme=dark' })._szPrefersDark(), true);
  const frame = { tag: 'iframe' };
  assert.strictEqual(load({ dark: true, frameElement: frame, filters: { iframe: 'invert(1) hue-rotate(180deg)' } })._szPrefersDark(), false);
  assert.strictEqual(load({ dark: true, filters: { html: 'invert(100%)' } })._szPrefersDark(), false);
});

await ok('theme: every colour in the light style has a dark value (no light patch left behind)', () => {
  const { makeStyle, _SZ_DARK } = load();
  const missing = [];
  for (const l of makeStyle(CONFIG).layers) {
    for (const k of Object.keys(l.paint || {})) {
      if (/-color$/.test(k) && !(_SZ_DARK[l.id] && k in _SZ_DARK[l.id])) missing.push(`${l.id}.${k}`);
    }
  }
  assert.deepStrictEqual(missing, []);
});

await ok('theme: the dark table names only layers and properties the light style has', () => {
  const { makeStyle, _SZ_DARK } = load();
  const L = layerMap(makeStyle(CONFIG));
  for (const [id, o] of Object.entries(_SZ_DARK)) {
    assert.ok(L[id], `dark table names missing layer ${id}`);
    for (const k of Object.keys(o)) assert.ok(k in (L[id].paint || {}), `${id} has no paint ${k}`);
  }
});

await ok('theme: only paint changes -- ids, order, filters, layout identical', () => {
  const light = load().makeStyle(CONFIG), dark = load({ dark: true }).makeStyle(CONFIG);
  const strip = s => JSON.stringify(s.layers.map(l => ({ ...l, paint: undefined })));
  assert.strictEqual(strip(dark), strip(light));
  assert.notStrictEqual(JSON.stringify(dark), JSON.stringify(light));
});

await ok('theme: dark labels keep >= 4.5:1 contrast on the dark background', () => {
  const s = load({ dark: true }).makeStyle(CONFIG);
  const bg = s.layers[0].paint['background-color'];
  for (const l of s.layers.filter(x => x.type === 'symbol')) {
    for (const c of colours(l.paint['text-color'])) {
      const r = contrast(c, bg);
      assert.ok(r >= 4.5, `${l.id} ${c} on ${bg}: ${r.toFixed(2)}`);
    }
  }
});

await ok('theme: a live scheme change repaints base layers and tells satellite mode', () => {
  let darkNow = false;
  const env = load({ darkNow: () => darkNow });
  const set = [], fired = [];
  const map = {
    getLayer: (id) => id !== 'water-lowzoom' ? { id } : undefined,  // one layer absent
    setPaintProperty: (id, k, v) => set.push([id, k, v]),
    fire: (t) => fired.push(t), triggerRepaint() {},
  };
  env.initMapTheme(map, CONFIG);
  assert.strictEqual(env.listeners.length, 1);
  env.listeners[0]();                       // no change -> nothing
  assert.strictEqual(set.length, 0);
  darkNow = true; env.listeners[0]();
  assert.ok(set.some(([id, k, v]) => id === 'background' && v === env._SZ_DARK.background[k]));
  assert.ok(!set.some(([id]) => id === 'water-lowzoom'), 'painted a layer the map does not have');
  assert.deepStrictEqual(fired, ['streetzim.theme']);
});

await ok('theme: satellite mode re-applies its overrides after a theme change', () => {
  const i = HTML.indexOf("map.on('streetzim.theme'");
  assert.ok(i > 0, 'satellite code does not listen for streetzim.theme');
  assert.match(HTML.slice(i, i + 200), /satSavedPaint = null;[\s\S]*showSatellite\(\)/);
});

// ========================================================================
// 2. POI icons
// ========================================================================
await ok('icons: every mapped icon has a Maki path and a colour group; none unused', () => {
  const { _SZ_MAKI, _SZ_POI_ICON, _SZ_POI_GROUP } = load();
  const used = new Set(Object.values(_SZ_POI_ICON));
  const grouped = new Set(Object.values(_SZ_POI_GROUP).flat());
  for (const icon of used) {
    assert.ok(_SZ_MAKI[icon], `no path for ${icon}`);
    assert.ok(grouped.has(icon), `no colour group for ${icon}`);
  }
  for (const icon of Object.keys(_SZ_MAKI)) assert.ok(used.has(icon), `${icon} shipped but never used`);
  for (const [icon, d] of Object.entries(_SZ_MAKI)) {
    assert.match(d, /^[Mm][MmLlHhVvCcSsQqTtAaZz0-9eE.,\s-]+$/, `${icon}: not a plain SVG path`);
  }
});

await ok('icons: every OMT class the tile profile writes gets an icon', () => {
  const block = /poiClasses\s*=\s*\{([\s\S]*?)\}/.exec(LUA)[1];
  const classes = new Set([...block.matchAll(/=\s*"([a-z_]+)"/g)].map(m => m[1]));
  assert.ok(classes.size >= 25, 'did not parse poiClasses');
  const { _SZ_POI_ICON } = load();
  assert.deepStrictEqual([...classes].filter(c => !_SZ_POI_ICON[c]), []);
});

await ok('icons: poi-label resolves subclass, then class, else no icon', () => {
  const L = layerMap(load().makeStyle(CONFIG));
  const lay = L['poi-label'].layout;
  const icon = (p) => evalExpr(lay['icon-image'], p);
  assert.strictEqual(icon({ class: 'amenity', subclass: 'restaurant' }), 'sz-poi-restaurant');
  assert.strictEqual(icon({ class: 'cafe', subclass: 'cafe' }), 'sz-poi-cafe');
  assert.strictEqual(icon({ class: 'lodging', subclass: 'chalet' }), 'sz-poi-lodging');
  assert.strictEqual(icon({ class: 'place_of_worship', subclass: 'muslim' }), 'sz-poi-religious-muslim');
  assert.strictEqual(icon({ class: 'place_of_worship', subclass: 'shinto' }), 'sz-poi-place-of-worship');
  assert.strictEqual(icon({ class: 'building' }), '');
  // label clears the disc only when there is one
  assert.deepStrictEqual(evalExpr(lay['text-offset'], { class: 'cafe', subclass: 'cafe' }), [0, 0.85]);
  assert.deepStrictEqual(evalExpr(lay['text-offset'], { class: 'building' }), [0, 0.5]);
});

await ok('icons: match labels are unique strings (MapLibre rejects duplicates)', () => {
  const walk = (e) => {
    if (!Array.isArray(e) || e[0] !== 'match') return;
    const labels = e.slice(2, -1).filter((_, i) => i % 2 === 0);
    assert.strictEqual(new Set(labels).size, labels.length);
    labels.forEach(l => assert.strictEqual(typeof l, 'string'));
    walk(e[e.length - 1]);
  };
  walk(load()._szPoiIconExpr());
});

await ok('icons: nameless POIs only from z16; rank thresholds unchanged', () => {
  const f = layerMap(load().makeStyle(CONFIG))['poi-label'].filter;
  assert.strictEqual(evalExpr(f, { rank: 3, name: 'X' }, 14), true);
  assert.strictEqual(evalExpr(f, { rank: 8, name: 'X' }, 14), false);
  assert.strictEqual(evalExpr(f, { rank: 8, name: 'X' }, 15), true);
  assert.strictEqual(evalExpr(f, { rank: 3 }, 15), false);
  assert.strictEqual(evalExpr(f, { rank: 25 }, 16), true);
});

await ok('icons: drawn on demand at the capped pixel ratio; foreign ids ignored', () => {
  const ops = [];
  const ctx = new Proxy({}, {
    get: (_, k) => k === 'getImageData' ? (x, y, w, h) => ({ width: w, height: h })
      : (...args) => ops.push([k, ...args]),
    set: () => true,
  });
  class P2D { constructor(d) { this.d = d; } }
  const env = load({ Path2D: P2D, dpr: 3, createElement: () => ({ getContext: () => ctx }) });
  const handlers = {}, added = [];
  const map = { on: (t, f) => { handlers[t] = f; }, hasImage: () => false,
                addImage: (id, img, opt) => added.push([id, img, opt]) };
  env.initPoiIcons(map);
  handlers.styleimagemissing({ id: 'some-other-image' });
  handlers.styleimagemissing({ id: 'sz-poi-not-an-icon' });
  assert.strictEqual(added.length, 0);
  handlers.styleimagemissing({ id: 'sz-poi-cafe' });
  assert.strictEqual(added.length, 1);
  assert.strictEqual(added[0][0], 'sz-poi-cafe');
  assert.deepStrictEqual(added[0][2], { pixelRatio: 2 });
  assert.strictEqual(added[0][1].width, 34);
  assert.ok(ops.some(([k, arg]) => k === 'fill' && arg instanceof P2D && arg.d === env._SZ_MAKI.cafe));
});

await ok('icons: no Path2D (old WebView) -> label only, no throw, no retry storm', () => {
  const env = load({ Path2D: undefined });
  const handlers = {}; let added = 0;
  env.initPoiIcons({ on: (t, f) => { handlers[t] = f; }, hasImage: () => false, addImage: () => added++ });
  handlers.styleimagemissing({ id: 'sz-poi-cafe' });
  handlers.styleimagemissing({ id: 'sz-poi-cafe' });
  assert.strictEqual(added, 0);
});

// ========================================================================
// 3. RTL text plugin
// ========================================================================
function rtlEnv(status, cfg, fetchResult) {
  const calls = { fetched: [], set: [], off: 0 };
  const lib = {
    getRTLTextPluginStatus: () => status.v,
    setRTLTextPlugin: (url, lazy) => { calls.set.push([url, lazy]); return Promise.resolve(); },
  };
  const env = load({
    fetcher: (url, signal) => { calls.fetched.push(url); assert.ok(signal); return fetchResult || Promise.resolve({ data: new ArrayBuffer(4) }); },
  });
  const handlers = {};
  const map = { on: (t, f) => { handlers[t] = f; }, off: () => { calls.off++; } };
  const r = env.initRtlText(map, cfg, lib);
  return { r, calls, handlers, env };
}
const tick = () => new Promise(r => setTimeout(r, 0));

await ok('rtl: a ZIM without the plugin (or a bad path) does nothing', () => {
  for (const cfg of [{}, { rtlTextPlugin: '' }, { rtlTextPlugin: '../x.js' },
                     { rtlTextPlugin: 'https://cdn/x.js' }, { rtlTextPlugin: '/x.js' }]) {
    const { r, calls, handlers } = rtlEnv({ v: 'requested' }, cfg);
    assert.strictEqual(r, false, JSON.stringify(cfg));
    assert.strictEqual(calls.fetched.length, 0);
    assert.strictEqual(handlers.data, undefined);
  }
});

await ok('rtl: nothing is fetched until MapLibre meets RTL text', async () => {
  const status = { v: 'unavailable' };
  const { r, calls, handlers } = rtlEnv(status, { rtlTextPlugin: 'mapbox-gl-rtl-text.js' });
  assert.strictEqual(r, true);
  handlers.data(); handlers.data();
  await tick();
  assert.strictEqual(calls.fetched.length, 0);
  status.v = 'requested';
  handlers.data(); handlers.data();
  await tick(); await tick();
  assert.deepStrictEqual(calls.fetched, ['http://zim/C/mapbox-gl-rtl-text.js']);
  assert.strictEqual(calls.set.length, 1);
  assert.match(calls.set[0][0], /^blob:/);
  assert.strictEqual(calls.set[0][1], false);
  assert.strictEqual(calls.off, 1);
});

await ok('rtl: a failed fetch leaves labels as they were and does not throw', async () => {
  const { calls, handlers, env } = rtlEnv({ v: 'requested' }, { rtlTextPlugin: 'mapbox-gl-rtl-text.js' },
                                          Promise.reject(new Error('HTTP 404')));
  handlers.data();
  await tick(); await tick();
  assert.strictEqual(calls.set.length, 0);
  assert.ok(env.logs.some(l => /rtl text plugin failed/.test(l[0])));
});

await ok('rtl: the viewer wires it up and credits it only when present', () => {
  assert.match(HTML, /initRtlText\(map, config\);/);
  assert.match(HTML, /\['attr-rtl-section', config\.rtlTextPlugin\]/);
  assert.match(HTML, /window\.__szFetchWithRetry = fetchWithRetry;/);
});

console.log(`\n${pass} passed${process.exitCode ? ', some FAILED' : ''}`);
