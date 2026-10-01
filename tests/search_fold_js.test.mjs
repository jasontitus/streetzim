// The viewer's search fold must be the writer's, character for character.
// cloud/search_shards.py `norm` (NFKD, drop canonical combining class != 0,
// lowercase) wrote every published search index; the viewer used to drop
// every \p{M} instead, which also removes Indic vowel signs and Thai vowels
// (marks with class 0): "कोलकाता" folded to "कलकत" in the viewer and stayed
// "कोलकाता" in the index, so the prefix keys and leaf paths differed and the
// search found nothing. These tests run the SEARCH_SHARDS block as the
// viewers ship it against the Python writer (PYTHON env, as CI sets it).
//
//   node tests/search_fold_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const REPO_VENV = `${REPO}/venv-linux/bin/python3`;
const PY = process.env.PYTHON || (fs.existsSync(REPO_VENV) ? REPO_VENV : 'python3');
const VIEWERS = ['resources/viewer/index.html', 'resources/viewer/places.html'];

let pass = 0;
function ok(name, fn) {
  try { fn(); console.log('ok   ', name); pass++; }
  catch (e) { console.error('FAIL ', name, '\n      ', e.message); process.exitCode = 1; }
}

function blockOf(file) {
  const h = fs.readFileSync(`${REPO}/${file}`, 'utf8');
  const a = h.indexOf('// BEGIN search-shards');
  const b = h.indexOf('// END search-shards');
  assert.ok(a >= 0 && b > a, `no search-shards block in ${file}`);
  return h.slice(a, b);
}
const S = new Function(blockOf(VIEWERS[0]) + '; return SEARCH_SHARDS;')();
const fold = S.fold;

function py(src, input) {
  const pre = `import json, sys, unicodedata\nsys.path.insert(0, ${JSON.stringify(REPO)})\n`;
  return JSON.parse(execFileSync(PY, ['-c', pre + src], {
    encoding: 'utf8', input: input === undefined ? '' : JSON.stringify(input),
    maxBuffer: 256 * 1024 * 1024,
  }));
}

const show = (s) => `${JSON.stringify(s)} [${Array.from(s).map(c =>
  c.codePointAt(0).toString(16)).join(' ')}]`;

// ---- (a) every code point whose decomposition carries a mark ------------
// Assigned in both Python's and this engine's Unicode (a character new in
// one of them is folded by whichever version the ZIM was written with; see
// tools/gen_combining_marks.py), and not a surrogate.
const CP = py(`
from cloud.search_shards import norm
out = {}
for c in range(0x110000):
    ch = chr(c)
    cat = unicodedata.category(ch)
    if cat in ('Cn', 'Cs'):
        continue
    d = unicodedata.normalize('NFKD', ch)
    if any(unicodedata.category(x).startswith('M') or unicodedata.combining(x) for x in d):
        out[c] = norm(ch)
print(json.dumps({'unicode': unicodedata.unidata_version, 'cp': out}))
`);

ok(`fold agrees with Python norm on every code point whose NFKD has a mark ` +
   `(Python Unicode ${CP.unicode}, ${Object.keys(CP.cp).length} code points)`, () => {
  const bad = [];
  let n = 0;
  for (const [c, want] of Object.entries(CP.cp)) {
    const ch = String.fromCodePoint(Number(c));
    if (/\p{Cn}/u.test(ch)) continue;            // unassigned in this engine
    n++;
    const got = fold(ch);
    if (got !== want) bad.push(`U+${Number(c).toString(16)}: js=${show(got)} py=${show(want)}`);
  }
  assert.ok(n > 2000, `only ${n} code points compared`);
  assert.deepStrictEqual(bad.slice(0, 15), [], `${bad.length} differ`);
});

// ---- (b) real names in many scripts -------------------------------------
const NAMES = [
  // Devanagari (Hindi, Marathi, Nepali)
  'कोलकाता', 'मुंबई', 'नई दिल्ली', 'पुणे', 'काठमाडौं', 'राष्ट्रपति भवन', 'गंगा नदी',
  // Bengali, Gurmukhi, Gujarati
  'কলকাতা', 'ঢাকা', 'চট্টগ্রাম', 'ਅੰਮ੍ਰਿਤਸਰ', 'ਲੁਧਿਆਣਾ', 'અમદાવાદ', 'સુરત',
  // Tamil, Telugu, Kannada, Malayalam
  'தமிழ்நாடு', 'சென்னை', 'மதுரை', 'హైదరాబాద్', 'విజయవాడ', 'ಬೆಂಗಳೂರು', 'ಮೈಸೂರು',
  'തിരുവനന്തപുരം', 'കൊച്ചി',
  // Odia, Sinhala
  'ଭୁବନେଶ୍ୱର', 'කොළඹ', 'මහනුවර',
  // Thai, Lao, Khmer, Myanmar, Tibetan
  'เชียงใหม่', 'กรุงเทพมหานคร', 'ถนนสุขุมวิท', 'ວຽງຈັນ', 'ຫຼວງພະບາງ', 'ភ្នំពេញ', 'សៀមរាប',
  'ရန်ကုန်', 'မန္တလေး', 'ལྷ་ས', 'ཐིམ་ཕུ',
  // Arabic with harakat, Hebrew with niqqud
  'القَاهِرَة', 'مَكَّة المُكَرَّمَة', 'شارع الملك فهد', 'יְרוּשָׁלַיִם', 'תֵּל אָבִיב',
  // Vietnamese, Greek with tonos, Latin with accents
  'Hà Nội', 'Thành phố Hồ Chí Minh', 'Đà Nẵng', 'Αθήνα', 'Θεσσαλονίκη', 'Ηράκλειο',
  'Café São Paulo', 'Écouen', 'Zürich Hauptbahnhof', 'Łódź', 'Ærøskøbing', 'Straße',
  'İstanbul', 'Ñuñoa', 'ΟΔΟΣ ΕΡΜΟΥ',
  // Korean, Japanese kana with (han)dakuten, CJK, Cyrillic, compatibility forms
  '서울특별시', '빌딩', 'ガソリンスタンド', 'ぎんざ', 'パン屋', 'ｶﾞｿﾘﾝ', '東京都', 'Москва',
  'Йошкар-Ола', 'ﬁnca', '①番地', 'Ⅻ',
];

// Both word rules (cloud/search_shards.py `words`): rule 2 is what the
// writer records now ("word_rule": 2), rule 1 what every older ZIM holds.
const PYN = py(`
from cloud.search_shards import norm, prefix_key, paths_for, prefixes_for, WORD_RULES
names = json.load(sys.stdin)
out = {}
for rule in WORD_RULES:
    out[rule] = []
    for n in names:
        nn = norm(n)
        # The writer's keys: the whole name's first two characters plus each
        # word of two or more characters.
        keys = prefixes_for(n, rule)
        paths = {k: sorted(list(p) for p in paths_for(k, n, 3, rule)) for k in keys}
        out[rule].append({'n': n, 'norm': nn, 'keys': sorted(keys), 'paths': paths})
print(json.dumps(out))
`, NAMES);
const MANIFEST = { 1: {}, 2: { word_rule: 2 } };

ok('fold agrees with Python norm on real names in 20+ scripts', () => {
  const bad = PYN[2].filter(r => fold(r.n) !== r.norm)
    .map(r => `${r.n}: js=${show(fold(r.n))} py=${show(r.norm)}`);
  assert.deepStrictEqual(bad, []);
});

// The viewer's getPrefixes (300-search.js) and places.html's prefixKeysFor:
// the words of the folded query, plus the whole query with spaces as '_'.
function jsKey(word) {                 // keyFor / prefixKeyFor
  if (!word) return '__';
  const c0 = word.codePointAt(0);
  if (c0 >= 128) return 'u' + c0.toString(16);
  const an = (ch) => {
    const c = ch.charCodeAt(0);
    return ((c >= 48 && c <= 57) || (c >= 97 && c <= 122) || ch === '_') ? ch : '_';
  };
  return an(word[0]) + (word.length > 1 ? (word.charCodeAt(1) >= 128 ? '_' : an(word[1])) : '_');
}
function jsWords(name, rule) {
  const q = fold(name);
  return S.words(q, MANIFEST[rule]).concat([q.replace(/\s/g, '_')]);
}

for (const rule of [1, 2]) {
  ok(`rule ${rule}: the prefix keys the viewer asks for are the ones the writer indexed`, () => {
    const bad = [];
    for (const r of PYN[rule]) {
      const js = [...new Set(jsWords(r.n, rule).map(jsKey))].sort();
      if (js.join() !== r.keys.join()) bad.push(`${r.n}: js=${js} py=${r.keys}`);
    }
    assert.deepStrictEqual(bad, []);
  });

  ok(`rule ${rule}: the leaf path the viewer targets is one the writer indexed the name under`, () => {
    const bad = [];
    let n = 0;
    for (const r of PYN[rule]) {
      for (const w of jsWords(r.n, rule)) {
        const key = jsKey(w);
        const toks = S.pathTokens(key, w).slice(0, 3);
        if (toks.length < 3) toks.push(S.TERMINAL);
        const have = (r.paths[key] || []).map(p => p.join('|'));
        n++;
        if (!have.includes(toks.join('|'))) {
          bad.push(`${r.n} word ${show(w)} key ${key}: js=${toks.join('|')} py=${JSON.stringify(have)}`);
        }
        // ... and leavesFor, given a manifest splitting that prefix on exactly
        // the writer's paths, selects it.
        const manifest = { char_split: { [key]: have.map(p => p.split('|').join('~')) } };
        const leaves = S.leavesFor(manifest, key, w, r.n, (x) => [x]);
        if (leaves !== null && !leaves.some(l => l.startsWith(`${key}~${toks[0]}`))) {
          bad.push(`${r.n}: leavesFor(${key}, ${w}) = ${JSON.stringify(leaves)}`);
        }
      }
    }
    assert.ok(n > NAMES.length, `only ${n} words checked`);
    assert.deepStrictEqual(bad, []);
  });
}

ok('fold keeps what the writer keeps: Indic vowel signs, Thai vowels', () => {
  assert.strictEqual(fold('कोलकाता'), 'कोलकाता');
  assert.strictEqual(Array.from(fold('मुंबई')).length, 5);
  assert.strictEqual(Array.from(fold('தமிழ்நாடு')).length, 8);   // virama dropped
  assert.strictEqual(Array.from(fold('เชียงใหม่')).length, 8);   // tone mark dropped
  assert.strictEqual(fold('Café São'), 'cafe sao');
  assert.strictEqual(fold('ガソリン'), fold('カソリン'));
  assert.strictEqual(fold(null), '');
  assert.strictEqual(fold(undefined), '');
});

ok('no viewer folds with \\p{M} any more; both delegate to SEARCH_SHARDS.fold', () => {
  for (const f of VIEWERS) {
    const h = fs.readFileSync(`${REPO}/${f}`, 'utf8');
    assert.ok(!/\\p\{M\}\/g?u/.test(h.replace(/NOT \/\\p\{M\}\//g, '')), `${f} still has /\\p{M}/`);
  }
  const idx = fs.readFileSync(`${REPO}/${VIEWERS[0]}`, 'utf8');
  assert.match(idx, /function normalizeText\(s\) \{\s*return SEARCH_SHARDS\.fold\(s\);/);
  const pl = fs.readFileSync(`${REPO}/${VIEWERS[1]}`, 'utf8');
  assert.match(pl, /function foldText\(s\) \{\s*return SEARCH_SHARDS\.fold\(s\);/);
});

console.log(`\n${pass} search-fold JS checks passed`);
