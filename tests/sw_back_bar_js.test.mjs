// The PWA's "Back to map" bar on Wikipedia articles (web/drive/sw.js,
// withBackToMap): in the map's language when the viewer has a UI table for
// it, English (exactly the old bar) otherwise. The full data path is in
// tests/test_sw_streaming.mjs (a browser); this runs the functions alone.
//
//   node tests/sw_back_bar_js.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const SW = fs.readFileSync(`${REPO}/web/drive/sw.js`, 'utf8');
const from = SW.indexOf("// The bar's words in the map's language");
const to = SW.indexOf('// Entries at least this big');
assert.ok(from > 0 && to > from, 'the back-bar code is not where this test looks for it');
const api = new Function(SW.slice(from, to) +
  '\nreturn { BACK_TO_MAP, backToMapText, backBarHtml, withBackToMap, mapLanguage };')();

let pass = 0;
async function ok(name, fn) {
  try { await fn(); console.log('ok   ', name); pass++; }
  catch (e) { console.error('FAIL ', name, '\n      ', e.stack || e.message); process.exitCode = 1; }
}
const enc = (s) => new TextEncoder().encode(s);
const dec = (b) => new TextDecoder().decode(b);
const run = (html, lang) => dec(api.withBackToMap(enc(html), lang));

// The bar sw.js added before it knew any language.
const OLD_BAR = '<nav class="sz-back"><a href="/drive/viewer/" ' +
  'onclick="if(history.length>1){history.back();return false}">' +
  '&#8592; Back to map</a></nav>';
const BARE = '<html><head><title>Foo</title></head><body><p>x</p></body></html>';
const BUILT = '<html><head></head><body><nav class="sz-back"><a href="../../../index.html#map=12/47.1/9.5" ' +
  'onclick="x">&#8592; Back to map</a></nav><p>Pack body.</p></body></html>';

await ok('English, no language, or one without a table: the old bar, byte for byte', () => {
  for (const lang of [undefined, '', 'en', 'EN', 'sw', 'qqx', 42]) {
    assert.strictEqual(api.backBarHtml(lang), OLD_BAR, String(lang));
    const out = run(BARE, lang);
    assert.ok(out.includes('<body>' + OLD_BAR + '<p>x</p>'), String(lang));
    assert.strictEqual(run(BUILT, lang), BUILT.replace('../../../index.html', '/drive/viewer/'), String(lang));
  }
});

await ok('a map in a language with a UI table: the bar in that language, added or rewritten', () => {
  const want = { de: '&#8592; Zurück zur Karte', th: '&#8592; กลับไปที่แผนที่', zh: '&#8592; 返回地图',
                 es: '&#8592; Volver al mapa', fr: '&#8592; Retour à la carte', ja: '&#8592; 地図に戻る',
                 ko: '&#8592; 지도로 돌아가기', el: '&#8592; Επιστροφή στον χάρτη' };
  for (const [lang, words] of Object.entries(want)) {
    for (const l of [lang, lang.toUpperCase(), lang + '-x']) {
      assert.strictEqual(api.backToMapText(l), words, l);
      assert.ok(run(BARE, l).includes('return false}">' + words + '</a></nav><p>x</p>'), l);
      const built = run(BUILT, l);
      assert.ok(built.includes('<a href="/drive/viewer/#map=12/47.1/9.5" onclick="x">' + words + '</a>'), built);
      assert.ok(!built.includes('Back to map'), l);
    }
  }
  assert.strictEqual(api.backToMapText('zh-Hans'), want.zh);
});

await ok('right-to-left languages: the words in their direction, the arrow pointing right', () => {
  for (const lang of ['ar', 'fa', 'he']) {
    for (const l of [lang, lang.toUpperCase(), lang + '-x']) {
      const words = api.backToMapText(l);
      assert.strictEqual(words, '<span dir="rtl">&#8594; ' + api.BACK_TO_MAP[lang] + '</span>', l);
      assert.ok(run(BUILT, l).includes('onclick="x">' + words + '</a>'), l);
    }
  }
  assert.ok(api.backToMapText('it').startsWith('&#8592; '));
});

await ok('Romansh and Swiss German maps get the German bar, as the viewer falls back', () => {
  for (const l of ['rm', 'rm-CH', 'gsw']) assert.strictEqual(api.backToMapText(l), '&#8592; ' + api.BACK_TO_MAP.de, l);
});

await ok('a bar that does not say "Back to map" is left as it is', () => {
  const other = BUILT.replace('&#8592; Back to map', 'Karte');
  assert.ok(run(other, 'de').includes('onclick="x">Karte</a>'));
});

await ok('one bar entry per viewer UI table', () => {
  const tables = fs.readdirSync(`${REPO}/resources/viewer/i18n`)
    .filter((n) => /^[a-z]{2,3}(-[a-z0-9]+)?\.json$/.test(n) && n !== 'en.json').map((n) => n.slice(0, -5)).sort();
  assert.ok(tables.length >= 3, tables.join());
  assert.deepStrictEqual(Object.keys(api.BACK_TO_MAP).sort(), tables);
  for (const t of Object.values(api.BACK_TO_MAP)) assert.doesNotMatch(t, /[<>&"]/);
});

await ok('the map language is read once per ZIM; none when it cannot be read', async () => {
  let reads = 0;
  const reader = (cfg) => ({
    findEntry: async (p) => (p === 'map-config.json' && cfg !== null ? { p } : null),
    readEntry: async () => { reads++; return { data: enc(cfg) }; },
  });
  const r = reader('{"language":"de"}');
  assert.strictEqual(await api.mapLanguage(r), 'de');
  assert.strictEqual(await api.mapLanguage(r), 'de');
  assert.strictEqual(reads, 1);
  assert.strictEqual(await api.mapLanguage(reader('{"name":"x"}')), '');
  assert.strictEqual(await api.mapLanguage(reader('not json')), '');
  assert.strictEqual(await api.mapLanguage(reader(null)), '');
  assert.strictEqual(await api.mapLanguage(reader('{"language":7}')), '');
  // Articles get it.
  assert.match(SW, /okResponse\(withBackToMap\(got\.data, await mapLanguage\(reader\)\), got\.mime\)/);
});

console.log(`\n${pass} passed`);
