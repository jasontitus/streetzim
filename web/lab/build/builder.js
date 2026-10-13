// The language builder page (/lab/build/): pick a region and a map language,
// open your copy of the region's map file, and the page adds Wikipedia in the
// chosen languages and saves a new ZIM. assembler-worker.js does the work.
//
// Analytics: GA4 (the site's Firebase project) gets packs_start, packs_end,
// build_start, build_progress (25/50/75%), build_end and build_probe, so we
// learn what download speeds visitors really get. Nothing is sent on localhost, from an
// automated browser, with ?notrack, or with localStorage.sz_dev set.
(function () {
  'use strict';
  const q = new URLSearchParams(location.search);
  const $ = (id) => document.getElementById(id);
  const PROBE_BYTES = 8 * 1048576;

  // ------------------------------------------------------------ analytics
  let logEvent = null;
  const devSession = (() => {
    try { if (localStorage.getItem('sz_dev')) return true; } catch (e) { /* no storage */ }
    return navigator.webdriver || q.has('notrack') || /^(localhost|127\.|\[::1\])/.test(location.hostname);
  })();
  const env = /--/.test(location.hostname) ? 'preview' : 'live';
  if (!devSession) {
    Promise.all([
      import('https://www.gstatic.com/firebasejs/11.0.2/firebase-app.js'),
      import('https://www.gstatic.com/firebasejs/11.0.2/firebase-analytics.js'),
    ]).then(([app, an]) => {
      const a = an.getAnalytics(app.initializeApp({
        apiKey: 'AIzaSyAGolk6U9X4PgPEY4VIbPvIvLE4usE5c60',
        authDomain: 'streetzim.firebaseapp.com',
        projectId: 'streetzim',
        storageBucket: 'streetzim.firebasestorage.app',
        messagingSenderId: '1056177608946',
        appId: '1:1056177608946:web:c6abf5a817f346b317986b',
        measurementId: 'G-S46FWQ9EGY',
      }));
      logEvent = (name, params) => { try { an.logEvent(a, name, Object.assign({ env }, params)); } catch (e) { /* ignore */ } };
    }).catch(() => {});
  }
  const track = (name, params) => { if (logEvent) logEvent(name, params); };
  $('trackNote').textContent = devSession ? 'Speed reports: off for this session.' : 'This page reports build speeds (no personal data) so we can tell how fast building is for visitors.';

  // ------------------------------------------------------------ state
  let index = null, region = null, entry = null, baseFile = null, running = null;
  let hiddenSince = null, hiddenMs = 0;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') hiddenSince = performance.now();
    else if (hiddenSince !== null) { hiddenMs += performance.now() - hiddenSince; hiddenSince = null; }
  });

  const fmtMB = (b) => (b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(0) + ' MB');
  const hex = (buf) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('');

  async function getJSON(url) {
    let r;
    try { r = await fetch(url, { cache: 'no-cache' }); } catch (e) { throw new Error('no connection (' + (e.message || e) + ')'); }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    try { return await r.json(); } catch (e) { throw new Error('the file is damaged'); }
  }

  async function load() {
    index = await getJSON('recipes/index.json');
    const sel = $('region');
    sel.innerHTML = '';
    for (const reg of index.regions) sel.add(new Option(reg.title, reg.id));
    const want = q.get('region');
    if (want && !index.regions.some((r) => r.id === want)) {
      $('notice').textContent = `No such region: “${want}”. Pick one from the list.`;
    } else if (want) sel.value = want;
    sel.onchange = () => { $('notice').textContent = ''; pickRegion(); };
    pickRegion();
  }

  function pickRegion() {
    region = index.regions.find((r) => r.id === $('region').value);
    if (!region) { $('notice').textContent = 'No such region. Pick one from the list.'; return; }
    const b = region.base;
    $('baseInfo').innerHTML = '';
    const a = document.createElement('a');
    a.href = b.download; a.textContent = b.file; a.rel = 'noopener';
    $('baseInfo').append('First download this map file (', a, `, ${fmtMB(b.size)}), then open it here. If you already have it, just open it.`);
    const sel = $('lang');
    sel.innerHTML = '';
    for (const m of region.menu) sel.add(new Option(m.label, m.lang));
    const pref = (navigator.languages || []).map((l) => l.split('-')[0]);
    const want = q.get('lang') || pref.find((l) => region.menu.some((m) => m.lang === l));
    if (want && region.menu.some((m) => m.lang === want)) sel.value = want;
    sel.onchange = pickLang;
    pickLang();
  }

  function pickLang() {
    entry = region.menu.find((m) => m.lang === $('lang').value);
    const ul = $('packs');
    ul.innerHTML = '';
    for (const p of entry.packs) {
      const li = document.createElement('li');
      li.textContent = p.in_base
        ? `${p.label}: ${p.articles.toLocaleString()} articles (already in the map file)`
        : `${p.label}: ${p.articles.toLocaleString()} articles, ${fmtMB(p.bytes)} to download`;
      ul.append(li);
    }
    $('total').textContent = `The new file: ${fmtMB(entry.size)}. To download now: ${fmtMB(entry.download_bytes)}.`;
    recipeFor(entry).catch((e) => { $('notice').textContent = `Could not load the recipe for ${entry.label} (${e.message || e}). Try again later.`; });
    checkBase();
  }

  $('baseFile').onchange = () => {
    baseFile = $('baseFile').files[0] || null;
    checkBase();
  };

  // ------------------------------------------------------------ base check
  // Before anything is downloaded or saved, the visitor's map file must be
  // the one the recipe was made from: same size, same ZIM UUID (header bytes
  // 8-24), and the SHA-256 of its first and last 16 MiB chunks as listed in
  // the recipe. The worker still checks every other chunk as it reads it.
  let baseOk = false, baseSeq = 0;
  const baseVerdicts = new Map();      // file+recipe base -> true | message
  async function verifyBase(file, b) {
    const wrong = `This isn't the right ${region.title} map file (a different version, or damaged). Download it again from the link above.`;
    if (file.size !== b.size) return wrong;
    const head = new Uint8Array(await file.slice(0, 24).arrayBuffer());
    if (head.length < 24 || head[0] !== 0x5a || head[1] !== 0x49 || head[2] !== 0x4d || head[3] !== 0x04) return wrong;
    if (b.uuid && hex(head.subarray(8, 24)) !== b.uuid.replace(/-/g, '').toLowerCase()) return wrong;
    const n = b.sha256.length;
    for (const k of n > 1 ? [0, n - 1] : [0]) {
      const a = k * b.chunk;
      const buf = await file.slice(a, Math.min(a + b.chunk, b.size)).arrayBuffer();
      if (hex(await crypto.subtle.digest('SHA-256', buf)) !== b.sha256[k]) return wrong;
    }
    return true;
  }
  async function checkBase() {
    const seq = ++baseSeq;
    const out = $('baseCheck');
    baseOk = false; out.className = ''; out.textContent = '';
    checkReady();
    if (!baseFile || !entry || !region) return;
    const file = baseFile;
    let verdict;
    if (file.size !== region.base.size) verdict = await verifyBase(file, region.base);
    else {
      out.textContent = 'Checking the file…';
      let b = null;
      try { b = (await recipeFor(entry)).base; } catch (e) { verdict = `Could not load the recipe for ${entry.label} (${e.message || e}). Try again later.`; }
      if (b) {
        const key = [file.name, file.size, file.lastModified, b.file, b.size, b.uuid, b.sha256[0], b.sha256[b.sha256.length - 1]].join('|');
        try {
          if (!baseVerdicts.has(key)) baseVerdicts.set(key, await verifyBase(file, b));
          verdict = baseVerdicts.get(key);
        } catch (e) { verdict = `Could not read the file (${e.message || e}). Open it again.`; }
      }
    }
    if (seq !== baseSeq) return;     // a newer file or language took over
    if (verdict === true) { baseOk = true; out.className = 'ok'; out.textContent = '✓ right file'; }
    else { out.className = 'bad'; out.textContent = verdict; }
    checkReady();
    ensurePacks();
  }

  // ------------------------------------------------------------ packs
  // All of the language's packs (each at most ~31 MB) are downloaded,
  // checked against the recipe's SHA-256 of each 16 MiB chunk and kept in
  // memory as soon as the language and a right map file are chosen. Build is
  // enabled only after that, so a missing or damaged pack stops the visitor
  // before any save target is opened or any byte is written.
  const STALL_MS = 30000;              // no byte for this long: abort and retry
  const PART = 4 * 1048576;            // range request size
  const PARALLEL = 4;                  // range requests in flight
  const TRIES = 5;                     // per range, with backoff 1, 2, 4, 8 s
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const langName = (() => {
    let dn = null;
    try { dn = new Intl.DisplayNames(['en'], { type: 'language' }); } catch (e) { /* old browser */ }
    return (code, fallback) => { try { const n = dn && dn.of(code); if (n && n !== code) return n; } catch (e) { /* unknown code */ } return fallback || code; };
  })();
  const packName = (id) => {
    const m = entry && entry.packs.find((p) => p.lang === id);
    return langName(id, m ? m.label.replace(/ Wikipedia$/, '') : id) + ' Wikipedia';
  };
  const newNet = () => ({ requests: 0, bytes: 0, ms: 0, ttfb: [], mbps: [], retries: 0, errors: 0, resets: 0, hash_retries: 0 });
  let net = newNet();
  let packsOk = false, packSeq = 0, packAbort = null;
  const packBytes = new Map();         // url|size|hashes -> verified Uint8Array
  const packKey = (p) => [p.url, p.size, p.sha256.join(',')].join('|');

  class FetchError extends Error {
    constructor(msg, permanent) { super(msg); this.permanent = !!permanent; }
  }
  // One range request. Aborted when no byte has arrived for STALL_MS (while
  // waiting for the answer, too); onBytes(n) is called as bytes arrive.
  async function fetchRange(url, a, b, size, signal, onBytes) {
    const ac = new AbortController();
    const outer = () => ac.abort();
    if (signal.aborted) throw new DOMException('aborted', 'AbortError');
    signal.addEventListener('abort', outer, { once: true });
    let timer = null, stalled = false;
    const arm = () => { clearTimeout(timer); timer = setTimeout(() => { stalled = true; ac.abort(); }, STALL_MS); };
    const t0 = performance.now();
    arm();
    try {
      const r = await fetch(url, { headers: { Range: `bytes=${a}-${b - 1}` }, cache: 'no-store', signal: ac.signal });
      const ttfb = performance.now() - t0;
      if (r.status !== 206) {
        const permanent = r.status >= 400 && r.status < 500 && r.status !== 408 && r.status !== 429;
        throw new FetchError(r.status === 200 ? 'the server ignored the range request' : `HTTP ${r.status}`, permanent);
      }
      const cr = r.headers.get('Content-Range');
      const m = cr && /^bytes (\d+)-(\d+)\/(\d+|\*)$/.exec(cr);
      if (cr && (!m || +m[1] !== a || +m[2] !== b - 1 || (m[3] !== '*' && +m[3] !== size))) throw new FetchError(`unexpected Content-Range ${cr}`);
      const buf = new Uint8Array(b - a);
      const reader = r.body.getReader();
      let n = 0;
      for (;;) {
        arm();
        const { done, value } = await reader.read();
        if (done) break;
        if (n + value.length > buf.length) throw new FetchError('the server sent too many bytes');
        buf.set(value, n); n += value.length; onBytes(value.length);
      }
      if (n !== b - a) throw new FetchError(`the server sent ${n} of ${b - a} bytes`);
      const ms = performance.now() - t0;
      net.requests++; net.bytes += n; net.ms += ms; net.ttfb.push(Math.round(ttfb));
      net.mbps.push(+(n / 1e6 / (ms / 1000)).toFixed(2));
      return buf;
    } catch (e) {
      if (signal.aborted) throw new DOMException('aborted', 'AbortError');
      if (stalled) throw new FetchError(`no data for ${STALL_MS / 1000} s`);
      if (e instanceof FetchError) throw e;
      net.resets++;
      throw new FetchError('the connection failed (' + (e.message || e) + ')');
    } finally {
      clearTimeout(timer);
      signal.removeEventListener('abort', outer);
    }
  }
  // A range, retried with backoff; onRetry(attempt, error) before each retry.
  async function fetchRetrying(url, a, b, size, signal, onBytes, onRetry) {
    let last;
    for (let attempt = 0; attempt < TRIES; attempt++) {
      if (attempt) { net.retries++; onRetry(attempt, last); await sleep(1000 * 2 ** (attempt - 1)); }
      let got = 0;
      try { return await fetchRange(url, a, b, size, signal, (n) => { got += n; onBytes(n); }); }
      catch (e) {
        onBytes(-got);                 // this attempt's bytes do not count
        if (e.name === 'AbortError') throw e;
        net.errors++; last = e;
        if (e.permanent) throw e;
      }
    }
    throw new FetchError(`${last.message}, ${TRIES} tries`);
  }
  // One pack, chunk by chunk; each chunk's SHA-256 must match the recipe. A
  // chunk that does not match is downloaded once more before giving up.
  async function downloadPack(p, signal, onProgress) {
    const out = new Uint8Array(p.size);
    let got = 0;
    const bump = (n) => { got += n; onProgress(got, null); };
    for (let k = 0; k < p.sha256.length; k++) {
      const a0 = k * p.chunk, b0 = Math.min(a0 + p.chunk, p.size);
      for (let pass = 0; ; pass++) {
        const parts = [];
        for (let a = a0; a < b0; a += PART) parts.push([a, Math.min(a + PART, b0)]);
        let next = 0, before = got;
        const lane = async () => {
          while (next < parts.length) {
            const [a, b] = parts[next++];
            out.set(await fetchRetrying(p.url, a, b, p.size, signal, bump, (i, e) => onProgress(got, `${e.message}; trying again (${i + 1} of ${TRIES})`)), a);
          }
        };
        await Promise.all(Array.from({ length: Math.min(PARALLEL, parts.length) }, lane));
        const sum = hex(await crypto.subtle.digest('SHA-256', out.subarray(a0, b0)));
        if (sum === p.sha256[k]) break;
        if (pass >= 1) throw new FetchError('it arrived damaged twice (it does not match its checksum)', true);
        net.hash_retries++;
        got = before; onProgress(got, 'a piece arrived damaged; downloading it again');
      }
    }
    return out;
  }
  async function ensurePacks() {
    const seq = ++packSeq;
    if (packAbort) packAbort.abort();
    packAbort = null; packsOk = false;
    const out = $('packStatus');
    out.className = ''; out.textContent = '';
    $('packRetry').hidden = true;
    checkReady();
    if (!baseOk || !entry) return;
    let rec;
    try { rec = await recipeFor(entry); } catch (e) { return; }   // #notice says why
    if (seq !== packSeq) return;
    const ids = Object.keys(rec.packs);
    const keep = new Set(ids.map((id) => packKey(rec.packs[id])));
    for (const k of [...packBytes.keys()]) if (!keep.has(k)) packBytes.delete(k);   // only this language's packs stay in memory
    const ac = packAbort = new AbortController();
    const t0 = performance.now();
    net = newNet();
    const todo = ids.filter((id) => !packBytes.has(packKey(rec.packs[id])));
    const evBase = { region: region.id, lang: entry.lang, packs: ids.join(','), download_mb: Math.round(todo.reduce((t, id) => t + rec.packs[id].size, 0) / 1e6) };
    if (todo.length) track('packs_start', evBase);
    try {
      for (const id of todo) {
        const p = rec.packs[id], name = packName(id);
        let shown = 0;
        const bytes = await downloadPack(p, ac.signal, (got, note) => {
          const now = performance.now();
          if (!note && now - shown < 250 && got < p.size) return;
          shown = now;
          out.textContent = `Downloading ${name}: ${(got / 1e6).toFixed(0)} of ${fmtMB(p.size)}` + (note ? ` — ${note}` : '');
        }).catch((e) => { e.pack = name; throw e; });
        packBytes.set(packKey(p), bytes);
      }
    } catch (e) {
      if (seq !== packSeq || e.name === 'AbortError') return;
      out.className = 'bad';
      out.textContent = /HTTP 40[34]|HTTP 410/.test(e.message)
        ? `${e.pack} isn't available right now (${e.message}). Nothing was saved. Try again later.`
        : `Could not download ${e.pack || 'the language pack'}: ${e.message}. Nothing was saved. Try again.`;
      $('packRetry').hidden = false;
      track('packs_end', Object.assign({}, evBase, netParams(), { outcome: /damaged/.test(e.message) ? 'hash_mismatch' : 'error', secs: Math.round((performance.now() - t0) / 1000) }));
      return;
    }
    if (seq !== packSeq) return;
    packAbort = null;
    if (todo.length) track('packs_end', Object.assign({}, evBase, netParams(), { outcome: 'ok', secs: Math.round((performance.now() - t0) / 1000) }));
    packsOk = true;
    out.className = 'ok';
    out.textContent = ids.length ? `✓ ${ids.map(packName).join(', ')} downloaded and checked (${fmtMB(ids.reduce((t, id) => t + rec.packs[id].size, 0))})` : '';
    checkReady();
  }
  $('packRetry').onclick = () => ensurePacks();

  function pct(a, p) { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; }
  function netParams() {
    return { net_mb: +(net.bytes / 1e6).toFixed(1), net_mbps_avg: net.ms ? +(net.bytes / 1e6 / (net.ms / 1000)).toFixed(2) : null,
             net_mbps_p10: pct(net.mbps, 0.1), ttfb_ms_p50: pct(net.ttfb, 0.5), retries: net.retries, resets: net.resets,
             hash_retries: net.hash_retries, requests: net.requests };
  }

  function checkReady() {
    $('go').disabled = !(baseOk && packsOk && region && entry) || !!running;
    for (const id of ['region', 'lang', 'baseFile']) $(id).disabled = !!running;
  }

  // The recipe of the chosen language; fetched once per language.
  const recipes = new Map();
  function recipeFor(e) {
    if (!recipes.has(e.recipe)) {
      const p = getJSON(e.recipe);
      p.catch(() => recipes.delete(e.recipe));     // a later try fetches it again
      recipes.set(e.recipe, p);
    }
    return recipes.get(e.recipe);
  }

  // ------------------------------------------------------------ sinks
  const within = (p, ms) => Promise.race([p, new Promise((r, j) => setTimeout(() => j(new Error('timed out')), ms))]);

  // The service worker that streams downloads, or null when it cannot be had
  // (no support, a failed registration, private modes): then OPFS is used.
  async function swController() {
    if (!('serviceWorker' in navigator)) return null;
    try {
      await within(navigator.serviceWorker.register('sw.js', { scope: './' }), 10000);
      await within(navigator.serviceWorker.ready, 10000);
      if (!navigator.serviceWorker.controller) {
        await new Promise((r) => { navigator.serviceWorker.addEventListener('controllerchange', r, { once: true }); setTimeout(r, 3000); });
      }
      return navigator.serviceWorker.controller;
    } catch (e) {
      console.warn('service worker unavailable, saving through browser storage instead:', e);
      return null;
    }
  }

  async function prepareSink(fileName, size) {
    if (window.showSaveFilePicker) {
      const handle = await window.showSaveFilePicker({ suggestedName: fileName, types: [{ description: 'ZIM file', accept: { 'application/octet-stream': ['.zim'] } }] });
      return { sink: 'fsa', handle, transfer: [] };
    }
    const ctl = await swController();
    if (ctl) {
      const ch = new MessageChannel();
      const id = Math.random().toString(36).slice(2);
      const ok = new Promise((r) => { ch.port1.onmessage = (e) => { if (e.data.type === 'registered') r(); }; });
      ctl.postMessage({ type: 'register', id, filename: fileName, size }, [ch.port2]);
      let registered = true;
      await within(ok, 5000).catch(() => { registered = false; });
      ch.port1.onmessage = null;
      if (!registered) return { sink: 'opfs', transfer: [] };
      const ifr = document.createElement('iframe');
      ifr.style.display = 'none'; ifr.src = '__dl/' + encodeURIComponent(id);
      document.body.append(ifr);
      return { sink: 'sw', port: ch.port1, transfer: [ch.port1] };
    }
    return { sink: 'opfs', transfer: [] };
  }

  // ------------------------------------------------------------ build
  $('go').onclick = async () => {
    // One build at a time: claim the slot before the first await, so a double
    // click cannot start a second worker or a second save dialog.
    if (running || $('go').disabled) return;
    running = { starting: true };
    $('go').disabled = true;
    let recipe, fileName, sinkInfo;
    try {
      recipe = await recipeFor(entry);
      fileName = `${recipe.file_name || recipe.name}.zim`;
      sinkInfo = await prepareSink(fileName, recipe.size);
    } catch (e) {
      running = null; checkReady();
      $('status').textContent = recipe ? 'Not started: ' + (e.message || e)
        : `Not started: could not load the recipe for ${entry.label} (${e.message || e}). Try again later.`;
      return;
    }
    const packs = Object.keys(recipe.packs).join(',');
    const bytes = {};
    for (const id of Object.keys(recipe.packs)) bytes[id] = packBytes.get(packKey(recipe.packs[id]));
    const t0 = performance.now();
    hiddenMs = 0; hiddenSince = document.visibilityState === 'hidden' ? performance.now() : null;
    const base = { region: region.id, lang: entry.lang, packs, size_mb: Math.round(recipe.size / 1e6),
                   download_mb: Math.round(entry.download_bytes / 1e6), sink: sinkInfo.sink };
    track('build_start', base);
    const worker = new Worker('assembler-worker.js');
    running = { worker, base, t0, sent: new Set() };
    checkReady();
    $('status').textContent = 'Starting…';
    const result = await new Promise((resolve) => {
      worker.onmessage = (e) => {
        if (e.data.type === 'progress') {
          const { written, size, ms } = e.data;
          $('prog').value = written / size;
          $('status').textContent = `${fmtMB(written)} of ${fmtMB(size)} — keep this tab open`;
          for (const p of [25, 50, 75]) {
            if (written / size * 100 >= p && !running.sent.has(p)) {
              running.sent.add(p);
              track('build_progress', Object.assign({}, base, { pct: p, elapsed_s: Math.round(ms / 1000) }));
            }
          }
        } else if (e.data.type === 'done') resolve(e.data.result);
      };
      worker.onerror = (e) => resolve({ ok: false, outcome: 'error', error: 'worker: ' + e.message });
      worker.postMessage({ cmd: 'run', recipe, baseFile, packs: bytes, sink: sinkInfo.sink, handle: sinkInfo.handle,
                           port: sinkInfo.port, fileName }, sinkInfo.transfer);
    });
    worker.terminate();
    if (hiddenSince !== null) { hiddenMs += performance.now() - hiddenSince; hiddenSince = performance.now(); }
    track('build_end', Object.assign({}, base, netParams(), {
      outcome: result.outcome, secs: Math.round((performance.now() - t0) / 1000), mbps_total: result.mbps_total,
      local_mbps: result.net && result.net.local_mbps, hidden_s: Math.round(hiddenMs / 1000), hash_ok: result.ok ? 1 : 0 }));
    running = null;
    checkReady();
    if (result.ok) {
      $('status').textContent = `Done and verified. ${fmtMB(result.size)} in ${Math.round(result.ms / 1000)} s. MD5 (as md5sum prints it): ${result.file_md5}`;
      if (sinkInfo.sink === 'opfs') {
        const root = await navigator.storage.getDirectory();
        const f = await (await root.getFileHandle(fileName)).getFile();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(f); a.download = fileName; a.textContent = 'Save ' + fileName;
        $('status').append(' ', a);
      }
      $('after').hidden = false;
    } else {
      $('status').textContent = 'Stopped: ' + result.error;
    }
  };

  window.addEventListener('pagehide', () => {
    if (!running || !running.base) return;
    track('build_end', Object.assign({}, running.base, netParams(), { outcome: 'abandoned',
      secs: Math.round((performance.now() - running.t0) / 1000) }));
  });

  // ------------------------------------------------------------ speed test
  $('probe').onclick = async () => {
    const p = region && region.probe;
    if (!p) return;
    $('probeOut').textContent = 'Testing…';
    const out = [];
    for (let i = 0; i < 2; i++) {
      const start = Math.floor(Math.random() * Math.max(1, (p.size - PROBE_BYTES) / 65536)) * 65536;
      const t = performance.now();
      try {
        const r = await fetch(p.url, { headers: { Range: `bytes=${start}-${start + PROBE_BYTES - 1}` }, cache: 'no-store' });
        const ttfb = performance.now() - t;
        const n = (await r.arrayBuffer()).byteLength;
        out.push({ ttfb: Math.round(ttfb), mbps: +(n / 1e6 / ((performance.now() - t) / 1000)).toFixed(2), status: r.status });
      } catch (e) { out.push({ error: String(e.message || e) }); }
    }
    const ok = out.filter((o) => o.mbps);
    $('probeOut').textContent = ok.length ? `About ${ok.map((o) => o.mbps).join(' and ')} MB/s (first byte after ${ok.map((o) => o.ttfb).join(' / ')} ms).` : 'The test failed: ' + JSON.stringify(out);
    track('build_probe', { region: region.id, ttfb_ms_1: out[0] && out[0].ttfb, mbps_1: out[0] && out[0].mbps,
                           ttfb_ms_2: out[1] && out[1].ttfb, mbps_2: out[1] && out[1].mbps, ok: ok.length });
  };

  load().catch((e) => { $('status').textContent = 'Could not load the list of maps: ' + (e.message || e); });
})();
