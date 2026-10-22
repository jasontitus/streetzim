// build-proxy/merged-webseed.js — a language version served virtually from
// its segment index (streetzim-webseed/1), the torrents' webseed.
//
//   node tests/build_proxy_merged_js.test.mjs
//
// Two local servers on 127.0.0.1 stand in for the site (index + blobs under
// /build/recipes/<region>/) and archive.org (/download/<item>/<file>
// redirecting to /node/<item>/<file>, which answers Range with 206). Checks
// every kind of range against the bytes the index describes, HEAD, 416/400,
// a stale UUID, a blob that is not the one the index names, an upstream that
// ignores Range, and the subrequest count (the storage node remembered).
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import http from 'node:http';
import { createMergedHandler, parseRange, slicesFor, checkIndex } from '../build-proxy/merged-webseed.js';

const rnd = (n, seed) => {
  const out = new Uint8Array(n);
  let x = seed >>> 0;
  for (let i = 0; i < n; i++) { x = (x * 1664525 + 1013904223) >>> 0; out[i] = x >>> 24; }
  return out;
};
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');

const A = rnd(300000, 1), B = rnd(200000, 2), X = rnd(1016, 3), Y = rnd(50, 4);
const UUID = '418f0e4d-684b-55b8-a110-d06933e1a0eb';
const FILE = 'osm_de_testland_2026-10-13.zim';
const blobFile = (h) => `ws-${h.slice(0, 32)}.bin`;
const index = {
  format: 'streetzim-webseed/1', region: 'testland', lang: 'de', uuid: UUID, file: FILE,
  md5: 'c2a3c8a72913fe5f3c0302f39df2fcec',
  sources: {
    base: { item: 'streetzim-testland', file: 'osm-testland-2026-10-13.zim', size: A.length },
    de: { item: 'streetzim-testland-lang', file: 'osm_testland_wiki-de_2026-10-13.zim', size: B.length },
  },
  blobs: { [sha(X)]: { file: blobFile(sha(X)), size: X.length }, [sha(Y)]: { file: blobFile(sha(Y)), size: Y.length } },
  parts: [
    [0, 1000, 'x', sha(X), 0],
    [1000, 250000, 's', 'base', 10000],
    [251000, 50, 'x', sha(Y), 0],
    [251050, 200000, 's', 'de', 0],
    [451050, 16, 'x', sha(X), 1000],
  ],
};
index.size = 451066;
const expected = new Uint8Array(index.size);
expected.set(X.subarray(0, 1000), 0);
expected.set(A.subarray(10000, 260000), 1000);
expected.set(Y, 251000);
expected.set(B, 251050);
expected.set(X.subarray(1000, 1016), 451050);

// ---------- the stand-ins ----------
const siteFiles = new Map([
  ['/build/recipes/testland/de.ws.json', Buffer.from(JSON.stringify(index))],
  [`/build/recipes/testland/${blobFile(sha(X))}`, Buffer.from(X)],
  [`/build/recipes/testland/${blobFile(sha(Y))}`, Buffer.from(Y)],
]);
const upFiles = new Map([
  ['streetzim-testland/osm-testland-2026-10-13.zim', A],
  ['streetzim-testland-lang/osm_testland_wiki-de_2026-10-13.zim', B],
]);
const hits = { site: 0, download: 0, node: 0 };
let ignoreRange = false;

function listen(handler) {
  return new Promise((resolve) => {
    const s = http.createServer(handler);
    s.listen(0, '127.0.0.1', () => resolve(s));
  });
}
const site = await listen((req, res) => {
  hits.site++;
  const body = siteFiles.get(req.url);
  if (!body) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Length': body.length });
  res.end(body);
});
const up = await listen((req, res) => {
  let m;
  if ((m = /^\/download\/(.+)$/.exec(req.url))) {
    hits.download++;
    res.writeHead(302, { Location: `http://127.0.0.1:${up.address().port}/node/${m[1]}` });
    res.end();
    return;
  }
  m = /^\/node\/(.+)$/.exec(req.url);
  const data = m && upFiles.get(m[1]);
  hits.node++;
  if (!data) { res.writeHead(404); res.end(); return; }
  const r = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range || '');
  if (!r || ignoreRange) { res.writeHead(200, { 'Content-Length': data.length }); res.end(Buffer.from(data)); return; }
  const a = Number(r[1]), b = Math.min(Number(r[2]), data.length - 1);
  res.writeHead(206, { 'Content-Range': `bytes ${a}-${b}/${data.length}`, 'Content-Length': b - a + 1 });
  res.end(Buffer.from(data.subarray(a, b + 1)));
});
const SITE = `http://127.0.0.1:${site.address().port}`;
const UP = `http://127.0.0.1:${up.address().port}/download/`;

const events = [];
let handle = createMergedHandler({ site: SITE, upstream: UP, stats: (e) => events.push(e) });
const urlFor = (uuid = UUID, file = FILE, region = 'testland', lang = 'de') =>
  `http://w.test/merged/${region}/${lang}/${uuid}/${file}`;
async function get(range, opts = {}) {
  const headers = range ? { Range: range } : {};
  const res = await handle(new Request(opts.url || urlFor(), { method: opts.method || 'GET', headers }), null);
  return res;
}
const settle = () => new Promise((r) => setTimeout(r, 20));

// ---------- pure helpers ----------
assert.deepEqual(parseRange('bytes=0-9', 100), [0, 9]);
assert.deepEqual(parseRange('bytes=90-', 100), [90, 99]);
assert.deepEqual(parseRange('bytes=-10', 100), [90, 99]);
assert.deepEqual(parseRange('bytes=95-200', 100), [95, 99]);
assert.equal(parseRange('bytes=100-', 100), 'unsatisfiable');
assert.equal(parseRange('bytes=0-1,4-5', 100), 'bad');
assert.equal(parseRange('bytes=9-1', 100), 'bad');
assert.equal(parseRange(null, 100), null);
assert.deepEqual(slicesFor(index, 999, 1001).map((s) => [s.part[0], s.lo, s.hi]), [[0, 999, 1000], [1000, 0, 2]]);
assert.equal(checkIndex(index, 'testland', 'de', UUID, FILE), '');
assert.equal(checkIndex(index, 'testland', 'de', UUID.replace('4', '5'), FILE), 'stale');
assert.equal(checkIndex({ ...index, parts: index.parts.slice(1) }, 'testland', 'de', UUID, FILE), 'index parts do not add up');
{ // lengths still add up to the size, but a part starts in the wrong place
  const parts = index.parts.map((p) => p.slice());
  parts[1][0] += 1; parts[2][0] -= 1;
  assert.equal(checkIndex({ ...index, parts }, 'testland', 'de', UUID, FILE), 'index parts do not add up');
}
assert.equal(checkIndex(index, 'testland', 'de', UUID, 'other.zim'), 'stale');
assert.equal(checkIndex({ ...index, sources: { ...index.sources, base: { ...index.sources.base, item: 'other' } } },
  'testland', 'de', UUID, FILE), 'bad source part');

// ---------- whole file ----------
let res = await get(null);
assert.equal(res.status, 200);
assert.equal(res.headers.get('content-length'), String(index.size));
assert.equal(res.headers.get('accept-ranges'), 'bytes');
assert.equal(res.headers.get('access-control-allow-origin'), '*');
assert.deepEqual(new Uint8Array(await res.arrayBuffer()), expected);
await settle();
const first = events.at(-1);
// index 1 + blobs 2 + each source part 2 (archive.org's redirect, then the node)
assert.equal(first.sub, 1 + 2 + 2 + 2, JSON.stringify(first));
assert.equal(first.parts, 5);

// ---------- ranges ----------
for (const [a, b] of [[0, 0], [999, 1000], [1000, 250999], [250990, 251060], [451049, 451065], [451065, 451065], [5, 451060]]) {
  res = await get(`bytes=${a}-${b}`);
  assert.equal(res.status, 206, `${a}-${b}`);
  assert.equal(res.headers.get('content-range'), `bytes ${a}-${b}/${index.size}`);
  assert.equal(res.headers.get('content-length'), String(b - a + 1));
  assert.deepEqual(new Uint8Array(await res.arrayBuffer()), expected.subarray(a, b + 1), `${a}-${b}`);
}
let seed = 9;
for (let i = 0; i < 40; i++) {
  seed = (seed * 1103515245 + 12345) >>> 0;
  const a = seed % index.size;
  seed = (seed * 1103515245 + 12345) >>> 0;
  const b = Math.min(index.size - 1, a + (seed % 300000));
  res = await get(`bytes=${a}-${b}`);
  assert.deepEqual(new Uint8Array(await res.arrayBuffer()), expected.subarray(a, b + 1), `random ${a}-${b}`);
}
res = await get('bytes=-100');
assert.equal(res.headers.get('content-range'), `bytes ${index.size - 100}-${index.size - 1}/${index.size}`);
assert.deepEqual(new Uint8Array(await res.arrayBuffer()), expected.subarray(index.size - 100));

// a source part after the first: the storage node is remembered, one subrequest
await settle();
events.length = 0;
res = await get('bytes=2000-3000');
await res.arrayBuffer();
await settle();
assert.equal(events.at(-1).sub, 1, JSON.stringify(events.at(-1)));     // index and node cached

// ---------- HEAD, errors ----------
res = await get('bytes=0-99', { method: 'HEAD' });
assert.equal(res.status, 206);
assert.equal(res.headers.get('content-length'), '100');
assert.equal(res.body, null);
res = await get(`bytes=${index.size}-`);
assert.equal(res.status, 416);
assert.equal(res.headers.get('content-range'), `bytes */${index.size}`);
res = await get('bytes=0-1,5-6');
assert.equal(res.status, 400);
res = await get(null, { url: urlFor(UUID.replace(/^4/, '5')) });         // the torrent of an older recipe
assert.equal(res.status, 404);
assert.match(await res.text(), /out of date/);
res = await get(null, { url: urlFor(UUID, 'osm_de_other_2026-10-13.zim') });   // the same UUID, another file name
assert.equal(res.status, 404);
res = await get(null, { url: urlFor(UUID, FILE, 'otherland') });          // no index there
assert.equal(res.status, 404);
res = await get(null, { url: 'http://w.test/merged/testland/de/not-a-uuid/x.zim' });
assert.equal(res.status, 404);
res = await handle(new Request(urlFor(), { method: 'POST' }), null);
assert.equal(res.status, 405);
res = await handle(new Request(urlFor(), { method: 'OPTIONS' }), null);
assert.equal(res.status, 204);

// a blob that is not the one the index names: the stream fails, no wrong bytes
handle = createMergedHandler({ site: SITE, upstream: UP, stats: (e) => events.push(e) });
siteFiles.set(`/build/recipes/testland/${blobFile(sha(Y))}`, Buffer.from(rnd(50, 99)));
res = await get('bytes=250990-251060');
await assert.rejects(res.arrayBuffer());
await settle();
assert.match(events.at(-1).error, /not the one the index names/);
siteFiles.set(`/build/recipes/testland/${blobFile(sha(Y))}`, Buffer.from(Y));

// an upstream that ignores Range would stream the whole file: refused
handle = createMergedHandler({ site: SITE, upstream: UP, stats: (e) => events.push(e) });
ignoreRange = true;
res = await get('bytes=1000-2000');
await assert.rejects(res.arrayBuffer());
await settle();
assert.match(events.at(-1).error, /HTTP 200/);
ignoreRange = false;

// a malformed index: 502, nothing served
siteFiles.set('/build/recipes/testland/de.ws.json', Buffer.from(JSON.stringify({ ...index, size: index.size + 1 })));
handle = createMergedHandler({ site: SITE, upstream: UP });
res = await get(null);
assert.equal(res.status, 502);

site.close(); up.close();
console.log('build_proxy_merged_js: all checks passed');
