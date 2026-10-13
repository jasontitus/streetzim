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
const ZSELF = {};
new Function('self', fs.readFileSync(`${REPO}/web/drive/fzstd.js`, 'utf8'))(ZSELF);
const ZReader = new Function('self', fs.readFileSync(SRC, 'utf8') + '; return self.StreetZimReader;')(ZSELF);
assert.ok(ZSELF.fzstd && ZReader, 'fzstd or the reader did not load');
const FIRST_READ = 4 * 1024 * 1024;   // zim-reader.js CLUSTER_FIRST_READ

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
// compressor needed. `single`: a single-segment frame with its content size
// (one-shot writers); else a window descriptor (libzim's streaming writer).
function zstdFrame(data, { blockSize = 128 * 1024, single = false, checksum = false, rle = false } = {}) {
  const head = [0x28, 0xB5, 0x2F, 0xFD];
  if (single) {
    const n = data.length;
    head.push(0xA0 | (checksum ? 4 : 0), n & 255, (n >>> 8) & 255, (n >>> 16) & 255, (n >>> 24) & 255);
  } else {
    head.push(checksum ? 0x04 : 0x00, 0x50);              // 1 MiB window
  }
  const out = [Buffer.from(head)];
  for (let o = 0; ; o += blockSize) {
    const chunk = data.subarray(o, o + blockSize);
    const last = o + blockSize >= data.length ? 1 : 0;
    const same = rle && chunk.length > 1 && chunk.every((x) => x === chunk[0]);
    const bh = last | (same ? 1 : 0) << 1 | chunk.length << 3;
    out.push(Buffer.from([bh & 255, (bh >>> 8) & 255, (bh >>> 16) & 255]));
    out.push(same ? Buffer.from([chunk[0]]) : Buffer.from(chunk));
    if (last) break;
  }
  if (checksum) out.push(Buffer.from([0x11, 0x22, 0x33, 0x44]));   // fzstd skips it
  return Buffer.concat(out);
}
// python-zstandard 0.25, level 19, streaming, with a checksum: one
// compressed (type 2) block holding payloadOf(PY_BLOBS).
const PY_FRAME = Buffer.from('KLUv/QRovQEA9AIMAAAAJwAAAPIAAAB7Im5hbWUiOiJjb21wcmVzc2VkIGJsb2NrIn10aWxlIGVuZAEABIIqZT4VPsY=', 'base64');
const PY_BLOBS = [Buffer.from('{"name":"compressed block"}'), Buffer.from('tile '.repeat(40) + 'end')];

// header | cluster 0 (zstd, `frames`) | `gap` bytes that are no cluster
// (old tables) | cluster 1 (raw) | cluster pointers | checksum. `cut`
// moves cluster 1 that many bytes earlier, into cluster 0's frame.
const RAW1 = [Buffer.from('raw one'), Buffer.from('raw two')];
function mergedLike(frames, { gap = 300 * 1024, cut = 0, extended = false } = {}) {
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
  const blob = new Blob([buf]);
  const file = { size: buf.length, slice(a, b) { reads.push([a, b]); return blob.slice(a, b); } };
  const r = new ZReader(file);
  r.header = { clusterCount: 2, clusterPtrPos: C, urlPtrPos: D, titlePtrPos: 0,
               mimeListPos: 0, checksumPos: D };
  return { r, buf, reads, A, B, c0 };
}
async function blobsOf(r, cluster, n) {
  const out = [];
  for (let i = 0; i < n; i++) out.push(Buffer.from(await r._readBlob(cluster, i)));
  return out;
}

const BLOBS = [Buffer.from('{"title":"Vaduz"}'), Buffer.alloc(300 * 1024, 0x41), Buffer.from(lcg(150 * 1024, 7))];

await ok('a zstd cluster followed by bytes that are no cluster decodes (raw + RLE blocks)', async () => {
  const frame = zstdFrame(payloadOf(BLOBS), { rle: true });
  assert.ok(frame.length < payloadOf(BLOBS).length - 100000, 'no RLE block in the frame');
  const m = mergedLike([frame]);
  // What the reader used to do: decode from the cluster to the boundary.
  assert.throws(() => ZSELF.fzstd.decompress(m.buf.subarray(m.A + 1, m.B)), /invalid zstd data/);
  assert.deepStrictEqual(await blobsOf(m.r, 0, 3), BLOBS);
  assert.deepStrictEqual(await blobsOf(m.r, 1, 2), RAW1);   // the cluster after the gap
});

await ok('compressed blocks with a checksum (python-zstandard) decode the same way', async () => {
  const m = mergedLike([PY_FRAME]);
  assert.throws(() => ZSELF.fzstd.decompress(m.buf.subarray(m.A + 1, m.B)), /invalid zstd data/);
  assert.deepStrictEqual(await blobsOf(m.r, 0, 2), PY_BLOBS);
});

if (typeof zlib.zstdCompressSync === 'function') {
  await ok('a Node-compressed cluster followed by other bytes decodes', async () => {
    const m = mergedLike([zlib.zstdCompressSync(payloadOf(BLOBS))]);
    assert.deepStrictEqual(await blobsOf(m.r, 0, 3), BLOBS);
  });
}

await ok('a far boundary is not read to: the first read is bounded', async () => {
  const m = mergedLike([PY_FRAME], { gap: 9 * 1024 * 1024 });
  assert.deepStrictEqual(await blobsOf(m.r, 0, 2), PY_BLOBS);
  const far = Math.max(...m.reads.filter(([a]) => a >= m.A && a < m.B).map(([, b]) => b));
  assert.ok(far <= m.A + FIRST_READ, `read up to ${far - m.A} bytes past the cluster start`);
  assert.ok(m.B - m.A > FIRST_READ * 2);
});

await ok('a frame longer than the first read is read on, never past the boundary', async () => {
  const big = [Buffer.from(lcg(5 * 1024 * 1024 + 12345, 3)), Buffer.from('tail')];
  const m = mergedLike([zstdFrame(payloadOf(big), { single: true, checksum: true })],
                       { gap: 20 * 1024 * 1024 });
  assert.deepStrictEqual(await blobsOf(m.r, 0, 2), big);
  const mine = m.reads.filter(([a]) => a >= m.A && a < m.B);
  // The 9-byte head probe, the first read, one read on: not one per block.
  assert.ok(mine.length >= 2 && mine.length <= 3, `${mine.length} reads`);
  const far = Math.max(...mine.map(([, b]) => b));
  assert.ok(far < m.B && far - m.A <= 4 * (m.c0.length + 8), `read ${far - m.A} for a ${m.c0.length}-byte cluster`);
});

await ok('a cluster split over two frames is decoded whole (8-byte offsets)', async () => {
  const p = payloadOf(BLOBS, true);
  const m = mergedLike([zstdFrame(p.subarray(0, 70000), { checksum: true }), zstdFrame(p.subarray(70000), { rle: true })],
                       { extended: true });
  assert.deepStrictEqual(await blobsOf(m.r, 0, 3), BLOBS);
});

await ok('a frame cut short by the next boundary is an error, not short data', async () => {
  const m = mergedLike([zstdFrame(payloadOf(BLOBS))], { gap: 0, cut: 1000 });
  await assert.rejects(m.r._readBlob(0, 2), /runs past the cluster end/);
});

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exitCode = 1;
