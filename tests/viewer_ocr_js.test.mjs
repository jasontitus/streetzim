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
  '\nreturn { _szSignText, _szSignWords, _szEditsWithin, _szSignSimilarity, _szSignScore, _szSignLines, _szBigrams, SZ_SIGN_MIN };')();
const L = new Function(slice(HTML, '// BEGIN sign-lines', '// END sign-lines') +
  slice(HTML, '// BEGIN sign-env', '// END sign-env') +
  '\nreturn { _szSignLineGroups, _szSignMerge, _szSignCompose, _szSignDigits, _szSignAddress, _szSignKiwixApp, _szSignKinds, _szSignIsAddress, _szSignPlate };')();
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

// What a photo's results are (_szSignKinds): never more certain than they
// are. Whole words against the lines as read, not the search's fuzzy match.
const W = (x) => M._szSignWords(x, norm);
const kinds = (recs, lines, all) => L._szSignKinds(recs.map((item) => ({ item })), lines, W, all);
const plate = (lines) => L._szSignPlate(lines, W);
const isAddr = (n, lines) => L._szSignIsAddress({ n, t: 'addr' }, plate(lines), W);
const road116 = (a, o) => ({ n: 'Road 116', nn: 'طريق 116', t: 'street', a, o });
ok('words: folded whole words, numbers apart from letters, Arabic-Indic digits as 0-9', () => {
  assert.deepStrictEqual(W('طريق ١١٦'), W('طریق 116'));    // Arabic and Persian yeh alike, as the matcher
  assert.deepStrictEqual(W('طريق ١١٦').slice(1), ['116']);
  assert.deepStrictEqual(W('10號 忠孝東路'), ['10', '號', '忠孝東路']);
  assert.deepStrictEqual(W('Road 105 702'), ['road', '105', '702']);
});
ok('a plate whose building the map lacks: the street only, and which of two the sign does not say', () => {
  const k = kinds([road116(26.22885, 50.57805), road116(26.17603, 50.45317)],
    [{ text: '366', size: 120 }, { text: '116 طريق', size: 100 }]);
  assert.deepStrictEqual(k[0], { kind: 'street', building: '366', found: false, several: true });
});
ok('an address is the plate\'s building number first, then its street; never another building', () => {
  const p702 = [{ text: '702', size: 2 }, { text: 'طريق 105', size: 1 }];
  assert.strictEqual(isAddr('105 702', p702), false);          // building 105 on Road 702
  assert.strictEqual(isAddr('702 105', p702), true);
  assert.strictEqual(isAddr('702 طريق 105', p702), true);
  assert.strictEqual(isAddr('702 105 Road', p702), true);
  assert.strictEqual(isAddr('702', p702), false);
  assert.strictEqual(isAddr('702 1050', p702), false);
  assert.strictEqual(isAddr('702 105 Hamad Town', p702), false);  // words that are not the street's
  // one line of the sign word for word: a German house sign
  assert.strictEqual(isAddr('10 Schulweg', [{ text: 'Schulweg 10' }]), true);
  assert.strictEqual(isAddr('10 Schulweg, Husum', [{ text: 'Schulweg 10' }]), true);
  assert.strictEqual(isAddr('12 Schulweg', [{ text: 'Schulweg 10' }]), false);
  // a speed limit beside an unnumbered street is no building
  assert.strictEqual(plate([{ text: '50' }, { text: 'Königstraße' }]).building, '');
  assert.deepStrictEqual(kinds([{ n: 'Königstraße', t: 'street', a: 1, o: 1 }], [{ text: '50' }, { text: 'Königstraße' }])[0],
    { kind: 'street', building: '', found: false, several: false });
});
ok('an address found: it is the address, and the street is only the street', () => {
  const lines = [{ text: '366', size: 2 }, { text: '116 طريق', size: 1 }];
  const k = kinds([road116(26.2288, 50.578), { n: '366 116', t: 'addr', a: 26.2289, o: 50.5781 }], lines);
  assert.deepStrictEqual(k.map((x) => x.kind), ['street', 'address']);
  assert.strictEqual(k[0].found, true);
});
ok('part of a line, a glued line or a bare number is only similar, never the sign\'s name', () => {
  assert.deepStrictEqual(kinds([{ n: 'Grand Mosque', t: 'poi', a: 1, o: 1 }], [{ text: 'Al Noor Grand Mosque' }]).map((x) => x.kind), ['similar']);
  assert.deepStrictEqual(kinds([{ n: '107', t: 'poi', a: 1, o: 1 }], [{ text: '107' }, { text: 'طريق 116' }]).map((x) => x.kind), ['similar']);
  assert.deepStrictEqual(kinds([{ n: 'Road 1012', nn: 'طريق 1012', t: 'street', a: 1, o: 1 }],
    [{ text: '2' }, { text: '101 طريق' }]).map((x) => x.kind), ['similar']);
  assert.deepStrictEqual(kinds([{ n: '366 727', t: 'addr', a: 1, o: 1 }], [{ text: '366' }, { text: '116 طريق' }]).map((x) => x.kind), ['similar']);
});
ok('a town is an area; a shop whose name is a line of the sign is the name', () => {
  const k = kinds([{ n: 'Bani Jamrah', nn: 'بني جمرة', t: 'place', a: 26.2, o: 50.45 },
    { n: "Hardee's", t: 'poi', a: 30.06, o: 31.22 }, { n: 'Hardees Express', t: 'poi', a: 30.06, o: 31.22 }],
  [{ text: 'بني جمرة' }, { text: 'Hardee’s' }]);
  assert.deepStrictEqual(k.map((x) => x.kind), ['area', 'name', 'similar']);
});
ok('the same name elsewhere: 150 m apart for a place, 2 km for a street, by any of its names', () => {
  const shop = (a, o, n = 'Starbucks') => ({ n, t: 'poi', a, o });
  assert.strictEqual(kinds([shop(26.21, 50.58)], [{ text: 'STARBUCKS' }], [shop(26.21, 50.58), shop(26.228, 50.58)])[0].several, true);
  assert.strictEqual(kinds([shop(26.21, 50.58)], [{ text: 'STARBUCKS' }], [shop(26.21, 50.58), shop(26.2105, 50.5803)])[0].several, false);
  assert.strictEqual(kinds([road116(26.22885, 50.57805)], [], [road116(26.22885, 50.57805), road116(26.2300, 50.5800)])[0].several, false);
  const ave = { n: 'Avenue 53', nn: 'شارع 53', t: 'street', a: 26.2, o: 50.5 };
  assert.strictEqual(kinds([ave], [{ text: 'شارع 53' }], [ave, { n: 'Ave 53', nn: 'شارع 53', t: 'street', a: 26.0, o: 50.5 }])[0].several, true);
  assert.strictEqual(kinds([shop(0, 179.9998)], [], [shop(0, 179.9998), shop(0, -179.9998)])[0].several, false);  // 44 m, across the antimeridian
  assert.deepStrictEqual(L._szSignKinds([], [{ text: '366' }], W), []);
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

ok('a vertical line is a line of its own, sized by its letters (a Tainan house-number plate)', () => {
  const lines = L._szSignLineGroups([
    { text: '84', box: { x: 330, y: 275, width: 475, height: 365 }, script: 'Hani' },
    { text: '安平區', box: { x: 80, y: 60, width: 140, height: 540 }, script: 'Hani' },
    { text: '延平街', box: { x: 240, y: 62, width: 560, height: 175 }, script: 'Hani' },
  ]);
  assert.deepStrictEqual(lines.map((l) => l.text), ['安平區', '延平街', '84']);
  assert.deepStrictEqual(lines.map((l) => l.size), [140, 175, 365]);
  // one tall letter is no vertical line: it joins the line beside it
  const one = L._szSignLineGroups([
    { text: '街', box: { x: 0, y: 0, width: 20, height: 40 }, script: 'Hani' },
    { text: '延平', box: { x: 25, y: 5, width: 50, height: 30 }, script: 'Hani' },
  ]);
  assert.deepStrictEqual(one.map((l) => l.text), ['街延平']);
});

ok('the coarse reading adds what the fine one missed, in the photo\'s scale, once', () => {
  const fine = [{ text: '84', box: { x: 330, y: 275, width: 475, height: 365 }, script: 'Hani' }];
  const coarse = [{ text: '84', box: { x: 75, y: 60, width: 110, height: 85 }, script: 'Hani' },
                  { text: '延平街', box: { x: 55, y: 14, width: 128, height: 40 }, script: 'Hani' }];
  const m = L._szSignMerge(fine, coarse, 4.4);
  assert.deepStrictEqual(m.map((it) => it.text), ['84', '延平街']);
  assert.deepStrictEqual(m[1].box, { x: 55 * 4.4, y: 14 * 4.4, width: 128 * 4.4, height: 40 * 4.4 });
  // the same text elsewhere on the photo is another line
  assert.strictEqual(L._szSignMerge(fine, [{ text: '84', box: { x: 0, y: 0, width: 10, height: 10 } }], 1).length, 2);
  // what the fine reading read, read again in another spelling, adds nothing
  const park = [{ text: 'No Parking', box: { x: 100, y: 500, width: 300, height: 40 }, script: 'Latn' }];
  assert.deepStrictEqual(L._szSignMerge(park, [{ text: 'NoParking', box: { x: 26, y: 124, width: 74, height: 11 } }], 4).map((it) => it.text),
                         ['No Parking']);
  assert.deepStrictEqual(L._szSignMerge(fine, null, 2), fine);
});

ok('a box read in parts reads as one model\'s words and the other\'s number (a Bahraini address plate)', () => {
  // the two recognizers on "2912 طريق" and "116 طريق" (Bahrain's map, 2026-10-29)
  const plate = (num, conf, arab, aconf) => ({ text: num, confidence: conf, script: 'Latn',
    readings: [{ text: num, confidence: conf, script: 'Latn' }, { text: arab, confidence: aconf, script: 'Arab' }] });
  assert.deepStrictEqual(L._szSignCompose(plate('2912', 0.996, 'طرئيق91 ', 0.652)), { text: 'طرئيق 2912', script: 'Arab' });
  assert.deepStrictEqual(L._szSignCompose(plate('116', 0.986, 'طريق1 ', 0.821)), { text: 'طريق 116', script: 'Arab' });
  // a reading with letters is kept as it is; so is a number nothing else read letters in
  assert.strictEqual(L._szSignCompose({ text: 'Road 9', confidence: 0.9, readings: [{ text: '9', confidence: 0.99 }] }).text, 'Road 9');
  assert.strictEqual(L._szSignCompose({ text: '460', confidence: 1, readings: [{ text: '460', confidence: 1 }] }).text, '460');
  // letters read with less than half the confidence are noise
  assert.strictEqual(L._szSignCompose(plate('84', 0.99, 'ض', 0.3)).text, '84');
  // a reading that already holds the number is taken as it is (the number first, or inside)
  assert.strictEqual(L._szSignCompose(plate('23', 0.99, '23號', 0.9)).text, '23號');
  assert.strictEqual(L._szSignCompose(plate('23', 0.99, '23번길', 0.9)).text, '23번길');
  assert.strictEqual(L._szSignCompose(plate('42', 0.99, '42nd St', 0.9)).text, '42nd St');
  const lines = L._szSignLineGroups([
    { text: '460', confidence: 1, box: { x: 140, y: 134, width: 300, height: 112 }, script: 'Latn', readings: [{ text: '460', confidence: 1, script: 'Latn' }] },
    { ...plate('2912', 0.996, 'طرئيق91 ', 0.652), box: { x: 68, y: 413, width: 580, height: 174 } }]);
  assert.deepStrictEqual(lines.map((l) => l.text), ['460', 'طرئيق 2912']);
  // small print inside the road's band (a stock photo's watermark) is a line of its own
  const marked = L._szSignLineGroups([
    { ...plate('2912', 0.996, 'طرئيق91 ', 0.652), box: { x: 68, y: 413, width: 580, height: 174 } },
    { text: 'a', confidence: 0.99, box: { x: 232, y: 425, width: 17, height: 17 }, script: 'Latn' },
    { text: 'alan', confidence: 0.99, box: { x: 567, y: 517, width: 73, height: 51 }, script: 'Latn' }]);
  assert.deepStrictEqual(marked.map((l) => l.text), ['طرئيق 2912', 'a', 'alan']);
  // a tilted line (each box lower than the last) stays one line; so does a number at 0.4 of the name
  const tilt = L._szSignLineGroups(['Avenida', 'Presidente', 'Juscelino', 'Kubitschek', 'de', 'Oliveira'].map((t, i) =>
    ({ text: t, confidence: 0.9, box: { x: i * 200, y: 100 + i * 80, width: 190, height: 175 }, script: 'Latn' })));
  assert.deepStrictEqual(tilt.map((l) => l.text), ['Avenida Presidente Juscelino Kubitschek de Oliveira']);
  const num = L._szSignLineGroups([{ text: 'Bahnhofstraße', confidence: 0.9, box: { x: 0, y: 0, width: 300, height: 100 }, script: 'Latn' },
                                   { text: '12', confidence: 0.9, box: { x: 310, y: 50, width: 40, height: 42 }, script: 'Latn' }]);
  assert.deepStrictEqual(num.map((l) => l.text), ['Bahnhofstraße 12']);
});

ok('a record written with Arabic-Indic digits matches the sign either way', () => {
  // Egypt's records: "جامعة ٦ اكتوبر"; the OCR may read the sign's ٦ as 6, or keep it
  assert.ok(matches({ n: 'جامعة ٦ اكتوبر' }, ['جامعة 6 اكتوبر']));
  assert.ok(matches({ n: 'جامعة ٦ اكتوبر' }, ['جامعة ٦ اكتوبر']));
  assert.ok(matches({ n: 'Road 116', nn: 'طريق 116' }, ['طريق ١١٦']));
  assert.ok(!matches({ n: 'جامعة ٦ اكتوبر' }, ['جامعة 7 اكتوبر']));
});

ok('Arabic-Indic and Persian digits read as 0-9', () => {
  assert.strictEqual(L._szSignDigits('طريق ١١٦'), 'طريق 116');
  assert.strictEqual(L._szSignDigits('خیابان ۱۲۳'), 'خیابان 123');
  assert.strictEqual(L._szSignCompose({ text: '٤٦٠', confidence: 1 }).text, '460');
});

ok('a building number beside a numbered street is also searched as an address', () => {
  const a = L._szSignAddress([{ text: '460', size: 112 }, { text: 'طريق 2912', size: 174 }, { text: 'alamy', size: 20 }], 174);
  assert.deepStrictEqual(a, [{ text: 'طريق 2912 460', weight: 1 }]);
  assert.deepStrictEqual(L._szSignAddress([{ text: '460', size: 1 }, { text: 'Gate', size: 1 }], 1), []);
  // The street read left to right (its number first): the building number
  // beside that number, so the line holds no "طريق 366", another road.
  const b = L._szSignAddress([{ text: '366', size: 120 }, { text: '116 طريق', size: 100 }], 120);
  assert.deepStrictEqual(b.map((x) => x.text), ['366 116 طريق']);
  const road366 = { n: 'Road 366', nn: 'طريق 366', t: 'street' };
  assert.ok(!matches(road366, ['366', '116 طريق', ...b.map((x) => x.text)]), 'Road 366 must not match Road 116\'s plate');
  assert.ok(matches({ n: 'Road 116', nn: 'طريق 116', t: 'street' }, ['366', '116 طريق', ...b.map((x) => x.text)]));
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

const P = new Function(slice(HTML, '// BEGIN sign-pick', '// END sign-pick') + '\nreturn { _szSignPickMode };')();
ok('the camera button offers the camera and the library on a phone, the library first in Kiwix on iOS', () => {
  assert.strictEqual(P._szSignPickMode('', true), 'both');        // a phone or tablet browser
  assert.strictEqual(P._szSignPickMode('', false), 'picker');     // a desktop: the file picker at once
  assert.strictEqual(P._szSignPickMode('ios', true), 'library');  // Take Photo closes the Kiwix app
  assert.strictEqual(P._szSignPickMode('ios', false), 'library');
  assert.strictEqual(P._szSignPickMode('mac', false), 'drop');    // no picker opens in Kiwix for macOS
  assert.strictEqual(P._szSignPickMode('mac', true), 'drop');
});
console.log(`${pass} passed`);
