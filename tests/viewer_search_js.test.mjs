// Administrative areas in the viewers' search (docs/search-records.md,
// t: 'admin'): the in-map search's scoring and camera (the admin-search
// block of resources/viewer/index.html, run as it ships), the Find page's
// name match and "Map" link (places.html), and the `bounds=` deep link.
//
//   node tests/viewer_search_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

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
const PLACES = fs.readFileSync(`${REPO}/resources/viewer/places.html`, 'utf8');
const REPO_VENV = `${REPO}/venv-linux/bin/python3`;
const PY = process.env.PYTHON || (fs.existsSync(REPO_VENV) ? REPO_VENV : 'python3');

let pass = 0;
function ok(name, fn) {
  try { fn(); console.log('ok   ', name); pass++; }
  catch (e) { console.error('FAIL ', name, '\n      ', e.stack || e.message); process.exitCode = 1; }
}

function slice(src, from, to) {
  const a = src.indexOf(from), b = src.indexOf(to, a);
  assert.ok(a >= 0 && b > a, `${from} .. ${to} not found`);
  return src.slice(a, b);
}
const V = new Function(slice(HTML, '// BEGIN admin-search', '// END admin-search') +
  '\nreturn { _szSearchForms, _szFormsContain, _szTextScore, _szAdminCamera, _szAdminKeys, _szFitAdminBox, SZ_ADMIN_ZOOM,' +
  ' _szRecordNames, _szLnNames, _szShownNames, _szVisitorLangs };')();
// The viewer's normalizeText (300-search.js, initSearch): SEARCH_SHARDS.fold.
const norm = new Function(slice(HTML, '// BEGIN search-shards', '// END search-shards') +
  '\nreturn SEARCH_SHARDS.fold;')();

function score(item, query) {
  const q = norm(query);
  return V._szTextScore(item, q, q.split(/\s+/).filter((w) => w), norm);
}
function rank(items, query) {
  return items.map((it) => [score(it, query), it])
    .filter(([s]) => s >= 0).sort((a, b) => b[0] - a[0]).map(([, it]) => it.n + '/' + it.t);
}

const ARLINGTON = { n: 'Arlington County', t: 'admin', s: 'county', al: 6, a: 38.88, o: -77.1 };
const ALEXANDRIA = { n: 'Alexandria', t: 'admin', s: 'city', al: 6, a: 38.80, o: -77.05 };
const DC = { n: 'District of Columbia', t: 'admin', s: 'district', al: 4,
             alt: ['D.C.', 'The District'], bb: [-77.11979, 38.79163, -76.90937, 38.99597] };
const SHOP = { n: 'Arlington', t: 'poi', s: 'shop' };
const TOWN = { n: 'Arlington', t: 'place', s: 'town' };
const ALEX_SHOP = { n: 'Alexandria', t: 'poi', s: 'clothes' };
const TOWN_HALL = { n: 'City of Alexandria Town Hall', t: 'poi', s: 'townhall' };

ok('the county ranks above a shop and a town of the same name', () => {
  assert.deepStrictEqual(rank([SHOP, TOWN, ARLINGTON], 'Arlington'),
    ['Arlington County/admin', 'Arlington/place', 'Arlington/poi']);
  assert.deepStrictEqual(rank([SHOP, ARLINGTON], 'Arlington County'), ['Arlington County/admin']);
});

ok('"City of Alexandria" finds the city by its type, above a town hall', () => {
  assert.deepStrictEqual(rank([ALEX_SHOP, TOWN_HALL, ALEXANDRIA], 'City of Alexandria'),
    ['Alexandria/admin', 'City of Alexandria Town Hall/poi']);
  assert.deepStrictEqual(rank([ALEX_SHOP, ALEXANDRIA], 'Alexandria city'), ['Alexandria/admin']);
  // Only admin areas skip connector words.
  assert.strictEqual(score(ALEX_SHOP, 'City of Alexandria'), -1);
  assert.strictEqual(score(ALEXANDRIA, 'Town of Alexandria'), -1);
});

ok('other names match', () => {
  assert.ok(score(DC, 'D.C.') > 0);
  assert.ok(score(DC, 'the district') > score(DC, 'district'));   // exact alt name
  assert.strictEqual(score(DC, 'Maryland'), -1);
});

ok('other types score as before the admin block', () => {
  // The pre-admin formula: +11 per word at a word start, +50 exact,
  // +25 prefix, type bonus, place subtype bonus.
  assert.strictEqual(score(TOWN, 'Arlington'), 11 + 50 + 25 + 20 + 30);
  assert.strictEqual(score(SHOP, 'arl'), 11 + 25 + 5);
  assert.strictEqual(score({ n: 'Rue du Lac', t: 'street' }, 'lac'), 11);
  assert.strictEqual(score({ n: 'Rue du Lac', t: 'street' }, 'du rue'), 22);
  assert.strictEqual(score({ n: 'Rue du Lac', t: 'street' }, 'of lac'), -1);
  assert.deepStrictEqual(Array.from(V._szSearchForms(SHOP, norm)), ['arlington']);
});

ok('the stream filter lets admin forms through', () => {
  assert.ok(V._szFormsContain(ALEXANDRIA, 'city of alexandria', norm));
  assert.ok(V._szFormsContain(DC, 'd.c.', norm));
  assert.ok(!V._szFormsContain(ALEX_SHOP, 'city of alexandria', norm));
  assert.ok(V._szFormsContain(ALEX_SHOP, 'alex', norm));
  assert.ok(V._szFormsContain(ALEX_SHOP, '', norm));
});

ok('the stream filter keeps what the scorer would rank, and no more', () => {
  const qs = ['county arlington', 'alexandria of city', 'city alexandria', 'the district',
              'of', 'of the', 'arlington', 'arl', 'town alexandria', 'washington'];
  for (const it of [ARLINGTON, ALEXANDRIA, DC]) {
    for (const q of qs) {
      assert.strictEqual(V._szFormsContain(it, norm(q), norm), score(it, q) >= 0, `${it.n} / ${q}`);
    }
  }
  assert.strictEqual(score(ALEXANDRIA, 'of'), -1);            // connectors alone
  assert.ok(!V._szFormsContain(ALEXANDRIA, 'of the', norm));
  assert.ok(score(ARLINGTON, 'county arlington') > 0);        // any word order
});

ok('a pick fits the box, else zooms by level', () => {
  assert.deepStrictEqual(V._szAdminCamera(DC),
    { bounds: [[-77.11979, 38.79163], [-76.90937, 38.99597]] });
  assert.deepStrictEqual(V._szAdminCamera(ALEXANDRIA), { zoom: 10 });
  assert.deepStrictEqual(V._szAdminCamera({ al: 2 }), { zoom: 5 });
  assert.deepStrictEqual(V._szAdminCamera({ al: 99 }), { zoom: 12 });
  for (const bb of [[1, 2, 0, 3], [0, -95, 1, 1], [0, 0, 1, 'x'], [0, 0, 1]]) {
    assert.deepStrictEqual(V._szAdminCamera({ al: 8, bb }), { zoom: 12 }, JSON.stringify(bb));
  }
});

ok('selectResult and applyHash use the box', () => {
  const search = fs.readFileSync(`${REPO}/resources/viewer/src/index/300-search.js`, 'utf8');
  assert.match(search, /adminCam = \(item && item\.t === 'admin'\) \? _szAdminCamera\(item\)/);
  assert.match(search, /_szFitAdminBox\(map, adminCam\.bounds, _topChrome, SZ_ADMIN_ZOOM\[item\.al\] \|\| 12\)/);
  assert.match(search, /_szFormsContain\(rec, qNorm, normalizeText\)/);
  assert.match(search, /var textScore = _szTextScore\(item, q, words, normalizeText\);/);
  const init = fs.readFileSync(`${REPO}/resources/viewer/src/index/120-map-init-and-style.js`, 'utf8');
  assert.match(init, /hash\.match\(\/bounds=/);
  assert.match(init, /if \(!fitted\) map\.flyTo/);
});

// ---- an area pick's camera on a short canvas -----------------------------
// MapLibre 5's rule (cameraForBounds): no camera when the padding leaves the
// box no room; fitBounds then does not move.
function fakeMap(w, h) {
  const m = { moved: null };
  m.getCanvas = () => ({ clientHeight: h, clientWidth: w });
  m.cameraForBounds = (b, o) => {
    const p = o.padding;
    if (p.top + p.bottom >= h || p.left + p.right >= w) return undefined;
    return { center: [0, 0], zoom: 10 };
  };
  m.fitBounds = (b, o) => { if (m.cameraForBounds(b, o)) m.moved = { kind: 'fit', bounds: b, opts: o }; };
  m.flyTo = (o) => { m.moved = { kind: 'fly', opts: o }; };
  return m;
}
const ATLANTA = { n: 'Atlanta', t: 'admin', s: 'city', al: 8, a: 33.75447, o: -84.38982,
                  bb: [-84.55085, 33.64792, -84.28956, 33.88682] };
const ATL_BOX = [[ATLANTA.bb[0], ATLANTA.bb[1]], [ATLANTA.bb[2], ATLANTA.bb[3]]];

ok('an area is framed on a canvas the keyboard shortened', () => {
  // 390x430 (iPhone, keyboard up) with the top chrome at 395 px: the old
  // padding (415 + 30) left no room, and the camera stayed put.
  const m = fakeMap(390, 430);
  V._szFitAdminBox(m, ATL_BOX, 395, 12);
  assert.strictEqual(m.moved && m.moved.kind, 'fit');
  const p = m.moved.opts.padding;
  assert.ok(p.top + p.bottom <= 430 - 60, JSON.stringify(p));
  assert.deepStrictEqual(m.moved.bounds, ATL_BOX);
});

ok('with room to spare the padding is the chrome plus 20, as before', () => {
  const m = fakeMap(390, 844);
  V._szFitAdminBox(m, ATL_BOX, 110, 12);
  assert.strictEqual(m.moved.kind, 'fit');
  assert.deepStrictEqual(m.moved.opts.padding, { top: 130, bottom: 30, left: 30, right: 30 });
  assert.strictEqual(m.moved.opts.maxZoom, 16);
});

ok('a box that cannot fit at all: fly to its centre at the zoom given', () => {
  const m = fakeMap(50, 430);     // left + right padding alone fill the width
  // A country's zoom (5), not a city's 12, so a hardcoded zoom shows.
  V._szFitAdminBox(m, ATL_BOX, 110, V.SZ_ADMIN_ZOOM[2]);
  assert.strictEqual(m.moved.kind, 'fly');
  assert.deepStrictEqual(m.moved.opts.center, [(ATL_BOX[0][0] + ATL_BOX[1][0]) / 2, (ATL_BOX[0][1] + ATL_BOX[1][1]) / 2]);
  assert.strictEqual(m.moved.opts.zoom, 5);
});

ok('the padding is capped by the canvas height, not its width', () => {
  // A wide, short canvas (landscape, keyboard up): capped by the 1200 px
  // width, the top padding would leave the box no room.
  const m = fakeMap(1200, 430);
  V._szFitAdminBox(m, ATL_BOX, 395, 12);
  assert.strictEqual(m.moved && m.moved.kind, 'fit');
  assert.ok(m.moved.opts.padding.top + m.moved.opts.padding.bottom <= 430 - 60,
            JSON.stringify(m.moved.opts.padding));
});

ok('selectResult measures the chrome with the results list closed, and the camera moves', () => {
  const SEARCH = fs.readFileSync(`${REPO}/resources/viewer/src/index/300-search.js`, 'utf8');
  const body = slice(SEARCH, '  function selectResult(el) {', '\n  // Input handler with debounce');
  const resultsEl = { style: { display: 'block' } };
  const input = { blurred: false, blur() { this.blurred = true; } };
  // The list lives inside #search-container: open, the box ends at its
  // bottom (415 px); closed, at the chips' (110 px).
  const doc = { getElementById: (id) => (id === 'search-container'
    ? { getBoundingClientRect: () => ({ bottom: resultsEl.style.display === 'none' ? 110 : 415 }) } : null) };
  const map = fakeMap(390, 430);
  const fitCalls = [];
  const pinned = [];
  const selectResult = new Function('lastSearchMatches', 'resultsEl', 'input', 'map', 'document',
    '_szAdminCamera', 'SZ_ADMIN_ZOOM', '_szFitAdminBox', '_szFlyToClear', 'placeSearchPin',
    '_szShownNames', 'SZ_LANG2',
    body + '\nreturn selectResult;')(
    [{ item: ATLANTA }], resultsEl, input, map, doc, V._szAdminCamera, V.SZ_ADMIN_ZOOM,
    (m, b, top, z) => { fitCalls.push(top); V._szFitAdminBox(m, b, top, z); },
    () => { throw new Error('an area must not take the point path'); },
    (m, lat, lon, name) => pinned.push(name), V._szShownNames, '');
  const attrs = { 'data-idx': '0', 'data-lat': String(ATLANTA.a), 'data-lon': String(ATLANTA.o),
                  'data-type': 'admin', 'data-name': 'Atlanta' };
  selectResult({ getAttribute: (k) => attrs[k] });
  assert.deepStrictEqual(fitCalls, [110]);
  assert.strictEqual(resultsEl.style.display, 'none');
  assert.ok(input.blurred);
  // The pin's popup and its Directions label take the name the row showed.
  assert.deepStrictEqual(pinned, ['Atlanta']);
  assert.strictEqual(map.moved && map.moved.kind, 'fit');
  assert.strictEqual(map.moved.opts.padding.top, 130);
});

// ---- places.html -------------------------------------------------------
const P = new Function(
  slice(PLACES, '// BEGIN search-shards', '// END search-shards') +
  slice(PLACES, 'function foldText', '\n}\n') + '\n}\n' +
  slice(PLACES, 'function matchesName', '// Chunk key for a word') +
  '\nreturn { matchesName, mapHashFor };')();

ok('Find page: admin areas match by other names and "<type> of <name>"', () => {
  assert.ok(P.matchesName(ALEXANDRIA, 'City of Alexandria'));
  assert.ok(P.matchesName(ALEXANDRIA, 'alexandria city'));
  assert.ok(P.matchesName(DC, 'D.C.'));
  assert.ok(!P.matchesName(ALEX_SHOP, 'City of Alexandria'));
  assert.ok(P.matchesName(ALEX_SHOP, 'clothes'));          // subtype, as before
  assert.ok(P.matchesName(ARLINGTON, 'county arlington'));
  assert.ok(!P.matchesName(ALEXANDRIA, 'of'));
  assert.ok(!P.matchesName(ALEXANDRIA, 'town of alexandria'));
});

ok('Find page: "Map" fits an admin area with a box', () => {
  const dc = Object.assign({ a: 38.8951, o: -77.03638 }, DC);
  assert.strictEqual(P.mapHashFor(dc, 'X/'),
    'X/index.html#map=10.8/38.89380/-77.01458&bounds=-77.11979,38.79163,-76.90937,38.99597'
    + '&pin=38.8951,-77.03638&label=District%20of%20Columbia');
  assert.strictEqual(P.mapHashFor(ALEXANDRIA, ''),
    'index.html#map=10/38.8/-77.05&pin=38.8,-77.05&label=Alexandria');
  assert.strictEqual(P.mapHashFor({ n: 'P', t: 'park', a: 1, o: 2 }, ''),
    'index.html#map=14/1/2&pin=1,2&label=P');
  assert.strictEqual(P.mapHashFor({ n: 'S', t: 'poi', a: 1, o: 2 }, ''),
    'index.html#map=16/1/2&pin=1,2&label=S');
});

ok('Find page and Kiwix pages agree on the fitted zoom', () => {
  const py = execFileSync(PY, ['-c',
    'import sys, json; sys.path.insert(0, sys.argv[1]);' +
    'from streetzim.zim_writer import kiwix_page_hash;' +
    'print(kiwix_page_hash(json.loads(sys.argv[2])))', REPO,
    JSON.stringify({ name: 'District of Columbia', type: 'admin', lat: 38.8951, lon: -77.03638,
                     admin_level: 4, bbox: DC.bb })], { encoding: 'utf8' }).trim();
  const js = P.mapHashFor(Object.assign({ a: 38.8951, o: -77.03638 }, DC), '').replace('index.html#', '');
  assert.strictEqual(js, py);
});


ok('an area wins over the place record at its point (same name and point)', () => {
  const place = { n: 'Monaco', t: 'place', s: 'country', a: 43.73235, o: 7.42768 };
  const area = { n: 'Monaco', t: 'admin', s: 'country', al: 2, a: 43.73235, o: 7.42768 };
  const keys = V._szAdminKeys([place, area]);
  assert.deepStrictEqual(Object.keys(keys), ['Monaco|43.73235|7.42768']);
  assert.deepStrictEqual(V._szAdminKeys([place]), {});
});

// Native-script names (`nn`, docs/search-records.md): matched like the name.
const PKU = { n: 'Peking University', nn: '北京大学', t: 'poi', s: 'university' };
const BEIJING = { n: 'Beijing', nn: '北京市', t: 'admin', s: 'city', al: 4 };

ok('a record answers to its native-script name', () => {
  assert.ok(score(PKU, '北京大学') >= 0 && score(PKU, '北京') >= 0);
  assert.ok(score(PKU, 'Peking') >= 0);
  assert.ok(score(PKU, '上海') < 0);
  assert.ok(score(PKU, '北京大学') > score(PKU, '北京'));     // exact beats prefix
  assert.ok(V._szFormsContain(PKU, norm('北京'), norm));
  assert.ok(!V._szFormsContain({ n: 'Peking University', t: 'poi' }, norm('北京'), norm));
  assert.ok(score(BEIJING, '北京市') >= 0 && V._szFormsContain(BEIJING, norm('北京'), norm));
  assert.ok(P.matchesName(PKU, '北京大学') && P.matchesName(BEIJING, '北京'));
  assert.ok(!P.matchesName({ n: 'Peking University', t: 'poi' }, '北京'));
});

// A build in another language: n is French, nl the English name.
const TOUR = { n: 'Tour Eiffel', nl: 'Eiffel Tower', t: 'poi', s: 'attraction' };
ok('a record answers to its English name in a build in another language', () => {
  assert.ok(score(TOUR, 'Eiffel Tower') >= 0 && score(TOUR, 'Tour Eiffel') >= 0);
  assert.ok(V._szFormsContain(TOUR, norm('tower'), norm));
  assert.ok(P.matchesName(TOUR, 'Eiffel Tower'));
});

// Map labels in a build's language (130-lowzoom-lakes.js, szLabelField /
// szLabelOf): name:<lang>, then the place's own name (OpenFreeMap: name;
// StreetZim tilemaker: name_int, then name:latin).
const LBL = new Function(slice(HTML, 'var SZ_LANG = ', 'function makeStyle') +
  '\nreturn { set: (l) => { SZ_LANG = l; }, field: szLabelField, local: _szLocalLabelField, first: _szFirstName,' +
  ' of: szLabelOf };')();
ok('labels follow the build language', () => {
  LBL.set('en');
  // The label is the local rule's name, cut at a ";" (130, _szFirstName).
  assert.deepStrictEqual(LBL.field(), LBL.first(LBL.local()));
  assert.deepStrictEqual(LBL.local()[1], ['get', 'name:latin']);
  assert.strictEqual(LBL.of({ 'name:latin': 'Tokyo', name_int: '東京', 'name:fr': 'Tokyo' }), 'Tokyo');
  LBL.set('fr');
  assert.deepStrictEqual(LBL.field(), LBL.first(LBL.local()));
  assert.deepStrictEqual(LBL.local().slice(1).map((g) => g[1]),
    ['name:fr', 'name', 'name_int', 'name:latin']);
  assert.strictEqual(LBL.of({ 'name:latin': 'Imperial Palace', name_int: '皇居', 'name:fr': 'Palais impérial' }), 'Palais impérial');
  assert.strictEqual(LBL.of({ 'name:latin': 'Peking University', name_int: '北京大学' }), '北京大学');
  assert.strictEqual(LBL.of({ name: '東京タワー', name_int: 'Tokyo Tower' }), '東京タワー');   // OpenFreeMap
  LBL.set('en');
});

// Search in the visitor's language: `ln` names (docs/search-records.md).
const GENEVA = { n: 'Geneva', nn: 'Genève', t: 'place', s: 'city',
                 ln: { de: 'Genf', fr: 'Genève', it: 'Ginevra', ru: 'Женева' } };
const GENF_SHOP = { n: 'Genfer Uhren', t: 'poi', s: 'watches' };
ok('a record answers to its names in other languages', () => {
  assert.ok(score(GENEVA, 'Genf') >= 0 && score(GENEVA, 'ginevra') >= 0 && score(GENEVA, 'женева') >= 0);
  assert.ok(V._szFormsContain(GENEVA, norm('Ginevra'), norm));
  assert.ok(!V._szFormsContain({ n: 'Geneva', t: 'place' }, norm('Ginevra'), norm));
  assert.ok(P.matchesName(GENEVA, 'Genf') && P.matchesName(GENEVA, 'Женева'));
  assert.ok(!P.matchesName({ n: 'Geneva', t: 'place' }, 'Genf'));
  // The city ranks above a shop whose name starts with the query.
  assert.deepStrictEqual(rank([GENF_SHOP, GENEVA], 'Genf'), ['Geneva/place', 'Genfer Uhren/poi']);
});
ok('an admin area answers to its names in other languages', () => {
  const CANTON = { n: 'Geneva', t: 'admin', s: 'canton', al: 4, ln: { de: 'Genf' } };
  assert.ok(score(CANTON, 'Genf') >= 0 && score(CANTON, 'Kanton Genf') < 0);
  assert.ok(score(CANTON, 'genf canton') >= 0);
  assert.ok(P.matchesName(CANTON, 'genf'));
});
ok('the names in other languages, each once, in order', () => {
  assert.deepStrictEqual(V._szLnNames(GENEVA), ['Genf', 'Genève', 'Ginevra', 'Женева']);
  assert.deepStrictEqual(V._szRecordNames(GENEVA), ['Geneva', 'Genève', 'Genf', 'Ginevra', 'Женева']);
  assert.deepStrictEqual(V._szLnNames({ n: 'x', ln: 'Genf' }), []);
  assert.deepStrictEqual(V._szLnNames({ n: 'x', ln: { de: 3, fr: '', it: 'a', rm: 'a' } }), ['a']);
  assert.deepStrictEqual(V._szLnNames({ n: 'x' }), []);
});
ok('a result shows the name in the map\'s second language', () => {
  assert.deepStrictEqual(V._szShownNames(GENEVA, 'de'), ['Genf', 'Genève']);
  assert.deepStrictEqual(V._szShownNames(GENEVA, 'fr'), ['Genève', 'Geneva']);
  assert.deepStrictEqual(V._szShownNames(GENEVA, 'ja'), ['Geneva', 'Genève']);
  assert.deepStrictEqual(V._szShownNames(GENEVA, ''), ['Geneva', 'Genève']);
  assert.deepStrictEqual(V._szShownNames({ n: 'Zurich', ln: { de: 'Zürich' } }, 'de'), ['Zürich', 'Zurich']);
  assert.deepStrictEqual(V._szShownNames({ n: 'Zurich' }, 'de'), ['Zurich', '']);
  // An inherited key is not a name.
  assert.deepStrictEqual(V._szShownNames({ n: 'X', ln: {} }, 'constructor'), ['X', '']);
  // "" stands for the record's own-script name (the writer does not repeat it).
  const ATHENS = { n: 'Athens', nn: 'Αθήνα', ln: { el: '', de: 'Athen' } };
  assert.deepStrictEqual(V._szShownNames(ATHENS, 'el'), ['Αθήνα', 'Athens']);
  assert.deepStrictEqual(V._szShownNames(ATHENS, 'de'), ['Athen', 'Αθήνα']);
  assert.deepStrictEqual(V._szShownNames({ n: 'X', ln: { el: '' } }, 'el'), ['X', '']);
  assert.deepStrictEqual(V._szLnNames(ATHENS), ['Athen']);
  assert.ok(score(Object.assign({ t: 'place' }, ATHENS), 'αθηνα') >= 0);
});

// A tapped label is looked up by its local name (szLabelOf: the record's
// n, nn or nl), never by a name in another language (window.
// __streetzimLookupPlace, 300-search.js).
{
  const a = HTML.indexOf('  window.__streetzimLookupPlace = function(name, lat, lon) {');
  const b = HTML.indexOf('\n  };\n', a);
  assert.ok(a > 0 && b > a);
  const make = new Function('manifest', 'getPrefixes', 'streamFilterChunks', 'normalizeText',
    '_haversineMetersStrip', 'window',
    HTML.slice(a, b + 5) + '\nreturn window.__streetzimLookupPlace;');
  const lookup = make({}, () => Object.assign(['ge'], { targeted: true }),
    () => Promise.resolve([Object.assign({ a: 46.2, o: 6.15 }, GENEVA)]), norm, () => 10, {});
  const byN = await lookup('Geneva', 46.2, 6.15);
  const byNn = await lookup('Genève', 46.2, 6.15);
  const byLn = await lookup('Ginevra', 46.2, 6.15);
  ok('a label is looked up by its local name, not by ln', () => {
    assert.strictEqual(byN && byN.n, 'Geneva');
    assert.strictEqual(byNn && byNn.n, 'Geneva');
    assert.strictEqual(byLn, null);
  });
}

// A merged Romansh map: the UI falls back to German (runtime.js
// SZ_UI_FALLBACK), while labels and search names stay Romansh (SZ_LANG2).
ok('Romansh: German UI, Romansh search names', () => {
  assert.strictEqual(SZ_UI_FALLBACK.rm, 'de');
  const CHUR = { n: 'Chur', t: 'place', s: 'town', ln: { rm: 'Cuira', it: 'Coira' } };
  assert.deepStrictEqual(V._szShownNames(CHUR, 'rm'), ['Cuira', 'Chur']);
  assert.ok(score(CHUR, 'Cuira') >= 0 && score(CHUR, 'coira') >= 0);
});

// Ranking: a name in another language than the visitor's (ln) finds its
// place but gets no exact-match bonus (_szTextScore's visitor languages).
// The reviewer's cases from Switzerland's records.
function scoreAs(item, query, visitor) {
  const q = norm(query);
  return V._szTextScore(item, q, q.split(/\s+/).filter((w) => w), norm, visitor);
}
const ILANZ = { n: 'Ilanz', t: 'place', s: 'town', ln: { rm: 'Glion', it: 'Ilanz' } };
const GLION = { n: 'Glion', t: 'place', s: 'village' };
const ALTSTADT = { n: 'Altstadt', t: 'place', s: 'suburb', ln: { fr: 'Vieille Ville' } };
const VIEILLE = { n: 'Vieille Ville', t: 'place', s: 'neighbourhood' };
const BERN = { n: 'Bern', t: 'place', s: 'city', ln: { fr: 'Berne', it: 'Berna' } };
const BERNE_LOC = { n: 'Berne', t: 'place', s: 'locality' };
const CHUR = { n: 'Chur', t: 'place', s: 'town', ln: { fr: 'Coire', rm: 'Cuira' } };
const COIRE_LOC = { n: 'Coire', t: 'place', s: 'locality' };
const MURTEN = { n: 'Murten', t: 'place', s: 'town', ln: { fr: 'Morat' } };
const MORAT_LOC = { n: 'Morat', t: 'place', s: 'hamlet' };
const top = (items, q, visitor) => items.map((it) => [scoreAs(it, q, visitor), it.n])
  .sort((x, y) => y[0] - x[0])[0][1];
ok('a name in a language the visitor does not read does not outrank a place of that name', () => {
  assert.strictEqual(top([ILANZ, GLION], 'Glion', ['en']), 'Glion');
  assert.strictEqual(top([ALTSTADT, VIEILLE], 'Vieille Ville', ['de']), 'Vieille Ville');
  assert.strictEqual(top([CHUR, COIRE_LOC], 'Coire', ['en']), 'Coire');
  // ... but a Romansh, French visitor gets the town; the big city wins anyway.
  assert.strictEqual(top([ILANZ, GLION], 'Glion', ['rm']), 'Ilanz');
  assert.strictEqual(top([ALTSTADT, VIEILLE], 'Vieille Ville', ['fr']), 'Altstadt');
  assert.strictEqual(top([CHUR, COIRE_LOC], 'Coire', ['fr', 'en']), 'Chur');
  assert.strictEqual(top([MURTEN, MORAT_LOC], 'Morat', ['fr']), 'Murten');
  assert.strictEqual(top([BERN, BERNE_LOC], 'Berne', ['en']), 'Bern');
  assert.strictEqual(top([GENEVA, GENF_SHOP], 'Genf', ['en']), 'Geneva');
  // The record's own names keep the bonus whatever the visitor reads.
  assert.ok(scoreAs(GLION, 'Glion', []) === scoreAs(GLION, 'Glion', ['rm']));
  assert.strictEqual(scoreAs(ILANZ, 'Ilanz', ['en']), scoreAs(ILANZ, 'Ilanz', ['it']));
});
ok('the visitor reads the map\'s second language, the UI\'s, the one asked for and the device\'s', () => {
  const VL = new Function('SZ_LANG2', 'SZ_UI_LANG', 'SZ_UI_ASKED', 'navigator',
    slice(HTML, 'function _szVisitorLangs()', '\n// The normalized texts') + '\nreturn _szVisitorLangs();');
  assert.deepStrictEqual(VL('rm', 'de', 'rm', undefined), ['rm', 'de']);
  assert.deepStrictEqual(VL('', 'en', 'en', { languages: ['fr-CH', 'de', 'fr'] }), ['en', 'fr', 'de']);
  assert.deepStrictEqual(VL('', 'en', 'en', { language: 'it-IT' }), ['en', 'it']);
  assert.deepStrictEqual(VL(undefined, undefined, undefined, undefined), []);
  // A French phone on an English map: "Coire" is Chur, "Morat" Murten.
  const fr = VL('', 'en', 'en', { languages: ['fr-FR'] });
  assert.strictEqual(top([CHUR, COIRE_LOC], 'Coire', fr), 'Chur');
  assert.strictEqual(top([MURTEN, MORAT_LOC], 'Morat', fr), 'Murten');
});
ok('forms carry the language of a name that only ln gives', () => {
  const f = V._szSearchForms({ n: 'Ilanz', t: 'place', ln: { rm: 'Glion', it: 'Ilanz', de: 'Glion' } }, norm);
  assert.deepStrictEqual(Array.from(f), ['ilanz', 'glion']);
  assert.deepStrictEqual(f.lang, [null, ['rm', 'de']]);
  const a = V._szSearchForms({ n: 'Geneva', t: 'admin', s: 'canton', ln: { de: 'Genf' } }, norm);
  assert.deepStrictEqual(Array.from(a), ['geneva', 'geneva canton', 'canton of geneva', 'genf', 'genf canton', 'canton of genf']);
  assert.deepStrictEqual(a.lang, [null, null, null, ['de'], ['de'], ['de']]);
  // Cached on the record, out of its JSON.
  const r = { n: 'X', t: 'poi', ln: { de: 'Y' } };
  assert.strictEqual(V._szSearchForms(r, norm), V._szSearchForms(r, norm));
  assert.strictEqual(JSON.stringify(r), '{"n":"X","t":"poi","ln":{"de":"Y"}}');
});

// A merged German map (SZ_LANG2 de): the pin and its Directions label say
// "Genf", as the result row did.
ok('a pick on a merged map pins the name the row showed', () => {
  const SEARCH = fs.readFileSync(`${REPO}/resources/viewer/src/index/300-search.js`, 'utf8');
  const body = slice(SEARCH, '  function selectResult(el) {', '\n  // Input handler with debounce');
  const pinned = [];
  const rec = Object.assign({ a: 46.2, o: 6.15 }, GENEVA);
  const doc = { getElementById: () => ({ getBoundingClientRect: () => ({ bottom: 110 }) }) };
  const sel = (lang) => new Function('lastSearchMatches', 'resultsEl', 'input', 'map', 'document',
    '_szAdminCamera', 'SZ_ADMIN_ZOOM', '_szFitAdminBox', '_szFlyToClear', 'placeSearchPin',
    '_szShownNames', 'SZ_LANG2', body + '\nreturn selectResult;')(
    [{ item: rec }], { style: {} }, { blur() {} }, fakeMap(390, 844), doc, V._szAdminCamera,
    V.SZ_ADMIN_ZOOM, () => {}, () => {}, (m, lat, lon, name) => pinned.push(name), V._szShownNames, lang);
  const attrs = { 'data-idx': '0', 'data-lat': '46.2', 'data-lon': '6.15', 'data-type': 'place',
                  'data-name': 'Geneva' };
  sel('de')({ getAttribute: (k) => attrs[k] });
  sel('')({ getAttribute: (k) => attrs[k] });
  assert.deepStrictEqual(pinned, ['Genf', 'Geneva']);
});
ok('shown names: never the same name twice, never a non-string', () => {
  assert.deepStrictEqual(V._szShownNames({ n: 'Genève', ln: { fr: 'Genève' } }, 'fr'), ['Genève', '']);
  assert.deepStrictEqual(V._szShownNames({ n: 'X', ln: { de: 3 } }, 'de'), ['X', '']);
  assert.deepStrictEqual(V._szShownNames({ n: 'X', nn: 'Y', ln: { de: ['Z'] } }, 'de'), ['X', 'Y']);
});
ok('the Find page matches part of a name in another language', () => {
  assert.ok(P.matchesName(GENEVA, 'Ginev') && P.matchesName(GENEVA, 'nevr'));
  assert.ok(!P.matchesName(GENEVA, 'Ginevrax'));
});

// A spelling several languages share keeps them all: Lucerne's "Lucerna"
// (es, it, rm) is the city for an Italian or a Romansh visitor.
ok('a name keeps every language that spells it so', () => {
  const LUCERNE = { n: 'Lucerne', nn: 'Luzern', t: 'place', s: 'town', ln: { es: 'Lucerna', it: 'Lucerna', rm: 'Lucerna', fr: '' } };
  const LUCERNA = { n: 'Lucerna', t: 'place', s: 'village' };
  for (const l of ['es', 'it', 'rm']) assert.strictEqual(top([LUCERNE, LUCERNA], 'Lucerna', [l]), 'Lucerne', l);
  assert.strictEqual(top([LUCERNE, LUCERNA], 'Lucerna', ['en']), 'Lucerna');
  assert.deepStrictEqual(V._szLnNames(LUCERNE), ['Lucerna']);
});

console.log(`\n${pass} passed`);
