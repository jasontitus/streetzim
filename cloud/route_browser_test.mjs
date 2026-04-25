// Headless-browser routing test harness for the /drive/ PWA.
//
// Drives the live Firebase deploy:
//   1. Launches a headless Chromium tab with web-security relaxed
//      (so the page can fetch a ZIM from a localhost:8765 serve).
//   2. Loads /drive/, fetches the configured ZIM, posts it to the
//      service worker via the same `set-zim` message the picker uses.
//   3. Navigates to /drive/viewer/?debug=1.
//   4. Runs each route in ROUTES, polls the routing-status text +
//      the route-distance result, prints PASS/FAIL with timing.
//
// Usage:
//   node cloud/route_browser_test.mjs                 # default Silicon Valley
//   ZIM_URL=http://localhost:8765/osm-japan-2026-04-25.zim \
//   ROUTES=japan node cloud/route_browser_test.mjs    # Japan tests
//   HEADFUL=1 node cloud/route_browser_test.mjs       # show the browser
//
// What it asserts:
//   * SW set-zim succeeds within 60 s.
//   * Viewer's `graph` global appears within 30 s of navigating.
//   * Each route either yields a distance text or hits the route's
//     bail-out path within ROUTE_TIMEOUT_MS — no infinite waits.
//
// Output is a one-row-per-route summary. Exit code: 0 if all PASS,
// 1 if any FAIL, 2 on harness error (network, SW unreachable, etc.)

import puppeteer from 'puppeteer';

const SITE = process.env.STREETZIM_SITE || 'https://streetzim.web.app';
const ZIM_URL = process.env.ZIM_URL || 'http://localhost:8765/osm-silicon-valley-2026-04-24.zim';
const ROUTES_NAME = process.env.ROUTES || 'silicon-valley';
const HEADFUL = process.env.HEADFUL === '1';
const ROUTE_TIMEOUT_MS = parseInt(process.env.ROUTE_TIMEOUT_MS || '180000', 10);

// Each row: {label, src: [lat,lon], dst: [lat,lon]}
const ROUTE_SETS = {
  'silicon-valley': [
    { label: 'Palo Alto → Mountain View (~5 km)',
      src: [37.4419, -122.1430], dst: [37.3861, -122.0839] },
    { label: 'Stanford → SF (~50 km)',
      src: [37.4275, -122.1697], dst: [37.7749, -122.4194] },
    { label: 'Cupertino → Berkeley (~70 km)',
      src: [37.3230, -122.0322], dst: [37.8716, -122.2727] },
    { label: 'San Jose → SF (~80 km)',
      src: [37.3382, -121.8863], dst: [37.7749, -122.4194] },
  ],
  'japan': [
    { label: 'Tokyo Sta → Tokyo Tower (~1.5 km)',
      src: [35.6812, 139.7671], dst: [35.6586, 139.7454] },
    { label: 'Tokyo → Yokohama (~30 km)',
      src: [35.6812, 139.7671], dst: [35.4437, 139.6380] },
    { label: 'Tokyo → Nagoya (~350 km)',
      src: [35.6812, 139.7671], dst: [35.1815, 136.9066] },
    { label: 'Tokyo → Oita (~830 km)',
      src: [35.6812, 139.7671], dst: [33.2382, 131.6126] },
    { label: 'Oita → Tokyo (reverse)',
      src: [33.2382, 131.6126], dst: [35.6812, 139.7671] },
  ],
  'west-asia': [
    { label: 'Tehran → Karaj (~40 km)',
      src: [35.6892, 51.3890], dst: [35.8327, 50.9915] },
    { label: 'Tehran → Qom (~140 km)',
      src: [35.6892, 51.3890], dst: [34.6416, 50.8746] },
    { label: 'Tehran → Isfahan (~430 km)',
      src: [35.6892, 51.3890], dst: [32.6546, 51.6680] },
    { label: 'Tehran → Baghdad (~700 km cross-border)',
      src: [35.6892, 51.3890], dst: [33.7433, 44.6260] },
    { label: 'Riyadh → Doha (~500 km cross-border)',
      src: [24.7136, 46.6753], dst: [25.2854, 51.5310] },
  ],
  'texas': [
    { label: 'Austin → Round Rock (~30 km)',
      src: [30.2672, -97.7431], dst: [30.5083, -97.6789] },
    { label: 'Austin → San Antonio (~130 km)',
      src: [30.2672, -97.7431], dst: [29.4241, -98.4936] },
    { label: 'Austin → Houston (~265 km)',
      src: [30.2672, -97.7431], dst: [29.7604, -95.3698] },
    { label: 'Houston → Dallas (~390 km)',
      src: [29.7604, -95.3698], dst: [32.7767, -96.7970] },
    { label: 'El Paso → Houston (~1180 km)',
      src: [31.7619, -106.4850], dst: [29.7604, -95.3698] },
  ],
  'central-us': [
    { label: 'Denver → Boulder (~45 km)',
      src: [39.7392, -104.9903], dst: [40.0150, -105.2705] },
    { label: 'Denver → Salt Lake City (~660 km)',
      src: [39.7392, -104.9903], dst: [40.7608, -111.8910] },
    { label: 'Phoenix → Las Vegas (~480 km)',
      src: [33.4484, -112.0740], dst: [36.1699, -115.1398] },
    { label: 'Salt Lake City → Albuquerque (~970 km)',
      src: [40.7608, -111.8910], dst: [35.0844, -106.6504] },
  ],
  'australia-nz': [
    { label: 'Sydney → Newcastle (~160 km)',
      src: [-33.8688, 151.2093], dst: [-32.9283, 151.7817] },
    { label: 'Sydney → Canberra (~290 km)',
      src: [-33.8688, 151.2093], dst: [-35.2809, 149.1300] },
    { label: 'Sydney → Melbourne (~880 km)',
      src: [-33.8688, 151.2093], dst: [-37.8136, 144.9631] },
    { label: 'Brisbane → Cairns (~1700 km)',
      src: [-27.4698, 153.0251], dst: [-16.9186, 145.7781] },
    { label: 'Auckland → Wellington (NZ) (~640 km)',
      src: [-36.8485, 174.7633], dst: [-41.2865, 174.7762] },
  ],
};

async function main() {
  const routes = ROUTE_SETS[ROUTES_NAME];
  if (!routes) {
    console.error('Unknown ROUTES=' + ROUTES_NAME +
      '. Choices: ' + Object.keys(ROUTE_SETS).join(', '));
    process.exit(2);
  }

  console.log('site=' + SITE + '  zim=' + ZIM_URL + '  routes=' + ROUTES_NAME);
  // Use system Chrome by default — Puppeteer's bundled Chromium is
  // sandboxed off the network in some environments (Claude Code's
  // shell, for example), which manifests as net::ERR_ABORTED on
  // every navigation. System Chrome inherits the user's network
  // policy and just works. Override with CHROME_PATH if needed.
  const chromePath = process.env.CHROME_PATH ||
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const browser = await puppeteer.launch({
    headless: !HEADFUL,
    executablePath: chromePath,
    args: [
      '--no-sandbox',
      '--disable-web-security',
      '--disable-features=IsolateOrigins,site-per-process',
    ],
    dumpio: !!process.env.DUMPIO,
  });
  const page = await browser.newPage();
  page.setDefaultNavigationTimeout(60_000);

  page.on('console', msg => {
    const t = msg.text();
    if (t.startsWith('[route]') || t.startsWith('[sw]')) {
      console.log('  > ' + t);
    }
  });
  page.on('pageerror', err =>
    console.log('  ! pageerror:', err.message + '\n' + (err.stack || '')));
  page.on('requestfailed', req => console.log('  ! reqfail:', req.url(),
    req.failure() && req.failure().errorText));
  page.on('response', resp => {
    if (resp.status() >= 400) {
      console.log('  ! ' + resp.status() + ' ' + resp.url());
    }
  });

  // 1. Load picker page so the SW registers + fetch the ZIM.
  console.log('\n[1/3] loading picker, fetching ZIM, posting to SW...');
  await page.goto(SITE + '/drive/?bust=1', { waitUntil: 'domcontentloaded' });

  // Wait for the SW to be controlling the page.
  await page.evaluate(() =>
    new Promise(r => navigator.serviceWorker.ready.then(r)));

  const setOk = await page.evaluate(async (zimUrl) => {
    try {
      const resp = await fetch(zimUrl);
      if (!resp.ok) return { ok: false, error: 'fetch ' + resp.status };
      const blob = await resp.blob();
      const ch = new MessageChannel();
      const reply = new Promise(r => { ch.port1.onmessage = e => r(e.data); });
      navigator.serviceWorker.controller.postMessage(
        { type: 'set-zim', blob, name: zimUrl.split('/').pop() }, [ch.port2]);
      return await reply;
    } catch (err) {
      return { ok: false, error: String(err && err.message || err) };
    }
  }, ZIM_URL);

  if (!setOk || !setOk.ok) {
    console.error('SW set-zim failed:', setOk);
    await browser.close();
    process.exit(2);
  }
  console.log('  SW accepted ZIM (' + (setOk.info && setOk.info.title) + ')');

  // 2. Navigate to viewer + wait for graph to load. The route mode
  // is encoded in the viewer URL (?route=full / ?route=two-pass /
  // default). We pass `default` first; comparison harness re-loads
  // with ?route=full afterward.
  const VIEWER_MODE = process.env.MODE || 'default';  // 'default' | 'full' | 'two-pass'
  const viewerQuery = '?debug=1' + (VIEWER_MODE === 'default' ? '' : '&route=' + VIEWER_MODE);
  console.log('\n[2/3] loading viewer (mode=' + VIEWER_MODE + ')...');
  await page.goto(SITE + '/drive/viewer/' + viewerQuery, { waitUntil: 'domcontentloaded' });
  // streetzimRouting is the public seam; it's defined when the
  // viewer's JS finishes parsing. Then we wait for it to expose a
  // graph-ready signal. The graph is closure-scoped inside an IIFE
  // so we can't see it directly — probe via streetzimRouting's
  // status hook instead.
  await page.waitForFunction(
    () => window.streetzimRouting && typeof window.streetzimRouting.open === 'function',
    { timeout: 30_000 });
  // Then wait for the routing status to be something other than
  // "Loading routing data...".
  await page.waitForFunction(() => {
    const s = (document.getElementById('routing-status') || {}).textContent || '';
    return !/Loading routing data/i.test(s);
  }, { timeout: 60_000 });
  console.log('  viewer ready');

  // 3. Run each route.
  console.log('\n[3/3] running ' + routes.length + ' routes...');
  const results = [];
  for (const route of routes) {
    console.log('\n=== ' + route.label + ' ===');
    const t0 = Date.now();

    const r = await page.evaluate(async (route, timeoutMs) => {
      // Reset prior route. clear() if available, else click the
      // visible button. Then explicitly blank the result + status
      // so the next poll loop only sees fresh values.
      if (window.streetzimRouting && window.streetzimRouting.clear) {
        window.streetzimRouting.clear();
      } else {
        const btn = Array.from(document.querySelectorAll('button'))
          .find(b => /clear route/i.test(b.textContent || ''));
        if (btn) btn.click();
      }
      const dEl = document.getElementById('route-distance');
      const tEl = document.getElementById('route-time');
      const sEl = document.getElementById('routing-status');
      if (dEl) dEl.textContent = '';
      if (tEl) tEl.textContent = '';
      if (sEl) sEl.textContent = '';
      await new Promise(r => setTimeout(r, 300));

      window.streetzimRouting.open();
      window.streetzimRouting.setOrigin(route.src[0], route.src[1], 'src');
      window.streetzimRouting.setDest(route.dst[0], route.dst[1], 'dst');

      const start = Date.now();
      let lastStatus = '';
      let peakHeapMB = 0;
      let peakCells = 0;
      while (Date.now() - start < timeoutMs) {
        const dist = (document.getElementById('route-distance') || {}).textContent || '';
        const time = (document.getElementById('route-time') || {}).textContent || '';
        const status = (document.getElementById('routing-status') || {}).textContent || '';
        if (status) lastStatus = status;
        // Heap snapshot — Chrome only.
        if (performance && performance.memory) {
          const mb = performance.memory.usedJSHeapSize / 1024 / 1024;
          if (mb > peakHeapMB) peakHeapMB = mb;
        }
        // Parse cells count from the status string ("... · 4 cells").
        const m = status.match(/\s(\d+)\s+cells\b/);
        if (m) {
          const c = parseInt(m[1], 10);
          if (c > peakCells) peakCells = c;
        }
        if (dist) {
          return { ok: true, distance: dist, time, status: lastStatus,
                   peakHeapMB, peakCells,
                   elapsedMs: Date.now() - start };
        }
        // "BAIL" appears in the status during a single phase that
        // hits its pop limit — but the chained strategy may still
        // try another phase next, so we must NOT treat it as final.
        // Final-failure markers come from the routing code's own
        // "no route found" / "Routing failed" status text.
        if (/no route found|Routing failed/i.test(status)) {
          return { ok: false, distance: '', time: '', status: lastStatus,
                   peakHeapMB, peakCells,
                   elapsedMs: Date.now() - start };
        }
        await new Promise(r => setTimeout(r, 500));
      }
      return { ok: false, distance: '', time: '', status: lastStatus || 'TIMEOUT',
               peakHeapMB, peakCells,
               elapsedMs: Date.now() - start };
    }, route, ROUTE_TIMEOUT_MS);

    const wallSec = ((Date.now() - t0) / 1000).toFixed(1);
    const tag = r.ok ? 'PASS' : 'FAIL';
    console.log('  [' + tag + '] ' + (r.distance || r.status) +
                '  time=' + (r.time || '–') +
                '  wall=' + wallSec + 's' +
                '  peak heap=' + r.peakHeapMB.toFixed(0) + 'MB' +
                '  peak cells=' + r.peakCells);
    results.push({ ...r, label: route.label, wallSec });
  }

  console.log('\n=== summary ===');
  const passed = results.filter(r => r.ok).length;
  console.log(passed + ' of ' + results.length + ' passed');
  for (const r of results) {
    console.log('  ' + (r.ok ? '[PASS]' : '[FAIL]') + '  ' +
      r.label + '  → ' + (r.distance || r.status) +
      '  (' + r.wallSec + 's, peak ' + r.peakHeapMB.toFixed(0) +
      'MB / ' + r.peakCells + ' cells)');
  }

  await browser.close();
  process.exit(passed === results.length ? 0 : 1);
}

main().catch(err => { console.error(err); process.exit(2); });
