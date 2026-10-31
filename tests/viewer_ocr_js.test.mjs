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
  '\nreturn { _szSignText, _szSignWords, _szEditsWithin, _szSignSimilarity, _szSignScore, _szSignLines, _szBigrams, SZ_SIGN_MIN, SZ_SIGN_DROP_TYPE, SZ_SIGN_LINE_TYPE };')();
const LN = HTML.slice(HTML.indexOf('function _szLnPairs('), HTML.indexOf('\n}\n', HTML.indexOf('function _szLnPairs(')) + 3);
const L = new Function(LN + slice(HTML, '// BEGIN sign-lines', '// END sign-lines') +
  slice(HTML, '// BEGIN sign-env', '// END sign-env') +
  '\nreturn { _szSignLineGroups, _szSignWeighted, _szSignMerge, _szSignCompose, _szSignDigits, _szSignAddress, _szSignKiwixApp, _szSignKinds, _szSignAddressKind, _szSignPlate, _szSignMayBeAddress, _szSignCyrillic };')();
// The photo's frame (sign-view): fit, bounds, what it shows, zoom, taps, parts.
const V = new Function(slice(HTML, '// BEGIN sign-view', '// END sign-view') +
  '\nreturn { _szSignFit, _szSignClamp, _szSignVisible, _szSignAt, _szSignZoomAt, _szSignHit, _szSignSubPart, _szSignIsWhole };')();
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
// are. Whole words in order against the lines as read, not the fuzzy match.
const W = (x) => M._szSignWords(x, norm);
const kinds = (recs, lines, all) => L._szSignKinds(recs.map((item) => ({ item })), lines, W, all);
const plate = (lines) => L._szSignPlate(lines, W);
const road116 = (a, o) => ({ n: 'Road 116', nn: 'طريق 116', t: 'street', a, o });
const addr = (n, a = 26.2, o = 50.5) => ({ n, t: 'addr', a, o });
const street = (n, nn) => ({ n, nn, t: 'street', a: 26.2, o: 50.5 });
// a plate: a number line over a street line, their boxes stacked
const stacked = (num, st) => [{ text: num, size: 100, box: { x: 100, y: 100, width: 300, height: 100 } },
                              { text: st, size: 60, box: { x: 90, y: 230, width: 320, height: 60 } }];
// lines one under another on one plate, each as tall as its size
const stack = (rows) => {
  let y = 0;
  return rows.map(([text, size]) => { const L = { text, size, box: { x: 100, y, width: 300, height: size } }; y += size + 20; return L; });
};
ok('words: folded whole words, numbers apart from letters, Arabic-Indic digits as 0-9', () => {
  assert.deepStrictEqual(W('طريق ١١٦'), W('طریق 116'));    // Arabic and Persian yeh alike, as the matcher
  assert.deepStrictEqual(W('طريق ١١٦').slice(1), ['116']);
  assert.deepStrictEqual(W('10號 忠孝東路'), ['10', '號', '忠孝東路']);
  assert.deepStrictEqual(W('Road 105 702'), ['road', '105', '702']);
});
ok('a plate whose building the map lacks: the street only, and which of two the sign does not say', () => {
  const k = kinds([road116(26.22885, 50.57805), road116(26.17603, 50.45317)], stack([['366', 120], ['116 طريق', 100]]));
  assert.deepStrictEqual(k[0], { kind: 'street', building: '366', plate: true, several: true });
});
ok('two lines: the plate\'s building number and a street of the map, in order; never another building', () => {
  const lines = stack([['702', 100], ['طريق 105', 60]]);
  const road105 = street('Road 105', 'طريق 105');
  const k = (n) => kinds([addr(n)], lines, [road105, addr(n)])[0].kind;
  assert.strictEqual(k('702 105'), 'address');          // a street named by its number alone
  assert.strictEqual(k('702 طريق 105'), 'address');
  assert.strictEqual(k('Bldng 702 Road 105, Riffa'), 'address');   // طريق is Road; filler before the number aside
  assert.strictEqual(k('105 702'), 'drop');            // building 105 on Road 702
  assert.strictEqual(k('702 1050'), 'drop');
  assert.strictEqual(k('702 105 Hamad Town'), 'drop');
  assert.strictEqual(kinds([addr('Bldng 702 Road 105')], stack([['702', 100], ['Road 105', 60]]),
    [street('Road 105'), addr('Bldng 702 Road 105')])[0].kind, 'address');   // filler before the number aside
  // a block line is no street: 366 727 is building 366 on Road 727
  const b = stack([['366', 100], ['Road 116', 60], ['Block 727', 40]]);
  const recs = [addr('366 727'), addr('366 116'), road116(26.2, 50.5)];
  assert.deepStrictEqual(kinds(recs, b, recs).map((x) => x.kind), ['drop', 'address', 'street']);
  // only the tallest number line is the building: a small "12" sticker is not
  const s12 = stack([['366', 100], ['12', 20], ['Road 116', 60]]);
  assert.strictEqual(kinds([addr('12 116')], s12, [addr('12 116'), road116(26.2, 50.5)])[0].kind, 'drop');
  // without the street among the records found, no address is confirmed
  assert.strictEqual(kinds([addr('366 116')], stack([['366', 100], ['Road 116', 60]]), [addr('366 116')])[0].kind, 'drop');
});
ok('street types count: Abbott Drive is not Abbott Avenue; abbreviations are their word', () => {
  const lines = stacked('1121', 'Abbott Drive');
  const recs = [street('Abbott Drive'), street('Abbott Avenue')];
  assert.strictEqual(kinds([addr('1121 Abbott Ave, Campbell')], lines, recs)[0].kind, 'drop');
  assert.strictEqual(kinds([addr('1121 Abbott Dr, Campbell')], lines, recs)[0].kind, 'address');
  assert.strictEqual(kinds([addr('1121 Abbott Drive')], lines, recs)[0].kind, 'address');
  // Bahrain: a record "7 Road 32" is not on Avenue 32 (شارع 32), nor "7 Avenue 32" on Road 32
  assert.strictEqual(kinds([addr('7 Road 32')], stack([['7', 100], ['Avenue 32', 60]]), [street('Avenue 32'), street('Road 32')])[0].kind, 'drop');
  assert.strictEqual(kinds([addr('7 Road 32')], stack([['7', 100], ['شارع 32', 60]]), [street('Avenue 32', 'شارع 32'), street('Road 32')])[0].kind, 'drop');
  assert.strictEqual(kinds([addr('7 Avenue 32')], stack([['7', 100], ['طريق 32', 60]]), [street('Road 32', 'طريق 32')])[0].kind, 'drop');
  // B Street: the letter is the street's, not the building's
  assert.strictEqual(kinds([addr('1018 B St, Hayward')], stacked('1018', 'B Street'), [street('B Street')])[0].kind, 'address');
});
ok('a number beside a street name alone may be a speed limit: its address is only to check', () => {
  const recs = [street('Schulweg'), addr('30 Schulweg, Husum')];
  // a "30" sign under "Schulweg", apart
  const apart = [{ text: 'Schulweg', size: 60, box: { x: 0, y: 0, width: 300, height: 60 } },
                 { text: '30', size: 120, box: { x: 40, y: 400, width: 120, height: 120 } }];
  assert.strictEqual(plate(apart).claim, '');
  assert.strictEqual(kinds([addr('30 Schulweg, Husum')], apart, recs)[0].kind, 'similar');
  // a street with a type word on one plate with its number: the house
  assert.strictEqual(plate(stacked('1121', 'Abbott Drive')).claim, '1121');
  assert.strictEqual(plate([{ text: '50' }, { text: 'Königstraße' }]).claim, '');
  assert.strictEqual(plate(stack([['366', 100], ['Road 116', 60]])).claim, '366');
  // the number elsewhere in the photo, or on a speed sign, is no building
  assert.strictEqual(plate([{ text: 'Road 1601', size: 60, box: { x: 0, y: 0, width: 300, height: 60 } },
                            { text: '40', size: 100, box: { x: 700, y: 0, width: 100, height: 100 } }]).claim, '');
  assert.strictEqual(plate(stack([['North 13th Street', 40], ['SPEED', 30], ['LIMIT', 30], ['25', 90]])).claim, '');
  assert.strictEqual(plate(stack([['25', 90], ['North 13th Street', 40]])).claim, '25');   // a house plaque
  assert.strictEqual(plate(stack([['366', 100], ['ZONE 30', 30], ['Road 116', 60]])).claim, '');
  assert.strictEqual(plate([{ text: '23號' }, { text: '忠孝東路三段' }]).claim, '23號');
  const k = kinds([{ n: 'Königstraße', t: 'street', a: 1, o: 1 }], [{ text: '50' }, { text: 'Königstraße' }]);
  assert.deepStrictEqual(k[0], { kind: 'street', building: '', plate: true, several: false });   // "Street only, not the address"
  const sign = kinds([{ n: 'Königstraße', t: 'street', a: 1, o: 1 }], [{ text: 'Königstraße' }]);
  assert.deepStrictEqual(sign[0], { kind: 'street', building: '', plate: false, several: false });  // a street sign: the same name
});
ok('one line: the street and the number in order, with a name in it', () => {
  const tp = [{ text: '三和路四段20巷101號' }];
  assert.strictEqual(kinds([addr('101號 三和路四段20巷')], tp)[0].kind, 'address');
  assert.strictEqual(kinds([addr('20號 三和路四段101巷')], tp)[0].kind, 'drop');   // house 20 of lane 101
  assert.strictEqual(kinds([addr('10 Schulweg, Husum')], [{ text: 'Schulweg 10' }])[0].kind, 'address');
  assert.strictEqual(kinds([addr('12 Schulweg')], [{ text: 'Schulweg 10' }])[0].kind, 'drop');
  assert.strictEqual(kinds([addr('1029 1225 Road')], [{ text: '1225 Road 1029' }])[0].kind, 'drop');
  assert.deepStrictEqual(kinds([addr('2831 Road, Manama'), { n: 'Road 2831', t: 'street', a: 26.2, o: 50.5 }], [{ text: 'Road 2831' }]).map((x) => x.kind),
    ['drop', 'street']);
});
ok('another building number on the sign leaves an address to check', () => {
  // a Taipei plate: the reader glued a stray "4" to the street and read the plate's "133" apart
  const tp = (x) => [{ text: '高言' }, { text: 'SINCE 19' }, { text: '4 中山北路一段' }, { text: x }, { text: '中山區' }];
  const a4 = [addr('4 中山北路一段, 中山區')];
  assert.strictEqual(kinds(a4, tp('中山區'))[0].kind, 'address');          // nothing else: the town confirms
  for (const x of ['福 133', '133', 'No. 133', 'No.133', '133番地', '門牌 133', '133之1', '133號之1', '133-1', '22-4', '22/4',
                   '133 號 1F', '133호', '１３３', '١٣٣'])
    assert.strictEqual(kinds(a4, tp(x))[0].kind, 'similar', x);
  // not a building's number: a floor, the hours, a unit, a postcode or phone, a year, a contact line, the same number
  for (const x of ['3F', 'B1', '24H', '#200', '0912345678', '© 2024', 'Tel 0912345678', '4', '4號', 'OPEN 24H', '☎ 133', '02 2345 6789', 'Room 133'])
    assert.strictEqual(kinds(a4, tp(x))[0].kind, 'address', x);
  const sw = (x) => [{ text: 'Schulweg 10' }, { text: x }];
  assert.strictEqual(kinds([addr('10 Schulweg, Husum')], sw('12a'))[0].kind, 'similar');
  assert.strictEqual(kinds([addr('12a Schulweg')], [{ text: 'Schulweg 12a' }, { text: '12b' }])[0].kind, 'similar');
  assert.strictEqual(kinds([addr('12a Schulweg')], [{ text: 'Schulweg 12a' }, { text: '12a' }])[0].kind, 'address');
  assert.strictEqual(kinds([addr('10 Schulweg, Duderstadt, Stadt')], sw('37115 Duderstadt'))[0].kind, 'address');
  assert.strictEqual(kinds([addr('101號 三和路四段20巷')], [{ text: '三和路四段20巷101號' }, { text: '20' }])[0].kind, 'address');  // the lane's own number
  // a plate: the tallest number with its street, and another number line on the sign
  const stack = (rows) => rows.map(([text, h], k) => ({ text, size: h, box: { x: 0, y: k * 130, width: 400, height: h } }));
  const drive = [addr('1121 Abbott Drive', 37.3, -121.9), { n: 'Abbott Drive', t: 'street', a: 37.3, o: -121.9 }];
  assert.strictEqual(kinds(drive, stack([['1121', 120], ['Abbott Drive', 60]]), drive)[0].kind, 'address');
  assert.strictEqual(kinds(drive, stack([['1121', 120], ['Abbott Drive', 60], ['1123', 110]]), drive)[0].kind, 'similar');
  assert.strictEqual(kinds(drive, stack([['1121', 120], ['Abbott Drive', 60], ['1123', 50]]), drive)[0].kind, 'similar');   // small print too: only a Cyrillic street's neighbours are told apart by size
  assert.strictEqual(kinds(drive, stack([['1121', 120], ['Abbott Drive', 60], ['Tel 408 555 1212', 30]]), drive)[0].kind, 'address');
});
ok('a name of 2 letters or 1 Chinese, Japanese or Korean character read exactly is only to check', () => {
  const at = (n) => ({ n, t: 'poi', a: 1, o: 1 });
  assert.strictEqual(kinds([at('MT')], [{ text: 'MT' }])[0].kind, 'similar');            // a logo read as letters
  assert.strictEqual(kinds([at('日')], [{ text: '日' }])[0].kind, 'similar');
  assert.strictEqual(kinds([at('역')], [{ text: '역' }])[0].kind, 'similar');            // one Hangul syllable, several letters folded
  assert.strictEqual(kinds([at('KFC')], [{ text: 'KFC' }])[0].kind, 'name');             // 3 letters: a name
  assert.strictEqual(kinds([at('全家')], [{ text: '全家' }])[0].kind, 'name');
  assert.strictEqual(kinds([at('서울')], [{ text: '서울' }])[0].kind, 'name');
  assert.strictEqual(kinds([at('역')], [{ text: '역앞' }])[0].kind, 'drop');            // a near miss of one syllable: noise
});
ok('the same address in many towns: every row and the sheet say so; green only for one', () => {
  const recs = [addr('10 Schulweg, Husum', 54.48, 9.05), addr('10 Schulweg, Anif', 47.74, 13.07)];
  const k = kinds(recs, [{ text: 'Schulweg 10' }], recs);
  assert.deepStrictEqual(k.map((x) => [x.kind, x.several]), [['address', true], ['address', true]]);
  assert.strictEqual(kinds([recs[0]], [{ text: 'Schulweg 10' }], [recs[0]])[0].several, false);
});
ok('a card naming another town is not the address of that town\'s namesake', () => {
  const card = [{ text: 'Hotel Alster' }, { text: 'Schulweg 10' }, { text: '20095 Hamburg' }];
  assert.strictEqual(kinds([addr('10 Schulweg, Husum')], card)[0].kind, 'similar');
  assert.strictEqual(kinds([addr('10 Schulweg, Hamburg')], card)[0].kind, 'address');
  const key = [{ text: '台中市' }, { text: '三和路四段20巷101號' }];
  assert.strictEqual(kinds([addr('101號 三和路四段20巷, 新北市')], key)[0].kind, 'similar');
  assert.strictEqual(kinds([addr('101號 三和路四段20巷, 台中市')], key)[0].kind, 'address');
  // a plate with only its block besides: no town asked
  const b = stack([['366', 100], ['Road 116', 60], ['Block 304', 40]]);
  assert.strictEqual(kinds([addr('366 116')], b, [addr('366 116'), road116(26.2, 50.5)])[0].kind, 'address');
  // a screw read as "a", a shop of the map: nothing; the country holding it: confirms
  const bahrain = { n: 'Bahrain', t: 'admin', s: 'country', a: 26.0, o: 50.55, bb: [50.3, 25.5, 50.9, 26.4] };
  const pharmacy = { n: 'Pharmacy', t: 'poi', a: 26.2, o: 50.5 };
  for (const extra of ['a', 'Pharmacy', 'Kingdom of Bahrain']) {
    const lines = stack([['366', 100], ['Road 116', 60], [extra, 30]]);
    assert.strictEqual(kinds([addr('366 116')], lines, [addr('366 116'), road116(26.2, 50.5), bahrain, pharmacy])[0].kind, 'address', extra);
  }
  // a country known by its point alone says nothing either way
  const bahrainPoint = { n: 'Bahrain', t: 'admin', s: 'country', a: 26.0, o: 50.55 };
  assert.strictEqual(kinds([addr('366 116')], stack([['366', 100], ['Road 116', 60], ['Kingdom of Bahrain', 30]]),
    [addr('366 116'), road116(26.2, 50.5), bahrainPoint])[0].kind, 'address');
  // a line the map knows nothing of (a city it does not have, a stock photo's watermark): to check
  assert.strictEqual(kinds([addr('366 116')], stack([['366', 100], ['Road 116', 60], ['Denver', 30]]), [addr('366 116'), road116(26.2, 50.5)])[0].kind, 'similar');
  // an area of the map named on the sign: consistent when it holds the address, a conflict when not
  const manama = { n: 'Manama', t: 'admin', a: 26.22, o: 50.58, bb: [50.5, 26.15, 50.65, 26.27] };
  const riffa = { n: 'Riffa', t: 'admin', a: 26.12, o: 50.55, bb: [50.5, 26.05, 50.6, 26.15] };
  const a116 = addr('366 116', 26.2289, 50.578);
  const at = (town) => kinds([a116], stack([['366', 100], ['Road 116', 60], [town, 40]]), [a116, road116(26.2288, 50.578), manama, riffa])[0].kind;
  assert.strictEqual(at('Manama'), 'address');
  assert.strictEqual(at('Riffa'), 'similar');
  // Taipei: the city with its district, 臺 for 台; a district of the city; a village name aside
  const tp = addr('100號 忠孝東路三段, 台北市', 25.04, 121.54);
  const st = { n: 'Section 3, Zhongxiao East Road', nn: '忠孝東路三段', t: 'street', a: 25.04, o: 121.54 };
  const daan = { n: 'Da\'an District', nn: '大安區', t: 'admin', a: 25.03, o: 121.54, bb: [121.52, 25.01, 121.56, 25.05] };
  const tpk = (extra) => kinds([tp], [{ text: '忠孝東路三段', size: 1 }, { text: '100號', size: 2 }, { text: extra, size: 1 }], [tp, st, daan])[0].kind;
  assert.strictEqual(tpk('臺北市大安區'), 'address');
  assert.strictEqual(tpk('大安區'), 'address');
  assert.strictEqual(tpk('信義區'), 'similar');        // a district the map does not show here: reads as a town
  assert.strictEqual(tpk('區'), 'address');           // a stray character from a sign's edge is nothing to read
  const village = addr('100號 忠孝東路三段, 民輝里', 25.04, 121.54);
  assert.strictEqual(kinds([village], [{ text: '忠孝東路三段', size: 1 }, { text: '100號', size: 2 }, { text: '大安區', size: 1 }], [village, st, daan])[0].kind, 'address');
  // a card whose own postcode line names another town, even with the record's town in a hotel's name
  assert.strictEqual(kinds([addr('10 Schulweg, Husum')], [{ text: 'Hotel Husum' }, { text: 'Schulweg 10' }, { text: '20095 Hamburg' }])[0].kind, 'similar');
  // a card from a town the map does not have, or a town near but not it: to check
  const sv = (extra, recs = []) => kinds([addr('1121 Abbott Ave, Campbell', 37.28, -121.95)],
    stacked('1121', 'Abbott Avenue').concat([{ text: extra, size: 30, box: { x: 90, y: 300, width: 320, height: 30 } }]),
    [street('Abbott Avenue'), addr('1121 Abbott Ave, Campbell', 37.28, -121.95), ...recs])[0].kind;
  const sanJose = { n: 'San Jose', t: 'admin', s: 'city', a: 37.3382, o: -121.8863 };   // a point, 8.6 km away
  const campbell = { n: 'Campbell', t: 'admin', s: 'city', a: 37.2872, o: -121.9500, bb: [-121.99, 37.26, -121.92, 37.30] };
  assert.strictEqual(sv('Denver'), 'similar');
  assert.strictEqual(sv('Denver, Colorado'), 'similar');
  assert.strictEqual(sv('San Jose', [sanJose]), 'similar');
  assert.strictEqual(sv('Campbell', [campbell]), 'address');
  assert.strictEqual(sv('Campbell, CA 95008', [campbell]), 'address');
  assert.strictEqual(sv('Tel 408 555 0100'), 'address');                       // a phone line says nothing of the town
  assert.strictEqual(sv('Hotel Abbott', [{ n: 'Hotel Abbott', t: 'poi', a: 37.28, o: -121.95 }]), 'address');   // a place of the map
  // Bahrain: a town by its point, near or not (its addresses name no town)
  const isa = { n: 'Isa Town', t: 'place', s: 'town', a: 26.1736, o: 50.5478 };
  const bh = (lat, lon) => kinds([addr('105 702', lat, lon)], stack([['105', 100], ['Road 702', 60], ['Isa Town', 40]]),
    [addr('105 702', lat, lon), street('Road 702'), isa])[0].kind;
  assert.strictEqual(bh(26.1736, 50.6500), 'similar');   // 10 km away
  assert.strictEqual(bh(26.1750, 50.5500), 'address');   // in Isa Town
  // a town of two parts: its first
  assert.strictEqual(kinds([addr('10 Schulweg, Duderstadt, Stadt')], [{ text: 'Schulweg 10' }, { text: '37115 Duderstadt' }])[0].kind, 'address');
});
ok('a number under a long street blade is no house number (a speed roundel)', () => {
  const pole = [{ text: 'North 13th Street', size: 40, box: { x: 0, y: 0, width: 420, height: 40 } },
                { text: '30', size: 90, box: { x: 160, y: 75, width: 90, height: 90 } }];
  assert.strictEqual(plate(pole).claim, '');
  const plaque = [{ text: '1121', size: 90, box: { x: 40, y: 0, width: 240, height: 90 } },
                  { text: 'Abbott Drive', size: 40, box: { x: 0, y: 110, width: 320, height: 40 } }];
  assert.strictEqual(plate(plaque).claim, '1121');
  // a house number as tall as a long street name is still the house's ("105" is no speed)
  const taaffe = [{ text: '105', size: 40, box: { x: 150, y: 0, width: 72, height: 40 } },
                  { text: 'North Taaffe Street', size: 40, box: { x: 0, y: 55, width: 456, height: 40 } }];
  assert.strictEqual(plate(taaffe).claim, '105');
  // an ordinal street is named, not numbered: "21st Avenue" is not 21 and Street
  const recs = [street('21st Avenue')];
  assert.strictEqual(kinds([addr('500 21st Ave')], stacked('500', '21st Avenue'), recs)[0].kind, 'address');
  assert.strictEqual(kinds([addr('500 21st Street')], stacked('500', '21st Avenue'), recs)[0].kind, 'drop');
});
ok('Taipei: the street and its lane on two lines, the 號 under them', () => {
  const st = { n: 'Section 3, Zhongxiao East Road', nn: '忠孝東路三段', t: 'street', a: 25.04, o: 121.55 };
  const a = addr('23號 忠孝東路三段217巷7弄', 25.04, 121.55);
  const k = kinds([a], stack([['忠孝東路三段', 50], ['217巷7弄', 40], ['23號', 80]]), [st, a]);
  assert.strictEqual(k[0].kind, 'address');
});
ok('Taipei: a 號 plate under its street finds the address', () => {
  const st = { n: 'Section 3, Zhongxiao East Road', nn: '忠孝東路三段', t: 'street', a: 25.04, o: 121.55 };
  const lines = [{ text: '忠孝東路三段', size: 1 }, { text: '23號', size: 2 }];
  const a23 = addr('23號 忠孝東路三段, 台北市', 25.04, 121.55);
  const k = kinds([st, a23], lines, [st, a23]);
  assert.deepStrictEqual(k.map((x) => x.kind), ['street', 'address']);
  assert.strictEqual(k[0].building, '');                // its address was found
});
ok('part of a line, a glued line or a bare number is only similar, never the sign\'s name', () => {
  assert.deepStrictEqual(kinds([{ n: 'Grand Mosque', t: 'poi', a: 1, o: 1 }], [{ text: 'Al Noor Grand Mosque' }]).map((x) => x.kind), ['similar']);
  assert.deepStrictEqual(kinds([{ n: '107', t: 'poi', a: 1, o: 1 }], [{ text: '107' }, { text: 'طريق 116' }]).map((x) => x.kind), ['drop']);   // a bare number, and too short
  assert.deepStrictEqual(kinds([{ n: 'Road 1012', nn: 'طريق 1012', t: 'street', a: 1, o: 1 }],
    [{ text: '2' }, { text: '101 طريق' }]).map((x) => x.kind), ['similar']);
  assert.deepStrictEqual(kinds([{ n: 'Al Noor Mosque', t: 'poi', a: 1, o: 1 }], [{ text: 'Mosque Noor Al Grand' }]).map((x) => x.kind), ['similar']);
  // a near miss too short to mean anything is not listed; read exactly, it is
  assert.deepStrictEqual(kinds([{ n: 'IG', t: 'poi', a: 1, o: 1 }, { n: 'Ightham', t: 'place', a: 1, o: 1 },
    { n: '注意', t: 'poi', a: 1, o: 1 }, { n: 'Broadoak', t: 'place', a: 1, o: 1 }], [{ text: 'IGHTHAM' }, { text: 'BROAD OAK' }]).map((x) => x.kind),
  ['drop', 'area', 'drop', 'similar']);
  assert.deepStrictEqual(kinds([{ n: 'KFC', t: 'poi', a: 1, o: 1 }], [{ text: 'KFC' }]).map((x) => x.kind), ['name']);   // read exactly, a shop
  assert.deepStrictEqual(kinds([{ n: '全家', t: 'poi', a: 1, o: 1 }], [{ text: '全家' }]).map((x) => x.kind), ['name']);
  // a town with its distance on a direction sign
  assert.deepStrictEqual(kinds([{ n: 'Ely', t: 'place', a: 1, o: 1 }, { n: 'Ulm', t: 'place', a: 2, o: 2 }], [{ text: 'Ely 12' }, { text: 'Ulm 35 km' }]).map((x) => x.kind), ['area', 'area']);
  assert.deepStrictEqual(kinds([{ n: 'Ely', t: 'place', a: 1, o: 1 }], [{ text: 'ELY' }]).map((x) => x.kind), ['area']);   // a town may be short
});
ok('a town is an area; a shop whose name is a line is the name; another language\'s name counts', () => {
  const k = kinds([{ n: 'Bani Jamrah', nn: 'بني جمرة', t: 'place', a: 26.2, o: 50.45 },
    { n: "Hardee's", t: 'poi', a: 30.06, o: 31.22 }, { n: 'Hardees Express', t: 'poi', a: 30.06, o: 31.22 },
    { n: 'Geneva', ln: { de: 'Genf' }, t: 'place', a: 46.2, o: 6.14 }],
  [{ text: 'بني جمرة' }, { text: 'Hardee’s' }, { text: 'GENF' }]);
  assert.deepStrictEqual(k.map((x) => x.kind), ['area', 'name', 'similar', 'area']);
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
ok('while the search runs: an address is weighed only when it shares the sign\'s number and street', () => {
  const p = plate(stack([['366', 100], ['Road 116', 60]]));
  const may = (n) => L._szSignMayBeAddress({ n }, p, W);
  assert.strictEqual(may('366 116'), true);
  assert.strictEqual(may('３６６ 116'), true);           // full-width digits
  assert.strictEqual(may('366 طريق 116'), true);         // types and filler aside
  assert.strictEqual(may('366 116 Road'), true);
  assert.strictEqual(may('366 Schulweg'), false);        // a word not on the sign
  assert.strictEqual(may('366 727'), false);             // a number not on the sign
  assert.strictEqual(may('10 Schulweg'), false);
  const q = plate([{ text: 'Schulweg 10' }]);
  assert.strictEqual(L._szSignMayBeAddress({ n: '10 Schulweg, Husum' }, q, W), true);
  assert.strictEqual(L._szSignMayBeAddress({ n: '10 Bahnhofstraße' }, q, W), false);
  assert.strictEqual(L._szSignAddressKind(addr('366 727'), p, W, null), 'drop');
  assert.strictEqual(L._szSignAddressKind(addr('366 116'), p, W, null), 'address');
  // the place after the comma is not weighed: its number is not the sign's
  const r = plate(stack([['1242', 100], ['Road 5136', 60]]));
  assert.strictEqual(L._szSignMayBeAddress({ n: 'Bldng 1242 Road No 5136, Askar 951' }, r, W), true);
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
ok('the photo frame: fitted whole, zoomed about a point, kept in bounds, its view a part of the photo', () => {
  // a 1600 x 1200 photo in a 320 x 300 frame: fitted to the width, centred down the frame
  const fit = V._szSignFit(320, 300, 1600, 1200);
  assert.deepStrictEqual([fit.k, fit.w, fit.h, fit.ox, fit.oy], [0.2, 320, 240, 0, 30]);
  const whole = V._szSignClamp({ s: 1, tx: fit.ox, ty: fit.oy }, 320, 300, fit);
  assert.deepStrictEqual(whole, { s: 1, tx: 0, ty: 30 });
  const all = V._szSignVisible(whole, 320, 300, fit, 1600, 1200);
  assert.deepStrictEqual([all.x, all.y, all.width, all.height], [0, 0, 1600, 1200]);
  // twice as big about the frame's centre: the middle half of the photo shows
  const z = V._szSignClamp(V._szSignZoomAt(whole, 2, 160, 150), 320, 300, fit);
  assert.strictEqual(z.s, 2);
  const mid = V._szSignVisible(z, 320, 300, fit, 1600, 1200);
  assert.deepStrictEqual([Math.round(mid.x), Math.round(mid.width)], [400, 800]);
  assert.ok(Math.abs(mid.y - 225) < 1e-9 && Math.abs(mid.height - 750) < 1e-9, JSON.stringify(mid));
  // the point under the fingers stays there
  const at = V._szSignAt(whole, fit, 160, 150), at2 = V._szSignAt(V._szSignZoomAt(whole, 3, 160, 150), fit, 160, 150);
  assert.ok(Math.abs(at.x - at2.x) < 1e-9 && Math.abs(at.y - at2.y) < 1e-9);
  // dragged past an edge: the photo still covers the frame where it is bigger
  const far = V._szSignClamp({ s: 2, tx: 500, ty: -900 }, 320, 300, fit);
  assert.deepStrictEqual(far, { s: 2, tx: 0, ty: 300 - 480 });
  // the canvas shows a part: the frame's view is a part of that part
  const part = V._szSignSubPart([0.5, 0.5, 1, 1], { x: 400, y: 300, width: 800, height: 600 }, 1600, 1200);
  assert.deepStrictEqual(part, [0.625, 0.625, 0.875, 0.875]);
  assert.strictEqual(V._szSignIsWhole([0, 0, 1, 1]), true);
  assert.strictEqual(V._szSignIsWhole(part), false);
});
ok('the photo frame, asymmetric: x and y kept apart, a tall photo in a wide frame, parts at the corner', () => {
  // a tall 600 x 1200 photo in a 400 x 300 frame: fitted to the height, centred across
  const fit = V._szSignFit(400, 300, 600, 1200);
  assert.deepStrictEqual([fit.k, fit.w, fit.h, fit.ox, fit.oy], [0.25, 150, 300, 125, 0]);
  const v1 = V._szSignClamp({ s: 1, tx: 0, ty: 0 }, 400, 300, fit);
  assert.deepStrictEqual(v1, { s: 1, tx: 125, ty: 0 });                       // centred: the pillarbox stays
  const all = V._szSignVisible(v1, 400, 300, fit, 600, 1200);
  assert.deepStrictEqual([all.x, all.y, all.width, all.height], [0, 0, 600, 1200]);   // x1 clamped to the canvas
  // a frame point as a canvas pixel: values, not just invariance
  assert.deepStrictEqual(V._szSignAt(v1, fit, 125, 0), { x: 0, y: 0 });
  assert.deepStrictEqual(V._szSignAt(v1, fit, 275, 300), { x: 600, y: 1200 });
  assert.deepStrictEqual(V._szSignAt({ s: 2, tx: -100, ty: -50 }, fit, 0, 10), { x: 200, y: 120 });
  // zoomed 3x into the top left: 400 x 300 CSS px of a 450 x 900 photo-in-frame
  const z = V._szSignClamp({ s: 3, tx: 0, ty: 0 }, 400, 300, fit);
  assert.deepStrictEqual(z, { s: 3, tx: 0, ty: 0 });
  const r = V._szSignVisible(z, 400, 300, fit, 600, 1200);
  assert.deepStrictEqual([r.x, r.y, Math.round(r.width), Math.round(r.height)], [0, 0, 533, 400]);
  // a part of a part, asymmetric: the canvas shows [0.1, 0.2, 0.5, 0.9] of the photo (cw 400, ch 700)
  const sub = V._szSignSubPart([0.1, 0.2, 0.5, 0.9], { x: 100, y: 70, width: 200, height: 350 }, 400, 700);
  assert.deepStrictEqual(sub.map((x) => +x.toFixed(6)), [0.2, 0.27, 0.4, 0.62]);
  const corner = V._szSignSubPart([0, 0, 1, 1], { x: 0, y: 0, width: 300, height: 100 }, 1200, 800);
  assert.deepStrictEqual(corner, [0, 0, 0.25, 0.125]);
  for (const part of [[0.1, 0, 1, 1], [0, 0.1, 1, 1], [0, 0, 0.9, 1], [0, 0, 1, 0.9]])
    assert.strictEqual(V._szSignIsWhole(part), false, JSON.stringify(part));
  assert.strictEqual(V._szSignIsWhole(null), true);
});
ok('a tap on the photo picks the line whose box it is in, the smallest of nested boxes', () => {
  const lines = [{ text: 'SIGN', box: { x: 100, y: 100, width: 400, height: 200 } },
                 { text: '366', box: { x: 150, y: 120, width: 60, height: 40 } },
                 { text: 'Road 116', box: { x: 150, y: 220, width: 300, height: 50 } }];
  assert.strictEqual(V._szSignHit(lines, 170, 130, 0).text, '366');
  assert.strictEqual(V._szSignHit(lines, 300, 240, 0).text, 'Road 116');
  assert.strictEqual(V._szSignHit(lines, 480, 110, 0).text, 'SIGN');
  assert.strictEqual(V._szSignHit(lines, 50, 50, 0), null);
  assert.strictEqual(V._szSignHit(lines, 95, 95, 6).text, 'SIGN');      // a finger's width outside the box
  for (const [x, y] of [[95, 200], [505, 200], [300, 95], [300, 305]])           // just outside each side
    assert.strictEqual(V._szSignHit(lines, x, y, 6).text, 'SIGN', `${x},${y}`);
  for (const [x, y] of [[93, 200], [507, 200], [300, 93], [300, 307]])           // beyond the tolerance
    assert.strictEqual(V._szSignHit(lines, x, y, 6), null, `${x},${y}`);
  assert.strictEqual(V._szSignHit([], 1, 1, 6), null);
});

ok('a Cyrillic word read with Latin or Greek look-alikes is read as Cyrillic', () => {
  assert.strictEqual(L._szSignCyrillic('BYЛИЦЯ'), 'ВУЛИЦЯ');
  assert.strictEqual(L._szSignCyrillic('KPACИЛИBCЬKA'), 'КРАСИЛІВСЬКА'.replace('І', 'И'));
  assert.strictEqual(L._szSignCyrillic('YΛИЦА'), 'УЛИЦА');
  assert.strictEqual(L._szSignCyrillic('2 КОСbIГИНА 18'), '2 КОСЫГИНА 18');
  assert.strictEqual(L._szSignCyrillic('BYЛ. KPACИЛИBCЬKA'), 'ВУЛ. КРАСИЛИВСЬКА');
  assert.strictEqual(L._szSignCyrillic('TAXI ТАКСИ HOP'), 'TAXI ТАКСИ HOP');   // a third Cyrillic: Latin words stay Latin
  assert.strictEqual(L._szSignCyrillic('КАФЕ ТАКСИ HOP'), 'КАФЕ ТАКСИ НОР');   // look-alikes only, on a Cyrillic line
  assert.strictEqual(L._szSignCyrillic('Road 116'), 'Road 116');              // no Cyrillic: as read
  assert.strictEqual(L._szSignCyrillic('SOHAIL JEWELLERY مشير'), 'SOHAIL JEWELLERY مشير');
  assert.strictEqual(L._szSignCyrillic('NPOB. ДЕСЯТИННИЙ').startsWith('NPOB.'), true);   // N is no look-alike: left as read
});
ok('Ukrainian і, є, ґ compare as и, е, г', () => {
  assert.strictEqual(M._szSignText('Красилівська', norm), M._szSignText('КРАСИЛИВСЬКА', norm));
  assert.strictEqual(M._szSignText('Подільський узвіз', norm), M._szSignText('ПОДИЛЬСЬКИЙ УЗВИЗ', norm));
  assert.strictEqual(M._szSignText('Європейська', norm), M._szSignText('ЕВРОПЕЙСЬКА', norm));
});
ok('Russian and Ukrainian address plates: the type alone on a line, misread or missing; neighbours in small print', () => {
  const ru = (n) => addr(n, 55.74, 37.74);
  // a Moscow plate: the name with the number, "УЛИЦА" under it (read "УЛУЦА" too)
  assert.strictEqual(kinds([ru('2 Басовская улица')], [{ text: 'БАСОВСКАЯ 2' }, { text: 'УЛИЦА' }])[0].kind, 'address');
  assert.strictEqual(kinds([ru('2 Басовская улица')], [{ text: 'БАСОВСКАЯ 2' }, { text: 'УЛУЦА' }])[0].kind, 'address');
  assert.strictEqual(kinds([ru('2 улица Косыгина')], [{ text: 'КОСЫГИНА 2' }, { text: 'УЛИЦА' }])[0].kind, 'address');
  assert.strictEqual(kinds([ru('28 Донская улица')], [{ text: 'ДОНСКАЯ УЛИЦА 28' }])[0].kind, 'address');
  assert.strictEqual(kinds([ru('2 улица Косыгина')], [{ text: 'УЛИЦА КОСЫГИНА 2' }])[0].kind, 'address');
  assert.strictEqual(kinds([ru('2 улица Косыгина')], [{ text: 'УЛ. КОСЫГИНА 2' }])[0].kind, 'address');
  assert.strictEqual(kinds([ru('2 улица Косыгина')], [{ text: 'ПЕР. КОСЫГИНА 2' }])[0].kind, 'drop');   // a lane of that name is not the street
  // another type on the sign is not this street's: "ПЕРЕУЛОК" is no "улица"
  assert.strictEqual(kinds([ru('2 Басовская улица')], [{ text: 'БАСОВСКАЯ 2' }, { text: 'ПЕРЕУЛОК' }])[0].kind, 'drop');
  // no type anywhere: which street of that name, the sign does not say
  assert.strictEqual(kinds([addr('18 Серпова вулиця', 50.46, 30.37)], [{ text: '18 СЕРПОВА' }])[0].kind, 'similar');
  // its neighbours' numbers in small print beside it (a Moscow plate's "◂ 32", "4 ▸") change nothing...
  const sized = (rows) => rows.map(([text, size]) => ({ text, size }));
  assert.strictEqual(kinds([ru('28 Донская улица')], sized([['ДОНСКАЯ УЛИЦА 28', 100], ['32', 40], ['4', 40]]))[0].kind, 'address');
  // ...another number as big as the address's is another building
  assert.strictEqual(kinds([ru('28 Донская улица')], sized([['ДОНСКАЯ УЛИЦА 28', 100], ['32', 90]]))[0].kind, 'similar');
  assert.strictEqual(kinds([ru('28 Донская улица')], [{ text: 'ДОНСКАЯ УЛИЦА 28' }, { text: '32' }])[0].kind, 'similar');   // sizes unknown
  // a house's Cyrillic letter: "12-А", not house 12
  assert.strictEqual(kinds([addr('12-А Серпова вулиця', 50.46, 30.37)], [{ text: 'ВУЛИЦЯ СЕРПОВА 12-А' }])[0].kind, 'address');
  assert.strictEqual(kinds([addr('12-А Серпова вулиця', 50.46, 30.37)], [{ text: 'ВУЛИЦЯ СЕРПОВА 12' }])[0].kind, 'drop');
});
ok('a Moscow plate: a tall number by the name does not glue its two rows; a misread type in a row; the name without its type found', () => {
  const box = (text, x, y, w, h) => ({ text, box: { x, y, width: w, height: h }, confidence: 0.9 });
  // "2" (two rows tall) | "КОСЫГИНА" over "18 ◂ УЛИЦА"
  const items = [box('2', 0, 0, 60, 80), box('КОСЫГИНА', 70, 0, 300, 38), box('18', 70, 42, 40, 36), box('УЛУЦА', 140, 42, 120, 36)];
  const lines = L._szSignLineGroups(items).map((x) => x.text);
  assert.deepStrictEqual(lines, ['2 КОСЫГИНА', '18 УЛУЦА']);
  // rows a little over each other (a tilted photo) stay apart; side by side, a word joins
  const tilt = [box('КОСЫГИНА', 70, 0, 300, 40), box('18', 70, 30, 40, 36)];
  assert.deepStrictEqual(L._szSignLineGroups(tilt).map((x) => x.text), ['КОСЫГИНА', '18']);
  assert.deepStrictEqual(L._szSignLineGroups([box('КОСЫГИНА', 70, 0, 300, 40), box('18', 380, 18, 40, 36)]).map((x) => x.text), ['КОСЫГИНА 18']);
  const ru = (n) => addr(n, 55.70, 37.56);
  assert.strictEqual(kinds([ru('2 улица Косыгина')], [{ text: '2 КОСЫГИНА' }, { text: '18 УЛУЦА' }])[0].kind, 'address');
  assert.strictEqual(kinds([ru('2 улица Косыгина')], [{ text: '2 КОСЫГИНА' }, { text: '18 ПЕРЕУЛОК' }])[0].kind, 'drop');
  // the search finds a street whose sign leaves its type out
  const rec = { n: 'Krasylivska Street', nn: 'Красилівська вулиця', t: 'street' };
  assert.ok(score(rec, ['КРАСИЛИВСЬКА']) >= M.SZ_SIGN_MIN, String(score(rec, ['КРАСИЛИВСЬКА'])));
  assert.ok(score({ n: 'Басовская улица', t: 'street' }, ['БАСОВСКАЯ 2']) >= M.SZ_SIGN_MIN);
  assert.ok(score(rec, ['BYN. КРАСИЛИВСЬКА']) >= M.SZ_SIGN_MIN);
  // only a street, and only where the name without its type is most of the line:
  // "Тверской" of "Тверской бульвар" is no match for a lane's long name
  const line5 = ['1-й ТВЕРСКОЙ-ЯМСК нереулок'];
  assert.strictEqual(score({ n: 'Тверской бульвар', t: 'street' }, line5), 0);
  assert.ok(score({ n: '1-й Тверской-Ямской переулок', t: 'street' }, line5) >= M.SZ_SIGN_MIN);
  assert.strictEqual(score({ n: 'Красилівська вулиця', t: 'poi' }, ['КРАСИЛИВСЬКА 8']), 0);
  assert.ok(score({ n: 'Красилівська вулиця', t: 'street' }, ['КРАСИЛИВСЬКА 8']) >= M.SZ_SIGN_MIN);
  assert.strictEqual(score({ n: 'Вул. Набережна', t: 'street' }, ['ВУЛИЦЯ']), 0);     // "Вул." left: three letters
  assert.strictEqual(score({ n: 'улица Бор', t: 'street' }, ['БОР']), 0);
  // a name left too short without its type is not looked for alone ("2-я" of "2-я улица")
  assert.ok(score({ n: '2-я улица', t: 'street' }, ['2-Я']) < M.SZ_SIGN_MIN, String(score({ n: '2-я улица', t: 'street' }, ['2-Я'])));
  // an ordinary line of words still joins up: a number and a word side by side
  assert.deepStrictEqual(L._szSignLineGroups([box('Road', 0, 0, 120, 40), box('116', 130, 2, 80, 40)]).map((x) => x.text), ['Road 116']);
  assert.deepStrictEqual(L._szSignLineGroups([box('116', 130, 2, 80, 40), box('Road', 0, 0, 120, 40)]).map((x) => x.text), ['Road 116']);
});
ok('the Russian and Ukrainian rules leave other scripts alone', () => {
  const plate = L._szSignPlate([], W);
  // one letter from a type: only a Cyrillic word, only a Cyrillic type ("Driver" is no "Drive")
  assert.deepStrictEqual(plate.norm(['улуца', 'нереулок']), ['улица', 'переулок']);
  assert.deepStrictEqual(plate.norm(['driver', 'stret', 'avenues', 'بیابان']), ['driver', 'stret', 'avenues', 'بیابان']);
  const en = (n) => addr(n, 37.77, -122.42), eg = (n) => addr(n, 30.04, 31.24);
  // a type alone on another line, or missing: a Cyrillic street's case only
  assert.strictEqual(kinds([en('12 Main Street')], [{ text: 'MAIN 12' }, { text: 'STREET' }])[0].kind, 'drop');
  assert.strictEqual(kinds([en('18 Main Street')], [{ text: '18 MAIN' }])[0].kind, 'drop');
  assert.strictEqual(kinds([eg('12 شارع التحرير')], [{ text: 'التحرير 12' }])[0].kind, 'drop');
  // the type elsewhere in the line
  assert.strictEqual(kinds([en('12 Main Street')], [{ text: 'MAIN 12 STREET' }])[0].kind, 'drop');
  // a number in small print beside it is still another building's
  const sized = (rows) => rows.map(([text, size]) => ({ text, size }));
  assert.strictEqual(kinds([en('28 Main Street')], sized([['28 MAIN STREET', 100], ['32', 40]]))[0].kind, 'similar');
  // the search drops only Russian and Ukrainian types
  assert.ok(score({ n: 'Main Street', t: 'street' }, ['MAIN']) < score({ n: 'Main', t: 'street' }, ['MAIN']));
});
ok('a tilted line with a number between its words stays whole in every script but a Cyrillic one', () => {
  const box = (text, x, y, w, h, script) => ({ text, box: { x, y, width: w, height: h }, confidence: 0.9, script });
  // each word lower than the last, as on a photo taken at an angle
  const fa = [box('خیابان', 200, 0, 120, 40, 'Arab'), box('۱۵', 150, 15, 40, 40, 'Arab'), box('خرداد', 30, 26, 110, 40, 'Arab')];
  assert.deepStrictEqual(L._szSignLineGroups(fa).map((x) => x.text), ['خیابان 15 خرداد']);
  const it = [box('Via', 0, 0, 60, 40), box('4', 70, 15, 20, 40), box('Novembre', 100, 26, 200, 40)];
  assert.deepStrictEqual(L._szSignLineGroups(it).map((x) => x.text), ['Via 4 Novembre']);
  // a Cyrillic word or two on a line mostly in another script does not make it a Cyrillic line
  const mixed = [box('Улица', -100, -15, 90, 40), ...it];
  assert.deepStrictEqual(L._szSignLineGroups(mixed).map((x) => x.text), ['Улица Via 4 Novembre']);
  const ar = [box('شارع', 200, 0, 90, 40, 'Arab'), box('٢٦', 150, 15, 40, 40, 'Arab'), box('يوليو', 30, 26, 110, 40, 'Arab')];
  assert.deepStrictEqual(L._szSignLineGroups(ar).map((x) => x.text), ['شارع 26 يوليو']);
});
ok('a Latin brand on a Russian sign is searched as read too; an initial is no house letter; short Ukrainian names without their type', () => {
  const box = (text, x, y, w, h) => ({ text, box: { x, y, width: w, height: h }, confidence: 0.9 });
  const lines = L._szSignLineGroups([box('СТРОИТЕЛЬНЫЙ', 0, 0, 300, 40), box('ГИПЕРМАРКЕТ', 310, 0, 280, 40), box('OBI', 600, 0, 80, 40)]);
  assert.deepStrictEqual(lines.map((x) => x.text), ['СТРОИТЕЛЬНЫЙ ГИПЕРМАРКЕТ ОВІ']);
  const weighted = L._szSignWeighted(lines, 40);
  assert.deepStrictEqual(weighted.map((x) => x.text), ['СТРОИТЕЛЬНЫЙ ГИПЕРМАРКЕТ ОВІ', 'СТРОИТЕЛЬНЫЙ ГИПЕРМАРКЕТ OBI']);
  assert.ok(score({ n: 'OBI', t: 'poi' }, weighted.map((x) => x.text)) >= M.SZ_SIGN_MIN);
  // a Cyrillic word's look-alikes are Cyrillic either way: no second line
  assert.deepStrictEqual(L._szSignWeighted(L._szSignLineGroups([box('KOCЫГИНА', 0, 0, 300, 40)]), 40).map((x) => x.text), ['КОСЫГИНА']);
  // "12 Λ. Κηφισίας" (Λεωφόρος), "12 N. Main Street": an abbreviation or an initial, not house 12Λ or 12N
  assert.strictEqual(kinds([addr('12 Λ. Κηφισίας', 38.0, 23.8)], [{ text: 'Λ. ΚΗΦΙΣΙΑΣ 12' }])[0].kind, 'address');
  assert.strictEqual(kinds([addr('12 N. Main Street', 37.77, -122.42)], [{ text: 'N. MAIN STREET 12' }])[0].kind, 'address');
  assert.strictEqual(kinds([addr('12a Main Street', 37.77, -122.42)], [{ text: 'MAIN STREET 12a' }])[0].kind, 'address');
  // the line's numbers and types aside, a short name is most of it
  for (const [n, line] of [['вулиця Миру', 'ВУЛ. МИРУ'], ['вулиця Миру', 'МИРУ 125'], ['провулок Тихий', 'ПРОВ. ТИХИЙ'], ['переулок Мира', 'ПЕР. МИРА'], ['проїзд Тихий', 'ТИХИЙ']])
    assert.ok(score({ n, t: 'street' }, [line]) >= M.SZ_SIGN_MIN, `${n} / ${line}: ${score({ n, t: 'street' }, [line])}`);
  // a line naming another type is not this street's, its type left out or not
  for (const line of ['1-й ТВЕРСКОЙ-ЯМСК ПЕРЕУЛОК', 'ТВЕРСКОЙ ПЕР.', 'ТВЕРСКОЙ ПЕРЕУЛОК'])
    assert.strictEqual(score({ n: 'Тверской бульвар', t: 'street' }, [line]), 0, line);
  assert.ok(score({ n: '1-й Тверской-Ямской переулок', t: 'street' }, ['1-й ТВЕРСКОЙ-ЯМСК ПЕРЕУЛОК']) >= M.SZ_SIGN_MIN);
  assert.ok(score({ n: 'Тверской переулок', t: 'street' }, ['ТВЕРСКОЙ ПЕР.']) >= M.SZ_SIGN_MIN);
  // "12 a." is a house's letter, a capital with a full stop an initial
  assert.strictEqual(kinds([addr('12 a. Main Street', 37.77, -122.42)], [{ text: 'MAIN STREET 12A' }])[0].kind, 'address');
  assert.strictEqual(kinds([addr('12 ø. Farimagsgade', 55.68, 12.57)], [{ text: 'Ø. FARIMAGSGADE 12' }])[0].kind, 'address');
  assert.strictEqual(kinds([addr('12 а. Серпова вулиця', 50.46, 30.37)], [{ text: 'ВУЛИЦЯ СЕРПОВА 12 А' }])[0].kind, 'address');
  // only a Cyrillic name is looked for without its type
  assert.strictEqual(score({ n: 'Hauptstraße', nn: 'Hauptstraße улица', t: 'street' }, ['HAUPTSTRASSE']), score({ n: 'Hauptstraße', t: 'street' }, ['HAUPTSTRASSE']));
  assert.strictEqual(score({ n: 'Hauptplatz', nn: 'Hauptplatz улица', t: 'street' }, ['HAUPTPLATZ 2']), score({ n: 'Hauptplatz', t: 'street' }, ['HAUPTPLATZ 2']));
  assert.strictEqual(score({ n: 'Main Road', nn: 'Hauptring улица', t: 'street' }, ['HAUPTRING']), 0);
  // and only a Russian or Ukrainian type is left out
  assert.strictEqual(score({ n: 'Main Road', nn: 'Тверская Street', t: 'street' }, ['ТВЕРСКАЯ']), 0);
  // the line as read does not take the place of the sign's twelfth line
  const twelve = Array.from({ length: 12 }, (_, i) => ({ text: i === 11 ? 'ЛЕНИНГРАДСКИЙ ПРОСПЕКТ' : `СТРОКА НОМЕР ${i} ТЕКСТ`, weight: 1 - i * 0.04 }));
  const withAlt = [twelve[0], { text: 'СТРОКА НОМЕР 0 TEKCT', weight: 1, extra: true }, ...twelve.slice(1)];
  assert.strictEqual(M._szSignLines(withAlt, norm, 12).length, 13);
  assert.ok(score({ n: 'Ленинградский проспект', t: 'street' }, withAlt) >= M.SZ_SIGN_MIN);
});
ok('the Russian and Ukrainian types are written as the search folds them', () => {
  for (const re of [M.SZ_SIGN_DROP_TYPE, M.SZ_SIGN_LINE_TYPE]) {
    const ws = re.source.match(/\(([^()]+)\)\(\?=/)[1].split('|');
    assert.ok(ws.length > 10);
    for (const w of ws) assert.strictEqual(norm(w), w, w);
  }
});
ok('Russian and Ukrainian street signs: abbreviated types, the type on its own line, Ukrainian letters', () => {
  const st = (n, nn) => ({ n, nn, t: 'street', a: 50.39, o: 30.51 });
  assert.strictEqual(kinds([st('Krasylivska Street', 'Красилівська вулиця')], [{ text: 'ВУЛ. КРАСИЛИВСЬКА' }])[0].kind, 'street');
  assert.strictEqual(kinds([st('Desiatynnyi Lane', 'Десятинний провулок')], [{ text: 'ПРОВ. ДЕСЯТИННИЙ' }])[0].kind, 'street');
  assert.strictEqual(kinds([st('Kudriavskyi Descent', 'Кудрявський узвіз')], [{ text: 'КУДРЯВСЬКИЙ СПУСК' }])[0].kind, 'street');
  assert.strictEqual(kinds([{ n: 'Басовская улица', t: 'street', a: 55.74, o: 37.74 }], [{ text: 'БАСОВСКАЯ' }, { text: 'УЛИЦА' }])[0].kind, 'street');
  // a lane is not a street of the same name
  assert.strictEqual(kinds([st('Desiatynna Street', 'Десятинна вулиця')], [{ text: 'ПРОВ. ДЕСЯТИННА' }])[0].kind, 'similar');
});
console.log(`${pass} passed`);
