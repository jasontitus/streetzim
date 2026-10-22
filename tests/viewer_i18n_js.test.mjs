// The viewer's UI-string runtime (resources/viewer/i18n/runtime.js, inlined
// into index.html and places.html by tools/build_i18n.py; docs/i18n.md):
// language choice, lookup with English fallback, placeholders, plurals,
// number/date formats, the pseudo-locale, static-markup translation, and
// the tables each built page carries.
//
//   node tests/viewer_i18n_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';
import zlib from 'node:zlib';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const RUNTIME = fs.readFileSync(`${REPO}/resources/viewer/i18n/runtime.js`, 'utf8');
const table = (lang) => JSON.parse(fs.readFileSync(`${REPO}/resources/viewer/i18n/${lang}.json`, 'utf8'));
const DE = table('de');
const EN = table('en');
const TH = table('th');
const ZH = table('zh');
const NEW_MACHINE = ['el', 'es', 'fr', 'ja', 'ko'];
// The core languages' tables added 2026-10-21 (docs/i18n.md), and Swedish.
const CORE_NEW = ['ar', 'fa', 'he', 'hi', 'id', 'it', 'nl', 'pl', 'pt', 'ru', 'sv', 'tr', 'uk', 'vi'];
const RTL = ['ar', 'fa', 'he'];
// Every table in resources/viewer/i18n but English.
const ALL = fs.readdirSync(`${REPO}/resources/viewer/i18n`)
  .filter((n) => /^[a-z]{2,3}\.json$/.test(n) && n !== 'en.json').map((n) => n.slice(0, -5)).sort();
const TABLES = Object.fromEntries(ALL.map((l) => [l, table(l)]));
// A language with no table (falls back to English).
const NO_TABLE = 'sw';

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
// Its tables decoded with node's zlib, not the runtime's szInflate (a
// broken decoder must not check itself): { lang: table as the lookups
// read it, place types nested under "type" }.
function decoded(file) {
  const block = JSON.parse(pageTables(file).json);
  assert.strictEqual(block.v, 2, file);
  const unpack = (s) => JSON.parse(zlib.inflateRawSync(Buffer.from(s, 'base64')).toString('utf8'));
  const keys = unpack(block.k);
  const out = {};
  for (const [lang, packed] of Object.entries(block.t)) {
    const texts = unpack(packed);
    assert.strictEqual(texts.length, keys.length, file + ' ' + lang);
    const t = Object.assign({}, block.m[lang]);
    keys.forEach((k, i) => {
      if (texts[i] === 0) return;
      if (k.startsWith('type.')) (t.type = t.type || {})[k.slice(5)] = texts[i];
      else t[k] = texts[i];
    });
    out[lang] = t;
  }
  return out;
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
             szInflate, szTableFrom,
             get lang() { return SZ_UI_LANG; }, get pseudo() { return SZ_I18N_PSEUDO; },
             get rtl() { return SZ_UI_RTL; }, get table() { return SZ_I18N; } };`)(document, window);
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

await ok('a language without a table (sw) falls back to English, string by string', () => {
  const { api } = load(INDEX_TABLES);
  assert.strictEqual(api.szSetUiLanguage(NO_TABLE, ''), 'en');
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
  assert.strictEqual(r.api.szSetUiLanguage(NO_TABLE, ''), 'en');
  assert.strictEqual(r.docEl.lang, '');
  // Places.html carries them too.
  const places = decoded('places.html');
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
                               [NO_TABLE, false], ['qps', false], ...NEW_MACHINE.map((l) => [l, true]),
                               ...CORE_NEW.map((l) => [l, true])]) {
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
    const t = decoded(file);
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
  const idx = decoded('index.html').de, plc = decoded('places.html').de;
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

await ok('the core languages: a table each, <html lang>, the note, a translated string', () => {
  for (const lang of CORE_NEW) {
    const r = load(INDEX_TABLES);
    assert.strictEqual(r.api.szSetUiLanguage(lang, ''), lang, lang);
    assert.strictEqual(r.docEl.lang, lang, lang);
    assert.strictEqual(r.api.szMachineTranslated(), true, lang);
    assert.strictEqual(r.api.szT('routing.clear', 'Clear route'), TABLES[lang]['routing.clear'], lang);
    assert.notStrictEqual(TABLES[lang]['routing.clear'], 'Clear route', lang);
    assert.strictEqual(r.api.szPlaceType('restaurant'), TABLES[lang]['type.restaurant'], lang);
    assert.strictEqual(r.api.szChipLabel('food', 'Food & Drink'), TABLES[lang]['chip.food'], lang);
  }
  // A merged map's language with a region subtag (pt-BR) or another case: the table.
  const r = load(INDEX_TABLES);
  assert.strictEqual(r.api.szSetUiLanguage('pt-BR', ''), 'pt');
  assert.deepStrictEqual(Object.keys(decoded('index.html')).sort(), ALL);
});

await ok('right to left: ar, fa, he set <html dir="rtl">; other languages leave it', () => {
  for (const lang of ALL) {
    const r = load(INDEX_TABLES);
    r.api.szSetUiLanguage(lang, '');
    assert.strictEqual(r.api.rtl, RTL.includes(lang), lang);
    assert.strictEqual(r.docEl.dir, RTL.includes(lang) ? 'rtl' : undefined, lang);
  }
  // ?uilang=he on an English map: RTL; English and the pseudo-locale: not.
  let r = load(INDEX_TABLES);
  r.api.szSetUiLanguage('en', '?uilang=he');
  assert.strictEqual(r.docEl.dir, 'rtl');
  for (const [lang, q] of [['en', ''], ['ar', '?uilang=qps'], ['ar', '?uilang=en'], [NO_TABLE, '']]) {
    r = load(INDEX_TABLES);
    r.api.szSetUiLanguage(lang, q);
    assert.strictEqual(r.api.rtl, false, lang + q);
    assert.strictEqual(r.docEl.dir, undefined, lang + q);
  }
  // Chosen again (a page that switches): back to ltr.
  r = load(INDEX_TABLES);
  r.api.szSetUiLanguage('ar', '');
  r.api.szSetUiLanguage('de', '');
  assert.strictEqual(r.docEl.dir, 'ltr');
});

await ok('right to left: values in a sentence are isolated runs; left to right: unchanged', () => {
  const FSI = '\u2068', PDI = '\u2069', LRI = '\u2066';
  let r = load(INDEX_TABLES);
  r.api.szSetUiLanguage('he', '');
  assert.strictEqual(r.api.szT('place.distance_from', '{dist} from {place}', { dist: '3 km', place: '25 de Mayo' }),
                     TABLES.he['place.distance_from'].replace('{dist}', FSI + '3 km' + PDI).replace('{place}', FSI + '25 de Mayo' + PDI));
  // An empty value adds nothing; a missing one stays visible as before.
  assert.strictEqual(r.api.szT('x.y', 'a{v}b{w}', { v: '' }), 'ab{w}');
  // Counts too ({n}), and szTn's own number.
  assert.ok(r.api.szTn('place.links', 5, { one: '{n} link', other: '{n} links' }).includes(FSI + '5' + PDI));
  // English and German: byte for byte as before.
  for (const lang of ['en', 'de']) {
    r = load(INDEX_TABLES);
    r.api.szSetUiLanguage(lang, '');
    assert.strictEqual(r.api.szT('x.y', '{a} and {b}', { a: '25 de Mayo', b: 3 }), '25 de Mayo and 3');
  }
  // szLtr: an LTR isolate in RTL only.
  const L = new Function('document', 'window', RUNTIME + '; return { szSetUiLanguage, szLtr };')(
    { documentElement: {}, getElementById: () => ({ textContent: INDEX_TABLES }) }, {});
  assert.strictEqual(L.szLtr('3.2 km'), '3.2 km');
  L.szSetUiLanguage('ar', '');
  assert.strictEqual(L.szLtr('3.2 km'), LRI + '3.2 km' + PDI);
  assert.strictEqual(L.szLtr(''), '');
});

await ok('Arabic and Persian write numbers and dates in Latin digits (_nu)', () => {
  for (const lang of ['ar', 'fa']) {
    assert.strictEqual(TABLES[lang]._nu, 'latn', lang);
    const r = load(INDEX_TABLES);
    r.api.szSetUiLanguage(lang, '');
    assert.match(r.api.szLocaleNum(1234567), /^1.234.567$/, lang);
    assert.match(r.api.szFixed(3.25, 1), /^3.[23]$/, lang);
    assert.match(r.api.szMonthYear(2026, 6), /\d{4}/, lang);
    assert.doesNotMatch(r.api.szMonthYear(2026, 6) + r.api.szLocaleNum(1234567), /[\u0660-\u0669\u06F0-\u06F9]/, lang);
    assert.strictEqual(r.api.szTn('place.links', 21, { one: '{n} link', other: '{n} links' }).replace(/[^0-9]/g, ''),
                       lang === 'ar' ? '21' : '21', lang);
  }
  // Hebrew and Hindi already default to Latin digits; Hindi counts in lakh / crore.
  const hi = load(INDEX_TABLES);
  hi.api.szSetUiLanguage('hi', '');
  assert.match(hi.api.szCompactNum(5600000), /^56\s+लाख$/);   // Intl puts a no-break space
  assert.strictEqual(hi.api.szCompactNum(1234), '1,234');
});

await ok('plural categories: Arabic six, Russian / Polish / Ukrainian four, Hebrew dual', () => {
  const F = { one: '{n} link', other: '{n} links' };
  const want = {
    ar: [[0, 'zero'], [1, 'one'], [2, 'two'], [3, 'few'], [11, 'many'], [100, 'other']],
    ru: [[1, 'one'], [2, 'few'], [5, 'many'], [21, 'one'], [1.5, 'other']],
    pl: [[1, 'one'], [3, 'few'], [5, 'many'], [22, 'few'], [1.5, 'other']],
    uk: [[1, 'one'], [4, 'few'], [11, 'many'], [21, 'one']],
    he: [[1, 'one'], [2, 'two'], [3, 'other']],
  };
  for (const [lang, cases] of Object.entries(want)) {
    const r = load(INDEX_TABLES);
    r.api.szSetUiLanguage(lang, '');
    for (const [n, cat] of cases) {
      const form = TABLES[lang]['place.links'][cat];
      assert.ok(form, `${lang} place.links.${cat}`);
      const got = r.api.szTn('place.links', n, F);
      const num = r.api.szLocaleNum(n);
      assert.strictEqual(got, form.replace('{n}', RTL.includes(lang) ? '\u2068' + num + '\u2069' : num), `${lang} n=${n}`);
    }
  }
});

await ok('the runtime decodes every table of both pages exactly as zlib does', () => {
  for (const file of ['index.html', 'places.html']) {
    const want = decoded(file);
    const block = JSON.parse(pageTables(file).json);
    const { api } = load(pageTables(file).json);
    for (const lang of ALL) {
      assert.deepStrictEqual(api.szTableFrom(block, lang), want[lang], `${file} ${lang}`);
    }
    assert.strictEqual(api.szTableFrom(block, 'en'), null);
    assert.strictEqual(api.szTableFrom(block, '__proto__'), null);
  }
});

await ok('a damaged block is English, not an exception', () => {
  const block = JSON.parse(INDEX_TABLES);
  const bad = [
    Object.assign({}, block, { t: Object.assign({}, block.t, { de: block.t.de.slice(0, 200) }) }),   // truncated
    Object.assign({}, block, { t: Object.assign({}, block.t, { de: '!!!' + block.t.de.slice(3) }) }), // not base64
    Object.assign({}, block, { k: block.t.de }),                                                     // keys ≠ texts
    Object.assign({}, block, { v: 1 }),                                                              // unknown format
  ];
  // TEXTS one short of KEYS: the keys would shift onto the wrong texts.
  const pack = (a) => zlib.deflateRawSync(Buffer.from(JSON.stringify(a))).toString('base64');
  const unpack = (b64) => JSON.parse(zlib.inflateRawSync(Buffer.from(b64, 'base64')).toString('utf8'));
  bad.push(Object.assign({}, block, { t: Object.assign({}, block.t, { de: pack(unpack(block.t.de).slice(1)) }) }));
  for (const b of bad) {
    const { api, docEl } = load(JSON.stringify(b));
    assert.strictEqual(api.szSetUiLanguage('de', ''), 'en');
    assert.strictEqual(api.szT('routing.clear', 'Clear route'), 'Clear route');
    assert.strictEqual(docEl.lang, '');
  }
});

await ok('szInflate: stored, fixed and dynamic blocks, against node zlib', () => {
  const { api } = load(INDEX_TABLES);
  let seed = 7;
  const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  for (let i = 0; i < 60; i++) {
    const len = Math.floor(rnd() * 40000);
    const buf = Buffer.alloc(len);
    for (let j = 0; j < len; j++) buf[j] = i % 3 === 0 ? Math.floor(rnd() * 256) : i % 3 === 1 ? 97 + Math.floor(rnd() * 3) : (j % 251) ^ (j >> 7);
    for (const o of [{ level: 0 }, { level: 9 }, { level: 6, strategy: zlib.constants.Z_FIXED }, { level: 6, strategy: zlib.constants.Z_HUFFMAN_ONLY }]) {
      const out = api.szInflate(Array.from(zlib.deflateRawSync(buf, o)));
      assert.ok(Buffer.from(out).equals(buf), `len ${len} ${JSON.stringify(o)}`);
    }
  }
  assert.throws(() => api.szInflate([0xff, 0xff, 0xff]));       // reserved block type
  assert.throws(() => api.szInflate([]));                       // empty
  // A stored block whose length and its complement disagree.
  const stored = Array.from(zlib.deflateRawSync(Buffer.from('hello'), { level: 0 }));
  assert.strictEqual(Buffer.from(api.szInflate(stored)).toString(), 'hello');
  const broken = stored.slice(); broken[3] ^= 1;
  assert.throws(() => api.szInflate(broken), /stored/);
  // Truncated: a dynamic block cut short.
  const dyn = Array.from(zlib.deflateRawSync(Buffer.from('abcabcabd'.repeat(500)), { level: 9 }));
  assert.throws(() => api.szInflate(dyn.slice(0, dyn.length >> 1)), /truncated|bad/);
});

await ok('place types: lumped OpenMapTiles classes read right in English; a table wins', () => {
  let r = load(INDEX_TABLES);
  r.api.szSetUiLanguage('en', '');
  // "beer" is pubs and beer gardens, "car" dealers, garages and taxi stands,
  // "college" colleges and universities (process-openmaptiles.lua).
  assert.strictEqual(r.api.szPlaceType('beer'), 'pub');
  assert.strictEqual(r.api.szPlaceType('car'), 'car services');
  assert.strictEqual(r.api.szPlaceType('college'), 'college or university');
  assert.strictEqual(r.api.szPlaceType('fast_food'), 'fast food');      // the rest as before
  assert.strictEqual(r.api.szPlaceType('hasOwnProperty'), 'hasOwnProperty');
  r = load(INDEX_TABLES);
  r.api.szSetUiLanguage('de', '');
  assert.strictEqual(r.api.szPlaceType('beer'), DE['type.beer']);
  r = load(INDEX_TABLES);
  r.api.szSetUiLanguage('en', '?uilang=qps');
  assert.strictEqual(r.api.szPlaceType('beer'), '[pub]');
  // No table labels a pub a beer shop any more.
  for (const lang of ALL) assert.doesNotMatch(TABLES[lang]['type.beer'], /shop|toko|negozio|winkel|sklep|loja|магазин|крамниц|butik|dükkân|cửa hàng|متجر|فروشگاه|חנות|दुकान/i, lang);
});

await ok('Hindi: lakh and crore in their long form ("_compact": "intl-long")', () => {
  assert.strictEqual(TABLES.hi._compact, 'intl-long');
  const r = load(INDEX_TABLES);
  r.api.szSetUiLanguage('hi', '');
  assert.match(r.api.szCompactNum(123456), /^1\.2\s+लाख$/);
  assert.match(r.api.szCompactNum(12345678), /^1\.2\s+करोड़$/);
  assert.match(r.api.szCompactNum(1234567890), /^1\.2\s+अरब$/);
  assert.strictEqual(r.api.szCompactNum(1234), '1,234');
  // Japanese keeps the short form.
  const ja = load(INDEX_TABLES);
  ja.api.szSetUiLanguage('ja', '');
  assert.strictEqual(ja.api.szCompactNum(12000), '1.2万');
});

await ok('the Find page has no count for an empty list (pt: CLDR puts 0 in "one")', () => {
  const html = fs.readFileSync(`${REPO}/resources/viewer/places.html`, 'utf8');
  const m = /\nfunction _szMatchCount\(n\) \{[\s\S]*?\n\}/.exec(html);
  assert.ok(m, '_szMatchCount not found');
  for (const lang of ['pt', 'en', 'ru']) {
    const docEl = {};
    const f = new Function('document', 'window', RUNTIME + m[0] + '; return { szSetUiLanguage, _szMatchCount };')(
      { documentElement: docEl, getElementById: () => ({ textContent: pageTables('places.html').json }) }, {});
    f.szSetUiLanguage(lang, '');
    const zero = lang === 'en' ? 'No matches' : TABLES[lang]['places.zero_matches'];
    assert.strictEqual(f._szMatchCount(0), zero, lang);
    assert.doesNotMatch(f._szMatchCount(0), /0/, lang);
    if (lang === 'pt') assert.strictEqual(f._szMatchCount(1), '1 resultado');
    if (lang === 'en') assert.strictEqual(f._szMatchCount(2), '2 matches');
  }
});

await ok('right to left: an off-route distance and a coordinate pair keep their order', () => {
  const FSI = '\u2068', LRI = '\u2066', PDI = '\u2069';
  const r = new Function('document', 'window', RUNTIME + '; return { szSetUiLanguage, szT, szLtr };')(
    { documentElement: {}, getElementById: () => ({ textContent: INDEX_TABLES }) }, {});
  r.szSetUiLanguage('he', '');
  const off = r.szT('drive.off_route', 'Off route ({dist})', { dist: r.szLtr('120 m') });
  assert.ok(off.includes(FSI + LRI + '120 m' + PDI + PDI), JSON.stringify(off));
  const near = r.szT('places.near_coords', 'Near ({coords})', { coords: r.szLtr('47.377, 8.542') });
  assert.ok(near.includes(LRI + '47.377, 8.542' + PDI), JSON.stringify(near));
});

await ok('right to left: a text field follows what is typed, the page again when empty', () => {
  for (const [lang, on] of [['he', true], ['de', false]]) {
    const listeners = [];
    const document = { documentElement: {}, getElementById: () => ({ textContent: INDEX_TABLES }),
                       addEventListener: (t, f, c) => listeners.push([t, f, c]) };
    const api = new Function('document', 'window', RUNTIME + '; return { szSetUiLanguage };')(document, {});
    api.szSetUiLanguage(lang, '');
    api.szSetUiLanguage(lang, '');            // chosen twice: one listener
    assert.strictEqual(listeners.length, on ? 1 : 0, lang);
    if (!on) continue;
    assert.strictEqual(listeners[0][0], 'input');
    const input = { tagName: 'INPUT', type: 'search', value: 'Athens', attrs: {},
                    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } };
    listeners[0][1]({ target: input });
    assert.strictEqual(input.attrs.dir, 'auto');
    input.value = '';
    listeners[0][1]({ target: input });
    assert.ok(!('dir' in input.attrs));
    const box = { tagName: 'INPUT', type: 'checkbox', value: 'on', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } };
    listeners[0][1]({ target: box });
    assert.ok(!('dir' in box.attrs));
  }
});

await ok('szInflate refuses a distance past the output; UTF-8 of every length decodes', () => {
  const api = new Function(RUNTIME + '; return { szInflate, szUnpack };')();
  // A fixed-Huffman block by hand: literal "a", then length 3 at distance
  // 1 (valid: "aaaa") or distance 2 (only one byte so far: must throw).
  const block = (distSym) => {
    const out = []; let acc = 0, n = 0;
    const bit = (b) => { acc |= b << n; if (++n === 8) { out.push(acc); acc = 0; n = 0; } };
    const huff = (code, len) => { for (let i = len - 1; i >= 0; i--) bit((code >> i) & 1); };
    bit(1); bit(1); bit(0);                 // BFINAL, BTYPE = 01 (fixed)
    huff(0x30 + 97, 8);                      // literal "a"
    huff(1, 7);                              // length symbol 257: 3
    huff(distSym, 5);                        // distance symbol: 0 -> 1, 1 -> 2
    huff(0, 7);                              // end of block
    if (n) out.push(acc);
    return out;
  };
  assert.strictEqual(Buffer.from(api.szInflate(block(0))).toString(), 'aaaa');
  assert.throws(() => api.szInflate(block(1)), /distance/);
  // 1-, 2-, 3- and 4-byte UTF-8, a surrogate pair among them.
  const text = ['a', 'é', 'ß', 'Ж', 'ש', '€', 'ก', '中', '𝄞', '😀', 'x😀y', '\u{10FFFD}'];   // the last: lead byte F4
  const packed = zlib.deflateRawSync(Buffer.from(JSON.stringify(text))).toString('base64');
  assert.deepStrictEqual(api.szUnpack(packed), text);
});

await ok('arrow keys follow the reading direction: the carousel and travel-mode handlers, run', () => {
  const PARTS = `${REPO}/resources/viewer/src/index`;
  const carousel = /\nfunction _findResultsKeydown\(e\) \{[\s\S]*?\n\}/.exec(fs.readFileSync(`${PARTS}/200-find-results-view.js`, 'utf8'))[0];
  const travel = /travelRow\.addEventListener\('keydown', (function\(e\) \{[\s\S]*?\n    \})\);/.exec(fs.readFileSync(`${PARTS}/500-routing-panel.js`, 'utf8'))[1];
  for (const [lang, rtl] of [['he', true], ['de', false]]) {
    const run = new Function('document', 'window', RUNTIME + `
      var scrolled = [], modes = [], focused = [];
      var _findResultsState = { strip: {}, items: [1, 2, 3, 4], active: 1 };
      function _scrollCarouselTo(i) { scrolled.push(i); }
      function _hidePlaceDetail() {} function clearFindResults() {} function _showPlaceDetail() {}
      ${carousel}
      var routingModes = ['drive', 'walk', 'bike'], travelMode = 'walk';
      function setTravelMode(m) { modes.push(m); }
      var travelBtns = { drive: { focus: function() { focused.push('drive'); } },
                         walk: { focus: function() {} }, bike: { focus: function() { focused.push('bike'); } } };
      var travelKey = ${travel};
      return { szSetUiLanguage: szSetUiLanguage, carousel: _findResultsKeydown, travel: travelKey,
               scrolled: scrolled, modes: modes, focused: focused };`)(
      { documentElement: {}, getElementById: () => ({ textContent: INDEX_TABLES }) }, {});
    run.szSetUiLanguage(lang, '');
    const key = (k) => ({ key: k, target: { tagName: 'DIV' }, preventDefault() {} });
    run.carousel(key('ArrowLeft')); run.carousel(key('ArrowRight')); run.carousel(key('ArrowDown')); run.carousel(key('ArrowUp'));
    // From card 1: left moves on in RTL (to 2), back in LTR (to 0).
    assert.deepStrictEqual(run.scrolled, rtl ? [2, 0, 2, 0] : [0, 2, 2, 0], lang);
    run.travel(key('ArrowLeft')); run.travel(key('ArrowRight')); run.travel(key('x'));
    // From walk: left is bike (next) in RTL, drive (previous) in LTR.
    assert.deepStrictEqual(run.modes, rtl ? ['bike', 'drive'] : ['drive', 'bike'], lang);
    assert.deepStrictEqual(run.focused, rtl ? ['bike', 'drive'] : ['drive', 'bike'], lang);
  }
});

await ok('a language without a table falls back along SZ_UI_FALLBACK (rm -> de), not straight to English', () => {
  const mk = () => {
    const docEl = {};
    const api = new Function('document', 'window', RUNTIME + `
      return { szSetUiLanguage: szSetUiLanguage, szT: szT, setFallback: function(m) { SZ_UI_FALLBACK = m; },
               get lang() { return SZ_UI_LANG; } };`)(
      { documentElement: docEl, getElementById: () => ({ textContent: INDEX_TABLES }) }, {});
    return { api, docEl };
  };
  for (const tag of ['rm', 'RM', 'rm-CH', 'gsw']) {
    const { api, docEl } = mk();
    assert.strictEqual(api.szSetUiLanguage(tag, ''), 'de', tag);
    assert.strictEqual(docEl.lang, 'de', tag);   // the UI text is German
    assert.strictEqual(api.szT('routing.clear', 'Clear route'), DE['routing.clear'], tag);
  }
  // No table and no fallback: English. ?uilang=rm also falls back.
  let r = mk(); assert.strictEqual(r.api.szSetUiLanguage('sw', ''), 'en');
  r = mk(); assert.strictEqual(r.api.szSetUiLanguage('en', '?uilang=rm'), 'de');
  // Data-driven, and a cycle in the map cannot hang the page.
  r = mk(); r.api.setFallback({ xx: 'yy', yy: 'xx' }); assert.strictEqual(r.api.szSetUiLanguage('xx', ''), 'en');
  r = mk(); r.api.setFallback({ xx: 'fr' }); assert.strictEqual(r.api.szSetUiLanguage('xx', ''), 'fr');
  r = mk(); r.api.setFallback({}); assert.strictEqual(r.api.szSetUiLanguage('rm', ''), 'en');
});

await ok('link roads: an exit or an on-ramp only beside a motorway or trunk (szLinkKind)', () => {
  const src = fs.readFileSync(`${REPO}/resources/viewer/src/index/600-driving-mode.js`, 'utf8');
  const fn = /\n  function szLinkKind\(fromCls, toCls\) \{[\s\S]*?\n  \}/.exec(src)[0];
  const szLinkKind = new Function(fn + '; return szLinkKind;')();
  // class ordinals (docs/formats.md): motorway 1, trunk 3, primary 5,
  // secondary 7, tertiary 9, residential 11, unclassified 13, service 14
  const cases = [
    [1, 7, 'exit'],     // motorway -> secondary: leave the motorway
    [1, 1, 'exit'],     // motorway -> motorway: an interchange is an exit
    [3, 11, 'exit'],    // trunk -> residential
    [1, 3, 'exit'],     // motorway -> trunk: leaving it
    [7, 1, 'on'],       // secondary -> motorway: an on-ramp
    [11, 3, 'on'],      // residential -> trunk
    [3, 1, 'on'],       // trunk -> motorway: joining the motorway
    [1, 0, 'exit'],     // off a motorway, the route ends on the link
    [0, 1, 'on'],       // onto a motorway, nothing known before
    [5, 5, ''],         // primary_link between primaries: a slip road in town
    [9, 5, ''],         // tertiary -> primary
    [13, 9, ''],        // unclassified -> tertiary (a tertiary_link)
    [5, 7, ''],
    [0, 0, ''],         // unknown both sides (a graph without classes)
    [16, 1, 'on'],      // a path before is no road: only the motorway counts
    [1, 16, 'exit'],
  ];
  for (const [a, b, want] of cases) assert.strictEqual(szLinkKind(a, b), want, `${a} -> ${b}`);
  for (const lang of ALL) for (const k of ['drive.exit_ramp', 'drive.exit_ramp_onto', 'drive.on_ramp', 'drive.on_ramp_onto'])
    assert.ok(TABLES[lang][k], `${lang} ${k}`);
  assert.strictEqual(DE['drive.exit_ramp'], 'Ausfahrt nehmen');
  assert.strictEqual(DE['drive.on_ramp_onto'], 'Auffahrt auf {road}');
});

// The HUD's code out of 600-driving-mode.js, run on a synthetic route:
// augmentRouteForDriving turns the roads into turns, szLinkCue picks the line.
function hudCode() {
  const src = fs.readFileSync(`${REPO}/resources/viewer/src/index/600-driving-mode.js`, 'utf8');
  const take = (name) => {
    const m = new RegExp('\\n  function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n  \\}').exec(src);
    assert.ok(m, name); return m[0];
  };
  return ['haversineMeters', 'augmentRouteForDriving', 'szLinkKind', 'szLinkCue'].map(take).join('\n');
}
// roads: [name, flags, cls0, cls1, distM]; coords along a meridian, 1 m apart.
function syntheticRoute(roads) {
  const total = roads.reduce((t, r) => t + r[4], 0);
  const coords = []; for (let i = 0; i <= total; i += 10) coords.push([23.7, 37.9 + i / 111320]);
  return { coords, distance: total, time: total / 10,
           roads: roads.map(([n, flags, cls0, cls1, distM], i) => ({ nameIdx: i, name: n, flags, cls0, cls1, distM })) };
}

await ok('the HUD on a synthetic route: exits, on-ramps, slip roads in town, an end on the link', () => {
  const run = (lang) => new Function('document', 'window', RUNTIME + hudCode() + `
    return { szSetUiLanguage: szSetUiLanguage, augment: augmentRouteForDriving, cue: szLinkCue };`)(
    { documentElement: {}, getElementById: () => ({ textContent: INDEX_TABLES }) }, {});
  const en = run('en'); en.szSetUiLanguage('en', '');
  const cueAt = (h, route, i, name) => {
    const r = h.augment(syntheticRoute(route));
    return h.cue(r.turns[i], r.turns, name);
  };
  // motorway (1, ending in class 1) -> motorway_link (2) -> secondary (7): an exit
  const exit = [['A8', 0, 1, 1, 400], ['', 2, 2, 2, 120], ['Main St', 0, 7, 7, 300]];
  let r = en.augment(syntheticRoute(exit));
  assert.deepStrictEqual(r.turns.map((t) => [t.flags, t.fromFlags, t.toCls, t.fromCls]), [[2, 0, 2, 1], [0, 2, 7, 2]]);
  assert.deepStrictEqual(r.turns.map((t) => t.distM), [400, 520]);
  assert.strictEqual(en.cue(r.turns[0], r.turns, 'Main St'), 'take exit toward Main St');
  assert.strictEqual(en.cue(r.turns[0], r.turns, ''), 'take exit');
  // a road that changes class keeps both ends: residential (11) that becomes
  // secondary (7) before the on-ramp; the class where it ends counts.
  assert.strictEqual(cueAt(en, [['Elm', 0, 11, 7, 300], ['', 2, 2, 2, 100], ['E75', 0, 1, 1, 500]], 0, 'E75'), 'take ramp onto E75');
  assert.strictEqual(cueAt(en, [['Elm', 0, 11, 7, 300], ['', 2, 2, 2, 100], ['E75', 0, 1, 1, 500]], 0, ''), 'take ramp');
  // two links in a row: the class after both counts (motorway -> link -> link -> primary)
  assert.strictEqual(cueAt(en, [['A1', 0, 1, 1, 300], ['', 2, 2, 2, 80], ['X', 2, 2, 2, 80], ['B', 0, 5, 5, 200]], 0, 'B'), 'take exit toward B');
  // a slip road in town and a tertiary_link: no exit / ramp wording
  assert.strictEqual(cueAt(en, [['Amalias', 0, 5, 5, 300], ['', 2, 6, 6, 60], ['Syngrou', 0, 5, 5, 300]], 0, 'Syngrou'), '');
  assert.strictEqual(cueAt(en, [['Odos', 0, 13, 13, 200], ['', 2, 10, 10, 50], ['Leof', 0, 9, 9, 200]], 0, 'Leof'), '');
  // the class after the link is the first road past it, not an earlier one:
  // residential -> primary -> link -> motorway is an on-ramp at the link
  assert.strictEqual(cueAt(en, [['Res', 0, 11, 11, 200], ['Prim', 0, 5, 5, 300], ['', 2, 2, 2, 80], ['A1', 0, 1, 1, 400]], 1, 'A1'), 'take ramp onto A1');
  // the road before counts where it ends: a motorway that becomes a primary,
  // then a link to a secondary, is a slip road in town
  assert.strictEqual(cueAt(en, [['A', 0, 1, 5, 400], ['', 2, 6, 6, 60], ['B', 0, 7, 7, 300]], 0, 'B'), '');
  // the route ends on the link off a motorway: an exit
  assert.strictEqual(cueAt(en, [['A8', 0, 1, 1, 400], ['', 2, 2, 2, 120]], 0, ''), 'take exit');
  // German
  const de = run('de'); de.szSetUiLanguage('de', '');
  assert.strictEqual(cueAt(de, exit, 0, 'Hauptstraße'), 'Ausfahrt Richtung Hauptstraße');
  assert.strictEqual(cueAt(de, [['L1', 0, 7, 7, 300], ['', 2, 2, 2, 100], ['A5', 0, 1, 1, 500]], 0, 'A5'), 'Auffahrt auf A5');
  // the HUD uses it, and falls back to the ordinary cue when it says ''
  const src = fs.readFileSync(`${REPO}/resources/viewer/src/index/600-driving-mode.js`, 'utf8');
  assert.match(src, /\} else if \(enteringLink && \(linkCue = szLinkCue\(nextTurn, state\.route\.turns, name\)\)\) \{\n[^\n]*\n[^\n]*\n\s+streetEl\.textContent = linkCue;/);
});

// The route engines' road building, run: the worker's (with its leg join)
// and the main thread's (530, shared with 540).
function engineCode(file, names) {
  const src = fs.readFileSync(`${REPO}/resources/viewer/${file}`, 'utf8');
  return names.map((name) => {
    // up to the closing brace at the function's own indentation
    const m = new RegExp('\\n( *)function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n\\1\\}').exec(src);
    assert.ok(m, file + ' ' + name); return m[0];
  }).join('\n');
}

await ok('route engines: segments carry flags and class, roads their classes at both ends, legs join', () => {
  for (const [file, names] of [['routing-worker.js', ['szRouteSegment', 'szRoadsFromSegments', 'concatenateLegs']],
                               ['src/index/530-routing-astar.js', ['szRouteSegment', 'szRoadsFromSegments']]]) {
    const e = new Function(engineCode(file, names) + `; return { seg: szRouteSegment, roads: szRoadsFromSegments,
      legs: typeof concatenateLegs === 'function' ? concatenateLegs : null };`)();
    // class_access: ordinal in bits 0..4, roundabout bit 8, oneway bit 7 and others ignored
    assert.deepStrictEqual(e.seg(7, 12.5, 2 | (1 << 7) | (1 << 5)), { nameIdx: 7, distM: 12.5, flags: 2, cls: 2 }, file);
    assert.deepStrictEqual(e.seg(3, 4, 5 | (1 << 8)), { nameIdx: 3, distM: 4, flags: 1, cls: 5 }, file);
    assert.deepStrictEqual(e.seg(3, 4, 11), { nameIdx: 3, distM: 4, flags: 0, cls: 11 }, file);
    assert.deepStrictEqual(e.seg(3, 4, 0), { nameIdx: 3, distM: 4, flags: 0, cls: 0 }, file);   // graphs before v4
    for (const c of [6, 8, 10]) assert.strictEqual(e.seg(0, 1, c).flags, 2, file + ' link ' + c);
    // segments end to start: A(11) A(7) [link 2] B(1) -> roads A(11..7), link, B
    const segRev = [e.seg(2, 50, 1), e.seg(1, 20, 2), e.seg(0, 30, 7), e.seg(0, 40, 11)];
    const roads = e.roads(segRev, file === 'routing-worker.js' ? (i) => 'n' + i : undefined);
    assert.deepStrictEqual(roads.map((r) => [r.nameIdx, r.distM, r.flags, r.cls0, r.cls1]),
                           [[0, 70, 0, 11, 7], [1, 20, 2, 2, 2], [2, 50, 0, 1, 1]], file);
    if (file === 'routing-worker.js') {
      assert.deepStrictEqual(roads.map((r) => r.name), ['n0', 'n1', 'n2']);
      // A long route's legs: the join road merges and keeps the class it ends with.
      const leg = (rs) => ({ coords: [[0, 0], [1, 1]], distance: 1, time: 1, roads: rs });
      const j = e.legs([leg([{ nameIdx: 0, name: 'A', distM: 5, flags: 0, cls0: 11, cls1: 9 }]),
                        leg([{ nameIdx: 0, name: 'A', distM: 6, flags: 0, cls0: 9, cls1: 7 },
                             { nameIdx: 4, name: 'L', distM: 2, flags: 2, cls0: 2, cls1: 2 }])]);
      assert.deepStrictEqual(j.roads.map((r) => [r.nameIdx, r.distM, r.flags, r.cls0, r.cls1]),
                             [[0, 11, 0, 11, 7], [4, 2, 2, 2, 2]]);
    }
  }
  // Every engine builds its route through these.
  const w = fs.readFileSync(`${REPO}/resources/viewer/routing-worker.js`, 'utf8');
  assert.match(w, /segRev\.push\(szRouteSegment\(nameIdx, distM, classAccess\)\);/);
  assert.match(w, /var roads = szRoadsFromSegments\(segRev, function\(i\) \{ return graph\.getName\(i\); \}\);/);
  const m = fs.readFileSync(`${REPO}/resources/viewer/src/index/530-routing-astar.js`, 'utf8');
  assert.match(m, /segRev\.push\(szRouteSegment\(nameIdx, distM, classAccess\)\);/);
  assert.match(m, /var roads = szRoadsFromSegments\(segRev\);/);
  const b = fs.readFileSync(`${REPO}/resources/viewer/src/index/540-routing-worker-bridge.js`, 'utf8');
  assert.match(b, /segRev\.push\(szRouteSegment\(nameIdx, distM, graph\.edgeClassAccess\(ei\)\)\);/);
  assert.match(b, /var roads = szRoadsFromSegments\(segRev\);/);
});

console.log(`${pass} passed`);
