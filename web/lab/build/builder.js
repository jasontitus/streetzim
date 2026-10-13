// The language builder page (/lab/build/): pick a region and a map language,
// open your copy of the region's map file, and the page adds Wikipedia in the
// chosen languages and saves a new ZIM. assembler-worker.js does the work.
//
// Analytics: GA4 (the site's Firebase project) gets build_start,
// build_progress (25/50/75%), build_end and build_probe, so we learn what
// download speeds visitors really get. Nothing is sent on localhost, from an
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
  }

  function checkReady() {
    $('go').disabled = !(baseOk && region && entry) || !!running;
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
    const t0 = performance.now();
    hiddenMs = 0; hiddenSince = document.visibilityState === 'hidden' ? performance.now() : null;
    const base = { region: region.id, lang: entry.lang, packs, size_mb: Math.round(recipe.size / 1e6),
                   download_mb: Math.round(entry.download_bytes / 1e6), sink: sinkInfo.sink };
    track('build_start', base);
    const worker = new Worker('assembler-worker.js');
    running = { worker, base, t0, sent: new Set(), net: null };
    checkReady();
    $('status').textContent = 'Starting…';
    const result = await new Promise((resolve) => {
      worker.onmessage = (e) => {
        if (e.data.type === 'progress') {
          const { written, size, ms, net } = e.data;
          running.net = net;
          $('prog').value = written / size;
          $('status').textContent = `${fmtMB(written)} of ${fmtMB(size)}` +
            (net.mbps_avg ? `, downloading at ${net.mbps_avg} MB/s` : '') + ' — keep this tab open';
          for (const p of [25, 50, 75]) {
            if (written / size * 100 >= p && !running.sent.has(p)) {
              running.sent.add(p);
              track('build_progress', Object.assign({}, base, { pct: p, elapsed_s: Math.round(ms / 1000), net_mbps_avg: net.mbps_avg }));
            }
          }
        } else if (e.data.type === 'done') resolve(e.data.result);
      };
      worker.onerror = (e) => resolve({ ok: false, outcome: 'error', error: 'worker: ' + e.message });
      worker.postMessage({ cmd: 'run', recipe, baseFile, sink: sinkInfo.sink, handle: sinkInfo.handle,
                           port: sinkInfo.port, fileName, inflight: +(q.get('inflight') || 4) }, sinkInfo.transfer);
    });
    worker.terminate();
    if (hiddenSince !== null) { hiddenMs += performance.now() - hiddenSince; hiddenSince = performance.now(); }
    const net = result.net || {};
    track('build_end', Object.assign({}, base, {
      outcome: result.outcome, secs: Math.round((performance.now() - t0) / 1000), mbps_total: result.mbps_total,
      net_mbps_avg: net.mbps_avg, net_mbps_p10: net.mbps_p10, ttfb_ms_p50: net.ttfb_p50,
      retries: net.retries, resets: net.resets, hidden_s: Math.round(hiddenMs / 1000),
      hash_ok: result.ok ? 1 : 0, requests: net.requests }));
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
    if (!running) return;
    const net = running.net || {};
    track('build_end', Object.assign({}, running.base, { outcome: 'abandoned',
      secs: Math.round((performance.now() - running.t0) / 1000), net_mbps_avg: net.mbps_avg,
      net_mbps_p10: net.mbps_p10, retries: net.retries, resets: net.resets }));
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
