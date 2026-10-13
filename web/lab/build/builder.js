// The language builder page (/lab/build/): pick a region and a map language,
// open your copy of the region's map file, and the page adds Wikipedia in the
// chosen languages and saves a new ZIM. assembler-worker.js does the work.
//
// Analytics: GA4 (the site's Firebase project) gets packs_start, packs_end,
// build_start, build_progress (25/50/75%), build_end and build_probe, so we
// learn what download speeds visitors really get: region, language, sizes,
// speeds and timings, never file names. Nothing is loaded or sent before the
// visitor's first click on the page; GA is loaded with the first report. Nothing is sent when the visitor
// ticks "Don't send speed reports" (remembered: localStorage.sz_no_reports),
// on localhost, from an automated browser, with ?notrack, or with
// localStorage.sz_dev set.
(function () {
  'use strict';
  const q = new URLSearchParams(location.search);
  const $ = (id) => document.getElementById(id);
  // Never inside another page (firebase.json also sends frame-ancestors
  // 'self' and X-Frame-Options): a framed copy could start downloads the
  // visitor cannot see. Nothing below runs.
  let framed = true;
  try { framed = window.top !== window.self; } catch (e) { /* cross-origin parent */ }
  if (framed) {
    const p = document.createElement('p'), a = document.createElement('a');
    a.href = location.href; a.target = '_blank'; a.rel = 'noopener'; a.textContent = 'Open it on its own';
    p.append('This page does not run inside another page. ', a, '.');
    document.body.replaceChildren(p);
    return;
  }

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
  let clicked = false;                 // nothing is reported before the first click
  document.addEventListener('click', () => { clicked = true; }, { capture: true, once: true });
  const track = (name, params) => {
    if (devSession || optedOut() || !clicked) return;
    if (!analytics) analytics = loadAnalytics();
    analytics.then((log) => { if (log && !optedOut()) log(name, params); });
  };
  $('trackNote').textContent = 'Speed reports: once you press Download, choose a map file or test your speed, this page sends Google Analytics the region, the language, file sizes, download speeds and timings, never file names or anything from your files. Google Analytics also records the page address and your browser type, and sets a cookie with a random ID. Nothing is sent before you click.';
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
    // ?go=1 (a download link): the language is chosen and the Download
    // button focused; one click starts it. Nothing starts without a click,
    // so another site cannot start a download by linking or redirecting here.
    if (q.get('go') === '1' && region && entry && region.menu.some((m) => m.lang === q.get('lang'))) {
      const u = new URL(location.href); u.searchParams.delete('go');
      history.replaceState(null, '', u);
      $('goHint').hidden = false;
      $('oneclick').focus();
      $('oneclick').scrollIntoView({ block: 'center' });
    }
    // The connection check waits for a sign of a person on the page (a
    // pointer, a key, a touch, a wheel; not 'scroll', which the page's own
    // scrollIntoView fires): a page opened by a redirect from another site
    // fetches nothing by itself.
    const human = ['pointermove', 'pointerdown', 'keydown', 'touchstart', 'wheel'];
    const onHuman = () => { human.forEach((t) => removeEventListener(t, onHuman, true)); measureSpeed(false); };
    human.forEach((t) => addEventListener(t, onHuman, { capture: true, passive: true }));
  }

  function pickRegion() {
    region = index.regions.find((r) => r.id === $('region').value);
    if (!region) { $('notice').textContent = 'No such region. Pick one from the list.'; return; }
    const b = region.base;
    $('baseInfo').innerHTML = '';
    const a = document.createElement('a');
    a.href = b.download; a.textContent = b.file; a.rel = 'noopener';
    $('baseInfo').append('Open your copy of ', a, ` (${fmtMB(b.size)}): only Wikipedia is downloaded, and the page checks that it is the right file.`);
    // The language list as a download page: one link per language. A link
    // opens ?region=&lang=&go=1, which starts at once; clicked here it
    // starts in place.
    $('dlHead').textContent = `${region.title}: downloads`;
    const dl = $('downloads');
    dl.innerHTML = '';
    for (const m of region.menu) {
      const li = document.createElement('li');
      const a2 = document.createElement('a');
      a2.href = `?region=${encodeURIComponent(region.id)}&lang=${encodeURIComponent(m.lang)}&go=1`;
      a2.textContent = `${langName(m.lang, m.label)} ${region.title}`;
      a2.lang = 'en';
      a2.onclick = (ev) => {
        if (running) { ev.preventDefault(); return; }    // one build at a time
        if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button) return;
        ev.preventDefault();
        $('lang').value = m.lang; pickLang();
        build('net');
      };
      const meta = document.createElement('span');
      meta.className = 'dim';
      const wiki = m.packs.map((p) => langName(p.lang, p.label.replace(/ Wikipedia$/, ''))).join(' + ');
      meta.textContent = `${m.label} · ${fmtMB(m.size)} · ${wiki} Wikipedia`;
      li.append(a2, meta);
      dl.append(li);
    }
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
    try {                            // the address names what is shown (a reload or a shared link shows it again)
      const u = new URL(location.href);
      u.searchParams.set('region', region.id); u.searchParams.set('lang', entry.lang);
      if (u.href !== location.href) history.replaceState(null, '', u);
    } catch (e) { /* history unavailable */ }
    if (/^Could not load the recipe/.test($('notice').textContent)) $('notice').textContent = '';
    const ul = $('packs');
    ul.innerHTML = '';
    for (const p of entry.packs) {
      const li = document.createElement('li');
      li.textContent = `${langName(p.lang, p.label.replace(/ Wikipedia$/, ''))} Wikipedia: ${p.articles.toLocaleString()} articles`;
      ul.append(li);
    }
    const nothingNew = entry.packs.every((p) => p.in_base);
    const name = langName(entry.lang, entry.label);
    $('total').textContent = nothingNew
      ? `The same ${region.title} map with its map language set to ${name}.`
      : `The ${region.title} map in ${name}, with Wikipedia in:`;
    $('oneclick').textContent = `Download ${name} ${region.title} (${fmtMB(entry.size)})`;
    showEstimate();
    $('haveBaseSummary').textContent = nothingNew
      ? `Already have the ${region.title} map? Use it, with nothing else to download`
      : `Already have the ${region.title} map? Use it and download only ${fmtMB(entry.download_bytes)}`;
    $('go').textContent = `Build ${name} ${region.title} from my copy`;
    recipeFor(entry).catch((e) => { $('notice').textContent = `Could not load the recipe for ${langName(entry.lang, entry.label)} (${e.message || e}). Try again later.`; });
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
      try { b = (await recipeFor(entry)).base; } catch (e) { verdict = `Could not load the recipe for ${langName(entry.lang, entry.label)} (${e.message || e}). Try again later.`; }
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
  // Range requests come from fetcher.js (shared with the worker): 30 s stall
  // timeout, resume from the first missing byte, laggards asked again,
  // backoff retries.
  const { FetchError, newNet, pct, fetchRetrying } = self.SZFetch;
  const PART = 4 * 1048576;            // range request size for packs
  const PARALLEL = 4;                  // pack range requests in flight
  const langName = (() => {
    let dn = null;
    try { dn = new Intl.DisplayNames(['en'], { type: 'language' }); } catch (e) { /* old browser */ }
    return (code, fallback) => { try { const n = dn && dn.of(code); if (n && n !== code) return n; } catch (e) { /* unknown code */ } return fallback || code; };
  })();
  const packName = (id) => {
    const m = entry && entry.packs.find((p) => p.lang === id);
    return langName(id, m ? m.label.replace(/ Wikipedia$/, '') : id) + ' Wikipedia';
  };
  let net = newNet();
  let packsOk = false, packSeq = 0, packAbort = null;
  const packBytes = new Map();         // url|size|hashes -> verified Uint8Array
  const packKey = (p) => [p.url, p.size, p.sha256.join(',')].join('|');

  // One pack, chunk by chunk; each chunk's SHA-256 must match the recipe. A
  // chunk that does not match is downloaded once more before giving up. A
  // range that fails for good stops the other ranges in flight.
  async function downloadPack(p, outer, onProgress) {
    const ac = new AbortController();
    const stop = () => ac.abort();
    outer.addEventListener('abort', stop, { once: true });
    try { return await downloadPackRanges(p, ac, onProgress); }
    finally { ac.abort(); outer.removeEventListener('abort', stop); }
  }
  async function downloadPackRanges(p, ac, onProgress) {
    const signal = ac.signal;
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
            await fetchRetrying(p.url, out, 0, a, b, p.size, signal, net, bump, (note) => onProgress(got, note));
          }
        };
        await Promise.all(Array.from({ length: Math.min(PARALLEL, parts.length) }, () => lane().catch((e) => { ac.abort(); throw e; })));
        const sum = hex(await crypto.subtle.digest('SHA-256', out.subarray(a0, b0)));
        if (sum === p.sha256[k]) break;
        if (pass >= 1) throw new FetchError('it arrived damaged twice (it does not match its checksum)', true);
        net.hash_retries++;
        got = before; onProgress(got, 'a piece arrived damaged; downloading it again');
      }
    }
    return out;
  }
  // Resolves true once the language's packs are in memory (false if they
  // could not be had, or a newer call took over). Without force it waits
  // for a right base file (the "build from my copy" path).
  async function ensurePacks(force) {
    if (!force && running) return false;   // a build's own download is running; leave it
    const seq = ++packSeq;
    if (packAbort) packAbort.abort();
    packAbort = null; packsOk = false;
    const out = $('packStatus');
    out.className = ''; out.textContent = '';
    $('packRetry').hidden = true;
    checkReady();
    if ((!baseOk && !force) || !entry) return false;
    let rec;
    try { rec = await recipeFor(entry); } catch (e) { return false; }   // #notice says why
    if (seq !== packSeq) return false;
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
        if (seq !== packSeq) return false;     // the language changed meanwhile
        packBytes.set(packKey(p), bytes);
      }
    } catch (e) {
      if (seq !== packSeq || e.name === 'AbortError') return false;
      packAbort = null; releaseScreen();
      out.className = 'bad';
      out.textContent = /HTTP 40[34]|HTTP 410/.test(e.message)
        ? `${e.pack} isn't available right now (${e.message}). Nothing was saved. Try again later.`
        : `Could not download ${e.pack || 'the language pack'}: ${e.message}. Nothing was saved. Try again.`;
      announce(out.textContent);
      $('packRetry').hidden = false;
      track('packs_end', Object.assign({}, evBase, netParams(), { outcome: /damaged/.test(e.message) ? 'hash_mismatch' : 'error', secs: Math.round((performance.now() - t0) / 1000) }));
      return false;
    }
    if (seq !== packSeq) return false;
    packAbort = null; releaseScreen();
    if (todo.length) track('packs_end', Object.assign({}, evBase, netParams(), { outcome: 'ok', secs: Math.round((performance.now() - t0) / 1000) }));
    packsOk = true;
    out.className = 'ok';
    out.textContent = ids.length ? `✓ ${ids.map(packName).join(', ')} downloaded and checked (${fmtMB(ids.reduce((t, id) => t + rec.packs[id].size, 0))})` : '';
    if (todo.length) announce(out.textContent.replace(/^✓ /, '') + '.');
    checkReady();
    return true;
  }
  $('packRetry').onclick = () => { if (running) return; if (baseOk) ensurePacks(); else build('net'); };

  function netParams() {
    return { net_mb: +(net.bytes / 1e6).toFixed(1), net_mbps_avg: net.ms ? +(net.bytes / 1e6 / (net.ms / 1000)).toFixed(2) : null,
             net_mbps_p10: pct(net.mbps, 0.1), ttfb_ms_p50: pct(net.ttfb, 0.5), retries: net.retries, resets: net.resets,
             slow_retries: net.slow_retries, hash_retries: net.hash_retries, requests: net.requests };
  }

  function checkReady() {
    $('go').disabled = !(baseOk && packsOk && region && entry) || !!running;
    $('oneclick').disabled = !(region && entry) || !!running;
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

  const SW_VERSION = 4;                // the sw.js protocol this page speaks
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
  // A device with little memory (navigator.deviceMemory <= 4 GB; phones and
  // tablets when the browser does not say; ?lowmem) downloads 2 chunks of
  // the map at a time instead of 4-6, and the service worker holds 16 MiB
  // of the download instead of 64 MiB.
  const lowMem = q.has('lowmem') || (navigator.deviceMemory ? navigator.deviceMemory <= 4
    : /iPhone|iPad|iPod|Android|Mobile/.test(navigator.userAgent) || (isWebKit && navigator.maxTouchPoints > 1));
  const MEM = lowMem ? { window: [2, 2], hwm: 16 * 1048576 } : { window: [4, 6], hwm: 64 * 1048576 };
  // A streamed download through the service worker. Resolves with the sink
  // once the browser has asked for the download ('started'), or null if it
  // did not within 10 s (a browser that blocks a download the visitor did
  // not click for; the page then offers a "Start download" button) or the
  // service worker cannot be had.
  async function streamSink(ctl, fileName, size) {
    const ch = new MessageChannel();
    const id = Math.random().toString(36).slice(2);
    let started;
    const isStarted = new Promise((r) => { started = r; });
    const ok = new Promise((r) => {
      ch.port1.onmessage = (e) => { if (e.data.type === 'registered') r(e.data.v); else if (e.data.type === 'started') started(true); };
    });
    ctl.postMessage({ type: 'register', id, filename: fileName, size, hwm: MEM.hwm }, [ch.port2]);
    const v = await within(ok, 5000).catch(() => null);
    if (v !== SW_VERSION) { ch.port1.onmessage = null; ch.port1.close(); return null; }
    const ifr = document.createElement('iframe');
    ifr.hidden = true; ifr.title = 'download'; ifr.src = '__dl/' + encodeURIComponent(id);
    document.body.append(ifr);
    if (!await within(isStarted, 10000).catch(() => false)) {
      ch.port1.postMessage({ type: 'error', reason: 'not started' });    // the service worker forgets it
      ch.port1.onmessage = null; ch.port1.close(); ifr.remove();
      return { blocked: true };
    }
    ch.port1.onmessage = null;
    // Messages to the service worker keep the browser from stopping it
    // while the download runs (Firefox stops idle ones after 30 s).
    const keepalive = setInterval(() => { const c = navigator.serviceWorker.controller; if (c) c.postMessage({ type: 'keepalive' }); }, 10000);
    return { sink: 'sw', port: ch.port1, transfer: [ch.port1],
             cleanup: (ok) => { clearInterval(keepalive); setTimeout(() => ifr.remove(), ok ? 5000 : 0); } };
  }
  async function opfsSink(size) {
    if (!navigator.storage || !navigator.storage.getDirectory) throw new Error('this browser cannot save a file this large. Chrome or Edge on a desktop works best');
    let room = null;
    try { const est = await navigator.storage.estimate(); if (est.quota) room = est.quota - (est.usage || 0); } catch (e) { /* unknown */ }
    if (room !== null && room < size) throw new Error(`your browser lets this site store ${fmtMB(room)}, and the file needs ${fmtMB(size)}. Free up disk space, or use Chrome or Edge on a desktop`);
    return { sink: 'opfs', transfer: [] };
  }
  // Where to write. picker: File System Access when the browser has it (the
  // "build from my copy" path, from a click); the one-click path does not
  // ask where to save. A blocked streamed download is offered again behind
  // a "Start download" button (a click is a user gesture); if it is blocked
  // again, browser storage is used.
  async function prepareSink(fileName, size, picker) {
    if (picker && window.showSaveFilePicker) {
      const handle = await window.showSaveFilePicker({ suggestedName: fileName, types: [{ description: 'ZIM file', accept: { 'application/octet-stream': ['.zim'] } }] });
      return { sink: 'fsa', handle, transfer: [] };
    }
    const ctl = isWebKit || swFailedBefore() ? null : await swController();
    if (ctl) {
      let got = await streamSink(ctl, fileName, size);
      if (got && got.blocked) {
        setStatus('Your browser did not start the download by itself.', true);
        announce('Your browser did not start the download by itself. Press Start download.');
        got = await new Promise((resolve) => {
          const b = $('startDl');
          b.hidden = false; b.disabled = false; b.focus();
          b.onclick = async () => { b.disabled = true; resolve(await streamSink(ctl, fileName, size)); };
        });
        $('startDl').hidden = true;
      }
      if (got && !got.blocked) return got;
    }
    return opfsSink(size);
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
  // On load: offer finished files again (Save / Delete), whether or not Save
  // was clicked — a cancelled download looks the same as a finished one, so
  // only the Delete button removes a finished file. Leftovers of builds that
  // did not finish are deleted. A file another tab is writing is locked.
  async function tidyBrowserStorage() {
    if (!navigator.storage || !navigator.storage.getDirectory) return;
    let dir;
    try { dir = await opfsDir(false); } catch (e) { return; }    // nothing built here yet
    const st = opfsStates();
    try {
      for await (const [name, h] of dir.entries()) {
        if (h.kind !== 'file') continue;
        if (st[name] === 'ready' || st[name] === 'saved') { if (!running) await offerSave(name, true); continue; }
        try { await dir.removeEntry(name); opfsState(name, null); } catch (e) { /* in use */ }
      }
    } catch (e) { /* no directory iteration in this browser */ }
  }

  // ------------------------------------------------------------ build
  // ------------------------------------------------------------ build
  // Two ways in. One click (#oneclick, a download link, ?go=1): packs, then
  // the base map downloaded through the range proxy as it is needed, and the
  // file goes to the browser's downloads (no save dialog). "Build from my
  // copy" (#go): the visitor's own base file and, where the browser has
  // one, a save dialog. Both claim the running slot before their first
  // await, so a double click cannot start a second build.
  $('go').onclick = () => { if (!running && !$('go').disabled) build('file'); };
  $('oneclick').onclick = () => { if (!running && !$('oneclick').disabled) build('net'); };

  // The base map's URL on the range proxy: index.json's base.url, else made
  // from base.download (https://archive.org/download/<item>/<file>) and the
  // proxy that serves the packs.
  function baseUrlFor(reg, recipe) {
    if (reg.base.url) return reg.base.url;
    const m = /^https:\/\/archive\.org\/download\/([^/]+)\/([^/?#]+)$/.exec(reg.base.download || '');
    const via = Object.values(recipe.packs).map((p) => p.url).concat(reg.probe ? [reg.probe.url] : []).find(Boolean);
    if (!m || !via) return null;
    return new URL(via).origin + '/' + m[1] + '/' + m[2];
  }
  const fmtDur = (s) => (s < 90 ? 'a minute' : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`);

  async function build(mode) {
    running = { starting: true, mode };
    checkReady();                    // locks region, language and file too
    $('save').hidden = true; $('after').hidden = true;
    holdScreen();
    let recipe, fileName, sinkInfo = null, baseUrl = null;
    const notStarted = (msg) => {
      running = null; releaseScreen(); checkReady();
      setStatus('Not started: ' + msg, true); announce($('status').textContent);
    };
    try { recipe = await recipeFor(entry); }
    catch (e) { notStarted(`could not load the recipe for ${langName(entry.lang, entry.label)} (${e.message || e}). Try again later.`); return; }
    fileName = `${recipe.file_name || recipe.name}.zim`;
    if (mode === 'file') {
      try { sinkInfo = await prepareSink(fileName, recipe.size, true); }
      catch (e) { notStarted(e.name === 'AbortError' ? 'no place to save the file was chosen.' : (e.message || e)); return; }
    } else {
      baseUrl = baseUrlFor(region, recipe);
      if (!baseUrl) { notStarted(`the ${region.title} map cannot be downloaded from here yet. Download the map file, then use "Already have the map?".`); return; }
      setStatus('Starting…', true);
      if (!await ensurePacks(true)) {  // #packStatus says why
        running = null; releaseScreen(); checkReady();
        setStatus('Not started: Wikipedia could not be downloaded (see above). Nothing was saved.', true);
        return;
      }
    }
    const packs = Object.keys(recipe.packs).join(',');
    const bytes = {};
    for (const id of Object.keys(recipe.packs)) bytes[id] = packBytes.get(packKey(recipe.packs[id]));
    const t0 = performance.now();
    hiddenMs = 0; hiddenSince = document.visibilityState === 'hidden' ? performance.now() : null;
    const base = { region: region.id, lang: entry.lang, packs, mode, size_mb: Math.round(recipe.size / 1e6),
                   download_mb: Math.round((mode === 'net' ? recipe.base.size : 0) / 1e6 + entry.download_bytes / 1e6),
                   sink: sinkInfo ? sinkInfo.sink : 'pending' };
    track('build_start', base);
    if (sinkInfo && sinkInfo.sink === 'opfs') opfsState(fileName, 'building');
    const worker = new Worker('assembler-worker.js?v=3');
    running = { worker, base, t0, sent: new Set(), mode };
    checkReady();
    setStatus(mode === 'net' ? `Downloading the ${region.title} map…` : 'Starting…', true);
    announce(mode === 'net' ? `Downloading and building the file. Keep this tab open.` : 'Building the file. Keep this tab open.');
    let note = null;
    const result = await new Promise((resolve) => {
      worker.onmessage = async (e) => {
        const d = e.data;
        if (d.type === 'need-sink') {
          // The base's first chunk and UUID are checked: now open the download.
          try { sinkInfo = await prepareSink(fileName, recipe.size, false); }
          catch (err) { resolve({ ok: false, outcome: 'no_sink', error: err.message || String(err) }); return; }
          base.sink = sinkInfo.sink;
          if (sinkInfo.sink === 'opfs') opfsState(fileName, 'building');
          worker.postMessage({ cmd: 'sink', sink: sinkInfo.sink, handle: sinkInfo.handle, port: sinkInfo.port, fileName }, sinkInfo.transfer);
        } else if (d.type === 'note') {
          note = { text: d.text, at: performance.now() };
        } else if (d.type === 'progress') {
          const { written, size, ms, closing } = d;
          $('prog').value = written / size;
          let text;
          if (closing && sinkInfo && sinkInfo.sink === 'sw') text = `All ${fmtMB(size)} built and checked; your browser is saving the rest`;
          else if (d.base && d.base.got < d.base.size) {
            // Speed over the last 30 s; time left from the average since the
            // first byte (the proxy's speed swings too much for the former).
            const left = d.base.size - d.base.got, bps = d.base.bps, avg = d.base.avg || bps;
            text = `Downloading the ${region.title} map: ${fmtMB(d.base.got)} of ${fmtMB(d.base.size)}` +
              (bps > 0 ? `, ${(bps / 1e6).toFixed(1)} MB/s` : '') + (avg > 0 && ms > 20000 ? `, about ${fmtDur(left / avg)} left` : '');
          } else text = `${fmtMB(written)} of ${fmtMB(size)}`;
          if (note && performance.now() - note.at < 15000) text += ` (${note.text})`;
          if (!sinkInfo && written === 0 && mode === 'net' && !(d.base && d.base.got)) text = `Connecting to the ${region.title} map…`;
          setStatus(text + ' — keep this tab open', closing);
          for (const p of [25, 50, 75]) {
            if (written / size * 100 >= p && !running.sent.has(p)) {
              running.sent.add(p);
              announce(`Building: ${p}% done.`);
              track('build_progress', Object.assign({}, base, { pct: p, elapsed_s: Math.round(ms / 1000) }));
            }
          }
        } else if (d.type === 'done') resolve(d.result);
      };
      worker.onerror = (e) => resolve({ ok: false, outcome: 'error', error: 'worker: ' + e.message });
      // One click: the pack bytes move to the worker (no copy) and are
      // downloaded again for another build. From a copy they are copied, so
      // the Build button stays ready (its save dialog needs the click).
      const packBufs = mode === 'net' ? Object.values(bytes).map((u8) => u8.buffer) : [];
      worker.postMessage({ cmd: 'run', proto: 4, recipe, baseFile: mode === 'file' ? baseFile : null, baseUrl, packs: bytes, mem: MEM,
                           sink: sinkInfo ? sinkInfo.sink : 'later', handle: sinkInfo && sinkInfo.handle,
                           port: sinkInfo && sinkInfo.port, fileName }, (sinkInfo ? sinkInfo.transfer : []).concat(packBufs));
      if (mode === 'net') {
        for (const id of Object.keys(recipe.packs)) packBytes.delete(packKey(recipe.packs[id]));
        packsOk = false;
      }
    });
    worker.terminate();
    $('startDl').hidden = true;
    if (mode === 'net') { $('packStatus').textContent = ''; $('packStatus').className = ''; }
    if (sinkInfo && sinkInfo.cleanup) sinkInfo.cleanup(result.ok);
    // Only a watchdog (not the visitor cancelling) moves this tab to OPFS.
    if (result.outcome === 'download_stopped' && result.watchdog) swFailed();
    if (hiddenSince !== null) { hiddenMs += performance.now() - hiddenSince; hiddenSince = performance.now(); }
    const rn = result.net || {};
    track('build_end', Object.assign({}, base, netParams(), {
      outcome: result.outcome, secs: Math.round((performance.now() - t0) / 1000), mbps_total: result.mbps_total,
      local_mbps: rn.local_mbps, base_mbps: rn.base_mbps, base_stream_mbps_avg: rn.base_stream_mbps_avg,
      base_stream_mbps_p10: rn.base_stream_mbps_p10, base_ttfb_ms_p50: rn.base_ttfb_p50,
      base_retries: rn.base_retries, base_slow_retries: rn.base_slow_retries, base_resets: rn.base_resets,
      hidden_s: Math.round(hiddenMs / 1000), hash_ok: result.ok ? 1 : 0 }));
    running = null;
    releaseScreen();
    checkReady();
    if (result.ok) {
      const secs = Math.round(result.ms / 1000);
      const tail = ` ${fmtMB(result.size)} in ${secs < 120 ? secs + ' s' : fmtDur(secs)}` +
        (rn.base_mbps ? ` (map downloaded at ${rn.base_mbps} MB/s)` : '') + `. MD5 (as md5sum prints it): ${result.file_md5}`;
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
      if (sinkInfo && sinkInfo.sink === 'opfs') opfsState(fileName, null);
      // The worker discarded what it wrote; File System Access leaves the
      // empty file the save dialog created, which goes too.
      if (sinkInfo && sinkInfo.sink === 'fsa') {
        try { const f = await sinkInfo.handle.getFile(); if (f.size === 0 && sinkInfo.handle.remove) await sinkInfo.handle.remove(); } catch (e) { /* keep it */ }
      }
      setStatus(result.outcome === 'download_stopped' ? 'Stopped. The download stopped — try again (Chrome or Edge on a desktop works best).'
        : result.outcome === 'base_download' ? `Stopped: could not download the ${region.title} map (${result.error}). Nothing was saved. Try again.`
        : result.outcome === 'wrong_base' && mode === 'net' ? `Stopped: the ${region.title} map on the server is not the one this language was made for (${result.error}). Nothing was saved. Try again later.`
        : result.outcome === 'no_sink' ? 'Not started: ' + result.error
        : 'Stopped: ' + result.error, true);
      announce($('status').textContent);
    }
  }


  window.addEventListener('pagehide', () => {
    if (!running || !running.base) return;
    track('build_end', Object.assign({}, running.base, netParams(), { outcome: 'abandoned',
      secs: Math.round((performance.now() - running.t0) / 1000) }));
  });

  // ------------------------------------------------------------ speed and estimate
  // A short check of the connection once someone is on the page (4 ranges of
  // 512 KiB in parallel from the region's probe file, as the build downloads
  // 4 at a time; nothing is reported), so the time a download takes can be
  // shown before the click. "Test my download speed" measures 4 x 2 MiB and
  // reports it. During a build the real speed replaces the estimate.
  let speedBps = null, speedState = 'none';
  async function measureSpeed(manual) {
    const p = region && region.probe;
    if (!p || document.visibilityState !== 'visible' && !manual) return null;
    const part = manual ? 2 * 1048576 : 512 * 1024;
    speedState = 'measuring'; showEstimate();
    const t = performance.now();
    const one = async () => {
      const start = Math.floor(Math.random() * Math.max(1, (p.size - part) / 65536)) * 65536;
      const t1 = performance.now();
      const r = await fetch(p.url, { headers: { Range: `bytes=${start}-${start + part - 1}` }, cache: 'no-store', signal: AbortSignal.timeout ? AbortSignal.timeout(60000) : undefined });
      const ttfb = performance.now() - t1;
      const n = (await r.arrayBuffer()).byteLength;
      if (r.status !== 206 || n !== part) throw new Error('HTTP ' + r.status);
      return { n, ttfb };
    };
    let res;
    try { res = await Promise.all([one(), one(), one(), one()]); } catch (e) { speedState = 'failed'; showEstimate(); return null; }
    const secs = (performance.now() - t) / 1000;
    speedBps = res.reduce((a, r) => a + r.n, 0) / secs;
    speedState = 'done'; showEstimate();
    return { mbps: +(speedBps / 1e6).toFixed(2), ttfb: Math.round(res.reduce((a, r) => a + r.ttfb, 0) / res.length) };
  }
  function showEstimate() {
    if (!entry) return;
    $('estimate').textContent = speedState === 'measuring' ? 'Checking your connection…'
      : speedState === 'done' ? `About ${fmtDur(entry.size / speedBps)} at your connection's speed now (${(speedBps / 1e6).toFixed(1)} MB/s).`
      : speedState === 'failed' ? "Could not check your connection's speed."
      : `About ${fmtDur(entry.size / 1e6)} at 1 MB/s, ${fmtDur(entry.size / 1e7)} at 10 MB/s.`;
  }
  $('probe').onclick = async () => {
    if (!region || !region.probe) return;
    $('probeOut').textContent = 'Testing…';
    const r = await measureSpeed(true);
    $('probeOut').textContent = r ? `About ${r.mbps} MB/s with 4 downloads at once (first byte after ${r.ttfb} ms).` : 'The test failed.';
    track('build_probe', { region: region.id, mbps: r && r.mbps, ttfb_ms: r && r.ttfb, ok: r ? 1 : 0 });
  };

  load().catch((e) => { $('notice').textContent = 'Could not load the list of maps (' + (e.message || e) + '). Try again later.'; });
  tidyBrowserStorage();
})();
