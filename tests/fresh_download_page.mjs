// The main page's Download of a map with today's viewer (web/template.html,
// docs/fresh-viewer.md), for tests/test_fresh_download_page.py: the page's own
// script, cut from the template, on a page with two such links, against a
// stand-in build worker on 127.0.0.1 that serves one map and answers 503 for
// the other ("/hawaii-viewer/"). Prints one JSON object: the URL each click
// downloaded from and the worker's requests.
//   node fresh_download_page.mjs TEMPLATE_HTML
import http from 'node:http';
import fs from 'node:fs';

if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const tpl = fs.readFileSync(process.argv[2], 'utf8');
const at = tpl.indexOf("a[data-fallback]");
const script = tpl.slice(tpl.lastIndexOf('<script>', at), tpl.indexOf('</script>', at) + '</script>'.length);
const requests = [];
const worker = http.createServer((req, res) => {
  requests.push(`${req.method} ${req.url}`);
  const h = { 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store' };
  if (req.url.includes('/hawaii-viewer/')) { res.writeHead(503, { ...h, 'Content-Type': 'text/plain' }); res.end('over budget\n'); return; }
  res.writeHead(200, { ...h, 'Content-Type': 'application/octet-stream', 'Content-Length': 4, 'Content-Disposition': 'attachment; filename="x.zim"' });
  res.end(req.method === 'HEAD' ? undefined : 'ZIM!');
});
await new Promise((r) => worker.listen(0, '127.0.0.1', r));
const W = `http://127.0.0.1:${worker.address().port}`;
const uuid = '34353f40-ff08-5e39-8495-a0a3c5bcd50d';
const page = `<!doctype html><meta charset="utf-8"><body>
<a id="taipei" class="btn btn-primary" href="${W}/merged/taipei-viewer/en/${uuid}/osm-taipei.v5590ad0.zim" data-fallback="/fallback/osm-taipei.zim" data-track="download">Download</a>
<a id="hawaii" class="btn btn-primary" href="${W}/merged/hawaii-viewer/en/${uuid}/osm-hawaii.v5590ad0.zim" data-fallback="/fallback/osm-hawaii.zim" data-track="download">Download</a>
${script}</body>`;
const site = http.createServer((req, res) => {
  if (req.url.startsWith('/fallback/')) {
    res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment' });
    res.end('ZIM archive.org stand-in');
    return;
  }
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(page);
});
await new Promise((r) => site.listen(0, 'localhost', r));
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true });
const ctx = await browser.newContext({ acceptDownloads: true });
const pg = await ctx.newPage();
const errors = [];
pg.on('pageerror', (e) => errors.push(e.message));
await pg.goto(`http://localhost:${site.address().port}/`);
const out = { errors };
let dl = pg.waitForEvent('download', { timeout: 15000 });
await pg.click('#taipei');
await pg.click('#taipei');                 // a second click while the first is checked
out.taipei = (await dl).url();
await pg.waitForTimeout(500);
dl = pg.waitForEvent('download', { timeout: 15000 });
await pg.click('#hawaii');
out.hawaii = (await dl).url();
out.requests = requests;
console.log(JSON.stringify(out));
await browser.close();
site.close(); worker.close();
