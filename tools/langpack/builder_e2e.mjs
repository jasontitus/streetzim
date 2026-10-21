// End-to-end run of the builder page (web/build/) in headless Chromium,
// against local files: the page assembles each language's map and the saved
// file must be byte-identical to the merge that passed the gates (the
// recipe's file_md5).
//
//   node builder_e2e.mjs --web WEB_DIR --recipes RECIPES_DIR --item ITEM=DIR [--item ...]
//        --region R --langs L1,L2 --out OUT_DIR [--mode net|file] [--base BASE.zim]
//
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
// "Done" with the expected MD5.
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
if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
fs.mkdirSync(OUT, { recursive: true });
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.json': 'application/json',
  '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json' };
const stats = { proxyRanges: 0, proxyBytes: 0, proxyRefused: 0, items: {} };

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
    res.end(`window.STREETZIM_PREVIEW_PROXY = ${JSON.stringify(PROXY)};\n`);
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
  res.writeHead(206, { ...CORS, 'Content-Type': 'application/octet-stream', 'Content-Length': b - a + 1,
                       'Content-Range': `bytes ${a}-${b}/${size}`, 'Accept-Ranges': 'bytes' });
  fs.createReadStream(f, { start: a, end: b }).pipe(res);
});
await new Promise((r) => page.listen(0, 'localhost', r));
await new Promise((r) => proxy.listen(0, '127.0.0.1', r));
PROXY = `http://127.0.0.1:${proxy.address().port}`;
const PAGE = `http://localhost:${page.address().port}/build/`;

async function md5File(p) {
  return new Promise((res, rej) => {
    const h = crypto.createHash('md5');
    fs.createReadStream(p, { highWaterMark: 16 << 20 }).on('data', (b) => h.update(b)).on('end', () => res(h.digest('hex'))).on('error', rej);
  });
}

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true,
                                        downloadsPath: path.join(OUT, 'dl') });
let allOk = true;
for (const L of LANGS) {
  const t0 = Date.now();
  const recipe = JSON.parse(fs.readFileSync(path.join(RECIPES, REGION, `${L}.json`), 'utf8'));
  const ctx = await browser.newContext({ acceptDownloads: true, locale: L === 'en' ? 'en-US' : L });
  await ctx.addInitScript(() => { Object.defineProperty(window, 'showSaveFilePicker', { value: undefined, configurable: true }); });
  const pg = await ctx.newPage();
  const log = [];
  pg.on('console', (m) => log.push(`${m.type()}: ${m.text()}`));
  pg.on('pageerror', (e) => log.push(`pageerror: ${e.message}`));
  const before = { ...stats, items: { ...stats.items } };
  await pg.goto(`${PAGE}?region=${encodeURIComponent(REGION)}&lang=${encodeURIComponent(L)}`);
  await pg.waitForFunction((l) => document.querySelector('#lang') && document.querySelector('#lang').value === l, L, { timeout: 60000 });
  const shown = await pg.evaluate(() => ({
    regionOptions: [...document.querySelectorAll('#region option')].map((o) => o.textContent),
    langOption: document.querySelector('#lang').selectedOptions[0].textContent,
    downloads: [...document.querySelectorAll('#downloads li a')].map((a) => a.textContent).slice(0, 25),
    packs: [...document.querySelectorAll('#packs li')].map((li) => li.textContent),
    button: document.querySelector('#oneclick').textContent, notice: document.querySelector('#notice').textContent }));
  const dlP = pg.waitForEvent('download', { timeout: 0 });
  if (MODE === 'file') {
    await pg.setInputFiles('#baseFile', BASE);
    await pg.waitForFunction(() => !document.querySelector('#go').disabled, null, { timeout: 30 * 60000, polling: 1000 });
    await pg.click('#go');
  } else {
    await pg.waitForFunction(() => !document.querySelector('#oneclick').disabled, null, { timeout: 60000 });
    await pg.waitForTimeout(1200);           // the page ignores a key press within 1 s of being ready (not a click; to be safe)
    await pg.click('#oneclick');
  }
  const status = await pg.waitForFunction(() => {
    const t = document.querySelector('#status').textContent;
    if (/^(Done|Stopped|Not started)/.test(t)) return t;
    const save = document.querySelector('#saveLink');
    return save ? 'SAVE ' + t : null;
  }, null, { timeout: 120 * 60000, polling: 1000 }).then((h) => h.jsonValue());
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
  res.proxy = { ranges: stats.proxyRanges - before.proxyRanges, bytes: stats.proxyBytes - before.proxyBytes,
                refused: stats.proxyRefused - before.proxyRefused,
                items: Object.fromEntries(Object.entries(stats.items).map(([k, v]) => [k, v - (before.items[k] || 0)])) };
  res.secs = Math.round((Date.now() - t0) / 1000);
  res.pass = res.md5 === recipe.file_md5 && res.bytes === recipe.size;
  allOk = allOk && res.pass;
  fs.writeFileSync(path.join(OUT, `e2e-${L}.json`), JSON.stringify(res, null, 1));
  console.log(JSON.stringify({ lang: L, mode: MODE, pass: res.pass, status: String(status).slice(0, 100), bytes: res.bytes,
                               md5: res.md5, expected: recipe.file_md5, secs: res.secs, proxy: res.proxy, langOption: shown.langOption }));
  await ctx.close();
}
await browser.close();
page.close(); proxy.close();
process.exit(allOk ? 0 : 1);
