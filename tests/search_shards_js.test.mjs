// The browser's leaf targeting must agree with cloud/search_shards.py, or a
// reader asks for leaves the writer never wrote. Two real bugs already came
// from that seam: a "u1107" token split into five characters, and "_" used
// for both "punctuation" and "word ends here" (which stranded 476 k japan
// records). These tests compare the JS against the Python planner directly,
// and against a real retrofitted manifest when one is present.
//
//   node tests/search_shards_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const PY = `${REPO}/venv-linux/bin/python3`;
const VIEWERS = [
  'resources/viewer/index.html', 'resources/viewer/places.html',
  'web/drive/viewer/index.html', 'web/drive/viewer/places.html',
];

let pass = 0;
function ok(name, fn) {
  try { fn(); console.log('ok   ', name); pass++; }
  catch (e) { console.error('FAIL ', name, '\n      ', e.message); process.exitCode = 1; }
}

// ---- load the block the viewers actually ship -------------------------
function blockOf(file) {
  const h = fs.readFileSync(`${REPO}/${file}`, 'utf8');
  const a = h.indexOf('// BEGIN search-shards');
  const b = h.indexOf('// END search-shards');
  assert.ok(a >= 0 && b > a, `no search-shards block in ${file}`);
  return h.slice(a, b);
}
const block = blockOf(VIEWERS[0]);
const S = new Function(block + '; return SEARCH_SHARDS;')();

ok('the block is identical in every viewer copy', () => {
  for (const f of VIEWERS) assert.strictEqual(blockOf(f), block, f);
});

// ---- tokens match the Python planner ----------------------------------
function pyPaths(prefix, name, depth) {
  const src = `
import json, sys
sys.path.insert(0, ${JSON.stringify(REPO)})
from cloud.search_shards import paths_for
print(json.dumps(sorted(list(p) for p in paths_for(${JSON.stringify(prefix)}, ${JSON.stringify(name)}, ${depth}))))
`;
  return JSON.parse(execFileSync(PY, ['-c', src], { encoding: 'utf8' }));
}

const WORDS = [
  ['ca', 'Caracas'], ['ca', 'Casa Cabral'], ['ca', 'Ca'], ['ca', 'Cáceres'],
  ['gi', 'Gim'], ['gi', 'Gimpo'], ['ch', 'ch빌딩'], ['da', 'DA의원'],
  ['ex', 'EXサポート'], ['10', '10-5 Chuo'], ['12', '1-12-1 Muramatsu'],
];

ok('JS tokens agree with the Python planner for one-word names', () => {
  for (const [prefix, name] of WORDS) {
    const word = name.toLowerCase().normalize('NFKD').replace(/\p{M}/gu, '')
      .split(/[^\p{L}\p{N}]+/u).find(w => w.length >= 2 && jsKey(w) === prefix);
    if (!word) continue;                      // whole-name-only case
    const js = S.pathTokens(prefix, word);
    const py = pyPaths(prefix, word, js.length || 1);
    // The planner pads a short word with its terminal token; compare the
    // characters the JS derived against the planner's, ignoring that pad.
    const stripped = py.map(p => p.filter(t => t !== S.TERMINAL));
    assert.ok(stripped.some(p => p.join('|') === js.join('|')),
      `${prefix} ${word}: js=${js} py=${JSON.stringify(py)}`);
  }
});

function jsKey(word) {           // mirrors prefixKeyFor / keyFor
  if (!word) return '__';
  const c0 = word.codePointAt(0);
  if (c0 >= 128) return 'u' + c0.toString(16);
  const an = (ch) => {
    const c = ch.charCodeAt(0);
    const alnum = (c >= 48 && c <= 57) || (c >= 97 && c <= 122);
    return (alnum || ch === '_') ? ch : '_';
  };
  return an(word[0]) + (word.length > 1 ? an(word[1]) : '_');
}

ok('a non-ASCII character becomes one u<hex> token, not five', () => {
  const t = S.tokenFor('빌');
  assert.strictEqual(t, 'u' + '빌'.codePointAt(0).toString(16));
  assert.ok(/^u[0-9a-f]+$/.test(t));
});

ok('the terminal token can never collide with a character token', () => {
  for (const ch of ['-', '_', ' ', 'a', '7', '빌', '東']) {
    assert.notStrictEqual(S.tokenFor(ch), S.TERMINAL, `token for ${ch}`);
  }
});

// ---- path selection ----------------------------------------------------
ok('typing 3 chars reads the whole subtree under that character', () => {
  const declared = ['r', 'r~a', 'r~b', 'r~_e', 'l', 'm'];
  const got = S.pathsFor(declared, ['r']);
  assert.deepStrictEqual(got.sort(), ['r', 'r~_e', 'r~a', 'r~b']);
});

ok('typing 4 chars still reads a shallower leaf that was never split', () => {
  assert.deepStrictEqual(S.pathsFor(['r', 'l'], ['r', 'a']), ['r']);
});

ok('a typed character with no declared path means no records', () => {
  const manifest = { char_split: { ca: ['r', 'l'] }, chunks: {} };
  const leaves = S.leavesFor(manifest, 'ca', 'caq', 'caq', () => []);
  assert.deepStrictEqual(leaves, []);
});

ok('a prefix with no character split falls back to the old path', () => {
  const manifest = { char_split: {}, chunks: {} };
  assert.strictEqual(S.leavesFor(manifest, 'ca', 'car', 'car', () => []), null);
});

ok('tiers are fetched place-like first, then POI, then street', () => {
  const manifest = { char_split: { ca: ['r'] }, chunks: {} };
  const leaves = S.leavesFor(manifest, 'ca', 'car', 'car', (n) => [n]);
  assert.deepStrictEqual(leaves, ['ca~r~c', 'ca~r~p', 'ca~r~s']);
});

ok('addresses are read only for a digit query of four or more characters', () => {
  const manifest = { char_split: { ca: ['r'] }, chunks: {} };
  const plain = S.leavesFor(manifest, 'ca', 'car', 'car', (n) => [n]);
  assert.ok(!plain.some(l => l.endsWith('~a')));
  const short = S.leavesFor(manifest, 'ca', 'car', '7 c', (n) => [n]);
  assert.ok(!short.some(l => l.endsWith('~a')), 'too short to be an address');
  const addr = S.leavesFor(manifest, 'ca', 'carrera', '123 carrera', (n) => [n]);
  assert.ok(addr.some(l => l.endsWith('~a')));
});

ok('leaves resolve through expandPrefix, so hash children are included', () => {
  const manifest = { char_split: { ca: ['r'] }, chunks: {} };
  const resolve = (n) => n === 'ca~r~p' ? ['ca~r~p-0', 'ca~r~p-1'] : [n];
  const leaves = S.leavesFor(manifest, 'ca', 'car', 'car', resolve);
  assert.ok(leaves.includes('ca~r~p-0') && leaves.includes('ca~r~p-1'));
  assert.ok(!leaves.includes('ca~r~p'));
});

ok('a leaf is never listed twice', () => {
  const manifest = { char_split: { ca: ['r', 'r~a'] }, chunks: {} };
  const leaves = S.leavesFor(manifest, 'ca', 'cara', 'cara', () => ['ca~r~c']);
  assert.strictEqual(new Set(leaves).size, leaves.length);
});

// ---- against a real retrofitted manifest, when one exists --------------
const REAL = '/storage/streetzim/tmp/chips-e2e/km-search-manifest.json';
if (fs.existsSync(REAL)) {
  const manifest = JSON.parse(fs.readFileSync(REAL, 'utf8'));
  const resolve = (name) => {
    if (manifest.chunks[name] !== undefined) return [name];
    const out = [];
    for (const k in manifest.chunks) if (k.startsWith(name + '-')) out.push(k);
    return out;
  };
  ok('every leaf named for a real ZIM exists in its manifest', () => {
    for (const [prefix, word] of [['ch', 'chase'], ['gi', 'gimpo'], ['gi', 'gim'],
                                  ['ex', 'express'], ['da', 'daejeon']]) {
      const leaves = S.leavesFor(manifest, prefix, word, word, resolve) || [];
      assert.ok(leaves.length, `${word}: no leaves`);
      for (const l of leaves) {
        assert.notStrictEqual(manifest.chunks[l], undefined, `${word} → missing ${l}`);
      }
    }
  });
} else {
  console.log('skip  real-manifest check (no retrofitted ZIM manifest on disk)');
}

console.log(`\n${pass} search-shards JS checks passed`);
