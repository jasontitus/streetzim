// web/drive/zim-reader.js had no tests, and a bug in it broke the Find page
// on east-coast-us: the LAST cluster by file offset has no cluster after it,
// so _clusterEnd fell back to checksumPos and handed fzstd the cluster plus
// 215 MB of trailing index structures. fzstd is one-shot and rejected it
// ("invalid zstd data"); libzim never noticed because it streams and reads
// the cluster's own blob table.
//
//   node tests/zim_reader_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';
import zlib from 'node:zlib';
import crypto from 'node:crypto';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const SRC = `${REPO}/web/drive/zim-reader.js`;

let pass = 0, fail = 0;
async function ok(name, fn) {
  try { await fn(); console.log('ok   ', name); pass++; }
  catch (e) { console.error('FAIL ', name, '\n      ', e.message); fail++; }
}

// The file is an IIFE that binds to `self` (worker global), so give it a
// shim and read the export back out — passing a fake `global` parameter
// does nothing, since the IIFE picks its own argument.
const ZimReader = new Function(
  'self', fs.readFileSync(SRC, 'utf8') + '; return self.StreetZimReader;')({});
assert.ok(ZimReader, 'StreetZimReader not exported');

// A reader stub: real _clusterEnd, fake header and range reads.
function readerWith({ clusterOffsets, header }) {
  const r = Object.create(ZimReader.prototype);
  r.header = Object.assign({
    clusterCount: clusterOffsets.length,
    clusterPtrPos: 1_000_000,
    urlPtrPos: 0, titlePtrPos: 0, mimeListPos: 0, checksumPos: 0,
  }, header);
  r._readRange = async (offset, length) => {
    assert.strictEqual(offset, r.header.clusterPtrPos, 'unexpected range read');
    const buf = new Uint8Array(length);
    const v = new DataView(buf.buffer);
    clusterOffsets.forEach((o, i) => {
      v.setUint32(i * 8, o >>> 0, true);
      v.setUint32(i * 8 + 4, Math.floor(o / 0x100000000), true);
    });
    return buf;
  };
  return r;
}

// east-coast-us, real offsets: last cluster at 11_087_822_105, the URL
// pointer list is the next structure at +1.2 MB, checksum 215 MB beyond.
const ECUS = {
  clusterOffsets: [1_000, 500_000, 11_087_822_105],
  header: {
    urlPtrPos: 11_089_042_415,
    titlePtrPos: 11_111_619_855,
    clusterPtrPos: 11_122_908_575,
    mimeListPos: 80,
    checksumPos: 11_304_401_613,
  },
};

await ok('the last cluster ends at the next structure, not the checksum', async () => {
  const end = await readerWith(ECUS)._clusterEnd(11_087_822_105);
  assert.strictEqual(end, 11_089_042_415,
    'last cluster must end where the URL pointer list begins');
});

await ok('a middle cluster still ends at the next cluster', async () => {
  assert.strictEqual(await readerWith(ECUS)._clusterEnd(1_000), 500_000);
});

await ok('clusters laid out out of order use the next-highest offset', async () => {
  // The pre-existing bug this function already guarded against: a writer
  // emitting clusters concurrently, so clusterPtr[n+1] < clusterPtr[n].
  const r = readerWith({
    clusterOffsets: [900_000, 100_000, 500_000],
    header: { urlPtrPos: 2_000_000, checksumPos: 9_000_000 },
  });
  assert.strictEqual(await r._clusterEnd(100_000), 500_000);
});

await ok('a structure before the clusters is never chosen as an end', async () => {
  // mimeListPos is at offset 80, long before any cluster.
  assert.strictEqual(await readerWith(ECUS)._clusterEnd(500_000), 11_087_822_105);
});

await ok('checksumPos remains the fallback when nothing follows', async () => {
  const r = readerWith({
    clusterOffsets: [1_000],
    header: { urlPtrPos: 0, titlePtrPos: 0, clusterPtrPos: 0, mimeListPos: 0,
              checksumPos: 7_777 },
  });
  assert.strictEqual(await r._clusterEnd(1_000), 7_777);
});

// ---- zstd clusters are decoded to the end of their frame ----
// A merged ZIM (a base plus Wikipedia packs, docs/language-matrix.md) keeps
// the base's bytes at their offsets, so the base's last cluster is followed
// by the base's old tables and only then by the next cluster. Decoding up
// to the next boundary handed fzstd the frame plus those tables ("invalid
// zstd data"); the reader now finds the frame's end from its block headers.
//
// Payloads are compared with Buffer.equals and reported by length and
// hash: assert.deepStrictEqual on 100 KB+ buffers builds a diff whose
// memory has no bound when a reader regresses (15.8 GB in 16 s).
const ZSELF = {};
new Function('self', fs.readFileSync(`${REPO}/web/drive/fzstd.js`, 'utf8'))(ZSELF);
// The reader sees `fake.fetch` as fetch: StreetZimHttpSource over a buffer.
const fake = { buf: null, fetch: null };
new Function('self', 'fetch', fs.readFileSync(SRC, 'utf8'))(ZSELF, (...a) => fake.fetch(...a));
const ZReader = ZSELF.StreetZimReader, HttpSource = ZSELF.StreetZimHttpSource;
assert.ok(ZSELF.fzstd && ZReader && HttpSource, 'fzstd or the reader did not load');
const FIRST_READ = 4 * 1024 * 1024;   // zim-reader.js CLUSTER_FIRST_READ
const WHOLE_READ = 64 * 1024 * 1024;  // zim-reader.js CLUSTER_WHOLE_READ
const MiB = 1024 * 1024;

const short = (b) => `${b.length} B sha256 ${crypto.createHash('sha256').update(b).digest('hex').slice(0, 12)}`;
function sameBlobs(got, want) {
  assert.strictEqual(got.length, want.length, 'blob count');
  got.forEach((g, i) => {
    const w = Buffer.from(want[i]);
    if (!Buffer.from(g.buffer, g.byteOffset, g.byteLength).equals(w)) {
      assert.fail(`blob ${i}: got ${short(Buffer.from(g))}, want ${short(w)}`);
    }
  });
}

function lcg(n, seed) {
  const out = new Uint8Array(n);
  let x = seed >>> 0;
  for (let i = 0; i < n; i++) { x = (x * 1664525 + 1013904223) >>> 0; out[i] = x >>> 24; }
  return out;
}
// A cluster's uncompressed payload: the blob offset table, then the blobs.
function payloadOf(blobs, extended) {
  const w = extended ? 8 : 4;
  const table = Buffer.alloc((blobs.length + 1) * w);
  let off = table.length;
  const put = (i, v) => { table.writeUInt32LE(v % 0x100000000, i * w); if (extended) table.writeUInt32LE(Math.floor(v / 0x100000000), i * w + 4); };
  put(0, off);
  blobs.forEach((b, i) => { off += b.length; put(i + 1, off); });
  return Buffer.concat([table, ...blobs.map((b) => Buffer.from(b))]);
}
// A zstd frame of raw (and, with `rle`, RLE) blocks, written by hand: no
// compressor needed. `fcs` (1, 2, 4 or 8 bytes): a single-segment frame
// with its content size in that many bytes (one-shot writers; zimru writes
// 1 byte for content under 256 B); else a window descriptor (libzim's
// streaming writer). `did`: a dictionary ID of 1, 2 or 4 bytes.
function zstdFrame(data, { blockSize = 128 * 1024, fcs = 0, did = 0, checksum = false, rle = false,
                           reserved = false } = {}) {
  const n = data.length;
  const fcsFlag = { 0: 0, 1: 0, 2: 1, 4: 2, 8: 3 }[fcs];
  const didFlag = { 0: 0, 1: 1, 2: 2, 4: 3 }[did];
  const head = [0x28, 0xB5, 0x2F, 0xFD,
    fcsFlag << 6 | (fcs ? 0x20 : 0) | (reserved ? 0x08 : 0) | (checksum ? 0x04 : 0) | didFlag];
  if (!fcs) head.push(0x50);                              // 1 MiB window
  for (let i = 0; i < did; i++) head.push(7 + i);
  if (fcs === 1) { assert.ok(n < 256); head.push(n); }
  if (fcs === 2) { assert.ok(n >= 256 && n < 65792); head.push((n - 256) & 255, (n - 256) >>> 8); }
  if (fcs >= 4) for (let i = 0; i < fcs; i++) head.push(i < 4 ? (n >>> (8 * i)) & 255 : 0);
  const out = [Buffer.from(head)];
  for (let o = 0; ; o += blockSize) {
    const chunk = data.subarray(o, o + blockSize);
    const last = o + blockSize >= n ? 1 : 0;
    const same = rle && chunk.length > 1 && chunk.every((x) => x === chunk[0]);
    const bh = last | (same ? 1 : 0) << 1 | chunk.length << 3;
    out.push(Buffer.from([bh & 255, (bh >>> 8) & 255, (bh >>> 16) & 255]));
    out.push(same ? Buffer.from([chunk[0]]) : Buffer.from(chunk));
    if (last) break;
  }
  if (checksum) out.push(Buffer.from([0x11, 0x22, 0x33, 0x44]));   // fzstd skips it
  return Buffer.concat(out);
}
// A skippable frame (RFC 8878 §3.1.2): magic 0x184D2A5?, u32 size, data.
function skippable(n, nibble = 3) {
  const b = Buffer.alloc(8 + n, 0xAB);
  b.writeUInt32LE(0x184D2A50 + nibble, 0); b.writeUInt32LE(n, 4);
  return b;
}
// python-zstandard 0.25, level 19, streaming, with a checksum: one
// compressed (type 2) block holding payloadOf(PY_BLOBS).
const PY_FRAME = Buffer.from('KLUv/QRovQEA9AIMAAAAJwAAAPIAAAB7Im5hbWUiOiJjb21wcmVzc2VkIGJsb2NrIn10aWxlIGVuZAEABIIqZT4VPsY=', 'base64');
const PY_BLOBS = [Buffer.from('{"name":"compressed block"}'), Buffer.from('tile '.repeat(40) + 'end')];

// header | cluster 0 (zstd, `frames`) | `gap` bytes that are no cluster
// (old tables) | cluster 1 (raw) | cluster pointers | checksum. `cut`
// moves cluster 1 that many bytes earlier, into cluster 0's frame.
// `http`: read through StreetZimHttpSource (requests counted), else a Blob.
const RAW1 = [Buffer.from('raw one'), Buffer.from('raw two')];
async function mergedLike(frames, { gap = 300 * 1024, cut = 0, extended = false, http = false } = {}) {
  const c0 = Buffer.concat([Buffer.from([extended ? 0x15 : 0x05]), ...frames]);
  const junk = Buffer.from(lcg(gap, 99));
  if (gap >= 4) assert.notStrictEqual(junk.readUInt32LE(0), 0xFD2FB528);
  const A = 80;
  const body = Buffer.concat([c0, junk]).subarray(0, c0.length + gap - cut);
  const B = A + body.length;
  const c1 = Buffer.concat([Buffer.from([0x01]), payloadOf(RAW1)]);
  const C = B + c1.length;
  const ptrs = Buffer.alloc(16);
  ptrs.writeBigUInt64LE(BigInt(A), 0); ptrs.writeBigUInt64LE(BigInt(B), 8);
  const D = C + 16;
  const buf = Buffer.concat([Buffer.alloc(80), body, c1, ptrs, Buffer.alloc(16)]);
  const reads = [];
  let file;
  if (http) {
    fake.fetch = async (url, init) => {
      const m = /bytes=(\d+)-(\d+)/.exec(init.headers.Range);
      const s = +m[1], e = Math.min(+m[2], buf.length - 1);
      reads.push([s, e + 1]);
      const part = buf.subarray(s, e + 1);
      return { status: 206, body: null,
               headers: new Headers({ 'Content-Range': `bytes ${s}-${e}/${buf.length}`, 'Content-Length': String(e - s + 1) }),
               arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.length) };
    };
    file = new HttpSource('http://zim.test/merged.zim');
    await file.open();
  } else {
    const blob = new Blob([buf]);
    file = { size: buf.length, slice(a, b) { reads.push([a, b]); return blob.slice(a, b); } };
  }
  const r = new ZReader(file);
  r.header = { clusterCount: 2, clusterPtrPos: C, urlPtrPos: D, titlePtrPos: 0,
               mimeListPos: 0, checksumPos: D };
  return { r, buf, reads, file, A, B, c0 };
}
async function blobsOf(r, cluster, n) {
  const out = [];
  for (let i = 0; i < n; i++) out.push(await r._readBlob(cluster, i));
  return out;
}

const BLOBS = [Buffer.from('{"title":"Vaduz"}'), Buffer.alloc(300 * 1024, 0x41), Buffer.from(lcg(150 * 1024, 7))];

await ok('a zstd cluster followed by bytes that are no cluster decodes (raw + RLE blocks)', async () => {
  const frame = zstdFrame(payloadOf(BLOBS), { rle: true });
  assert.ok(frame.length < payloadOf(BLOBS).length - 100000, 'no RLE block in the frame');
  const m = await mergedLike([frame]);
  // What the reader used to do: decode from the cluster to the boundary.
  assert.throws(() => ZSELF.fzstd.decompress(m.buf.subarray(m.A + 1, m.B)), /invalid zstd data/);
  sameBlobs(await blobsOf(m.r, 0, 3), BLOBS);
  sameBlobs(await blobsOf(m.r, 1, 2), RAW1);   // the cluster after the gap
});

await ok('compressed blocks with a checksum (python-zstandard) decode the same way', async () => {
  const m = await mergedLike([PY_FRAME]);
  assert.throws(() => ZSELF.fzstd.decompress(m.buf.subarray(m.A + 1, m.B)), /invalid zstd data/);
  sameBlobs(await blobsOf(m.r, 0, 2), PY_BLOBS);
});

if (typeof zlib.zstdCompressSync === 'function') {
  await ok('a Node-compressed cluster followed by other bytes decodes', async () => {
    const m = await mergedLike([zlib.zstdCompressSync(payloadOf(BLOBS))]);
    sameBlobs(await blobsOf(m.r, 0, 3), BLOBS);
  });
}

await ok('frame headers: content size in 1, 2, 4 or 8 bytes; dictionary IDs of 1, 2, 4 bytes', async () => {
  const tiny = [Buffer.from('ab'), Buffer.from('cd')];          // < 256 B: zimru's 1-byte FCS
  const mid = [Buffer.from('m'.repeat(900)), Buffer.from(lcg(3000, 5))];   // 256 B..64 KiB: 2-byte FCS
  for (const [blobs, opts] of [[tiny, { fcs: 1 }], [mid, { fcs: 2 }], [BLOBS, { fcs: 4 }],
                               [BLOBS, { fcs: 8, checksum: true }], [tiny, { did: 1 }],
                               [tiny, { did: 2, fcs: 1 }], [BLOBS, { did: 4 }]]) {
    const m = await mergedLike([zstdFrame(payloadOf(blobs), opts)]);
    sameBlobs(await blobsOf(m.r, 0, blobs.length), blobs);
  }
});

await ok('skippable frames are skipped by their size, before and between frames', async () => {
  const p = payloadOf(BLOBS);
  let m = await mergedLike([skippable(37), zstdFrame(p)]);
  sameBlobs(await blobsOf(m.r, 0, 3), BLOBS);
  m = await mergedLike([zstdFrame(p.subarray(0, 50000)), skippable(1000, 0xF), zstdFrame(p.subarray(50000))]);
  sameBlobs(await blobsOf(m.r, 0, 3), BLOBS);
});

await ok('a cluster split over two frames is decoded whole (8-byte offsets)', async () => {
  const p = payloadOf(BLOBS, true);
  const m = await mergedLike([zstdFrame(p.subarray(0, 70000), { checksum: true }), zstdFrame(p.subarray(70000), { rle: true })],
                             { extended: true });
  sameBlobs(await blobsOf(m.r, 0, 3), BLOBS);
});

await ok('a first frame shorter than the blob table: read on to the next frame', async () => {
  const many = Array.from({ length: 40 }, (_, i) => Buffer.from('blob ' + i));
  const p = payloadOf(many);                                    // a 164-byte table
  const m = await mergedLike([zstdFrame(p.subarray(0, 6)), zstdFrame(p.subarray(6, 100)), zstdFrame(p.subarray(100))]);
  sameBlobs(await blobsOf(m.r, 0, 40), many);
});

await ok('a frame cut short by the next boundary is an error, not short data', async () => {
  const m = await mergedLike([zstdFrame(payloadOf(BLOBS))], { gap: 0, cut: 1000 });
  await assert.rejects(m.r._readBlob(0, 2), /runs past the cluster end/);
});

await ok('bytes that are no frame, and a reserved header bit, are errors', async () => {
  let m = await mergedLike([Buffer.from('not zstd at all, no frame')], { gap: 0 });
  await assert.rejects(m.r._readBlob(0, 0), /invalid zstd data \(no frame/);
  m = await mergedLike([zstdFrame(payloadOf(BLOBS), { reserved: true })]);
  await assert.rejects(m.r._readBlob(0, 0), /reserved bit/);
});

// ---- how many requests a cluster load costs (StreetZimHttpSource) ----
// Over HTTP each read is a request. A cluster whose next boundary is near
// (every cluster of a plain ZIM: Europe's 7,048 clusters of 4-36 MiB) is
// one request for exactly its bytes, as before; only a far boundary (a
// merged base's last cluster) is read in steps.
async function loadCost(frames, opts) {
  const m = await mergedLike(frames, { ...opts, http: true });
  await m.r._rawClusterMeta(0);                 // pointer list and head: cached blocks
  await m.r._clusterEnd(m.A);
  const before = m.file.stats.requests, r0 = m.reads.length;
  await m.r._loadCluster(0);
  const mine = m.reads.slice(r0);
  return { m, requests: m.file.stats.requests - before, bytes: mine.reduce((n, [a, b]) => n + b - a, 0),
           far: Math.max(...mine.map(([, b]) => b)) };
}
const bigFrame = (n, seed) => zstdFrame(payloadOf([Buffer.from(lcg(n, seed))]), { fcs: 4 });

await ok('a near boundary: one request for exactly the cluster, at 4 MiB + 1 block and at 24 MiB', async () => {
  for (const n of [4 * MiB + 50 * 1024, 5_100_000, 24 * MiB]) {
    const f = bigFrame(n, n & 255);
    const c = await loadCost([f], { gap: 0 });
    assert.strictEqual(c.requests, 1, `${n}: ${c.requests} requests`);
    assert.strictEqual(c.bytes, f.length + 1, `${n}: read ${c.bytes} for a ${f.length + 1}-byte cluster`);
    const blob = await c.m.r._readBlob(0, 0);
    assert.ok(blob.length === n && Buffer.from(blob.buffer, blob.byteOffset, 16).equals(Buffer.from(lcg(16, n & 255))));
  }
});

await ok('a near boundary with old tables before it: still one request (as before)', async () => {
  const c = await loadCost([PY_FRAME], { gap: 9 * MiB });
  assert.strictEqual(c.requests, 1);
  sameBlobs(await blobsOf(c.m.r, 0, 2), PY_BLOBS);
});

await ok('a boundary exactly CLUSTER_WHOLE_READ away: still one whole read; one byte more: bounded', async () => {
  const span0 = await (async () => { const c = await loadCost([PY_FRAME], { gap: 0 }); return c.m.B - c.m.A; })();
  let c = await loadCost([PY_FRAME], { gap: WHOLE_READ - span0 });
  assert.strictEqual(c.m.B - c.m.A, WHOLE_READ);
  assert.deepStrictEqual([c.requests, c.bytes], [1, WHOLE_READ]);
  sameBlobs(await blobsOf(c.m.r, 0, 2), PY_BLOBS);
  c = await loadCost([PY_FRAME], { gap: WHOLE_READ - span0 + 1 });
  assert.strictEqual(c.m.B - c.m.A, WHOLE_READ + 1);
  assert.deepStrictEqual([c.requests, c.bytes], [1, FIRST_READ]);
  sameBlobs(await blobsOf(c.m.r, 0, 2), PY_BLOBS);
});

await ok('a far boundary: the first read is bounded, and a long frame is read on in one more', async () => {
  const gap = WHOLE_READ + 2 * MiB;
  let c = await loadCost([PY_FRAME], { gap });
  assert.deepStrictEqual([c.requests, c.bytes], [1, FIRST_READ]);
  sameBlobs(await blobsOf(c.m.r, 0, 2), PY_BLOBS);
  const f = bigFrame(5 * MiB + 12345, 3);
  c = await loadCost([f], { gap });
  assert.strictEqual(c.requests, 2, `${c.requests} requests`);
  assert.ok(c.far - c.m.A <= 4 * FIRST_READ && c.far < c.m.B, `read to ${c.far - c.m.A}`);
  const blob = await c.m.r._readBlob(0, 0);
  assert.strictEqual(blob.length, 5 * MiB + 12345);
});

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exitCode = 1;
