// Grouped siblings (cloud/search_shards.py GROUP_BYTES): small children of a
// split search node share one leaf named by a code-point range token,
// "r<lo>.<hi>", and such a prefix is listed under the manifest's char_ranges.
// The viewers must pick exactly the leaves the writer filled. This compares
// both viewers' real call sites (index.html getPrefixes, places.html
// searchLeavesFor) against the real writer pass (zim_writer._search_emit_chunks)
// on a multi-script corpus with thousands of CJK first characters, and checks
// that a viewer which ignores char_ranges (every viewer before this change)
// still finds every record through the whole prefix.
//
//   node tests/search_ranges_js.test.mjs
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
const S = new Function(blockOf(VIEWERS[0]) + '; return SEARCH_SHARDS;')();

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

// ---- unit: range tokens ------------------------------------------------
ok('the search-shards block is identical in both viewers', () => {
  assert.strictEqual(blockOf(VIEWERS[1]), blockOf(VIEWERS[0]));
});

ok('a range token covers the code points between its bounds, inclusive', () => {
  assert.ok(S.tokenMatches('r4e00.4e8b', 'u4e00'));
  assert.ok(S.tokenMatches('r4e00.4e8b', 'u4e8b'));
  assert.ok(!S.tokenMatches('r4e00.4e8b', 'u4e8c'));
  assert.ok(!S.tokenMatches('r4e00.4e8b', 'u4dff'));
  assert.ok(!S.tokenMatches('r4e00.4e8b', S.TERMINAL));
  assert.ok(S.tokenMatches('r61.66', 'c') && !S.tokenMatches('r61.66', 'g'));
  assert.ok(S.tokenMatches('r', 'r') && !S.tokenMatches('r', 's'), 'the ASCII token r is no range');
  assert.ok(!S.tokenMatches('u4e00', 'u4e01'));
});

ok('pathsFor reads a range in both directions', () => {
  const declared = ['r4e00.4e8b', 'u5b66', 'u5b66~r4e00.4e10', 'u5b66~u4e11', '_e'];
  assert.deepStrictEqual(S.pathsFor(declared, ['u4e05']), ['r4e00.4e8b']);
  assert.deepStrictEqual(S.pathsFor(declared, ['u4e05', 'u4e06']), ['r4e00.4e8b']);
  assert.deepStrictEqual(S.pathsFor(declared, ['u5b66']).sort(),
    ['u5b66', 'u5b66~r4e00.4e10', 'u5b66~u4e11']);
  assert.deepStrictEqual(S.pathsFor(declared, ['u5b66', 'u4e0f']), ['u5b66', 'u5b66~r4e00.4e10']);
  assert.deepStrictEqual(S.pathsFor(declared, ['u4e8c']), [], 'outside every range: no records');
});

ok('leavesFor takes char_ranges, char_split first, and neither means the old path', () => {
  const m = { char_ranges: { u5927: ['r4e00.4e8b'] }, chunks: {} };
  assert.deepStrictEqual(S.leavesFor(m, 'u5927', '大丁', '大丁', (n) => [n]),
    ['u5927~r4e00.4e8b~c', 'u5927~r4e00.4e8b~p', 'u5927~r4e00.4e8b~s']);
  assert.deepStrictEqual(S.leavesFor(m, 'u5927', '大学', '大学', (n) => [n]), []);
  assert.strictEqual(S.leavesFor({ chunks: {} }, 'u5927', '大丁', '大丁', (n) => [n]), null);
  const both = { char_split: { u5927: ['u4e01'] }, char_ranges: { u5927: ['r4e00.4e8b'] } };
  assert.deepStrictEqual(S.splitPaths(both, 'u5927'), ['u4e01']);
});

// ---- the writer's real layout -------------------------------------------
const PLAN = JSON.parse(execFileSync(PY, ['-c', `
import json, random, sys, tempfile, os
sys.path.insert(0, ${JSON.stringify(REPO)})
from cloud.search_shards import prefixes_for
from streetzim import zim_writer
rnd = random.Random(5)
TYPES = ('place', 'poi', 'street', 'addr')
recs = []
for i in range(3000):                    # 3,000 distinct CJK first characters
    recs.append({'n': chr(0x4E00 + i * 5) + chr(0x4E00 + rnd.randrange(20000)), 't': 'poi'})
for head in '大新中東':                   # hot CJK prefixes, thousands of 2nd chars
    for _ in range(2500):
        n = head + chr(0x4E00 + int(rnd.paretovariate(0.6)) % 3000)
        if rnd.random() < 0.6:
            n += chr(0x4E00 + rnd.randrange(20000))
        if rnd.random() < 0.3:
            n += rnd.choice(['路', '街', ' 3号', 'ビル', '역'])
        recs.append({'n': n, 't': rnd.choice(TYPES)})
for _ in range(3000):                    # pinyin, Hangul and kana mixed in
    syl = rnd.choice(['ong', 'ang', 'en', 'u', 'i', 'ao', 'ou', 'eng', 'uan'])
    tail = rnd.choice(['', ' Lu', ' 大道', '东路', ' 서울', 'まち'])
    recs.append({'n': 'Zh' + syl + rnd.choice('bcdfghjklmnpqrstwxyz') + str(rnd.randrange(99)) + tail,
                 't': rnd.choice(TYPES)})
for _ in range(1500):
    recs.append({'n': rnd.choice(['서', '대', '東', 'さ']) + chr(0xAC00 + rnd.randrange(400)) + '동',
                 't': rnd.choice(TYPES)})
for c in 'abcdefghijklmnopqrstuvwxyz':  # Latin: every child big
    for i in range(60):
        recs.append({'n': f'Ca{c}a {i}', 't': 'street', 'pad': 'y' * 400})
for i, r in enumerate(recs):
    r['i'] = i
tmp = tempfile.mkdtemp(dir=os.environ.get('TMPDIR'))
counts = {}
for r in recs:
    line = json.dumps(r, separators=(',', ':')) + '\\n'
    for k in prefixes_for(r['n']):
        with open(os.path.join(tmp, k + '.jsonl'), 'a', encoding='utf-8') as f:
            f.write(line)
        counts[k] = counts.get(k, 0) + 1
class I:
    def __init__(self, path, title, mime, data, compress=True, **kw):
        self.path, self.data = path, data
items = {}
class C:
    def add_item(self, it):
        items[it.path] = it.data
import contextlib, io
with contextlib.redirect_stdout(io.StringIO()):
    zim_writer._search_emit_chunks(C(), I, split_hot_search_chunks_mb=0.05, chunk_tmp=tmp,
                                   chunk_counts=counts, total_features=len(recs))
where = {}
for p, blob in items.items():
    if p == 'search-data/manifest.json':
        continue
    leaf = p[len('search-data/'):-len('.json')]
    for r in json.loads(blob):
        where.setdefault(r['i'], []).append(leaf)
print(json.dumps({'recs': recs, 'manifest': json.loads(items['search-data/manifest.json']),
                  'where': where}))
`], { encoding: 'utf8', maxBuffer: 512 * 1024 * 1024, env: process.env }));

const { recs, manifest, where } = PLAN;
const firstChars = new Set(recs.map((r) => Array.from(r.n)[0]));

ok(`the writer grouped siblings (${Object.keys(manifest.char_ranges || {}).length} char_ranges ` +
   `prefixes, ${firstChars.size} distinct first characters)`, () => {
  assert.ok(firstChars.size > 3000);
  for (const p of ['u5927', 'u65b0', 'u4e2d', 'u6771']) {
    assert.ok(manifest.char_ranges && manifest.char_ranges[p], `no char_ranges for ${p}`);
    assert.ok(manifest.char_ranges[p].some((x) => /(^|~)r[0-9a-f]+\.[0-9a-f]+$/.test(x)), p);
  }
  assert.ok(manifest.char_split && manifest.char_split.ca, 'ca has no range: char_split');
});

const getPrefixesFor = (m) => new Function('SEARCH_SHARDS', 'manifest', 'normalizeText',
  fnSource(html(VIEWERS[0]), 'getPrefixes') + '; return getPrefixes;')(S, m, S.fold);
const PL = html(VIEWERS[1]);
const placesFor = (m) => new Function('SEARCH_SHARDS', 'state',
  ['foldText', 'prefixKeyFor', 'prefixesFor', 'searchLeavesFor', '_searchHashParents', 'expandPrefix']
    .map((n) => fnSource(PL, n)).join('\n') +
  '; return { searchLeavesFor, expandPrefix, prefixesFor };')(S, { manifests: { search: m } });

function queriesFor(name) {
  const cps = Array.from(name);
  const qs = new Set([name]);
  for (const w of name.split(/\s+/)) if (Array.from(w).length >= 2) qs.add(w);
  for (let k = 2; k < cps.length; k++) qs.add(cps.slice(0, k).join(''));
  return [...qs];
}

function check(m, label, { placesToo }) {
  const getPrefixes = getPrefixesFor(m);
  const places = placesFor(m);
  const bad = [];
  let n = 0, targeted = 0;
  for (const r of recs) {
    const leaves = where[r.i];
    assert.ok(leaves && leaves.length, `record ${r.n} is in no leaf`);
    for (const q of queriesFor(r.n)) {
      const f = S.fold(q).trim();
      if (Array.from(f).length < 2) continue;
      // Addresses are read only for a query that earns them (leavesFor).
      if (r.t === 'addr' && !(/\d/.test(f) && f.replace(/\W/g, '').length >= 4)) continue;
      n++;
      const a = getPrefixes(q);
      if (a.targeted) targeted++;
      if (!a.some((x) => leaves.includes(x))) bad.push(`index ${q} -> ${a.slice(0, 4)} not in ${leaves}`);
      if (placesToo && f.length >= 3) {
        const b = places.searchLeavesFor(q) || places.prefixesFor(q).flatMap(places.expandPrefix);
        if (!b.some((x) => leaves.includes(x))) bad.push(`places ${q} -> ${b.slice(0, 4)} not in ${leaves}`);
      }
    }
  }
  assert.ok(n > 30000, `only ${n} queries`);
  assert.deepStrictEqual(bad.slice(0, 20), [], `${label}: ${bad.length} misses of ${n}`);
  return { n, targeted };
}

ok('every query reads a leaf holding the record (index.html getPrefixes + places.html searchLeavesFor)', () => {
  const { n, targeted } = check(manifest, 'new viewer', { placesToo: true });
  assert.ok(targeted > n / 2, `only ${targeted}/${n} queries targeted`);
});

ok('places.html targets a ZIM whose only split prefixes have ranges', () => {
  const only = { ...manifest };
  delete only.char_split;
  const r = recs.find((x) => x.t === 'poi' && x.n.startsWith('大') && Array.from(x.n).length >= 3);
  const got = placesFor(only).searchLeavesFor(r.n);
  assert.ok(got && got.targeted, `not targeted: ${got}`);
  assert.ok(got.some((x) => where[r.i].includes(x)), `${r.n}: ${got} not in ${where[r.i]}`);
});

ok('a viewer that ignores char_ranges (every viewer before it) still finds every record', () => {
  const old = { ...manifest };
  delete old.char_ranges;
  check(old, 'old viewer', { placesToo: false });
});

ok('no viewer query reads a leaf the manifest does not declare', () => {
  const getPrefixes = getPrefixesFor(manifest);
  for (const r of recs.slice(0, 4000)) {
    for (const q of queriesFor(r.n)) {
      for (const l of getPrefixes(q)) {
        assert.notStrictEqual(manifest.chunks[l], undefined, `${q} -> ${l}`);
      }
    }
  }
});

console.log(`\n${pass} search-ranges JS checks passed`);
