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

function make() {
  const el = () => ({ textContent: '', value: '', style: {}, addEventListener() {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    getBoundingClientRect() { return { height: 10 }; } });
  const env = { snaps: [], routes: [] };
  const body = `
  var window = {}; var graph = {}; var loadGraphInflight = false; function loadGraph() {}
  var statusEl = el(), originInput = el(), destInput = el(), resultEl = el(), clearBtn = el(), goRow = el(),
      panel = el(), distEl = el(), timeEl = el(), originResultsEl = el(), destResultsEl = el();
  var minBtn = null; var travelMode = 'drive'; var navPlanSeq = 0; var routingModes = ['drive', 'walk', 'bike'];
  var originNode = -1, destNode = -1, originCoordE7 = null, destCoordE7 = null, originMarker = null,
      destMarker = null, lastRoute = null, routeDrawn = false;
  var travelBtns = {}; var driveMode = { active: false, exit() {}, setRoute() {} };
  var __routeDebugLabel, __routeDebugPops;
  function syncTravelButtons() {} function resetGoButtons() {} function setExpandHint() {}
  function stopRouteProgressIndicator() {} function startRouteProgressIndicator() {} function cancelInFlightRoute() {}
  function coordLabel(a, b) { return a + ',' + b; } function makeMarkerEl() { return {}; }
  function haversine() { return 1000; } function unwrapLngs(c) { return c; } function drawRoute() {}
  function formatDistance() { return ''; } function formatTime() { return ''; } function renderRoads() {}
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
  assert.deepStrictEqual([a.state.destNode, a.state.dMarker], [20, [2, 2]]);
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
console.log(`${pass} passed`);
