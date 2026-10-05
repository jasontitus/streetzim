// The map style the viewer ships: the dark theme (135-map-theme.js), the POI
// icons (136-poi-icons.js) and the lazy RTL text plugin (137-rtl-text.js).
// Runs the code straight out of resources/viewer/index.html (the copy ZIMs
// and the in-place patcher ship) against stubbed browser globals.
//
//   node tests/viewer_style_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
// The UI-string runtime (szT, szFixed, …) as globals, as index.html has it:
// the code under test calls it (docs/i18n.md). Indirect eval: global scope.
(0, eval)(fs.readFileSync(`${REPO}/resources/viewer/i18n/runtime.js`, 'utf8'));
// The parts joined, as written: resources/viewer/index.html is the same
// code without comments (tools/viewer_compact.py; tools/lint_viewer.mjs
// proves the token streams equal), and these tests find code by them.
const PARTS_DIR = `${REPO}/resources/viewer/src/index`;
const HTML = fs.readdirSync(PARTS_DIR).filter((n) => /^\d{3}-[\w-]+\.(html|js)$/.test(n)).sort()
  .map((n) => fs.readFileSync(`${PARTS_DIR}/${n}`, 'utf8')).join('');
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
  const winListeners = {};
  const mq = {
    get matches() { return env.darkNow ? env.darkNow() : dark; },
    addEventListener: (t, f) => listeners.push(f),
  };
  const window = {
    matchMedia: () => mq,
    frameElement: env.frameElement || null,
    devicePixelRatio: env.dpr || 1,
    __szFetchWithRetry: env.fetcher,
    addEventListener: (t, f) => (winListeners[t] = winListeners[t] || []).push(f),
  };
  // env.storage: a Storage stub (see memStorage); env.storageGetterThrows:
  // reading window.localStorage itself throws (sandboxed frame).
  Object.defineProperty(window, 'localStorage', { get() {
    if (env.storageGetterThrows) throw new Error('SecurityError');
    return env.storage || null;
  } });
  const classes = new Set();
  const document = {
    documentElement: { tag: 'html', classList: {
      add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) } },
    createElement: env.createElement || (() => ({ getContext: () => null })),
  };
  const getComputedStyle = (el) => ({ filter: (env.filters || {})[el && el.tag] || 'none' });
  const logs = [];
  const fn = new Function('window', 'document', 'location', 'getComputedStyle',
    'baseUrl', 'dbg', 'describeError', 'Path2D', 'fetch',
    SRC + '\nreturn { makeStyle, _SZ_DARK, _SZ_MAKI, _SZ_POI_ICON, _SZ_POI_GROUP,' +
    ' _szPoiIconExpr, _szPrefersDark, _szThemeStyle, initMapTheme, initPoiIcons,' +
    ' _szRenderPoiIcon, initRtlText, SZ_THEME_KEY, szReadThemeMode, szWriteThemeMode,' +
    ' szNextThemeMode, szThemeMode, szSetThemeMode, szThemeButton };');
  const api = fn(window, document, { search }, getComputedStyle, 'http://zim/C/',
    (...x) => logs.push(x), (e) => String(e && e.message || e), env.Path2D, env.fetch);
  return { ...api, window, mq, listeners, winListeners, logs, htmlClasses: classes };
}
const CONFIG = { minZoom: 0, maxZoom: 14 };

// localStorage stand-in; `fail` names methods that throw (quota, Kiwix iOS).
function memStorage(init = {}, fail = []) {
  const m = new Map(Object.entries(init));
  const guard = (k) => { if (fail.includes(k)) throw new Error(k + ' denied'); };
  return {
    m,
    getItem(k) { guard('getItem'); return m.has(k) ? m.get(k) : null; },
    setItem(k, v) { guard('setItem'); m.set(k, String(v)); },
    removeItem(k) { guard('removeItem'); m.delete(k); },
  };
}
// A map that keeps the paint it is given, seeded from a style, plus layers
// the viewer adds at run time (route, search pin) that no theme may touch.
function paintMap(style, { loaded = true } = {}) {
  const paint = new Map(), fired = [], once = {};
  for (const l of style.layers) paint.set(l.id, JSON.parse(JSON.stringify(l.paint || {})));
  paint.set('route-line', { 'line-color': '#1a73e8' });
  paint.set('search-pin', { 'circle-color': '#e11d48' });
  const m = {
    paint, fired, waits: once, loaded,
    // Before its style has loaded MapLibre has no layers at all.
    getLayer: (id) => m.loaded && paint.has(id) ? { id } : undefined,
    setPaintProperty: (id, k, v) => { paint.get(id)[k] = JSON.parse(JSON.stringify(v)); },
    fire: (t, d) => fired.push([t, d && d.dark]),
    triggerRepaint() {},
  };
  m.once = (t, f) => { (once[t] = once[t] || []).push(f); };
  // The style finishes loading: MapLibre fires 'styledata'.
  m.finishLoading = () => { m.loaded = true; const fs = once.styledata || []; once.styledata = []; fs.forEach(f => f()); };
  m.pending = () => (once.styledata || []).length;
  return m;
}
function paintOf(style) { return Object.fromEntries(style.layers.map(l => [l.id, l.paint || {}])); }
// A <button> just big enough for szThemeButton.
function fakeButton() {
  const attrs = {}, handlers = {};
  return {
    attrs, handlers, innerHTML: '', title: '',
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k]; },
    addEventListener(t, f) { handlers[t] = f; },
    click() { handlers.click(); },
  };
}

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

// ---- Dark UI chrome (html.sz-dark, 010-styles.html) ----------------------
await ok('theme: the page chrome gets html.sz-dark exactly when the map is dark', () => {
  assert.ok(!load().htmlClasses.has('sz-dark'));
  assert.ok(load({ dark: true }).htmlClasses.has('sz-dark'));
  assert.ok(!load({ dark: true, search: '?theme=light' }).htmlClasses.has('sz-dark'));
  assert.ok(load({ search: '?theme=dark' }).htmlClasses.has('sz-dark'));
  assert.ok(!load({ dark: true, filters: { html: 'invert(1) hue-rotate(180deg)' } }).htmlClasses.has('sz-dark'));
  // ... and follows a live scheme change both ways.
  let darkNow = false;
  const env = load({ darkNow: () => darkNow });
  const map = { getLayer: () => ({}), setPaintProperty() {}, fire() {}, triggerRepaint() {} };
  env.initMapTheme(map, CONFIG);
  darkNow = true; env.listeners[0]();
  assert.ok(env.htmlClasses.has('sz-dark'));
  darkNow = false; env.listeners[0]();
  assert.ok(!env.htmlClasses.has('sz-dark'));
});

// The dark block's custom properties, and the colours of a few rules in it.
const DARK_CSS = (() => {
  const i = HTML.indexOf('  html.sz-dark {');
  assert.ok(i > 0, 'no html.sz-dark block in index.html');
  return HTML.slice(i, HTML.indexOf('</style>', i));
})();
const DARK_VARS = Object.fromEntries([...DARK_CSS.slice(0, DARK_CSS.indexOf('}')).matchAll(/(--[\w-]+):\s*([^;]+);/g)]
  .map(m => [m[1], m[2].trim()]));
function darkRule(selector, prop) {
  const i = DARK_CSS.indexOf(selector + ' {');
  assert.ok(i >= 0, 'no dark rule for ' + selector);
  const body = DARK_CSS.slice(i, DARK_CSS.indexOf('}', i));
  const m = new RegExp('(?:^|[{;\\s])' + prop + ':\\s*([^;]+);').exec(body);
  assert.ok(m, `${selector} sets no ${prop}`);
  return m[1].trim();
}
// A translucent panel over the worst case behind it (white) -- light text
// on it is then at its lowest contrast.
function overWhite(c) {
  const m = /^rgba\(([^)]+)\)$/.exec(c);
  if (!m) return c;
  const [r, g, b, a] = m[1].split(',').map(Number);
  const hex = (v) => Math.round(v * a + 255 * (1 - a)).toString(16).padStart(2, '0');
  return '#' + hex(r) + hex(g) + hex(b);
}

await ok('theme: dark chrome text keeps >= 4.5:1 (WCAG AA) on every dark panel colour', () => {
  const v = (k) => { assert.ok(DARK_VARS[k], 'missing ' + k); return DARK_VARS[k]; };
  const panels = {
    '--panel-bg': v('--panel-bg'),
    '#info': darkRule('html.sz-dark #info', 'background'),
    '#attr-btn': darkRule('html.sz-dark #attr-btn', 'background'),
    'chip': darkRule('html.sz-dark #find-chips .find-chip:not(.on)', 'background'),
    'scale': darkRule('html.sz-dark .maplibregl-ctrl.maplibregl-ctrl-scale', 'background-color'),
    'attribution': darkRule('html.sz-dark .maplibregl-ctrl.maplibregl-ctrl-attrib,\n  html.sz-dark .maplibregl-ctrl-attrib.maplibregl-compact', 'background-color'),
    'popup': darkRule('html.sz-dark .maplibregl-popup-content', 'background'),
    '--szd-surface': v('--szd-surface'), '--szd-surface-a': v('--szd-surface-a'),
    '--szd-surface-hover': v('--szd-surface-hover'),
  };
  const texts = ['--text-primary', '--text-secondary', '--text-tertiary',
                 '--szd-fg', '--szd-fg-2', '--szd-fg-3', '--szd-link', '--szd-warn'].map(v);
  texts.push(darkRule('html.sz-dark .maplibregl-ctrl.maplibregl-ctrl-scale', 'color'));
  for (const [name, bg] of Object.entries(panels)) {
    for (const fg of texts) {
      const c = contrast(fg, overWhite(bg));
      assert.ok(c >= 4.5, `${fg} on ${name} (${bg}) is ${c.toFixed(2)}:1`);
    }
  }
  assert.ok(contrast(v('--szd-btn2-fg'), v('--szd-btn2-bg')) >= 4.5);
  assert.ok(contrast('#8ab4f8', v('--szd-surface')) >= 4.5);            // pin links, "Change map"
  // Filled buttons keep their light-mode colours: white on blue passes.
  for (const bg of ['#1a73e8', '#2563eb', '#2a4a7a']) assert.ok(contrast('#ffffff', bg) >= 4.5, bg);
});

await ok('theme: light mode is untouched -- --szd-* exist only under html.sz-dark', () => {
  const used = new Set([...HTML.matchAll(/var\((--szd-[\w-]+),\s*([^)]+\)?)\)/g)].map(m => m[1]));
  assert.ok(used.size >= 8, 'the JS-built sheets should use the --szd-* tokens');
  for (const k of used) assert.ok(DARK_VARS[k], `${k} is used but not defined for dark mode`);
  // Defined nowhere else, so in light mode every var() falls back to the
  // literal light colour it replaced.
  // (The print block below sets them back to "initial", i.e. undefined.)
  const szd = Object.keys(DARK_VARS).filter(k => k.startsWith('--szd-'));
  const defs = [...HTML.matchAll(/(--szd-[\w-]+)\s*:\s*([^;]+);/g)];
  assert.strictEqual(defs.filter(m => m[2].trim() !== 'initial').length, szd.length);
  assert.deepStrictEqual(defs.filter(m => m[2].trim() === 'initial').map(m => m[1]).sort(), szd.sort());
  const root = HTML.slice(HTML.indexOf(':root {'), HTML.indexOf('}', HTML.indexOf(':root {')));
  assert.match(root, /--panel-bg: rgba\(255,255,255,0\.96\);/);
  assert.match(root, /--text-tertiary: #9ca3af;/);
});

await ok('theme: dark focus rings are visible (>= 3:1 on the panel)', () => {
  const ring = darkRule('html.sz-dark #search-input:focus,\n  html.sz-dark .routing-input:focus', 'box-shadow');
  const col = /#[0-9a-f]{6}/i.exec(ring)[0];
  assert.match(ring, /^0 0 0 2px #/);
  for (const bg of [DARK_VARS['--panel-bg'], DARK_VARS['--szd-surface']]) {
    assert.ok(contrast(col, overWhite(bg)) >= 3, `${col} on ${bg}`);
  }
});

await ok('theme: printing a dark page gives the light chrome', () => {
  const i = DARK_CSS.indexOf('@media print {');
  assert.ok(i > 0, 'no print block');
  const p = DARK_CSS.slice(i);
  assert.match(p, /color-scheme: light;/);
  assert.match(p, /--panel-bg: rgba\(255,255,255,0\.96\);/);
  assert.match(p, /--text-tertiary: #9ca3af;/);
});

await ok('theme: JS-built panels take their light colours through --szd-* tokens', () => {
  // Inline styles (no space after the colon) with a light surface or dark
  // text colour would stay light in dark mode. White text on a filled
  // button, the backdrop and the debug overlays are fine either way.
  const allowed = /^(color:#fff|background:#(1a73e8|2a4a7a)|background:rgba\(0,0,0,0\.(32|55|85)\)|color:#0f0)$/;
  const bad = [...HTML.matchAll(/(?:background|color):(?:#[0-9a-fA-F]{3,6}\b|rgba\([^)]*\))/g)]
    .map(m => m[0]).filter(x => !allowed.test(x));
  assert.deepStrictEqual(bad, []);
});

// ---- Light/dark switch (szThemeButton, szSetThemeMode) --------------------
await ok('switch: a tap cycles Auto -> Light -> Dark -> Auto; junk is Auto', () => {
  const { szNextThemeMode } = load();
  assert.strictEqual(szNextThemeMode('auto'), 'light');
  assert.strictEqual(szNextThemeMode('light'), 'dark');
  assert.strictEqual(szNextThemeMode('dark'), 'auto');
  assert.strictEqual(szNextThemeMode('sepia'), 'auto');
  assert.strictEqual(szNextThemeMode(undefined), 'auto');
});

await ok('switch: precedence is ?theme= > saved choice > auto (invert host, OS scheme)', () => {
  const st = (v) => memStorage(v ? { 'streetzim.theme': v } : {});
  const dark = (env) => load(env)._szPrefersDark();
  // Saved choice beats the OS scheme both ways.
  assert.strictEqual(dark({ dark: true, storage: st('light') }), false);
  assert.strictEqual(dark({ dark: false, storage: st('dark') }), true);
  // (Under a host that inverts the page each mode means what the reader
  // sees; see the inverting-host test below.)
  // The URL beats the saved choice.
  assert.strictEqual(dark({ dark: true, storage: st('dark'), search: '?theme=light' }), false);
  assert.strictEqual(dark({ dark: false, storage: st('light'), search: '?theme=dark' }), true);
  // Nothing saved, or something unknown: auto as before.
  assert.strictEqual(dark({ dark: true, storage: st() }), true);
  assert.strictEqual(dark({ dark: true, storage: st('sepia') }), true);
  assert.strictEqual(dark({ dark: false, storage: st('auto') }), false);
  assert.strictEqual(dark({ dark: true, storage: st('auto'), filters: { html: 'invert(1)' } }), false);
  // The chrome agrees from the first paint.
  assert.ok(load({ dark: false, storage: st('dark') }).htmlClasses.has('sz-dark'));
  assert.ok(!load({ dark: true, storage: st('light') }).htmlClasses.has('sz-dark'));
  // What the button shows.
  assert.strictEqual(load({ storage: st('light') }).szThemeMode(), 'light');
  assert.strictEqual(load({ storage: st('light'), search: '?theme=dark' }).szThemeMode(), 'dark');
  assert.strictEqual(load({ storage: st() }).szThemeMode(), 'auto');
});

await ok('switch: storage that throws or is missing means Auto, and switching still works', () => {
  for (const env of [
    { storageGetterThrows: true },
    { storage: memStorage({ 'streetzim.theme': 'light' }, ['getItem', 'setItem', 'removeItem']) },
    { storage: null },
  ]) {
    const e = load({ dark: true, ...env });
    assert.strictEqual(e.szThemeMode(), 'auto', JSON.stringify(Object.keys(env)));
    assert.ok(e.htmlClasses.has('sz-dark'));
    const map = paintMap(e.makeStyle(CONFIG));
    e.initMapTheme(map, CONFIG);
    e.szSetThemeMode('light');                       // no throw
    assert.strictEqual(e.szThemeMode(), 'light');
    assert.ok(!e.htmlClasses.has('sz-dark'));
    assert.strictEqual(map.paint.get('background')['background-color'], '#f8f4f0');
  }
  // Writes: light/dark saved, auto removes the key; a failing write is quiet.
  const { szWriteThemeMode, szReadThemeMode, SZ_THEME_KEY } = load();
  const s = memStorage();
  szWriteThemeMode(s, 'dark'); assert.strictEqual(s.m.get(SZ_THEME_KEY), 'dark');
  assert.strictEqual(szReadThemeMode(s), 'dark');
  szWriteThemeMode(s, 'auto'); assert.ok(!s.m.has(SZ_THEME_KEY));
  szWriteThemeMode(memStorage({}, ['setItem']), 'dark');
  szWriteThemeMode(null, 'dark');
  assert.strictEqual(szReadThemeMode(memStorage({ [SZ_THEME_KEY]: 'dark' }, ['getItem'])), 'auto');
});

await ok('switch: every dark-styled layer goes back to light and to dark again; runtime layers untouched', () => {
  const storage = memStorage();
  const e = load({ dark: true, storage });
  const LIGHT = paintOf(load().makeStyle(CONFIG));
  const DARK = paintOf(load({ dark: true }).makeStyle(CONFIG));
  const map = paintMap(e.makeStyle(CONFIG));
  e.initMapTheme(map, CONFIG);
  const darkIds = Object.keys(e._SZ_DARK);
  const base = (m) => Object.fromEntries([...m.paint].filter(([id]) => id in LIGHT));
  assert.deepStrictEqual(base(map), DARK);
  e.szSetThemeMode('light');
  assert.deepStrictEqual(base(map), LIGHT);
  for (const id of darkIds) {
    for (const k of Object.keys(e._SZ_DARK[id])) {
      assert.notDeepStrictEqual(map.paint.get(id)[k], e._SZ_DARK[id][k], `${id}.${k} still dark`);
    }
  }
  assert.strictEqual(storage.m.get('streetzim.theme'), 'light');
  assert.ok(!e.htmlClasses.has('sz-dark'));
  e.szSetThemeMode('dark');
  assert.deepStrictEqual(base(map), DARK);
  assert.ok(e.htmlClasses.has('sz-dark'));
  e.szSetThemeMode('auto');                          // OS is dark: nothing to do
  assert.deepStrictEqual(base(map), DARK);
  assert.ok(!storage.m.has('streetzim.theme'));
  assert.deepStrictEqual(map.paint.get('route-line'), { 'line-color': '#1a73e8' });
  assert.deepStrictEqual(map.paint.get('search-pin'), { 'circle-color': '#e11d48' });
  // Satellite mode hears each real change, and only those.
  assert.deepStrictEqual(map.fired, [['streetzim.theme', false], ['streetzim.theme', true]]);
});

await ok('switch: a chosen theme ignores OS scheme changes; Auto follows them again', () => {
  let darkNow = false;
  const e = load({ darkNow: () => darkNow, storage: memStorage() });
  const map = paintMap(e.makeStyle(CONFIG));
  e.initMapTheme(map, CONFIG);
  e.szSetThemeMode('light');
  darkNow = true; e.listeners[0]();
  assert.strictEqual(map.paint.get('background')['background-color'], '#f8f4f0');
  assert.ok(!e.htmlClasses.has('sz-dark'));
  e.szSetThemeMode('auto');                          // back to the (now dark) OS
  assert.strictEqual(map.paint.get('background')['background-color'], e._SZ_DARK.background['background-color']);
  darkNow = false; e.listeners[0]();
  assert.strictEqual(map.paint.get('background')['background-color'], '#f8f4f0');
});

await ok('switch: the button names and draws its mode, and a tap moves on and is saved', () => {
  const storage = memStorage();
  const e = load({ dark: true, storage, createElement: fakeButton });
  const map = paintMap(e.makeStyle(CONFIG));
  e.initMapTheme(map, CONFIG);
  const btn = e.szThemeButton();
  assert.strictEqual(btn.type, 'button');
  assert.strictEqual(btn.className, 'sz-theme-btn');
  const seen = [];
  for (let i = 0; i < 4; i++) {
    seen.push([btn.attrs['data-mode'], btn.title, btn.attrs['aria-label'] === btn.title,
      storage.m.get('streetzim.theme') || null, e.htmlClasses.has('sz-dark')]);
    if (i < 3) btn.click();
  }
  assert.deepStrictEqual(seen, [
    ['auto', 'Map theme: auto (follows the system)', true, null, true],
    ['light', 'Map theme: light', true, 'light', false],
    ['dark', 'Map theme: dark', true, 'dark', true],
    ['auto', 'Map theme: auto (follows the system)', true, null, true],
  ]);
  // Three different inline icons, no external assets.
  const icons = new Set();
  const b2 = load({ storage: memStorage(), createElement: fakeButton }).szThemeButton();
  for (let i = 0; i < 3; i++) { icons.add(b2.innerHTML); b2.click(); }
  assert.strictEqual(icons.size, 3);
  for (const svg of icons) { assert.match(svg, /^<svg viewBox="0 0 24 24"/); assert.doesNotMatch(svg, /href|url\(/); }
  // Opened with ?theme=dark: the button shows dark, and a tap takes over.
  const u = load({ dark: false, search: '?theme=dark', storage: memStorage(), createElement: fakeButton });
  const ub = u.szThemeButton();
  assert.strictEqual(ub.attrs['data-mode'], 'dark');
  u.initMapTheme(paintMap(u.makeStyle(CONFIG)), CONFIG);
  ub.click();
  assert.strictEqual(ub.attrs['data-mode'], 'auto');
  assert.ok(!u.htmlClasses.has('sz-dark'));         // OS is light
});

await ok('switch: under an inverting host (Kiwix JS dark mode) every mode means what the reader SEES', () => {
  const st = (v) => memStorage(v ? { 'streetzim.theme': v } : {});
  const frame = { tag: 'iframe' };
  const hosts = [
    { filters: { html: 'invert(1) hue-rotate(180deg)' } },
    { frameElement: frame, filters: { iframe: 'invert(100%) hue-rotate(180deg)' } },
  ];
  const DARK_BG = load()._SZ_DARK.background['background-color'], LIGHT_BG = '#f8f4f0';
  for (const host of hosts) for (const os of [false, true]) {
    // [saved, url] -> style built (dark?) ; the host then inverts it.
    const cases = [
      [null, '', false],          // Auto: as before -- light style, shown dark by the host
      ['light', '', true],        // reader wants light: dark style, inverted to light
      ['dark', '', false],        // reader wants dark: light style, inverted to dark
      [null, '?theme=dark', false],
      [null, '?theme=light', true],
      ['dark', '?theme=light', true],
    ];
    for (const [saved, search, styleDark] of cases) {
      const e = load({ ...host, dark: os, search, storage: st(saved) });
      const tag = `${JSON.stringify(host.filters)} os=${os} saved=${saved} url=${search}`;
      assert.strictEqual(e._szPrefersDark(), styleDark, tag);
      assert.strictEqual(e.makeStyle(CONFIG).layers[0].paint['background-color'], styleDark ? DARK_BG : LIGHT_BG, tag);
      assert.strictEqual(e.htmlClasses.has('sz-dark'), styleDark, tag + ' (chrome)');
    }
  }
  // Without inversion nothing flips.
  assert.strictEqual(load({ dark: false, storage: st('dark') })._szPrefersDark(), true);
  assert.strictEqual(load({ dark: true, storage: st('light') })._szPrefersDark(), false);
  // And a tap under inversion: Auto -> Light builds the dark style.
  const e = load({ dark: true, storage: st(), filters: { html: 'invert(1)' }, createElement: fakeButton });
  const map = paintMap(e.makeStyle(CONFIG));
  e.initMapTheme(map, CONFIG);
  const btn = e.szThemeButton();
  btn.click();
  assert.strictEqual(btn.attrs['data-mode'], 'light');
  assert.strictEqual(map.paint.get('background')['background-color'], DARK_BG);
  btn.click();
  assert.strictEqual(map.paint.get('background')['background-color'], LIGHT_BG);
});

await ok('switch: a tap before the style has loaded switches the chrome now and the map once it loads', () => {
  const e = load({ dark: true, storage: memStorage(), createElement: fakeButton });
  const DARK = paintOf(load({ dark: true }).makeStyle(CONFIG)), LIGHT = paintOf(load().makeStyle(CONFIG));
  const map = paintMap(e.makeStyle(CONFIG), { loaded: false });
  e.initMapTheme(map, CONFIG);
  const btn = e.szThemeButton();
  btn.click();                                       // Auto (dark) -> Light, map not ready
  assert.ok(!e.htmlClasses.has('sz-dark'), 'chrome did not switch at once');
  assert.strictEqual(map.fired.length, 0);
  assert.strictEqual(map.pending(), 1, 'nothing waits for the style');
  e.listeners[0]();                                  // an OS 'change' while waiting: still one waiter
  assert.strictEqual(map.pending(), 1);
  btn.click();                                       // Light -> Dark, still loading: no second waiter
  assert.ok(e.htmlClasses.has('sz-dark'));
  assert.strictEqual(map.pending(), 1);
  btn.click();                                       // Dark -> Auto: the OS is dark, as painted
  map.finishLoading();
  const base = () => Object.fromEntries([...map.paint].filter(([id]) => id in LIGHT));
  assert.deepStrictEqual(base(), DARK);              // ended where it started: nothing to paint
  assert.strictEqual(map.fired.length, 0);
  btn.click();                                       // Auto -> Light on a loaded map
  assert.deepStrictEqual(base(), LIGHT);
  assert.ok(!e.htmlClasses.has('sz-dark'));

  // One tap, then load: the map catches up, and the next tap is not lost.
  const f = load({ dark: true, storage: memStorage(), createElement: fakeButton });
  const m2 = paintMap(f.makeStyle(CONFIG), { loaded: false });
  f.initMapTheme(m2, CONFIG);
  const b2 = f.szThemeButton();
  b2.click();                                        // -> Light before load
  const base2 = () => Object.fromEntries([...m2.paint].filter(([id]) => id in LIGHT));
  assert.deepStrictEqual(base2(), DARK);             // not painted yet
  m2.finishLoading();
  assert.deepStrictEqual(base2(), LIGHT);
  assert.deepStrictEqual(m2.fired, [['streetzim.theme', false]]);
  b2.click();                                        // -> Dark
  assert.deepStrictEqual(base2(), DARK);
  // A 'styledata' that comes before the layers exist waits again.
  const g = load({ dark: false, storage: memStorage() });
  const m3 = paintMap(g.makeStyle(CONFIG), { loaded: false });
  g.initMapTheme(m3, CONFIG);
  g.szSetThemeMode('dark');
  const fs = m3.waits.styledata; m3.waits.styledata = []; fs.forEach(fn => fn());   // still not loaded
  assert.strictEqual(m3.pending(), 1);
  m3.finishLoading();
  assert.strictEqual(m3.paint.get('background')['background-color'], g._SZ_DARK.background['background-color']);
});

await ok('switch: an OS scheme change before the style has loaded is applied once it loads', () => {
  let darkNow = false;
  const e = load({ darkNow: () => darkNow, storage: memStorage() });
  const map = paintMap(e.makeStyle(CONFIG), { loaded: false });
  e.initMapTheme(map, CONFIG);
  darkNow = true; e.listeners[0]();
  assert.ok(e.htmlClasses.has('sz-dark'));
  assert.strictEqual(map.paint.get('background')['background-color'], '#f8f4f0');
  map.finishLoading();
  assert.strictEqual(map.paint.get('background')['background-color'], e._SZ_DARK.background['background-color']);
});

await ok('switch: another tab changing the choice is followed (storage event)', () => {
  const storage = memStorage();
  const e = load({ dark: false, storage, createElement: fakeButton });
  const map = paintMap(e.makeStyle(CONFIG));
  e.initMapTheme(map, CONFIG);
  const btn = e.szThemeButton();
  assert.strictEqual((e.winListeners.storage || []).length, 1, 'no storage listener');
  const fire = (key) => e.winListeners.storage.forEach(f => f({ key }));
  storage.m.set('streetzim.theme', 'dark');
  fire('streetzim.units');                           // someone else's key: ignored
  assert.strictEqual(btn.attrs['data-mode'], 'auto');
  fire('streetzim.theme');
  assert.strictEqual(btn.attrs['data-mode'], 'dark');
  assert.strictEqual(map.paint.get('background')['background-color'], e._SZ_DARK.background['background-color']);
  assert.ok(e.htmlClasses.has('sz-dark'));
  storage.m.clear(); fire(null);                     // localStorage.clear() elsewhere
  assert.strictEqual(btn.attrs['data-mode'], 'auto');
  assert.strictEqual(map.paint.get('background')['background-color'], '#f8f4f0');
  // A window without addEventListener (old WebView stub) still loads.
  const w = load({ storage: memStorage() });
  delete w.window.addEventListener;
  w.initMapTheme(paintMap(w.makeStyle(CONFIG)), CONFIG);
});

await ok('switch: it sits in the Home group, and the layer panel clears both', () => {
  const i = HTML.indexOf('function initHomeButton(');
  const body = HTML.slice(i, HTML.indexOf('\n}\n', i));
  assert.match(body, /div\.appendChild\(btn\);[\s\S]*div\.appendChild\(szThemeButton\(\)\)/);
  // Zoom/compass 10-142, Home 152-196, switch 196-240 (44 px coarse buttons), +10.
  assert.match(HTML, /#controls \{[^}]*top: calc\(250px \+ var\(--top-inset, 0px\)\)/);
  assert.match(HTML, /\.sz-home-btn svg, \.sz-theme-btn svg \{[^}]*fill: #333/);
  // Landscape phones: Home's group is a row (Home | switch), the panel row
  // moves left of it, so the group never reaches the locate button.
  const land = HTML.slice(HTML.indexOf('@media (max-height: 500px) {\n    #controls {'));
  const rule = land.slice(0, land.indexOf('\n  }'));
  assert.match(rule, /#controls \{ top: calc\(152px \+ var\(--top-inset, 0px\)\); right: 110px; flex-direction: row; \}/);
  assert.match(rule, /\.sz-home-group \{ display: flex; flex-direction: row; \}/);
  assert.match(body, /'maplibregl-ctrl maplibregl-ctrl-group sz-home-group'/);
  assert.match(HTML, /html\.sz-dark \.sz-home-btn svg, html\.sz-dark \.sz-theme-btn svg \{ fill: #e8eaed; \}/);
});

await ok('switch: places.html takes the same choice (?theme= > saved > OS)', () => {
  const P = fs.readFileSync(`${REPO}/resources/viewer/places.html`, 'utf8');
  const a = P.indexOf('// Light/dark: the same choice as the map viewer');
  assert.ok(a > 0, 'places.html has no theme script');
  const src = P.slice(a, P.indexOf('</script>', a));
  function run(search, storage, getterThrows, opts = {}) {
    const attrs = {};
    const window = { addEventListener: (t, f) => { if (t === 'storage') opts.onStorage = f; } };
    Object.defineProperty(window, 'localStorage', { get() { if (getterThrows) throw new Error('x'); return storage; } });
    const root = { tag: 'html', setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: (k) => { delete attrs[k]; } };
    const gcs = (el) => ({ filter: el && el.tag === opts.inverted ? 'invert(1) hue-rotate(180deg)' : 'none' });
    window.frameElement = opts.frame ? { tag: 'iframe' } : null;
    new Function('window', 'document', 'location', 'getComputedStyle', src)(window,
      { documentElement: root }, { search }, gcs);
    opts.attrs = attrs;
    return attrs['data-sz-theme'] || null;
  }
  assert.strictEqual(run('', memStorage({ 'streetzim.theme': 'dark' })), 'dark');
  assert.strictEqual(run('', memStorage({ 'streetzim.theme': 'light' })), 'light');
  assert.strictEqual(run('?q=x&theme=light', memStorage({ 'streetzim.theme': 'dark' })), 'light');
  assert.strictEqual(run('', memStorage({ 'streetzim.theme': 'sepia' })), null);
  assert.strictEqual(run('', memStorage({}, ['getItem'])), null);
  assert.strictEqual(run('', null, true), null);
  // Inverting host: what the reader sees -- flipped; Auto is the light set.
  for (const inv of [{ inverted: 'html' }, { inverted: 'iframe', frame: true }]) {
    assert.strictEqual(run('', memStorage({ 'streetzim.theme': 'dark' }), false, { ...inv }), 'light');
    assert.strictEqual(run('', memStorage({ 'streetzim.theme': 'light' }), false, { ...inv }), 'dark');
    assert.strictEqual(run('', memStorage(), false, { ...inv }), 'light');
    assert.strictEqual(run('?theme=dark', memStorage(), false, { ...inv }), 'light');
  }
  // Another tab changes the choice.
  const o = {}, st = memStorage();
  assert.strictEqual(run('', st, false, o), null);
  st.m.set('streetzim.theme', 'dark'); o.onStorage({ key: 'streetzim.theme' });
  assert.strictEqual(o.attrs['data-sz-theme'], 'dark');
  st.m.clear(); o.onStorage({ key: null });
  assert.strictEqual(o.attrs['data-sz-theme'], undefined);
  // The CSS: OS dark unless the reader chose light; chosen dark always.
  const css = P.slice(P.indexOf('<style>'), P.indexOf('</style>'));
  const vars = (sel) => (new RegExp(sel.replace(/[[\]()]/g, '\\$&') + ' \\{([^}]*)\\}').exec(css) || [])[1];
  const media = vars(':root:not([data-sz-theme=light])'), chosen = vars(':root[data-sz-theme=dark]');
  assert.ok(media && chosen, 'places.html dark rules missing');
  assert.match(css, /@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-sz-theme=light\]\)/);
  const bg = (t) => /--bg:\s*([^;]+);/.exec(t)[1];
  assert.strictEqual(bg(media), bg(chosen));
  assert.notStrictEqual(bg(media), /--bg:\s*([^;]+);/.exec(css)[1]);
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
