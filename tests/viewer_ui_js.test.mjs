// The viewer's last-view memory, Home button and About text
// (140-view-home-about.js) and the full-page error for a browser that
// cannot run it (025-home-about-fatal.html). Runs the code straight out of
// resources/viewer/index.html against stubbed browser globals.
//
//   node tests/viewer_ui_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const HTML = fs.readFileSync(`${REPO}/resources/viewer/index.html`, 'utf8');

let pass = 0;
async function ok(name, fn) {
  try { await fn(); console.log('ok   ', name); pass++; }
  catch (e) { console.error('FAIL ', name, '\n      ', e.stack || e.message); process.exitCode = 1; }
}

function slice(from, to) {
  const a = HTML.indexOf(from), b = HTML.indexOf(to, a);
  assert.ok(a >= 0 && b > a, `${from} .. ${to} not found in index.html`);
  return HTML.slice(a, b);
}
const VIEW_SRC = slice('// BEGIN view-home-about', '// END view-home-about');
const FATAL_SRC = slice('function szFatalPage', '</script>');

// Just enough DOM for these functions.
function fakeDocument() {
  const byId = {};
  const listeners = {};
  function el(tag) {
    const e = {
      tag, children: [], style: {}, attrs: {}, textContent: '', parentNode: null,
      classList: { set: new Set(), contains(c) { return this.set.has(c); }, add(c) { this.set.add(c); } },
      appendChild(c) { c.parentNode = e; e.children.push(c); if (c.id) byId[c.id] = c; return c; },
      removeChild(c) { e.children = e.children.filter((x) => x !== c); delete byId[c.id]; },
      setAttribute(k, v) { e.attrs[k] = v; },
      addEventListener(t, f) { (e.on = e.on || {})[t] = f; },
      text() { return [e.textContent].concat(e.children.map((c) => c.text())).join(' '); },
    };
    return e;
  }
  const body = el('body');
  return {
    body, documentElement: el('html'), title: '', visibilityState: 'visible',
    createElement: el,
    getElementById: (id) => byId[id] || null,
    addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
    _byId: byId, _el: el, _listeners: listeners,
  };
}

function memStorage(opts = {}) {
  const m = new Map();
  return {
    m,
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { if (opts.throwOnSet) throw new Error('QuotaExceededError'); m.set(k, String(v)); },
    removeItem: (k) => m.delete(k),
  };
}

function loadView(env = {}) {
  const document = env.document || fakeDocument();
  const winOn = {};
  const window = { addEventListener(t, f) { (winOn[t] = winOn[t] || []).push(f); }, _on: winOn };
  if ('storage' in env) {
    Object.defineProperty(window, 'localStorage', {
      get() { if (env.storage === 'throws') throw new Error('SecurityError'); return env.storage; },
    });
  }
  const fn = new Function('window', 'document', 'location', 'setTimeout', 'clearTimeout',
    VIEW_SRC + '\nreturn { SZ_VIEWER_VERSION, _szStorage, _szViewKey, _szHashSetsView,' +
    ' _szReadView, _szWriteView, _szOpeningCamera, initViewMemory, initHomeButton,' +
    ' _szAboutText, _szMonth, initAbout };');
  // Fake timers: `timers` holds the pending ones; runTimers() fires them.
  const timers = new Map();
  let next = 1;
  const api = fn(window, document, { pathname: env.pathname || '/C/index.html' },
    (f, ms) => { timers.set(next, { f, ms }); return next++; }, (id) => { timers.delete(id); });
  const pending = (ms) => [...timers.values()].filter((t) => ms === undefined || t.ms === ms).length;
  const runTimers = (ms) => {
    const due = [...timers.entries()].filter(([, t]) => ms === undefined || t.ms === ms);
    due.forEach(([id]) => timers.delete(id));
    due.forEach(([, t]) => t.f());
  };
  return { ...api, document, window, timers, pending, runTimers };
}

const CONFIG = { name: 'Monaco', center: [7.42, 43.74], zoom: 13, minZoom: 0,
  bounds: [7.40, 43.72, 7.44, 43.76] };

await ok('storage: a throwing or missing localStorage reads as none', () => {
  assert.strictEqual(loadView({ storage: 'throws' })._szStorage(), null);
  assert.strictEqual(loadView({ storage: null })._szStorage(), null);
  assert.strictEqual(loadView({})._szStorage(), null);
  assert.strictEqual(loadView({ storage: memStorage({ throwOnSet: true }) })._szStorage(), null);
  assert.ok(loadView({ storage: memStorage() })._szStorage());
});

await ok('a saved view reopens; bearing and pitch only when used', () => {
  const v = loadView({ storage: memStorage() });
  const s = v._szStorage();
  assert.ok(v._szWriteView(CONFIG, s, { lng: 7.43123456, lat: 43.7312345, zoom: 15.456, bearing: 0, pitch: 0 }));
  const raw = JSON.parse([...s.m.values()][0]);
  assert.deepStrictEqual(raw, { c: [7.43123, 43.73123], z: 15.46, h: '7.42,43.74,13' });
  assert.deepStrictEqual(v._szOpeningCamera(CONFIG, '', s),
    { center: [7.43123, 43.73123], zoom: 15.46, bearing: 0, pitch: 0 });
  v._szWriteView(CONFIG, s, { lng: 7.43, lat: 43.73, zoom: 16, bearing: -30.26, pitch: 45 });
  assert.deepStrictEqual(v._szOpeningCamera(CONFIG, '', s),
    { center: [7.43, 43.73], zoom: 16, bearing: -30.3, pitch: 45 });
});

await ok('deep links win over the saved view', () => {
  const v = loadView({ storage: memStorage() });
  const s = v._szStorage();
  v._szWriteView(CONFIG, s, { lng: 7.43, lat: 43.73, zoom: 16 });
  const home = { center: CONFIG.center, zoom: CONFIG.zoom, bearing: 0, pitch: 0 };
  for (const h of ['#map=16/43.7/7.4', '#dest=43.7,7.4&label=x', '#origin=43.7,7.4',
                   '#pin=43.7,7.4', '#find=results', '#label=a&map=3/1/2']) {
    assert.deepStrictEqual(v._szOpeningCamera(CONFIG, h, s), home, h);
  }
  // Other fragments and none at all leave the choice to the saved view.
  assert.strictEqual(v._szOpeningCamera(CONFIG, '#label=x', s).zoom, 16);
  assert.strictEqual(v._szOpeningCamera(CONFIG, '', s).zoom, 16);
});

await ok('views are kept per map and invalid ones ignored', () => {
  const v = loadView({ storage: memStorage() });
  const s = v._szStorage();
  const other = { ...CONFIG, name: 'Andorra', bounds: [1.4, 42.4, 1.8, 42.7], center: [1.6, 42.5] };
  v._szWriteView(CONFIG, s, { lng: 7.43, lat: 43.73, zoom: 16 });
  assert.notStrictEqual(v._szViewKey(CONFIG), v._szViewKey(other));
  assert.strictEqual(v._szReadView(other, s), null);
  // Same name, new release with the same bounds: same key.
  assert.strictEqual(v._szViewKey({ ...CONFIG, zoom: 9 }), v._szViewKey(CONFIG));
  // No name or bounds: the page path.
  assert.strictEqual(v._szViewKey({}), 'streetzim.view./C/index.html');
  const key = v._szViewKey(CONFIG);
  for (const bad of ['not json', '{"c":[7.43]}', '{"c":[7.43,43.73],"z":"9"}',
                     '{"c":[999,43.73],"z":9}', '{"c":[2.35,48.85],"z":9}',   // outside bounds
                     '{"c":[7.43,43.73],"z":99}', 'null']) {
    s.m.set(key, bad);
    assert.strictEqual(v._szReadView(CONFIG, s), null, bad);
  }
  s.m.set(key, '{"c":[7.43,43.73],"z":14,"b":"x","p":400,"h":"7.42,43.74,13"}');
  assert.deepStrictEqual(v._szReadView(CONFIG, s), { center: [7.43, 43.73], zoom: 14, bearing: 0, pitch: 0 });
  assert.strictEqual(v._szReadView(CONFIG, null), null);
  assert.strictEqual(v._szWriteView(CONFIG, memStorage({ throwOnSet: true }), { lng: 1, lat: 1, zoom: 1 }), false);
});

// A map whose camera methods move `cam` and fire movestart/moveend like
// MapLibre (no movestart while already moving; `hold` leaves a move open).
function fakeMap() {
  const on = {};
  const canvasOn = {};
  const map = {
    cam: { lng: 7.425, lat: 43.735, zoom: 17, bearing: 0 },
    moving: false,
    on: (t, f) => { (on[t] = on[t] || []).push(f); },
    fire(t, e) { (on[t] || []).forEach((f) => f(e || {})); },
    controls: [], eased: null,
    addControl(c, pos) { this.controls.push([c, pos]); },
    getCanvasContainer: () => ({ addEventListener: (t, f) => { canvasOn[t] = f; } }),
    input(t) { canvasOn[t]({ type: t }); },
    start(e) { if (!this.moving) { this.moving = true; this.fire('movestart', e); } },
    end(e) { this.moving = false; this.fire('moveend', e); },
    move(o, e, hold) { this.start(e); Object.assign(this.cam, o); if (!hold) this.end(e); },
    easeTo(o, e, hold) { this.eased = o; this.move(o.center ? { lng: o.center[0], lat: o.center[1], zoom: o.zoom } : o, e, hold); },
    flyTo(o, e, hold) { this.easeTo(o, e, hold); },
    jumpTo(o, e) { this.easeTo(o, e); },
    getCenter() { return { lng: this.cam.lng, lat: this.cam.lat }; },
    getZoom() { return this.cam.zoom; }, getBearing() { return this.cam.bearing; }, getPitch: () => 0,
  };
  return map;
}

const GESTURE = { originalEvent: { type: 'mousedown' } };

await ok('reader moves are saved, but not while driving', () => {
  const storage = memStorage();
  const v = loadView({ storage });
  const map = fakeMap();
  v.initViewMemory(map, CONFIG);
  map.fire('movestart', GESTURE);
  map.fire('moveend', {});          // e.g. the end of a drag's inertia
  v.runTimers();
  assert.deepStrictEqual(JSON.parse(storage.m.get(v._szViewKey(CONFIG))),
    { c: [7.425, 43.735], z: 17, h: '7.42,43.74,13' });
  storage.m.clear();
  const hud = v.document._el('div'); hud.id = 'drive-hud'; hud.classList.add('visible');
  v.document.body.appendChild(hud);
  map.fire('movestart', GESTURE);
  map.fire('moveend', GESTURE);
  v.runTimers();
  assert.strictEqual(storage.m.size, 0);
});

const saved = (v, storage) => JSON.parse(storage.m.get(v._szViewKey(CONFIG)) || 'null');

await ok('a plain wheel notch (no originalEvent) is saved', () => {
  const storage = memStorage();
  const v = loadView({ storage });
  const map = fakeMap();
  v.initViewMemory(map, CONFIG);
  map.input('wheel');
  map.move({ zoom: 16 }, {});
  assert.strictEqual(v.pending(400), 1);
  v.runTimers();
  assert.strictEqual(saved(v, storage).z, 16);
});

await ok('code that cuts into a drag is not saved as the reader\'s', () => {
  const storage = memStorage();
  const v = loadView({ storage });
  const map = fakeMap();
  v.initViewMemory(map, CONFIG);
  map.input('pointerdown');
  map.move({ lng: 7.43 }, GESTURE, true);              // drag, button still down
  map.flyTo({ center: [7.41, 43.73], zoom: 14 });      // no movestart: already moving
  v.runTimers();
  assert.strictEqual(saved(v, storage), null);
});

await ok('a wheel during a flyTo that carries on does not make it the reader\'s', () => {
  const storage = memStorage();
  const v = loadView({ storage });
  const map = fakeMap();
  v.initViewMemory(map, CONFIG);
  map.flyTo({ center: [7.43, 43.74], zoom: 15 }, undefined, true);
  map.input('wheel');
  map.end({});
  v.runTimers();
  assert.strictEqual(saved(v, storage), null);
  // The reader's next move is theirs again.
  map.input('wheel');
  map.move({ zoom: 15.5 }, {});
  v.runTimers();
  assert.strictEqual(saved(v, storage).z, 15.5);
});

await ok('pan then Home: the pan is saved, not a mid-ease or Home camera', () => {
  const storage = memStorage();
  const v = loadView({ storage });
  const map = fakeMap();
  v.initViewMemory(map, CONFIG);
  map.move({ lng: 7.431, zoom: 15 }, GESTURE);          // pan ends, save pending
  assert.strictEqual(v.pending(400), 1);
  v.runTimers(0);                                       // the event loop turns before a click
  map.easeTo({ center: CONFIG.center, zoom: 13 });
  assert.strictEqual(v.pending(400), 0);
  v.runTimers();
  assert.deepStrictEqual(saved(v, storage).c, [7.431, 43.735]);
  assert.strictEqual(saved(v, storage).z, 15);
});

await ok('a new movestart cancels the pending save', () => {
  const storage = memStorage();
  const v = loadView({ storage });
  const map = fakeMap();
  v.initViewMemory(map, CONFIG);
  map.move({ zoom: 15 }, GESTURE);
  map.start({});                                        // unmarked, no recent input
  assert.strictEqual(v.pending(400), 0);
  map.end({});
  v.runTimers();
  assert.strictEqual(saved(v, storage), null);
});

await ok('a small rotate is saved after its snap to north', () => {
  const storage = memStorage();
  const v = loadView({ storage });
  const map = fakeMap();
  v.initViewMemory(map, CONFIG);
  map.input('touchstart');
  map.move({ bearing: -3.4 }, GESTURE);
  // MapLibre calls resetNorth() from inside that moveend, without an event.
  const fire = map.fire;
  let snapped = false;
  map.fire = function(t, e) {
    fire.call(this, t, e);
    if (t === 'moveend' && !snapped) { snapped = true; map.easeTo({ bearing: 0 }, undefined); }
  };
  map.cam.bearing = -3.4;
  map.start(GESTURE); map.end(GESTURE);
  v.runTimers();
  assert.strictEqual(map.cam.bearing, 0);
  assert.strictEqual(saved(v, storage).b, undefined);
});

await ok('opening, deep links and other programmatic moves save nothing', () => {
  const storage = memStorage();
  const v = loadView({ storage });
  v._szWriteView(CONFIG, storage, { lng: 7.43, lat: 43.73, zoom: 16 });
  const before = storage.m.get(v._szViewKey(CONFIG));
  const map = fakeMap();
  v.initViewMemory(map, CONFIG);
  map.fire('moveend', {});                            // the opening camera
  map.fire('movestart', {}); map.fire('moveend', {}); // unmarked moves
  map.flyTo({ center: [7.41, 43.74], zoom: 16 });      // #map= / #pin= deep link
  map.easeTo({ center: CONFIG.center, zoom: 13 });     // Home
  assert.strictEqual(v.pending(400), 0);
  assert.strictEqual(storage.m.get(v._szViewKey(CONFIG)), before);
});

await ok('a changed opening view in map-config drops the saved view', () => {
  const v = loadView({ storage: memStorage() });
  const s = v._szStorage();
  v._szWriteView(CONFIG, s, { lng: 7.43, lat: 43.73, zoom: 16 });
  assert.ok(v._szReadView(CONFIG, s));
  // Same name and bounds (the key), new --map-center / --map-zoom.
  assert.strictEqual(v._szViewKey({ ...CONFIG, center: [7.41, 43.73] }), v._szViewKey(CONFIG));
  assert.strictEqual(v._szReadView({ ...CONFIG, center: [7.41, 43.73] }, s), null);
  assert.strictEqual(v._szReadView({ ...CONFIG, zoom: 12 }, s), null);
  // A view saved without the fingerprint is not trusted either.
  s.m.set(v._szViewKey(CONFIG), '{"c":[7.43,43.73],"z":16}');
  assert.strictEqual(v._szReadView(CONFIG, s), null);
});

await ok('no storage: memory is off and nothing throws', () => {
  const v = loadView({ storage: 'throws' });
  const map = fakeMap();
  v.initViewMemory(map, CONFIG);
  map.fire('moveend', GESTURE);
  assert.strictEqual(v.pending(), 0);
});

await ok('Home returns to the config view, and not while driving', () => {
  const v = loadView({});
  const map = fakeMap();
  v.initHomeButton(map, CONFIG);
  const [ctrl, pos] = map.controls[0];
  assert.strictEqual(pos, 'top-right');
  const div = ctrl.onAdd(map);
  const btn = div.children[0];
  assert.match(btn.attrs['aria-label'], /whole map/);
  btn.on.click();
  assert.deepStrictEqual(map.eased, { center: CONFIG.center, zoom: 13, bearing: 0, pitch: 0, duration: 800 });
  map.eased = null;
  const hud = v.document._el('div'); hud.id = 'drive-hud'; hud.classList.add('visible');
  v.document.body.appendChild(hud);
  btn.on.click();
  assert.strictEqual(map.eased, null);
});

await ok('About text from new and old map-config.json', () => {
  const v = loadView({});
  const full = v._szAboutText({ name: 'Monaco', title: 'OSM - Monaco', description: 'Offline map.',
    buildDate: '2026/09', generator: 'streetzim 1.0.0' });
  assert.strictEqual(full.title, 'OSM - Monaco');
  assert.strictEqual(full.desc, 'Offline map.');
  assert.strictEqual(full.meta, 'Built September 2026 with streetzim 1.0.0 · viewer: streetzim '
    + v.SZ_VIEWER_VERSION);
  const old = v._szAboutText({ name: 'Hawaii', buildDate: '2026/04' });
  assert.strictEqual(old.title, 'Hawaii');
  assert.strictEqual(old.desc, '');
  assert.match(old.meta, /^Built April 2026 · viewer: streetzim /);
  assert.strictEqual(v._szAboutText(null).title, 'Offline OpenStreetMap');
  assert.strictEqual(v._szMonth('sometime'), 'sometime');
  assert.strictEqual(v._szMonth('2026-07-14'), 'July 2026');
  assert.strictEqual(v._szAboutText({ generator: 'streetzim 2.0.0' }).meta.split(' \u00b7 ')[0], 'Built with streetzim 2.0.0');
  const doc = fakeDocument();
  const w = loadView({ document: doc });
  for (const id of ['about-title', 'about-desc', 'about-meta']) {
    const e = doc._el('p'); e.id = id; doc.body.appendChild(e);
  }
  w.initAbout({ name: '<b>x</b>' });
  assert.strictEqual(doc.getElementById('about-title').textContent, '<b>x</b>');
});

await ok('viewer version matches streetzim/__about__.py', () => {
  const about = fs.readFileSync(`${REPO}/streetzim/__about__.py`, 'utf8');
  const m = /__version__\s*=\s*"([^"]+)"/.exec(about);
  assert.ok(m);
  assert.strictEqual(loadView({}).SZ_VIEWER_VERSION, m[1]);
});

function runFatal(window) {
  const document = fakeDocument();
  new Function('window', 'document', 'location', 'navigator', FATAL_SRC)(
    window, document, { href: 'zim://x/C/index.html', reload() {} }, { userAgent: 'OldWebView' });
  return document;
}

await ok('a browser without Fetch gets the full-page explanation', () => {
  const window = { Promise, maplibregl: {} };
  const doc = runFatal(window);
  assert.deepStrictEqual(window.__szUnsupported, ['Fetch API']);
  const page = doc.getElementById('sz-fatal');
  assert.ok(page, 'page shown');
  assert.match(page.text(), /cannot show the map/);
  assert.match(page.text(), /missing: Fetch API/);
  assert.match(page.text(), /OldWebView/);
});

await ok('missing MapLibre is reported; a full browser sees nothing', () => {
  const w1 = { fetch() {}, Promise };
  runFatal(w1);
  assert.match(w1.__szUnsupported.join(), /MapLibre/);
  const w2 = { fetch() {}, Promise, maplibregl: {} };
  const doc = runFatal(w2);
  assert.strictEqual(w2.__szUnsupported, undefined);
  assert.strictEqual(doc.getElementById('sz-fatal'), null);
});

await ok('the main script stays out when the browser is unsupported', () => {
  assert.match(HTML, /if \(window\.__szUnsupported\) return;/);
  assert.match(HTML, /if \(!window\.__szUnsupported\) fetchConfig\(1\)/);
  // 025 runs before the main script does.
  assert.ok(HTML.indexOf('window.__szUnsupported = missing') < HTML.indexOf('maplibregl.addProtocol'));
});

console.log(`\n${pass} passed`);
