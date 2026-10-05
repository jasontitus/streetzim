// The viewer's UI-string runtime (resources/viewer/i18n/runtime.js, inlined
// into index.html and places.html by tools/build_i18n.py; docs/i18n.md):
// language choice, lookup with English fallback, placeholders, plurals,
// number/date formats, the pseudo-locale, static-markup translation, and
// the tables each built page carries.
//
//   node tests/viewer_i18n_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const RUNTIME = fs.readFileSync(`${REPO}/resources/viewer/i18n/runtime.js`, 'utf8');
const DE = JSON.parse(fs.readFileSync(`${REPO}/resources/viewer/i18n/de.json`, 'utf8'));
const EN = JSON.parse(fs.readFileSync(`${REPO}/resources/viewer/i18n/en.json`, 'utf8'));

let pass = 0;
async function ok(name, fn) {
  try { await fn(); console.log('ok   ', name); pass++; }
  catch (e) { console.error('FAIL ', name, '\n      ', e.stack || e.message); process.exitCode = 1; }
}

// The tables a built page carries (its <script id="sz-i18n">).
function pageTables(file) {
  const html = fs.readFileSync(`${REPO}/resources/viewer/${file}`, 'utf8');
  const m = /<script type="application\/json" id="sz-i18n">([\s\S]*?)<\/script>/.exec(html);
  assert.ok(m, file + ' has no sz-i18n block');
  return { html, json: m[1] };
}

// A fresh runtime with a fake document: `tables` is the inline JSON text,
// `els` the elements querySelectorAll returns (for szApplyI18n).
function load(tables, els = []) {
  const docEl = { lang: '' };
  const events = [];
  const document = {
    documentElement: docEl,
    getElementById: (id) => (id === 'sz-i18n' && tables != null ? { textContent: tables } : null),
    querySelectorAll: () => els,
    createEvent: () => ({ initEvent(t) { this.type = t; } }),
  };
  const window = { dispatchEvent: (e) => events.push(e.type) };
  const api = new Function('document', 'window', RUNTIME + `
    return { szSetUiLanguage, szUseLanguage, szT, szTn, szFixed, szLocaleNum, szMonthYear,
             szApplyI18n, szChipLabel, szChipInSentence, szPlaceType, szCompactNum,
             get lang() { return SZ_UI_LANG; }, get pseudo() { return SZ_I18N_PSEUDO; } };`)(document, window);
  return { api, docEl, events };
}
function el(attrs, text) {
  return { textContent: text, attrs: Object.assign({}, attrs),
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = v; } };
}

const { json: INDEX_TABLES } = pageTables('index.html');

await ok('English: no table, the call site text, exactly as before', () => {
  const { api, docEl } = load(INDEX_TABLES);
  assert.strictEqual(api.szSetUiLanguage(undefined, ''), 'en');
  assert.strictEqual(api.szT('routing.no_route', 'No route found'), 'No route found');
  assert.strictEqual(api.szT('x.y', 'No {chip} in this map', { chip: 'bars' }), 'No bars in this map');
  assert.strictEqual(api.szTn('place.links', 1, { one: '{n} link', other: '{n} links' }), '1 link');
  assert.strictEqual(api.szTn('place.links', 3, { one: '{n} link', other: '{n} links' }), '3 links');
  // Formats are the old toFixed / toLocaleString / month names.
  assert.strictEqual(api.szFixed(1.25, 1), (1.25).toFixed(1));
  assert.strictEqual(api.szFixed(12345.678, 2), '12345.68');
  assert.strictEqual(api.szLocaleNum(209668), (209668).toLocaleString());
  assert.strictEqual(api.szMonthYear(2026, 6), 'July 2026');
  assert.strictEqual(api.szCompactNum(1234), '1.2K');
  assert.strictEqual(api.szCompactNum(5600000), '5.6M');
  assert.strictEqual(api.szCompactNum(999), '999');
  assert.strictEqual(api.szChipLabel('food', 'Restaurants & More'), 'Restaurants & More');
  assert.strictEqual(api.szChipInSentence('food', 'Food & Drink'), 'food & drink');
  assert.strictEqual(api.szPlaceType('fast_food'), 'fast food');
  assert.strictEqual(docEl.lang, '');   // an English page is left as it was
});

await ok('a language without a table (fr) falls back to English, string by string', () => {
  const { api } = load(INDEX_TABLES);
  assert.strictEqual(api.szSetUiLanguage('fr', ''), 'en');
  assert.strictEqual(api.szT('routing.clear', 'Clear route'), 'Clear route');
});

await ok('German: the table, its plurals and number/date formats', () => {
  const { api, docEl } = load(INDEX_TABLES);
  assert.strictEqual(api.szSetUiLanguage('de', ''), 'de');
  assert.strictEqual(docEl.lang, 'de');
  assert.strictEqual(api.szT('routing.clear', 'Clear route'), DE['routing.clear']);
  assert.strictEqual(api.szT('find.none_in_map', 'No {chip} in this map', { chip: 'Parks' }),
                     DE['find.none_in_map'].replace('{chip}', 'Parks'));
  assert.strictEqual(api.szTn('place.links', 1, { one: '{n} link', other: '{n} links' }), '1 Link');
  assert.strictEqual(api.szTn('place.links', 2, { one: '{n} link', other: '{n} links' }), '2 Links');
  assert.strictEqual(api.szFixed(1.25, 1), (1.25).toFixed(1).replace('.', ','));
  assert.strictEqual(api.szLocaleNum(209668), '209.668');
  assert.strictEqual(api.szMonthYear(2026, 6), 'Juli 2026');
  assert.strictEqual(api.szCompactNum(5600000), '5,6 Mio.');
  assert.strictEqual(api.szChipLabel('food', 'Food & Drink'), DE['chip.food']);
  assert.strictEqual(api.szChipLabel('restaurants', 'Restaurants'), DE['chip.restaurants']);
  assert.strictEqual(api.szChipLabel('new_chip', 'New Chip'), 'New Chip');   // unknown id: as given
  assert.strictEqual(api.szChipInSentence('food', 'Food & Drink'), DE['chip.food']);   // no lower-casing
  assert.strictEqual(api.szPlaceType('fast_food'), DE['type.fast_food']);
  assert.strictEqual(api.szPlaceType('kebab_shop_xyz'), 'kebab shop xyz');   // not in the table
  // A key the table lacks shows the call site's English, never the key.
  assert.strictEqual(api.szT('no.such_key', 'English text'), 'English text');
});

await ok('?uilang= overrides the map language; qps is the pseudo-locale', () => {
  let r = load(INDEX_TABLES);
  assert.strictEqual(r.api.szSetUiLanguage('fr', '?uilang=de&x=1'), 'de');
  r = load(INDEX_TABLES);
  assert.strictEqual(r.api.szSetUiLanguage('de', '?bust=1&uilang=en'), 'en');
  r = load(INDEX_TABLES);
  r.api.szSetUiLanguage('de', '?uilang=qps');
  assert.strictEqual(r.api.pseudo, true);
  assert.strictEqual(r.api.szT('routing.clear', 'Clear route'), '[Clear route]');
  assert.strictEqual(r.api.szTn('place.links', 2, { one: '{n} link', other: '{n} links' }), '[2 links]');
  assert.strictEqual(r.api.szChipLabel('food', 'Food & Drink'), '[Food & Drink]');
  assert.strictEqual(r.api.szPlaceType('cafe'), '[cafe]');
  // Formats stay English under the pseudo-locale.
  assert.strictEqual(r.api.szFixed(1.25, 1), '1.3');
});

await ok('a broken or missing table is English, not an exception', () => {
  for (const t of [null, '{not json', '[]', '{"de": null}']) {
    const { api } = load(t);
    assert.strictEqual(api.szSetUiLanguage('de', ''), 'en', String(t));
    assert.strictEqual(api.szT('routing.clear', 'Clear route'), 'Clear route');
  }
});

await ok('placeholders: every one filled, an unknown one left visible', () => {
  const { api } = load(INDEX_TABLES);
  api.szSetUiLanguage('en', '');
  assert.strictEqual(api.szT('a.b', '{a} and {b}', { a: 1, b: 0 }), '1 and 0');
  assert.strictEqual(api.szT('a.b', '{a} and {c}', { a: 1 }), '1 and {c}');
  assert.strictEqual(api.szT('a.b', '$& {a}', { a: '$1' }), '$& $1');
});

await ok('static markup: one pass in German, untouched in English', () => {
  const mk = () => [el({ 'data-i18n': 'routing.clear' }, 'Clear route'),
                    el({ 'data-i18n-placeholder': 'search.placeholder', placeholder: 'Search places, streets, POIs...' }, ''),
                    el({ 'data-i18n-title': 'common.close', title: 'Close', 'data-i18n-aria-label': 'common.close',
                         'aria-label': 'Close' }, '×'),
                    el({ 'data-i18n': 'no.such_key' }, '  Some\n   English  ')];
  let els = mk();
  let r = load(INDEX_TABLES, els);
  r.api.szUseLanguage(undefined, '');
  assert.deepStrictEqual(els.map((e) => e.textContent), ['Clear route', '', '×', '  Some\n   English  ']);
  assert.deepStrictEqual(r.events, ['sz-ui-language']);
  els = mk();
  r = load(INDEX_TABLES, els);
  r.api.szUseLanguage('de', '');
  assert.strictEqual(els[0].textContent, DE['routing.clear']);
  assert.strictEqual(els[1].attrs.placeholder, DE['search.placeholder']);
  assert.strictEqual(els[2].attrs.title, DE['common.close']);
  assert.strictEqual(els[2].attrs['aria-label'], DE['common.close']);
  assert.strictEqual(els[2].textContent, '×');
  assert.strictEqual(els[3].textContent, 'Some English');   // missing key: the markup's own text
  els = mk();
  r = load(INDEX_TABLES, els);
  r.api.szUseLanguage('de', '?uilang=qps');
  assert.strictEqual(els[0].textContent, '[Clear route]');
  assert.strictEqual(els[1].attrs.placeholder, '[Search places, streets, POIs...]');
});

await ok('each page carries the German for exactly its own keys', () => {
  for (const file of ['index.html', 'places.html']) {
    const t = JSON.parse(pageTables(file).json);
    assert.deepStrictEqual(Object.keys(t), ['de'], file);
    for (const [k, v] of Object.entries(t.de)) assert.deepStrictEqual(v, DE[k], file + ' ' + k);
  }
  const idx = JSON.parse(INDEX_TABLES).de, plc = JSON.parse(pageTables('places.html').json).de;
  assert.ok('drive.roundabout' in idx && !('drive.roundabout' in plc));
  assert.ok('places.title' in plc && !('places.title' in idx));
  assert.ok('type.restaurant' in idx && 'type.restaurant' in plc);
  // Every English key is in some page's table.
  for (const k of Object.keys(EN)) assert.ok(k in idx || k in plc, 'not inlined anywhere: ' + k);
});

console.log(`${pass} passed`);
