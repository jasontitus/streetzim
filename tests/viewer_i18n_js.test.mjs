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

console.log(`${pass} passed`);
