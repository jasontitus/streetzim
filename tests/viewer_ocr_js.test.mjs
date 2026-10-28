// Search with a photo (docs/ocr.md): how the sign reader's text lines are
// put together from the detector's boxes (the sign-lines block of
// 305-ocr-search.js) and matched against search records (the sign-match
// block of 300-search.js), run as they ship.
//
//   node tests/viewer_ocr_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const PARTS_DIR = `${REPO}/resources/viewer/src/index`;
const HTML = fs.readdirSync(PARTS_DIR).filter((n) => /^\d{3}-[\w-]+\.(html|js)$/.test(n)).sort()
  .map((n) => fs.readFileSync(`${PARTS_DIR}/${n}`, 'utf8')).join('');

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
const M = new Function(
  slice(HTML, '// BEGIN admin-search', '// END admin-search') +
  slice(HTML, '// BEGIN sign-match', '// END sign-match') +
  '\nreturn { _szSignText, _szEditsWithin, _szSignSimilarity, _szSignScore, _szSignLines, _szBigrams, SZ_SIGN_MIN };')();
const L = new Function(slice(HTML, '// BEGIN sign-lines', '// END sign-lines') +
  slice(HTML, '// BEGIN sign-env', '// END sign-env') +
  '\nreturn { _szSignLineGroups, _szSignKiwixApp };')();
// The viewer's normalizeText (300-search.js): SEARCH_SHARDS.fold.
const norm = new Function(slice(HTML, '// BEGIN search-shards', '// END search-shards') +
  '\nreturn SEARCH_SHARDS.fold;')();

function score(rec, texts) {
  const s = M._szSignScore(rec, M._szSignLines(texts, norm, 12), norm);
  return s ? s.sim : 0;
}
const matches = (rec, texts) => score(rec, texts) >= M.SZ_SIGN_MIN;

ok('a sign is compared folded, without spaces, Arabic and Persian letters alike', () => {
  assert.strictEqual(M._szSignText('Königstraße 12', norm), 'konigstraße12');
  assert.strictEqual(M._szSignText('خيابان وليعصر', norm), M._szSignText('خیابان ولیعصر', norm));
  assert.strictEqual(M._szSignText('Ƙofar Mata', norm), 'kofarmata');
  assert.strictEqual(M._szSignText('王府井 大街', norm), '王府井大街');
});

ok('edits within: the fewest edits that find a name in a line', () => {
  assert.strictEqual(M._szEditsWithin('abc', 'xxabcxx'), 0);
  assert.strictEqual(M._szEditsWithin('abc', 'xxabxx'), 1);
  assert.strictEqual(M._szEditsWithin('abcd', 'ab'), 2);
});

ok('the name read exactly on a cluttered sign matches fully', () => {
  assert.strictEqual(score({ n: 'Königstraße', t: 'street' }, ['OPEN', 'Königstraße', 'No Parking']), 1);
  assert.strictEqual(score({ n: 'Peking University', nn: '北京大学', t: 'poi' }, ['北京大学']), 1);
});

ok('letters a recognizer drops still match (Vietnamese, Hausa, Persian)', () => {
  // PP-OCRv6 has no ờ, ễ, ệ: "Đường Nguyễn Huệ" comes out "Đưng Nguyn Hu".
  assert.ok(matches({ n: 'Đường Nguyễn Huệ', t: 'street' }, ['Đưng Nguyn Hu']));
  assert.ok(matches({ n: 'Ƙofar Mata', t: 'place' }, ['Kofar Mata']));
  assert.ok(matches({ n: 'Vali-e Asr Street', nn: 'خیابان ولیعصر', t: 'street' }, ['خيابان وليعصر']));
});

ok('Arabic words given in the wrong order still match', () => {
  assert.ok(matches({ n: 'Tahlia Street', nn: 'شارع التحلية', t: 'street' }, ['التحلية شارع']));
});

ok('other names do not match: short ones need exact text, long ones most of it', () => {
  assert.ok(!matches({ n: 'Open Air Museum', t: 'poi' }, ['OPEN', 'No Parking']));
  assert.ok(!matches({ n: 'Rue', t: 'street' }, ['Ruo de la Paix']));
  assert.ok(!matches({ n: 'Königsallee', t: 'street' }, ['Königstraße']));
  assert.ok(!matches({ n: 'Shibuya Station', nn: '渋谷駅', t: 'poi' }, ['新宿御苑']));
});

ok('numbers must be read exactly', () => {
  assert.ok(matches({ n: 'Road 719', nn: 'طريق 719', t: 'street' }, ['طريق 719']));
  assert.ok(!matches({ n: 'Road 717', nn: 'طريق 717', t: 'street' }, ['طريق 719']));
  assert.ok(!matches({ n: 'Road 7', nn: 'طريق 7', t: 'street' }, ['طريق 719']));
  assert.ok(!matches({ n: '5th Avenue', t: 'street' }, ['6th Avenue']));
});

ok('a name in another language counts (ln)', () => {
  assert.ok(matches({ n: 'Geneva', ln: { de: 'Genf', ru: 'Женева' }, t: 'place' }, ['Женева']));
});

ok('the sign\'s big text outweighs its small print', () => {
  const lines = M._szSignLines([{ text: 'Monaco', weight: 0.7 }, { text: 'Rue Paradis', weight: 1 }], norm, 12);
  assert.deepStrictEqual(lines.map((l) => l.text), ['rueparadis', 'monaco']);
  assert.deepStrictEqual(M._szSignScore({ n: 'Monaco', t: 'admin' }, lines, norm),
                         { sim: 1, weight: 0.7, cover: 1, line: 'Monaco' });
  assert.deepStrictEqual(M._szSignScore({ n: 'Rue Paradis', t: 'street' }, lines, norm),
                         { sim: 1, weight: 1, cover: 1, line: 'Rue Paradis' });
});

ok('a name covering the whole line before one covering part of it; short names are whole words', () => {
  const lines = M._szSignLines(['Manama Central Market'], norm, 12);
  const market = M._szSignScore({ n: 'Manama Central Market', t: 'poi' }, lines, norm);
  const city = M._szSignScore({ n: 'Manama', t: 'place' }, lines, norm);
  assert.strictEqual(market.cover, 1);
  assert.ok(city.cover < 0.4, city.cover);
  // "mana-MACE-ntral" holds it, but it is not a word of the line
  assert.strictEqual(M._szSignScore({ n: 'Mace', t: 'poi' }, lines, norm), null);
  // a line without spaces (Chinese) has no words to check: a short name in it counts
  assert.ok(M._szSignScore({ n: 'Wangfujing', nn: '王府井', t: 'place' }, M._szSignLines(['王府井大街'], norm, 12), norm));
});

ok('lines: at least two letters, no repeats, longest first, capped', () => {
  const ls = M._szSignLines(['A', 'OPEN', 'open', 'Bahnhofstraße'], norm, 12);
  assert.deepStrictEqual(ls.map((l) => l.text), ['bahnhofstraße', 'open']);
  assert.strictEqual(M._szSignLines(['aa', 'bb', 'cc'], norm, 2).length, 2);
});

ok('boxes on one line join left to right; lines go top to bottom', () => {
  const lines = L._szSignLineGroups([
    { text: 'Gate', box: { x: 120, y: 50, width: 60, height: 30 }, script: 'latn' },
    { text: 'Brandenburg', box: { x: 10, y: 48, width: 100, height: 32 }, script: 'latn' },
    { text: 'Pariser Platz', box: { x: 10, y: 120, width: 140, height: 20 }, script: 'latn' },
  ]);
  assert.deepStrictEqual(lines.map((l) => l.text), ['Brandenburg Gate', 'Pariser Platz']);
  assert.deepStrictEqual(lines[0].box, { x: 10, y: 48, width: 170, height: 32 });
});

ok('Arabic boxes join right to left; Chinese and Thai words without a space', () => {
  const ar = L._szSignLineGroups([
    { text: 'الملك', box: { x: 100, y: 10, width: 50, height: 20 }, script: 'Arab' },
    { text: 'شارع', box: { x: 160, y: 10, width: 40, height: 20 }, script: 'Arab' },
  ]);
  assert.strictEqual(ar[0].text, 'شارع الملك');
  const zh = L._szSignLineGroups([
    { text: '大街', box: { x: 60, y: 10, width: 40, height: 20 }, script: 'Hani' },
    { text: '王府井', box: { x: 0, y: 10, width: 55, height: 20 }, script: 'Hani' },
  ]);
  assert.strictEqual(zh[0].text, '王府井大街');
  const th = L._szSignLineGroups([
    { text: 'ถนน', box: { x: 0, y: 10, width: 40, height: 20 }, script: 'Thai' },
    { text: 'สีลม', box: { x: 45, y: 10, width: 40, height: 20 }, script: 'Thai' },
  ]);
  assert.strictEqual(th[0].text, 'ถนนสีลม');
});

ok('boxes without text or size are skipped', () => {
  assert.deepStrictEqual(L._szSignLineGroups([{ text: '', box: { x: 0, y: 0, width: 1, height: 1 } },
                                              { text: 'x', box: { x: 0, y: 0, width: 1, height: 0 } }]), []);
});

ok('Kiwix\'s Apple apps are told apart from browsers and from each other', () => {
  const iphone = { userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148' };
  const ipad = { userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15', platform: 'MacIntel', maxTouchPoints: 5 };
  const mac = { userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15', platform: 'MacIntel', maxTouchPoints: 0 };
  const linux = { userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 QtWebEngine/6.5.3 Chrome/108.0 Safari/537.36' };
  assert.strictEqual(L._szSignKiwixApp('zim:', iphone), 'ios');     // camera: no permission
  assert.strictEqual(L._szSignKiwixApp('zim:', ipad), 'ios');
  assert.strictEqual(L._szSignKiwixApp('zim:', mac), 'mac');        // no file picker at all
  assert.strictEqual(L._szSignKiwixApp('zim:', linux), '');         // Kiwix desktop: Qt's picker works
  assert.strictEqual(L._szSignKiwixApp('https:', iphone), '');      // Safari, the /drive/ PWA
  assert.strictEqual(L._szSignKiwixApp('http:', mac), '');          // kiwix-serve in Safari
});


const T = new Function(slice(HTML, '// BEGIN sign-load', '// END sign-load') + '\nreturn { _szInTurn };')();

await (async () => {
  const name = "the reader's files load one at a time, results in order";
  try {
    let open = 0, most = 0;
    const log = [];
    const job = (v, ms) => () => {
      open++; most = Math.max(most, open); log.push('start ' + v);
      return new Promise((resolve) => setTimeout(() => { open--; log.push('end ' + v); resolve(v); }, ms));
    };
    const got = await T._szInTurn([job('a', 30), job('b', 5), job('c', 15)]);
    assert.deepStrictEqual(got, ['a', 'b', 'c']);
    assert.strictEqual(most, 1);
    assert.deepStrictEqual(log, ['start a', 'end a', 'start b', 'end b', 'start c', 'end c']);
    assert.deepStrictEqual(await T._szInTurn([]), []);
    // A failure stops the rest and rejects.
    let ran = false;
    await assert.rejects(T._szInTurn([() => Promise.reject(new Error('404')), () => { ran = true; return 1; }]), /404/);
    assert.strictEqual(ran, false);
    console.log('ok   ', name); pass++;
  } catch (e) { console.error('FAIL ', name, '\n      ', e.stack || e.message); process.exitCode = 1; }
})();
console.log(`${pass} passed`);
