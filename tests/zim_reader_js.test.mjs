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

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exitCode = 1;
