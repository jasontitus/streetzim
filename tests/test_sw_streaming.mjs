// The service worker's data path, end to end in Chromium: a synthetic ZIM
// with entries big enough to be streamed (docs/mobile-browser-review.md,
// finding A2) is loaded both as a URL (HTTP range source) and as a File
// (what the picker hands over), and /drive/viewer/<entry> is fetched from
// a controlled page with and without Range headers.
//
//   node tests/test_sw_streaming.mjs
//
// Checks: 200 with the exact bytes, 206 with Content-Range, 416 past the
// end, invalid ranges ignored, the 4 MiB streaming threshold on both
// 4- and 8-byte raw clusters, buffered small and zstd entries, the
// "Back to map" bar on articles, optional-probe 200s, concurrent reads,
// that streaming keeps the big entries out of the worker's caches, that
// a small entry sharing a raw cluster with a big one is read on its own,
// the 503 + X-Streetzim-Upstream + page notice while the origin is down
// and recovery when it is back, the viewer's own reaction to both (a
// "not answering" line with Try again instead of a bounce; a bounce to
// the picker when there is no ZIM), the preview banner's byte counter
// and ping, and the check-zim → page-side IndexedDB write → reload-zim
// hand-off the picker uses for local files.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
import { buildZim, pattern, haveZstd, fnv1a } from './lib/synthetic_zim.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = Number(process.env.SITE_PORT || 8791);
const HEADFUL = process.env.HEADFUL === '1';

function findChrome() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const base = process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers';
  try {
    for (const d of fs.readdirSync(base)) {
      if (!/^chromium-\d+$/.test(d)) continue;
      const p = path.join(base, d, 'chrome-linux', 'chrome');
      if (fs.existsSync(p)) return p;
    }
  } catch (e) {}
  return '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
}

function waitForPort(port, up, ms = 15000) {
  const t0 = Date.now();
  return new Promise((resolve, reject) => {
    (function tick() {
      const s = net.connect(port, '127.0.0.1');
      s.once('connect', () => { s.destroy(); up ? resolve() : retry(); });
      s.once('error', () => { s.destroy(); up ? retry() : resolve(); });
      function retry() {
        if (Date.now() - t0 > ms) reject(new Error('port ' + port + (up ? ' never came up' : ' never closed')));
        else setTimeout(tick, 150);
      }
    })();
  });
}

function startSite(siteDir) {
  return spawn('python3', [path.join(ROOT, 'scripts/serve-web-local.py'), String(PORT), siteDir],
    { stdio: 'ignore' });
}

const MiB = 1024 * 1024;

async function main() {
  // ---- the ZIM ----
  const BIG = pattern(8 * MiB, 1);            // raw, 4-byte offsets, streamed
  const CELLS = pattern(4 * MiB + 1, 2);      // raw, 8-byte offsets, just over the threshold
  const UNDER = pattern(4 * MiB - 1, 4);      // raw, just under: buffered
  const SMALL_A = pattern(3000, 11), SMALL_B = pattern(5000, 12), SMALL_C = pattern(7000, 13);
  const TILE = pattern(300 * 1024, 3);
  const ARTICLE = Buffer.from('<!DOCTYPE html><html><head><meta charset="utf-8"><title>Foo Bar</title></head>' +
    '<body><h1>Foo Bar</h1><p>Article body.</p></body></html>');
  const BIN = 'application/octet-stream';
  const zstdCluster = haveZstd ? 2 : 1;
  const entries = [
    { path: 'small/a.bin', mime: BIN, data: SMALL_A, cluster: 0 },
    { path: 'big/0.bin', mime: BIN, data: BIG, cluster: 0 },
    { path: 'small/b.bin', mime: BIN, data: SMALL_B, cluster: 0 },
    { path: 'big/cells.bin', mime: BIN, data: CELLS, cluster: 1 },
    { path: 'big/under.bin', mime: BIN, data: UNDER, cluster: 1 },
    { path: 'small/c.bin', mime: BIN, data: SMALL_C, cluster: 1 },
    { path: 'tiles/1/2/3.pbf', mime: 'application/x-protobuf', data: TILE, cluster: zstdCluster },
    { path: 'map-config.json', mime: 'application/json', cluster: zstdCluster,
      data: Buffer.from('{"name":"synthetic","center":[-77.03,38.9],"zoom":10,"minZoom":8,"maxZoom":14,' +
                        '"bounds":[-77.2,38.8,-76.9,39.0],"buildDate":"2026-09-18"}') },
    { path: 'wiki-article/Foo_Bar.html', mime: 'text/html', data: ARTICLE, cluster: zstdCluster },
  ];
  const specs = [{ extended: false }, { extended: true }];
  if (haveZstd) specs.push({ zstd: true });
  const zim = buildZim(entries, specs);

  const siteDir = fs.mkdtempSync(path.join(os.tmpdir(), 'szsw-'));
  fs.cpSync(path.join(ROOT, 'web', 'drive'), path.join(siteDir, 'drive'), { recursive: true });
  fs.writeFileSync(path.join(siteDir, 'drive', 'preview-config.js'), 'window.STREETZIM_PREVIEW_PROXY = "";\n');
  const zimPath = path.join(siteDir, 'synthetic.zim');
  fs.writeFileSync(zimPath, zim);
  console.log('synthetic ZIM: ' + zim.length + ' bytes, ' + entries.length + ' entries' +
    (haveZstd ? ' (with a zstd cluster)' : ' (no zstd in this Node)'));

  let site = startSite(siteDir);
  let failures = 0;
  const check = (name, fn) => Promise.resolve().then(fn).then(
    () => console.log('  [PASS] ' + name),
    (e) => { failures++; console.log('  [FAIL] ' + name + ' — ' + (e && e.stack || e)); });

  const browser = await puppeteer.launch({
    headless: !HEADFUL,
    executablePath: findChrome(),
    // SwiftShader keeps WebGL (the viewer's MapLibre) alive on GPU-less runners.
    args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
    protocolTimeout: 300_000,
  });
  // Service-worker console output and uncaught exceptions: the SW is its
  // own target, and Puppeteer does not relay worker console events, so
  // attach a CDP session. An uncaught rejection in the SW is a failure —
  // it is what turned a 503 into a bare network error on the page.
  let swExceptions = 0;
  browser.on('targetcreated', async (t) => {
    if (t.type() !== 'service_worker') return;
    try {
      const s = await t.createCDPSession();
      await s.send('Runtime.enable');
      s.on('Runtime.consoleAPICalled', (e) => {
        if (e.type !== 'error' && e.type !== 'warning') return;
        console.log('    [sw console] ' + e.type + ': ' + e.args.map((a) =>
          a.value !== undefined ? a.value : (a.description || a.type)).join(' ').split('\n')[0]);
      });
      s.on('Runtime.exceptionThrown', (e) => {
        swExceptions++;
        const d = e.exceptionDetails;
        console.log('    [sw exception] ' + (d.exception && d.exception.description || d.text).split('\n')[0] +
          ' (' + d.url + ':' + d.lineNumber + ')');
      });
    } catch (e) {}
  });
  try {
    await waitForPort(PORT, true);
    const page = await browser.newPage();
    page.on('pageerror', (err) => { failures++; console.log('  [FAIL] pageerror: ' + err.message); });
    page.on('console', (m) => {
      if (m.type() === 'error' || m.type() === 'warn' || m.type() === 'warning') {
        console.log('    [page console] ' + m.type() + ': ' + m.text());
      }
    });
    const origin = 'http://127.0.0.1:' + PORT;
    await page.goto(origin + '/drive/', { waitUntil: 'domcontentloaded' });
    await page.evaluate(() => navigator.serviceWorker.ready);
    // clients.claim() may take a tick; a reload is the sure way to be controlled.
    const controlled = await page.evaluate(() => !!navigator.serviceWorker.controller);
    if (!controlled) {
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.evaluate(() => navigator.serviceWorker.ready);
    }
    await page.waitForFunction(() => !!navigator.serviceWorker.controller, { timeout: 15000 });

    const installHelpers = () => page.evaluate(() => {
      window.__szAsk = (msg, transfer) => new Promise((resolve, reject) => {
        const ch = new MessageChannel();
        const t = setTimeout(() => reject(new Error('SW timeout for ' + msg.type)), 120000);
        ch.port1.onmessage = (e) => { clearTimeout(t); resolve(e.data); };
        navigator.serviceWorker.controller.postMessage(msg, [ch.port2].concat(transfer || []));
      });
      window.__szFetch = async (url, headers) => {
        const res = await fetch(url, { headers: headers || {}, cache: 'no-store' });
        const buf = new Uint8Array(await res.arrayBuffer());
        let h = 0x811c9dc5;
        for (let i = 0; i < buf.length; i++) { h ^= buf[i]; h = Math.imul(h, 0x01000193) >>> 0; }
        const hdr = {};
        for (const k of ['content-type', 'content-length', 'content-range', 'accept-ranges',
                         'x-streetzim-absent', 'x-streetzim-upstream', 'retry-after']) {
          const v = res.headers.get(k);
          if (v !== null) hdr[k] = v;
        }
        return { status: res.status, headers: hdr, length: buf.length, hash: h >>> 0,
                 text: buf.length < 8192 ? new TextDecoder().decode(buf) : null };
      };
      window.__szUpstream = [];
      navigator.serviceWorker.addEventListener('message', (e) => {
        if (e.data && e.data.type === 'streetzim-upstream') window.__szUpstream.push(e.data);
      });
    });
    await installHelpers();
    const goto = async (p) => {
      await page.goto(origin + p, { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => !!navigator.serviceWorker.controller, { timeout: 15000 });
      await installHelpers();
    };
    const ask = (msg) => page.evaluate((m) => window.__szAsk(m), msg);
    const get = (p, headers) => page.evaluate((u, h) => window.__szFetch(u, h), '/drive/viewer/' + p, headers || {});
    const status = () => ask({ type: 'status' });

    // Entry-level checks shared by both sources.
    async function entryChecks(label) {
      await check(label + ': big raw entry streams whole (200)', async () => {
        const r = await get('big/0.bin');
        assert.equal(r.status, 200);
        assert.equal(r.headers['content-length'], String(BIG.length));
        assert.equal(r.headers['accept-ranges'], 'bytes');
        assert.equal(r.headers['content-type'], BIN);
        assert.equal(r.length, BIG.length);
        assert.equal(r.hash, fnv1a(BIG));
      });
      await check(label + ': Range inside the entry → 206', async () => {
        const r = await get('big/0.bin', { Range: 'bytes=1000-2999' });
        assert.equal(r.status, 206);
        assert.equal(r.headers['content-range'], 'bytes 1000-2999/' + BIG.length);
        assert.equal(r.headers['content-length'], '2000');
        assert.equal(r.length, 2000);
        assert.equal(r.hash, fnv1a(BIG.subarray(1000, 3000)));
      });
      await check(label + ': open-ended Range → 206 to the end', async () => {
        const r = await get('big/0.bin', { Range: 'bytes=' + (BIG.length - 8) + '-' });
        assert.equal(r.status, 206);
        assert.equal(r.headers['content-range'], 'bytes ' + (BIG.length - 8) + '-' + (BIG.length - 1) + '/' + BIG.length);
        assert.equal(r.length, 8);
        assert.equal(r.hash, fnv1a(BIG.subarray(BIG.length - 8)));
      });
      await check(label + ': Range end past the entry is clamped', async () => {
        const r = await get('big/0.bin', { Range: 'bytes=' + (BIG.length - 16) + '-99999999999' });
        assert.equal(r.status, 206);
        assert.equal(r.headers['content-range'], 'bytes ' + (BIG.length - 16) + '-' + (BIG.length - 1) + '/' + BIG.length);
        assert.equal(r.length, 16);
      });
      await check(label + ': Range past the end → 416', async () => {
        const r = await get('big/0.bin', { Range: 'bytes=' + BIG.length + '-' });
        assert.equal(r.status, 416);
        assert.equal(r.headers['content-range'], 'bytes */' + BIG.length);
        assert.equal(r.length, 0);
      });
      await check(label + ': backwards Range is ignored → 200 whole', async () => {
        const r = await get('big/0.bin', { Range: 'bytes=5-2' });
        assert.equal(r.status, 200);
        assert.equal(r.length, BIG.length);
      });
      await check(label + ': 8-byte-offset raw cluster streams too (threshold + 1)', async () => {
        const r = await get('big/cells.bin');
        assert.equal(r.status, 200);
        assert.equal(r.length, CELLS.length);
        assert.equal(r.hash, fnv1a(CELLS));
        const p = await get('big/cells.bin', { Range: 'bytes=4194300-' });
        assert.equal(p.status, 206);
        assert.equal(p.headers['content-range'], 'bytes 4194300-4194304/' + CELLS.length);
        assert.equal(p.hash, fnv1a(CELLS.subarray(4194300)));
      });
      await check(label + ': concurrent reads of the same and different entries', async () => {
        const rs = await page.evaluate(() => Promise.all([
          window.__szFetch('/drive/viewer/big/cells.bin'),
          window.__szFetch('/drive/viewer/big/cells.bin'),
          window.__szFetch('/drive/viewer/small/a.bin'),
          window.__szFetch('/drive/viewer/small/a.bin'),
          window.__szFetch('/drive/viewer/big/0.bin', { Range: 'bytes=0-9' }),
        ]));
        assert.deepEqual(rs.map((r) => r.status), [200, 200, 200, 200, 206]);
        assert.equal(rs[0].hash, fnv1a(CELLS)); assert.equal(rs[1].hash, fnv1a(CELLS));
        assert.equal(rs[2].hash, fnv1a(SMALL_A)); assert.equal(rs[3].hash, fnv1a(SMALL_A));
        assert.equal(rs[4].hash, fnv1a(BIG.subarray(0, 10)));
      });
      await check(label + ': big entries were streamed, not kept in memory', async () => {
        const st = await status();
        assert.ok(st.ok && st.loaded, JSON.stringify(st));
        const c = st.cache;
        assert.ok(c && c.clusters && c.blobs, 'cache stats: ' + JSON.stringify(c));
        const held = c.clusters.bytes + c.blobs.bytes;
        assert.ok(held < 1 * MiB, 'reader holds ' + held + ' bytes after streaming ' + JSON.stringify(c));
      });
      await check(label + ': small entry sharing a raw cluster with a big one is read alone', async () => {
        const before = await status();
        const r = await get('small/b.bin');
        assert.equal(r.status, 200);
        assert.equal(r.hash, fnv1a(SMALL_B));
        const after = await status();
        const held = after.cache.clusters.bytes + after.cache.blobs.bytes;
        assert.ok(held < 1 * MiB, 'reader holds ' + held + ' bytes: ' + JSON.stringify(after.cache));
        if (before.stats && after.stats) {
          const moved = after.stats.bytes - before.stats.bytes;
          assert.ok(moved < 512 * 1024, 'fetched ' + moved + ' bytes for a 5000-byte entry');
        }
      });
      await check(label + ': small raw entry with Range → 206 (buffered path)', async () => {
        const r = await get('small/a.bin', { Range: 'bytes=100-199' });
        assert.equal(r.status, 206);
        assert.equal(r.headers['content-range'], 'bytes 100-199/' + SMALL_A.length);
        assert.equal(r.hash, fnv1a(SMALL_A.subarray(100, 200)));
        const w = await get('small/c.bin');
        assert.equal(w.status, 200);
        assert.equal(w.hash, fnv1a(SMALL_C));
      });
      await check(label + ': entry just under the threshold is buffered and exact', async () => {
        const r = await get('big/under.bin');
        assert.equal(r.status, 200);
        assert.equal(r.length, UNDER.length);
        assert.equal(r.hash, fnv1a(UNDER));
        const p = await get('big/under.bin', { Range: 'bytes=10-19' });
        assert.equal(p.status, 206);
        assert.equal(p.hash, fnv1a(UNDER.subarray(10, 20)));
      });
      await check(label + ': ' + (haveZstd ? 'zstd' : 'raw') + ' tile entry is exact (200 and 206)', async () => {
        const r = await get('tiles/1/2/3.pbf');
        assert.equal(r.status, 200);
        assert.equal(r.headers['content-type'], 'application/x-protobuf');
        assert.equal(r.hash, fnv1a(TILE));
        const p = await get('tiles/1/2/3.pbf', { Range: 'bytes=1024-2047' });
        assert.equal(p.status, 206);
        assert.equal(p.hash, fnv1a(TILE.subarray(1024, 2048)));
      });
      await check(label + ': article gets the Back-to-map bar', async () => {
        const r = await get('wiki-article/Foo_Bar.html');
        assert.equal(r.status, 200);
        assert.match(r.headers['content-type'], /^text\/html/);
        assert.ok(r.text.includes('class="sz-back"'), r.text);
        assert.ok(r.text.includes('href="/drive/viewer/"'), r.text);
        assert.ok(r.text.includes('<p>Article body.</p>'));
      });
      await check(label + ': optional probe → 200 + X-Streetzim-Absent, other misses → 404', async () => {
        const r = await get('routing-data/graph.bin');
        assert.equal(r.status, 200);
        assert.equal(r.headers['x-streetzim-absent'], '1');
        assert.equal(r.length, 0);
        const m = await get('nope/missing.bin');
        assert.equal(m.status, 404);
        const j = await get('map-config.json');
        assert.equal(j.status, 200);
        assert.equal(JSON.parse(j.text).name, 'synthetic');
      });
    }

    // ---- 1. URL source (the online preview) ----
    console.log('\n[url source]');
    await check('set-zim with a same-origin URL', async () => {
      const r = await ask({ type: 'set-zim', url: origin + '/synthetic.zim', name: 'synthetic.zim' });
      assert.ok(r.ok, JSON.stringify(r));
      assert.equal(r.source, 'url');
      assert.equal(r.info.articles, entries.length);
      const st = await status();
      assert.equal(st.source, 'url');
      assert.equal(st.url, origin + '/synthetic.zim');
      assert.equal(st.sizeBytes, zim.length);
    });
    await entryChecks('url');
    await check('ping reports the bytes moved without touching IndexedDB', async () => {
      const p = await ask({ type: 'ping' });
      assert.ok(p.ok && p.loaded, JSON.stringify(p));
      assert.ok(p.stats && p.stats.bytes > 0, JSON.stringify(p.stats));
    });

    // ---- 2. The viewer over the URL source ----
    console.log('\n[viewer]');
    await check('viewer renders the streamed ZIM with banner, byte counter and Change map', async () => {
      await goto('/drive/viewer/?bust=' + Date.now());
      await page.waitForSelector('#preview-banner:not([hidden])', { timeout: 30000 });
      await page.waitForFunction(() => document.querySelector('#info h3').textContent === 'synthetic', { timeout: 30000 });
      const t = await page.evaluate(() => ({
        bytes: document.getElementById('preview-banner-bytes').textContent,
        note: document.getElementById('preview-banner-note').textContent,
        change: (document.querySelector('#info a[href="/drive/?picker=1"]') || {}).textContent || null,
        manifest: (document.querySelector('link[rel="manifest"]') || {}).href || null,
        apple: !!document.querySelector('meta[name="apple-mobile-web-app-capable"]'),
        url: location.pathname,
      }));
      assert.match(t.bytes, /^ · [\d.]+ (KB|MB) streamed so far$/, t.bytes);
      assert.equal(t.note, '');
      assert.equal(t.change, 'Change map');
      assert.equal(t.manifest, origin + '/drive/manifest.webmanifest');
      assert.ok(t.apple, 'Apple metas injected on the /drive/ path');
      assert.equal(t.url, '/drive/viewer/');
    });

    // ---- 3. The origin goes away ----
    console.log('\n[outage]');
    await check('origin down → 503 + X-Streetzim-Upstream and a page notice; back up → 200', async () => {
      site.kill();
      await waitForPort(PORT, false);
      // A range nothing has asked for yet, so the HTTP cache cannot answer.
      const r = await get('big/0.bin', { Range: 'bytes=4000000-4999999' });
      assert.equal(r.status, 503, JSON.stringify(r));
      assert.equal(r.headers['x-streetzim-upstream'], 'network');
      assert.equal(r.headers['retry-after'], '30');
      assert.match(r.text, /^Upstream error: /);
      const notices = await page.evaluate(() => window.__szUpstream);
      assert.ok(notices.length >= 1, 'no streetzim-upstream message reached the page');
      assert.equal(notices[0].status, 0);
      const note = await page.evaluate(() => document.getElementById('preview-banner-note').textContent);
      assert.match(note, /No connection to the source/, note);
      site = startSite(siteDir);
      await waitForPort(PORT, true);
      const ok = await get('big/0.bin', { Range: 'bytes=4000000-4999999' });
      assert.equal(ok.status, 206, JSON.stringify(ok));
      assert.equal(ok.hash, fnv1a(BIG.subarray(4000000, 5000000)));
      const st = await status();
      assert.ok(st.loaded && st.source === 'url', JSON.stringify(st));
    });
    await check('viewer started while the origin is down says so instead of bouncing; recovers', async () => {
      site.kill();
      await waitForPort(PORT, false);
      // A cold reader (as after a worker restart) cannot open the source.
      const rl = await ask({ type: 'reload-zim' });
      assert.equal(rl.ok, false, JSON.stringify(rl));
      await goto('/drive/viewer/?bust=' + Date.now());          // shell from the precache
      await page.waitForFunction(() => /not answering/.test(document.querySelector('#info h3').textContent), { timeout: 60000 });
      assert.equal(await page.evaluate(() => location.pathname), '/drive/viewer/');
      assert.ok(await page.$('#info a[href="/drive/?picker=1"]'), 'Change map link');
      site = startSite(siteDir);
      await waitForPort(PORT, true);
      await page.evaluate(() => { const a = [...document.querySelectorAll('#info a')].find((x) => /Try again/.test(x.textContent)); a.click(); });
      await page.waitForFunction(() => document.querySelector('#info h3') && document.querySelector('#info h3').textContent === 'synthetic', { timeout: 60000 });
      await installHelpers();
    });

    // ---- 4. File source through check-zim → page-side IDB write → reload-zim ----
    console.log('\n[file source]');
    await goto('/drive/?picker=1');
    const picker = await page.$('#file-fallback');
    assert.ok(picker, 'no #file-fallback on /drive/');
    await picker.uploadFile(zimPath);
    await check('check-zim validates without persisting', async () => {
      const r = await page.evaluate(async () => {
        const f = document.getElementById('file-fallback').files[0];
        return window.__szAsk({ type: 'check-zim', blob: f });
      });
      assert.ok(r.ok, JSON.stringify(r));
      assert.equal(r.info.articles, entries.length);
      const st = await status();
      assert.equal(st.source, 'url', 'check-zim must not replace the current record');
    });
    await check('check-zim rejects a non-ZIM', async () => {
      const r = await page.evaluate(() =>
        window.__szAsk({ type: 'check-zim', blob: new Blob([new Uint8Array(4096)]) }));
      assert.equal(r.ok, false);
      assert.ok(/zim|magic/i.test(r.error), r.error);
    });
    await check('page writes the record, reload-zim opens it as a file', async () => {
      const r = await page.evaluate(async () => {
        const f = document.getElementById('file-fallback').files[0];
        await new Promise((resolve, reject) => {
          const req = indexedDB.open('streetzim-drive', 1);
          req.onupgradeneeded = () => {
            if (!req.result.objectStoreNames.contains('zim')) req.result.createObjectStore('zim', { keyPath: 'id' });
          };
          req.onerror = () => reject(req.error);
          req.onsuccess = () => {
            const db = req.result;
            const tx = db.transaction('zim', 'readwrite');
            tx.objectStore('zim').put({ id: 'current', blob: f, name: f.name, addedAt: Date.now() });
            tx.oncomplete = () => { db.close(); resolve(); };
            tx.onerror = () => reject(tx.error);
          };
        });
        return window.__szAsk({ type: 'reload-zim' });
      });
      assert.ok(r.ok, JSON.stringify(r));
      assert.equal(r.source, 'file');
      const st = await status();
      assert.equal(st.source, 'file');
      assert.equal(st.name, 'synthetic.zim');
      assert.equal(st.loaded, true);
      assert.equal(st.url, null);
    });
    await entryChecks('file');

    // ---- 5. clear-zim, then the legacy set-zim {blob} path ----
    console.log('\n[clear + legacy set-zim]');
    await check('clear-zim → no ZIM: 503 without the upstream header', async () => {
      const r = await ask({ type: 'clear-zim' });
      assert.ok(r.ok, JSON.stringify(r));
      const st = await status();
      assert.equal(st.loaded, false);
      assert.equal(st.present, false);
      const g = await get('small/a.bin');
      assert.equal(g.status, 503);
      assert.equal(g.headers['x-streetzim-upstream'], undefined);
      assert.equal(g.text, 'No ZIM loaded');
      const rl = await ask({ type: 'reload-zim' });
      assert.equal(rl.ok, false);
      assert.match(rl.error, /no ZIM record/);
    });
    await check('viewer with no ZIM bounces to the picker', async () => {
      await page.goto(origin + '/drive/viewer/?bust=' + Date.now(), { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => location.pathname === '/drive/' && location.search === '?picker=1', { timeout: 30000 });
      await page.waitForFunction(() => !!navigator.serviceWorker.controller, { timeout: 15000 });
      await installHelpers();
    });
    await check('set-zim {blob} still works', async () => {
      await (await page.$('#file-fallback')).uploadFile(zimPath);   // fresh page after the bounce
      const r = await page.evaluate(async () => {
        const f = document.getElementById('file-fallback').files[0];
        return window.__szAsk({ type: 'set-zim', blob: f, name: f.name });
      });
      assert.ok(r.ok, JSON.stringify(r));
      assert.equal(r.source, 'file');
      const g = await get('small/a.bin');
      assert.equal(g.status, 200);
      assert.equal(g.hash, fnv1a(SMALL_A));
      const u = await ask({ type: 'bogus' });
      assert.equal(u.ok, false);
    });
  } finally {
    await browser.close().catch(() => {});
    try { site.kill(); } catch (e) {}
    fs.rmSync(siteDir, { recursive: true, force: true });
  }
  if (swExceptions) { failures++; console.log('  [FAIL] ' + swExceptions + ' uncaught exception(s) in the service worker'); }
  console.log(failures ? '\n' + failures + ' FAILED' : '\nall passed');
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
