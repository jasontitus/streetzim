// Keeps the viewer's two hand-written chip lists in lock-step with the
// build's single source of truth, cloud/chip_rules.py CHIP_RULES.
//
//   node tests/chip_rules_js.test.mjs          (PYTHON=… to pick the venv)
//
// Why literals and not a chip-rules.json the viewer fetches: published ZIMs
// get their viewer swapped in place (cloud/patch_viewer_inplace.py), which
// can overwrite index.html / places.html but cannot add a new ZIM entry. A
// fetched JSON would 404 on every existing ZIM. So the rules stay inline in
// the viewer, and this test is what makes "edit chip_rules.py, forget the
// viewer" fail CI instead of shipping.
//
// Checked:
//   * places.html `CATEGORIES` (block `chip-rules`) equals CHIP_RULES field
//     by field, in order (id, label, cat, subtypes, includeRegex,
//     nameInclude).
//   * index.html `EXPLORE_CHIPS` (block `chip-rail`) has exactly the same
//     ids and labels (order is a deliberate UI choice, so it is free) and
//     every entry has an emoji.
//   * The resources/viewer and web/drive/viewer copies of each block are
//     byte-identical (web/drive/viewer is a copy made by
//     scripts/sync-drive-viewer.sh).
//   * On a shared corpus, places.html's legacy client-side filter gives the
//     same answer as record_matches_chip() in Python, so a regex that means
//     different things in the two dialects fails here. One known, accepted
//     difference is left out of the corpus: JS `\b` is ASCII-only, so
//     "Museumà" matches /\bmuseum\b/i in JS but not in Python. It only
//     affects that legacy path (ZIMs without per-chip files) on such names.
import { readFileSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const AUTHOR_VENV = '/storage/streetzim/venv-linux/bin/python3';
const PY = process.env.PYTHON || (existsSync(AUTHOR_VENV) ? AUTHOR_VENV : 'python3');

function extractBlock(file, name) {
  const src = readFileSync(join(ROOT, file), 'utf8');
  const begin = `// BEGIN ${name}`;
  const end = `// END ${name}`;
  const a = src.indexOf(begin);
  const b = src.indexOf(end);
  assert.ok(a >= 0 && b > a, `${file}: ${name} block missing`);
  assert.equal(src.indexOf(begin, a + 1), -1, `${file}: two ${name} blocks`);
  return src.slice(a, b + end.length);
}

function sameAcrossCopies(file, name) {
  const blocks = [`resources/viewer/${file}`, `web/drive/viewer/${file}`]
    .map(f => extractBlock(f, name));
  assert.equal(blocks[1], blocks[0],
    `${name} differs between resources/viewer/${file} and web/drive/viewer/${file}; ` +
    'run scripts/sync-drive-viewer.sh');
  return blocks[0];
}

// Python side, as plain JSON. Regexes travel as (source, ignoreCase).
const py = JSON.parse(execFileSync(PY, ['-c', `
import json, sys
sys.path.insert(0, ${JSON.stringify(ROOT)})
from cloud.chip_rules import rules_as_json
print(json.dumps(rules_as_json()))
`], { encoding: 'utf8' }));

const CATEGORIES = new Function(
  sameAcrossCopies('places.html', 'chip-rules') + '\nreturn CATEGORIES;')();
const EXPLORE_CHIPS = new Function(
  sameAcrossCopies('index.html', 'chip-rail') + '\nreturn EXPLORE_CHIPS;')();

let failures = 0;
function test(name, fn) {
  try { fn(); console.log('ok   ', name); }
  catch (e) { failures++; console.log('FAIL ', name, '\n     ', e.message.split('\n').slice(0, 6).join('\n      ')); }
}

const rxJson = r => r ? { source: r.source, i: r.flags.includes('i') } : null;

test('places.html CATEGORIES equals CHIP_RULES (order and every field)', () => {
  const js = CATEGORIES.map(c => ({
    id: c.id, label: c.label, cat: c.cat,
    subtypes: c.subtypes || [], includeRegex: rxJson(c.includeRegex),
    nameSubtypes: (c.nameInclude && c.nameInclude.subtypes) || [],
    namePattern: rxJson(c.nameInclude && c.nameInclude.pattern),
  }));
  assert.deepEqual(js, py);
});

test('index.html EXPLORE_CHIPS has the same ids and labels as CHIP_RULES', () => {
  const want = Object.fromEntries(py.map(c => [c.id, c.label]));
  const got = Object.fromEntries(EXPLORE_CHIPS.map(c => [c.id, c.label]));
  assert.equal(EXPLORE_CHIPS.length, Object.keys(got).length, 'duplicate id in EXPLORE_CHIPS');
  assert.deepEqual(got, want);
});

test('every EXPLORE_CHIPS entry has an emoji', () => {
  for (const c of EXPLORE_CHIPS) assert.ok(c.emoji, `${c.id} has no emoji`);
});

// Mirror of the legacy filter in places.html (the path taken when a ZIM has
// no per-chip files). Records reaching it are already limited to def.cat.
function jsMatches(def, r) {
  if (r.t !== def.cat) return false;
  if (!(def.subtypes && def.subtypes.length)) return true;
  if (def.subtypes.includes(r.s)) return true;
  if (def.includeRegex && r.s && def.includeRegex.test(r.s)) return true;
  const extra = def.nameInclude;
  if (extra && extra.subtypes && extra.pattern && extra.subtypes.includes(r.s)
      && extra.pattern.test(r.n || '')) return true;
  return false;
}

test('legacy JS filter agrees with record_matches_chip on a shared corpus', () => {
  const subtypes = new Set(['', 'attraction', 'tourism', 'viewpoint', 'unknown',
    'vietnamese_restaurant', 'food_truck', 'coffee_roaster', 'wine_store',
    'store', 'art_gallery', 'history_museum', 'hospital', 'dentist']);
  for (const c of py) for (const s of [...c.subtypes, ...c.nameSubtypes]) subtypes.add(s);
  const names = ['', 'Royal Observatory', 'National Museum', 'Museumsinsel',
    'City Gallery', 'Old Castle', 'Monument', 'Café Central', 'Planetarium',
    'Muséum national', 'Städtisches Museum', 'Музей', 'Library Bar'];
  const cats = [...new Set(py.map(c => c.cat))];
  const records = [];
  for (const t of cats) for (const s of subtypes) for (const n of names) records.push({ t, s, n });

  const pyOut = JSON.parse(execFileSync(PY, ['-c', `
import json, sys
sys.path.insert(0, ${JSON.stringify(ROOT)})
from cloud.chip_rules import CHIP_RULES, record_matches_chip
recs = json.load(sys.stdin)
out = {}
for c in CHIP_RULES:
    # parks keeps the whole category; see split_records_by_chip.
    whole = not c.subtypes and not c.include_regex
    out[c.id] = [(r["t"] == c.from_cat) if whole else record_matches_chip(r, c) for r in recs]
print(json.dumps(out))
`], { input: JSON.stringify(records), encoding: 'utf8', maxBuffer: 1 << 28 }));

  const diffs = [];
  for (const def of CATEGORIES) {
    records.forEach((r, i) => {
      const j = jsMatches(def, r);
      if (j !== pyOut[def.id][i]) diffs.push(`${def.id}: ${JSON.stringify(r)} js=${j} py=${pyOut[def.id][i]}`);
    });
  }
  assert.deepEqual(diffs, [], `${diffs.length} disagreement(s):\n${diffs.slice(0, 10).join('\n')}`);
});

console.log(failures ? `\n${failures} chip-rules check(s) FAILED` : '\nall chip-rules checks passed');
process.exit(failures ? 1 : 0);
