// Assembles a map in other languages from a recipe (streetzim-recipe/2),
// in a dedicated worker. Plain JS (ES2020), no build step.
//
// A recipe lists the merged ZIM's bytes in order: ranges of the base ZIM
// (the visitor's own copy, a File), ranges of Wikipedia packs (downloaded
// and checked by the page before the build starts, passed in as bytes), and
// small generated pieces (inline, base64). Every byte is checked before it is written: base and pack bytes
// against the recipe's SHA-256 of each 16 MiB chunk of their file, inline
// pieces against their own SHA-256. The ZIM's MD5 is computed as the bytes
// go out, appended as its last 16 bytes, and compared with the recipe's; so
// is the MD5 of the whole file (recipe file_md5, what md5sum prints).
//
// In:  {cmd:'run', recipe, baseFile, packs: {id: Uint8Array}, sink, handle?,
//       port?, fileName}
// Out: {type:'progress', written, size, ms}  {type:'done', result}
/* global hashwasm */
importScripts('vendor/md5.umd.min.js');

const hex = (buf) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('');
const b64dec = (s) => { const bin = atob(s); const u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u; };
const sha256 = async (u8) => hex(await crypto.subtle.digest('SHA-256', u8));

// ---------------------------------------------------------------- sinks
// open(size) / write(u8) / close() / abort(reason)
class WritableSink {                 // File System Access: the visitor picked a file
  constructor(handle) { this.handle = handle; }
  async open() { this.w = await this.handle.createWritable({ keepExistingData: false }); }
  async write(u8) { await this.w.write(u8); }
  async close() { await this.w.close(); }
  async abort() { try { await this.w.abort(); } catch (e) { /* already closed */ } }
}

// The page's service worker streams a download (sw.js has the protocol).
// Watchdogs, so a dead stream ends the build instead of hanging it:
// - the service worker answers a ping every PING_MS; no answer for DEAD_MS
//   means the browser stopped it;
// - a write that waits STALL_MS for room (an 'ack'), or an end that waits
//   STALL_MS without the browser reading more ('pulled'), means the browser
//   is not reading the download (never started, cancelled, or stuck);
// - 'cancelled' from the service worker ends it at once.
// close() returns only when the browser has read the whole stream.
const PING_MS = 5000, DEAD_MS = 20000, STALL_MS = 60000;
class SinkStopped extends Error {}
class SwDownloadSink {
  constructor(port) {
    this.port = port; this.waiters = []; this.credit = 0; this.failed = null;
    this.pulled = 0; this.drained = false; this.lastPong = this.lastPull = Date.now(); this.waitingSince = null;
    port.onmessage = (e) => {
      const d = e.data || {};
      if (d.type === 'ack') { this.credit++; this.wake(); }
      else if (d.type === 'pong') this.lastPong = Date.now();
      else if (d.type === 'pulled') { if (d.bytes !== this.pulled) { this.pulled = d.bytes; this.lastPull = Date.now(); } }
      else if (d.type === 'drained') { this.drained = true; this.wake(); }
      else if (d.type === 'cancelled') this.fail('the browser cancelled the download (' + (d.reason || 'cancelled') + ')');
    };
  }
  wake() { this.waiters.splice(0).forEach((w) => w()); }
  fail(why) { if (!this.failed) { this.failed = new SinkStopped(why); this.wake(); } }
  async open() {
    this.credit = 1;
    this.timer = setInterval(() => {
      const now = Date.now();
      this.port.postMessage({ type: 'ping' });
      if (now - this.lastPong > DEAD_MS) this.fail('the service worker stopped');
      else if (this.waitingSince !== null && now - Math.max(this.waitingSince, this.lastPull) > STALL_MS) this.fail('the browser stopped reading the download');
    }, PING_MS);
  }
  async until(ready) {               // wait for ready(), or a failure
    this.waitingSince = Date.now();
    try {
      while (!ready()) {
        if (this.failed) throw this.failed;
        await new Promise((r) => this.waiters.push(r));
      }
      if (this.failed) throw this.failed;
    } finally { this.waitingSince = null; }
  }
  async write(u8) {
    await this.until(() => this.credit > 0);
    this.credit--;
    const c = u8.slice();            // a private copy to transfer
    this.port.postMessage({ type: 'chunk', chunk: c }, [c.buffer]);
  }
  async close() {
    this.port.postMessage({ type: 'end' });
    try { await this.until(() => this.drained); } finally { clearInterval(this.timer); }
  }
  async abort(reason) { clearInterval(this.timer); this.port.postMessage({ type: 'error', reason: String(reason) }); }
}

class OpfsSink {                     // browser-private storage (folder lab-build/); the page offers the file after
  constructor(name) { this.name = name; }
  async dir() { return (await navigator.storage.getDirectory()).getDirectoryHandle('lab-build', { create: true }); }
  async open() {
    const dir = await this.dir();
    await dir.removeEntry(this.name).catch(() => {});
    this.fh = await dir.getFileHandle(this.name, { create: true });
    this.h = await this.fh.createSyncAccessHandle(); this.pos = 0;
  }
  async write(u8) { let n = 0; while (n < u8.length) n += this.h.write(u8.subarray(n), { at: this.pos + n }); this.pos += u8.length; }
  async close() { this.h.flush(); this.h.close(); }
  async abort() {
    try { this.h.close(); } catch (e) { /* closed */ }
    await (await this.dir()).removeEntry(this.name).catch(() => {});
  }
}

// ---------------------------------------------------------------- sources
// Reads verified chunks of one source file: chunk k is bytes
// [k*C, min((k+1)*C, size)), checked against recipe sha256[k].
class Source {
  constructor(name, desc, read, net) {
    this.name = name; this.size = desc.size; this.C = desc.chunk; this.hashes = desc.sha256;
    this.read = read; this.net = net; this.cache = new Map(); this.inflight = new Map();
    if (Math.ceil(this.size / this.C) !== this.hashes.length) throw new Error(`${name}: ${this.hashes.length} chunk hashes for ${this.size} bytes`);
  }
  span(k) { return [k * this.C, Math.min((k + 1) * this.C, this.size)]; }
  get(k) {
    // Always a promise: prefetch() chains .catch() on what this returns.
    if (this.cache.has(k)) return Promise.resolve(this.cache.get(k));
    if (!this.inflight.has(k)) {
      const [a, b] = this.span(k);
      this.inflight.set(k, (async () => {
        const buf = await this.read(a, b);
        if (buf.length !== b - a) throw new Error(`${this.name} chunk ${k}: ${buf.length} bytes, want ${b - a}`);
        const got = await sha256(buf);
        if (got !== this.hashes[k]) throw new Error(`${this.name} chunk ${k} does not match the recipe (SHA-256 ${got.slice(0, 12)}…)`);
        this.cache.set(k, buf); this.inflight.delete(k);
        return buf;
      })());
    }
    return this.inflight.get(k);
  }
  drop(k) { this.cache.delete(k); }
}

function fileReader(file, net) {
  return async (a, b) => {
    const t = performance.now();
    const u8 = new Uint8Array(await file.slice(a, b).arrayBuffer());
    net.local_ms += performance.now() - t; net.local_bytes += u8.length;
    return u8;
  };
}

function memoryReader(u8) {          // a pack the page has downloaded and checked
  return async (a, b) => u8.subarray(a, b);
}

// ---------------------------------------------------------------- run
async function run(o) {
  const t0 = performance.now();
  const recipe = o.recipe;
  if (recipe.format !== 'streetzim-recipe/2') throw new Error('unknown recipe format ' + recipe.format);
  if (o.baseFile.size !== recipe.base.size) {
    return { ok: false, outcome: 'wrong_base', error: `This is not the expected base file: it has ${o.baseFile.size} bytes, the recipe needs ${recipe.base.file} (${recipe.base.size} bytes).` };
  }
  const net = { local_bytes: 0, local_ms: 0 };
  const sources = { base: new Source('base file', recipe.base, fileReader(o.baseFile, net), net) };
  for (const [id, p] of Object.entries(recipe.packs)) {
    const bytes = o.packs && o.packs[id];
    if (!bytes || bytes.length !== p.size) throw new Error(`the ${id} pack was not downloaded`);
    sources['pack:' + id] = new Source(`${id} pack`, p, memoryReader(bytes), net);
  }

  const last = recipe.segments[recipe.segments.length - 1];
  if (!last || last.src !== 'md5' || recipe.segments.filter((x) => x.src === 'md5').length !== 1) throw new Error('the recipe must end with the checksum');
  // The reads, in order: [sourceKey, chunk index, take from, take to] per piece.
  const steps = [];
  for (const s of recipe.segments) {
    if (s.src === 'inline' || s.src === 'md5') { steps.push(s); continue; }
    const key = s.src === 'base' ? 'base' : 'pack:' + s.pack;
    const src = sources[key];
    if (!src) throw new Error('recipe names an unknown source ' + key);
    for (let off = s.off; off < s.off + s.len;) {
      const k = Math.floor(off / src.C), [a, b] = src.span(k);
      const end = Math.min(b, s.off + s.len);
      steps.push({ src: key, k, from: off - a, to: end - a });
      off = end;
    }
  }
  // A chunk stays cached from its first read until its last use. The tail of
  // a merged ZIM interleaves hundreds of small base ranges (from one or two
  // chunks) with generated pieces, so dropping a chunk whenever the next
  // step differs re-read it hundreds of times. lastUse[i] says step i is the
  // last to need its chunk; MAX_LIVE caps memory if a plan ever spreads a
  // chunk's uses far apart (the chunk with the furthest next use goes first).
  const MAX_LIVE = 6;
  const keyOf = (s) => s.src + '#' + s.k;
  const nextUse = new Array(steps.length).fill(Infinity);
  const lastSeen = new Map();
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].k === undefined) continue;
    const key = keyOf(steps[i]);
    if (lastSeen.has(key)) nextUse[i] = lastSeen.get(key);
    lastSeen.set(key, i);
  }
  const pendingUse = new Map();        // key -> index of its next use, for cached chunks
  const release = (i) => {
    const s = steps[i], key = keyOf(s);
    if (nextUse[i] === Infinity) { sources[s.src].drop(s.k); pendingUse.delete(key); return; }
    pendingUse.set(key, nextUse[i]);
    let live = 0;
    for (const src of Object.values(sources)) live += src.cache.size;
    while (live > MAX_LIVE) {
      let worst = null;
      for (const [k2, at] of pendingUse) if (!worst || at > worst[1]) worst = [k2, at];
      if (!worst) break;
      const [srcKey, k] = worst[0].split('#');
      sources[srcKey].drop(+k); pendingUse.delete(worst[0]); live--;
    }
  };

  // Prefetch: read up to 2 chunks ahead.
  const ahead = [];
  let prevKey = null;
  for (let i = 0; i < steps.length; i++) {
    if (steps[i].k === undefined) continue;
    const key = keyOf(steps[i]);
    if (key !== prevKey) ahead.push(i);
    prevKey = key;
  }
  let nextAhead = 0;
  const prefetch = (i) => {
    while (nextAhead < ahead.length && ahead[nextAhead] < i) nextAhead++;
    for (let j = nextAhead; j < ahead.length && j < nextAhead + 2; j++) {
      const s = steps[ahead[j]];
      sources[s.src].get(s.k).catch(() => {});
    }
  };

  const sink = o.sink === 'fsa' ? new WritableSink(o.handle)
    : o.sink === 'sw' ? new SwDownloadSink(o.port)
    : o.sink === 'opfs' ? new OpfsSink(o.fileName)
    : null;
  if (!sink) throw new Error('unknown sink ' + o.sink);
  const md5 = await hashwasm.createMD5(); md5.init();
  await sink.open(recipe.size);
  let written = 0, lastProg = 0, digest = null, fileMd5 = null;
  try {
    for (let i = 0; i < steps.length; i++) {
      const s = steps[i];
      let data;
      if (s.src === 'inline') {
        data = b64dec(s.data);
        if (data.length !== s.len || await sha256(data) !== s.sha256) throw new Error(`generated piece at ${written} does not match the recipe`);
      } else if (s.src === 'md5') {
        // The ZIM's own checksum covers every byte before it. The MD5 of the
        // whole file (what md5sum prints) also covers these 16 bytes: resume
        // the hash from its state before the digest and add them.
        const state = md5.save();
        data = md5.digest('binary');
        digest = hex(data);
        if (digest !== recipe.md5) throw new Error(`checksum mismatch: got ${digest}, want ${recipe.md5}`);
        md5.load(state); md5.update(data);
        fileMd5 = md5.digest('hex');
        if (recipe.file_md5 && fileMd5 !== recipe.file_md5) throw new Error(`checksum mismatch: whole file ${fileMd5}, want ${recipe.file_md5}`);
      } else {
        prefetch(i);
        const buf = await sources[s.src].get(s.k);
        data = buf.subarray(s.from, s.to);
        release(i);
      }
      if (s.src !== 'md5') md5.update(data);
      const n = data.length;
      await sink.write(data);
      written += n;
      const now = performance.now();
      if (now - lastProg > 300) { lastProg = now; postMessage({ type: 'progress', written, size: recipe.size, ms: now - t0, saved: sink.pulled }); }
    }
    postMessage({ type: 'progress', written, size: recipe.size, ms: performance.now() - t0, saved: sink.pulled, closing: true });
    await sink.close();
  } catch (e) {
    await sink.abort(e);
    const outcome = e instanceof SinkStopped ? 'download_stopped' : /checksum|match/.test(String(e.message)) ? 'hash_mismatch' : 'error';
    return { ok: false, outcome, error: String(e && e.message || e), written, net: summary(net), ms: Math.round(performance.now() - t0) };
  }
  const ms = performance.now() - t0;
  return { ok: true, outcome: 'ok', md5: digest, file_md5: fileMd5, size: recipe.size, written, ms: Math.round(ms),
           mbps_total: +(written / 1e6 / (ms / 1000)).toFixed(1), net: summary(net) };
}

function summary(net) {
  return { local_mb: +(net.local_bytes / 1e6).toFixed(1),
           local_mbps: net.local_ms ? +(net.local_bytes / 1e6 / (net.local_ms / 1000)).toFixed(1) : null };
}

onmessage = async (e) => {
  if (e.data.cmd !== 'run') return;
  let result;
  try { result = await run(e.data); } catch (err) { result = { ok: false, outcome: 'error', error: String(err && err.message || err) }; }
  postMessage({ type: 'done', result });
};
