// The library page (web/library/index.html) in headless Chromium, for
// tests/test_library_page.py: node library_page.mjs SITE_DIR
//
// SITE_DIR holds library/index.html and library/catalog.json (the test
// writes it from the real cloud/region-places.tsv); this serves it on
// localhost with /drive/preview-config.js naming a stand-in build worker on
// 127.0.0.1, whose origin replaces https://merged.invalid in catalog.json.
// The stand-in answers HEAD and GET under /merged/: 503 for a path with
// "/iceland/" or "/hawaii-viewer/", else 200 (GET: a few bytes as an attachment);
// files under /fallback/ stand in for archive.org's. Prints one JSON
// object: {scenario: what the page showed}, plus pageErrors.
// PLAYWRIGHT_CORE, CHROME_PATH, BROWSER_LIBS: as tools/langpack/builder_e2e.mjs.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const SITE = path.resolve(process.argv[2]);
const merged = [];
const worker = http.createServer((req, res) => {
  merged.push(`${req.method} ${req.url}`);
  const head = { 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store' };
  if (req.url.includes('/iceland/') || req.url.includes('/hawaii-viewer/')) { res.writeHead(503, { ...head, 'Content-Type': 'text/plain' }); res.end('over budget\n'); return; }
  res.writeHead(200, { ...head, 'Content-Type': 'application/octet-stream', 'Content-Length': 4,
                       'Content-Disposition': 'attachment; filename="x.zim"' });
  res.end(req.method === 'HEAD' ? undefined : 'ZIM!');
});
await new Promise((r) => worker.listen(0, '127.0.0.1', r));
const MERGED = `http://127.0.0.1:${worker.address().port}`;
const site = http.createServer((req, res) => {
  const p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  if (p === '/drive/preview-config.js') {
    res.writeHead(200, { 'Content-Type': 'text/javascript' });
    res.end(`window.STREETZIM_MERGED_ORIGIN = ${JSON.stringify(MERGED)};\n`);
    return;
  }
  const f = path.join(SITE, p.endsWith('/') ? p + 'index.html' : p);
  if (!f.startsWith(SITE + path.sep) || !fs.existsSync(f)) { res.writeHead(404, { 'Content-Type': 'text/plain' }); res.end('not here\n'); return; }
  let body = fs.readFileSync(f);
  if (f.endsWith('catalog.json')) body = Buffer.from(body.toString('utf8').split('https://merged.invalid').join(MERGED));
  res.writeHead(200, { 'Content-Type': f.endsWith('.html') ? 'text/html; charset=utf-8' : f.endsWith('.json') ? 'application/json' : 'application/octet-stream' });
  res.end(body);
});
await new Promise((r) => site.listen(0, 'localhost', r));
const PAGE = `http://localhost:${site.address().port}/library/`;
const PHONE = 'Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0';

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true });
const out = { pageErrors: [] };
async function open(hash, o = {}) {
  const ctx = await browser.newContext({ locale: o.locale || 'en-US', acceptDownloads: true,
    ...(o.ua ? { userAgent: o.ua, isMobile: true, hasTouch: true } : {}), viewport: o.viewport || { width: 1280, height: 900 } });
  const pg = await ctx.newPage();
  pg.on('pageerror', (e) => out.pageErrors.push(`${hash}: ${e.message}`));
  await pg.goto(PAGE + hash);
  await pg.waitForFunction(() => /\d+ maps?$|could not be loaded/.test(document.getElementById('count').textContent), null, { timeout: 30000 });
  return { ctx, pg };
}
const show = (pg) => pg.evaluate(() => {
  const opts = (id) => [...document.getElementById(id).options].slice(1).map((o) => o.value);
  const val = (id) => document.getElementById(id).value;
  return {
    count: document.getElementById('count').textContent, hash: decodeURIComponent(location.hash),
    titles: [...document.querySelectorAll('.kx-title')].map((t) => t.firstChild.textContent),
    bars: [...document.querySelectorAll('.kx-download')].map((a) => [a.closest('.kx-book').querySelector('.kx-title').firstChild.textContent,
      a.textContent, a.getAttribute('href'), a.getAttribute('aria-label')]),
    options: { continent: opts('f-continent'), region: opts('f-region'), country: opts('f-country'), area: opts('f-sub') },
    values: { lang: val('f-lang'), continent: val('f-continent'), region: val('f-region'), country: val('f-country'), area: val('f-sub') },
    langhint: document.getElementById('langhint').hidden ? null : document.getElementById('langhint').textContent,
    places: document.getElementById('f-places-text').textContent,
    visibleSelects: [...document.querySelectorAll('select')].filter((s) => s.offsetParent).map((s) => s.id),
    scrollWidth: document.documentElement.scrollWidth, width: innerWidth,
  };
});
async function scenario(name, hash, o, act) {
  const { ctx, pg } = await open(hash, o);
  try { out[name] = act ? await act(pg) : await show(pg); } finally { await ctx.close(); }
}
const pick = async (pg, id, v) => { await pg.selectOption('#' + id, v); await pg.waitForTimeout(50); };
const E = encodeURIComponent;

await scenario('eastern-asia', `#lang=en&continent=Asia&region=${E('Eastern Asia')}`, {}, async (pg) => {
  const before = await show(pg); await pick(pg, 'f-country', 'China'); return { before, china: await show(pg) };
});
await scenario('western-asia', `#lang=en&continent=Asia&region=${E('Western Asia')}`);
await scenario('iran', `#lang=en&continent=Asia&region=${E('Southern Asia')}&country=Iran`);
await scenario('caribbean', `#lang=en&continent=${E('North America')}&region=Caribbean`);
await scenario('micronesia', `#lang=en&continent=Oceania&region=Micronesia`);
await scenario('czechia', `#lang=en&continent=Europe&region=${E('Eastern Europe')}&country=Czechia`);
await scenario('hawaii', `#lang=en&country=${E('United States')}&area=Hawaii`);
await scenario('florida', `#lang=en&country=${E('United States')}&area=Florida`);
await scenario('taipei', `#lang=en&country=Taiwan&area=Taipei`);
await scenario('madagascar', '#lang=all&q=madagascar');
await scenario('rank', '#lang=all&q=switzerland');
await scenario('reset', `#lang=en&continent=Asia&region=${E('Eastern Asia')}&country=China`, {}, async (pg) => {
  await pick(pg, 'f-continent', 'Europe'); return show(pg);
});
await scenario('malformed', '#q=%E0%A4&lang=en', {}, async (pg) => {
  const first = await show(pg); await pick(pg, 'f-continent', 'Europe'); return { first, after: await show(pg) };
});
await scenario('lang-all', '#lang=all&country=Japan', { locale: 'de-DE' }, async (pg) => {
  const first = await show(pg); await pg.fill('#f-q', 'tokyo'); await pg.waitForTimeout(400); return { first, after: await show(pg) };
});
await scenario('german', '', { locale: 'de-DE' });
await scenario('desktop-de', '#lang=de');
// maps with today's viewer (the worker's file): Taipei's worker answers,
// Hawaii's does not (archive.org's file, here /fallback/, instead)
await scenario('fresh', '#lang=all', {}, async (pg) => {
  const bar = (title) => pg.locator('.kx-book', { has: pg.locator('.kx-title', { hasText: new RegExp('^' + title + '$') }) }).locator('.kx-download').first();
  let dlP = pg.waitForEvent('download', { timeout: 15000 });
  await bar('Taipei').click();
  const taipei = (await dlP).url();
  await pg.waitForTimeout(300);
  dlP = pg.waitForEvent('download', { timeout: 15000 });
  await bar('Hawaii').click();
  const hawaii = (await dlP).url();
  return { taipei, hawaii, bars: (await show(pg)).bars.filter((b) => b[0] === 'Taipei' || b[0] === 'Hawaii') };
});
await scenario('phone', '#lang=de', { ua: PHONE, viewport: { width: 360, height: 780 } }, async (pg) => {
  const first = await show(pg);
  await pg.tap('#f-places'); await pg.waitForTimeout(100);
  const opened = (await show(pg)).visibleSelects;
  const bar = (title) => pg.locator('.kx-book', { has: pg.locator('.kx-title', { hasText: title }) }).locator('.kx-download').first();
  const dlP = pg.waitForEvent('download', { timeout: 15000 });
  await bar('Greece').tap();
  const dl = await dlP;
  const greece = dl.url();
  await pg.waitForTimeout(300);
  await Promise.all([pg.waitForURL(/\/build\//, { timeout: 15000 }), bar('Iceland').tap()]);
  return { first, opened, greece, iceland: pg.url() };
});
out.merged = merged;
console.log(JSON.stringify(out));
await browser.close();
site.close(); worker.close();
