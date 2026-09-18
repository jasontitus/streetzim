// preview-proxy/ia-range-proxy.js against the real archive.org.
//
//   node tests/test_preview_proxy.mjs [/path/to/osm-washington-dc-2026-09-08.zim]
//
// Needs network. With the ZIM on disk the proxied bytes are compared to
// the file; without it only status/headers/lengths are checked. Override
// the item/file with IA_ITEM / IA_FILE.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { handleRequest } from '../preview-proxy/ia-range-proxy.js';

const ITEM = process.env.IA_ITEM || 'streetzim-washington-dc';
const FILE = process.env.IA_FILE || 'osm-washington-dc-2026-09-08.zim';
const LOCAL = process.argv[2] || process.env.ZIM_FILE || '';
const BASE = 'https://proxy.example/';

const req = (p, init) => handleRequest(new Request(BASE.replace(/\/$/, '') + p, init));
let failures = 0;
const check = (name, fn) => Promise.resolve().then(fn).then(() => console.log('  [PASS] ' + name), (e) => { failures++; console.log('  [FAIL] ' + name + ' — ' + (e && e.stack || e)); });

await check('ranged GET → 206 with the exact bytes and CORS headers', async () => {
  const res = await req(`/${ITEM}/${FILE}`, { headers: { Range: 'bytes=0-79', Origin: 'https://streetzim.web.app' } });
  assert.equal(res.status, 206);
  assert.equal(res.headers.get('access-control-allow-origin'), '*');
  assert.match(res.headers.get('access-control-expose-headers'), /Content-Range/);
  assert.equal(res.headers.get('accept-ranges'), 'bytes');
  assert.match(res.headers.get('content-range'), /^bytes 0-79\/\d+$/);
  assert.equal(res.headers.get('content-length'), '80');
  const body = Buffer.from(await res.arrayBuffer());
  assert.equal(body.length, 80);
  assert.equal(body.readUInt32LE(0), 0x044D495A, 'ZIM magic');
  if (LOCAL) {
    const fd = fs.openSync(LOCAL, 'r'); const want = Buffer.alloc(80); fs.readSync(fd, want, 0, 80, 0); fs.closeSync(fd);
    assert.ok(body.equals(want), 'bytes differ from the local file');
  }
});

await check('mid-file range', async () => {
  const res = await req(`/${ITEM}/${FILE}`, { headers: { Range: 'bytes=1000000-1000099' } });
  assert.equal(res.status, 206);
  assert.match(res.headers.get('content-range'), /^bytes 1000000-1000099\//);
  const body = Buffer.from(await res.arrayBuffer());
  assert.equal(body.length, 100);
  if (LOCAL) {
    const fd = fs.openSync(LOCAL, 'r'); const want = Buffer.alloc(100); fs.readSync(fd, want, 0, 100, 1000000); fs.closeSync(fd);
    assert.ok(body.equals(want), 'bytes differ from the local file');
  }
});

await check('HEAD reports the size', async () => {
  const res = await req(`/${ITEM}/${FILE}`, { method: 'HEAD' });
  assert.ok(res.status === 200 || res.status === 206, 'status ' + res.status);
  assert.ok(parseInt(res.headers.get('content-length'), 10) > 1_000_000);
  assert.equal(res.headers.get('access-control-allow-origin'), '*');
});

await check('OPTIONS preflight', async () => {
  const res = await req(`/${ITEM}/${FILE}`, { method: 'OPTIONS', headers: { Origin: 'https://streetzim.web.app', 'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'range' } });
  assert.equal(res.status, 204);
  assert.match(res.headers.get('access-control-allow-headers'), /Range/);
});

await check('refuses non-StreetZim items, non-.zim files, whole-file GETs, bad paths', async () => {
  assert.equal((await req('/someone-elses-item/x.zim', { headers: { Range: 'bytes=0-1' } })).status, 403);
  assert.equal((await req(`/${ITEM}/notes.txt`, { headers: { Range: 'bytes=0-1' } })).status, 403);
  // `..` is normalised away by the URL parser before the handler sees it,
  // leaving a one-segment path → 404 (never an upstream fetch).
  assert.equal((await req(`/${ITEM}/../${FILE}`, { headers: { Range: 'bytes=0-1' } })).status, 404);
  assert.equal((await req(`/${ITEM}/${FILE}`)).status, 400);
  assert.equal((await req(`/${ITEM}/${FILE}`, { headers: { Range: 'bytes=0-1,5-9' } })).status, 400);
  assert.equal((await req(`/${ITEM}/${FILE}`, { method: 'POST', headers: { Range: 'bytes=0-1' } })).status, 405);
  assert.equal((await req('/')).status, 404);
  assert.equal((await req(`/${ITEM}`)).status, 404);
});

await check('a missing file passes archive.org\'s 404 through, uncached', async () => {
  const res = await req(`/${ITEM}/osm-does-not-exist-2020-01-01.zim`, { headers: { Range: 'bytes=0-1' } });
  assert.equal(res.status, 404);
  assert.equal(res.headers.get('cache-control'), 'no-store');
  await res.arrayBuffer();
});

if (failures) { console.log(failures + ' failure(s)'); process.exit(1); }
console.log('all passed');
