// Assembles a map in other languages from a recipe (streetzim-recipe/2),
// in a dedicated worker. Plain JS (ES2020), no build step.
//
// A recipe lists the merged ZIM's bytes in order: ranges of the base ZIM
// (the visitor's own copy, a File), ranges of Wikipedia packs (fetched
// through the site's range proxy), and small generated pieces (inline,
// base64). Every byte is checked before it is written: base and pack bytes
// against the recipe's SHA-256 of each 16 MiB chunk of their file, inline
// pieces against their own SHA-256. The ZIM's MD5 is computed as the bytes
// go out, appended as its last 16 bytes, and compared with the recipe's; so
// is the MD5 of the whole file (recipe file_md5, what md5sum prints).
//
// In:  {cmd:'run', recipe, recipeUrl, baseFile, sink, handle?, port?,
//       inflight?, maxRetries?}
// Out: {type:'progress', written, size, ms, net}  {type:'done', result}
/* global hashwasm */
importScripts('vendor/md5.umd.min.js');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
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

class SwDownloadSink {               // the page's service worker streams a download
  constructor(port) {
    this.port = port; this.waiters = []; this.credit = 0; this.cancelled = null;
    port.onmessage = (e) => {
      if (e.data.type === 'ack') { this.credit++; const w = this.waiters.shift(); if (w) { this.credit--; w(); } }
      else if (e.data.type === 'cancelled') { this.cancelled = e.data.reason || 'cancelled'; this.waiters.splice(0).forEach((w) => w()); }
    };
  }
  async open() { this.credit = 1; }
  async write(u8) {
    if (this.credit > 0) this.credit--; else await new Promise((r) => this.waiters.push(r));
    if (this.cancelled) throw new Error('download cancelled: ' + this.cancelled);
    const c = u8.slice();            // a private copy to transfer
    this.port.postMessage({ type: 'chunk', chunk: c }, [c.buffer]);
  }
  async close() { this.port.postMessage({ type: 'end' }); }
  async abort(reason) { this.port.postMessage({ type: 'error', reason: String(reason) }); }
}

class OpfsSink {                     // browser-private storage; the page offers the file after
  constructor(name) { this.name = name; }
  async open() {
    const root = await navigator.storage.getDirectory();
    await root.removeEntry(this.name).catch(() => {});
    this.fh = await root.getFileHandle(this.name, { create: true });
    this.h = await this.fh.createSyncAccessHandle(); this.pos = 0;
  }
  async write(u8) { let n = 0; while (n < u8.length) n += this.h.write(u8.subarray(n), { at: this.pos + n }); this.pos += u8.length; }
  async close() { this.h.flush(); this.h.close(); }
  async abort() {
    try { this.h.close(); } catch (e) { /* closed */ }
    const root = await navigator.storage.getDirectory(); await root.removeEntry(this.name).catch(() => {});
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

function httpReader(url, size, net, maxRetries) {
  return async (a, b) => {
    let last;
    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      if (attempt) { net.retries++; await sleep(Math.min(8000, 300 * 2 ** (attempt - 1))); }
      try {
        const t0 = performance.now();
        const r = await fetch(url, { headers: { Range: `bytes=${a}-${b - 1}` }, cache: 'no-store' });
        const ttfb = performance.now() - t0;
        if (r.status !== 206) throw new Error(`HTTP ${r.status}`);
        const cr = r.headers.get('Content-Range');
        const m = cr && /^bytes (\d+)-(\d+)\/(\d+|\*)$/.exec(cr);
        if (cr && (!m || +m[1] !== a || +m[2] !== b - 1 || (m[3] !== '*' && +m[3] !== size))) throw new Error(`Content-Range ${cr}`);
        const u8 = new Uint8Array(await r.arrayBuffer());
        const ms = performance.now() - t0;
        net.requests++; net.bytes += u8.length; net.ms += ms; net.ttfb.push(Math.round(ttfb));
        net.mbps.push(+(u8.length / 1e6 / (ms / 1000)).toFixed(2));
        if (u8.length !== b - a) throw new Error(`short body ${u8.length}`);
        return u8;
      } catch (e) {
        last = e; net.errors++;
        if (/network|reset|fetch/i.test(String(e && e.message))) net.resets++;
      }
    }
    throw new Error(`fetch failed after ${maxRetries + 1} tries: ${last && last.message}`);
  };
}

// ---------------------------------------------------------------- run
async function run(o) {
  const t0 = performance.now();
  const recipe = o.recipe;
  if (recipe.format !== 'streetzim-recipe/2') throw new Error('unknown recipe format ' + recipe.format);
  if (o.baseFile.size !== recipe.base.size) {
    return { ok: false, outcome: 'wrong_base', error: `This is not the expected base file: it has ${o.baseFile.size} bytes, the recipe needs ${recipe.base.file} (${recipe.base.size} bytes).` };
  }
  const net = { requests: 0, bytes: 0, ms: 0, ttfb: [], mbps: [], retries: 0, errors: 0, resets: 0, local_bytes: 0, local_ms: 0 };
  const maxRetries = o.maxRetries ?? 6;
  const sources = { base: new Source('base file', recipe.base, fileReader(o.baseFile, net), net) };
  for (const [id, p] of Object.entries(recipe.packs)) sources['pack:' + id] = new Source(`${id} pack`, p, httpReader(p.url, p.size, net, maxRetries), net);

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

  // Prefetch: keep up to `inflight` network chunks (and 2 local) ahead.
  const inflight = o.inflight || 4;
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
    let netN = 0, locN = 0;
    for (let j = nextAhead; j < ahead.length && (netN < inflight || locN < 2); j++) {
      const s = steps[ahead[j]];
      if (s.src === 'base') { if (locN < 2) { sources.base.get(s.k).catch(() => {}); locN++; } }
      else if (netN < inflight) { sources[s.src].get(s.k).catch(() => {}); netN++; }
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
      if (now - lastProg > 300) { lastProg = now; postMessage({ type: 'progress', written, size: recipe.size, ms: now - t0, net: summary(net) }); }
    }
    await sink.close();
  } catch (e) {
    await sink.abort(e);
    return { ok: false, outcome: /checksum|match/.test(String(e.message)) ? 'hash_mismatch' : 'error', error: String(e && e.message || e), written, net: summary(net), ms: Math.round(performance.now() - t0) };
  }
  const ms = performance.now() - t0;
  return { ok: true, outcome: 'ok', md5: digest, file_md5: fileMd5, size: recipe.size, written, ms: Math.round(ms),
           mbps_total: +(written / 1e6 / (ms / 1000)).toFixed(1), net: summary(net) };
}

function pct(a, p) { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; }
function summary(net) {
  return { requests: net.requests, mb: +(net.bytes / 1e6).toFixed(1),
           mbps_avg: net.ms ? +(net.bytes / 1e6 / (net.ms / 1000)).toFixed(2) : null,
           mbps_p10: pct(net.mbps, 0.1), ttfb_p50: pct(net.ttfb, 0.5),
           retries: net.retries, errors: net.errors, resets: net.resets,
           local_mb: +(net.local_bytes / 1e6).toFixed(1),
           local_mbps: net.local_ms ? +(net.local_bytes / 1e6 / (net.local_ms / 1000)).toFixed(1) : null };
}

onmessage = async (e) => {
  if (e.data.cmd !== 'run') return;
  let result;
  try { result = await run(e.data); } catch (err) { result = { ok: false, outcome: 'error', error: String(err && err.message || err) }; }
  postMessage({ type: 'done', result });
};
