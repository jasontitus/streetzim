// A minimal ZIM writer — enough for web/drive/zim-reader.js and the
// service worker: header, MIME list, URL/title/cluster pointer lists,
// dirents sorted by URL, raw clusters (4- or 8-byte offset tables) and,
// when Node has zstd, compressed ones. Shared by the reader and
// service-worker tests.
import zlib from 'node:zlib';

// ---------- a minimal ZIM writer (enough for the reader) ----------
export function cmpCodePoints(a, b) {
  const A = [...a], B = [...b];
  for (let i = 0; i < Math.min(A.length, B.length); i++) {
    const ca = A[i].codePointAt(0), cb = B[i].codePointAt(0);
    if (ca !== cb) return ca < cb ? -1 : 1;
  }
  return A.length - B.length;
}

export function buildCluster(blobs, { extended, zstd }) {
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
export function buildZim(entries, clusterSpecs) {
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

export function pattern(n, seed) {
  const out = new Uint8Array(n);
  let x = seed >>> 0;
  for (let i = 0; i < n; i++) { x = (x * 1664525 + 1013904223) >>> 0; out[i] = x >>> 24; }
  return out;
}


export const haveZstd = typeof zlib.zstdCompressSync === 'function';

// FNV-1a over a byte array — what the browser side computes to compare
// a streamed body with the pattern without shipping the bytes back.
export function fnv1a(u8) {
  let h = 0x811c9dc5;
  for (let i = 0; i < u8.length; i++) { h ^= u8[i]; h = Math.imul(h, 0x01000193) >>> 0; }
  return h >>> 0;
}
