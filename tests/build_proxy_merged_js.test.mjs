// build-proxy/ — a language version served virtually from its segment index
// (streetzim-webseed/1), the torrents' webseed, and worker.js's switches and
// daily meter.
//
//   node tests/build_proxy_merged_js.test.mjs
//
// Two local servers on 127.0.0.1 stand in for the site (index + blobs under
// /build/recipes/<region>/) and archive.org (/download/<item>/<file>
// redirecting to /node/<item>/<file>, which answers Range with 206 and
// honours If-Range against its ETag). Checks every kind of range against the
// bytes the index describes, RFC range handling, HEAD, a stale UUID or file
// (after asking the site itself), an edge-cached old index, site errors, a
// blob that is not the one the index names, upstream answers that are not
// exactly the asked bytes, If-Range both ways, the subrequest count, the
// blob cache (one fetch per blob, its byte count, eviction), the meter, and
// worker.js's switches and Durable Object meter.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import http from 'node:http';
import { createMergedHandler, parseRange, slicesFor, checkIndex, isolateMeter } from '../build-proxy/merged-webseed.js';
import worker, { flag, doMeter, WebseedMeter, _reset } from '../build-proxy/worker.js';

const rnd = (n, seed) => {
  const out = new Uint8Array(n);
  let x = seed >>> 0;
  for (let i = 0; i < n; i++) { x = (x * 1664525 + 1013904223) >>> 0; out[i] = x >>> 24; }
  return out;
};
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');

const A = rnd(300000, 1), B = rnd(200000, 2), X = rnd(1016, 3), Y = rnd(50, 4);
const UUID = '418f0e4d-684b-55b8-a110-d06933e1a0eb';
const UUID2 = '518f0e4d-684b-55b8-a110-d06933e1a0eb';
const FILE = 'osm_de_testland_2026-10-13.zim';
const blobFile = (h) => `ws-${h.slice(0, 32)}.bin`;
const ETAG_A = '"6ad42478-493e0"';
const index = {
  format: 'streetzim-webseed/1', region: 'testland', lang: 'de', uuid: UUID, file: FILE,
  md5: 'c2a3c8a72913fe5f3c0302f39df2fcec',
  sources: {
    base: { item: 'streetzim-testland', file: 'osm-testland-2026-10-13.zim', size: A.length, etag: ETAG_A },
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
const siteFiles = new Map();
const setIndex = (ix) => siteFiles.set('/build/recipes/testland/de.ws.json', Buffer.from(JSON.stringify(ix)));
setIndex(index);
siteFiles.set(`/build/recipes/testland/${blobFile(sha(X))}`, Buffer.from(X));
siteFiles.set(`/build/recipes/testland/${blobFile(sha(Y))}`, Buffer.from(Y));
let staleEdge = null;          // an index the "edge cache" still serves to plain (non-fresh) requests
const upFiles = new Map([
  ['streetzim-testland/osm-testland-2026-10-13.zim', { data: A, etag: ETAG_A }],
  ['streetzim-testland-lang/osm_testland_wiki-de_2026-10-13.zim', { data: B, etag: '"1-2"' }],
]);
const hits = { site: 0, download: 0, node: 0, blobs: 0 };
let upMode = 'ok';             // ok | ignore-range | shift | wrong-total | short-length | node404-once

function listen(handler) {
  return new Promise((resolve) => {
    const s = http.createServer(handler);
    s.listen(0, '127.0.0.1', () => resolve(s));
  });
}
const site = await listen((req, res) => {
  hits.site++;
  const u = new URL(req.url, 'http://x');
  if (u.pathname.endsWith('.bin')) hits.blobs++;
  let body = siteFiles.get(u.pathname);
  if (staleEdge && u.pathname.endsWith('.ws.json') && !u.searchParams.has('fresh')) body = Buffer.from(JSON.stringify(staleEdge));
  if (!body) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Length': body.length });
  res.end(body);
});
let downloadsSeen = 0;
const up = await listen((req, res) => {
  let m;
  if ((m = /^\/download\/(.+)$/.exec(req.url))) {
    hits.download++; downloadsSeen++;
    res.writeHead(302, { Location: `http://127.0.0.1:${up.address().port}/node/${m[1]}` });
    res.end();
    return;
  }
  m = /^\/node\/(.+)$/.exec(req.url);
  const f = m && upFiles.get(m[1]);
  hits.node++;
  if (upMode === 'node404-once') { upMode = 'ok'; res.writeHead(404); res.end(); return; }
  if (!f) { res.writeHead(404); res.end(); return; }
  const data = f.data;
  const r = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range || '');
  const ir = req.headers['if-range'];
  if (!r || upMode === 'ignore-range' || (ir && ir !== f.etag)) {
    res.writeHead(200, { 'Content-Length': data.length, ETag: f.etag }); res.end(Buffer.from(data)); return;
  }
  let a = Number(r[1]), b = Math.min(Number(r[2]), data.length - 1);
  let total = data.length;
  const len = b - a + 1;
  if (upMode === 'shift') { a += 1; b += 1; }
  if (upMode === 'wrong-total') total += 1;
  const sendLen = upMode === 'short-length' ? len - 1 : len;
  res.writeHead(206, { 'Content-Range': `bytes ${a}-${b}/${total}`, 'Content-Length': sendLen, ETag: f.etag });
  res.end(Buffer.from(data.subarray(a, a + sendLen)));
});
const SITE = `http://127.0.0.1:${site.address().port}`;
const UP = `http://127.0.0.1:${up.address().port}/download/`;

const events = [];
const mk = (extra = {}) => createMergedHandler({ site: SITE, upstream: UP, stats: (e) => events.push(e), ...extra });
let handle = mk();
const urlFor = (uuid = UUID, file = FILE, region = 'testland', lang = 'de') =>
  `http://w.test/merged/${region}/${lang}/${uuid}/${file}`;
async function get(range, opts = {}) {
  const headers = { ...(range ? { Range: range } : {}), ...(opts.headers || {}) };
  return (opts.handle || handle)(new Request(opts.url || urlFor(), { method: opts.method || 'GET', headers }), null);
}
const settle = () => new Promise((r) => setTimeout(r, 30));
const bytesOf = async (res) => new Uint8Array(await res.arrayBuffer());

// ---------- pure helpers ----------
assert.deepEqual(parseRange('bytes=0-9', 100), [0, 9]);
assert.deepEqual(parseRange('bytes=90-', 100), [90, 99]);
assert.deepEqual(parseRange('bytes=-10', 100), [90, 99]);
assert.deepEqual(parseRange('bytes=-1000', 100), [0, 99]);
assert.deepEqual(parseRange('bytes=95-200', 100), [95, 99]);
assert.deepEqual(parseRange('BYTES=1-2', 100), [1, 2]);
assert.equal(parseRange('bytes=100-', 100), 'unsatisfiable');
assert.equal(parseRange('bytes=-0', 100), 'unsatisfiable');
assert.equal(parseRange('bytes=0-1,4-5', 100), null);           // several ranges: ignored, the whole file
assert.equal(parseRange('items=0-5', 100), null);               // another unit: ignored
assert.equal(parseRange('bytes=9-1', 100), null);               // invalid: ignored
assert.equal(parseRange('bytes=-', 100), null);
assert.equal(parseRange(null, 100), null);
assert.deepEqual(slicesFor(index, 999, 1001).map((s) => [s.part[0], s.lo, s.hi]), [[0, 999, 1000], [1000, 0, 2]]);
assert.deepEqual(slicesFor(index, 1000, 1000).map((s) => [s.part[0], s.lo, s.hi]), [[1000, 0, 1]]);
assert.equal(checkIndex(index, 'testland', 'de', UUID, FILE), '');
assert.equal(checkIndex(index, 'testland', 'de', UUID2, FILE), 'stale');
assert.equal(checkIndex(index, 'testland', 'de', UUID, 'other.zim'), 'stale');
assert.equal(checkIndex(index, 'testland', 'fr', UUID, FILE), 'index is for another region or language');
assert.equal(checkIndex(index, 'otherland', 'de', UUID, FILE), 'index is for another region or language');
assert.equal(checkIndex({ ...index, parts: index.parts.slice(1) }, 'testland', 'de', UUID, FILE), 'index parts do not add up');
{ // lengths still add up to the size, but a part starts in the wrong place
  const parts = index.parts.map((p) => p.slice());
  parts[1][0] += 1; parts[2][0] -= 1;
  assert.equal(checkIndex({ ...index, parts }, 'testland', 'de', UUID, FILE), 'index parts do not add up');
}
assert.equal(checkIndex({ ...index, sources: { ...index.sources, base: { ...index.sources.base, item: 'other' } } },
  'testland', 'de', UUID, FILE), 'bad source part');
{ // a source range past the file's end; a blob range past the blob's end; a blob name that is not ws-<hex>.bin
  const parts = index.parts.map((p) => p.slice());
  parts[1][4] = A.length - 250000 + 1;
  assert.equal(checkIndex({ ...index, parts }, 'testland', 'de', UUID, FILE), 'bad source part');
  const parts2 = index.parts.map((p) => p.slice());
  parts2[4][4] = 1001;
  assert.equal(checkIndex({ ...index, parts: parts2 }, 'testland', 'de', UUID, FILE), 'bad blob part');
  const blobs = { ...index.blobs, [sha(Y)]: { file: '../ws-x.bin', size: Y.length } };
  assert.equal(checkIndex({ ...index, blobs }, 'testland', 'de', UUID, FILE), 'bad blob part');
}

// ---------- whole file ----------
let res = await get(null);
assert.equal(res.status, 200);
assert.equal(res.headers.get('content-length'), String(index.size));
assert.equal(res.headers.get('accept-ranges'), 'bytes');
assert.equal(res.headers.get('access-control-allow-origin'), '*');
assert.equal(res.headers.get('vary'), 'Range');
assert.deepEqual(await bytesOf(res), expected);
await settle();
// index 1 + blobs 2 + each source part 2 (archive.org's redirect, then the node)
assert.equal(events.at(-1).sub, 1 + 2 + 2 + 2, JSON.stringify(events.at(-1)));
assert.equal(events.at(-1).parts, 5);
res = await get('bytes=0-1,5-9');                                  // several ranges: the whole file
assert.equal(res.status, 200);
assert.deepEqual(await bytesOf(res), expected);

// ---------- ranges ----------
for (const [a, b] of [[0, 0], [999, 1000], [1000, 250999], [250990, 251060], [451049, 451065], [451065, 451065], [5, 451060]]) {
  res = await get(`bytes=${a}-${b}`);
  assert.equal(res.status, 206, `${a}-${b}`);
  assert.equal(res.headers.get('content-range'), `bytes ${a}-${b}/${index.size}`);
  assert.equal(res.headers.get('content-length'), String(b - a + 1));
  assert.deepEqual(await bytesOf(res), expected.subarray(a, b + 1), `${a}-${b}`);
}
let seed = 9;
for (let i = 0; i < 40; i++) {
  seed = (seed * 1103515245 + 12345) >>> 0;
  const a = seed % index.size;
  seed = (seed * 1103515245 + 12345) >>> 0;
  const b = Math.min(index.size - 1, a + (seed % 300000));
  res = await get(`bytes=${a}-${b}`);
  assert.deepEqual(await bytesOf(res), expected.subarray(a, b + 1), `random ${a}-${b}`);
}
res = await get('bytes=-100');
assert.equal(res.headers.get('content-range'), `bytes ${index.size - 100}-${index.size - 1}/${index.size}`);
assert.deepEqual(await bytesOf(res), expected.subarray(index.size - 100));

// a source part after the first: the storage node is remembered, one subrequest
await settle();
res = await get('bytes=2000-3000');
await res.arrayBuffer();
await settle();
assert.equal(events.at(-1).sub, 1, JSON.stringify(events.at(-1)));     // index and node cached
// the remembered node fails once: back to archive.org's download URL
upMode = 'node404-once';
const d0 = downloadsSeen;
res = await get('bytes=2000-3000');
assert.deepEqual(await bytesOf(res), expected.subarray(2000, 3001));
await settle();
assert.equal(downloadsSeen, d0 + 1);
assert.equal(events.at(-1).sub, 1 + 2, JSON.stringify(events.at(-1)));

// ---------- client If-Range ----------
res = await get('bytes=0-9', { headers: { 'If-Range': `"${UUID}-${index.md5}"` } });
assert.equal(res.status, 206);
await res.arrayBuffer();
res = await get('bytes=0-9', { headers: { 'If-Range': '"something-else"' } });
assert.equal(res.status, 200);                                     // another version: the whole file
assert.equal((await bytesOf(res)).length, index.size);

// ---------- HEAD, errors ----------
res = await get('bytes=0-99', { method: 'HEAD' });
assert.equal(res.status, 206);
assert.equal(res.headers.get('content-length'), '100');
assert.equal(res.body, null);
res = await get(null, { method: 'HEAD' });
assert.equal(res.status, 200);
assert.equal(res.headers.get('content-length'), String(index.size));
res = await get(`bytes=${index.size}-`);
assert.equal(res.status, 416);
assert.equal(res.headers.get('content-range'), `bytes */${index.size}`);
res = await get('bytes=-0');
assert.equal(res.status, 416);
const s0 = hits.site;
res = await get(null, { url: urlFor(UUID2) });                       // the torrent of another recipe
assert.equal(res.status, 404);
assert.match(await res.text(), /out of date/);
assert.ok(hits.site > s0, 'asked the site again before answering 404');
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

// a new recipe published while an isolate holds the old index (and the edge
// cache still serves the old one): the new torrent works at once, the old
// one gets 404
setIndex({ ...index, uuid: UUID2 });
staleEdge = index;
res = await get('bytes=0-9', { url: urlFor(UUID2) });
assert.equal(res.status, 206);
assert.deepEqual(await bytesOf(res), expected.subarray(0, 10));
res = await get('bytes=0-9', { url: urlFor(UUID) });
assert.equal(res.status, 404);
staleEdge = null;
setIndex(index);
// a fresh isolate whose first answer is an edge-cached old index
staleEdge = { ...index, uuid: UUID2 };
res = await get('bytes=0-9', { handle: mk() });
assert.equal(res.status, 206, 'an edge-cached old index does not make the current torrent "out of date"');
await res.arrayBuffer();
staleEdge = null;
// an index for another language at this path: 502, not 404
setIndex({ ...index, lang: 'fr' });
res = await get(null, { handle: mk() });
assert.equal(res.status, 502);
setIndex(index);
// the index is revalidated after its TTL
{
  let t = 1e12;
  const h = mk({ now: () => t });
  await (await get('bytes=0-9', { handle: h })).arrayBuffer();
  const s1 = hits.site;
  await (await get('bytes=0-9', { handle: h })).arrayBuffer();
  assert.equal(hits.site, s1, 'within the TTL: cached');
  t += 6 * 60 * 1000;
  await (await get('bytes=0-9', { handle: h })).arrayBuffer();
  assert.equal(hits.site, s1 + 1, 'after the TTL: asked again');
}

// the site does not answer: one retry, then 503 with Retry-After (not 404)
{
  let fails = 2;
  const flaky = (url, init) => (url.startsWith(SITE) && fails-- > 0 ? Promise.reject(new Error('ECONNRESET')) : fetch(url, init));
  res = await get('bytes=0-9', { handle: mk({ fetchImpl: flaky }) });
  assert.equal(res.status, 503);
  assert.equal(res.headers.get('retry-after'), '30');
  fails = 1;
  res = await get('bytes=0-9', { handle: mk({ fetchImpl: flaky }) });
  assert.equal(res.status, 206, 'one transient site error is retried');
  await res.arrayBuffer();
}

// a blob that is not the one the index names: the stream fails, no wrong bytes
handle = mk();
siteFiles.set(`/build/recipes/testland/${blobFile(sha(Y))}`, Buffer.from(rnd(50, 99)));
res = await get('bytes=250990-251060');
await assert.rejects(res.arrayBuffer());
await settle();
assert.match(events.at(-1).error, /not the one the index names/);
siteFiles.set(`/build/recipes/testland/${blobFile(sha(Y))}`, Buffer.from(Y));

// archive.org answers that are not exactly these bytes of this file: refused
for (const [mode, why] of [['ignore-range', /HTTP 200/], ['shift', /HTTP 206 bytes 10001-11001\/300000/], ['wrong-total', /\/300001/],
  ['short-length', /length 1000/]]) {
  handle = mk();
  upMode = mode;
  res = await get('bytes=1000-2000');
  await assert.rejects(res.arrayBuffer(), mode);
  await settle();
  assert.match(events.at(-1).error, why, mode);
  upMode = 'ok';
}
// If-Range: the index's ETag no longer matches archive.org's file -> 200 -> refused
handle = mk();
upFiles.get('streetzim-testland/osm-testland-2026-10-13.zim').etag = '"6ad42479-493e0"';
res = await get('bytes=1000-2000');
await assert.rejects(res.arrayBuffer());
await settle();
assert.match(events.at(-1).error, /HTTP 200/);
upFiles.get('streetzim-testland/osm-testland-2026-10-13.zim').etag = ETAG_A;
res = await get('bytes=1000-2000');
assert.deepEqual(await bytesOf(res), expected.subarray(1000, 2001));

// ---------- the blob cache ----------
handle = mk();
const b0 = hits.blobs;
const rs = await Promise.all([1, 2, 3, 4].map(() => get('bytes=0-10')));
for (const r of rs) assert.deepEqual(await bytesOf(r), expected.subarray(0, 11));
assert.equal(hits.blobs, b0 + 1, 'concurrent misses share one fetch');
assert.equal(handle.state().blobCacheBytes, X.length, 'counted once');
res = await get('bytes=0-10');
await res.arrayBuffer();
assert.equal(hits.blobs, b0 + 1);
assert.deepEqual(await bytesOf(await get('bytes=0-999')), X.subarray(0, 1000), 'the cached blob is intact');
handle = mk({ blobCacheBytes: 1020 });                               // X (1016) + Y (50) does not fit
await (await get('bytes=0-10')).arrayBuffer();
await (await get('bytes=251000-251049')).arrayBuffer();
assert.equal(handle.state().blobs, 1);
assert.ok(handle.state().blobCacheBytes <= 1020, JSON.stringify(handle.state()));

// ---------- the meter ----------
{
  let calls = 0;
  const h = mk({ meter: { allow: () => { calls++; return calls <= 1; } } });
  res = await get('bytes=0-9', { handle: h });
  assert.equal(res.status, 206);
  await res.arrayBuffer();
  const s2 = hits.site;
  res = await get('bytes=0-9', { handle: h });
  assert.equal(res.status, 503);
  assert.equal(res.headers.get('retry-after'), '3600');
  assert.equal(hits.site, s2, 'over budget: nothing fetched');
  const im = isolateMeter(2);
  assert.deepEqual([im.allow(), im.allow(), im.allow()], [true, true, false]);
  assert.equal(isolateMeter(0).allow(), true);
}

// ---------- worker.js: switches and the Durable Object meter ----------
for (const [v, d, want] of [['1', false, true], ['true', false, true], ['YES', false, true], ['on', false, true],
  ['0', true, false], ['false', true, false], ['No', true, false], ['off', true, false], ['', true, true],
  [undefined, false, false], ['maybe', true, true]]) assert.equal(flag(v, d), want, `flag(${v})`);
const env = { SITE_ORIGIN: SITE, UPSTREAM: UP };
const call = (path, e) => { _reset(); return worker.fetch(new Request(`http://w.test${path}`, { headers: { Range: 'bytes=0-9' } }), e, null); };
const mpath = new URL(urlFor()).pathname;
res = await call(mpath, { ...env, OFF: 'true' });
assert.equal(res.status, 503);
res = await call(mpath, { ...env, OFF: '0' });
assert.equal(res.status, 206);
await res.arrayBuffer();
res = await call(mpath, { ...env, MERGED: 'off' });
assert.equal(res.status, 503);
res = await call('/not-a-proxy-path', { ...env, MERGED: 'off' });     // the preview handler still answers
assert.equal(res.status, 404);
res = await call('/not-a-proxy-path', { ...env, OFF: '1' });
assert.equal(res.status, 503);
res = await call(mpath, { ...env, MERGED: '1', WEBSEED_DAILY_BUDGET: '0' });
assert.equal(res.status, 206);
await res.arrayBuffer();
res = await call(mpath, { ...env, WEBSEED_DAILY_BUDGET: '-1' });     // a negative budget: no cap either
assert.equal(res.status, 206);
await res.arrayBuffer();
{ // the Durable Object meter: isolates report in batches and learn the day's total
  const store = new Map();
  const dobj = new WebseedMeter({ storage: { get: async (k) => store.get(k), put: async (k, v) => { store.set(k, v); } } });
  let doCalls = 0;
  const ns = { idFromName: (n) => n, get: () => ({ fetch: (u) => { doCalls++; return dobj.fetch(new Request(u)); } }) };
  const m1 = doMeter(ns, 30), m2 = doMeter(ns, 30);
  const waits = [];
  const ctx = { waitUntil: (p) => waits.push(p) };
  let ok1 = 0;
  // the first request reports at once (no report yet), then every 25th
  for (let i = 0; i < 26; i++) if (m1.allow(ctx)) ok1++;
  await Promise.all(waits);
  assert.equal(ok1, 26);
  assert.equal(doCalls, 2);
  assert.equal(store.get('count').n, 26, JSON.stringify(store.get('count')));
  for (let i = 0; i < 6; i++) m2.allow(ctx);                         // m2 learns the total on its first flush
  await Promise.all(waits);
  assert.equal(m2.allow(ctx), false, 'over the shared budget');
  // a worker wired to the DO meter: the first request is allowed and reports,
  // the next one is over a budget of 1 (the shared count already exceeds it)
  _reset();
  const e1 = { ...env, METER: ns, WEBSEED_DAILY_BUDGET: '1' };
  res = await worker.fetch(new Request(`http://w.test${mpath}`, { headers: { Range: 'bytes=0-9' } }), e1, ctx);
  assert.equal(res.status, 206);
  await res.arrayBuffer();
  await Promise.all(waits);
  res = await worker.fetch(new Request(`http://w.test${mpath}`, { headers: { Range: 'bytes=0-9' } }), e1, ctx);
  assert.equal(res.status, 503, 'over the budget of 1');
  // requests while a report is in flight still count (seen under workerd: a
  // batch report reset the local count before the total came back)
  {
    const st2 = new Map();
    const d2 = new WebseedMeter({ storage: { get: async (k) => st2.get(k), put: async (k, v) => { st2.set(k, v); } } });
    const ns2 = { idFromName: (n) => n, get: () => ({ fetch: (u) => d2.fetch(new Request(u)) }) };
    const m3 = doMeter(ns2, 3);
    const w3 = [];
    const ctx3 = { waitUntil: (p) => w3.push(p) };
    const got = [];
    for (let i = 0; i < 60; i++) got.push(m3.allow(ctx3));
    assert.equal(got.filter(Boolean).length, 3, JSON.stringify(got));
    await Promise.all(w3);
    for (let i = 0; i < 30; i++) got.push(m3.allow(ctx3));
    await Promise.all(w3);
    assert.equal(got.filter(Boolean).length, 3, 'still over the budget after the reports came back');
  }
  // a new UTC day starts from 0
  store.set('count', { day: '2000-01-01', n: 999 });
  const r = await dobj.fetch(new Request('https://meter/add?n=3'));
  assert.equal((await r.json()).n, 3);
}

site.close(); up.close();
console.log('build_proxy_merged_js: all checks passed');
