// StreetZimHttpSource — the Blob look-alike over HTTP Range requests that
// the online preview streams ZIMs through (web/drive/zim-reader.js).
//
//   node tests/test_zim_http_source.mjs [/path/to/some.zim]
//
// Serves a synthetic ZIM (raw 4-byte-offset cluster, raw 8-byte "extended"
// cluster, and a zstd cluster when Node has zstd) plus, when given, a real
// ZIM through scripts/serve-web-local.py, and checks that every entry read
// over HTTP is byte-identical to the Blob-backed reader, that raw clusters
// are read piecemeal, that requests coalesce, and that a server which
// ignores Range is refused before it streams the whole file.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import zlib from 'node:zlib';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const REAL_ZIM = process.argv[2] || process.env.ZIM_FILE || '';

// ---------- load the reader the way the service worker does ----------
function loadReader() {
  const ctx = { console, fetch, TextDecoder, DataView, Uint8Array, Map, Promise, setTimeout };
  ctx.self = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/drive/fzstd.js'), 'utf8'), ctx);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/drive/zim-reader.js'), 'utf8'), ctx);
  return { Reader: ctx.StreetZimReader, HttpSource: ctx.StreetZimHttpSource };
}

// ---------- a minimal ZIM writer (enough for the reader) ----------
function cmpCodePoints(a, b) {
  const A = [...a], B = [...b];
  for (let i = 0; i < Math.min(A.length, B.length); i++) {
    const ca = A[i].codePointAt(0), cb = B[i].codePointAt(0);
    if (ca !== cb) return ca < cb ? -1 : 1;
  }
  return A.length - B.length;
}

function buildCluster(blobs, { extended, zstd }) {
  const word = extended ? 8 : 4;
  const table = new Uint8Array((blobs.length + 1) * word);
  const tv = new DataView(table.buffer);
  let off = table.byteLength;
  const put = (i, v) => { if (extended) { tv.setUint32(i * 8, v >>> 0, true); tv.setUint32(i * 8 + 4, Math.floor(v / 0x100000000), true); } else tv.setUint32(i * 4, v, true); };
  put(0, off);
  blobs.forEach((b, i) => { off += b.byteLength; put(i + 1, off); });
  let payload = Buffer.concat([Buffer.from(table), ...blobs.map((b) => Buffer.from(b))]);
  let info = extended ? 0x11 : 0x01;
  if (zstd) { payload = zlib.zstdCompressSync(payload); info = extended ? 0x15 : 0x05; }
  return Buffer.concat([Buffer.from([info]), payload]);
}

// entries: [{path, mime, data, cluster}]; clusters: [{extended, zstd}]
function buildZim(entries, clusterSpecs) {
  const mimes = [...new Set(entries.map((e) => e.mime))];
  const sorted = [...entries].sort((a, b) => cmpCodePoints(a.path, b.path));
  const blobIndex = new Map();
  const clusterBlobs = clusterSpecs.map(() => []);
  for (const e of sorted) {
    blobIndex.set(e.path, clusterBlobs[e.cluster].length);
    clusterBlobs[e.cluster].push(e.data);
  }
  const dirents = sorted.map((e) => {
    const url = Buffer.from(e.path, 'utf8');
    const d = Buffer.alloc(16 + url.length + 1 + 1);
    d.writeUInt16LE(mimes.indexOf(e.mime), 0);
    d[2] = 0; d[3] = 'C'.charCodeAt(0);
    d.writeUInt32LE(0, 4);
    d.writeUInt32LE(e.cluster, 8);
    d.writeUInt32LE(blobIndex.get(e.path), 12);
    url.copy(d, 16); d[16 + url.length] = 0; d[17 + url.length] = 0;
    return d;
  });
  const clusters = clusterSpecs.map((spec, i) => buildCluster(clusterBlobs[i], spec));
  const mimeList = Buffer.concat([...mimes.map((m) => Buffer.from(m + '\0')), Buffer.from('\0')]);
  const header = Buffer.alloc(80);
  let pos = 80;
  const mimeListPos = pos; pos += mimeList.length;
  const urlPtrPos = pos; pos += 8 * sorted.length;
  const titlePtrPos = pos; pos += 4 * sorted.length;
  const clusterPtrPos = pos; pos += 8 * clusters.length;
  const direntPos = pos; pos += dirents.reduce((n, d) => n + d.length, 0);
  const clusterPos = pos; pos += clusters.reduce((n, c) => n + c.length, 0);
  const checksumPos = pos;
  header.writeUInt32LE(0x044D495A, 0);
  header.writeUInt16LE(6, 4); header.writeUInt16LE(1, 6);
  header.writeUInt32LE(sorted.length, 24);
  header.writeUInt32LE(clusters.length, 28);
  header.writeBigUInt64LE(BigInt(urlPtrPos), 32);
  header.writeBigUInt64LE(BigInt(titlePtrPos), 40);
  header.writeBigUInt64LE(BigInt(clusterPtrPos), 48);
  header.writeBigUInt64LE(BigInt(mimeListPos), 56);
  header.writeUInt32LE(0xFFFFFFFF, 64); header.writeUInt32LE(0xFFFFFFFF, 68);
  header.writeBigUInt64LE(BigInt(checksumPos), 72);
  const urlPtrs = Buffer.alloc(8 * sorted.length);
  const titlePtrs = Buffer.alloc(4 * sorted.length);
  let d = direntPos;
  dirents.forEach((de, i) => { urlPtrs.writeBigUInt64LE(BigInt(d), i * 8); titlePtrs.writeUInt32LE(i, i * 4); d += de.length; });
  const clusterPtrs = Buffer.alloc(8 * clusters.length);
  let c = clusterPos;
  clusters.forEach((cl, i) => { clusterPtrs.writeBigUInt64LE(BigInt(c), i * 8); c += cl.length; });
  return Buffer.concat([header, mimeList, urlPtrs, titlePtrs, clusterPtrs, ...dirents, ...clusters, Buffer.alloc(16)]);
}

function pattern(n, seed) {
  const out = new Uint8Array(n);
  let x = seed >>> 0;
  for (let i = 0; i < n; i++) { x = (x * 1664525 + 1013904223) >>> 0; out[i] = x >>> 24; }
  return out;
}

// ---------- servers ----------
function waitForPort(port, ms = 15000) {
  const t0 = Date.now();
  return new Promise((resolve, reject) => {
    (function tick() {
      fetch('http://127.0.0.1:' + port + '/', { method: 'HEAD' })
        .then(() => resolve())
        .catch(() => (Date.now() - t0 > ms ? reject(new Error('port ' + port + ' never came up')) : setTimeout(tick, 150)));
    })();
  });
}

async function main() {
  const { Reader, HttpSource } = loadReader();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'szhttp-'));
  const haveZstd = typeof zlib.zstdCompressSync === 'function';

  // Cluster 0: raw, 4-byte offsets, 8 blobs of 256 KiB → 2 MiB. Reading one
  // blob must not pull the whole cluster.
  // Cluster 1: raw, 8-byte "extended" offsets, many small blobs.
  // Cluster 2: zstd (skipped when Node lacks zstd; the real ZIM covers it).
  const entries = [];
  for (let i = 0; i < 8; i++) entries.push({ path: `big/${i}.bin`, mime: 'application/octet-stream', data: pattern(256 * 1024, 100 + i), cluster: 0 });
  for (let i = 0; i < 40; i++) entries.push({ path: `small/${String(i).padStart(3, '0')}.bin`, mime: 'application/octet-stream', data: pattern(3000 + i * 97, 500 + i), cluster: 1 });
  const specs = [{ extended: false }, { extended: true }];
  if (haveZstd) {
    for (let i = 0; i < 12; i++) entries.push({ path: `tiles/${i}.pbf`, mime: 'application/x-protobuf', data: Buffer.from(`tile ${i} `.repeat(500 + i)), cluster: 2 });
    specs.push({ zstd: true });
  }
  entries.push({ path: 'map-config.json', mime: 'application/json', data: Buffer.from('{"name":"synthetic"}'), cluster: 1 });
  const zim = buildZim(entries, specs);
  const synthPath = path.join(tmp, 'synthetic.zim');
  fs.writeFileSync(synthPath, zim);
  if (REAL_ZIM) fs.symlinkSync(path.resolve(REAL_ZIM), path.join(tmp, path.basename(REAL_ZIM)));

  const PORT = 8781, PLAIN = 8782;
  const rangeSrv = spawn('python3', [path.join(ROOT, 'scripts/serve-web-local.py'), String(PORT), tmp], { stdio: 'ignore' });
  // python -m http.server answers Range with a 200 + whole body: the case
  // the source must refuse.
  const plainSrv = spawn('python3', ['-m', 'http.server', '--bind', '127.0.0.1', String(PLAIN)], { cwd: tmp, stdio: 'ignore' });
  let failures = 0;
  const check = (name, fn) => Promise.resolve().then(fn).then(() => console.log('  [PASS] ' + name), (e) => { failures++; console.log('  [FAIL] ' + name + ' — ' + (e && e.stack || e)); });
  try {
    await waitForPort(PORT); await waitForPort(PLAIN);

    await check('synthetic ZIM opens over HTTP and via Blob identically', async () => {
      const src = new HttpSource(`http://127.0.0.1:${PORT}/synthetic.zim`, { blockSize: 128 * 1024 });
      await src.open();
      assert.equal(src.size, zim.length);
      const http = new Reader(src); await http.open();
      const local = new Reader(await fs.openAsBlob(synthPath)); await local.open();
      assert.deepEqual(http.header, local.header);
      assert.deepEqual(http.mimeList, local.mimeList);
      const cfg = await http.read('map-config.json');
      assert.equal(new TextDecoder().decode(cfg.data), '{"name":"synthetic"}');
      for (const e of entries) {
        const a = await http.read(e.path), b = await local.read(e.path);
        assert.ok(a && b, 'missing ' + e.path);
        assert.equal(a.mime, e.mime);
        assert.ok(Buffer.from(a.data).equals(Buffer.from(b.data)) && Buffer.from(a.data).equals(Buffer.from(e.data)), 'bytes differ for ' + e.path);
      }
      assert.equal(await http.read('nope/none.bin'), null);
    });

    await check('raw cluster blobs are read piecemeal, not whole-cluster', async () => {
      const src = new HttpSource(`http://127.0.0.1:${PORT}/synthetic.zim`, { blockSize: 128 * 1024 });
      await src.open();
      const r = new Reader(src); await r.open();
      const before = src.stats.bytes;
      const one = await r.read('big/5.bin');
      assert.equal(one.data.byteLength, 256 * 1024);
      const cost = src.stats.bytes - before;
      // One 256 KiB blob + offset table + dirent/pointer blocks ≪ the 2 MiB cluster.
      assert.ok(cost < 700 * 1024, 'read of one blob cost ' + cost + ' bytes');
      assert.ok(cost >= 256 * 1024, 'read of one blob cost only ' + cost + ' bytes');
      // A second blob from the same cluster reuses the cached offset table.
      const b2 = src.stats.bytes;
      await r.read('big/6.bin');
      assert.ok(src.stats.bytes - b2 <= 256 * 1024 + 128 * 1024, 'second blob cost ' + (src.stats.bytes - b2));
      // Small blobs in the extended cluster come out of a handful of blocks.
      const b3 = src.stats.bytes, q3 = src.stats.requests;
      for (let i = 0; i < 40; i++) await r.read(`small/${String(i).padStart(3, '0')}.bin`);
      assert.ok(src.stats.requests - q3 <= 6, '40 small reads took ' + (src.stats.requests - q3) + ' requests');
      assert.ok(src.stats.bytes - b3 <= 6 * 128 * 1024, '40 small reads pulled ' + (src.stats.bytes - b3) + ' bytes');
    });

    await check('concurrent lookups coalesce block fetches', async () => {
      const src = new HttpSource(`http://127.0.0.1:${PORT}/synthetic.zim`, { blockSize: 64 * 1024 });
      await src.open();
      const r = new Reader(src); await r.open();
      const q0 = src.stats.requests;
      const all = await Promise.all(entries.filter((e) => e.path.startsWith('small/')).map((e) => r.read(e.path)));
      assert.ok(all.every(Boolean));
      const blocks = Math.ceil(zim.length / (64 * 1024));
      assert.ok(src.stats.requests - q0 <= blocks, `${src.stats.requests - q0} requests for ${blocks} blocks`);
    });

    await check('reads spanning block boundaries and exact-range reads', async () => {
      const src = new HttpSource(`http://127.0.0.1:${PORT}/synthetic.zim`, { blockSize: 4096 });
      await src.open();
      const whole = zim;
      for (const [off, len] of [[4090, 20], [0, 4096], [4095, 4097], [8000, 100000], [zim.length - 5, 5]]) {
        const got = Buffer.from(await src.slice(off, off + len).arrayBuffer());
        assert.ok(got.equals(whole.subarray(off, off + len)), `slice ${off}+${len}`);
      }
      await assert.rejects(() => src.slice(zim.length - 2, zim.length + 2).arrayBuffer(), /out-of-range/);
    });

    await check('a server that ignores Range is refused before streaming the file', async () => {
      const src = new HttpSource(`http://127.0.0.1:${PLAIN}/synthetic.zim`);
      await assert.rejects(() => src.open(), /ignores Range/);
      assert.equal(src.size, 0);
    });

    await check('HEAD fallback when Content-Range is not readable', async () => {
      // Simulate a CORS server that hides Content-Range: wrap fetch.
      const ctx = { console, TextDecoder, DataView, Uint8Array, Map, Promise, setTimeout };
      ctx.fetch = async (url, init) => {
        const res = await fetch(url, init);
        if (init && init.method === 'HEAD') return res;
        const h = new Headers(res.headers); h.delete('content-range');
        return new Response(res.body, { status: res.status, headers: h });
      };
      ctx.self = ctx; ctx.globalThis = ctx; vm.createContext(ctx);
      vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/drive/fzstd.js'), 'utf8'), ctx);
      vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/drive/zim-reader.js'), 'utf8'), ctx);
      const src = new ctx.StreetZimHttpSource(`http://127.0.0.1:${PORT}/synthetic.zim`);
      await src.open();
      assert.equal(src.size, zim.length);
      const r = new ctx.StreetZimReader(src); await r.open();
      assert.ok(await r.read('big/0.bin'));
    });

    if (REAL_ZIM) {
      await check('real ZIM: entries read over HTTP match the Blob reader', async () => {
        const name = path.basename(REAL_ZIM);
        const src = new HttpSource(`http://127.0.0.1:${PORT}/${name}`);
        await src.open();
        const http = new Reader(src); await http.open();
        const local = new Reader(await fs.openAsBlob(REAL_ZIM)); await local.open();
        assert.deepEqual(http.info, local.info);
        const paths = ['map-config.json', 'index.html', 'search-data/manifest.json', 'category-index/manifest.json'];
        const cfg = JSON.parse(new TextDecoder().decode((await local.read('map-config.json')).data));
        // A few tiles around the configured centre at the top zoom.
        const z = cfg.maxZoom || 14, n = 2 ** z;
        const x = Math.floor((cfg.center[0] + 180) / 360 * n);
        const lat = cfg.center[1] * Math.PI / 180;
        const y = Math.floor((1 - Math.log(Math.tan(lat) + 1 / Math.cos(lat)) / Math.PI) / 2 * n);
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) paths.push(`tiles/${z}/${x + dx}/${y + dy}.pbf`);
        let found = 0;
        const t0 = Date.now();
        for (const p of paths) {
          const a = await http.read(p), b = await local.read(p);
          assert.equal(!!a, !!b, 'presence differs for ' + p);
          if (!a) continue;
          found++;
          assert.ok(Buffer.from(a.data).equals(Buffer.from(b.data)), 'bytes differ for ' + p);
        }
        assert.ok(found >= 5, 'only ' + found + ' of the sample paths exist');
        console.log(`         ${found} entries, ${src.stats.requests} requests, ${(src.stats.bytes / 1048576).toFixed(1)} MB, ${Date.now() - t0} ms (local loopback)`);
      });
    }
  } finally {
    rangeSrv.kill(); plainSrv.kill();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  if (failures) { console.log(failures + ' failure(s)'); process.exit(1); }
  console.log('all passed');
}

main().catch((e) => { console.error(e); process.exit(1); });
