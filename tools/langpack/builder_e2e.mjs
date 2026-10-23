// End-to-end run of the builder page (web/build/) in headless Chromium,
// against local files: the page assembles each language's map and the saved
// file must be byte-identical to the merge that passed the gates (the
// recipe's file_md5).
//
//   node builder_e2e.mjs --web WEB_DIR --recipes RECIPES_DIR --item ITEM=DIR [--item ...]
//        --region R --langs L1,L2 --out OUT_DIR [--mode net|file] [--base BASE.zim]
//        [--corrupt ITEM/FILE@OFFSET] [--expect pass|fail] [--proxy-url URL]
//
// --merged-origin: the build worker origin the page's preview-config.js names
// (STREETZIM_MERGED_ORIGIN): its entries' webseeds get "Preview in browser".
// --proxy-url: the page uses that range proxy (the real one in front of
// archive.org, web/drive/preview-config.js) instead of the local stand-in
// below; then no --item is needed and the "proxy" counts stay 0.
//
// Feedback (what a visitor sees after the click):
//   --throttle DOWN_KBPS:UP_KBPS:LATENCY_MS  Chromium's network emulation
//        (CDP Network.emulateNetworkConditions) for the page and its worker
//   --watch-secs S   record every visible change of the job panel, the
//        button and the status from the click on, for S seconds or until
//        the build ends: "feedback": first_change_ms (click to the first
//        change), max_gap_ms (longest time without any visible change),
//        max_progress_gap_ms (without a change of the phase line or the bar)
//   --feedback-only  stop after the watch (the build is abandoned): PASS when
//        the first change came within 200 ms and no gap reached 1.5 s
//   --viewport WxH (default 1280x900)   --shots DIR  a screenshot per phase
// Every run records "ui": the texts #baseCheck (a live region) and
// #packStatus took, the buttons' labels, the phases in order, every value
// of the bar, the focused element during and after the job, the busy
// button at the end, and the worker's last progress message.
//   --corrupt-times N  corrupt only the first N answers (a transient fault)
//   --status-timeout-secs S  how long to wait for the page's final word (7200)
// Two local servers stand in for the site and the range proxy:
//   page  http://localhost:P1/  WEB_DIR, with /build/recipes/ = RECIPES_DIR and
//         /drive/preview-config.js naming the proxy below (the one place the
//         page reads its proxy from)
//   proxy http://127.0.0.1:P2/<item>/<file>  the file from that item's DIR,
//         a single byte range only (400 without one), CORS as the real proxy
//         (preview-proxy/ia-range-proxy.js): a cross-origin range request each
// --mode net: the one-click Download (packs, then the base through the
// proxy); --mode file: "build from my copy" with --base picked in the file
// input (showSaveFilePicker hidden: a headless browser cannot drive it, so
// the page streams through its service worker). Writes OUT_DIR/e2e-<L>.json
// and prints one JSON line per language; exit 0 when every language ends
// "Done" with the expected MD5. --corrupt flips the byte at OFFSET of that
// file in every answer the proxy gives that covers it; with --expect fail a
// language passes when the page stops ("Stopped" / "Not started") and saves
// nothing. A language that throws is a FAIL; the others still run.
// PLAYWRIGHT_CORE, CHROME_PATH, BROWSER_LIBS: as the gates (ops/langpack/host-env.sh).
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const opts = (k) => argv.flatMap((a, i) => (a === k ? [argv[i + 1]] : []));
const WEB = path.resolve(opt('--web'));
const RECIPES = path.resolve(opt('--recipes'));
const ITEMS = Object.fromEntries(opts('--item').map((s) => { const [k, v] = s.split('='); return [k, path.resolve(v)]; }));
const REGION = opt('--region');
const LANGS = opt('--langs').split(',');
const OUT = path.resolve(opt('--out'));
const MODE = opt('--mode', 'net');
const BASE = opt('--base');
const EXPECT = opt('--expect', 'pass');
const PROXY_URL = opt('--proxy-url', '');
const MERGED_ORIGIN = opt('--merged-origin', '');
const WAIT_PREVIEWS = +opt('--wait-previews', '0');   // links that come later (from a segment index)
const THROTTLE = opt('--throttle', '');
const WATCH = +opt('--watch-secs', '0');
const FEEDBACK_ONLY = argv.includes('--feedback-only');
const [VW, VH] = opt('--viewport', '1280x900').split('x').map(Number);
const SHOTS = opt('--shots', '');
const CORRUPT_TIMES = +opt('--corrupt-times', '0');
const STATUS_TIMEOUT = +opt('--status-timeout-secs', '7200');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const CORRUPT = (() => {
  const c = opt('--corrupt'); if (!c) return null;
  const m = /^([^/]+)\/([^@]+)@(\d+)$/.exec(c); if (!m) throw new Error('--corrupt ITEM/FILE@OFFSET');
  return { item: m[1], file: m[2], at: +m[3] };
})();
if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
fs.mkdirSync(OUT, { recursive: true });
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.json': 'application/json',
  '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json' };
const stats = { proxyRanges: 0, proxyBytes: 0, proxyRefused: 0, items: {}, files: {} };

function within(dir, rel) {
  const p = path.resolve(dir, rel);
  return p === dir || p.startsWith(dir + path.sep) ? p : null;
}
function sendFile(req, res, p, headers = {}) {
  let st;
  try { st = fs.statSync(p); } catch (e) { res.writeHead(404, headers); res.end('not found'); return; }
  if (st.isDirectory()) { sendFile(req, res, path.join(p, 'index.html'), headers); return; }
  res.writeHead(200, { 'Content-Type': TYPES[path.extname(p)] || 'application/octet-stream', 'Content-Length': st.size,
                       'Cache-Control': 'no-store', ...headers });
  if (req.method === 'HEAD') { res.end(); return; }
  fs.createReadStream(p).pipe(res);
}
let PROXY = '';
const page = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  const p = decodeURIComponent(u.pathname);
  if (p === '/drive/preview-config.js') {
    res.writeHead(200, { 'Content-Type': 'text/javascript', 'Cache-Control': 'no-store' });
    res.end(`window.STREETZIM_PREVIEW_PROXY = ${JSON.stringify(PROXY)};\n`
      + `window.STREETZIM_MERGED_ORIGIN = ${JSON.stringify(MERGED_ORIGIN)};\n`);
    return;
  }
  if (p.startsWith('/build/recipes/')) {
    const f = within(RECIPES, p.slice('/build/recipes/'.length));
    if (!f) { res.writeHead(403); res.end(); return; }
    sendFile(req, res, f);
    return;
  }
  const f = within(WEB, p.slice(1));
  if (!f) { res.writeHead(403); res.end(); return; }
  sendFile(req, res, f);
});
const CORS = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
  'Access-Control-Allow-Headers': 'Range, If-Range',
  'Access-Control-Expose-Headers': 'Content-Range, Content-Length, Accept-Ranges, ETag, Last-Modified' };
const proxy = http.createServer((req, res) => {
  if (req.method === 'OPTIONS') { res.writeHead(204, CORS); res.end(); return; }
  const m = /^\/([^/]+)\/([^/]+)$/.exec(new URL(req.url, 'http://x').pathname);
  const dir = m && ITEMS[m[1]];
  const f = dir && within(dir, decodeURIComponent(m[2]));
  if (!f || !fs.existsSync(f)) { res.writeHead(404, CORS); res.end('no such file'); return; }
  const size = fs.statSync(f).size;
  const q = new URL(req.url, 'http://x').searchParams.get('bytes');
  const range = q ? 'bytes=' + q : req.headers.range;
  const r = /^bytes=(\d+)-(\d*)$/.exec(range || '');
  if (req.method === 'GET' && !r) { stats.proxyRefused++; res.writeHead(400, CORS); res.end('a single byte range is required'); return; }
  if (req.method === 'HEAD') { res.writeHead(200, { ...CORS, 'Content-Length': size, 'Accept-Ranges': 'bytes' }); res.end(); return; }
  const a = +r[1], b = Math.min(r[2] === '' ? size - 1 : +r[2], size - 1);
  if (a > b) { res.writeHead(416, { ...CORS, 'Content-Range': `bytes */${size}` }); res.end(); return; }
  stats.proxyRanges++; stats.proxyBytes += b - a + 1; stats.items[m[1]] = (stats.items[m[1]] || 0) + 1;
  const fk = decodeURIComponent(m[2]); stats.files[fk] = (stats.files[fk] || 0) + 1;
  res.writeHead(206, { ...CORS, 'Content-Type': 'application/octet-stream', 'Content-Length': b - a + 1,
                       'Content-Range': `bytes ${a}-${b}/${size}`, 'Accept-Ranges': 'bytes' });
  if (CORRUPT && CORRUPT.item === m[1] && CORRUPT.file === decodeURIComponent(m[2]) && a <= CORRUPT.at && CORRUPT.at <= b
      && !(CORRUPT_TIMES && (stats.corrupted || 0) >= CORRUPT_TIMES)) {
    const buf = Buffer.alloc(b - a + 1);
    const fd = fs.openSync(f, 'r'); fs.readSync(fd, buf, 0, buf.length, a); fs.closeSync(fd);
    buf[CORRUPT.at - a] ^= 0xff; stats.corrupted = (stats.corrupted || 0) + 1;
    res.end(buf);
    return;
  }
  fs.createReadStream(f, { start: a, end: b }).pipe(res);
});
await new Promise((r) => page.listen(0, 'localhost', r));
await new Promise((r) => proxy.listen(0, '127.0.0.1', r));
PROXY = PROXY_URL || `http://127.0.0.1:${proxy.address().port}`;
const PAGE = `http://localhost:${page.address().port}/build/`;

async function md5File(p) {
  return new Promise((res, rej) => {
    const h = crypto.createHash('md5');
    fs.createReadStream(p, { highWaterMark: 16 << 20 }).on('data', (b) => h.update(b)).on('end', () => res(h.digest('hex'))).on('error', rej);
  });
}

// The two servers keep node alive: whatever fails from here, end the
// process (a browser that cannot start left it waiting forever).
const quit = (code) => { page.close(); proxy.close(); process.exit(code); };
process.on('unhandledRejection', (e) => { console.error('builder_e2e:', e && e.stack || e); quit(1); });
let browser;
try {
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true,
                                    downloadsPath: path.join(OUT, 'dl'), timeout: 120000 });
} catch (e) {
  for (const L of LANGS) console.log(JSON.stringify({ lang: L, mode: MODE, pass: false, error: 'browser: ' + String(e && e.message || e).slice(0, 300) }));
  quit(1);
}
let allOk = true;
async function runOne(L) {
  const t0 = Date.now();
  const recipe = JSON.parse(fs.readFileSync(path.join(RECIPES, REGION, `${L}.json`), 'utf8'));
  const ctx = await browser.newContext({ acceptDownloads: true, locale: L === 'en' ? 'en-US' : L });
  await ctx.addInitScript(() => { Object.defineProperty(window, 'showSaveFilePicker', { value: undefined, configurable: true }); });
  const pg = await ctx.newPage();
  await pg.setViewportSize({ width: VW, height: VH });
  if (THROTTLE) {
    const [down, up, lat] = THROTTLE.split(':').map(Number);
    const cdp = await ctx.newCDPSession(pg);
    await cdp.send('Network.enable');
    await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: lat || 0,
      downloadThroughput: down * 1000 / 8, uploadThroughput: (up || down) * 1000 / 8 });
  }
  const log = [];
  pg.on('console', (m) => log.push(`${m.type()}: ${m.text()}`));
  pg.on('pageerror', (e) => log.push(`pageerror: ${e.message}`));
  const before = { ...stats, items: { ...stats.items }, files: { ...stats.files } };
  await pg.goto(`${PAGE}?region=${encodeURIComponent(REGION)}&lang=${encodeURIComponent(L)}`);
  await pg.waitForFunction((l) => document.querySelector('#lang') && document.querySelector('#lang').value === l, L, { timeout: 60000 });
  if (WAIT_PREVIEWS) {
    await pg.waitForFunction((n) => document.querySelectorAll('#downloads li a.preview').length >= n, WAIT_PREVIEWS,
      { timeout: 15000 }).catch(() => {});
  }
  const shown = await pg.evaluate(() => ({
    regionOptions: [...document.querySelectorAll('#region option')].map((o) => o.textContent),
    langOption: document.querySelector('#lang').selectedOptions[0].textContent,
    downloads: [...document.querySelectorAll('#downloads li a')].map((a) => a.textContent).slice(0, 25),
    torrents: [...document.querySelectorAll('#downloads li a.torrent')].map((a) => a.getAttribute('href')),
    previews: [...document.querySelectorAll('#downloads li a.preview')].map((a) => a.getAttribute('href')),
    packs: [...document.querySelectorAll('#packs li')].map((li) => li.textContent),
    button: document.querySelector('#oneclick').textContent, notice: document.querySelector('#notice').textContent }));
  const dlP = pg.waitForEvent('download', { timeout: 0 });
  dlP.catch(() => {});                       // no download (an expected failure): closing the context rejects it
  const snap = () => ({ ranges: stats.proxyRanges - before.proxyRanges, bytes: stats.proxyBytes - before.proxyBytes,
                        items: Object.fromEntries(Object.entries(stats.items).map(([k, v]) => [k, v - (before.items[k] || 0)])),
                        files: Object.fromEntries(Object.entries(stats.files).map(([k, v]) => [k, v - (before.files[k] || 0)])) });
  let beforeBuild = null, feedback = null;    // what the proxy served before Build was pressed; the watch
  // what the page showed, from now on (not only after the click)
  await pg.evaluate(() => {
    const q = (s) => document.querySelector(s);
    const ui = window.__ui = { baseCheckTexts: [], packStatusTexts: [], buttonTexts: [], phases: [], bar: [], phaseWrites: 0,
                               activeDuring: null };
    const push = (arr, v) => { if (v && arr[arr.length - 1] !== v && arr.length < 400) arr.push(v); };
    new MutationObserver((recs) => {
      for (const r of recs) {
        const t = r.target.nodeType === 3 ? r.target.parentNode : r.target;
        if (t && t.id === 'phase' && r.type !== 'attributes') ui.phaseWrites++;
      }
      push(ui.baseCheckTexts, q('#baseCheck') && q('#baseCheck').textContent);
      push(ui.packStatusTexts, q('#packStatus') && q('#packStatus').textContent);
      push(ui.buttonTexts, q('#oneclick').textContent); push(ui.buttonTexts, q('#go') && q('#go').textContent);
      push(ui.phases, q('#job') && q('#job').dataset.phase);
      const v = q('#prog') && q('#prog').getAttribute('value');
      if (v !== null && v !== undefined && ui.bar[ui.bar.length - 1] !== +v && ui.bar.length < 4000) ui.bar.push(+v);
    }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
    document.addEventListener('click', () => setTimeout(() => {
      const a = document.activeElement; ui.activeDuring = a ? a.id || a.tagName : null; }, 1000), { capture: true, once: true });
  });
  const uiEnd = () => pg.evaluate(() => {
    const q = (s) => document.querySelector(s), a = document.activeElement;
    const btn = (b) => b && { text: b.textContent, cls: b.className, disabled: b.disabled, ariaDisabled: b.getAttribute('aria-disabled') };
    const lp = window.__szProgress || null;
    return Object.assign({}, window.__ui, { oneclick: btn(q('#oneclick')), go: btn(q('#go')), activeEnd: a ? a.id || a.tagName : null,
      jobHidden: q('#job') ? q('#job').hidden : null, langDisabled: q('#lang') ? q('#lang').disabled : null,
      workerPhases: window.__szPhaseLog || [],
      lastProgress: lp && { written: lp.written, size: lp.size, map: lp.map, packs: lp.packs, base: lp.base && { got: lp.base.got, size: lp.base.size } } });
  });
  // every visible change from the click on (page time), and the click itself
  await pg.evaluate(() => {
    window.__vis = []; window.__prog = []; window.__progText = []; window.__clickAt = null;
    document.addEventListener('click', () => { if (window.__clickAt === null) window.__clickAt = performance.now(); }, true);
    const q = (s) => document.querySelector(s);
    const progSig = () => [q('#phase') && q('#phase').textContent, q('#prog') && q('#prog').getAttribute('value'),
                           q('#job') && q('#job').hidden].join('|');
    const sig = () => [progSig(), q('#elapsed') && q('#elapsed').textContent, q('#oneclick').textContent,
                       q('#oneclick').className, q('#go') && q('#go').textContent, q('#status').textContent].join('|');
    let last = sig(), lastP = progSig();
    new MutationObserver(() => {
      if (window.__clickAt === null) return;
      const now = performance.now(), s = sig(), p = progSig();
      if (s !== last) { last = s; window.__vis.push(now); }
      if (p !== lastP) { lastP = p; window.__prog.push(now); window.__progText.push(p.slice(0, 160)); }
    }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
  });
  const phases = [];
  let shotN = 0;
  const shot = async (tag) => {
    if (!SHOTS) return;
    fs.mkdirSync(SHOTS, { recursive: true });
    await pg.screenshot({ path: path.join(SHOTS, `${L}-${VW}x${VH}-${String(++shotN).padStart(2, '0')}-${tag}.png`), fullPage: false });
  };
  // after the click: the phases seen (a screenshot each), until the watch ends or the build does
  const watch = async () => {
    const tEnd = Date.now() + WATCH * 1000;
    let seen = null;
    while (Date.now() < tEnd) {
      const st = await pg.evaluate(() => ({ phase: document.querySelector('#job') ? document.querySelector('#job').dataset.phase || '' : '',
                                            status: document.querySelector('#status').textContent }));
      if (st.phase && st.phase !== seen) { seen = st.phase; phases.push(st.phase); await shot(st.phase); }
      if (/^(Done|Stopped|Not started)/.test(st.status)) { await shot('end'); break; }
      await sleep(150);
    }
    return pg.evaluate(() => {
      const c = window.__clickAt, now = performance.now();
      const gaps = (ts) => { let m = 0, prev = c; for (const t of [...ts, now]) { m = Math.max(m, t - prev); prev = t; } return Math.round(m); };
      // where the longest wait for the phase line or the bar was: the line before it, and after
      let gi = -1, gm = -1, prev = c;
      window.__prog.forEach((t, i) => { if (t - prev > gm) { gm = t - prev; gi = i; } prev = t; });
      const around = gi >= 0 ? { at_s: Math.round((window.__prog[gi] - c) / 100) / 10, before: window.__progText[gi - 1] || null,
                                  after: window.__progText[gi] } : null;
      return { first_change_ms: window.__vis.length ? Math.round(window.__vis[0] - c) : null, max_gap_ms: gaps(window.__vis),
               max_progress_gap_ms: gaps(window.__prog), longest_progress_gap: around, changes: window.__vis.length,
               watched_s: Math.round((now - c) / 1000) };
    });
  };
  if (MODE === 'file') {
    await pg.click('#haveBaseSummary');      // "Already have the map?" opens the file step
    await pg.setInputFiles('#baseFile', BASE);
    // Build enabled, or the page turned the file (or a pack) down: say so now, not after 30 min
    const ready = await (await pg.waitForFunction(() => {
      if (!document.querySelector('#go').disabled) return 'ready';
      const bad = document.querySelector('#baseCheck.bad, #packStatus.bad');
      return bad ? bad.textContent : null;
    }, null, { timeout: 30 * 60000, polling: 1000 })).jsonValue();
    if (ready !== 'ready') throw new Error(`Build never enabled: ${ready}`);
    beforeBuild = snap();
    await pg.click('#go');
    if (WATCH) feedback = await watch();
  } else {
    await pg.waitForFunction(() => !document.querySelector('#oneclick').disabled, null, { timeout: 60000 });
    await pg.waitForTimeout(1200);           // the page ignores a key press within 1 s of being ready (not a click; to be safe)
    beforeBuild = snap();
    await pg.click('#oneclick');
    if (WATCH) feedback = await watch();
  }
  if (FEEDBACK_ONLY) {
    const fb = feedback || {};
    const res = { lang: L, mode: MODE, viewport: `${VW}x${VH}`, throttle: THROTTLE, feedback: fb, phases, ui: await uiEnd(),
                  pass: fb.first_change_ms !== null && fb.first_change_ms < 200 && fb.max_gap_ms < 1500 };
    fs.writeFileSync(path.join(OUT, `feedback-${L}-${VW}x${VH}.json`), JSON.stringify(res, null, 1));
    console.log(JSON.stringify(res));
    await Promise.race([ctx.close().catch(() => {}), sleep(10000)]);
    return res.pass;
  }
  const status = await pg.waitForFunction(() => {
    const t = document.querySelector('#status').textContent;
    if (/^(Done|Stopped|Not started)/.test(t)) return t;
    const save = document.querySelector('#saveLink');
    return save ? 'SAVE ' + t : null;
  }, null, { timeout: STATUS_TIMEOUT * 1000, polling: 1000 }).then((h) => h.jsonValue());
  if (/^SAVE /.test(status)) await pg.click('#saveLink');     // built into browser storage: the Save step
  const res = { lang: L, mode: MODE, status, shown, expected_md5: recipe.file_md5, expected_size: recipe.size, log: log.slice(-15) };
  if (/^(Done|SAVE )/.test(status)) {
    const dl = await dlP;
    res.suggested = dl.suggestedFilename();
    const p = await dl.path();
    res.download_failure = await dl.failure();
    res.bytes = fs.statSync(p).size;
    res.md5 = await md5File(p);
    res.saved = path.join(OUT, `${REGION}-${L}.zim`);
    fs.renameSync(p, res.saved);
  }
  res.proxy_before_build = beforeBuild;
  res.proxy = { ranges: stats.proxyRanges - before.proxyRanges, bytes: stats.proxyBytes - before.proxyBytes,
                refused: stats.proxyRefused - before.proxyRefused,
                items: Object.fromEntries(Object.entries(stats.items).map(([k, v]) => [k, v - (before.items[k] || 0)])),
                files: Object.fromEntries(Object.entries(stats.files).map(([k, v]) => [k, v - (before.files[k] || 0)])) };
  res.secs = Math.round((Date.now() - t0) / 1000);
  res.pass = EXPECT === 'fail'
    ? /^(Stopped|Not started)/.test(String(status)) && !res.md5
    : res.md5 === recipe.file_md5 && res.bytes === recipe.size;
  res.expect = EXPECT; res.corrupted = stats.corrupted || 0;
  res.ui = await uiEnd();
  if (feedback) { res.feedback = feedback; res.phases = phases; }
  fs.writeFileSync(path.join(OUT, `e2e-${L}.json`), JSON.stringify(res, null, 1));
  console.log(JSON.stringify({ lang: L, mode: MODE, expect: EXPECT, pass: res.pass, status: String(status).slice(0, 160), bytes: res.bytes,
                               md5: res.md5, expected: recipe.file_md5, secs: res.secs, proxy: res.proxy, proxy_before_build: beforeBuild,
                               feedback: res.feedback, phases: res.phases, ui: res.ui,
                               langOption: shown.langOption }));
  await ctx.close();
  return res.pass;
}
for (const L of LANGS) {
  try {
    allOk = (await runOne(L)) && allOk;
  } catch (e) {
    allOk = false;
    const res = { lang: L, mode: MODE, expect: EXPECT, pass: false, error: String(e && e.stack || e).slice(0, 2000) };
    fs.writeFileSync(path.join(OUT, `e2e-${L}.json`), JSON.stringify(res, null, 1));
    console.log(JSON.stringify({ lang: L, mode: MODE, pass: false, error: String(e && e.message || e).slice(0, 300) }));
  }
}
// a browser that died may never answer the close
await Promise.race([browser.close().catch(() => {}), new Promise((r) => setTimeout(r, 15000))]);
quit(allOk ? 0 : 1);
