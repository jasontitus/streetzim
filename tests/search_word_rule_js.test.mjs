// The viewers must split a query into words exactly as the writer split the
// names it indexed, under the rule the ZIM records (search-data/manifest.json
// "word_rule"; absent = rule 1, every ZIM written before it existed).
//
//   rule 1: a word is a run of letters/digits; any mark ends it. "कोलकाता"
//           was indexed as "क" "लक" "त", "พัทยา" as "พ" "ทยา".
//   rule 2: marks continue a word and never start one (cloud/search_shards.py
//           `words`), so those names are whole words.
//
// These tests run the SEARCH_SHARDS block and the real call sites (index.html
// getPrefixes, places.html prefixesFor / searchLeavesFor) against the Python
// writer and planner (PYTHON env, as CI sets it).
//
//   node tests/search_word_rule_js.test.mjs
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

const html = (f) => fs.readFileSync(`${REPO}/${f}`, 'utf8');
function blockOf(file) {
  const h = html(file);
  const a = h.indexOf('// BEGIN search-shards');
  const b = h.indexOf('// END search-shards');
  assert.ok(a >= 0 && b > a, `no search-shards block in ${file}`);
  return h.slice(a, b);
}
const BLOCK = blockOf(VIEWERS[0]);
const S = new Function(BLOCK + '; return SEARCH_SHARDS;')();

// The source text of `function <name>(...) {...}` in a viewer, by brace
// matching (the functions below hold no unbalanced brace in a string).
function fnSource(text, name) {
  const a = text.search(new RegExp(`(^|\\n)\\s*(async\\s+)?function ${name}\\(`));
  assert.ok(a >= 0, `no function ${name}`);
  let i = text.indexOf('{', text.indexOf(`function ${name}(`, a));
  let depth = 0;
  for (; i < text.length; i++) {
    if (text[i] === '{') depth++;
    else if (text[i] === '}' && --depth === 0) break;
  }
  return text.slice(text.indexOf(`function ${name}(`, a), i + 1);
}

function py(src, input) {
  const pre = `import json, sys, unicodedata\nsys.path.insert(0, ${JSON.stringify(REPO)})\n`;
  return JSON.parse(execFileSync(PY, ['-c', pre + src], {
    encoding: 'utf8', input: input === undefined ? '' : JSON.stringify(input),
    maxBuffer: 512 * 1024 * 1024,
  }));
}
const show = (s) => `${JSON.stringify(s)} [${Array.from(s).map(c =>
  c.codePointAt(0).toString(16)).join(' ')}]`;

// ---- (a) the character sets, code point by code point --------------------
const CP = py(`
out = []
for c in range(0x110000):
    ch = chr(c)
    cat = unicodedata.category(ch)
    if cat in ('Cn', 'Cs'):
        continue
    word = (ch.isalnum() or cat[0] == 'M') and ch != '_'
    out.append([c, int(word), int(cat[0] == 'M')])
print(json.dumps({'unicode': unicodedata.unidata_version, 'cp': out}))
`);

ok(`rule 2's /[\\p{L}\\p{M}\\p{N}]/u is Python's isalnum-or-mark minus "_" ` +
   `(Python Unicode ${CP.unicode}, every code point assigned in both)`, () => {
  const bad = [];
  let n = 0;
  for (const [c, word, mark] of CP.cp) {
    const ch = String.fromCodePoint(c);
    if (/\p{Cn}/u.test(ch)) continue;             // unassigned in this engine
    n++;
    if (/[\p{L}\p{M}\p{N}]/u.test(ch) !== !!word) bad.push(`U+${c.toString(16)} word`);
    if (/\p{M}/u.test(ch) !== !!mark) bad.push(`U+${c.toString(16)} mark`);
  }
  assert.ok(n > 250000, `only ${n} code points compared`);
  assert.deepStrictEqual(bad.slice(0, 20), [], `${bad.length} differ`);
});

// ---- (b) words of a multi-script corpus ------------------------------------
const NAMES = [
  // Thai, Lao, Khmer, Myanmar, Tibetan
  'พัทยา', 'เชียงใหม่', 'กรุงเทพ', 'กรุงเทพมหานคร', 'วัดพระแก้ว', 'ถนน พัทยา',
  'ถนนสุขุมวิท', 'ซอย ๑๒', 'หาดป่าตอง', 'ภูเก็ต', 'ວຽງຈັນ', 'ຫຼວງພະບາງ', 'ភ្នំពេញ', 'សៀមរាប',
  'ရန်ကုန်', 'မန္တလေး', 'ལྷ་ས', 'ཐིམ་ཕུ',
  // Devanagari, Bengali, Gurmukhi, Gujarati, Tamil, Telugu, Kannada,
  // Malayalam, Odia, Sinhala
  'कोलकाता', 'मुंबई', 'नई दिल्ली', 'गली ४२', 'काठमाडौं', 'কলকাতা', 'ঢাকা', 'চট্টগ্রাম',
  'ਅੰਮ੍ਰਿਤਸਰ', 'અમદાવાદ', 'சென்னை', 'தமிழ்நாடு', 'హైదరాబాద్', 'ಬೆಂಗಳೂರು', 'കൊച്ചി',
  'ଭୁବନେଶ୍ୱର', 'කොළඹ', 'මහනුවර',
  // leading / stray marks, marks next to "_" and punctuation
  'ัabc', 'x ัิก', 'ाि', 'का-ाख', 'क_ाख',
  // unchanged scripts
  'Café São Paulo', 'Écouen', 'Zürich Hauptbahnhof', 'Łódź', 'Straße', 'İstanbul',
  'Hà Nội', 'Đà Nẵng', "Rue de l'Église", '1-12-1 Muramatsu', '45 Broadway',
  'Москва', 'Йошкар-Ола', 'Αθήνα', 'ΟΔΟΣ ΕΡΜΟΥ', '東京都', '서울특별시', 'ガソリンスタンド',
  'ｶﾞｿﾘﾝ', 'القَاهِرَة', 'شارع ١٢٣', 'יְרוּשָׁלַיִם', 'Tbilisi თბილისი', 'ª', 'ﬁnca',
  '①番地', 'Ⅻ', '𠀀𠀁 astral', '𠀀 road', '𝒜𝒷 math', 'a_b c',
];

const PYW = py(`
from cloud.search_shards import norm, words
out = []
for n in json.load(sys.stdin):
    nn = norm(n)
    out.append({'n': n, 'norm': nn,
                'w1': [w for w in words(nn, 1) if len(w) >= 2],
                'w2': [w for w in words(nn, 2) if len(w) >= 2]})
print(json.dumps(out))
`, NAMES);

ok('rule 2: SEARCH_SHARDS.words equals the writer\'s words (>= 2 chars)', () => {
  const bad = [];
  for (const r of PYW) {
    const js = S.words(S.fold(r.n), { word_rule: 2 });
    if (JSON.stringify(js) !== JSON.stringify(r.w2)) {
      bad.push(`${show(r.n)}: js=${JSON.stringify(js)} py=${JSON.stringify(r.w2)}`);
    }
  }
  assert.deepStrictEqual(bad, []);
});

// Rule 1 is the split every viewer shipped until now, kept byte for byte so
// an old ZIM is read as it was written.
const OLD_SPLIT = (q) => q.split(/[^\p{L}\p{N}]+/u).filter((w) => w.length >= 2);

ok('rule 1 (no word_rule): exactly the old viewer split, and the writer\'s rule 1', () => {
  const bad = [];
  for (const r of PYW) {
    const q = S.fold(r.n);
    for (const m of [undefined, null, {}, { word_rule: 1 }, { chunks: {} }]) {
      const js = S.words(q, m);
      if (JSON.stringify(js) !== JSON.stringify(OLD_SPLIT(q))) bad.push(`${r.n}: ${JSON.stringify(m)}`);
    }
    // vs the writer: equal except a 1-code-point astral word, which the
    // old split counted as 2 UTF-16 units (an extra key, never a miss).
    const js = S.words(q, {}).filter((w) => Array.from(w).length >= 2);
    if (JSON.stringify(js) !== JSON.stringify(r.w1)) {
      bad.push(`${show(r.n)}: js=${JSON.stringify(js)} py=${JSON.stringify(r.w1)}`);
    }
  }
  assert.deepStrictEqual(bad, []);
});

ok('rule 2 is only taken from "word_rule": 2', () => {
  assert.strictEqual(S.wordRule(undefined), 1);
  assert.strictEqual(S.wordRule({}), 1);
  assert.strictEqual(S.wordRule({ word_rule: 1 }), 1);
  assert.strictEqual(S.wordRule({ word_rule: '2' }), 1);
  assert.strictEqual(S.wordRule({ word_rule: 2 }), 2);
  assert.deepStrictEqual(S.words('พัทยา', {}), ['ทยา']);
  assert.deepStrictEqual(S.words('พัทยา', { word_rule: 2 }), ['พัทยา']);
  assert.deepStrictEqual(S.words(S.fold('कोलकाता'), {}), ['लक']);
  assert.deepStrictEqual(S.words(S.fold('कोलकाता'), { word_rule: 2 }), ['कोलकाता']);
});

// ---- (c) the real call sites, against a planned index ----------------------
// Records named in many scripts, many copies so the planner splits their
// prefixes; for each rule, Python buckets them (prefixes_for), plans each
// prefix (Aggregator + leaf_for, tiny target), and reports the manifest and
// the leaves each record went to -- the writer's layout, in miniature.
const PLAN = py(`
from cloud.search_shards import (Aggregator, char_split_paths, leaf_for,
                                 prefixes_for, tier_for)
names = json.load(sys.stdin)
recs = []
for i, n in enumerate(names):
    for t in ('place', 'poi', 'street'):
        for j in range(3):
            recs.append({'n': n if j == 0 else f'{n} {j}', 't': t, 'a': i, 'o': j})
out = {}
for rule in (1, 2):
    buckets = {}
    for r in recs:
        for k in prefixes_for(r['n'], rule):
            buckets.setdefault(k, []).append(r)
    chunks, char_split, where = {}, {}, {}
    for k, rs in buckets.items():
        agg = Aggregator(k, rule=rule)
        for r in rs:
            agg.add(r, 100)
        planned = agg.leaves(target_bytes=150)
        by = {}
        for t, p, _c, _b in planned:
            by.setdefault(t, set()).add(p)
        char_split[k] = char_split_paths(planned)
        for r in rs:
            for leaf in leaf_for(k, r, by.get(tier_for(r), ()), rule):
                chunks[leaf] = chunks.get(leaf, 0) + 1
                where.setdefault(f"{r['n']}|{r['t']}|{r['a']}|{r['o']}", []).append(leaf)
    m = {'total': len(recs), 'chunks': chunks, 'char_split': char_split}
    if rule == 2:
        m['word_rule'] = 2
    out[rule] = {'manifest': m, 'where': where}
print(json.dumps({'recs': recs, 'plan': out}))
`, NAMES);

// index.html getPrefixes, as built, with its closure's free names supplied.
const IDX = html(VIEWERS[0]);
const getPrefixesFor = (manifest) => new Function('SEARCH_SHARDS', 'manifest', 'normalizeText',
  fnSource(IDX, 'getPrefixes') + '; return getPrefixes;')(S, manifest, S.fold);

// places.html prefixesFor / searchLeavesFor / expandPrefix, as shipped.
const PL = html(VIEWERS[1]);
const placesFor = (manifest) => new Function('SEARCH_SHARDS', 'state',
  ['foldText', 'prefixKeyFor', 'prefixesFor', 'searchLeavesFor', '_searchHashParents', 'expandPrefix']
    .map((n) => fnSource(PL, n)).join('\n') +
  '; return { prefixesFor, searchLeavesFor };')(S, { manifests: { search: manifest } });

// The queries a user types for a record: the whole name, each word, and the
// name typed so far (3+ code points).
function queriesFor(name) {
  const cps = Array.from(name);
  const qs = new Set([name]);
  for (const w of name.split(/\s+/)) if (Array.from(w).length >= 3) qs.add(w);
  for (let k = 3; k < cps.length; k++) qs.add(cps.slice(0, k).join(''));
  return [...qs];
}

for (const rule of [1, 2]) {
  const { manifest, where } = PLAN.plan[rule];
  const getPrefixes = getPrefixesFor(manifest);
  const places = placesFor(manifest);
  ok(`rule ${rule}: every viewer query reads a leaf holding the record ` +
     `(index.html getPrefixes + places.html searchLeavesFor, ${PLAN.recs.length} records)`, () => {
    const bad = [];
    let n = 0;
    for (const r of PLAN.recs) {
      const leaves = where[`${r.n}|${r.t}|${r.a}|${r.o}`];
      assert.ok(leaves && leaves.length, `record ${r.n} reached no leaf`);
      for (const q of queriesFor(r.n)) {
        if (Array.from(S.fold(q).trim()).length < 3) continue;
        // Nobody types a vowel sign first; no rule indexes a word there.
        if (/^\p{M}/u.test(S.fold(q))) continue;
        n++;
        const a = getPrefixes(q);
        const b = places.searchLeavesFor(q) || [];
        if (!a.some((x) => leaves.includes(x))) bad.push(`index ${show(q)} -> ${a} not in ${leaves}`);
        if (!b.some((x) => leaves.includes(x))) bad.push(`places ${show(q)} -> ${b} not in ${leaves}`);
      }
    }
    assert.ok(n > 500, `only ${n} queries checked`);
    assert.deepStrictEqual(bad.slice(0, 40), [], `${bad.length} misses`);
  });
}

// Each call site must take its words from SEARCH_SHARDS.words, with the
// ZIM's own manifest: the whole-query key alone finds most records, so a call
// site splitting on its own (or passing no manifest, i.e. rule 1 on a rule-2
// ZIM) would still find things -- by reading the 1-2 character fragment
// subtrees rule 2 exists to avoid. A spy proves the words come from here.
for (const rule of [1, 2]) {
  const { manifest } = PLAN.plan[rule];
  ok(`rule ${rule}: index.html getPrefixes and places.html prefixesFor/searchLeavesFor ` +
     'split through SEARCH_SHARDS.words with the ZIM manifest', () => {
    const calls = [];
    const spy = Object.assign({}, S, {
      words(t, m) { calls.push([t, m]); return S.words(t, m); },
    });
    const getPrefixes = new Function('SEARCH_SHARDS', 'manifest', 'normalizeText',
      fnSource(IDX, 'getPrefixes') + '; return getPrefixes;')(spy, manifest, S.fold);
    const places = new Function('SEARCH_SHARDS', 'state',
      ['foldText', 'prefixKeyFor', 'prefixesFor', 'searchLeavesFor', '_searchHashParents', 'expandPrefix']
        .map((n) => fnSource(PL, n)).join('\n') +
      '; return { prefixesFor, searchLeavesFor };')(spy, { manifests: { search: manifest } });
    const q = 'ถนน พัทยา';
    for (const [site, run] of [['getPrefixes', () => getPrefixes(q)],
                               ['prefixesFor', () => places.prefixesFor(q)],
                               ['searchLeavesFor', () => places.searchLeavesFor(q)]]) {
      calls.length = 0;
      run();
      assert.ok(calls.length >= 1, `${site} did not call SEARCH_SHARDS.words`);
      for (const [t, m] of calls) {
        assert.strictEqual(m, manifest, `${site} passed another manifest`);
        assert.strictEqual(t.trim(), S.fold(q).trim(), `${site} split ${show(t)}`);
      }
    }
  });
}

ok('rule 2 reads fewer leaves than rule 1 for names cut at a vowel sign', () => {
  let fewer = 0;
  for (const q of ['พัทยา', 'เชียงใหม่', 'कोलकाता', 'ภูเก็ต', 'ວຽງຈັນ']) {
    const l1 = getPrefixesFor(PLAN.plan[1].manifest)(q).length;
    const l2 = getPrefixesFor(PLAN.plan[2].manifest)(q).length;
    assert.ok(l2 <= l1, `${q}: rule 2 ${l2} > rule 1 ${l1}`);
    if (l2 < l1) fewer++;
  }
  assert.ok(fewer >= 3, `only ${fewer} queries got cheaper`);
});

ok('the old places.html prefixesFor keys are unchanged for a rule-1 ZIM', () => {
  const places = placesFor({ chunks: {} });
  const oldKeys = (q) => {
    const f = S.fold(q).trim();
    const out = new Set();
    const k = (w) => {
      const c0 = w.codePointAt(0);
      if (c0 >= 128) return 'u' + c0.toString(16);
      const an = (ch) => (/[0-9a-z_]/.test(ch) ? ch : '_');
      return an(w[0]) + (w.length > 1 ? an(w[1]) : '_');
    };
    if (f.length >= 2) out.add(k(f.replace(/\s/g, '_')));
    for (const w of f.split(/[^\p{L}\p{N}]+/u)) if (w.length >= 2) out.add(k(w));
    return [...out];
  };
  for (const n of NAMES) {
    assert.deepStrictEqual(places.prefixesFor(n), oldKeys(n), n);
  }
});

ok('no viewer splits a query into words except through SEARCH_SHARDS.words', () => {
  for (const f of [...VIEWERS, 'web/drive/viewer/index.html', 'web/drive/viewer/places.html']) {
    const h = html(f);
    const outside = h.replace(blockOf(f), '');
    assert.ok(!/split\(\/\[\^\\p\{L\}/.test(outside), `${f} splits words outside SEARCH_SHARDS`);
    assert.strictEqual(blockOf(f), BLOCK, `${f}: search-shards block differs`);
  }
});

console.log(`\n${pass} word-rule JS checks passed`);
