// The viewer's MapLibre loader (part 000, between its BEGIN/END
// maplibre-loader markers) in headless Chromium, for
// tests/test_maplibre_loader.py: node maplibre_loader_page.mjs SITE_DIR
//
// SITE_DIR holds one directory per map layout, each with an index.html (the
// loader, then a map whose only layer is a GeoJSON circle: drawing it needs
// MapLibre's worker) and the MapLibre files of that layout, plus frame.html
// (new/index.html in a sandboxed iframe: an opaque origin). Served on
// 127.0.0.1; files under /csp/ carry a CSP that forbids eval. Prints one JSON
// object: {scenario: {v: MapLibre version, n: features drawn, err}}.
// PLAYWRIGHT_CORE, CHROME_PATH, BROWSER_LIBS: as tools/langpack/builder_e2e.mjs.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const SITE = path.resolve(process.argv[2]);
const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '');
  const file = path.join(SITE, rel);
  if (!file.startsWith(SITE + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404); res.end(); return;
  }
  const headers = { 'Content-Type': file.endsWith('.html') ? 'text/html; charset=utf-8' : 'text/javascript; charset=utf-8' };
  if (rel.startsWith('csp/')) headers['Content-Security-Policy'] = "script-src 'self' 'unsafe-inline' blob:; worker-src blob:";
  res.writeHead(200, headers);
  res.end(fs.readFileSync(file));
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true,
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist',
         '--allow-file-access-from-files'] });

async function run(url, { noWebgl2 = false, frame = null } = {}) {
  const ctx = await browser.newContext();
  if (noWebgl2) {
    await ctx.addInitScript(() => {
      const orig = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function (t, ...a) {
        return t === 'webgl2' ? null : orig.call(this, t, ...a);
      };
    });
  }
  const page = await ctx.newPage();
  try {
    await page.goto(url);
    let target = page;
    if (frame) {
      await page.waitForFunction((f) => document.querySelector('iframe') && true, frame, { timeout: 10000 });
      for (let i = 0; i < 100 && !(target = page.frames().find((f) => f.url().includes(frame))); i++) {
        await page.waitForTimeout(100);
      }
    }
    await target.waitForFunction(() => window.__done || window.__err, null, { timeout: 45000 });
    return await target.evaluate(() => window.__done || { err: window.__err });
  } catch (e) {
    return { err: String(e && e.message || e).slice(0, 200) };
  } finally {
    await ctx.close();
  }
}

const out = {
  'new': await run(`${base}/new/index.html`),
  'new, no WebGL2': await run(`${base}/new/index.html`, { noWebgl2: true }),
  'old': await run(`${base}/old/index.html`),
  'old, no WebGL2': await run(`${base}/old/index.html`, { noWebgl2: true }),
  'broken 6': await run(`${base}/broken/index.html`),
  'CSP without eval': await run(`${base}/csp/index.html`),
  'sandboxed iframe': await run(`${base}/frame.html`, { frame: 'new/index.html' }),
  'file:': await run(pathToFileURL(path.join(SITE, 'new', 'index.html')).href),
};
await browser.close();
server.close();
console.log(JSON.stringify(out));
