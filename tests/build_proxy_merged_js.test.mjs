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
import { AsyncLocalStorage } from 'node:async_hooks';
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
let upMode = 'ok';             // ok | ignore-range | ignore-range-de | shift | wrong-total | short-length | node404-once | node404-de-once

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
  if (upMode === 'node404-de-once' && m[1].startsWith('streetzim-testland-lang/')) { upMode = 'ok'; res.writeHead(404); res.end(); return; }
  if (!f) { res.writeHead(404); res.end(); return; }
  const data = f.data;
  const r = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range || '');
  const ir = req.headers['if-range'];
  if (!r || upMode === 'ignore-range' || (upMode === 'ignore-range-de' && m[1].startsWith('streetzim-testland-lang/'))
      || (ir && ir !== f.etag)) {
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

// the same for a later part of the range (fetched after the headers): the
// fallback happens inside the request, the bytes arrive whole
upMode = 'node404-de-once';
const d1 = downloadsSeen;
res = await get('bytes=250990-251100');                            // base, blob Y, then the pack
assert.deepEqual(await bytesOf(res), expected.subarray(250990, 251101));
assert.equal(downloadsSeen, d1 + 1);
upMode = 'ok';

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

// archive.org answers that are not exactly these bytes of this file: refused.
// Not replaced (the same ETag and size: archive.org ignoring Range, a shifted
// range, a short body): on the range's first archive.org part, tried once
// more, then 503 with Retry-After before any body — never "out of date"
for (const [mode, rng, why] of [['ignore-range', 'bytes=1000-2000', /HTTP 200/],
  ['shift', 'bytes=1000-2000', /HTTP 206 bytes 10001-11001\/300000/], ['short-length', 'bytes=1000-2000', /length 1000/]]) {
  handle = mk();
  upMode = mode;
  const n0 = hits.node;
  res = await get(rng);
  assert.equal(res.status, 503, mode);
  assert.equal(res.headers.get('retry-after'), '30', mode);
  assert.match(await res.text(), why, mode);
  assert.ok(hits.node - n0 >= 2, `${mode}: tried again`);
  assert.equal(handle.state().stale, 0, `${mode}: not out of date`);
  upMode = 'ok';
}
// the same on a later part (after the headers went out): the stream is cut,
// which clients retry; still not out of date
handle = mk();
upMode = 'ignore-range-de';
res = await get('bytes=1000-251100');                              // base (fine), blob Y, then the pack
assert.equal(res.status, 206);
await assert.rejects(res.arrayBuffer());
await settle();
assert.match(events.at(-1).error, /HTTP 200/);
assert.equal(handle.state().stale, 0);
upMode = 'ok';
// Replaced (a 200 to our If-Range, a 206 of a file of another size): "out of
// date" before any body, and remembered: later requests for this version get
// the 404 without asking archive.org (a torrent client drops the web seed
// after one such error instead of retrying a cut 206 every second)
for (const [mode, change] of [['etag', () => { upFiles.get('streetzim-testland/osm-testland-2026-10-13.zim').etag = '"6ad42479-493e0"'; }],
  ['wrong-total', () => { upMode = 'wrong-total'; }]]) {
  handle = mk();
  await (await get('bytes=0-9')).arrayBuffer();                  // the index and the header blob are cached
  change();
  res = await get('bytes=1000-2000');
  assert.equal(res.status, 404, mode);
  assert.match(await res.text(), /out of date/);
  assert.equal(handle.state().stale, 1);
  const n0 = hits.node + hits.download;
  res = await get('bytes=0-9');
  assert.equal(res.status, 404, `${mode}: remembered`);
  await res.text();
  assert.equal(hits.node + hits.download, n0, `${mode}: archive.org not asked again`);
  upFiles.get('streetzim-testland/osm-testland-2026-10-13.zim').etag = ETAG_A;
  upMode = 'ok';
}
res = await get('bytes=1000-2000', { handle: mk() });            // a new isolate, the file as indexed again
assert.deepEqual(await bytesOf(res), expected.subarray(1000, 2001));
{ // the memo expires: after 10 minutes the version is checked again
  let t = 1e12;
  const h = mk({ now: () => t });
  upFiles.get('streetzim-testland/osm-testland-2026-10-13.zim').etag = '"6ad42479-493e0"';
  res = await get('bytes=1000-2000', { handle: h });
  assert.equal(res.status, 404);
  await res.text();
  upFiles.get('streetzim-testland/osm-testland-2026-10-13.zim').etag = ETAG_A;   // put back (a false alarm, say)
  t += 5 * 60 * 1000;
  res = await get('bytes=1000-2000', { handle: h });
  assert.equal(res.status, 404, 'within 10 minutes: still remembered');
  await res.text();
  t += 6 * 60 * 1000;
  res = await get('bytes=1000-2000', { handle: h });
  assert.equal(res.status, 206, 'after 10 minutes: checked again');
  assert.deepEqual(await bytesOf(res), expected.subarray(1000, 2001));
}
// a replaced file met only after the headers (not the first archive.org part
// of the range): the stream is cut, and the version is out of date from then on
{
  handle = mk();
  upFiles.get('streetzim-testland-lang/osm_testland_wiki-de_2026-10-13.zim').etag = '"9-9"';
  const ix2 = { ...index, sources: { ...index.sources, de: { ...index.sources.de, etag: '"1-2"' } } };
  setIndex(ix2);
  res = await get('bytes=1000-251100');                              // base first (fine), then de (replaced)
  assert.equal(res.status, 206);
  await assert.rejects(res.arrayBuffer());
  await settle();
  assert.equal(handle.state().stale, 1);
  res = await get('bytes=0-9');
  assert.equal(res.status, 404);
  await res.text();
  setIndex(index);
  upFiles.get('streetzim-testland-lang/osm_testland_wiki-de_2026-10-13.zim').etag = '"1-2"';
}

// ---------- the subrequest budget at request time ----------
{
  // a fresh isolate with 3 subrequests: the index 1, the header blob 1, the
  // base's first part 2 (archive.org's redirect) does not fit
  res = await get(null, { handle: mk({ subrequests: 3 }) });         // the whole file does not fit: ranges only
  assert.equal(res.status, 416);
  assert.match(await res.text(), /byte ranges/);
  const h = mk({ subrequests: 3 });
  res = await get(`bytes=0-${index.size - 1}`, { handle: h });       // a range is cut to what fits
  assert.equal(res.status, 206);
  assert.equal(res.headers.get('content-range'), `bytes 0-999/${index.size}`);
  assert.deepEqual(await bytesOf(res), expected.subarray(0, 1000));
  // with the index and that blob cached, more fits: the base part (2) and the next blob (1)
  res = await get(`bytes=0-${index.size - 1}`, { handle: h });
  assert.equal(res.headers.get('content-range'), `bytes 0-251049/${index.size}`);
  assert.deepEqual(await bytesOf(res), expected.subarray(0, 251050));
  res = await get('bytes=1000-1099', { handle: mk({ subrequests: 1 }) });   // nothing fits: try again later
  assert.equal(res.status, 503);
  await res.text();
}

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
  assert.equal(isolateMeter(0).allow(), false, 'a budget of 0 switches the webseed off');
  assert.equal(isolateMeter(NaN).allow(), false);
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
for (const b of ['0', '-1', 'lots']) {                                 // 0, negative or garbage: the webseed is off
  res = await call(mpath, { ...env, WEBSEED_DAILY_BUDGET: b });
  assert.equal(res.status, 503, `budget ${b}`);
  await res.text();
}
res = await call(mpath, { ...env });                                   // unset: the default budget
assert.equal(res.status, 206);
await res.arrayBuffer();

// A Durable Object namespace whose stubs, like workerd's, can do I/O only in
// the request that created them ("Cannot perform I/O on behalf of a
// different request"); each test request runs in its own context.
const als = new AsyncLocalStorage();
const inRequest = (fn) => als.run({}, fn);
function strictNs(dobjFetch, count) {
  return {
    idFromName: (n) => n,
    get: () => {
      const owner = als.getStore();
      return {
        fetch: async (u) => {
          if (als.getStore() !== owner) throw new Error('Cannot perform I/O on behalf of a different request');
          count.calls++;
          return dobjFetch(u);
        },
      };
    },
  };
}
function newDO() {
  const store = new Map();
  const dobj = new WebseedMeter({ storage: { get: async (k) => store.get(k), put: async (k, v) => { store.set(k, v); } } });
  return { store, fetch: (u) => dobj.fetch(new Request(u)) };
}
{ // two isolates share one count; a stub is made inside each report
  const d = newDO(), count = { calls: 0 };
  const ns = strictNs(d.fetch, count);
  let release;
  const gate = () => new Promise((r) => { release = r; });
  const mA = doMeter(ns, 30, { sleep: gate }), mB = doMeter(ns, 30, { sleep: gate });
  const waits = [];
  const ctx = { waitUntil: (p) => waits.push(p) };
  const okA = [];
  for (let i = 0; i < 26; i++) okA.push(await inRequest(() => mA.allow(ctx)));
  await Promise.all(waits.splice(0).filter((p) => p !== undefined).map((p) => Promise.race([p, Promise.resolve()])));
  await settle();
  assert.equal(okA.filter(Boolean).length, 26);
  // request 1 asked the meter before answering; request 26 reported the batch
  assert.equal(d.store.get('count').n, 26, JSON.stringify(d.store.get('count')));
  assert.equal(count.calls, 2);
  // isolate B: its first request learns the shared 27; 3 more fit the budget of 30
  const okB = [];
  for (let i = 0; i < 5; i++) okB.push(await inRequest(() => mB.allow(ctx)));
  assert.deepEqual(okB, [true, true, true, true, false]);
  // the tail: B's unreported requests are reported when the timer fires,
  // although B gets no 25th request
  const before = d.store.get('count').n;
  release();
  await Promise.all(waits);
  await settle();
  assert.equal(d.store.get('count').n, before + 4, 'the quiet isolate reported its tail');
}
{ // fail closed: no meter, no webseed; back once the meter answers
  let up2 = false, t = 1e12;
  const d = newDO(), count = { calls: 0 };
  const ns = strictNs((u) => (up2 ? d.fetch(u) : Promise.reject(new Error('DO down'))), count);
  const m = doMeter(ns, 100, { now: () => t });
  assert.equal(await inRequest(() => m.allow(null)), false, 'the meter is unreachable: refused');
  const c0 = count.calls;
  assert.equal(await inRequest(() => m.allow(null)), false);
  assert.equal(count.calls, c0, 'not asked again within the retry pause');
  up2 = true;
  t += 6000;
  assert.equal(await inRequest(() => m.allow(null)), true, 'back once it answers');
  assert.equal(d.store.get('count').n, 3, 'the refused requests were counted too');
}
{ // out-of-order answers: the total never goes down; reports that came back
  // no longer count as in flight
  const answers = [{ n: 50 }, { n: 40 }];
  const day = new Date().toISOString().slice(0, 10);
  const ns = { idFromName: (n) => n, get: () => ({ fetch: async () => new Response(JSON.stringify({ day, ...answers.shift() })) }) };
  const m = doMeter(ns, 55, { sleep: () => new Promise(() => {}) });   // no tail timer here
  await m.allow(null);                                               // synced: total 50
  assert.equal(m.state().total, 50);
  const ctx = { waitUntil: () => {} };
  for (let i = 0; i < 25; i++) await m.allow(ctx);                   // the 25th reports; the answer says 40
  await settle();
  assert.equal(answers.length, 0, 'both answers were used');
  assert.equal(m.state().total, 50);                                 // the (stale) 40 did not lower it
  const d = newDO();
  const ns2 = { idFromName: (n) => n, get: () => ({ fetch: (u) => d.fetch(u) }) };
  const m2 = doMeter(ns2, 30);
  const w2 = [];
  const ctx2 = { waitUntil: (p) => w2.push(p) };
  for (let i = 0; i < 26; i++) await m2.allow(ctx2);                 // 1 + a batch of 25
  await Promise.all(w2.filter((p) => p !== undefined));
  await settle();
  assert.equal(m2.state().inflight, 0);
  assert.equal(m2.state().total, 26);
  const more = [];
  for (let i = 0; i < 5; i++) more.push(await m2.allow(ctx2));
  assert.deepEqual(more, [true, true, true, true, false]);
  let asked = 0;
  const ns3 = { idFromName: (n) => n, get: () => ({ fetch: (u) => { asked++; return d.fetch(u); } }) };
  assert.equal(await doMeter(ns3, 0).allow(null), false, 'a budget of 0: off');
  assert.equal(await doMeter(ns3, NaN).allow(null), false);
  assert.equal(asked, 0, 'off: the meter is not even asked');
}
{ // a report not yet answered still counts: the requests it carried are in flight
  const d = newDO();
  let hold = null;
  const ns = { idFromName: (n) => n, get: () => ({ fetch: async (u) => { if (hold) await hold; return d.fetch(u); } }) };
  const m = doMeter(ns, 30, { sleep: () => new Promise(() => {}) });
  await m.allow(null);                                               // synced: total 1
  let release;
  hold = new Promise((r) => { release = r; });
  const ctx = { waitUntil: () => {} };
  const got = [];
  for (let i = 0; i < 30; i++) got.push(await m.allow(ctx));         // the 25th reports; its answer is held
  assert.equal(m.state().inflight, 25);
  assert.deepEqual(got.map((x, i) => [i, x]).filter(([, x]) => !x).map(([i]) => i), [29], JSON.stringify(got));
  release();
  await settle();
  assert.equal(m.state().inflight, 0);
}
{ // a worker wired to the DO meter: the first request (asked first) fits a
  // budget of 1, the next does not
  const d = newDO(), count = { calls: 0 };
  const e1 = { ...env, METER: strictNs(d.fetch, count), WEBSEED_DAILY_BUDGET: '1' };
  _reset();
  const waits = [];
  const ctx = { waitUntil: (p) => waits.push(p) };
  res = await inRequest(() => worker.fetch(new Request(`http://w.test${mpath}`, { headers: { Range: 'bytes=0-9' } }), e1, ctx));
  assert.equal(res.status, 206);
  await res.arrayBuffer();
  res = await inRequest(() => worker.fetch(new Request(`http://w.test${mpath}`, { headers: { Range: 'bytes=0-9' } }), e1, ctx));
  assert.equal(res.status, 503, 'over the budget of 1');
  await res.text();
  // a new UTC day starts from 0
  d.store.set('count', { day: '2000-01-01', n: 999 });
  assert.equal((await (await d.fetch('https://meter/add?n=3')).json()).n, 3);
}

site.close(); up.close();
console.log('build_proxy_merged_js: all checks passed');
