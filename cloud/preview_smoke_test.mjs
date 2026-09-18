// Online-preview smoke test for streetzim.web.app/drive/.
//
// Drives the /drive/ picker with ?zim=<url> through headless Chrome and
// checks that the viewer really renders a ZIM streamed by HTTP range
// requests through the service worker (docs/online-preview.md). Two
// scenarios:
//
//   same-origin  ?zim=http://127.0.0.1:<port>/<zim> — no proxy involved;
//                scripts/serve-web-local.py answers Range itself. Exercises
//                the SW + reader path on a local copy of the ZIM.
//   proxy        ?zim=https://archive.org/download/<item>/<file>, with the
//                served preview-config.js pointed at
//                preview-proxy/serve-local.mjs — real archive.org bytes over
//                the cross-origin CORS path a deployed site uses.
//
// Usage:
//   ZIM_FILE=/path/osm-washington-dc-2026-09-08.zim \
//   IA_ZIM=https://archive.org/download/streetzim-washington-dc/osm-washington-dc-2026-09-08.zim \
//   CHROME_PATH=... SHOT_DIR=/tmp node cloud/preview_smoke_test.mjs [same-origin|proxy|all]
//
// ZIM_FILE is needed for same-origin, IA_ZIM (plus network) for proxy;
// SHOT_DIR saves a screenshot of the rendered map per scenario. With
// SITE_URL set (e.g. https://streetzim.web.app, or the Firebase Hosting
// emulator) no local servers are started: the picker at SITE_URL/drive/
// is driven as deployed, with whatever preview-config.js it serves —
// PROXY_BASE is then only reported. H2=1
// runs the local proxy as HTTPS/HTTP-2 on a throw-away self-signed
// certificate (needs openssl), which is what a deployed worker speaks
// and what the viewer's parallel lookups are tuned for — over plain
// HTTP/1.1 Chrome caps a host at 6 connections and timings come out
// pessimistic. The
// picker page, the redirect into the viewer, the routing API, tiles
// served through the SW, the preview banner and the SW's own status
// message are all asserted; any console error or /drive/ request
// failure fails the run, as in pwa_smoke_test.mjs.

import puppeteer, { KnownDevices } from 'puppeteer';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ZIM_FILE = process.env.ZIM_FILE || '';
const IA_ZIM = process.env.IA_ZIM ||
  'https://archive.org/download/streetzim-washington-dc/osm-washington-dc-2026-09-08.zim';
const CHROME_PATH = process.env.CHROME_PATH ||
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const HEADFUL = process.env.HEADFUL === '1';
const SITE_PORT = Number(process.env.SITE_PORT || 8765);
const PROXY_PORT = Number(process.env.PROXY_PORT || 8766);
const SHOT_DIR = process.env.SHOT_DIR || '';
const H2 = process.env.H2 === '1';
const SITE_URL = (process.env.SITE_URL || '').replace(/\/+$/, '');
// Extra Chrome flags, whitespace-separated — e.g. a corporate proxy:
// CHROME_ARGS="--proxy-server=http://127.0.0.1:3128 --ignore-certificate-errors"
const CHROME_ARGS = (process.env.CHROME_ARGS || '').split(/\s+/).filter(Boolean);
// Phone emulation (viewport, pixel ratio, touch, user agent) from
// Puppeteer's device list, e.g. DEVICE="iPhone 13" or DEVICE="Pixel 5".
// Layout and touch only — the engine is still Chromium.
const DEVICE = process.env.DEVICE || '';
const which = process.argv[2] || 'all';

// Optional files the viewer probes for and copes without: the Q-ID →
// title bridge lives on the deployed origin, not in the repo, so a local
// run 404s it; wiki-geo-index.json only exists in ZIMs built with
// bundled Wikipedia articles (older continent builds lack it).
const OPTIONAL_WEB_FILES = ['/drive/wiki-qid-titles.json', '/drive/viewer/wiki-geo-index.json'];

const failures = [];
function fail(label, detail) {
  failures.push(label + ': ' + detail);
  console.log('  [FAIL] ' + label + ' — ' + detail);
}
function pass(label, detail) {
  console.log('  [PASS] ' + label + (detail ? ' — ' + detail : ''));
}

function waitForPort(port, ms = 20000) {
  const t0 = Date.now();
  return new Promise((resolve, reject) => {
    (function tick() {
      const sock = net.connect({ host: '127.0.0.1', port }, () => { sock.destroy(); resolve(); });
      sock.on('error', () => {
        sock.destroy();
        if (Date.now() - t0 > ms) reject(new Error('port ' + port + ' never came up'));
        else setTimeout(tick, 150);
      });
    })();
  });
}

function waitUntil(fn, ms, what) {
  const t0 = Date.now();
  return new Promise((resolve, reject) => {
    (function tick() {
      if (fn()) return resolve();
      if (Date.now() - t0 > ms) return reject(new Error('timed out waiting for ' + what));
      setTimeout(tick, 200);
    })();
  });
}

function writePreviewConfig(siteDir, proxyBase) {
  fs.writeFileSync(path.join(siteDir, 'drive', 'preview-config.js'),
    "window.STREETZIM_PREVIEW_PROXY = " + JSON.stringify(proxyBase) + ";\n");
}

async function runScenario(browser, siteDir, name, zimParam, proxyBase, expectSource) {
  console.log('\n[' + name + '] ' + zimParam + (proxyBase ? '  via ' + proxyBase +
    (SITE_URL ? '' : (H2 ? ' (HTTP/2)' : ' (HTTP/1.1)')) : ''));
  if (!SITE_URL) writePreviewConfig(siteDir, proxyBase);
  // A fresh context = fresh service-worker registry, IndexedDB and HTTP
  // cache, so scenarios cannot leak into each other.
  const context = await browser.createBrowserContext();
  const page = await context.newPage();
  page.setDefaultNavigationTimeout(90_000);
  if (DEVICE) {
    const dev = KnownDevices[DEVICE];
    if (!dev) throw new Error('unknown DEVICE "' + DEVICE + '" — try "iPhone 13" or "Pixel 5"');
    await page.emulate(dev);
  }
  const tag = DEVICE ? '-' + DEVICE.replace(/\s+/g, '_') : '';
  const errors = [];
  const tileStatuses = [];
  const zimErrors = [];
  const notFound = [];
  page.on('pageerror', (err) => errors.push('pageerror: ' + err.message));
  page.on('console', (m) => {
    const type = m.type();
    if (type === 'error') errors.push('console: ' + m.text());
    else if (type === 'warn' || type === 'warning') console.log('  · warn: ' + m.text().slice(0, 200));
  });
  page.on('response', (resp) => {
    const u = resp.url();
    if (u.includes('/drive/viewer/tiles/')) tileStatuses.push(resp.status());
    if (u.includes('/drive/viewer/') && resp.status() >= 500) zimErrors.push(resp.status() + ' ' + u);
    // Name every 404 so a console "Failed to load resource" line (which
    // carries no URL) can be matched to what was asked for.
    if (resp.status() === 404) {
      notFound.push(u);
      const optional = OPTIONAL_WEB_FILES.some((f) => u.includes(f));
      console.log('  ' + (optional ? '· optional' : '!') + ' 404 ' + u +
        (resp.fromServiceWorker() ? ' (via SW)' : ' (network)'));
    }
  });
  page.on('requestfailed', (req) => {
    const f = req.failure();
    if (!req.url().includes('/drive/')) return;
    if (f && f.errorText === 'net::ERR_ABORTED') return;   // MapLibre cancelling stale tiles
    errors.push('reqfail: ' + req.url() + ' ' + (f && f.errorText));
  });

  const t0 = Date.now();
  const siteBase = SITE_URL || 'http://127.0.0.1:' + SITE_PORT;
  const pickerUrl = siteBase + '/drive/?zim=' + encodeURIComponent(zimParam);
  try {
    if (SHOT_DIR && DEVICE) {
      // The picker as a visitor sees it before choosing anything.
      await page.goto(siteBase + '/drive/', { waitUntil: 'networkidle0', timeout: 60_000 });
      await new Promise((r) => setTimeout(r, 1500));
      await page.screenshot({ path: path.join(SHOT_DIR, 'picker-' + name + tag + '.png') });
    }
    await page.goto(pickerUrl, { waitUntil: 'domcontentloaded' });
    // The picker registers the SW, opens the ZIM header over HTTP and
    // then redirects into the viewer. page.url() tracks that without
    // evaluating anything in a document that is about to go away.
    await waitUntil(() => /\/drive\/viewer\/?/.test(page.url()), 90_000, 'redirect into the viewer');
    await page.waitForFunction(() => document.readyState !== 'loading', { timeout: 30_000 });
  } catch (e) {
    let status = '?';
    try {
      status = await page.evaluate(() => (document.getElementById('status-title') || {}).textContent +
        ' / ' + (document.getElementById('status') || {}).textContent);
    } catch (_) {}
    fail(name + ' picker → viewer redirect', e.message + ' (picker status: ' + status + ')');
    await context.close();
    return;
  }
  const tRedirect = Date.now() - t0;
  if (!/\/drive\/viewer\/?/.test(page.url())) {
    fail(name + ' picker → viewer redirect', 'landed on ' + page.url());
    await context.close();
    return;
  }
  pass(name + ' picker → viewer redirect', tRedirect + ' ms');
  const ctl = await page.evaluate(() => ({
    controlled: !!(navigator.serviceWorker && navigator.serviceWorker.controller),
    href: location.href,
  })).catch(() => ({}));
  console.log('  · viewer page ' + JSON.stringify(ctl));

  try {
    await page.waitForFunction(
      () => window.streetzimRouting && typeof window.streetzimRouting.open === 'function',
      { timeout: 60_000 });
    pass(name + ' viewer API ready', (Date.now() - t0) + ' ms');
  } catch (e) {
    fail(name + ' viewer API ready', e.message);
  }

  // The search placeholder flips to "Search N places..." once
  // search-data/manifest.json has come through the SW — proof that ZIM
  // content is being read off the streamed file, not the shell.
  try {
    await page.waitForFunction(() => {
      const i = document.getElementById('search-input');
      const ph = (i && i.placeholder) || '';
      return /^Search [\d,.\s\u00a0\u202f]+ places/.test(ph) || ph === 'Search unavailable';
    }, { timeout: 90_000 });
    const ph = await page.evaluate(() => document.getElementById('search-input').placeholder);
    if (ph === 'Search unavailable') throw new Error('search manifest failed to load');
    pass(name + ' search index read from the streamed ZIM', ph + ' after ' + (Date.now() - t0) + ' ms');
  } catch (e) {
    fail(name + ' search index read from the streamed ZIM', e.message);
  }

  // Optional deeper checks over the streamed ZIM: a real search (the
  // prefix shards come through the SW) and the Wiki panel (the
  // Wikidata buckets, the largest blobs a ZIM has).
  if (process.env.SMOKE_SEARCH) {
    const term = process.env.SMOKE_SEARCH;
    const ts = Date.now();
    try {
      await page.click('#search-input');
      await page.type('#search-input', term, { delay: 30 });
      await page.waitForFunction(() => {
        const r = document.getElementById('search-results');
        if (!r) return false;
        if (r.querySelector('.search-no-results')) return true;
        return Array.from(r.querySelectorAll('.search-result'))
          .some((el) => el.textContent.trim() !== 'Searching…');
      }, { timeout: 120_000 });
      const res = await page.evaluate(() => {
        const r = document.getElementById('search-results');
        const rows = Array.from(r.querySelectorAll('.search-result'))
          .filter((el) => el.textContent.trim() !== 'Searching…');
        return { none: !!r.querySelector('.search-no-results'), n: rows.length,
                 first: rows[0] ? rows[0].textContent.trim().slice(0, 60) : '' };
      });
      if (res.none || res.n === 0) throw new Error('no results for "' + term + '"');
      pass(name + ' search "' + term + '" over the streamed shards',
        res.n + ' rows in ' + (Date.now() - ts) + ' ms — first: ' + res.first);
    } catch (e) {
      fail(name + ' search "' + term + '"', e.message);
    }
    await page.evaluate(() => {
      const i = document.getElementById('search-input');
      if (i) { i.value = ''; i.blur(); }
      const r = document.getElementById('search-results');
      if (r) r.style.display = 'none';
    });
  }
  if (process.env.SMOKE_WIKI === '1') {
    const tw = Date.now();
    try {
      await page.waitForFunction(() => {
        const b = document.getElementById('wiki-toggle');
        return b && getComputedStyle(b).display !== 'none';
      }, { timeout: 30_000 });
      await page.click('#wiki-toggle');
      await page.waitForFunction(() => {
        const l = document.getElementById('wiki-panel-list');
        return l && l.childElementCount > 0;
      }, { timeout: 120_000 });
      const w = await page.evaluate(() => ({
        n: document.getElementById('wiki-panel-list').childElementCount,
        count: (document.getElementById('wiki-panel-count') || {}).textContent || '',
        first: (document.getElementById('wiki-panel-list').firstElementChild.textContent || '').trim().slice(0, 60),
      }));
      pass(name + ' Wiki panel over the streamed Wikidata',
        w.n + ' entries in ' + (Date.now() - tw) + ' ms' + (w.count ? ' (' + w.count.trim() + ')' : '') + ' — first: ' + w.first);
    } catch (e) {
      fail(name + ' Wiki panel', e.message);
    }
  }

  // Ask the SW for its range-request tally while it is still busy with
  // the map (an idle SW is torn down after ~30 s and a fresh one would
  // report a fresh, near-empty reader).
  async function swStatus() {
    return page.evaluate(() => new Promise((resolve, reject) => {
      const ch = new MessageChannel();
      const t = setTimeout(() => reject(new Error('status timeout')), 10_000);
      ch.port1.onmessage = (e) => { clearTimeout(t); resolve(e.data); };
      navigator.serviceWorker.controller.postMessage({ type: 'status' }, [ch.port2]);
    }));
  }
  try {
    await new Promise((r) => setTimeout(r, 3000));      // first tiles land
    const status = await swStatus();
    if (!status || !status.ok || status.source !== 'url') throw new Error('status ' + JSON.stringify(status));
    const st = status.stats || { requests: 0, bytes: 0 };
    if (st.requests < 5 || st.bytes < 512 * 1024) {
      throw new Error('only ' + st.requests + ' range requests / ' + st.bytes + ' bytes — is the SW reading the ZIM?');
    }
    pass(name + ' SW is streaming the ZIM by range requests',
      status.name + ' · ' + status.info.sizeMB + ' MB · ' + st.requests + ' range requests, ' +
      (st.bytes / 1048576).toFixed(1) + ' MB fetched so far');
  } catch (e) {
    fail(name + ' SW is streaming the ZIM by range requests', e.message);
  }

  try {
    await page.waitForSelector('#preview-banner:not([hidden])', { timeout: 15_000 });
    const banner = await page.evaluate(() => ({
      text: document.getElementById('preview-banner').innerText.replace(/\s+/g, ' ').trim(),
      href: document.getElementById('preview-banner-download').href,
    }));
    if (banner.href !== expectSource) throw new Error('Download link is ' + banner.href + ', expected ' + expectSource);
    pass(name + ' preview banner', banner.text);
  } catch (e) {
    fail(name + ' preview banner', e.message);
  }

  // Let the map settle, then take the picture the user would see.
  await new Promise((r) => setTimeout(r, 4000));
  if (SHOT_DIR) {
    const shot = path.join(SHOT_DIR, 'preview-' + name + tag + '.png');
    await page.screenshot({ path: shot });
    console.log('  · screenshot ' + shot);
  }
  try {
    const status = await swStatus();
    const st = (status && status.stats) || { requests: 0, bytes: 0 };
    console.log('  · at the end: ' + st.requests + ' range requests, ' +
      (st.bytes / 1048576).toFixed(1) + ' MB fetched; ' + tileStatuses.length +
      ' tile responses seen on the page target');
  } catch (e) {}
  if (zimErrors.length) fail(name + ' ZIM responses', zimErrors.slice(0, 5).join('; '));
  // A "Failed to load resource ... 404" console line is only tolerated
  // when every 404 seen was one of the optional web-served files.
  const unexpected404 = notFound.filter((u) => !OPTIONAL_WEB_FILES.some((f) => u.includes(f)));
  const realErrors = errors.filter((e) =>
    !(unexpected404.length === 0 && /Failed to load resource.*404/.test(e)));
  if (unexpected404.length) fail(name + ' unexpected 404s', unexpected404.slice(0, 5).join('; '));
  if (realErrors.length) fail(name + ' console/network errors', realErrors.slice(0, 8).join(' | '));
  else pass(name + ' no console or /drive/ request errors');
  await context.close();
}

async function main() {
  const siteDir = fs.mkdtempSync(path.join(os.tmpdir(), 'szpreview-'));
  let site = null, proxy = null;
  let proxyBase = process.env.PROXY_BASE || '';
  if (!SITE_URL) {
    fs.cpSync(path.join(ROOT, 'web', 'drive'), path.join(siteDir, 'drive'), { recursive: true });
    if (ZIM_FILE) fs.symlinkSync(path.resolve(ZIM_FILE), path.join(siteDir, path.basename(ZIM_FILE)));
    writePreviewConfig(siteDir, '');

    site = spawn('python3', [path.join(ROOT, 'scripts/serve-web-local.py'), String(SITE_PORT), siteDir],
      { stdio: 'ignore' });
    const proxyEnv = { ...process.env };
    if (H2) {
      const cert = path.join(siteDir, 'proxy-cert.pem'), key = path.join(siteDir, 'proxy-key.pem');
      execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key,
        '-out', cert, '-days', '1', '-subj', '/CN=127.0.0.1',
        '-addext', 'subjectAltName=IP:127.0.0.1'], { stdio: 'ignore' });
      proxyEnv.H2_CERT = cert; proxyEnv.H2_KEY = key;
    }
    proxy = spawn(process.execPath, [path.join(ROOT, 'preview-proxy/serve-local.mjs'), String(PROXY_PORT)],
      { stdio: 'ignore', env: proxyEnv });
    proxyBase = (H2 ? 'https' : 'http') + '://127.0.0.1:' + PROXY_PORT;
  }
  let browser = null;
  try {
    if (!SITE_URL) {
      await waitForPort(SITE_PORT);
      await waitForPort(PROXY_PORT);
    }
    browser = await puppeteer.launch({
      headless: !HEADFUL,
      executablePath: CHROME_PATH,
      // SwiftShader keeps WebGL (MapLibre) alive on GPU-less runners.
      args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
             '--window-size=1200,800'].concat(H2 ? ['--ignore-certificate-errors'] : [], CHROME_ARGS),
      defaultViewport: { width: 1200, height: 800 },
      protocolTimeout: 300_000,
    });
    if (which === 'all' || which === 'same-origin') {
      if (SITE_URL) console.log('\n[same-origin] skipped: SITE_URL serves no local ZIM');
      else if (!ZIM_FILE) fail('same-origin', 'ZIM_FILE not set');
      else {
        const local = 'http://127.0.0.1:' + SITE_PORT + '/' + path.basename(ZIM_FILE);
        await runScenario(browser, siteDir, 'same-origin', local, '', local);
      }
    }
    if (which === 'all' || which === 'proxy') {
      await runScenario(browser, siteDir, 'proxy', IA_ZIM, proxyBase, IA_ZIM);
    }
  } catch (e) {
    fail('harness', e && e.stack || String(e));
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (site) site.kill();
    if (proxy) proxy.kill();
    fs.rmSync(siteDir, { recursive: true, force: true });
  }
  console.log('');
  if (failures.length) {
    console.log(failures.length + ' failure(s):');
    for (const f of failures) console.log('  - ' + f);
    process.exit(1);
  }
  console.log('all passed');
}

main();
