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
  '\nreturn { _szSearchForms, _szFormsContain, _szTextScore, _szAdminCamera, _szAdminKeys };')();
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
  assert.deepStrictEqual(V._szSearchForms(SHOP, norm), ['arlington']);
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
  assert.match(search, /map\.fitBounds\(adminCam\.bounds/);
  assert.match(search, /_szFormsContain\(rec, qNorm, normalizeText\)/);
  assert.match(search, /var textScore = _szTextScore\(item, q, words, normalizeText\);/);
  const init = fs.readFileSync(`${REPO}/resources/viewer/src/index/120-map-init-and-style.js`, 'utf8');
  assert.match(init, /hash\.match\(\/bounds=/);
  assert.match(init, /if \(!fitted\) map\.flyTo/);
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

console.log(`\n${pass} passed`);

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
  '\nreturn { set: (l) => { SZ_LANG = l; }, field: szLabelField, of: szLabelOf };')();
ok('labels follow the build language', () => {
  LBL.set('en');
  assert.deepStrictEqual(LBL.field()[1], ['get', 'name:latin']);
  assert.strictEqual(LBL.of({ 'name:latin': 'Tokyo', name_int: '東京', 'name:fr': 'Tokyo' }), 'Tokyo');
  LBL.set('fr');
  assert.deepStrictEqual(LBL.field().slice(1).map((g) => g[1]),
    ['name:fr', 'name', 'name_int', 'name:latin']);
  assert.strictEqual(LBL.of({ 'name:latin': 'Imperial Palace', name_int: '皇居', 'name:fr': 'Palais impérial' }), 'Palais impérial');
  assert.strictEqual(LBL.of({ 'name:latin': 'Peking University', name_int: '北京大学' }), '北京大学');
  assert.strictEqual(LBL.of({ name: '東京タワー', name_int: 'Tokyo Tower' }), '東京タワー');   // OpenFreeMap
  LBL.set('en');
});
