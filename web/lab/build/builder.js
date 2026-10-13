// The language builder page (/lab/build/): pick a region and a map language,
// open your copy of the region's map file, and the page adds Wikipedia in the
// chosen languages and saves a new ZIM. assembler-worker.js does the work.
//
// Analytics: GA4 (the site's Firebase project) gets packs_start, packs_end,
// build_start, build_progress (25/50/75%), build_end and build_probe, so we
// learn what download speeds visitors really get: region, language, sizes,
// speeds and timings, never file names. GA is loaded only when the first
// report is sent (a build or a speed test). Nothing is sent when the visitor
// ticks "Don't send speed reports" (remembered: localStorage.sz_no_reports),
// on localhost, from an automated browser, with ?notrack, or with
// localStorage.sz_dev set.
(function () {
  'use strict';
  const q = new URLSearchParams(location.search);
  const $ = (id) => document.getElementById(id);
  const PROBE_BYTES = 8 * 1048576;

  // ------------------------------------------------------------ analytics
  const devSession = (() => {
    try { if (localStorage.getItem('sz_dev')) return true; } catch (e) { /* no storage */ }
    return navigator.webdriver || q.has('notrack') || /^(localhost|127\.|\[::1\])/.test(location.hostname);
  })();
  const optedOut = () => { try { return !!localStorage.getItem('sz_no_reports'); } catch (e) { return false; } };
  const env = /--/.test(location.hostname) ? 'preview' : 'live';
  let analytics = null;                // Promise<logEvent | null>, made on the first report
  const loadAnalytics = () => Promise.all([
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
      return (name, params) => { try { an.logEvent(a, name, Object.assign({ env }, params)); } catch (e) { /* ignore */ } };
    }).catch(() => null);
  const track = (name, params) => {
    if (devSession || optedOut()) return;
    if (!analytics) analytics = loadAnalytics();
    analytics.then((log) => { if (log && !optedOut()) log(name, params); });
  };
  $('trackNote').textContent = 'Speed reports: when you build or test your speed, this page sends Google Analytics the region, the language, file sizes, download speeds and timings, never file names or anything from your files. Google Analytics also records the page address and your browser type, and sets a cookie with a random ID.';
  $('noReports').checked = devSession || optedOut();
  $('noReports').disabled = devSession;
  if (devSession) $('noReportsLabel').append(' (off for this session)');
  $('noReports').onchange = () => {
    try { if ($('noReports').checked) localStorage.setItem('sz_no_reports', '1'); else localStorage.removeItem('sz_no_reports'); } catch (e) { /* no storage */ }
  };

  // ------------------------------------------------------------ state
  let index = null, region = null, entry = null, baseFile = null, running = null;
  let hiddenSince = null, hiddenMs = 0;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') hiddenSince = performance.now();
    else if (hiddenSince !== null) { hiddenMs += performance.now() - hiddenSince; hiddenSince = null; }
  });

  // #status changes several times a second; screen readers get #milestone,
  // a live region that changes only at milestones.
  const announce = (msg) => { $('milestone').textContent = msg; };
  let statusAt = 0;
  const setStatus = (text, force) => {
    const now = performance.now();
    if (!force && now - statusAt < 1000) return;
    statusAt = now; $('status').textContent = text;
  };

  // While packs download or a build runs: keep the screen on where the
  // browser allows it, and warn before the tab is closed.
  let wakeLock = null;
  const busy = () => !!running || !!packAbort;
  async function holdScreen() {
    if (!('wakeLock' in navigator) || document.visibilityState !== 'visible' || (wakeLock && !wakeLock.released)) return;
    try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) { /* refused, e.g. battery saver */ }
  }
  function releaseScreen() { if (wakeLock && !busy()) { wakeLock.release().catch(() => {}); wakeLock = null; } }
  document.addEventListener('visibilitychange', () => { if (busy() && document.visibilityState === 'visible') holdScreen(); });
  window.addEventListener('beforeunload', (e) => { if (busy()) { e.preventDefault(); e.returnValue = ''; } });
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
    const nothingNew = entry.packs.every((p) => p.in_base);
    $('total').textContent = nothingNew
      ? `Nothing to download: the new file is the same map with its map language set to ${langName(entry.lang, entry.label)} (${fmtMB(entry.size)}).`
      : `The new file: the map in ${langName(entry.lang, entry.label)}, ${fmtMB(entry.size)}. To download now: ${fmtMB(entry.download_bytes)}.`;
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
  const SLOW_AFTER_MS = 20000, SLOW_BPS = 50e3, SLOW_TRIES = 3;
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
  const newNet = () => ({ requests: 0, bytes: 0, ms: 0, ttfb: [], mbps: [], retries: 0, slow_retries: 0, errors: 0, resets: 0, hash_retries: 0 });
  let net = newNet();
  let packsOk = false, packSeq = 0, packAbort = null;
  const packBytes = new Map();         // url|size|hashes -> verified Uint8Array
  const packKey = (p) => [p.url, p.size, p.sha256.join(',')].join('|');

  class FetchError extends Error {
    constructor(msg, permanent) { super(msg); this.permanent = !!permanent; }
  }
  // One range request, its bytes handed to onChunk as they arrive. Aborted
  // when no byte has arrived for STALL_MS (while waiting for the answer,
  // too); with crawlCheck, also when it has run SLOW_AFTER_MS at under
  // SLOW_BPS (the proxy's upstream sometimes trickles one request while the
  // next one is fast).
  async function fetchRange(url, a, b, size, signal, onChunk, crawlCheck) {
    const ac = new AbortController();
    const outer = () => ac.abort();
    if (signal.aborted) throw new DOMException('aborted', 'AbortError');
    signal.addEventListener('abort', outer, { once: true });
    let timer = null, stalled = false, n = 0;
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
      const reader = r.body.getReader();
      for (;;) {
        arm();
        const { done, value } = await reader.read();
        if (done) break;
        if (n + value.length > b - a) throw new FetchError('the server sent too many bytes');
        onChunk(value); n += value.length;
        const ms = performance.now() - t0;
        if (crawlCheck && n < b - a && ms > SLOW_AFTER_MS && n / (ms / 1000) < SLOW_BPS) {
          reader.cancel().catch(() => {});
          const e = new FetchError(`only ${Math.round(n / (ms / 1000) / 1e3)} KB/s`); e.slow = true; throw e;
        }
      }
      if (n !== b - a) throw new FetchError(`the server sent ${n} of ${b - a} bytes`);
      const ms = performance.now() - t0;
      net.requests++; net.ttfb.push(Math.round(ttfb));
      net.mbps.push(+(n / 1e6 / (ms / 1000)).toFixed(2));
    } catch (e) {
      if (signal.aborted) throw new DOMException('aborted', 'AbortError');
      if (stalled) throw new FetchError(`no data for ${STALL_MS / 1000} s`);
      if (e instanceof FetchError) throw e;
      net.resets++;
      throw new FetchError('the connection failed (' + (e.message || e) + ')');
    } finally {
      net.bytes += n; net.ms += performance.now() - t0;
      clearTimeout(timer);
      signal.removeEventListener('abort', outer);
    }
  }
  // Fills out[a, b) from the same bytes of url. After a failure it resumes
  // from the first missing byte: at once for a crawling request (up to
  // SLOW_TRIES times), else after a backoff of 1, 2, 4, 8 s (TRIES requests
  // in all). onRetry(note) explains each retry.
  async function fetchRetrying(url, out, a, b, size, signal, onBytes, onRetry) {
    let pos = a, failures = 0, slow = 0;
    for (;;) {
      try {
        await fetchRange(url, pos, b, size, signal, (chunk) => { out.set(chunk, pos); pos += chunk.length; onBytes(chunk.length); }, slow < SLOW_TRIES);
        return;
      } catch (e) {
        if (e.name === 'AbortError' || e.permanent) throw e;
        net.errors++;
        if (e.slow) { slow++; net.slow_retries++; onRetry(`${e.message}; asking again`); continue; }
        if (++failures >= TRIES) throw new FetchError(`${e.message}, ${TRIES} tries`);
        net.retries++;
        onRetry(`${e.message}; trying again (${failures + 1} of ${TRIES})`);
        await sleep(1000 * 2 ** (failures - 1));
      }
    }
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
            await fetchRetrying(p.url, out, a, b, p.size, signal, bump, (note) => onProgress(got, note));
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
    const t0 = performance.now();
    net = newNet();
    const todo = ids.filter((id) => !packBytes.has(packKey(rec.packs[id])));
    const evBase = { region: region.id, lang: entry.lang, packs: ids.join(','), download_mb: Math.round(todo.reduce((t, id) => t + rec.packs[id].size, 0) / 1e6) };
    const ac = packAbort = new AbortController();
    if (todo.length) {
      track('packs_start', evBase);
      holdScreen();
      announce(`Downloading ${todo.map(packName).join(', ')}.`);
    }
    try {
      for (const id of todo) {
        const p = rec.packs[id], name = packName(id);
        let shown = 0;
        const bytes = await downloadPack(p, ac.signal, (got, note) => {
          const now = performance.now();
          if (!note && now - shown < 1000 && got < p.size) return;
          shown = now;
          out.textContent = `Downloading ${name}: ${(got / 1e6).toFixed(0)} of ${fmtMB(p.size)}` + (note ? ` — ${note}` : '');
        }).catch((e) => { e.pack = name; throw e; });
        packBytes.set(packKey(p), bytes);
      }
    } catch (e) {
      if (seq !== packSeq || e.name === 'AbortError') return;
      packAbort = null; releaseScreen();
      out.className = 'bad';
      out.textContent = /HTTP 40[34]|HTTP 410/.test(e.message)
        ? `${e.pack} isn't available right now (${e.message}). Nothing was saved. Try again later.`
        : `Could not download ${e.pack || 'the language pack'}: ${e.message}. Nothing was saved. Try again.`;
      announce(out.textContent);
      $('packRetry').hidden = false;
      track('packs_end', Object.assign({}, evBase, netParams(), { outcome: /damaged/.test(e.message) ? 'hash_mismatch' : 'error', secs: Math.round((performance.now() - t0) / 1000) }));
      return;
    }
    if (seq !== packSeq) return;
    packAbort = null; releaseScreen();
    if (todo.length) track('packs_end', Object.assign({}, evBase, netParams(), { outcome: 'ok', secs: Math.round((performance.now() - t0) / 1000) }));
    packsOk = true;
    out.className = 'ok';
    out.textContent = ids.length ? `✓ ${ids.map(packName).join(', ')} downloaded and checked (${fmtMB(ids.reduce((t, id) => t + rec.packs[id].size, 0))})` : '';
    if (todo.length) announce(out.textContent.replace(/^✓ /, '') + '. Ready to build.');
    checkReady();
  }
  $('packRetry').onclick = () => ensurePacks();

  function pct(a, p) { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; }
  function netParams() {
    return { net_mb: +(net.bytes / 1e6).toFixed(1), net_mbps_avg: net.ms ? +(net.bytes / 1e6 / (net.ms / 1000)).toFixed(2) : null,
             net_mbps_p10: pct(net.mbps, 0.1), ttfb_ms_p50: pct(net.ttfb, 0.5), retries: net.retries, resets: net.resets,
             slow_retries: net.slow_retries, hash_retries: net.hash_retries, requests: net.requests };
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
  // A streamed download that stopped once is not tried again in this tab.
  const swFailedBefore = () => { try { return !!sessionStorage.getItem('sz_sw_failed'); } catch (e) { return false; } };
  const swFailed = () => { try { sessionStorage.setItem('sz_sw_failed', '1'); } catch (e) { /* no storage */ } };
  const within = (p, ms) => Promise.race([p, new Promise((r, j) => setTimeout(() => j(new Error('timed out')), ms))]);

  const SW_VERSION = 2;                // the sw.js protocol this page speaks
  const controllerChange = (ms) => new Promise((r) => { navigator.serviceWorker.addEventListener('controllerchange', r, { once: true }); setTimeout(r, ms); });
  // {v, busy} from a service worker; null if it does not answer (an old one).
  function swStatus(w) {
    return new Promise((r) => {
      if (!w) { r(null); return; }
      const ch = new MessageChannel();
      const t = setTimeout(() => r(null), 1000);
      ch.port1.onmessage = (e) => { clearTimeout(t); r(e.data); };
      w.postMessage({ type: 'status' }, [ch.port2]);
    });
  }
  // The service worker that streams downloads, or null when it cannot be had
  // (no support, a failed registration, private modes): then OPFS is used.
  // A newer sw.js waits until the running one has no download in progress.
  async function swController() {
    if (!('serviceWorker' in navigator)) return null;
    try {
      const reg = await within(navigator.serviceWorker.register('sw.js', { scope: './' }), 10000);
      await within(navigator.serviceWorker.ready, 10000);
      const inst = reg.installing;
      if (inst) await new Promise((r) => { inst.addEventListener('statechange', () => { if (inst.state !== 'installing') r(); }); setTimeout(r, 5000); });
      if (reg.waiting) {
        const st = await swStatus(reg.active);
        if (!st || !st.busy) { const changed = controllerChange(3000); reg.waiting.postMessage({ type: 'skipWaiting' }); await changed; }
      }
      if (!navigator.serviceWorker.controller) await controllerChange(3000);
      return navigator.serviceWorker.controller;
    } catch (e) {
      console.warn('service worker unavailable, saving through browser storage instead:', e);
      return null;
    }
  }

  // The best place to write to, in the design doc's order: File System
  // Access (Chrome, Edge on a desktop); a service-worker streamed download
  // (Firefox, Chrome on Android); browser storage (OPFS) and then a save
  // step (Safari and every iOS browser, which are WebKit, and wherever the
  // stream cannot be had).
  const isWebKit = /Apple/.test(navigator.vendor || '');
  async function prepareSink(fileName, size) {
    if (window.showSaveFilePicker) {
      const handle = await window.showSaveFilePicker({ suggestedName: fileName, types: [{ description: 'ZIM file', accept: { 'application/octet-stream': ['.zim'] } }] });
      return { sink: 'fsa', handle, transfer: [] };
    }
    const ctl = isWebKit || swFailedBefore() ? null : await swController();
    if (ctl) {
      const ch = new MessageChannel();
      const id = Math.random().toString(36).slice(2);
      const ok = new Promise((r) => { ch.port1.onmessage = (e) => { if (e.data.type === 'registered') r(e.data.v); }; });
      ctl.postMessage({ type: 'register', id, filename: fileName, size }, [ch.port2]);
      const v = await within(ok, 5000).catch(() => null);
      ch.port1.onmessage = null;
      if (v === SW_VERSION) {
        const ifr = document.createElement('iframe');
        ifr.hidden = true; ifr.title = 'download'; ifr.src = '__dl/' + encodeURIComponent(id);
        document.body.append(ifr);
        // Messages to the service worker keep the browser from stopping it
        // while the download runs (Firefox stops idle ones after 30 s).
        const keepalive = setInterval(() => { const c = navigator.serviceWorker.controller; if (c) c.postMessage({ type: 'keepalive' }); }, 10000);
        return { sink: 'sw', port: ch.port1, transfer: [ch.port1],
                 cleanup: (ok) => { clearInterval(keepalive); setTimeout(() => ifr.remove(), ok ? 5000 : 0); } };
      }
      ch.port1.close();
    }
    if (!navigator.storage || !navigator.storage.getDirectory) throw new Error('this browser cannot save a file this large. Chrome or Edge on a desktop works best');
    let room = null;
    try { const est = await navigator.storage.estimate(); if (est.quota) room = est.quota - (est.usage || 0); } catch (e) { /* unknown */ }
    if (room !== null && room < size) throw new Error(`your browser lets this site store ${fmtMB(room)}, and the file needs ${fmtMB(size)}. Free up disk space, or use Chrome or Edge on a desktop`);
    return { sink: 'opfs', transfer: [] };
  }

  // ------------------------------------------------------------ browser storage (OPFS)
  // A file built into browser storage (folder lab-build/) is offered with a
  // Save link; the visitor then deletes the browser's copy, or it is deleted
  // on the next visit once it has been saved. States per file name, in
  // localStorage: building | ready | saved.
  const OPFS_DIR = 'lab-build';
  const opfsDir = async (create) => (await navigator.storage.getDirectory()).getDirectoryHandle(OPFS_DIR, { create: !!create });
  const opfsStates = () => { try { return JSON.parse(localStorage.getItem('sz_lab_opfs') || '{}'); } catch (e) { return {}; } };
  const opfsState = (name, state) => {
    const st = opfsStates();
    if (state) st[name] = state; else delete st[name];
    try { localStorage.setItem('sz_lab_opfs', JSON.stringify(st)); } catch (e) { /* no storage */ }
  };
  async function offerSave(name, earlier) {
    const dir = await opfsDir();
    const f = await (await dir.getFileHandle(name)).getFile();
    const box = $('save');
    box.innerHTML = ''; box.hidden = false;
    const head = document.createElement('p');
    head.textContent = earlier ? `A file built earlier is still in this browser's storage: ${name} (${fmtMB(f.size)}).`
      : 'One more step: save the file to your device.';
    const a = document.createElement('a');
    a.id = 'saveLink'; a.className = 'button'; a.href = URL.createObjectURL(f); a.download = name;
    a.textContent = `Save ${name} (${fmtMB(f.size)})`;
    const del = document.createElement('button');
    del.id = 'dropCopy'; del.className = 'secondary'; del.textContent = "Delete the browser's copy";
    const note = document.createElement('p');
    note.className = 'dim'; note.hidden = true;
    note.textContent = `When your browser shows the download as finished, delete the browser's copy to free ${fmtMB(f.size)}.`;
    a.onclick = () => { opfsState(name, 'saved'); note.hidden = false; $('after').hidden = false; announce('Saving the file. When the download has finished, delete the browser\'s copy.'); };
    del.onclick = async () => {
      del.disabled = true;
      try { await dir.removeEntry(name); } catch (e) { del.disabled = false; note.hidden = false; note.textContent = 'Could not delete it yet (' + (e.message || e) + '). Try again in a moment.'; return; }
      URL.revokeObjectURL(a.href);
      opfsState(name, null);
      box.textContent = "The browser's copy is deleted.";
    };
    const row = document.createElement('p');
    row.append(a, ' ', del);
    box.append(head, row, note);
  }
  // On load: offer files that were built but not saved; delete the rest
  // (saved ones, and leftovers of builds that did not finish). A file that
  // another tab is still writing is locked, and stays.
  async function tidyBrowserStorage() {
    if (!navigator.storage || !navigator.storage.getDirectory) return;
    let dir;
    try { dir = await opfsDir(false); } catch (e) { return; }    // nothing built here yet
    const st = opfsStates();
    try {
      for await (const [name, h] of dir.entries()) {
        if (h.kind !== 'file') continue;
        if (st[name] === 'ready') { if (!running) await offerSave(name, true); continue; }
        try { await dir.removeEntry(name); opfsState(name, null); } catch (e) { /* in use */ }
      }
    } catch (e) { /* no directory iteration in this browser */ }
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
      setStatus(!recipe ? `Not started: could not load the recipe for ${entry.label} (${e.message || e}). Try again later.`
        : e.name === 'AbortError' ? 'Not started: no place to save the file was chosen.'
        : 'Not started: ' + (e.message || e), true);
      announce($('status').textContent);
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
    if (sinkInfo.sink === 'opfs') opfsState(fileName, 'building');
    const worker = new Worker('assembler-worker.js');
    running = { worker, base, t0, sent: new Set() };
    checkReady();
    holdScreen();
    $('save').hidden = true; $('after').hidden = true;
    setStatus('Starting…', true);
    announce('Building the file. Keep this tab open.');
    const result = await new Promise((resolve) => {
      worker.onmessage = (e) => {
        if (e.data.type === 'progress') {
          const { written, size, ms, closing } = e.data;
          $('prog').value = written / size;
          setStatus(closing && sinkInfo.sink === 'sw' ? `All ${fmtMB(size)} built and checked; your browser is saving the rest — keep this tab open`
            : `${fmtMB(written)} of ${fmtMB(size)} — keep this tab open`, closing);
          for (const p of [25, 50, 75]) {
            if (written / size * 100 >= p && !running.sent.has(p)) {
              running.sent.add(p);
              announce(`Building: ${p}% done.`);
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
    if (sinkInfo.cleanup) sinkInfo.cleanup(result.ok);
    if (result.outcome === 'download_stopped') swFailed();
    if (hiddenSince !== null) { hiddenMs += performance.now() - hiddenSince; hiddenSince = performance.now(); }
    track('build_end', Object.assign({}, base, netParams(), {
      outcome: result.outcome, secs: Math.round((performance.now() - t0) / 1000), mbps_total: result.mbps_total,
      local_mbps: result.net && result.net.local_mbps, hidden_s: Math.round(hiddenMs / 1000), hash_ok: result.ok ? 1 : 0 }));
    running = null;
    releaseScreen();
    checkReady();
    if (result.ok) {
      const tail = ` ${fmtMB(result.size)} in ${Math.round(result.ms / 1000)} s. MD5 (as md5sum prints it): ${result.file_md5}`;
      if (sinkInfo.sink === 'opfs') {
        opfsState(fileName, 'ready');
        setStatus('Done and verified; one more step: save it.' + tail, true);
        announce('Done and verified. One more step: save the file.');
        await offerSave(fileName, false);
      } else {                         // saved (or, streamed, being saved by the browser)
        setStatus((sinkInfo.sink === 'sw' ? 'Done and verified. Your browser has the whole file and is finishing the download; look in your downloads.'
          : 'Done and verified. The file is saved.') + tail, true);
        announce($('status').textContent.replace(/ MD5.*$/, ''));
        $('after').hidden = false;
      }
    } else {
      if (sinkInfo.sink === 'opfs') opfsState(fileName, null);
      // The worker discarded what it wrote; File System Access leaves the
      // empty file the save dialog created, which goes too.
      if (sinkInfo.sink === 'fsa') {
        try { const f = await sinkInfo.handle.getFile(); if (f.size === 0 && sinkInfo.handle.remove) await sinkInfo.handle.remove(); } catch (e) { /* keep it */ }
      }
      setStatus(result.outcome === 'download_stopped'
        ? 'Stopped. The download stopped — try again (Chrome or Edge on a desktop works best).'
        : 'Stopped: ' + result.error, true);
      announce($('status').textContent);
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
        const r = await fetch(p.url, { headers: { Range: `bytes=${start}-${start + PROBE_BYTES - 1}` }, cache: 'no-store', signal: AbortSignal.timeout ? AbortSignal.timeout(60000) : undefined });
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

  load().catch((e) => { $('notice').textContent = 'Could not load the list of maps (' + (e.message || e) + '). Try again later.'; });
  tidyBrowserStorage();
})();
