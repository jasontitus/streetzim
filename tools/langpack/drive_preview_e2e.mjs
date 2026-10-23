// "Preview in browser" end to end: the /drive/ picker streams a map's
// language version from the build worker (build-proxy/) and the viewer shows
// it in that language. Headless Chromium against local servers only.
//
//   node drive_preview_e2e.mjs --site ORIGIN --zim URL --lang L
//        [--article PATH --article-text TEXT] [--refuse URL ...]
//        [--browse-secs S [--browse-center LON,LAT,ZOOM]] [--mark ORIGIN]
//
// --site: where web/ is served (scripts/serve-web-local.py), its
// /drive/preview-config.js naming the worker as STREETZIM_MERGED_ORIGIN.
// --zim: the entry's webseed URL on that worker. Checks:
//   refused   each --refuse URL (another origin, another path on the worker,
//             a query) is refused by the picker: "Can't stream that"
//   opened    ?zim=URL hands over to /drive/viewer/
//   ui_lang   the viewer chose L's UI table (SZ_UI_LANG; a language with no
//             table: the fallback the viewer declares) and <html lang> says so
//   map_lang  map-config.json, read through the service worker, says L
//   article   --article (a ZIM path, e.g. ~w/de/wiki-article/X) comes back
//             through the service worker with --article-text in it
// --browse-secs: then pans and zooms the map around its centre for S
// seconds (what a visitor does) and reports what the page asked for.
// --mark ORIGIN: before and after the browse, GET ORIGIN/mark/<phase> (a
// 404 in the worker's log that brackets the browse's requests).
// Prints one JSON line; exit 0 when every check passes.
// PLAYWRIGHT_CORE, CHROME_PATH, BROWSER_LIBS: as the gates (ops/langpack/host-env.sh).
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');

const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const opts = (k) => argv.flatMap((a, i) => (a === k ? [argv[i + 1]] : []));
const SITE = opt('--site').replace(/\/+$/, '');
const ZIM = opt('--zim');
const LANG = opt('--lang');
const ARTICLE = opt('--article', '');
const ARTICLE_TEXT = opt('--article-text', '');
const REFUSE = opts('--refuse');
const BROWSE = +opt('--browse-secs', '0');
const MARK = opt('--mark', '');
const CENTER = opt('--browse-center', '');      // lon,lat,zoom: where the visitor goes first
if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = { zim: ZIM, lang: LANG, checks: {} };
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true, args: ['--no-sandbox'] });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const errors = [];
const page = await ctx.newPage();
page.on('pageerror', (e) => errors.push(e.message));
async function step(name, fn) {
  try { const r = await fn(); out[name] = r; out.checks[name] = !!r.pass; }
  catch (e) { out[name] = { error: String(e && e.message || e).slice(0, 300) }; out.checks[name] = false; }
}
const pickerSays = () => page.evaluate(() => ({
  title: document.getElementById('status-title').textContent, msg: document.getElementById('status').textContent }));
try {
  await step('refused', async () => {
    const rows = [];
    for (const u of REFUSE) {
      await page.goto(`${SITE}/drive/?zim=${encodeURIComponent(u)}`);
      await page.waitForFunction(() => /Can't stream that|Failed|Connecting/.test(document.getElementById('status-title').textContent),
        null, { timeout: 30000 });
      const s = await pickerSays();
      rows.push({ url: u, ...s, refused: s.title === "Can't stream that" });
    }
    return { pass: rows.every((r) => r.refused), rows };
  });

  await step('opened', async () => {
    const t0 = Date.now();
    await page.goto(`${SITE}/drive/?zim=${encodeURIComponent(ZIM)}`);
    await page.waitForURL(/\/drive\/viewer\//, { timeout: 180000 });
    return { pass: true, ms: Date.now() - t0, url: page.url() };
  });

  await step('ui_lang', async () => {
    // the viewer starts in English and switches once map-config.json is read
    const settled = await page.waitForFunction((lang) => {
      if (typeof SZ_UI_LANG === 'undefined') return false;
      const fb = typeof SZ_UI_FALLBACK !== 'undefined' && SZ_UI_FALLBACK ? SZ_UI_FALLBACK[lang] : null;
      const want = fb || lang;
      return SZ_UI_LANG === want && document.documentElement.lang === want;
    }, LANG, { timeout: 120000, polling: 250 }).then(() => true, () => false);
    const f = await page.evaluate(() => ({
      ui: SZ_UI_LANG, html: document.documentElement.lang, dir: document.documentElement.dir,
      fallback: typeof SZ_UI_FALLBACK !== 'undefined' ? SZ_UI_FALLBACK : null }));
    const want = (f.fallback && f.fallback[LANG]) || LANG;
    return { pass: settled && f.ui === want && f.html === want, want, ...f };
  });

  await step('map_lang', async () => {
    const cfg = await page.evaluate(async () => (await fetch('map-config.json')).json());
    return { pass: cfg.language === LANG, language: cfg.language };
  });

  if (ARTICLE) {
    await step('article', async () => {
      const r = await page.evaluate(async (p) => {
        const res = await fetch(p);
        return { status: res.status, type: res.headers.get('content-type'), text: (await res.text()).slice(0, 20000) };
      }, ARTICLE);
      return { pass: r.status === 200 && r.text.includes(ARTICLE_TEXT), status: r.status, type: r.type, found: r.text.includes(ARTICLE_TEXT) };
    });
  }

  if (BROWSE > 0) {
    await step('browse', async () => {
      await page.waitForFunction(() => window.__szMap && window.__szMap.loaded(), null, { timeout: 180000, polling: 500 });
      const mark = async (phase) => { if (MARK) await fetch(`${MARK.replace(/\/+$/, '')}/mark/${phase}`).catch(() => {}); };
      const ping = () => page.evaluate(() => new Promise((ok) => {
        const ch = new MessageChannel();
        ch.port1.onmessage = (e) => ok(e.data);
        navigator.serviceWorker.controller.postMessage({ type: 'ping' }, [ch.port2]);
        setTimeout(() => ok(null), 3000);
      }));
      const before = await ping();
      await mark('browse-start');
      const t0 = Date.now();
      if (CENTER) {
        const [lng, lat, z] = CENTER.split(',').map(Number);
        await page.evaluate(([lng, lat, z]) => window.__szMap.jumpTo({ center: [lng, lat], zoom: z }), [lng, lat, z]);
      }
      const c = await page.evaluate(() => { const m = window.__szMap; const p = m.getCenter(); return { lng: p.lng, lat: p.lat, z: m.getZoom() }; });
      // a visitor's minute: zoom in step by step while looking around, then out
      const moves = [[0, 0, 1], [300, 0, 0], [0, 250, 0], [0, 0, 1], [-300, 0, 0], [0, -250, 0], [0, 0, 1], [400, 200, 0],
                     [-400, -200, 0], [0, 0, 1], [300, -200, 0], [0, 0, -2], [-300, 200, 0], [0, 0, -2]];
      let i = 0;
      while (Date.now() - t0 < BROWSE * 1000) {
        const [dx, dy, dz] = moves[i++ % moves.length];
        await page.evaluate(([dx, dy, dz]) => {
          const m = window.__szMap;
          if (dz) m.easeTo({ zoom: Math.max(m.getMinZoom(), Math.min(m.getMaxZoom(), m.getZoom() + dz)), duration: 600 });
          else m.panBy([dx, dy], { duration: 600 });
        }, [dx, dy, dz]);
        await sleep(2500);
      }
      await page.waitForFunction(() => window.__szMap.loaded(), null, { timeout: 60000, polling: 500 }).catch(() => {});
      const secs = (Date.now() - t0) / 1000;
      await mark('browse-end');
      const after = await ping();
      const reqs = after && before && after.stats && before.stats ? after.stats.requests - before.stats.requests : null;
      const bytes = after && before && after.stats && before.stats ? after.stats.bytes - before.stats.bytes : null;
      return { pass: reqs !== null, secs: Math.round(secs), moves: i, start: c, reader_requests: reqs,
               reader_mb: bytes === null ? null : +(bytes / 1048576).toFixed(1),
               requests_per_min: reqs === null ? null : Math.round(reqs / secs * 60) };
    });
  }
} catch (e) {
  out.error = String(e && e.message || e).slice(0, 300);
} finally {
  out.page_errors = errors.slice(0, 5);
  out.pass = Object.values(out.checks).length > 0 && Object.values(out.checks).every(Boolean) && !out.error;
  await Promise.race([browser.close(), sleep(5000)]);
}
console.log(JSON.stringify(out));
process.exit(out.pass ? 0 : 1);
