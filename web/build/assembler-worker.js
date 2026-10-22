// Assembles a map in other languages from a recipe (streetzim-recipe/2),
// in a dedicated worker. Plain JS (ES2020), no build step.
//
// A recipe lists the merged ZIM's bytes in order: ranges of the base ZIM
// (the visitor's own copy, a File, or else downloaded here through the range
// proxy as it is needed), ranges of Wikipedia packs (downloaded and checked
// by the page before the build starts, passed in as bytes), and small
// generated pieces (inline, base64). Every byte is checked before it is
// written: base and pack bytes
// against the recipe's SHA-256 of each 16 MiB chunk of their file, inline
// pieces against their own SHA-256. The ZIM's MD5 is computed as the bytes
// go out, appended as its last 16 bytes, and compared with the recipe's; so
// is the MD5 of the whole file (recipe file_md5, what md5sum prints).
//
// In:  {cmd:'run', proto, recipe, baseFile | baseUrl, packs: {id: Uint8Array},
//       sink: 'fsa'|'sw'|'opfs'|'later', handle?, port?, fileName}
//      {cmd:'sink', sink, handle?, port?}   answers 'need-sink' when sink was 'later'
// Out: {type:'need-sink'} once the base's first chunk (and its ZIM UUID) is
//      checked, so nothing is saved before the base is known to be right;
//      {type:'progress', written, size, ms, saved, base?}  {type:'note', text}
//      {type:'done', result}
/* global hashwasm, SZFetch */
importScripts('vendor/md5.umd.min.js', 'fetcher.js?v=4');

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
// - every tick (PING_MS) sends a ping that the service worker answers;
//   DEAD_PINGS ticks in a row without an answer mean the browser stopped
//   it, however far apart the ticks are (a hidden tab's timers can be
//   throttled to one a minute);
// - a write that waits STALL_MS for room (an 'ack'), or an end that waits
//   STALL_MS without the browser reading more ('pulled'), means the browser
//   is not reading the download (never started, cancelled, or stuck);
// - 'cancelled' from the service worker ends it at once.
// A tick that comes more than 3 times later than the ticks before it (at
// least 3 x PING_MS) means the device slept: the clocks of the time-based
// checks restart instead of firing, and the downloads resume on their own
// (fetcher.js). Steadily slow ticks (throttling) are not sleep: after the
// first one they set the pace, and the checks run on real time again.
// close() returns only when the browser has read the whole stream.
const PING_MS = 5000, DEAD_PINGS = 4, STALL_MS = 60000;
class SinkStopped extends Error {
  constructor(msg, watchdog) { super(msg); this.watchdog = !!watchdog; }
}
class SwDownloadSink {
  constructor(port) {
    this.port = port; this.waiters = []; this.credit = 0; this.failed = null;
    this.pulled = 0; this.drained = false; this.lastPong = this.lastPull = Date.now(); this.waitingSince = null;
    port.onmessage = (e) => {
      const d = e.data || {};
      if (d.type === 'ack') { this.credit++; this.wake(); }
      else if (d.type === 'pong') { this.lastPong = Date.now(); this.unanswered = 0; }
      else if (d.type === 'pulled') { if (d.bytes !== this.pulled) { this.pulled = d.bytes; this.lastPull = Date.now(); } }
      else if (d.type === 'drained') { this.drained = true; this.wake(); }
      else if (d.type === 'cancelled') this.fail('the browser cancelled the download (' + (d.reason || 'cancelled') + ')');
    };
  }
  wake() { this.waiters.splice(0).forEach((w) => w()); }
  fail(why, watchdog) { if (!this.failed) { this.failed = new SinkStopped(why, watchdog); this.wake(); } }
  async open() {
    this.credit = 1;
    this.unanswered = 0;
    this.lastTick = Date.now();
    this.pace = PING_MS;             // the recent gap between ticks
    this.timer = setInterval(() => {
      const now = Date.now(), gap = now - this.lastTick;
      const slept = gap > 3 * Math.max(PING_MS, this.pace);
      this.pace = gap; this.lastTick = now;
      if (this.unanswered >= DEAD_PINGS) { this.fail('the service worker stopped', true); return; }
      this.port.postMessage({ type: 'ping' });
      this.unanswered++;
      if (slept) {                   // woke from sleep: the time-based checks start again
        this.suspensions = (this.suspensions || 0) + 1;
        this.lastPull = now;
        if (this.waitingSince !== null) this.waitingSince = now;
        return;
      }
      if (this.waitingSince !== null && now - Math.max(this.waitingSince, this.lastPull) > Math.max(STALL_MS, 3 * this.pace)) this.fail('the browser stopped reading the download', true);
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
  // own: the caller is done with u8's whole buffer, so it moves to the
  // service worker as it is; otherwise a private copy is transferred.
  async write(u8, own) {
    await this.until(() => this.credit > 0);
    this.credit--;
    const c = own && u8.byteOffset === 0 && u8.byteLength === u8.buffer.byteLength ? u8 : u8.slice();
    this.port.postMessage({ type: 'chunk', chunk: c }, [c.buffer]);
    return c === u8;                 // true: u8 is gone (transferred)
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
  // flush() and close() return promises in older Safari (15.2-16.x); await them.
  async close() { await this.h.flush(); await this.h.close(); }
  async abort() {
    // Never throws: storage that cannot even be opened (Firefox private
    // mode) must not hide the error that stopped the build.
    try { await this.h.close(); } catch (e) { /* closed, or never opened */ }
    try { await (await this.dir()).removeEntry(this.name); } catch (e) { /* nothing to delete */ }
  }
}

// ---------------------------------------------------------------- sources
// Reads verified chunks of one source file: chunk k is bytes
// [k*C, min((k+1)*C, size)), checked against recipe sha256[k]. A base's
// desc.mask lists [offset, length] ranges (its viewer slots and its own
// checksum) that the hashes were taken with zeroed: they are zeroed here
// too before hashing (the recipe carries the slots inline and never reads
// them from the base), so a copy one viewer patch older or newer passes.
function maskChunk(buf, start, mask) {
  const end = start + buf.length;
  for (const [off, n] of mask || []) {
    const lo = Math.max(off, start), hi = Math.min(off + n, end);
    if (lo < hi) buf.fill(0, lo - start, hi - start);
  }
}
class Source {
  constructor(name, desc, read, net) {
    this.name = name; this.size = desc.size; this.C = desc.chunk; this.hashes = desc.sha256; this.mask = desc.mask || null;
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
        // A downloaded chunk that does not match is downloaded once more.
        for (let pass = 0; ; pass++) {
          const buf = await this.read(a, b);
          if (buf.length !== b - a) throw new Error(`${this.name} chunk ${k}: ${buf.length} bytes, want ${b - a}`);
          if (this.mask) maskChunk(buf, a, this.mask);
          const got = await sha256(buf);
          if (got === this.hashes[k]) { this.cache.set(k, buf); this.inflight.delete(k); return buf; }
          if (!this.refetch || pass >= 1) throw new Error(`${this.name} chunk ${k} does not match the recipe (SHA-256 ${got.slice(0, 12)}…)`);
          this.net.hash_retries = (this.net.hash_retries || 0) + 1;
        }
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

// The base map over HTTP, for a visitor without a copy: whole 16 MiB chunks
// through the range proxy (fetcher.js: stall timeout, resume, laggards asked
// again, retries with backoff up to 30 s). run() keeps `win.k` chunks in
// flight ahead of the assembly: 4 at first, one more (up to 6) each time the
// throughput of the last 4 chunks rose by 10 %, one fewer (down to 4) when it
// fell by 20 %.
// mem.window [min, max] chunks in flight ([4, 6], or [2, 2] on a device with
// little memory). After 8 failed tries a chunk waits for the connection and
// tries again (fetcher.js patienceMs) until nothing has arrived for 30 min;
// net.waiting then says how long it has been. Chunk buffers come from a pool
// that release() refills, so a long download does not churn 16 MiB buffers.
const PATIENCE_MS = 30 * 60000;
function httpReader(url, size, chunk, net, signal, onRetry, mem) {
  const [kMin, kMax] = mem.window;
  const win = { k: kMin, done: 0, t: performance.now(), bytes: 0, rate: 0 };
  const pool = [];
  const read = async (a, b) => {
    const out = b - a === chunk && pool.length ? pool.pop() : new Uint8Array(b - a);
    if (!net.base_t0) net.base_t0 = performance.now();
    try {
      await SZFetch.fetchRetrying(url, out, a, a, b, size, signal, net.http, (n) => { net.base_bytes += n; }, onRetry,
        { tries: 8, maxBackoffMs: 30000, patienceMs: mem.patienceMs || PATIENCE_MS, onWait: (ms) => { net.waiting = ms; net.waitingAt = performance.now(); } });
    } catch (e) { e.baseDownload = true; throw e; }
    net.base_t1 = performance.now();
    if (++win.done % 4 === 0) {
      const now = performance.now();
      const rate = (net.base_bytes - win.bytes) / ((now - win.t) / 1000);
      if (win.rate && rate > win.rate * 1.1 && win.k < kMax) win.k++;
      else if (win.rate && rate < win.rate * 0.8 && win.k > kMin) win.k--;
      win.rate = rate; win.t = now; win.bytes = net.base_bytes;
    }
    return out;
  };
  const free = (u8) => { if (u8.byteLength === chunk && pool.length < kMax + 1) pool.push(u8); };
  return { read, win, free };
}

// ---------------------------------------------------------------- run
async function run(o) {
  const t0 = performance.now();
  const recipe = o.recipe;
  if (recipe.format !== 'streetzim-recipe/2') throw new Error('unknown recipe format ' + recipe.format);
  const net = { local_bytes: 0, local_ms: 0, base_bytes: 0, http: SZFetch.newNet(), hash_retries: 0 };
  const abort = new AbortController();
  let http = null;
  const mem = o.mem || { window: [4, 6] };
  if (o.baseUrl) {
    http = httpReader(o.baseUrl, recipe.base.size, recipe.base.chunk, net, abort.signal, (text) => postMessage({ type: 'note', text }), mem);
  } else if (!o.baseFile || o.baseFile.size !== recipe.base.size) {
    return { ok: false, outcome: 'wrong_base', error: `This is not the expected base file (${recipe.base.file}, ${recipe.base.size} bytes).` };
  }
  const sources = { base: new Source('base file', recipe.base, http ? http.read : fileReader(o.baseFile, net), net) };
  sources.base.refetch = !!http;
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
    if (src.mask && src.mask.some(([o, n]) => s.off < o + n && o < s.off + s.len)) throw new Error('the recipe reads a masked viewer slot from the base');
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
  // step differs re-read it hundreds of times. nextUse[i] is the next step
  // that needs step i's chunk (Infinity after its last use); MAX_LIVE caps
  // memory if a plan ever spreads a chunk's uses far apart (the chunk with
  // the furthest next use goes first).
  const keyOf = (s) => s.src + '#' + s.k;
  const nextUse = new Array(steps.length).fill(Infinity);
  const lastSeen = new Map();
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].k === undefined) continue;
    const key = keyOf(steps[i]);
    if (lastSeen.has(key)) nextUse[i] = lastSeen.get(key);
    lastSeen.set(key, i);
  }
  // How many chunks the plan needs to keep at once for a later use (1 for
  // California): from the network all of them are kept, so nothing is
  // downloaded twice; a plan needing more than KEEP_CAP stops here, before
  // anything is downloaded. From a local file at most 3 (reading again is
  // cheap).
  const KEEP_CAP = 8;
  let keepNeeded = 0;
  {
    const held = new Set();
    for (let i = 0; i < steps.length; i++) {
      if (steps[i].k === undefined) continue;
      const key = keyOf(steps[i]);
      if (nextUse[i] === Infinity) held.delete(key); else held.add(key);
      keepNeeded = Math.max(keepNeeded, held.size);
    }
  }
  if (http && keepNeeded > KEEP_CAP) {
    return { ok: false, outcome: 'error', error: `this language's recipe would keep ${keepNeeded} pieces of the map in memory at once (at most ${KEEP_CAP}); use "Already have the map?" with a downloaded map file` };
  }
  const MAX_LIVE = http ? keepNeeded : Math.min(keepNeeded, 3);
  const pendingUse = new Map();        // key -> index of its next use, for cached chunks
  const release = (i) => {
    const s = steps[i], key = keyOf(s);
    if (nextUse[i] === Infinity) { sources[s.src].drop(s.k); pendingUse.delete(key); return; }
    pendingUse.set(key, nextUse[i]);
    while (pendingUse.size > MAX_LIVE) {      // chunks kept for a later use; read-ahead is bounded apart
      let worst = null;
      for (const [k2, at] of pendingUse) if (!worst || at > worst[1]) worst = [k2, at];
      const [srcKey, k] = worst[0].split('#');
      sources[srcKey].drop(+k); pendingUse.delete(worst[0]);
    }
  };

  // Read ahead: the next 2 chunks from a file or memory; from the network,
  // the next http.win.k (the sliding window).
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
    const depth = http ? http.win.k : 2;
    const seen = new Set();
    for (let j = nextAhead; j < ahead.length && j < nextAhead + 64 && seen.size < depth; j++) {
      const s = steps[ahead[j]], key = keyOf(s);
      if (seen.has(key)) continue;
      seen.add(key);
      sources[s.src].get(s.k).catch(() => {});
    }
  };

  // The base must be the one the recipe was made from before anything is
  // saved: its first chunk (which holds the ZIM header) is checked against
  // the recipe's SHA-256, and its UUID (header bytes 8-24) against base.uuid.
  // Meanwhile the read-ahead starts, so the download runs while the page
  // sets up where to save.
  prefetch(0);
  try {
    const head = await sources.base.get(0);
    if (recipe.base.uuid && hex(head.subarray(8, 24)) !== recipe.base.uuid.replace(/-/g, '').toLowerCase()) throw new Error('the base map is not the one the recipe was made from (its UUID differs)');
  } catch (e) {
    abort.abort();
    return { ok: false, outcome: e.baseDownload ? 'base_download' : /match|UUID/.test(String(e.message)) ? 'wrong_base' : 'error', error: String(e && e.message || e), written: 0, net: summary(net), ms: Math.round(performance.now() - t0) };
  }
  if (o.sink === 'later') {
    postMessage({ type: 'need-sink' });
    Object.assign(o, await new Promise((r) => { sinkReply = r; }));
  }

  const sink = o.sink === 'fsa' ? new WritableSink(o.handle)
    : o.sink === 'sw' ? new SwDownloadSink(o.port)
    : o.sink === 'opfs' ? new OpfsSink(o.fileName)
    : null;
  if (!sink) throw new Error('unknown sink ' + o.sink);
  const md5 = await hashwasm.createMD5(); md5.init();
  let written = 0, digest = null, fileMd5 = null, lastUse = null;
  // Progress every 500 ms, also while the assembly waits for the network.
  const rate = [];                    // [ms, base bytes] over the last 30 s
  const progress = (extra) => {
    const now = performance.now();
    const msg = { type: 'progress', written, size: recipe.size, ms: now - t0, saved: sink.pulled };
    if (http) {
      rate.push([now, net.base_bytes]);
      while (rate.length > 2 && now - rate[0][0] > 30000) rate.shift();
      const [t1, b1] = rate[0];
      if (net.waitingAt && now - net.waitingAt < 2500) msg.waiting = net.waiting;
      msg.base = { got: net.base_bytes, size: recipe.base.size, bps: now > t1 ? (net.base_bytes - b1) / ((now - t1) / 1000) : 0, k: http.win.k,
                   avg: net.base_t0 && now > net.base_t0 ? net.base_bytes / ((now - net.base_t0) / 1000) : 0 };
    }
    postMessage(Object.assign(msg, extra));
  };
  const ticker = setInterval(progress, 500);
  try {
    await sink.open(recipe.size);
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
        // The chunk's last use: its buffer can move to the service worker
        // (no copy) or, after the write, go back to the download pool.
        if (s.src === 'base' && nextUse[i] === Infinity) lastUse = buf;
      }
      if (s.src !== 'md5') md5.update(data);
      const n = data.length;
      const moved = await sink.write(data, !!lastUse && data.buffer === lastUse.buffer);
      if (lastUse && !moved && http) http.free(lastUse);
      lastUse = null;
      written += n;
    }
    clearInterval(ticker);
    progress({ closing: true });
    await sink.close();
  } catch (e) {
    clearInterval(ticker);
    abort.abort();
    await sink.abort(e);
    const outcome = e instanceof SinkStopped ? 'download_stopped' : e.baseDownload ? 'base_download' : /checksum|match/.test(String(e.message)) ? 'hash_mismatch' : 'error';
    return { ok: false, outcome, watchdog: !!(e instanceof SinkStopped && e.watchdog), error: String(e && e.message || e), written, net: summary(net), ms: Math.round(performance.now() - t0) };
  }
  const ms = performance.now() - t0;
  return { ok: true, outcome: 'ok', md5: digest, file_md5: fileMd5, size: recipe.size, written, ms: Math.round(ms),
           mbps_total: +(written / 1e6 / (ms / 1000)).toFixed(1), net: summary(net) };
}

function summary(net) {
  const h = net.http || { ms: 0, mbps: [], ttfb: [] };
  return { local_mb: +(net.local_bytes / 1e6).toFixed(1),
           local_mbps: net.local_ms ? +(net.local_bytes / 1e6 / (net.local_ms / 1000)).toFixed(1) : null,
           base_mb: +((net.base_bytes || 0) / 1e6).toFixed(1),
           // wall clock, all requests together; then per request
           base_mbps: net.base_t1 > net.base_t0 ? +(net.base_bytes / 1e6 / ((net.base_t1 - net.base_t0) / 1000)).toFixed(2) : null,
           base_stream_mbps_avg: h.ms ? +(h.bytes / 1e6 / (h.ms / 1000)).toFixed(2) : null,
           base_stream_mbps_p10: SZFetch.pct(h.mbps, 0.1), base_ttfb_p50: SZFetch.pct(h.ttfb, 0.5),
           base_requests: h.requests, base_retries: h.retries, base_slow_retries: h.slow_retries,
           base_resets: h.resets, base_hash_retries: net.hash_retries || 0, base_waits: h.waits || 0,
           base_wait_s: Math.round((h.wait_ms || 0) / 1000) };
}

// PROTO: what builder.js sends. A tab opened before an update loads its old
// builder.js with this worker; it gets a plain "reload" instead of a failure.
const PROTO = 6;
let sinkReply = null;                // set while run() waits for {cmd:'sink'}
onmessage = async (e) => {
  if (e.data.cmd === 'sink') { if (sinkReply) { const r = sinkReply; sinkReply = null; r(e.data); } return; }
  if (e.data.cmd !== 'run') return;
  let result;
  if (e.data.proto !== PROTO) {
    postMessage({ type: 'done', result: { ok: false, outcome: 'stale_page', error: 'This page was updated while it was open. Reload it and try again.' } });
    return;
  }
  try { result = await run(e.data); } catch (err) { result = { ok: false, outcome: 'error', error: String(err && err.message || err) }; }
  postMessage({ type: 'done', result });
};
