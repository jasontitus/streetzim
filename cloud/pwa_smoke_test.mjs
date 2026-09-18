// PWA UX smoke test for streetzim.web.app/drive/.
//
// Loads a ZIM via the picker, navigates the viewer + places.html,
// and exercises:
//   1. Top-bar search (#search-input) — does typing produce results?
//   2. Find page (places.html) chip — does clicking a category render
//      records? does the first result have a "Directions" button?
//   3. Directions panel — does clicking origin field show typeahead
//      suggestions when we type a city name?
//   4. Wikidata popup → "Directions to here" CTA — does it open the
//      routing panel and pre-fill destination?
//
// Each step that fails prints a [FAIL] line; the script exits 1 if
// any step fails. Run after every PWA deploy that touches viewer
// JS / sw.js / places.html.
//
// Usage:
//   ZIM_URL=http://localhost:8765/osm-japan-chips-v2.zim \
//     node cloud/pwa_smoke_test.mjs
//   HEADFUL=1 ZIM_URL=... node cloud/pwa_smoke_test.mjs   # watch it
//
// Defaults to Silicon Valley (small, fast). Specify ZIM_URL to test
// other regions.

import puppeteer from 'puppeteer';

const SITE = process.env.STREETZIM_SITE || 'https://streetzim.web.app';
// The search step used to hardcode "Palo Alto" regardless of region. It
// passed elsewhere by luck (something matched the substring) and failed
// central-asia honestly: a Central Asia ZIM has no Palo Alto, so the wait
// for results ran out the 15 s clock and the gate reported a browser
// failure for a ZIM whose search was fine — the CLI gate found 164 hits
// for 'Tashkent' in the same file. Callers pass the registry's
// smoke_search column; the old value stays the default.
const SMOKE_SEARCH = process.env.SMOKE_SEARCH || 'Palo Alto';
// How long the Find page gets to answer a name search. Generous for a cold
// 20 GB ZIM over the service worker, but well under the 99 s that shipped on
// south-america before search had prefix locality
// (docs/search-prefix-locality.md).
const SEARCH_BUDGET_MS = +(process.env.SEARCH_BUDGET_MS || 20_000);
const ZIM_FILE = process.env.ZIM_FILE || '';
const ZIM_URL = process.env.ZIM_URL ||
  'http://localhost:8765/osm-silicon-valley-2026-04-24.zim';
const HEADFUL = process.env.HEADFUL === '1';
const CHROME_PATH = process.env.CHROME_PATH ||
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

// Pairs that exercise long-distance routing — what the user actually
// hits when planning a trip. Picked to be far apart relative to each
// region's bbox so the spatial graph spans many cells (route memory
// and convergence time scale with cells touched). Lookup is by
// substring of the ZIM URL; falls back to silicon-valley.
const ROUTE_PAIRS = {
  'silicon-valley': {
    o: { lat: 37.7749, lon: -122.4194, label: 'San Francisco' },
    d: { lat: 37.3382, lon: -121.8863, label: 'San Jose' },
    crow_km: 75,
  },
  'carolinas': {
    // Raleigh → Asheville (~360 km) — cross-state E→W, runs the route
    // straight through the western-NC mountains (the terrain-gap zone),
    // exercising the spatial graph across the full bbox.
    o: { lat: 35.7796, lon: -78.6382, label: 'Raleigh' },
    d: { lat: 35.5951, lon: -82.5515, label: 'Asheville' },
    crow_km: 360,
  },
  'california': {
    // SF → LA (~560 km) — the canonical N-S cross-state route. Exercises
    // spatial graph cell paging across the full Central Valley + Grapevine.
    o: { lat: 37.7749, lon: -122.4194, label: 'San Francisco' },
    d: { lat: 34.0522, lon: -118.2437, label: 'Los Angeles' },
    crow_km: 560,
  },
  'canada': {
    // Toronto → Montreal (~505 km) is the Canada smoke default —
    // exercises spatial graph + cell paging, completes in ~7 s on
    // the current algorithm so it makes a workable regression
    // gate. For the cross-Canada stress test (Toronto → Vancouver,
    // 3300 km, 5+ minutes today) set LONG_ROUTE=1 — it's currently
    // expected to hit our pop-limit chain and exercise the spinner
    // ETA cap rather than complete in any reasonable time.
    o: { lat: 43.6532, lon:  -79.3832, label: 'Toronto' },
    d: { lat: 45.5019, lon:  -73.5674, label: 'Montreal' },
    crow_km: 505,
  },
  'canada-long': {
    o: { lat: 43.6532, lon:  -79.3832, label: 'Toronto' },
    d: { lat: 49.2827, lon: -123.1207, label: 'Vancouver' },
    crow_km: 3360,
  },
  'ukraine': {
    // Kyiv → Kharkiv (~410 km E-W) crosses several spatial cells.
    // Long variant Lviv → Volgograd is the full west-east bbox span
    // (~1,500 km), exercises cross-region cell paging.
    o: { lat: 50.4501, lon:  30.5234, label: 'Kyiv' },
    d: { lat: 49.9935, lon:  36.2304, label: 'Kharkiv' },
    crow_km: 410,
  },
  'brazil': {
    // Rio → São Paulo (~360 km along BR-116 / Via Dutra) — the
    // canonical Brazilian inter-city route.
    o: { lat: -22.9068, lon: -43.1729, label: 'Rio de Janeiro' },
    d: { lat: -23.5505, lon: -46.6333, label: 'São Paulo' },
    crow_km: 360,
  },
  'argentina': {
    // Buenos Aires → Mendoza (~1,000 km W) — cross-Argentina along
    // RN7. Long enough to stress cell paging across the Pampas.
    o: { lat: -34.6037, lon: -58.3816, label: 'Buenos Aires' },
    d: { lat: -32.8908, lon: -68.8272, label: 'Mendoza' },
    crow_km: 1000,
  },
  'ukraine-long': {
    o: { lat: 49.8397, lon:  24.0297, label: 'Lviv' },
    d: { lat: 48.7080, lon:  44.5133, label: 'Volgograd' },
    crow_km: 1500,
  },
  'japan': {
    o: { lat: 35.6762, lon:  139.6503, label: 'Tokyo' },
    d: { lat: 34.6937, lon:  135.5023, label: 'Osaka' },
    crow_km: 400,
  },
  'central-us': {
    o: { lat: 39.7392, lon: -104.9903, label: 'Denver' },
    d: { lat: 41.8781, lon:  -87.6298, label: 'Chicago' },
    crow_km: 1480,
  },
  'iran': {
    o: { lat: 35.6892, lon:   51.3890, label: 'Tehran' },
    d: { lat: 32.6539, lon:   51.6660, label: 'Isfahan' },
    crow_km: 340,
  },
  'california': {
    o: { lat: 37.7749, lon: -122.4194, label: 'San Francisco' },
    d: { lat: 34.0522, lon: -118.2437, label: 'Los Angeles' },
    crow_km: 560,
  },
  'colorado': {
    o: { lat: 39.7392, lon: -104.9903, label: 'Denver' },
    d: { lat: 38.8339, lon: -104.8214, label: 'Colorado Springs' },
    crow_km: 100,
  },
  'texas': {
    o: { lat: 30.2672, lon:  -97.7431, label: 'Austin' },
    d: { lat: 29.7604, lon:  -95.3698, label: 'Houston' },
    crow_km: 235,
  },
  'midwest-us': {
    o: { lat: 41.8781, lon:  -87.6298, label: 'Chicago' },
    d: { lat: 38.6270, lon:  -90.1994, label: 'St. Louis' },
    crow_km: 420,
  },
  'baltics': {
    o: { lat: 54.6872, lon:   25.2797, label: 'Vilnius' },
    d: { lat: 56.9496, lon:   24.1052, label: 'Riga' },
    crow_km: 270,
  },
  'hispaniola': {
    o: { lat: 18.4861, lon:  -69.9312, label: 'Santo Domingo' },
    d: { lat: 18.5944, lon:  -72.3074, label: 'Port-au-Prince' },
    crow_km: 250,
  },
  'washington-dc': {
    o: { lat: 38.9072, lon:  -77.0369, label: 'Washington' },
    d: { lat: 38.8816, lon:  -77.0910, label: 'Arlington' },
    crow_km: 8,
  },
  'australia-nz': {
    o: { lat: -33.8688, lon: 151.2093, label: 'Sydney' },
    d: { lat: -37.8136, lon: 144.9631, label: 'Melbourne' },
    crow_km: 715,
  },
  'central-america-caribbean': {
    o: { lat: 21.1619, lon: -86.8515, label: 'Cancún' },
    d: { lat: 20.9674, lon: -89.5926, label: 'Mérida' },
    crow_km: 300,
  },
  'himalayas': {
    o: { lat: 27.7172, lon:  85.3240, label: 'Kathmandu' },
    d: { lat: 28.2096, lon:  83.9856, label: 'Pokhara' },
    crow_km: 200,
  },
};

function pickRoutePair(zimUrl) {
  // SMOKE_ROUTE="lat,lon;lat,lon[;crow_km]" overrides the table — the
  // refresh queue passes each region's registry pair (cloud/regions.tsv)
  // so regions without a ROUTE_PAIRS entry don't fall back to Silicon
  // Valley coordinates (out of bbox → a false "no route").
  const envRoute = process.env.SMOKE_ROUTE;
  if (envRoute) {
    const parts = envRoute.split(';');
    const [ola, olo] = parts[0].split(',').map(Number);
    const [dla, dlo] = (parts[1] || '').split(',').map(Number);
    if ([ola, olo, dla, dlo].every(Number.isFinite)) {
      let crow = Number(parts[2]);
      if (!Number.isFinite(crow) || crow <= 0) {
        const R = 6371, toRad = (d) => d * Math.PI / 180;
        const dLat = toRad(dla - ola), dLon = toRad(dlo - olo);
        const a = Math.sin(dLat / 2) ** 2 +
          Math.cos(toRad(ola)) * Math.cos(toRad(dla)) * Math.sin(dLon / 2) ** 2;
        crow = Math.round(2 * R * Math.asin(Math.sqrt(a)));
      }
      return { region: 'env', o: { lat: ola, lon: olo, label: 'origin' },
               d: { lat: dla, lon: dlo, label: 'destination' }, crow_km: crow };
    }
    console.warn('SMOKE_ROUTE unparseable, ignoring: ' + envRoute);
  }
  const wantLong = process.env.LONG_ROUTE === '1';
  for (const key of Object.keys(ROUTE_PAIRS)) {
    if (key.endsWith('-long')) continue;
    if (!zimUrl.includes(key)) continue;
    if (wantLong) {
      const longKey = key + '-long';
      if (ROUTE_PAIRS[longKey]) return { region: longKey, ...ROUTE_PAIRS[longKey] };
    }
    return { region: key, ...ROUTE_PAIRS[key] };
  }
  return { region: 'silicon-valley', ...ROUTE_PAIRS['silicon-valley'] };
}

const failures = [];
function fail(label, detail) {
  failures.push(label + ': ' + detail);
  console.log('  [FAIL] ' + label + ' — ' + detail);
}
function pass(label, detail) {
  console.log('  [PASS] ' + label + (detail ? ' — ' + detail : ''));
}

async function main() {
  console.log('site=' + SITE + '  zim=' + (ZIM_FILE || ZIM_URL) +
              (ZIM_FILE ? '  (local file)' : ''));
  const browser = await puppeteer.launch({
    headless: !HEADFUL,
    executablePath: CHROME_PATH,
    args: ['--no-sandbox', '--disable-web-security'],
    // Default 180s lets a long route eat the entire CDP budget and
    // turn into "Runtime.callFunctionOn timed out" rather than a
    // clean failure with a useful last-status snapshot.
    protocolTimeout: 600_000,
  });
  const page = await browser.newPage();
  page.setDefaultNavigationTimeout(60_000);

  // Strict console / network failure tracking. The user's standing
  // request: "No errors." Anything that lands as a pageerror or as
  // type:'error' on console fails the run. Network 404s for /drive/*
  // or the test ZIM also fail. Step labels track which test was
  // running when the error fired.
  let currentStep = 'init';
  const consoleErrs = [];
  const network404s = [];
  page.on('pageerror', err => {
    console.log('  ! pageerror [' + currentStep + ']:', err.message);
    consoleErrs.push(currentStep + ': pageerror: ' + err.message);
  });
  page.on('console', m => {
    const type = m.type();
    // Chrome/Puppeteer reports console.warn as type 'warn' (some
    // older docs say 'warning' — both happen across versions); accept
    // either so warnings always surface to the smoke output.
    if (type !== 'error' && type !== 'warning' && type !== 'warn') return;
    const text = m.text();
    console.log('  ! ' + type + ' [' + currentStep + ']:', text);
    // The browser emits a URL-less "Failed to load resource ... 404" for
    // every 404. Each real 404 is already captured WITH its URL by the
    // response handler below, so this mirror adds no information and must
    // not fail a run on its own -- otherwise an allowlisted tile 404 still
    // fails the region through the console side (argentina, 2026-09-18).
    if (type === 'error' && /Failed to load resource/.test(text) && /\b404\b/.test(text)) return;
    if (type === 'error') consoleErrs.push(currentStep + ': console: ' + text);
  });
  // Log every 404 — even off-origin ones — so console-error lines
  // (which the browser emits without a URL) can be matched up.
  // /drive/* and ZIM-URL 404s are *failures*; others are noise.
  page.on('response', resp => {
    const u = resp.url();
    const s = resp.status();
    if (s !== 404) return;
    const ours = u.includes('/drive/') || u.includes(ZIM_URL);
    // Vector tiles missing at the bbox edge are a pre-existing gap in the
    // source data, not a retrofit defect: argentina's two 404 tiles are
    // absent from EVERY generation of that region (09-07, 09-09, 09-10 and
    // the 09-18 output), and validate_zim only warns about missing tile
    // zooms. Report them; never fail a region on them.
    const isTile = /\/tiles\/\d+\/\d+\/\d+\.pbf(\?|$)/.test(u);
    // /drive/wiki-qid-titles.json is generated at deploy time and served
    // by Firebase; a local copy of web/drive has no such file and the
    // viewer treats its absence as "no titles" (cloud/preview_smoke_test
    // lists it as optional for the same reason).
    const isOptional = /\/drive\/wiki-qid-titles\.json(\?|$)/.test(u);
    console.log('  ! 404 [' + currentStep + ']' + (ours ? '' : ' (off-origin)')
                + (isTile ? ' (missing tile, not fatal)' : '')
                + (isOptional ? ' (optional, deploy-generated)' : '') + ':', u);
    if (ours && !isTile && !isOptional) network404s.push(currentStep + ': 404 ' + u);
  });
  page.on('requestfailed', req => {
    const u = req.url();
    if (!u.includes('/drive/') && !u.includes(ZIM_URL)) return;
    const reason = req.failure() && req.failure().errorText;
    // net::ERR_ABORTED is MapLibre cancelling stale tile requests when
    // the viewport changes mid-fetch (panEnd, zoom transitions). Not a
    // real failure — the new viewport's tiles will be requested fresh.
    // Treating these as errors made route-perf flake whenever fitBounds
    // animated through multiple zoom levels.
    if (reason === 'net::ERR_ABORTED') {
      console.log('  · req cancelled [' + currentStep + ']:', u);
      return;
    }
    console.log('  ! reqfail [' + currentStep + ']:', u, reason);
    consoleErrs.push(currentStep + ': reqfail: ' + u + ' ' + reason);
  });

  // 1. Load ZIM via picker → SW.
  currentStep = 'setup';
  console.log('\n[setup] loading ZIM into SW...');
  await page.goto(SITE + '/drive/?bust=' + Date.now(), { waitUntil: 'domcontentloaded' });
  await page.evaluate(() =>
    new Promise(r => navigator.serviceWorker.ready.then(r)));
  // Prefer handing the SW a real on-disk File (ZIM_FILE), which is what
  // the picker gives it in production: File is lazily read from disk, so
  // nothing is buffered. Fetching the URL and calling resp.blob() instead
  // makes the browser materialise the WHOLE ZIM — Chrome simply fails
  // ("Failed to fetch") on east-coast-us at 12.4 GB, which is why that
  // region could never clear the browser gate. The URL path stays as the
  // fallback for testing a genuinely remote ZIM.
  let setOk;
  if (ZIM_FILE) {
    const picker = await page.$('#file-fallback');
    if (!picker) { console.error('no #file-fallback input on /drive/'); process.exit(2); }
    // uploadFile goes through CDP DOM.setFileInputFiles — the browser gets
    // a path, not bytes, so a 12 GB ZIM costs nothing here.
    await picker.uploadFile(ZIM_FILE);
    setOk = await page.evaluate(async () => {
      try {
        const f = document.getElementById('file-fallback').files[0];
        if (!f) return { ok: false, error: 'no file on #file-fallback' };
        const ch = new MessageChannel();
        const reply = new Promise(r => { ch.port1.onmessage = e => r(e.data); });
        navigator.serviceWorker.controller.postMessage(
          { type: 'set-zim', blob: f, name: f.name }, [ch.port2]);
        return await reply;
      } catch (err) {
        return { ok: false, error: String(err && err.message || err) };
      }
    });
  } else {
    setOk = await page.evaluate(async (zimUrl) => {
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
  }
  if (!setOk || !setOk.ok) {
    console.error('SW set-zim failed:', setOk);
    await browser.close();
    process.exit(2);
  }
  pass('SW loaded ZIM', setOk.info && setOk.info.title || '?');

  // 2. Open viewer.
  currentStep = 'viewer';
  console.log('\n[viewer] navigate + wait for graph...');
  await page.goto(SITE + '/drive/viewer/?bust=' + Date.now(),
    { waitUntil: 'domcontentloaded' });
  try {
    await page.waitForFunction(
      () => window.streetzimRouting && typeof window.streetzimRouting.open === 'function',
      { timeout: 30_000 });
    pass('viewer streetzimRouting API ready');
  } catch (e) {
    fail('viewer streetzimRouting API ready', e.message);
  }
  // The online-preview banner is for ZIMs streamed off the web only. A
  // locally picked file (this test) must never show it — nor does a ZIM
  // opened in Kiwix, where the path and the missing service worker keep
  // it hidden — so the "downloaded file is faster" pitch never appears
  // to someone who already has the file.
  {
    const bannerShown = await page.evaluate(() => {
      const b = document.getElementById('preview-banner');
      return !!b && !b.hidden;
    });
    if (bannerShown) fail('preview banner hidden for a local file', 'banner is visible');
    else pass('preview banner hidden for a local file');
  }

  // 3. Top-bar search.
  currentStep = 'search';
  console.log('\n[search] does the search bar return results for "' + SMOKE_SEARCH + '"?');
  try {
    await page.waitForSelector('#search-input', { timeout: 10_000 });
    // Wait for the search manifest before typing. doSearch() returns silently
    // while the manifest is still loading, so on a 22 GB ZIM the keystrokes
    // could all land before it was ready and nothing ever rendered — the
    // likely real cause of central-asia's 2026-09-11 timeout. The placeholder
    // flips to "Search N places..." (or "Search unavailable") once it loads.
    await page.waitForFunction(() => {
      const i = document.getElementById('search-input');
      const ph = (i && i.placeholder) || '';
      return /^Search [\d,.\s\u00a0\u202f]+ places/.test(ph) || ph === 'Search unavailable';
    }, { timeout: 120_000 });
    const ph = await page.evaluate(() => document.getElementById('search-input').placeholder);
    if (ph === 'Search unavailable') throw new Error('search manifest failed to load');
    await page.click('#search-input');
    await page.type('#search-input', SMOKE_SEARCH, { delay: 30 });
    // Require a REAL result row. This used to wait for any child of
    // #search-results, but the viewer renders a "Searching…" placeholder row
    // immediately and a "No results found" row on a miss — both are children —
    // so the check could not fail: korea-mongolia "passed" searching Palo Alto.
    await page.waitForFunction(() => {
      const r = document.getElementById('search-results');
      if (!r) return false;
      if (r.querySelector('.search-no-results')) return true;
      return Array.from(r.querySelectorAll('.search-result'))
        .some(el => el.textContent.trim() !== 'Searching…');
    }, { timeout: 60_000 });
    const res = await page.evaluate(() => {
      const r = document.getElementById('search-results');
      const rows = Array.from(r.querySelectorAll('.search-result'))
        .filter(el => el.textContent.trim() !== 'Searching…');
      return { none: !!r.querySelector('.search-no-results'), n: rows.length,
               first: rows[0] ? rows[0].textContent.trim().slice(0, 60) : '' };
    });
    if (res.none || res.n === 0) {
      throw new Error('no results for "' + SMOKE_SEARCH + '"');
    }
    pass('search results', res.n + ' rows — first: ' + res.first);
  } catch (e) {
    fail('search', e.message);
  }
  // Clear the search input so it doesn't bleed into the next step.
  await page.evaluate(() => {
    const i = document.getElementById('search-input');
    if (i) { i.value = ''; i.blur(); }
    const r = document.getElementById('search-results');
    if (r) r.style.display = 'none';
  });

  // 4. Find page → chip click → results.
  currentStep = 'find';
  console.log('\n[find] navigate to places.html, click Restaurants chip...');
  // Every Find-chip file the page pulls, with its size. Geo-sharded
  // chips (cloud/chip_shards.py) must never make a phone parse a whole
  // chip — east-coast-us Shops was 147 MB of JSON as name-hash buckets.
  const chipFetches = { files: 0, bytes: 0, pending: [] };
  try {
    page.on('response', resp => {
      const u = resp.url();
      if (u.includes('search-data') || u.includes('category-index') ||
          u.includes('routing-data')) {
        console.log('    network:', resp.status(), u);
      }
      if (/category-index\/chip-[^/]+\.json/.test(u) && resp.status() === 200) {
        chipFetches.files++;
        const len = parseInt((resp.headers() || {})['content-length'] || '', 10);
        if (Number.isFinite(len)) chipFetches.bytes += len;
        else chipFetches.pending.push(resp.buffer()
          .then(b => { chipFetches.bytes += b.length; }).catch(() => {}));
      }
    });
    // #lat/#lon → places.html uses viewport-origin mode and skips
    // the GPS prompt that would deny in headless and bail
    // runChipQuery before any results render.
    await page.goto(SITE + '/drive/viewer/places/?bust=' + Date.now() +
      '#lat=37.4419&lon=-122.143',
      { waitUntil: 'domcontentloaded' });
    await new Promise(r => setTimeout(r, 4000));
    const initDiag = await page.evaluate(() => ({
      title: document.title,
      chips: document.querySelectorAll('nav.chips button').length,
      status: (document.getElementById('status') || {}).textContent || '',
      HERE: location.pathname.replace(/[^/]+\/?$/, ''),
      manifests: typeof state !== 'undefined' ? {
        search: !!state.manifests.search,
        cat: !!state.manifests.cat,
      } : 'state undefined',
    }));
    console.log('    init-diag:', JSON.stringify(initDiag));
    // Find a chip with id ending in "restaurants" — places.html uses
    // role-style chip buttons.
    try {
      await page.waitForSelector('nav.chips button', { timeout: 10_000 });
    } catch (e) {
      // Diagnostic: dump status + URL + a few obvious DOM bits.
      const diag = await page.evaluate(() => ({
        url: location.href,
        title: document.title,
        status: (document.getElementById('status') || {}).textContent || '',
        chipsHtml: (document.getElementById('chips') || {}).outerHTML ?
          (document.getElementById('chips').outerHTML.slice(0, 200)) : null,
        bodyStart: document.body.innerHTML.slice(0, 200),
      }));
      console.log('  diag:', JSON.stringify(diag));
      throw e;
    }
    const chipButtons = await page.$$('nav.chips button');
    // "Food & Drink" since the 2026-09-16 merge, "Restaurants" on every ZIM
    // built before it. Matching one label only would fail here on the other
    // — and every later step (Gas distance, directions, route) is nested
    // inside this branch, so a miss silently skips them all.
    let restaurants = null;
    for (const btn of chipButtons) {
      const text = await page.evaluate(b => b.textContent, btn);
      if (/food|restaurant/i.test(text)) { restaurants = btn; break; }
    }
    if (!restaurants) {
      fail('find chip exists', 'no chip labelled "Food & Drink" or "Restaurants"');
    } else {
      await restaurants.click();
      await page.waitForFunction(() => {
        const list = document.getElementById('results');
        return list && list.children && list.children.length > 0;
      }, { timeout: 30_000 });
      const n = await page.evaluate(() =>
        document.getElementById('results').children.length);
      pass('find chip Restaurants returned', n + ' rows');
      await Promise.all(chipFetches.pending);
      const chipLayout = await page.evaluate(() => {
        // "food" since the 2026-09-16 merge, "restaurants" before it. Reading
        // only the old id returned null on a merged ZIM, which quietly
        // disabled the 64 MB fetch-size failure below.
        const chips = (typeof state !== 'undefined' && state.manifests.cat
                       && state.manifests.cat.chips) || {};
        const c = chips.food || chips.restaurants;
        return c ? { layout: c.layout || 'legacy', bytes: c.bytes || 0,
                     files: (c.sub_chunks || [1]).length } : null;
      });
      const mb = (x) => (x / 1048576).toFixed(1) + ' MB';
      const chipDetail = `${chipFetches.files} file(s), ${mb(chipFetches.bytes)} fetched`
        + (chipLayout ? ` of ${mb(chipLayout.bytes)} in ${chipLayout.files} (${chipLayout.layout})` : '');
      if (chipLayout && chipLayout.layout === 'geo' && chipFetches.bytes > 64 * 1048576) {
        fail('find chip fetch size', chipDetail + ' — geo shards should load a neighbourhood, not the chip');
      } else {
        pass('find chip fetch size', chipDetail);
      }

      // ----- Find-page name search, with a clock on it -----
      // Nothing used to exercise THIS page's search (the earlier step is
      // the map page's), and nothing timed it — which is how a search
      // that took 99 s and fetched 320 MB shipped unnoticed on
      // south-america. See docs/search-prefix-locality.md.
      const searchWord = (SMOKE_SEARCH || '').trim().split(/\s+/)[0] || '';
      if (searchWord.length >= 3) {
        // Turn the chip off first. rerunQuery() prefers state.chip, so typing
        // with one active just filters the chip's already-fetched records in
        // memory and never reads search-data — i.e. it would time the wrong
        // thing and pass while the slow path stayed untested.
        await page.evaluate(() => {
          const on = document.querySelector('nav.chips button.on');
          if (on) on.click();
        });
        await page.waitForFunction(
          () => typeof state === 'undefined' || !state.chip,
          { timeout: 5_000 }).catch(() => {});
        const before = Date.now();
        await page.evaluate((w) => {
          const i = document.getElementById('q');
          i.focus(); i.value = w;
          i.dispatchEvent(new Event('input', { bubbles: true }));
        }, searchWord);
        let searched = true;
        try {
          await page.waitForFunction(() => {
            const st = document.getElementById('status-text');
            const list = document.getElementById('results');
            if (!st) return false;
            const t = st.textContent || '';
            if (/^\s*Searching/i.test(t)) return false;
            return /match|nearest|No |Couldn/i.test(t)
                   || (list && list.children.length > 0);
          }, { timeout: SEARCH_BUDGET_MS });
        } catch (e) {
          searched = false;
        }
        const secs = ((Date.now() - before) / 1000).toFixed(1);
        const rows = await page.evaluate(() =>
          document.getElementById('results').children.length);
        const status = await page.evaluate(() => {
          const st = document.getElementById('status-text');
          return st ? st.textContent.replace(/\s+/g, ' ').trim().slice(0, 60) : '';
        });
        if (!searched) {
          fail('find page search', `"${searchWord}" still searching after `
            + `${SEARCH_BUDGET_MS / 1000}s — status "${status}"`);
        } else if (rows === 0) {
          // The smoke search term is a place in this region, so zero rows is
          // a failure however politely the page words it — "No matches."
          // used to satisfy the status check and pass with an empty list.
          fail('find page search', `"${searchWord}" rendered no rows — status "${status}"`);
        } else {
          pass('find page search', `"${searchWord}" → ${rows} rows in ${secs}s`);
        }
        // Clear it so the later chip steps start from a clean page.
        await page.evaluate(() => {
          const i = document.getElementById('q');
          i.value = '';
          i.dispatchEvent(new Event('input', { bubbles: true }));
        });
      }

      // ----- city-pinned chip filter -----
      // Type a city into "Search near", click the typeahead pick, then
      // tap the Gas chip and confirm the first result is genuinely
      // close to the city (not still sorted by some other key like
      // GPS-default or alphabetical). Without this, Find could ship
      // looking healthy while ignoring the user's chosen origin.
      const nearCity = pickRoutePair(ZIM_URL).o.label;
      console.log('    near-city:', nearCity);
      await page.evaluate((c) => {
        const inp = document.getElementById('near-input');
        inp.focus();
        inp.value = c;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
      }, nearCity);
      let nearReady = true;
      try {
        await page.waitForFunction(() => {
          const r = document.getElementById('near-results');
          return r && !r.hidden && r.children.length > 0;
        }, { timeout: 15_000 });
      } catch (e) {
        fail('near typeahead', `no suggestions for "${nearCity}"`);
        nearReady = false;
      }
      if (nearReady) {
        // Dump the typeahead candidates so we can see what we're
        // about to click — the first match is what `pickNearResult`
        // wires into state.origin. If it's "Denver Reservoir" 200 km
        // from actual Denver, the Gas-distance assertion will read
        // as a sort failure when really it's a typeahead-rank issue.
        const candidates = await page.evaluate(() => {
          const list = document.getElementById('near-results');
          return Array.from(list.children).map(c =>
            (c.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80));
        });
        console.log('    near-candidates[0..3]:', JSON.stringify(candidates.slice(0, 3)));
        await page.click('#near-results .near-result');
        // After click, near-input value reflects the picked place name
        // and state.origin is set. Wait for both to settle.
        try {
          await page.waitForFunction((typed) => {
            const inp = document.getElementById('near-input');
            return inp && inp.value && inp.value !== typed && inp.value !== 'My location';
          }, { timeout: 5_000 }, nearCity);
        } catch (e) { /* fall through with whatever label is set */ }
        const nearLabel = await page.$eval('#near-input', el => el.value);
        const originDump = await page.evaluate(() => {
          if (typeof state === 'undefined' || !state.origin) return null;
          return { lat: state.origin.lat, lon: state.origin.lon, mode: state.originMode };
        });
        console.log('    state.origin after pick:', JSON.stringify(originDump));
        pass('near pinned origin', `-> "${nearLabel}"`);

        // Click the Gas chip — different from Restaurants so it forces
        // a re-query with the new origin.
        let gasChip = null;
        for (const btn of chipButtons) {
          const txt = await page.evaluate(b => b.textContent, btn);
          if (/^\s*(gas|fuel)/i.test(txt.trim())) { gasChip = btn; break; }
        }
        if (!gasChip) {
          fail('near chip exists', 'no Gas/Fuel chip on places.html');
        } else {
          await gasChip.click();
          try {
            // Wait for the Gas rows themselves: the list is already
            // non-empty (Restaurants, re-sorted for the new origin), so
            // "has children" alone reads the previous chip's rows.
            // Viewers before 2026-05 have #status, not #status-text, and
            // no state.resultsChipId (which names the chip on screen).
            await page.waitForFunction(() => {
              const st = document.getElementById('status-text')
                         || document.getElementById('status');
              return typeof state !== 'undefined' && state.chip
                && /gas|fuel/i.test(state.chip.id)
                && st && !/^\s*Loading/i.test(st.textContent)
                && (state.resultsChipId === undefined
                    || state.resultsChipId === state.chip.id
                    || /couldn.t load/i.test(st.textContent));
            }, { timeout: 30_000 });
            const gasStatus = await page.evaluate(() => {
              const st = document.getElementById('status-text')
                         || document.getElementById('status');
              return st ? st.textContent.trim() : '';
            });
            if (/couldn.t load/i.test(gasStatus)) throw new Error(gasStatus);
            // Read first result's distance from .meta — format is
            // "<kind> · <dist> · <city>" e.g. "Gas · 1.2 km · Tokyo"
            // or "<kind> · 230 m · …".
            const firstMeta = await page.$eval(
              '#results li .meta', el => el.textContent);
            const m = firstMeta.match(/(\d+(?:\.\d+)?)\s*(m|km)\b/);
            const distKm = m
              ? parseFloat(m[1]) * (m[2] === 'km' ? 1 : 0.001)
              : null;
            // 50 km is generous — a city's nearest gas station is
            // typically <5 km, but small regions / rural areas may
            // legitimately stretch this. We're guarding against the
            // obvious break (sort ignored, first result on the other
            // side of the bbox), not measuring map quality.
            // The URL-liveness filter (commit c950da8) drops POI records
            // whose Overture website is dead — which in sparse regions
            // (Iran/Caucasus/Central-America) thins out gas stations near
            // major cities enough that the nearest surviving Gas POI may
            // be tens of km away. Threshold loosened from 50 km to 250 km
            // to accept that side effect. CAC's 941 km result is still a
            // real bug (URL filter ate too much, OR sort regressed) and
            // gets flagged.
            const NEAR_THRESHOLD_KM = 250;
            if (distKm == null) {
              fail('near chip distance',
                `first Gas result has no distance — meta="${firstMeta}"`);
            } else if (distKm > NEAR_THRESHOLD_KM) {
              fail('near chip distance',
                `first Gas result ${distKm.toFixed(1)} km from "${nearLabel}" — origin sort not applied?`);
            } else {
              pass('near chip distance',
                `first Gas result ${distKm < 1 ? Math.round(distKm * 1000) + ' m' : distKm.toFixed(1) + ' km'} from "${nearLabel}"`);
            }
            // Reset state.origin back to the viewport seed before
            // the directions step. Without this, the city-pinned
            // origin keeps the closest restaurant ~at-the-pin
            // (Kitchen 69 ~12 m from Denver), so the dir-href has
            // origin ≈ dest, and the viewer's routing trips on the
            // degenerate same-point case (`Cannot read properties
            // of undefined (reading 'lng')`). That viewer bug is
            // worth fixing separately; here we just hand the next
            // step a clean slate so it tests what it tested before.
            await page.evaluate(() => {
              if (typeof state !== 'undefined') {
                state.origin = { lat: 37.4419, lon: -122.143 };
                state.originMode = 'viewport';
                state.originLabel = 'Near (37.442, -122.143)';
                const inp = document.getElementById('near-input');
                if (inp) inp.value = state.originLabel;
              }
            });
            // Toggle Gas off, re-click Restaurants so the rerender
            // uses the restored origin.
            await gasChip.click();
            await page.waitForFunction(() => {
              const list = document.getElementById('results');
              return list && list.children && list.children.length === 0;
            }, { timeout: 5_000 }).catch(() => {});
            await restaurants.click();
            await page.waitForFunction(() => {
              const list = document.getElementById('results');
              return list && list.children && list.children.length > 0;
            }, { timeout: 30_000 });
          } catch (e) {
            fail('near chip results', `Gas chip empty: ${e.message}`);
          }
        }
      }

      // Click the first result's directions button (it's an <a> or
      // <button> inside a result <li>).
      const dirBtn = await page.$('#results li a[href*="dest="], #results li button, #results .pin-directions');
      if (dirBtn) {
        const dirHref = await page.evaluate(b => b.getAttribute('href') || '', dirBtn);
        console.log('    dir-href:', dirHref.slice(0, 120));
        // Anchors with href trigger navigation; wait for it explicitly.
        // (Click + setTimeout is racy because the new page's JS hasn't
        // necessarily attached window.streetzimRouting yet.)
        await Promise.all([
          page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 30_000 }),
          dirBtn.click(),
        ]);
        console.log('    after-click url:', page.url());
        await page.waitForFunction(
          () => window.streetzimRouting && typeof window.streetzimRouting.open === 'function',
          { timeout: 30_000 });
        // The dest input fills only after graph load completes, which
        // can take 5-15s on a fresh ZIM. queueGraphPick polls every
        // 100 ms up to 30s — wait for streetzimRouting.graphReady,
        // then for the input to actually populate.
        await page.waitForFunction(() => {
          const r = window.streetzimRouting;
          if (!r || !r.graphReady) return false;
          const i = document.getElementById('routing-dest-input');
          return i && i.value && i.value.length > 0;
        }, { timeout: 30_000 });
        const dest = await page.evaluate(() =>
          (document.getElementById('routing-dest-input') || {}).value || '');
        if (dest) pass('directions handoff filled dest input', dest.slice(0, 40));
        else fail('directions handoff', 'routing-dest-input empty');
      } else {
        fail('find directions button', 'no <a href*="dest="> in #results');
      }
    }
  } catch (e) {
    fail('find page', e.message);
  }

  // 5. Origin typeahead.
  currentStep = 'directions';
  console.log('\n[directions] type in origin field, expect suggestions...');
  try {
    await page.evaluate(() => window.streetzimRouting.open());
    await page.waitForSelector('#routing-origin-input', { timeout: 10_000 });
    // Step 4's dir-href can include a far-away origin (the hardcoded
    // Palo Alto seed), which on a small ZIM (DC) snaps to the nearest
    // in-bbox node, succeeds at routing, fitBounds zooms out, and the
    // panel auto-minimizes when its height exceeds 50% of viewport —
    // hiding #routing-body. The origin input is then in-DOM but has
    // no layout box, so page.click fails with "Node is either not
    // clickable". Defensively un-minimize before clicking.
    // Un-minimize defensively (auto-minimize fires when route fitBounds
    // makes the panel exceed 50% of viewport — common on bigger regional
    // routes like Colorado's Western-Slope-to-central-CO snap). Then set
    // input value directly + dispatch input event rather than click+type
    // — page.click/type require an interactable element, and the panel
    // can re-minimize between un-minimize and click due to a route-done
    // event landing in the same tick. Direct event dispatch bypasses
    // Puppeteer's interactability check.
    await page.evaluate(() => {
      const p = document.getElementById('routing-panel');
      if (p && p.classList.contains('minimized')) p.classList.remove('minimized');
      const i = document.getElementById('routing-origin-input');
      if (i) {
        i.focus();
        i.value = 'Mount';
        i.dispatchEvent(new Event('input', { bubbles: true }));
      }
    });
    await page.waitForFunction(() => {
      const r = document.getElementById('routing-origin-results');
      return r && r.children.length > 0;
    }, { timeout: 15_000 });
    const sugg = await page.evaluate(() =>
      document.getElementById('routing-origin-results').children.length);
    pass('origin typeahead', sugg + ' suggestions');
  } catch (e) {
    fail('origin typeahead', e.message);
  }

  // 6. Long-distance routing perf. The user complaint that motivated
  // this step: "I enter locations and it cranks (without an indicator
  // that tells me how close I am to done) and then sits there. Maybe
  // it is really slow." So time setOrigin+setDest -> route-result
  // visible, and stream the live status text every second so we can
  // see what the on-screen indicator actually says along the way.
  currentStep = 'route-perf';
  const route = pickRoutePair(ZIM_URL);
  console.log('\n[route-perf] ' + route.region + ': ' +
    route.o.label + ' → ' + route.d.label +
    ' (~' + route.crow_km + ' km crow-fly)');
  try {
    // Reset routing UI to a known state, then set both endpoints.
    // setOrigin+setDest both fires computeAndDrawRoute() (drive mode
    // is the default) — same code path the user invokes by entering
    // both fields and clicking "Drive".
    await page.evaluate(() => {
      const r = window.streetzimRouting;
      if (r && typeof r.clear === 'function') r.clear();
      r && r.open && r.open();
    });
    const t0 = Date.now();
    await page.evaluate((o, d) => {
      const r = window.streetzimRouting;
      r.setOrigin(o.lat, o.lon, o.label);
      r.setDest(d.lat, d.lon, d.label);
    }, route.o, route.d);
    let lastStatus = '';
    const statusTimer = setInterval(async () => {
      try {
        const s = await page.evaluate(() => {
          const el = document.getElementById('routing-status');
          return el ? el.textContent : '';
        });
        if (!s) return;
        // Drop the spinner glyph + sub-second elapsed time from the
        // dedup key — those tick every 100ms and would log every
        // poll. Compare on the meaningful state (label, pops, cells).
        const key = s.replace(/^.\s/, '').replace(/·\s\d+\.\d+s/, '');
        if (key === lastStatus) return;
        lastStatus = key;
        const dt = ((Date.now() - t0) / 1000).toFixed(1);
        console.log('    +' + dt + 's status: ' + s.slice(0, 110));
      } catch {}
    }, 1000);
    try {
      await page.waitForFunction(() => {
        const res = document.getElementById('routing-result');
        if (!res) return false;
        const visible = res.offsetParent !== null
          || (res.style.display && res.style.display !== 'none');
        const dist = document.getElementById('route-distance');
        return visible && dist && dist.textContent.trim().length > 0;
      }, { timeout: 360_000, polling: 500 });
      const elapsed = Date.now() - t0;
      const summary = await page.evaluate(() => {
        // window.__streetzim_lastRoute is set in computeAndDrawRoute's
        // success path. coords are MapLibre [lng, lat] pairs.
        const r = window.__streetzim_lastRoute;
        const first = (r && r.coords && r.coords[0]) || null;
        const last = (r && r.coords && r.coords[r.coords.length - 1]) || null;
        return {
          dist: (document.getElementById('route-distance') || {}).textContent || '',
          time: (document.getElementById('route-time') || {}).textContent || '',
          status: (document.getElementById('routing-status') || {}).textContent || '',
          coordCount: (r && r.coords && r.coords.length) || 0,
          first, last,
          totalKm: r && r.distance ? r.distance / 1000 : null,
        };
      });
      // Coverage: the user's standing rule — "highway-only for the
      // sketch is fine but it still needs to get me from where I
      // start to where I am going" — and the follow-up tightening:
      // "I don't want directions to get within 5 km. I want it to be
      // within 100 m or so." 100 m is roughly one block (and well
      // tighter than the typical street-grid snap). Verifies drawn
      // route's endpoints are within 0.1 km of the requested
      // origin/dest AND total distance ≥ 80 % of crow-fly.
      function haversineKm(la1, lo1, la2, lo2) {
        const R = 6371;
        const dLat = (la2 - la1) * Math.PI / 180;
        const dLon = (lo2 - lo1) * Math.PI / 180;
        const a = Math.sin(dLat/2)**2 +
          Math.cos(la1*Math.PI/180) * Math.cos(la2*Math.PI/180) *
          Math.sin(dLon/2)**2;
        return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
      }
      if (!summary.first || !summary.last) {
        fail('route coverage', 'no coords on lastRoute');
      } else {
        const startGapKm = haversineKm(
          route.o.lat, route.o.lon, summary.first[1], summary.first[0]);
        const endGapKm = haversineKm(
          route.d.lat, route.d.lon, summary.last[1], summary.last[0]);
        const minDist = route.crow_km * 0.8;
        const enoughDist = (summary.totalKm || 0) >= minDist;
        // Tightened to 100 m per user direction: "I don't want
        // directions to get within 5 km. I want it to be within 100 m
        // or so." 100 m is roughly half a city block — well within
        // the typical street-grid snap, and tight enough that a
        // regression dropping the start/end legs would surface.
        const TIGHT_M = 100;
        const startMeters = startGapKm * 1000;
        const endMeters = endGapKm * 1000;
        const tightStart = startMeters < TIGHT_M;
        const tightEnd = endMeters < TIGHT_M;
        if (tightStart && tightEnd && enoughDist) {
          pass('route coverage',
            'start gap ' + startMeters.toFixed(0) + ' m · end gap ' +
            endMeters.toFixed(0) + ' m · ' + (summary.totalKm||0).toFixed(0) +
            ' km / crow ' + route.crow_km + ' km · ' +
            summary.coordCount + ' coords');
        } else {
          fail('route coverage',
            'start gap ' + startMeters.toFixed(0) + ' m, end gap ' +
            endMeters.toFixed(0) + ' m, total ' + (summary.totalKm||0).toFixed(0) +
            ' km < ' + minDist.toFixed(0) + ' km (80% crow-fly)');
        }
      }
      pass('route ready', (elapsed / 1000).toFixed(1) + 's · ' +
        summary.dist + ' / ' + summary.time);
    } finally {
      clearInterval(statusTimer);
    }
  } catch (e) {
    const finalStatus = await page.evaluate(() =>
      (document.getElementById('routing-status') || {}).textContent || ''
    ).catch(() => '');
    fail('route-perf', e.message + ' (last status: "' + finalStatus + '")');
  }

  // Summary.
  console.log('\n=== summary ===');
  for (const e of consoleErrs) failures.push('console-error: ' + e);
  for (const e of network404s) failures.push('network-404: ' + e);
  if (failures.length === 0) {
    console.log('all checks passed (no console errors, no /drive/ 404s)');
  } else {
    console.log('FAILED:');
    for (const f of failures) console.log('  - ' + f);
  }
  await browser.close();
  process.exit(failures.length === 0 ? 0 : 1);
}

main().catch(err => { console.error(err); process.exit(2); });
