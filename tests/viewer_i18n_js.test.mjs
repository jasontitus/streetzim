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
const table = (lang) => JSON.parse(fs.readFileSync(`${REPO}/resources/viewer/i18n/${lang}.json`, 'utf8'));
const DE = table('de');
const EN = table('en');
const TH = table('th');
const ZH = table('zh');
const NEW_MACHINE = ['el', 'es', 'fr', 'ja', 'ko'];
const TABLES = { de: DE, th: TH, zh: ZH, ...Object.fromEntries(NEW_MACHINE.map((l) => [l, table(l)])) };

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
             szApplyI18n, szChipLabel, szChipInSentence, szPlaceType, szCompactNum, szCompactIntl, szMachineTranslated,
             get lang() { return SZ_UI_LANG; }, get pseudo() { return SZ_I18N_PSEUDO; } };`)(document, window);
  return { api, docEl, events };
}
function el(attrs, text) {
  return { textContent: text, attrs: Object.assign({}, attrs), style: { display: 'none' },
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

await ok('a language without a table (it) falls back to English, string by string', () => {
  const { api } = load(INDEX_TABLES);
  assert.strictEqual(api.szSetUiLanguage('it', ''), 'en');
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

await ok('Thai and Chinese: picked by the map language and by ?uilang=', () => {
  let r = load(INDEX_TABLES);
  assert.strictEqual(r.api.szSetUiLanguage('th', ''), 'th');
  assert.strictEqual(r.docEl.lang, 'th');
  assert.strictEqual(r.api.szT('routing.clear', 'Clear route'), TH['routing.clear']);
  assert.strictEqual(r.api.szChipLabel('food', 'Food & Drink'), TH['chip.food']);
  assert.strictEqual(r.api.szPlaceType('restaurant'), TH['type.restaurant']);
  // One plural category ("other") for every count, the number in {n}.
  assert.strictEqual(r.api.szTn('place.links', 1, { one: '{n} link', other: '{n} links' }),
                     TH['place.links'].other.replace('{n}', '1'));
  assert.strictEqual(r.api.szCompactNum(5600000), TH['num.millions'].replace('{n}', '5.6'));
  r = load(INDEX_TABLES);
  assert.strictEqual(r.api.szSetUiLanguage('zh', ''), 'zh');
  assert.strictEqual(r.docEl.lang, 'zh-Hans');   // the table's _lang: Simplified glyphs
  assert.strictEqual(r.api.szT('routing.clear', 'Clear route'), ZH['routing.clear']);
  assert.strictEqual(r.api.szTn('search.placeholder_count', 1234, { one: 'Search {n} places...', other: 'Search {n} places...' }),
                     ZH['search.placeholder_count'].other.replace('{n}', '1,234'));
  assert.strictEqual(r.api.szMonthYear(2026, 6), '2026年7月');
  for (const [map, q, want] of [['en', '?uilang=th', 'th'], ['de', '?x=1&uilang=zh', 'zh'],
                                ['en', '?uilang=zh-CN', 'zh'], ['th', '', 'th'], ['zh', '?uilang=en', 'en']]) {
    r = load(INDEX_TABLES);
    assert.strictEqual(r.api.szSetUiLanguage(map, q), want, map + ' ' + q);
  }
});

await ok('Spanish, French, Japanese, Korean, Greek: a table, <html lang> = the map language', () => {
  for (const lang of NEW_MACHINE) {
    const r = load(INDEX_TABLES);
    assert.strictEqual(r.api.szSetUiLanguage(lang, ''), lang);
    assert.strictEqual(r.docEl.lang, lang);
    assert.strictEqual(r.api.szT('routing.clear', 'Clear route'), TABLES[lang]['routing.clear']);
    assert.notStrictEqual(TABLES[lang]['routing.clear'], 'Clear route', lang);
    assert.strictEqual(r.api.szMachineTranslated(), true, lang);
    // A region subtag or upper case: the same table.
    const r2 = load(INDEX_TABLES);
    assert.strictEqual(r2.api.szSetUiLanguage(lang.toUpperCase() + '-XX', ''), lang);
    assert.strictEqual(r2.docEl.lang, lang);
  }
  // A language without a table: English, <html lang> untouched.
  const r = load(INDEX_TABLES);
  assert.strictEqual(r.api.szSetUiLanguage('it', ''), 'en');
  assert.strictEqual(r.docEl.lang, '');
  // Places.html carries them too.
  const places = JSON.parse(pageTables('places.html').json);
  for (const lang of NEW_MACHINE) assert.ok(places[lang] && places[lang]._machine === true, lang);
});

await ok('Japanese, Korean, Chinese: large numbers in ten thousands, smaller ones written out', () => {
  const NS = [999, 1234, 9999, 12000, 123456, 5600000, 1.1e9];
  const want = {
    ja: ['999', '1,234', '9,999', '1.2万', '12.3万', '560万', '11億'],
    ko: ['999', '1,234', '9,999', '1.2만', '12.3만', '560만', '11억'],
    zh: ['999', '1,234', '9,999', '1.2万', '12.3万', '560万', '11亿'],
  };
  for (const [lang, w] of Object.entries(want)) {
    assert.strictEqual(TABLES[lang]._compact, 'intl', lang);
    const r = load(INDEX_TABLES);
    r.api.szSetUiLanguage(lang, '');
    assert.deepStrictEqual(NS.map((n) => r.api.szCompactNum(n)), w, lang);
    assert.deepStrictEqual(NS.map((n) => r.api.szCompactIntl(n)), w, lang);
  }
  // Every other table, and English: num.* as before; szCompactIntl says ''.
  for (const lang of ['de', 'th', 'es', 'fr', 'el']) {
    const r = load(INDEX_TABLES);
    r.api.szSetUiLanguage(lang, '');
    assert.strictEqual(r.api.szCompactIntl(5600000), '', lang);
    assert.strictEqual(r.api.szCompactNum(5600000), TABLES[lang]['num.millions'].replace('{n}', r.api.szFixed(5.6, 1)), lang);
  }
  const en = load(INDEX_TABLES);
  en.api.szSetUiLanguage('en', '');
  assert.deepStrictEqual([999, 1234, 5600000].map((n) => en.api.szCompactNum(n)), ['999', '1.2K', '5.6M']);
  assert.strictEqual(en.api.szCompactIntl(5600000), '');
  // Where Intl has no compact notation (an older engine ignores the
  // option): the number written out, never a 千 / 천 form.
  const NF = Intl.NumberFormat;
  try {
    Intl.NumberFormat = function(l, o) { const f = new NF(l, Object.assign({}, o, { notation: 'standard' })); return f; };
    const r = load(INDEX_TABLES);
    r.api.szSetUiLanguage('ja', '');
    assert.strictEqual(r.api.szCompactNum(5600000), '5,600,000');
  } finally { Intl.NumberFormat = NF; }
  // The Wiki list's population (410) takes it too.
  const panel = fs.readFileSync(`${REPO}/resources/viewer/src/index/410-wiki-sidebar.js`, 'utf8');
  assert.match(panel, /var pop = szCompactIntl\(wd\.p\) \|\| \(wd\.p >= 1e6/);
});

await ok('the machine-translation note shows for machine tables only', () => {
  const note = () => el({ 'data-i18n': 'common.machine_translated', 'data-i18n-machine': '' },
                        'Interface translated by machine — corrections welcome');
  const plain = () => el({ 'data-i18n': 'routing.clear' }, 'Clear route');
  for (const [lang, shown] of [['th', true], ['zh', true], ['de', false], ['en', false],
                               ['it', false], ['qps', false], ...NEW_MACHINE.map((l) => [l, true])]) {
    const els = [note(), plain()];
    const r = load(INDEX_TABLES, els);
    r.api.szUseLanguage(lang, '');
    assert.strictEqual(r.api.szMachineTranslated(), shown, lang);
    assert.strictEqual(els[0].style.display, shown ? '' : 'none', lang);
    assert.strictEqual(els[1].style.display, 'none', lang + ': only data-i18n-machine is touched');
    if (shown) assert.strictEqual(els[0].textContent, TABLES[lang]['common.machine_translated'], lang);
  }
  // ?uilang=de on a Thai map: German, no note.
  const els = [note()];
  const r = load(INDEX_TABLES, els);
  r.api.szUseLanguage('th', '?uilang=de');
  assert.strictEqual(els[0].style.display, 'none');
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

await ok('each page carries every table, with exactly its own keys', () => {
  for (const file of ['index.html', 'places.html']) {
    const t = JSON.parse(pageTables(file).json);
    assert.deepStrictEqual(Object.keys(t).sort(), Object.keys(TABLES).sort(), file);
    for (const [lang, src] of Object.entries(TABLES)) {
      for (const [k, v] of Object.entries(t[lang])) {
        if (k === 'type') {   // place types, nested (tools/build_i18n.py tables_for)
          for (const [tk, tv] of Object.entries(v)) assert.strictEqual(tv, src['type.' + tk], `${file} ${lang} type.${tk}`);
          continue;
        }
        assert.deepStrictEqual(v, src[k], `${file} ${lang} ${k}`);
      }
      // Table settings go to both pages.
      for (const k of ['_machine', '_lang']) assert.strictEqual(t[lang][k], src[k], `${file} ${lang} ${k}`);
    }
  }
  const idx = JSON.parse(INDEX_TABLES).de, plc = JSON.parse(pageTables('places.html').json).de;
  assert.ok('drive.roundabout' in idx && !('drive.roundabout' in plc));
  assert.ok('places.title' in plc && !('places.title' in idx));
  assert.ok('restaurant' in idx.type && 'restaurant' in plc.type);
  // A place type that is also an Object property does not leak through.
  const { api } = load(INDEX_TABLES);
  api.szSetUiLanguage('de', '');
  assert.strictEqual(api.szPlaceType('constructor'), 'constructor');
  assert.strictEqual(api.szPlaceType('__proto__'), '  proto  ');   // the raw key, underscores as spaces
  // Every English key is in some page's table.
  for (const k of Object.keys(EN)) assert.ok(k in idx || k in plc, 'not inlined anywhere: ' + k);
});

console.log(`${pass} passed`);
