// Tests for the `chip-shards` block shared by resources/viewer/index.html
// and places.html (the viewer side of cloud/chip_shards.py).
//
//   node tests/chip_shards_js.test.mjs
//
// Shards come from the real Python planner (PYTHON env, default the
// build venv) so the viewer is tested against the layout the build emits.
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const PY = process.env.PYTHON || '/storage/streetzim/venv-linux/bin/python3';

function extractBlock(file) {
  const src = readFileSync(join(ROOT, file), 'utf8');
  const a = src.indexOf('// BEGIN chip-shards');
  const b = src.indexOf('// END chip-shards');
  assert.ok(a >= 0 && b > a, `${file}: chip-shards block missing`);
  return src.slice(a, b + '// END chip-shards'.length);
}

const blocks = ['resources/viewer/index.html', 'resources/viewer/places.html',
                'web/drive/viewer/index.html', 'web/drive/viewer/places.html']
  .map(extractBlock);
for (const b of blocks.slice(1)) {
  assert.equal(b, blocks[0], 'chip-shards blocks differ between viewer files');
}
const CHIP_SHARDS = new Function(blocks[0] + '\nreturn CHIP_SHARDS;')();
const { distKm, _test: T } = CHIP_SHARDS;

let failures = 0;
async function test(name, fn) {
  try { await fn(); console.log('ok   ', name); }
  catch (e) { failures++; console.log('FAIL ', name, '\n     ', e.stack.split('\n').slice(0, 3).join('\n      ')); }
}

// Deterministic PRNG.
function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}

// Build a chip with the Python planner; returns {meta, files}.
function plan(records, targetBytes) {
  const dir = mkdtempSync(join(tmpdir(), 'chipshards-'));
  try {
    writeFileSync(join(dir, 'in.json'), JSON.stringify(records));
    const out = execFileSync(PY, ['-c', `
import json, sys
sys.path.insert(0, ${JSON.stringify(ROOT)})
from cloud.chip_shards import plan_chip
recs = json.load(open(${JSON.stringify(join(dir, 'in.json'))}))
p = plan_chip(recs, ${targetBytes})
files = {path: blob.decode("utf-8") for path, _t, blob in p.files("c", "C")}
print(json.dumps({"meta": p.manifest_entry("C"), "files": files}))
`], { maxBuffer: 1 << 30 });
    return JSON.parse(out);
  } finally { rmSync(dir, { recursive: true, force: true }); }
}

function fetcherFor(files, log) {
  return (suffix) => {
    if (log) log.push(suffix);
    const body = files[`category-index/chip-c-${suffix}.json`];
    if (body === undefined) return Promise.reject(new Error('404 ' + suffix));
    return Promise.resolve(JSON.parse(body));
  };
}

function brute(records, { point, bbox, predicate, k }) {
  const nb = T.normBbox(bbox);
  return records
    .filter(r => (!nb || T.bboxHasPoint(nb, r.a, r.o)) && (!predicate || predicate(r)))
    .map(r => ({ r, d: distKm(point.lat, point.lon, r.a, r.o) }))
    .sort((x, y) => x.d - y.d)
    .slice(0, k);
}

function makeRecords(seed, n, gen) {
  const R = rng(seed);
  const out = [];
  for (let i = 0; i < n; i++) {
    const [a, o] = gen(R, i);
    out.push({ n: `P${i}`, t: 'poi', s: i % 7 === 0 ? 'bakery' : 'restaurant',
               a: +a.toFixed(6), o: +o.toFixed(6), l: 'x'.repeat(20) });
  }
  return out;
}

function sameDistances(got, want, msg) {
  assert.equal(got.records.length, want.length, msg + ': count');
  for (let i = 0; i < want.length; i++) {
    assert.ok(Math.abs(got.dists[i] - want[i].d) < 1e-9,
      `${msg}: #${i} got ${got.dists[i]} want ${want[i].d}`);
  }
}

// ---------------------------------------------------------------------

await test('lower bound never exceeds a true distance (high lat, antimeridian)', () => {
  const R = rng(1);
  for (let t = 0; t < 20000; t++) {
    const s = -89 + R() * 170, n = Math.min(90, s + R() * 30);
    const w = -180 + R() * 360, width = R() * (R() < 0.2 ? 350 : 20);
    const e = ((w + width + 180) % 360) - 180;
    const plat = -90 + R() * 180, plon = -540 + R() * 1080;  // unwrapped too
    const lb = T.boxLowerBoundKm(plat, ((plon % 360) + 540) % 360 - 180, s, w, n, e);
    for (let q = 0; q < 4; q++) {
      const lat = s + R() * (n - s);
      const lon = ((w + R() * width + 180) % 360) - 180;
      const d = distKm(plat, plon, lat, lon);
      assert.ok(lb <= d + 1e-9, `lb ${lb} > d ${d} box ${[s, w, n, e]} p ${[plat, plon]} q ${[lat, lon]}`);
    }
  }
});

await test('bbox normalisation: unwrapped, wrapped, and 360+ spans', () => {
  const fiji = T.normBbox({ s: -20, w: 170, n: -15, e: 190 });
  assert.ok(T.bboxHasPoint(fiji, -17, -179));
  assert.ok(T.bboxHasPoint(fiji, -17, 175));
  assert.ok(!T.bboxHasPoint(fiji, -17, 0));
  const world = T.normBbox({ s: -80, w: -250, n: 80, e: 110 });
  assert.ok(world.all && T.bboxHasPoint(world, 0, 150));
  // shard crossing ±180 vs unwrapped viewport, and vs plain viewport
  const shardWrap = [-20, 178, -15, -178, 1, 1];
  assert.ok(T.bboxMeetsShard(fiji, shardWrap));
  assert.ok(T.bboxMeetsShard(T.normBbox({ s: -20, w: -179, n: -15, e: -170 }), shardWrap));
  assert.ok(!T.bboxMeetsShard(T.normBbox({ s: -20, w: -170, n: -15, e: -160 }), shardWrap));
  // viewport crossing ±180 vs plain shard on either side
  assert.ok(T.bboxMeetsShard(fiji, [-20, -179.5, -15, -179, 1, 1]));
  assert.ok(!T.bboxMeetsShard(fiji, [-20, 100, -15, 120, 1, 1]));
  // viewport entirely inside a shard (neither contains the other's start
  // unless the check is two-sided)
  assert.ok(T.bboxMeetsShard(T.normBbox({ s: 0, w: 10, n: 1, e: 11 }), [-5, 0, 5, 20, 1, 1]));
  // 180 and -180 are the same meridian on both sides of the comparison
  assert.ok(T.bboxMeetsShard(T.normBbox({ s: 0, w: -180, n: 1, e: -170 }), [0, 180, 1, 180, 1, 1]));
  assert.ok(T.bboxMeetsShard(T.normBbox({ s: 0, w: -180, n: 1, e: -179 }), [0, 170, 1, 180, 1, 1]));
  assert.ok(T.bboxHasPoint(T.normBbox({ s: 0, w: -180, n: 1, e: -179 }), 0.5, 180));
  assert.equal(T.boxLowerBoundKm(0.5, -180, 0, 170, 1, 180), 0);
});

const scenarios = [
  ['mid-latitude region', 2, (R) => [30 + R() * 15, -100 + R() * 30]],
  ['high latitude (Russia/Canada)', 3, (R) => [55 + R() * 30, 20 + R() * 150]],
  ['antimeridian (Chukotka/Aleutians)', 4,
    (R) => [50 + R() * 20, R() < 0.5 ? 170 + R() * 10 : -180 + R() * 15]],
  ['dense city + sparse rest', 5,
    (R, i) => i % 3 ? [40.7 + (R() - 0.5) * 0.1, -74 + (R() - 0.5) * 0.1]
                    : [25 + R() * 20, -90 + R() * 20]],
];

for (const [label, seed, gen] of scenarios) {
  const records = makeRecords(seed, 6000, gen);
  const { meta, files } = plan(records, 24 * 1024);
  assert.equal(meta.layout, 'geo');
  const R = rng(seed * 101);
  await test(`kNN exact vs brute force: ${label}`, async () => {
    for (let t = 0; t < 25; t++) {
      const pick = records[Math.floor(R() * records.length)];
      const point = t % 5 === 4
        ? { lat: -60 + R() * 150, lon: -180 + R() * 360 }       // often outside the data
        : { lat: pick.a + (R() - 0.5), lon: pick.o + (R() - 0.5) };
      const k = [1, 10, 300][t % 3];
      const predicate = t % 4 === 1 ? (r) => r.s === 'bakery' : null;
      const got = await CHIP_SHARDS.load(meta, {
        fetchShard: fetcherFor(files), point, k, predicate, budgetBytes: 1 << 30 });
      assert.ok(!got.partial);
      sameDistances(got, brute(records, { point, predicate, k }), `t${t}`);
    }
  });
  await test(`kNN inside a viewport: ${label}`, async () => {
    for (let t = 0; t < 20; t++) {
      const c = records[Math.floor(R() * records.length)];
      const half = 0.05 + R() * 3;
      const shift = t % 2 ? 360 : 0;   // MapLibre world copy
      const bbox = { s: c.a - half, n: c.a + half, w: c.o - half * 2 + shift, e: c.o + half * 2 + shift };
      const point = { lat: c.a, lon: c.o + shift };
      const got = await CHIP_SHARDS.load(meta, {
        fetchShard: fetcherFor(files), point, bbox, k: 300, budgetBytes: 1 << 30 });
      sameDistances(got, brute(records, { point, bbox, k: 300 }), `t${t}`);
    }
  });
}

await test('budget cap → partial, and results are exact within radiusKm', async () => {
  const records = makeRecords(8, 8000, (R) => [40 + R() * 10, -80 + R() * 10]);
  const { meta, files } = plan(records, 16 * 1024);
  const point = { lat: 45, lon: -75 };
  const predicate = (r) => r.s === 'bakery';
  const got = await CHIP_SHARDS.load(meta, {
    fetchShard: fetcherFor(files), point, predicate, k: 300, budgetBytes: 40 * 1024 });
  assert.ok(got.partial, 'expected partial');
  assert.ok(got.bytesLoaded <= 40 * 1024);
  assert.ok(got.radiusKm > 0 && got.radiusKm < Infinity);
  const want = brute(records, { point, predicate, k: 100000 }).filter(x => x.d < got.radiusKm);
  const gotNames = new Set(got.records.map(r => r.n));
  for (const x of want.slice(0, got.records.length)) {
    assert.ok(gotNames.has(x.r.n), `missing ${x.r.n} at ${x.d} < radius ${got.radiusKm}`);
  }
});

await test('no matches under a budget stop: partial, empty, not failed', async () => {
  const records = makeRecords(12, 6000, (R) => [40 + R() * 10, -80 + R() * 10]);
  const { meta, files } = plan(records, 16 * 1024);
  const got = await CHIP_SHARDS.load(meta, {
    fetchShard: fetcherFor(files), point: { lat: 45, lon: -75 },
    predicate: (r) => r.n === 'nope', k: 300, budgetBytes: 40 * 1024 });
  assert.equal(got.records.length, 0);
  assert.ok(got.partial && !got.failed, JSON.stringify({ p: got.partial, f: got.failed }));
});

await test('dense city: the first shard alone answers when it can', async () => {
  const R0 = rng(13);
  const records = makeRecords(13, 8000, (R, i) => i < 4000
    ? [40.75 + (R0() - 0.5) * 0.02, -73.99 + (R0() - 0.5) * 0.02]
    : [30 + R() * 15, -95 + R() * 20]);
  const { meta, files } = plan(records, 64 * 1024);
  assert.ok(meta.shards.length > 8, `only ${meta.shards.length} shards`);
  // A point ON a record: the first shard yields distance 0, which no
  // other shard's lower bound can beat, so nothing else may be fetched.
  // (A point on a k-d boundary legitimately needs a neighbour too.)
  for (const idx of [5, 1234, 3999]) {
    const point = { lat: records[idx].a, lon: records[idx].o };
    const log = [];
    const got = await CHIP_SHARDS.load(meta, { fetchShard: fetcherFor(files, log), point, k: 1 });
    assert.equal(log.length, 1, `record ${idx}: fetched ${log.length} shards`);
    assert.ok(got.dists[0] < 1e-6, `record ${idx}: nearest at ${got.dists[0]} km`);
  }
});

await test('fetch failure → partial, not cached; retry succeeds', async () => {
  const records = makeRecords(9, 4000, (R) => [10 + R() * 5, 10 + R() * 5]);
  const { meta, files } = plan(records, 16 * 1024);
  const cache = CHIP_SHARDS.makeCache(1 << 30);
  const point = { lat: 12.5, lon: 12.5 };
  let failOnce = true;
  const flaky = (suffix) => {
    if (failOnce) { failOnce = false; return Promise.reject(new Error('io')); }
    return fetcherFor(files)(suffix);
  };
  const a = await CHIP_SHARDS.load(meta, { fetchShard: flaky, cache, cacheKey: 'c', point, k: 300 });
  assert.ok(a.partial && a.failed, 'first load should be partial and failed');
  const b = await CHIP_SHARDS.load(meta, { fetchShard: flaky, cache, cacheKey: 'c', point, k: 300 });
  assert.ok(!b.partial, 'second load should not reuse the failure');
  sameDistances(b, brute(records, { point, k: 300 }), 'retry');
});

await test('cancellation stops further fetches and resolves null', async () => {
  const records = makeRecords(10, 6000, (R) => [0 + R() * 20, 0 + R() * 20]);
  const { meta, files } = plan(records, 8 * 1024);
  const log = [];
  let cancel = false;
  const fetchShard = (s) => { cancel = true; return fetcherFor(files, log)(s); };
  const res = await CHIP_SHARDS.load(meta, {
    fetchShard, point: { lat: 10, lon: 10 }, k: 5000, isCancelled: () => cancel });
  assert.equal(res, null);
  assert.ok(log.length <= 4, `fetched ${log.length} shards after cancel`);
});

await test('cache keys are namespaced per chip; LRU respects the cap', async () => {
  const cache = CHIP_SHARDS.makeCache(100);
  const a = await cache.get('cafes/g000', 60, () => ['cafe']);
  const b = await cache.get('bars/g000', 60, () => ['bar']);
  assert.deepEqual(a, ['cafe']);
  assert.deepEqual(b, ['bar']);
  let refetched = false;
  await cache.get('cafes/g000', 60, () => { refetched = true; return ['cafe']; });
  assert.ok(refetched, 'oldest entry should have been evicted at the cap');
});

await test('bbox with nothing inside falls back to nearest when asked', async () => {
  const records = makeRecords(11, 3000, (R) => [48 + R(), 2 + R()]);
  const { meta, files } = plan(records, 16 * 1024);
  const bbox = { s: 10, n: 11, w: 10, e: 11 };
  const point = { lat: 10.5, lon: 10.5 };
  const no = await CHIP_SHARDS.load(meta, { fetchShard: fetcherFor(files), point, bbox, k: 10 });
  assert.equal(no.records.length, 0);
  const yes = await CHIP_SHARDS.load(meta, {
    fetchShard: fetcherFor(files), point, bbox, k: 10, fallbackToNearest: true });
  assert.ok(yes.fellBack);
  sameDistances(yes, brute(records, { point, k: 10 }), 'fallback');
});

await test('legacy manifests are not geo', () => {
  assert.ok(!CHIP_SHARDS.isGeo({ sub_chunks: ['0', '1'], n_sub_buckets: 2 }));
  assert.ok(!CHIP_SHARDS.isGeo({ count: 3 }));
  assert.ok(!CHIP_SHARDS.isGeo({ layout: 'geo', sub_chunks: ['g000'], shards: [] }));
});

await test('centre handles antimeridian regions', () => {
  const c = CHIP_SHARDS.centre({ layout: 'geo', sub_chunks: ['a', 'b'],
    shards: [[60, 175, 62, 179, 10, 1], [60, -179, 62, -175, 10, 1]] });
  assert.ok(Math.abs(Math.abs(c.lon) - 180) < 1e-6, JSON.stringify(c));
});

if (failures) { console.log(`\n${failures} failure(s)`); process.exit(1); }
console.log('\nall chip-shards tests passed');
