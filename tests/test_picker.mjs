// The /drive/ picker's flows in Chromium, against a synthetic ZIM served
// by scripts/serve-web-local.py (docs/mobile-browser-review.md §A4, §A5,
// §B5):
//   - a local pick is validated, checked against the storage estimate,
//     written to IndexedDB from the page, opened by the worker, and the
//     page moves on to the viewer;
//   - a pick bigger than the free quota is refused before anything is
//     written;
//   - ?zim= does not auto-stream under Data Saver, but Stream still does;
//   - an installed (standalone) launch forwards to the viewer when a ZIM
//     is loaded, and ?picker=1 keeps the picker;
//   - the viewer's "could not open" bounce reason is shown once.
//
//   node tests/test_picker.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
import { buildZim, pattern } from './lib/synthetic_zim.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = Number(process.env.SITE_PORT || 8792);

function findChrome() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const base = process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers';
  try {
    for (const d of fs.readdirSync(base)) {
      const p = path.join(base, d, 'chrome-linux', 'chrome');
      if (/^chromium-\d+$/.test(d) && fs.existsSync(p)) return p;
    }
  } catch (e) {}
  return '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
}
function waitForPort(port, ms = 15000) {
  const t0 = Date.now();
  return new Promise((resolve, reject) => {
    (function tick() {
      const s = net.connect(port, '127.0.0.1');
      s.once('connect', () => { s.destroy(); resolve(); });
      s.once('error', () => { s.destroy(); Date.now() - t0 > ms ? reject(new Error('port ' + port)) : setTimeout(tick, 150); });
    })();
  });
}

async function main() {
  const entries = [
    { path: 'map-config.json', mime: 'application/json', cluster: 0,
      data: Buffer.from('{"name":"synthetic","center":[-77.03,38.9],"zoom":10,"minZoom":8,"maxZoom":14,"buildDate":"2026-09-18"}') },
    { path: 'small/a.bin', mime: 'application/octet-stream', data: pattern(3000, 1), cluster: 0 },
  ];
  const zim = buildZim(entries, [{ extended: false }]);
  // A perfectly valid ZIM that is not a map: must be refused before storing.
  const notMap = buildZim([{ path: 'A/index.html', mime: 'text/html', data: Buffer.from('<p>hi</p>'), cluster: 0 }], [{ extended: false }]);
  const siteDir = fs.mkdtempSync(path.join(os.tmpdir(), 'szpick-'));
  fs.cpSync(path.join(ROOT, 'web', 'drive'), path.join(siteDir, 'drive'), { recursive: true });
  fs.writeFileSync(path.join(siteDir, 'drive', 'preview-config.js'), 'window.STREETZIM_PREVIEW_PROXY = "";\n');
  const zimPath = path.join(siteDir, 'synthetic.zim');
  fs.writeFileSync(zimPath, zim);
  const notMapPath = path.join(siteDir, 'notmap.zim');
  fs.writeFileSync(notMapPath, notMap);
  // A server left behind by an earlier run would answer with stale files.
  await new Promise((resolve, reject) => {
    const probe = net.connect(PORT, '127.0.0.1');
    probe.once('connect', () => { probe.destroy(); reject(new Error('port ' + PORT + ' is already in use: stop that server or set SITE_PORT')); });
    probe.once('error', () => { probe.destroy(); resolve(); });
  });
  const site = spawn('python3', [path.join(ROOT, 'scripts/serve-web-local.py'), String(PORT), siteDir], { stdio: 'ignore' });
  const origin = 'http://127.0.0.1:' + PORT;
  let failures = 0;
  const check = (name, fn) => Promise.resolve().then(fn).then(
    () => console.log('  [PASS] ' + name),
    (e) => { failures++; console.log('  [FAIL] ' + name + ' — ' + (e && e.stack || e)); });
  const browser = await puppeteer.launch({
    headless: process.env.HEADFUL !== '1',
    executablePath: findChrome(),
    args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
    protocolTimeout: 300_000,
  });
  // Each scenario gets its own context: fresh worker, IndexedDB, storage.
  async function scenario(name, init, fn) {
    const context = await browser.createBrowserContext();
    const page = await context.newPage();
    page.on('pageerror', (err) => { failures++; console.log('  [FAIL] pageerror in ' + name + ': ' + err.message + ' @ ' + page.url()); });
    if (init) await page.evaluateOnNewDocument(init);
    await check(name, () => fn(page));
    await context.close();
  }
  const status = (page) => page.evaluate(() => ({
    title: document.getElementById('status-title').textContent,
    msg: document.getElementById('status').textContent,
    url: location.pathname + location.search,
  }));
  const ready = async (page, q) => {
    await page.goto(origin + '/drive/' + (q || ''), { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => !!navigator.serviceWorker.controller, { timeout: 15000 })
      .catch(async () => { await page.reload({ waitUntil: 'domcontentloaded' }); });
    await page.waitForFunction(() => !!navigator.serviceWorker.controller, { timeout: 15000 });
    await page.waitForFunction(() => !/Checking|Initializing|Starting up/.test(document.getElementById('status-title').textContent), { timeout: 20000 });
  };
  const ask = (page, msg) => page.evaluate((m) => new Promise((resolve) => {
    const ch = new MessageChannel();
    ch.port1.onmessage = (e) => resolve(e.data);
    navigator.serviceWorker.controller.postMessage(m, [ch.port2]);
  }), msg);
  // The picker's own path on every mobile browser: no showOpenFilePicker
  // (the scenarios below delete it — headless Chromium's never settles),
  // so the hidden <input type=file> is armed; uploadFile then fires its
  // change event.
  const noFsAccess = 'delete window.showOpenFilePicker;';
  const pick = async (page, file) => {
    await page.evaluate(() => document.getElementById('pick-btn').click());
    await page.waitForFunction(() => typeof document.getElementById('file-fallback').onchange === 'function', { timeout: 10000 });
    await (await page.$('#file-fallback')).uploadFile(file || zimPath);
  };

  try {
    await waitForPort(PORT);
    console.log('picker at ' + origin + '/drive/');

    await scenario('local pick: validated, stored from the page, opened, on to the viewer', noFsAccess, async (page) => {
      await ready(page);
      assert.equal((await status(page)).title, 'No ZIM loaded');
      await pick(page);
      await page.waitForFunction(() => document.getElementById('status-title').textContent === 'ZIM loaded', { timeout: 60000 });
      const st = await status(page);
      assert.match(st.msg, /synthetic\.zim/);
      await page.waitForFunction(() => location.pathname === '/drive/viewer/', { timeout: 15000 });
      await page.waitForFunction(() => !!navigator.serviceWorker.controller, { timeout: 15000 });
      const r = await ask(page, { type: 'status' });
      assert.equal(r.source, 'file');
      assert.equal(r.name, 'synthetic.zim');
      assert.equal(r.loaded, true);
    });

    await scenario('a pick bigger than the free quota is refused before writing', noFsAccess + `
      Object.defineProperty(navigator, 'storage', { value: {
        estimate: () => Promise.resolve({ quota: 4096, usage: 1024 }),
        persist: () => Promise.resolve(false),
      } });`, async (page) => {
      await ready(page);
      await pick(page);
      await page.waitForFunction(() => /Not enough storage/.test(document.getElementById('status-title').textContent), { timeout: 30000 });
      const st = await status(page);
      assert.match(st.msg, /reports it can store only about 0 MB more/);
      assert.equal(await page.$eval('#pick-btn', (b) => b.disabled), false, 'buttons re-enabled');
      assert.equal(st.url, '/drive/');
      const r = await ask(page, { type: 'status' });
      assert.equal(r.present, false, 'nothing must have been stored');
    });

    await scenario('?zim= under Data Saver waits for Stream; Stream then works',
      "Object.defineProperty(navigator, 'connection', { value: { saveData: true, effectiveType: '3g' } });",
      async (page) => {
      await ready(page, '?zim=' + encodeURIComponent(origin + '/synthetic.zim'));
      await page.waitForFunction(() => /Data Saver/.test(document.getElementById('status-title').textContent), { timeout: 20000 });
      await new Promise((r) => setTimeout(r, 1500));
      const st = await status(page);
      assert.equal(st.url.split('?')[0], '/drive/', 'must not have moved on');
      assert.equal(await page.$eval('#url-input', (i) => i.value), origin + '/synthetic.zim');
      let r = await ask(page, { type: 'status' });
      assert.equal(r.present, false, 'nothing streamed yet');
      await page.click('#url-form button[type=submit]');
      await page.waitForFunction(() => location.pathname === '/drive/viewer/', { timeout: 30000 });
      await page.waitForFunction(() => !!navigator.serviceWorker.controller, { timeout: 15000 });
      r = await ask(page, { type: 'status' });
      assert.equal(r.source, 'url');
    });

    await scenario('standalone launch forwards to a loaded map; ?picker=1 stays',
      "Object.defineProperty(navigator, 'standalone', { value: true });", async (page) => {
      await ready(page, '?picker=1');
      const set = await ask(page, { type: 'set-zim', url: origin + '/synthetic.zim', name: 'synthetic.zim' });
      assert.ok(set.ok, JSON.stringify(set));
      await page.goto(origin + '/drive/?picker=1', { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => document.getElementById('status-title').textContent.indexOf('Streaming from') === 0, { timeout: 20000 });
      await new Promise((r) => setTimeout(r, 1200));
      assert.equal(await page.evaluate(() => location.pathname), '/drive/');
      await page.goto(origin + '/drive/', { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => location.pathname === '/drive/viewer/', { timeout: 20000 });
    });

    await scenario('a ZIM without map-config.json is refused before anything is stored', noFsAccess, async (page) => {
      await ready(page);
      await pick(page, notMapPath);
      await page.waitForFunction(() => document.getElementById('status-title').textContent === 'Failed to open ZIM', { timeout: 30000 })
        .catch(async (e) => { throw new Error(e.message + ' — ' + JSON.stringify(await status(page))); });
      const st = await status(page);
      assert.match(st.msg, /not a StreetZim map/);
      const r = await ask(page, { type: 'status' });
      assert.equal(r.present, false);
      const set = await ask(page, { type: 'set-zim', url: origin + '/notmap.zim', name: 'notmap.zim' });
      assert.equal(set.ok, false);
      assert.match(set.error, /not a StreetZim map/);
    });

    await scenario('standalone: a bounce reason suppresses the forward and hides Open viewer',
      "Object.defineProperty(navigator, 'standalone', { value: true });", async (page) => {
      await ready(page, '?picker=1');
      const set = await ask(page, { type: 'set-zim', url: origin + '/synthetic.zim', name: 'synthetic.zim' });
      assert.ok(set.ok, JSON.stringify(set));
      await page.evaluate(() => sessionStorage.setItem('streetzim_redirect_reason', 'zim-error'));
      await page.goto(origin + '/drive/', { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => /could not be opened/.test(document.getElementById('status-title').textContent), { timeout: 20000 });
      await new Promise((r) => setTimeout(r, 1200));
      assert.equal(await page.evaluate(() => location.pathname), '/drive/');
      assert.equal(await page.$eval('#open-btn', (b) => b.style.display), 'none');
      assert.equal(await page.$eval('#clear-btn', (b) => b.style.display), '');
    });

    await scenario('the viewer\'s "could not open" bounce reason is shown once', null, async (page) => {
      await ready(page);
      await page.evaluate(() => sessionStorage.setItem('streetzim_redirect_reason', 'zim-error'));
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => /could not be opened/.test(document.getElementById('status-title').textContent), { timeout: 20000 });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => document.getElementById('status-title').textContent === 'No ZIM loaded', { timeout: 20000 });
      assert.equal(await page.evaluate(() => sessionStorage.getItem('streetzim_redirect_reason')), null);
    });
  } finally {
    await browser.close().catch(() => {});
    site.kill();
    fs.rmSync(siteDir, { recursive: true, force: true });
  }
  console.log(failures ? '\n' + failures + ' FAILED' : '\nall passed');
  process.exit(failures ? 1 : 0);
}
main().catch((e) => { console.error(e); process.exit(1); });
