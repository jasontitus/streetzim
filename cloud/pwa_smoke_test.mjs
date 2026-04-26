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
  'japan': {
    o: { lat: 35.6762, lon:  139.6503, label: 'Tokyo' },
    d: { lat: 33.2382, lon:  131.6126, label: 'Oita' },
    crow_km: 920,
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
};

function pickRoutePair(zimUrl) {
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
  console.log('site=' + SITE + '  zim=' + ZIM_URL);
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
    if (type !== 'error' && type !== 'warning') return;
    const text = m.text();
    console.log('  ! ' + type + ' [' + currentStep + ']:', text);
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
    console.log('  ! 404 [' + currentStep + ']' + (ours ? '' : ' (off-origin)') + ':', u);
    if (ours) network404s.push(currentStep + ': 404 ' + u);
  });
  page.on('requestfailed', req => {
    const u = req.url();
    if (!u.includes('/drive/') && !u.includes(ZIM_URL)) return;
    const reason = req.failure() && req.failure().errorText;
    console.log('  ! reqfail [' + currentStep + ']:', u, reason);
    consoleErrs.push(currentStep + ': reqfail: ' + u + ' ' + reason);
  });

  // 1. Load ZIM via picker → SW.
  currentStep = 'setup';
  console.log('\n[setup] loading ZIM into SW...');
  await page.goto(SITE + '/drive/?bust=' + Date.now(), { waitUntil: 'domcontentloaded' });
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

  // 3. Top-bar search.
  currentStep = 'search';
  console.log('\n[search] does the search bar return results for "Palo Alto"?');
  try {
    await page.waitForSelector('#search-input', { timeout: 10_000 });
    await page.click('#search-input');
    await page.type('#search-input', 'Palo Alto', { delay: 30 });
    await page.waitForFunction(() => {
      const r = document.getElementById('search-results');
      return r && r.children.length > 0;
    }, { timeout: 15_000 });
    const count = await page.evaluate(() =>
      document.getElementById('search-results').children.length);
    pass('search results', count + ' rows');
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
  try {
    page.on('response', resp => {
      const u = resp.url();
      if (u.includes('search-data') || u.includes('category-index') ||
          u.includes('routing-data')) {
        console.log('    network:', resp.status(), u);
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
    let restaurants = null;
    for (const btn of chipButtons) {
      const text = await page.evaluate(b => b.textContent, btn);
      if (/restaurants/i.test(text)) { restaurants = btn; break; }
    }
    if (!restaurants) {
      fail('find chip exists', 'no chip with "Restaurants" label');
    } else {
      await restaurants.click();
      await page.waitForFunction(() => {
        const list = document.getElementById('results');
        return list && list.children && list.children.length > 0;
      }, { timeout: 30_000 });
      const n = await page.evaluate(() =>
        document.getElementById('results').children.length);
      pass('find chip Restaurants returned', n + ' rows');

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
    await page.click('#routing-origin-input');
    await page.evaluate(() => {
      const i = document.getElementById('routing-origin-input');
      if (i) i.value = '';
    });
    await page.type('#routing-origin-input', 'Mount', { delay: 40 });
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
