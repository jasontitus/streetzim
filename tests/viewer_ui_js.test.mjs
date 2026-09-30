// The viewer's last-view memory, Home button and About text
// (140-view-home-about.js) and the full-page error for a browser that
// cannot run it (025-home-about-fatal.html). Runs the code straight out of
// resources/viewer/index.html against stubbed browser globals.
//
//   node tests/viewer_ui_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

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
    ' _szReadView, _szWriteView, _szOpeningCamera, _szMaxBounds, _szClearZoom, initViewMemory, initHomeButton,' +
    ' _szAboutText, _szMonth, initAbout, _szSatellite, _szSatelliteCreditHtml,' +
    ' szLocaleUnit, szReadUnit, szWriteUnit, szUnit, szFormatDistance };');
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

await ok('maxBounds is the built box itself: no margin of empty map around it', () => {
  const v = loadView({});
  assert.deepStrictEqual(v._szMaxBounds(CONFIG), [[7.40, 43.72], [7.44, 43.76]]);
  // Across the antimeridian the box is unwrapped (east past 180) and passes through.
  assert.deepStrictEqual(v._szMaxBounds({ bounds: [172.8, -23.2, 183.5, -11.2] }), [[172.8, -23.2], [183.5, -11.2]]);
  for (const bad of [undefined, null, [], [1, 2, 3], [7.44, 43.72, 7.40, 43.76], [7.4, 43.76, 7.44, 43.72],
                     ['7.4', 43.72, 7.44, 43.76], [7.4, NaN, 7.44, 43.76]]) {
    assert.strictEqual(v._szMaxBounds({ bounds: bad }), undefined, JSON.stringify(bad));
  }
  assert.strictEqual(v._szMaxBounds({}), undefined);
  // World and near-world boxes: no maxBounds (a 360-degree one makes
  // MapLibre 5.23 throw in _calcMatrices and the viewer shows "Error
  // loading map"). World ZIMs are built with -180,-85,180,85.
  for (const w of [[-180, -85, 180, 85], [-180, -85.0511, 180, 85.0511], [-180, -90, 180, 90],
                   [-180, -60, 180, 75], [-179.95, -85, 179.95, 85], [100, -60, 460, 75]]) {
    assert.strictEqual(v._szMaxBounds({ bounds: w }), undefined, JSON.stringify(w));
  }
  assert.deepStrictEqual(v._szMaxBounds({ bounds: [-179.9, -85, 179.9, 85] }), [[-179.9, -85], [179.9, 85]]);
  // Latitudes past Web Mercator's limit are clamped to it.
  assert.deepStrictEqual(v._szMaxBounds({ bounds: [-10, -90, 10, 90] }), [[-10, -85.0511], [10, 85.0511]]);
  assert.deepStrictEqual(v._szMaxBounds({ bounds: [-170, -85.06, 170, 85.06] }), [[-170, -85.0511], [170, 85.0511]]);
  assert.strictEqual(v._szMaxBounds({ bounds: [0, 86, 10, 89] }), undefined);
  // The map is built with it (the old code padded by 0.01 degrees, which
  // showed as a blank strip -- the sea cut off -- at every edge).
  assert.match(HTML, /maxBounds: _szMaxBounds\(config\)/);
  assert.doesNotMatch(HTML, /config\.bounds\[\d\] [-+] 0\.01/);
});

await ok('a result held at the box edge under a sheet is cleared by zooming in', () => {
  const v = loadView({});
  // 844 px phone, 200 px strip at the bottom, 110 px of search box on top.
  const Z = (y, z, yN, yS) => v._szClearZoom(y, 844, 110, 200, z, 20, yN, yS);
  assert.strictEqual(Z(312, 17, -5000, 5000), null);             // clear: nothing to do
  // Under the strip, 100 px from the south edge: zoom in log2(240/100).
  assert.ok(Math.abs(Z(744, 17, -5000, 844) - (17 + Math.log2(2.4))) < 1e-9);
  // Above the screen (MapLibre's offset at the north edge), 32 px below the
  // north edge: log2(150/32).
  assert.ok(Math.abs(Z(-78, 15, -110, 3000) - (15 + Math.log2(150 / 32))) < 1e-9);
  // Hidden but far from any edge: recentre at the same zoom.
  assert.strictEqual(Z(800, 15, -3000, 3000), 15);
  // Capped at maxZoom; nothing to do on the edge itself.
  assert.strictEqual(Z(834, 18, -5000, 844), 20);
  assert.strictEqual(Z(844, 17, -5000, 844), null);
  // The find strip, search and wiki fly through it.
  for (const pat of [/_szFlyToClear\(map, \{\s*center: \[r\.o, r\.a\]/, /_szFlyToClear\(map, \{ center: \[lon, lat\], zoom: zoom/,
                     /_szFlyToClear\(map, \{ center: \[lon, lat\], zoom: targetZoom/, /_szFlyToClear\(map, \{ center: \[lng, lat\]/]) {
    assert.match(HTML, pat);
  }
});

await ok('units: one setting for every distance, locale default, kept across visits', () => {
  const v = loadView({ storage: memStorage() });
  const f = v.szFormatDistance;
  for (const [m, unit, want] of [[850, 'metric', '850 m'], [1400, 'metric', '1.4 km'], [12400, 'metric', '12 km'],
                                 [150, 'imperial', '492 ft'], [1500, 'imperial', '0.9 mi'], [2253, 'imperial', '1.4 mi'],
                                 [19312, 'imperial', '12 mi'], [null, 'metric', ''], [NaN, 'imperial', '']]) {
    assert.strictEqual(f(m, unit), want, `${m} ${unit}`);
  }
  // Nothing saved: the locale decides. Imperial for US English and a US,
  // Liberian or Myanmar region; metric otherwise (en-GB included).
  const nav = (...l) => ({ languages: l, language: l[0] });
  for (const [n, want] of [[nav('en-US'), 'imperial'], [nav('en-us', 'de'), 'imperial'],
                           [nav('es-US'), 'imperial'], [nav('en-LR'), 'imperial'], [nav('my-MM'), 'imperial'],
                           [nav('de-CH'), 'metric'], [nav('en-GB'), 'metric'], [nav('fr'), 'metric'],
                           [nav('en'), 'metric'], [nav('de-CH', 'en-US'), 'metric'],
                           [nav('zh-Hant-TW'), 'metric'], [{ language: 'en-US' }, 'imperial'],
                           [{}, 'metric'], [null, 'metric']]) {
    assert.strictEqual(v.szLocaleUnit(n), want, JSON.stringify(n));
  }
  const s = v._szStorage();
  assert.strictEqual(v.szReadUnit(s, nav('en-US')), 'imperial');
  assert.strictEqual(v.szReadUnit(s, nav('de-CH')), 'metric');
  // A saved choice wins over the locale, both ways.
  v.szWriteUnit(s, 'metric');
  assert.strictEqual(v.szReadUnit(s, nav('en-US')), 'metric');
  v.szWriteUnit(s, 'imperial');
  assert.strictEqual(v.szReadUnit(s, nav('de-CH')), 'imperial');
  assert.strictEqual(v.szUnit(), 'imperial');                        // no map yet: the stored choice
  v.window.__szMap = { _streetzimUnit: 'metric' };
  assert.strictEqual(v.szUnit(), 'metric');                          // the map's live setting wins
  assert.strictEqual(v.szReadUnit(null, nav('en-GB')), 'metric');
  assert.strictEqual(v.szReadUnit({ getItem() { throw new Error('SecurityError'); } }, nav('en-US')), 'imperial');
  // The scale bar starts from and saves the setting; Find cards, "Nearby",
  // the place sheet and the routing panel format through it.
  assert.match(HTML, /var scaleUnit = szReadUnit\(_szStorage\(\)\);/);
  assert.match(HTML, /szWriteUnit\(_szStorage\(\), scaleUnit\);/);
  assert.match(HTML, /function _formatDistanceStrip\(m\) \{\s*return szFormatDistance\(m, szUnit\(\)\);/);
  assert.match(HTML, /'Nearby \(within ' \+ szFormatDistance\(1500, szUnit\(\)\) \+ '\)'/);
  assert.match(HTML, /function formatDistance\(meters\) \{\s*return szFormatDistance\(meters, szUnit\(\)\);/);
  assert.doesNotMatch(HTML, /1\.5 km/);
  // places.html reads the same key and prints the same strings.
  const P = fs.readFileSync(`${REPO}/resources/viewer/places.html`, 'utf8');
  const src = P.slice(P.indexOf('function distanceUnit(nav)'), P.indexOf('\n}\n', P.indexOf('function formatDistance(m, unit)')) + 3);
  const store = new Map();
  const window = { localStorage: { getItem: (k) => store.get(k) ?? null } };
  const places = new Function('window', src + '\nreturn { distanceUnit, formatDistance };')(window);
  // Same locale default and the same saved-choice rule as the viewer.
  for (const n of [nav('en-US'), nav('de-CH'), nav('en-GB'), nav('my-MM'), nav('en'), {}, null]) {
    assert.strictEqual(places.distanceUnit(n), v.szLocaleUnit(n), JSON.stringify(n));
  }
  store.set('streetzim.units', 'metric');
  assert.strictEqual(places.distanceUnit(nav('en-US')), 'metric');
  store.set('streetzim.units', 'imperial');
  assert.strictEqual(places.distanceUnit(nav('de-CH')), 'imperial');
  for (const m of [150, 850, 1400, 2253, 12400, 19312]) {
    for (const unit of ['metric', 'imperial']) assert.strictEqual(places.formatDistance(m, unit), f(m, unit));
  }
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

await ok('About and credits name the satellite source and flag non-commercial imagery', () => {
  const v = loadView({});
  const free = { hasSatellite: true, satelliteSource: 's2cloudless-2016', satelliteLicense: 'CC BY 4.0',
    satelliteLicenseUrl: 'https://creativecommons.org/licenses/by/4.0/',
    satelliteAttribution: 'EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH' +
      ' (Contains modified Copernicus Sentinel data 2016 & 2017)',
    satelliteNonCommercial: false };
  const nc = Object.assign({}, free, { satelliteSource: 's2cloudless-2021',
    satelliteLicense: 'CC BY-NC-SA 4.0', satelliteNonCommercial: true,
    satelliteLicenseUrl: 'https://creativecommons.org/licenses/by-nc-sa/4.0/',
    satelliteAttribution: 'EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH' +
      ' (Contains modified Copernicus Sentinel data 2021)' });
  assert.strictEqual(v._szSatellite({}), null);
  assert.strictEqual(v._szAboutText({}).notice, '');
  assert.strictEqual(v._szAboutText(free).notice, '');
  assert.strictEqual(v._szAboutText(free).satLicense,
    'CC BY 4.0 \u2014 creativecommons.org/licenses/by/4.0/');
  assert.match(v._szAboutText(nc).notice, /^Restricted: .*CC BY-NC-SA 4\.0.*non-commercial/);
  assert.match(v._szAboutText(nc).satLicense, /^CC BY-NC-SA 4\.0, non-commercial use only/);
  // A ZIM from before the choice: the 2021 mosaic, non-commercial.
  const old = v._szSatellite({ hasSatellite: true });
  assert.ok(old.nonCommercial && old.license === 'CC BY-NC-SA 4.0');
  for (const [cfg, restricted] of [[nc, true], [free, false], [{ name: 'x' }, false]]) {
    const doc = fakeDocument();
    const w = loadView({ document: doc });
    for (const id of ['about-title', 'about-desc', 'about-meta', 'about-notice',
                      'attr-satellite-by', 'attr-satellite-license']) {
      const e = doc._el('p'); e.id = id; e.textContent = 'static'; doc.body.appendChild(e);
    }
    w.initAbout(cfg);
    assert.strictEqual(doc.getElementById('about-notice').style.display, restricted ? '' : 'none');
    assert.strictEqual(doc.getElementById('attr-satellite-by').textContent,
      cfg.hasSatellite ? cfg.satelliteAttribution : 'static');
  }
});

await ok('on-map satellite credit: short, linked, escaped, wired into the source', () => {
  const v = loadView({});
  const free = v._szSatelliteCreditHtml({ hasSatellite: true, satelliteYear: '2016',
    satelliteLicense: 'CC BY 4.0', satelliteLicenseUrl: 'https://creativecommons.org/licenses/by/4.0/',
    satelliteAttribution: 'EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (x)',
    satelliteNonCommercial: false });
  assert.strictEqual(free, '&copy; <a href="https://cloudless.eox.at" target="_blank" rel="noopener">EOxCloudless</a> 2016' +
    ' by <a href="https://eox.at" target="_blank" rel="noopener">EOX</a> &middot; ' +
    '<a href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noopener">CC BY 4.0</a>');
  const nc = v._szSatelliteCreditHtml({ hasSatellite: true, satelliteYear: '2021', satelliteLicense: 'CC BY-NC-SA 4.0',
    satelliteLicenseUrl: 'https://creativecommons.org/licenses/by-nc-sa/4.0/', satelliteAttribution: 'a',
    satelliteNonCommercial: true });
  assert.match(nc, /EOxCloudless<\/a> 2021 .*CC BY-NC-SA 4\.0<\/a> \(non-commercial\)$/);
  assert.match(v._szSatelliteCreditHtml({ hasSatellite: true }), /EOxCloudless<\/a> 2021 .*CC BY-NC-SA 4\.0/);
  assert.strictEqual(v._szSatelliteCreditHtml({}), '');
  // map-config values are data: escaped, and only a CC 4.0 URL becomes a link.
  const bad = v._szSatelliteCreditHtml({ hasSatellite: true, satelliteYear: '<img src=x onerror=alert(1)>',
    satelliteLicense: '<b>L</b>', satelliteLicenseUrl: 'javascript:alert(1)', satelliteAttribution: 'a' });
  assert.ok(!bad.includes('<img') && !bad.includes('<b>') && !bad.includes('javascript:'));
  assert.match(bad, /&lt;img src=x onerror=alert\(1\)&gt;/);
  const init = fs.readFileSync(`${REPO}/resources/viewer/src/index/120-map-init-and-style.js`, 'utf8');
  assert.match(init, /var satCredit = _szSatelliteCreditHtml\(config\);/);
  assert.match(init, /map\.addSource\('satellite', \{\s*type: 'raster',\s*tiles: \[[^\]]*\],\s*attribution: satCredit,/);
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

// ---- Find chip rail: which chips this ZIM can serve (240-on-map-find-ui.js)

const CHIP_AVAIL_SRC = slice('// BEGIN chip-availability', '// END chip-availability');
const RESOLVE_SRC = slice('async function _findResolveChipDef', 'async function loadChipOnMap');
// env.manifest: what category-index/manifest.json serves (200), or
// env.fetch: a fetch stub. env.document: a fake DOM for the rail.
function loadChips(env = {}) {
  const fetch = env.fetch || ((u) => Promise.resolve({ ok: true, status: 200,
    json: () => Promise.resolve(env.manifest) }));
  const fn = new Function('baseUrl', 'fetch', 'document', '_showFindToast',
    CHIP_AVAIL_SRC + RESOLVE_SRC + '\nreturn { _findChipHas, _findChipsPlan, _findResolveChipDef,' +
    ' _findFetchCatManifest, _findChipsReconcile, _findChipsApply, _findChipsRecheck,' +
    ' _findChipUnavailable, cached: () => _findCatManifest };');
  const toasts = env.toasts || [];
  return fn('http://z/C/', fetch, env.document || railDocument([]).document,
    (t) => toasts.push(t));
}
// fetch stub answering the manifest URL from a script of responses:
// a number is an HTTP status (200 serves `manifest`), 'net' throws.
function scriptedFetch(script, manifest) {
  const calls = [];
  const fetch = (u) => {
    calls.push(u);
    const step = script.length > 1 ? script.shift() : script[0];
    if (step === 'net') return Promise.reject(new TypeError('Failed to fetch'));
    return Promise.resolve({ ok: step === 200, status: step,
                             json: () => Promise.resolve(manifest) });
  };
  return { fetch, calls };
}
// A rail of chip buttons, enough DOM for _findChipsApply.
function railDocument(ids) {
  const input = { id: 'search-input', focus() { doc.activeElement = input; } };
  const btns = ids.map((id) => {
    // kbd: focused from the keyboard (matches :focus-visible).
    const b = { hidden: false, dataset: { chip: id }, cls: new Set(), kbd: false,
                classList: { contains: (c) => b.cls.has(c) },
                matches: (q) => q === ':focus-visible' && b.kbd,
                focus(kbd = true) { doc.activeElement = b; b.kbd = kbd; },
                blur() { if (doc.activeElement === b) doc.activeElement = null; } };
    return b;
  });
  const rail = { hidden: false, querySelectorAll: () => btns };
  const doc = { activeElement: null,
                getElementById: (id) => (id === 'search-input' ? input : id === 'find-chips' ? rail : null) };
  return { document: doc, rail, btns, input };
}
const flush = () => new Promise((r) => setTimeout(r, 0));
const RAIL = ['food', 'bars', 'shops', 'health', 'museums', 'landmarks',
              'libraries', 'parks', 'fuel', 'hotels'];
const chipEntry = (count) => ({ label: 'x', count, bytes: 10 });
const shown = (plan) => RAIL.filter((id) => plan.show[id]);

await ok('chips: no chip data in the manifest hides the whole rail', () => {
  const { _findChipsPlan } = loadChips();
  // Built without --split-find-chips: a manifest with categories, no chips.
  for (const m of [{ total: 5, categories: { poi: 3 } }, { chips: {} }, {}]) {
    const plan = _findChipsPlan(m, RAIL);
    assert.strictEqual(plan.rail, false, JSON.stringify(m));
    assert.deepStrictEqual(shown(plan), []);
  }
});

await ok('chips: an unreadable manifest keeps every chip', () => {
  const { _findChipsPlan } = loadChips();
  for (const m of [null, { _szUnknown: true }]) {
    const plan = _findChipsPlan(m, RAIL);
    assert.strictEqual(plan.rail, true);
    assert.deepStrictEqual(shown(plan), RAIL);
  }
});

await ok('chips: count-0 chips are hidden (tilemaker Parks / Gas)', () => {
  const { _findChipsPlan } = loadChips();
  const chips = Object.fromEntries(RAIL.map((id) => [id, chipEntry(7)]));
  chips.parks = chipEntry(0);
  chips.fuel = chipEntry(0);
  const plan = _findChipsPlan({ chips }, RAIL);
  assert.strictEqual(plan.rail, true);
  assert.deepStrictEqual(shown(plan), RAIL.filter((id) => id !== 'parks' && id !== 'fuel'));
  // Every listed chip empty: nothing to show, rail hidden.
  const empty = Object.fromEntries(RAIL.map((id) => [id, chipEntry(0)]));
  assert.strictEqual(_findChipsPlan({ chips: empty }, RAIL).rail, false);
  // An entry without a count (older manifests) counts as present.
  assert.ok(_findChipsPlan({ chips: { bars: { label: 'Bars' } } }, RAIL).show.bars);
});

await ok('chips: Food & Drink stands for a pre-merge restaurants/cafes pair', () => {
  const { _findChipsPlan } = loadChips();
  const plan = _findChipsPlan({ chips: { restaurants: chipEntry(3), cafes: chipEntry(0) } }, RAIL);
  assert.deepStrictEqual(shown(plan), ['food']);
  assert.deepStrictEqual(shown(_findChipsPlan({ chips: { cafes: chipEntry(0) } }, RAIL)), []);
  // A chip set the rail does not know at all: better all than none.
  assert.deepStrictEqual(shown(_findChipsPlan({ chips: { zzz: chipEntry(4) } }, RAIL)), RAIL);
});

await ok('chips: a tap resolves to nothing when the ZIM cannot serve it', async () => {
  const noChips = loadChips({ manifest: { total: 1, categories: { poi: 1 } } });
  assert.strictEqual(await noChips._findResolveChipDef({ id: 'food', label: 'Food & Drink' }), null);
  const tm = loadChips({ manifest: { chips: { parks: chipEntry(0), bars: chipEntry(2),
                                  restaurants: chipEntry(1), cafes: chipEntry(0) } } });
  assert.strictEqual(await tm._findResolveChipDef({ id: 'parks', label: 'Parks' }), null);
  assert.deepStrictEqual((await tm._findResolveChipDef({ id: 'bars', label: 'Bars' })).sources, ['bars']);
  assert.deepStrictEqual((await tm._findResolveChipDef({ id: 'food', label: 'Food' })).sources,
    ['restaurants']);
  // Manifest unreadable (a 503 here): try the chip's own file.
  const unk = loadChips({ fetch: scriptedFetch([503]).fetch });
  assert.deepStrictEqual((await unk._findResolveChipDef({ id: 'bars', label: 'Bars' })).sources, ['bars']);
});

await ok('chips: hidden chips and rail are really hidden, and taps always say something', () => {
  // `display: inline-flex` on the chip outranks the UA [hidden] rule.
  assert.match(HTML, /#find-chips\[hidden\], #find-chips \.find-chip\[hidden\] \{ display: none; \}/);
  assert.match(HTML, /No category search in this map/);
  // Legacy-layout chip that loaded nothing, and a failed fetch, both toast.
  const legacy = slice("console.warn('[streetzim] chip fetch failed:'", 'var bounds = map.getBounds();');
  assert.match(legacy, /_showFindToast\('Couldn\\u2019t load '/);
  assert.match(legacy, /if \(!Array\.isArray\(data\) \|\| !data\.length\) \{\s*_showFindToast\('No '/);
});

await ok('manifest: only 404 / 410 mean "no chip data"; the answer is cached', async () => {
  for (const status of [404, 410]) {
    const f = scriptedFetch([status]);
    const c = loadChips({ fetch: f.fetch });
    assert.deepStrictEqual(await c._findFetchCatManifest(), {}, String(status));
    assert.deepStrictEqual(c.cached(), {});
    await c._findFetchCatManifest();
    assert.strictEqual(f.calls.length, 1, 'cached, not fetched again');
    assert.strictEqual(c._findChipsPlan(await c._findFetchCatManifest(), RAIL).rail, false);
  }
  const f = scriptedFetch([200], { chips: { bars: chipEntry(2) } });
  const c = loadChips({ fetch: f.fetch });
  assert.deepStrictEqual(await c._findFetchCatManifest(), { chips: { bars: chipEntry(2) } });
  assert.strictEqual(f.calls[0], 'http://z/C/category-index/manifest.json');
});

await ok('manifest: 5xx and network errors are unknown, not cached, and retried', async () => {
  for (const first of [500, 503, 502, 'net']) {
    const f = scriptedFetch([first, 200], { chips: { bars: chipEntry(2) } });
    const c = loadChips({ fetch: f.fetch });
    const m1 = await c._findFetchCatManifest();
    assert.deepStrictEqual(m1, { _szUnknown: true }, String(first));
    assert.strictEqual(c.cached(), null);
    // The rail stays whole on an unknown answer.
    assert.deepStrictEqual(shown(c._findChipsPlan(m1, RAIL)), RAIL);
    const m2 = await c._findFetchCatManifest();
    assert.deepStrictEqual(m2, { chips: { bars: chipEntry(2) } });
    assert.strictEqual(f.calls.length, 2);
    await c._findFetchCatManifest();
    assert.strictEqual(f.calls.length, 2, 'the real manifest is cached');
  }
});

await ok('reconcile: runs again once a later fetch reads the real manifest', async () => {
  const d = railDocument(RAIL);
  const f = scriptedFetch([503, 200], { chips: { bars: chipEntry(3), parks: chipEntry(0) } });
  const c = loadChips({ fetch: f.fetch, document: d.document });
  await c._findChipsReconcile(d.rail);
  assert.ok(d.btns.every((b) => !b.hidden) && !d.rail.hidden, '503: full rail kept');
  await c._findFetchCatManifest();          // e.g. a chip tap, later
  await flush();
  assert.deepStrictEqual(d.btns.filter((b) => !b.hidden).map((b) => b.dataset.chip), ['bars']);
  // No chips key at all, after an unknown first answer: rail goes.
  const d2 = railDocument(RAIL);
  const c2 = loadChips({ fetch: scriptedFetch(['net', 200], { total: 3 }).fetch, document: d2.document });
  await c2._findChipsReconcile(d2.rail);
  assert.strictEqual(d2.rail.hidden, false);
  await c2._findFetchCatManifest();
  assert.strictEqual(d2.rail.hidden, true);
});

await ok('reconcile: leaves an active or loading chip alone', () => {
  const d = railDocument(RAIL);
  const c = loadChips({ document: d.document });
  d.btns[7].dataset.szLoading = '1';       // parks, tap still resolving
  d.btns[8].cls.add('on');                 // fuel, active
  c._findChipsApply(d.rail, { chips: { bars: chipEntry(1), parks: chipEntry(0), fuel: chipEntry(0) } });
  assert.deepStrictEqual(d.btns.filter((b) => !b.hidden).map((b) => b.dataset.chip),
    ['bars', 'parks', 'fuel']);
  // Even with no chip data the rail stays while a chip is busy.
  const d2 = railDocument(RAIL);
  const c2 = loadChips({ document: d2.document });
  d2.btns[0].dataset.szLoading = '1';
  c2._findChipsApply(d2.rail, {});
  assert.strictEqual(d2.rail.hidden, false);
  assert.deepStrictEqual(d2.btns.filter((b) => !b.hidden).map((b) => b.dataset.chip), ['food']);
});

await ok('reconcile: focus moves off a chip before it is hidden', () => {
  const d = railDocument(RAIL);
  const c = loadChips({ document: d.document });
  d.btns[7].focus();                       // parks, count 0
  c._findChipsApply(d.rail, { chips: { bars: chipEntry(1), parks: chipEntry(0), hotels: chipEntry(2) } });
  assert.strictEqual(d.document.activeElement, d.btns[9], 'next visible chip (hotels)');
  const d2 = railDocument(RAIL);
  const c2 = loadChips({ document: d2.document });
  d2.btns[9].focus();                      // hotels; wraps to the first visible
  c2._findChipsApply(d2.rail, { chips: { bars: chipEntry(1), hotels: chipEntry(0) } });
  assert.strictEqual(d2.document.activeElement, d2.btns[1]);
  const d3 = railDocument(RAIL);
  const c3 = loadChips({ document: d3.document });
  d3.btns[2].focus();                      // whole rail goes: the search box
  c3._findChipsApply(d3.rail, { total: 1 });
  assert.strictEqual(d3.rail.hidden, true);
  assert.strictEqual(d3.document.activeElement, d3.input);
  // After a tap (no :focus-visible) focus is dropped, never sent to the
  // search box — that would pop the soft keyboard on Android.
  const d5 = railDocument(RAIL);
  const c5 = loadChips({ document: d5.document });
  d5.btns[7].focus(false);
  c5._findChipsApply(d5.rail, { chips: { bars: chipEntry(1), parks: chipEntry(0) } });
  assert.strictEqual(d5.document.activeElement, null);
  // Focus elsewhere is not touched.
  const d4 = railDocument(RAIL);
  const c4 = loadChips({ document: d4.document });
  d4.btns[1].focus();
  c4._findChipsApply(d4.rail, { chips: { bars: chipEntry(1) } });
  assert.strictEqual(d4.document.activeElement, d4.btns[1]);
});

await ok('reconcile: the rail goes whenever no chip is left visible', () => {
  const d = railDocument(['bars', 'parks']);
  const c = loadChips({ document: d.document });
  // An unrecognised chip set keeps all; one recognised chip at 0 hides all.
  c._findChipsApply(d.rail, { chips: { parks: chipEntry(0) } });
  assert.strictEqual(d.rail.hidden, true);
  assert.ok(d.btns.every((b) => b.hidden));
});

await ok('unresolved tap: chip hidden, rail hidden when it was the last, tap focus dropped', () => {
  const d = railDocument(['bars', 'parks', 'fuel']);
  const toasts = [];
  const c = loadChips({ document: d.document, toasts,
    manifest: { chips: { bars: chipEntry(1), parks: chipEntry(0), fuel: chipEntry(0) } } });
  return c._findFetchCatManifest().then(() => {
    d.btns[1].focus(false);                 // tapped Parks
    c._findChipUnavailable({ id: 'parks', label: 'Parks' });
    assert.deepStrictEqual(d.btns.map((b) => b.hidden), [false, true, false]);
    assert.strictEqual(d.rail.hidden, false);
    assert.strictEqual(d.document.activeElement, null, 'blurred, not the search box');
    assert.deepStrictEqual(toasts, ['No parks in this map']);
    // Bars and Gas go too (Gas by keyboard): the rail goes, focus to the input.
    d.btns[0].hidden = true;
    d.btns[2].focus(true);
    c._findChipUnavailable({ id: 'fuel', label: 'Gas' });
    assert.strictEqual(d.rail.hidden, true);
    assert.strictEqual(d.document.activeElement, d.input);
  });
});

await ok('unresolved tap on a ZIM with no chip data hides the rail', async () => {
  const d = railDocument(RAIL);
  const toasts = [];
  const c = loadChips({ document: d.document, toasts, manifest: { total: 3 } });
  await c._findFetchCatManifest();
  c._findChipUnavailable({ id: 'food', label: 'Food & Drink' });
  assert.strictEqual(d.rail.hidden, true);
  assert.deepStrictEqual(toasts, ['No category search in this map']);
});

// A fetch whose answers the test releases one at a time.
function deferredFetch(manifest) {
  const pending = [];
  const fetch = () => new Promise((res, rej) => pending.push({ res, rej }));
  const answer = (status) => {
    const p = pending.shift();
    if (status === 'net') p.rej(new TypeError('Failed to fetch'));
    else p.res({ ok: status === 200, status, json: () => Promise.resolve(manifest) });
    return flush();
  };
  return { fetch, pending, answer };
}

await ok('manifest: concurrent callers share one fetch', async () => {
  const f = deferredFetch({ chips: { bars: chipEntry(1) } });
  const d = railDocument(RAIL);
  const c = loadChips({ fetch: f.fetch, document: d.document });
  const r = c._findChipsReconcile(d.rail);
  const tap = c._findFetchCatManifest();
  assert.strictEqual(f.pending.length, 1, 'one request in flight');
  await f.answer(200);
  await r;
  assert.deepStrictEqual(await tap, { chips: { bars: chipEntry(1) } });
  assert.deepStrictEqual(d.btns.filter((b) => !b.hidden).map((b) => b.dataset.chip), ['bars']);
});

await ok('reconcile answers first (503), the tap\'s later fetch second (200)', async () => {
  const f = deferredFetch({ chips: { bars: chipEntry(1), parks: chipEntry(0) } });
  const d = railDocument(RAIL);
  const c = loadChips({ fetch: f.fetch, document: d.document });
  const r = c._findChipsReconcile(d.rail);
  await f.answer(503);
  await r;
  assert.ok(d.btns.every((b) => !b.hidden), 'full rail after the 503');
  d.btns[7].dataset.szLoading = '1';       // Parks tapped: its fetch goes out
  const tap = c._findFetchCatManifest();
  assert.strictEqual(f.pending.length, 1);
  await f.answer(200);
  await tap;
  // Reconciled — except the busy Parks chip, which the tap's done() rechecks.
  assert.deepStrictEqual(d.btns.filter((b) => !b.hidden).map((b) => b.dataset.chip), ['bars', 'parks']);
  delete d.btns[7].dataset.szLoading;
  c._findChipsRecheck();
  assert.deepStrictEqual(d.btns.filter((b) => !b.hidden).map((b) => b.dataset.chip), ['bars']);
});

// initFindChips' act(): data-sz-loading while the tap runs, cleared after.
const INIT_CHIPS_SRC = slice('function initFindChips(map)', 'function _findChipPaintActive');
function runInitChips(loadChipOnMap) {
  const btns = [];
  const el = (tag) => {
    const e = { tag, children: [], dataset: {}, on: {}, setAttribute() {},
      appendChild(c) { e.children.push(c); if (tag === 'div') btns.push(c); return c; },
      addEventListener(t, f) { e.on[t] = f; } };
    return e;
  };
  const rail = el('div');
  const document = { createElement: el,
    getElementById: (id) => (id === 'find-chips' ? rail : null) };
  const calls = { recheck: 0, toasts: [] };
  new Function('document', 'EXPLORE_CHIPS', 'loadChipOnMap', '_findChipsReconcile',
    '_findChipsRecheck', '_findChipPaintActive', '_showFindToast', 'console',
    'var _szChipSynth = 0, _chipOrigPlaceholder = null;\n' + INIT_CHIPS_SRC +
    '\ninitFindChips({});')(document, [{ id: 'bars', label: 'Bars', emoji: 'b' }],
    loadChipOnMap, () => {}, () => { calls.recheck++; }, () => {},
    (t) => calls.toasts.push(t), { warn() {} });
  return { btn: btns[0], calls };
}

await ok('act(): a chip is marked loading while its tap runs', async () => {
  let release;
  const run = runInitChips(() => new Promise((r) => { release = r; }));
  run.btn.on.click();
  assert.strictEqual(run.btn.dataset.szLoading, '1');
  release();
  await flush();
  assert.strictEqual(run.btn.dataset.szLoading, undefined);
  assert.strictEqual(run.calls.recheck, 1, 'plan re-applied after the tap');
  // A failed load clears it too, and says so.
  let fail;
  const run2 = runInitChips(() => new Promise((_r, j) => { fail = j; }));
  run2.btn.on.click();
  assert.strictEqual(run2.btn.dataset.szLoading, '1');
  fail(new Error('boom'));
  await flush();
  assert.strictEqual(run2.btn.dataset.szLoading, undefined);
  assert.strictEqual(run2.calls.recheck, 1);
  assert.match(run2.calls.toasts[0], /Couldn.t load bars/);
});

// _findFetchChipData's sub_chunks fan-out (legacy name-hash buckets).
const CHIP_DATA_SRC = slice('var _FIND_CHIP_CACHE_MAX', '// Helper for the "no results" toast');
function loadChipData(manifest, bucketStatus) {
  const calls = [];
  const fetch = (u) => {
    calls.push(u);
    const st = bucketStatus(u);
    if (st === 'net') return Promise.reject(new TypeError('Failed to fetch'));
    return Promise.resolve({ ok: st === 200, status: st, json: () => Promise.resolve([{ n: u }]) });
  };
  const fn = new Function('baseUrl', 'fetch', 'navigator', '_findFetchCatManifest', '_findChipCache',
    CHIP_DATA_SRC + '\nreturn _findFetchChipData;');
  return { get: fn('http://z/C/', fetch, {}, () => Promise.resolve(manifest), new Map()), calls };
}

await ok('chip buckets: all failing rejects ("Couldn\'t load"), some failing still shows the rest', async () => {
  const m = { chips: { shops: { count: 4, sub_chunks: ['0', '1', '2'] } } };
  const all = loadChipData(m, (u) => (/-1\.json$/.test(u) ? 'net' : 503));
  await assert.rejects(all.get('shops'), /all 3 buckets failed/);
  assert.strictEqual(all.calls.length, 3);
  const some = loadChipData(m, (u) => (/-1\.json$/.test(u) ? 500 : 200));
  const recs = await some.get('shops');
  assert.deepStrictEqual(recs.map((r) => r.n.replace('http://z/C/category-index/', '')),
    ['chip-shops-0.json', 'chip-shops-2.json']);
  // An empty bucket that loaded fine is not a failure.
  const empty = loadChipData({ chips: { shops: { count: 0, sub_chunks: ['0'] } } }, () => 200);
  assert.strictEqual((await empty.get('shops')).length, 1);
});

await ok('toast: announced politely, wraps as a box', () => {
  const t = slice('function _showFindToast', '// Generation counter');
  assert.match(t, /setAttribute\('role', 'status'\)/);
  assert.match(t, /setAttribute\('aria-live', 'polite'\)/);
  assert.match(t, /border-radius:18px/);
});

// ---- Files that live beside the /drive/ PWA, never in a ZIM (020, 100)

const STAMP_SRC = slice('var SZ_ON_DRIVE_PWA', '</script>');
const BRIDGE_SRC = slice('(function loadWikiTitleBridge()', '// Resolve the in-ZIM article path');
function runPageLoad(pathname, protocol = 'http:') {
  const fetched = [];
  const fetch = (u) => { fetched.push(u); return new Promise(() => {}); };
  const stamp = { style: {}, textContent: '…', addEventListener() {} };
  const document = { getElementById: (id) => (id === 'viewer-build-stamp' ? stamp : null),
                     body: { classList: { add() {} } } };
  const window = { location: { pathname, protocol, search: '' } };
  new Function('window', 'document', 'fetch', 'baseUrl',
    STAMP_SRC + '\n' + BRIDGE_SRC)(window, document, fetch, 'http://h' + pathname.replace(/[^/]*$/, ''));
  return { fetched, stamp };
}

await ok('kiwix-serve: no build-info.js or wiki-qid-titles.json requests', () => {
  for (const path of ['/content/monaco/index.html', '/viewer#monaco/index.html',
                      '/C/index.html', '/drive/index.html']) {
    const { fetched, stamp } = runPageLoad(path);
    assert.deepStrictEqual(fetched, [], path);
    assert.strictEqual(stamp.style.display, 'none', path);
  }
});

await ok('the /drive/ PWA still loads its stamp and Q-ID bridge', () => {
  for (const path of ['/drive/viewer/', '/drive/viewer/index.html']) {
    const { fetched, stamp } = runPageLoad(path, 'https:');
    assert.strictEqual(fetched.length, 2, path);
    assert.match(fetched[0], /^\/drive\/build-info\.js\?t=/);
    assert.strictEqual(fetched[1], 'http://h/drive/viewer/../wiki-qid-titles.json');
    assert.notStrictEqual(stamp.style.display, 'none');
  }
});

// ---- Search result distance (300-search.js) ------------------------------
const PROX_SRC = slice('// BEGIN proximity-label', '// END proximity-label');
const proxLabel = new Function(PROX_SRC + '\nreturn _szProximityLabel;')();

await ok('search distance: lower-case unit symbols, in the scale bar\'s units', () => {
  const cases = [
    [0.2, undefined, 'nearby'], [1.2, 'imperial', '1 mi'], [12, 'imperial', '10 mi'],
    [123, 'imperial', '120 mi'], [1234, 'imperial', '1200 mi'],
    [0.2, 'metric', 'nearby'], [1.2, 'metric', '2 km'], [12, 'metric', '20 km'],
    [123, 'metric', '200 km'], [1234, 'metric', '2000 km'],
  ];
  for (const [miles, unit, want] of cases) assert.strictEqual(proxLabel(miles, unit), want, `${miles} ${unit}`);
  assert.strictEqual(proxLabel(NaN, 'metric'), '');
  for (const [miles, unit] of cases) assert.match(proxLabel(miles, unit), /^(nearby|\d+ (mi|km))$/);
});

await ok('search results: the subline is printed as written ("1 mi", not "1 Mi")', () => {
  // The whole line used to be text-transform: capitalize ("1 Mi", "Nearby").
  const css = slice('.search-result-type {', '.search-no-results');
  assert.doesNotMatch(css, /text-transform/);
  assert.match(HTML, /proximityLabel = _szProximityLabel\(dist \* 69, map\._streetzimUnit\);/);
});

// ---- the in-ZIM Wikipedia button (100-wiki-bridge-and-viewport.js) ----
// A record's `w` gets a button only when its article was bundled, which
// wiki-geo-index.json lists; before, any `w` did, and most opened a
// missing page in a non-English country's map.
function wikiPath(index, qidTitles) {
  return new Function('WIKI_GEO_INDEX', 'WIKI_GEO_QID', 'WIKI_QID_TITLES',
    slice('// BEGIN wiki-article-path', '// END wiki-article-path') +
    '\nreturn { _wikiArticlePath, _wikiTagTitle };')(
    index, index && Object.fromEntries(Object.entries(index).map(([t, g]) => [g[3], t])),
    qidTitles || null);
}

await ok('a Wikipedia button only for a bundled article', () => {
  const index = { 'Utrecht': [52.09, 5.12, 'admin', 'Q803', ''],
                  'Aalten_(dorp)': [51.92, 6.58, 'place', 'Q2', ''] };
  const W = wikiPath(index, { Q9: 'Not Bundled' });
  assert.strictEqual(W._wikiArticlePath('Q803', 'en:Utrecht'), 'Utrecht');
  assert.strictEqual(W._wikiArticlePath(null, 'EN:Utrecht'), 'Utrecht');
  assert.strictEqual(W._wikiArticlePath(null, 'nl:Aalten (dorp)'), 'Aalten_(dorp)');
  // A Dutch title English Wikipedia does not have: no page, no button.
  assert.strictEqual(W._wikiArticlePath('Q7', 'nl:Utrecht (stad)'), null);
  assert.strictEqual(W._wikiArticlePath('Q803', null), 'Utrecht');     // by Q-ID
  assert.strictEqual(W._wikiArticlePath('Q9', null), null);            // bridge, not bundled
  assert.strictEqual(W._wikiArticlePath(null, 'constructor'), null);
  // No index (not loaded yet, or a ZIM without articles): no button.
  assert.strictEqual(wikiPath(null)._wikiArticlePath('Q803', 'en:Utrecht'), null);
});

await ok('the tag-to-title rule is the build\'s (cloud/wiki_articles._strip_lang)', () => {
  const W = wikiPath({});
  const tags = ['en:Golden Gate Bridge', 'NL:Utrecht (stad)', 'Foo: a bar', 'Mission: Impossible',
                'nds-nl:Foo', 'a:b', 'Ål:Bø', '12:x', 'Q1:x', 'Expo Park/USC station'];
  const py = process.env.PYTHON || (fs.existsSync(`${REPO}/venv-linux/bin/python3`)
    ? `${REPO}/venv-linux/bin/python3` : 'python3');
  let want;
  try {
    want = JSON.parse(execFileSync(py, ['-c',
      'import json,sys; from cloud.wiki_articles import _underscore; ' +
      'print(json.dumps([_underscore(t) for t in json.loads(sys.argv[1])]))',
      JSON.stringify(tags)], { cwd: REPO, encoding: 'utf8' }));
  } catch (e) {
    console.log('      (no python with the repo importable; checking the known answers)');
    want = ['Golden_Gate_Bridge', 'Utrecht_(stad)', '_a_bar', 'Mission:_Impossible',
            'nds-nl:Foo', 'a:b', 'Bø', '12:x', 'Q1:x', 'Expo_Park/USC_station'];
  }
  assert.deepStrictEqual(tags.map(W._wikiTagTitle), want);
});

console.log(`\n${pass} passed`);
