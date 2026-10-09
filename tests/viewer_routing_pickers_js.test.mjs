// The routing panel's pickers with a moved end (an end the router moved
// off a road cut off from the rest: endMoved / startMoved). Runs the
// pickers' source (550-routing-pickers.js up to the typeahead, plus
// clearRoute from 500) with nearestNode / findRoute stubbed as promises
// the test resolves in its own order.
//
//   node tests/viewer_routing_pickers_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
// The UI-string runtime (szT, szFixed, …) as globals, as index.html has it:
// the code under test calls it (docs/i18n.md). Indirect eval: global scope.
(0, eval)(fs.readFileSync(`${REPO}/resources/viewer/i18n/runtime.js`, 'utf8'));
const PICK_SRC = fs.readFileSync(`${REPO}/resources/viewer/src/index/550-routing-pickers.js`, 'utf8');
const PICK = PICK_SRC.slice(0, PICK_SRC.indexOf('  // Typeahead: wire each input'));
const PANEL = fs.readFileSync(`${REPO}/resources/viewer/src/index/500-routing-panel.js`, 'utf8');
const CLEAR = (() => {
  const at = PANEL.indexOf('  function clearRoute()');
  let depth = 0;
  for (let i = PANEL.indexOf('{', at); i < PANEL.length; i++) {
    if (PANEL[i] === '{') depth++;
    else if (PANEL[i] === '}' && --depth === 0) return PANEL.slice(at, i + 1);
  }
  throw new Error('clearRoute not found');
})();
// SZ_HERE / szIsHere / szHereLabel: top level of 500, before initRouting.
const HERE = PANEL.slice(PANEL.indexOf('// BEGIN sz-here'), PANEL.indexOf('// END sz-here'));
assert(PICK.includes('function computeAndDrawRoute') && CLEAR.includes('destPick = null'));
assert(HERE.includes('function szIsHere'));

// Great-circle metres, as the viewer's haversine (520-routing-graph-load-and-snap.js).
function hav(lat1, lon1, lat2, lon2) {
  const R = 6371000, r = Math.PI / 180;
  const a = Math.sin((lat2 - lat1) * r / 2) ** 2
          + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin((lon2 - lon1) * r / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}
const km = (m) => (m / 1000).toFixed(1) + ' km';

function make(opts = {}) {
  const el = () => ({ textContent: '', value: '', style: {}, addEventListener() {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    getBoundingClientRect() { return { height: 10 }; } });
  const env = { snaps: [], routes: [], hav };
  const body = `
  var window = { innerHeight: ${opts.vh || 0} }; var graph = ${opts.noGraph ? 'null' : '{}'}; var loadGraphInflight = false; function loadGraph() {}
  var statusEl = el(), originInput = el(), destInput = el(), resultEl = el(), clearBtn = el(), goRow = el(),
      panel = el(), distEl = el(), timeEl = el(), originResultsEl = el(), destResultsEl = el();
  var panelClasses = new Set();
  panel.classList = { add(c) { panelClasses.add(c); }, remove(c) { panelClasses.delete(c); },
                      contains(c) { return panelClasses.has(c); } };
  panel.getBoundingClientRect = function() { return { height: ${opts.panelH || 10} }; };
  var minBtn = null; var travelMode = 'drive'; var navPlanSeq = 0; var routingModes = ['drive', 'walk', 'bike'];
  var originNode = -1, destNode = -1, originCoordE7 = null, destCoordE7 = null, originMarker = null,
      destMarker = null, lastRoute = null, routeDrawn = false;
  var travelBtns = {}; var driveMode = { active: false, exit() {}, setRoute() {} };
  var __routeDebugLabel, __routeDebugPops;
  function setRoutingStatus(t, st) { statusEl.textContent = t; statusEl.state = st || ''; }
  function syncTravelButtons() {} function resetGoButtons() {} function setExpandHint() {}
  function stopRouteProgressIndicator() {} function startRouteProgressIndicator() {} function cancelInFlightRoute() {}
  function coordLabel(a, b) { return a + ',' + b; } function makeMarkerEl() { return {}; }
  function haversine(a, b, c, d) { return ${opts.realDistance ? 'env.hav(a, b, c, d)' : '1000'}; }
  function unwrapLngs(c) { return c; } function drawRoute() {}
  function formatDistance(m) { return ${opts.realDistance ? "(m / 1000).toFixed(1) + ' km'" : "''"}; }
  function formatTime() { return ''; } function renderRoads() {}
  function augmentRouteForDriving(r) { return Object.assign({}, r); }
  var maplibregl = { Marker: function() { var m = { ll: null, setLngLat(x) { m.ll = x; return m; },
                       addTo() { return m; }, remove() {} }; return m; },
                     LngLatBounds: function() { return { extend() { return this; } }; } };
  var map = { getMaxBounds() { return null; }, fitBounds() {}, getLayer() { return false; },
              getSource() { return false; }, removeLayer() {}, removeSource() {}, resize() {} };
  function nearestNode(lat, lon, which) { return new Promise(function(res, rej) {
    env.snaps.push({ which: which, lat: lat, lon: lon, res: res, rej: rej }); }); }
  function findRoute(o, d, travel, picks) { return new Promise(function(res, rej) {
    env.routes.push({ o: o, d: d, travel: travel, picks: picks, res: res, rej: rej }); }); }
  ${HERE}
  ${PICK}
  ${CLEAR}
  env.SZ_HERE = SZ_HERE;
  env.api = { setOriginFromLatLon, setDestFromLatLon, setTravelMode, clearRoute, win: window, originInput,
    get state() { return { originNode, destNode, originMoved, destMoved, status: statusEl.textContent,
      statusState: statusEl.state, minimized: panelClasses.has('minimized'),
      oIn: originInput.value, dIn: destInput.value, oMarker: originMarker && originMarker.ll,
      dMarker: destMarker && destMarker.ll }; } };`;
  new Function('env', 'el', body)(env, el);
  return env;
}
const SZ_HERE_OF = (e) => e.SZ_HERE;
const settle = async () => { for (let k = 0; k < 5; k++) await new Promise(r => setTimeout(r, 15)); };
async function snap(e, which, node, lat, lon) {
  const s = e.snaps.shift();
  assert.strictEqual(s && s.which, which);
  s.res({ node, lat: lat ?? s.lat, lon: lon ?? s.lon });
  await settle();
}
async function route(e, extra) {
  const r = e.routes.shift();
  r.res(extra === null ? null : Object.assign({ coords: [[0, 0], [1, 1]], distance: 1, time: 1, roads: [] }, extra));
  await settle();
  return r;
}
const MOVED_D = 'Destination moved to the nearest reachable road';
const MOVED_O = 'Start moved to the nearest reachable road';
// Origin 10 at (1,1), destination 20 at (2,2), the route moves the destination to 21 at (2.1,2.1).
async function movedDest(originLabel) {
  const e = make(); const a = e.api;
  a.setOriginFromLatLon(1, 1, originLabel || 'O'); await snap(e, 'origin', 10);
  a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
  await route(e, { endMoved: { node: 21, lat: 2.1, lon: 2.1 } });
  assert.strictEqual(a.state.status, MOVED_D);
  assert.deepStrictEqual([a.state.destNode, a.state.dMarker], [21, [2.1, 2.1]]);
  return e;
}

let pass = 0;
async function ok(name, fn) {
  try { await fn(); console.log('ok   ', name); pass++; }
  catch (err) { console.error('FAIL ', name, '\n      ', err.stack || err.message); process.exitCode = 1; }
}
console.log = ((log) => (...a) => { if (!String(a[0]).startsWith('[streetzim]')) log(...a); })(console.log);

await ok('a new start routes to the destination\'s own snap, with the picks', async () => {
  const e = await movedDest(); const a = e.api;
  a.setOriginFromLatLon(3, 3, 'O2'); await snap(e, 'origin', 30);
  assert.strictEqual(e.snaps.length, 0);            // no second snap of the destination
  assert.strictEqual(e.routes.length, 1);
  const r = e.routes[0];
  assert.deepStrictEqual([r.o, r.d], [30, 20]);
  assert.deepStrictEqual(r.picks.dest, { lat: 2, lon: 2 });
  assert.deepStrictEqual(a.state.dMarker, [2.1, 2.1]);   // no jump before the route is known
  await route(e, { endMoved: { node: 21, lat: 2.1, lon: 2.1 } });
  assert.strictEqual(a.state.status, MOVED_D);
  assert.deepStrictEqual(a.state.dMarker, [2.1, 2.1]);
});
await ok('a new start that reaches the destination\'s snap puts its marker back', async () => {
  const e = await movedDest(); const a = e.api;
  a.setOriginFromLatLon(3, 3, 'O2'); await snap(e, 'origin', 30);
  await route(e, {});
  assert.strictEqual(a.state.status, '');
  assert.deepStrictEqual([a.state.destNode, a.state.destMoved, a.state.dMarker], [20, false, [2, 2]]);
});
await ok('no route after a new start: marker back on the destination\'s snap', async () => {
  const e = await movedDest(); const a = e.api;
  a.setOriginFromLatLon(3, 3, 'O2'); await snap(e, 'origin', 30);
  await route(e, null);
  assert.strictEqual(a.state.status, 'No route found');
  assert.strictEqual(a.state.statusState, 'no-route');   // what the gates read
  assert.deepStrictEqual([a.state.destNode, a.state.dMarker], [20, [2, 2]]);
});
await ok('a moved GPS start while the start field is being edited: a new destination still routes', async () => {
  const e = make(); const a = e.api;
  a.setOriginFromLatLon(1, 1, 'Current location'); await snap(e, 'origin', 10);
  a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
  await route(e, { startMoved: { node: 11, lat: 1.1, lon: 1.1 } });
  assert.strictEqual(a.state.status, MOVED_O);
  a.win.__streetzim_originBeingEdited = true;
  a.originInput.value = 'San D';                      // half typed
  a.setDestFromLatLon(4, 4, 'D2'); await snap(e, 'dest', 40);
  assert.strictEqual(e.routes.length, 1);
  assert.deepStrictEqual([e.routes[0].o, e.routes[0].d], [10, 40]);
  assert.strictEqual(a.state.oIn, 'San D');           // the typing is left alone
  assert.strictEqual(a.win.__streetzim_originBeingEdited, true);
});
await ok('both ends moved: the message says so, a travel-mode change clears it', async () => {
  const e = make(); const a = e.api;
  a.setOriginFromLatLon(1, 1, 'O'); await snap(e, 'origin', 10);
  a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
  await route(e, { startMoved: { node: 11, lat: 1.1, lon: 1.1 }, endMoved: { node: 21, lat: 2.1, lon: 2.1 } });
  assert.strictEqual(a.state.status, 'Start and destination moved to the nearest reachable roads');
  a.setTravelMode('walk');
  const [so, sd] = e.snaps.splice(0);
  sd.res({ node: 200, lat: 2, lon: 2 }); await settle();
  so.res({ node: 100, lat: 1, lon: 1 }); await settle();
  assert.strictEqual(e.routes.length, 1);
  assert.deepStrictEqual([e.routes[0].o, e.routes[0].d], [100, 200]);
  await route(e, {});
  assert.deepStrictEqual([a.state.status, a.state.originMoved, a.state.destMoved], ['', false, false]);
});
await ok('the moved note stays on later routes while the marker is moved', async () => {
  const e = await movedDest(); const a = e.api;
  a.setOriginFromLatLon(3, 3, 'O2'); await snap(e, 'origin', 30);
  await route(e, { endMoved: { node: 21, lat: 2.1, lon: 2.1 } });
  assert.strictEqual(a.state.status, MOVED_D);
});
await ok('a new destination pick is not "moved" unless its route moves it', async () => {
  const e = await movedDest(); const a = e.api;
  a.setDestFromLatLon(5, 5, 'D3'); await snap(e, 'dest', 50);
  assert.deepStrictEqual([e.routes[0].o, e.routes[0].d], [10, 50]);
  await route(e, {});
  assert.deepStrictEqual([a.state.status, a.state.destMoved, a.state.dMarker], ['', false, [5, 5]]);
});
await ok('a moved start goes back to its snap when a new destination is picked', async () => {
  const e = make(); const a = e.api;
  a.setOriginFromLatLon(1, 1, 'O'); await snap(e, 'origin', 10);
  a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
  await route(e, { startMoved: { node: 11, lat: 1.1, lon: 1.1 } });
  assert.strictEqual(a.state.status, MOVED_O);
  a.setDestFromLatLon(4, 4, 'D2'); await snap(e, 'dest', 40);
  assert.deepStrictEqual([e.routes[0].o, e.routes[0].d], [10, 40]);
  await route(e, {});
  assert.deepStrictEqual([a.state.status, a.state.originMoved, a.state.oMarker], ['', false, [1, 1]]);
});
await ok('a failed route after a new start: markers where the ends now are', async () => {
  const e = await movedDest(); const a = e.api;
  a.setOriginFromLatLon(3, 3, 'O2'); await snap(e, 'origin', 30);
  e.routes.shift().rej(new Error('boom')); await settle();
  assert.strictEqual(a.state.status, 'Routing failed');
  assert.strictEqual(a.state.statusState, 'failed');
  assert.deepStrictEqual([a.state.destNode, a.state.dMarker], [20, [2, 2]]);
});
await ok('data-state: done after a clean route, reset by clear (no stale failure)', async () => {
  const e = make(); const a = e.api;
  a.setOriginFromLatLon(1, 1, 'O'); await snap(e, 'origin', 10);
  a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
  await route(e, null);
  assert.strictEqual(a.state.statusState, 'no-route');
  a.clearRoute();
  assert.deepStrictEqual([a.state.status, a.state.statusState], ['', '']);
  a.setOriginFromLatLon(1, 1, 'O'); await snap(e, 'origin', 10);
  a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
  await route(e, {});
  assert.deepStrictEqual([a.state.status, a.state.statusState], ['', 'done']);
});
await ok('clear forgets moved ends', async () => {
  const e = await movedDest(); const a = e.api;
  a.clearRoute();
  assert.deepStrictEqual([a.state.originMoved, a.state.destMoved, a.state.destNode], [false, false, -1]);
});
await ok('a GPS start (SZ_HERE) shows the words and stays the GPS fix across a travel-mode change', async () => {
  const e = make(); const a = e.api;
  a.setOriginFromLatLon(1, 1, SZ_HERE_OF(e)); await snap(e, 'origin', 10);
  assert.strictEqual(a.state.oIn, 'Current location');
  assert.strictEqual(a.originInput._szHere, true);
  a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
  await route(e, {});
  a.setTravelMode('walk');
  await snap(e, 'origin', 10);
  assert.strictEqual(a.state.oIn, 'Current location');
  assert.strictEqual(a.originInput._szHere, true);   // still the device position
});
await ok('the GPS state is a flag, not the text: a place named like it is a place', async () => {
  const e = make(); const a = e.api;
  a.setOriginFromLatLon(1, 1, 'Main St'); await snap(e, 'origin', 10);
  assert.strictEqual(a.originInput._szHere, false);
  a.win.__streetzim_originBeingEdited = true;
  a.setOriginFromLatLon(5, 5, SZ_HERE_OF(e));          // a GPS auto-fill while typing: skipped
  assert.strictEqual(e.snaps.length, 0);
  a.win.__streetzim_originBeingEdited = false;
  a.setOriginFromLatLon(5, 5, 'Current location');     // the legacy English token still means GPS
  await snap(e, 'origin', 50);
  assert.strictEqual(a.originInput._szHere, true);
  a.clearRoute();
  assert.strictEqual(a.originInput._szHere, false);
});
await ok('in German the GPS start reads "Aktueller Standort" and is still the GPS fix after a mode change', async () => {
  globalThis.SZ_I18N = { 'routing.current_location': 'Aktueller Standort' };
  try {
    const e = make(); const a = e.api;
    a.setOriginFromLatLon(1, 1, SZ_HERE_OF(e)); await snap(e, 'origin', 10);
    assert.strictEqual(a.state.oIn, 'Aktueller Standort');
    a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
    await route(e, {});
    a.setTravelMode('walk');
    await snap(e, 'origin', 10);
    // Re-planned from the flag: the German words are not the English token.
    assert.strictEqual(a.originInput._szHere, true);
    assert.strictEqual(a.state.oIn, 'Aktueller Standort');
  } finally {
    globalThis.SZ_I18N = null;
  }
});
await ok('a GPS start picked while the graph loads is flagged too (queued pick)', async () => {
  const e = make({ noGraph: true }); const a = e.api;
  a.setOriginFromLatLon(1, 1, SZ_HERE_OF(e));
  assert.strictEqual(a.state.oIn, 'Current location');
  assert.strictEqual(a.originInput._szHere, true);
  await new Promise((r) => setTimeout(r, 150));      // the retry gives up (no load in flight)
});
await ok('the failure states are translated (pseudo-locale brackets them)', async () => {
  globalThis.SZ_I18N_PSEUDO = true;
  try {
    let e = make(); let a = e.api;
    a.setOriginFromLatLon(1, 1, 'O'); await snap(e, 'origin', 10);
    a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
    await route(e, null);
    assert.deepStrictEqual([a.state.status, a.state.statusState], ['[No route found]', 'no-route']);
    e = make(); a = e.api;
    a.setOriginFromLatLon(1, 1, 'O'); await snap(e, 'origin', 10);
    a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
    e.routes.shift().rej(new Error('boom')); await settle();
    assert.deepStrictEqual([a.state.status, a.state.statusState], ['[Routing failed]', 'failed']);
    e = make(); a = e.api;
    a.setTravelMode('walk');
    a.setOriginFromLatLon(1, 1, 'O'); await snap(e, 'origin', 10);
    a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20);
    await route(e, null);
    assert.strictEqual(a.state.status, '[No walking route found]');
  } finally {
    globalThis.SZ_I18N_PSEUDO = false;
  }
});
// Far ends (routeEndsNote): an end 2 km or more from the point picked says how far.
async function farRoute(oSnap, dSnap, extra) {
  const e = make({ realDistance: true }); const a = e.api;
  a.setOriginFromLatLon(1, 1, 'O'); await snap(e, 'origin', 10, ...oSnap);
  a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20, ...dSnap);
  await route(e, extra || {});
  return a.state;
}
await ok('a destination far from every road: the note gives the distance', async () => {
  const st = await farRoute([1, 1], [2, 2.1]);
  assert.strictEqual(st.status, `The route ends ${km(hav(2, 2, 2, 2.1))} from the destination`);
  assert.strictEqual(st.statusState, 'done');
});
await ok('a start far from its pick: the note says the start', async () => {
  const st = await farRoute([1.05, 1], [2, 2]);
  assert.strictEqual(st.status, `The route starts ${km(hav(1, 1, 1.05, 1))} from the start point`);
});
await ok('both ends far: one note with both distances, in order', async () => {
  const st = await farRoute([1.05, 1], [2, 2.1]);
  assert.strictEqual(st.status, `The route starts ${km(hav(1, 1, 1.05, 1))} from the start point`
    + ` and ends ${km(hav(2, 2, 2, 2.1))} from the destination`);
});
await ok('ends under 2 km from their picks: no note', async () => {
  const st = await farRoute([1.012, 1], [2, 2.017]);        // about 1.3 and 1.9 km
  assert.ok(hav(2, 2, 2, 2.017) > 1800 && hav(2, 2, 2, 2.017) < 2000);
  assert.strictEqual(st.status, '');
});
await ok('a destination the router moved far: the note gives the final distance', async () => {
  const st = await farRoute([1, 1], [2, 2.001], { endMoved: { node: 21, lat: 2.1, lon: 2.1 } });
  assert.strictEqual(st.status, `The route ends ${km(hav(2, 2, 2.1, 2.1))} from the destination`);
  assert.strictEqual(st.destMoved, true);
});
await ok('a destination the router moved a little: the moved note, not a distance', async () => {
  const st = await farRoute([1, 1], [2, 2], { endMoved: { node: 21, lat: 2.005, lon: 2 } });
  assert.strictEqual(st.status, MOVED_D);
});
await ok('a destination 3 km from its pick gets the note', async () => {
  const st = await farRoute([1, 1], [2, 2.027]);          // about 3.0 km
  assert.ok(hav(2, 2, 2, 2.027) > 2900 && hav(2, 2, 2, 2.027) < 3100);
  assert.strictEqual(st.status, `The route ends ${km(hav(2, 2, 2, 2.027))} from the destination`);
});
await ok('a far-end note keeps the panel unfolded (folded, the status line is hidden)', async () => {
  for (const [dSnap, folded] of [[[2, 2.1], false], [[2, 2.001], true]]) {
    const e = make({ realDistance: true, vh: 100, panelH: 80 }); const a = e.api;
    a.setOriginFromLatLon(1, 1, 'O'); await snap(e, 'origin', 10, 1, 1);
    a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20, ...dSnap);
    await route(e, {});
    assert.strictEqual(a.state.minimized, folded, JSON.stringify(dSnap));
  }
});
await ok('a route that lands while a newer pick snaps is described by its own picks', async () => {
  const e = make({ realDistance: true }); const a = e.api;
  a.setOriginFromLatLon(1, 1, 'O'); await snap(e, 'origin', 10, 1, 1);
  a.setDestFromLatLon(2, 2, 'D'); await snap(e, 'dest', 20, 2, 2);
  a.setDestFromLatLon(3, 3, 'D2');                          // its snap is still pending
  await route(e, {});                                       // the route to D lands first
  assert.strictEqual(a.state.status, '');
});
console.log(`${pass} passed`);
