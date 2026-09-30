// Find results: "Search this area" and chip taps search the view the reader
// chose without moving it, show only pins the reader can see, and results
// from elsewhere (a name search, a chip that fell back) are still framed.
//
// A chip tap, then "Search this area", each re-framed the results with
// fitBounds(maxZoom 14): a reader at zoom 16 was pulled out to 14 on every
// tap (16 times the area). Loads the viewer from a ZIM served by
// kiwix-serve and checks, at each zoom in ZOOMS:
//   1. tapping the chip leaves the camera where it was, and every result
//      pin is in the visible area (below the chip rail, above the strip);
//   2. after the reader moves, the "Search this area" pill appears, and
//      tapping it re-renders the results without moving the camera;
// and once:
//   3. tapping Gas (which falls back to the nearest stations and flies
//      there) then Food & Drink 100 ms later leaves the food pins visible;
//   4. a name-search hand-off (a stash with one far result) still moves
//      the camera to show it, at zoom 14 at most;
//   5. on a rotated map (bearing 45) every chip pin is on screen;
//   6. Food & Drink after another chip, on a landscape phone, and "Search
//      this area" on a name search's results leave no pin under the
//      strip, the search box, the chip rail or the map controls, or with
//      its body off the top of the screen;
//   7. with GEO_ORIGIN (the same map repacked with its chips as
//      geographic shards, e.g. `--chip-shard-mb 0.003`), Food & Drink
//      shows as many pins there as in the single-file layout, portrait
//      and landscape, with the cap on results (300) lowered to 8.
//
//   ZIM_ORIGIN=http://127.0.0.1:8902/content/<book> CHROME_PATH=... \
//     node tools/search_area_check.mjs
//
// START / PAN / FAR ("lon,lat") and ZOOMS default to Monaco (the CI ZIM);
// START must have Food & Drink in view. Prints "SEARCH AREA OK" when every
// check passed; exits 1 otherwise.
import puppeteer from 'puppeteer-core';

const origin = process.env.ZIM_ORIGIN;
if (!origin) { console.error('ZIM_ORIGIN is required'); process.exit(2); }
const pt = (s, d) => (s || d).split(',').map(Number);
const START = pt(process.env.START, '7.4246,43.7396');   // Monte Carlo
const PAN = pt(process.env.PAN, '7.4200,43.7355');       // towards the port
const FAR = pt(process.env.FAR, '7.4160,43.7310');       // Fontvieille
const ZOOMS = (process.env.ZOOMS || '15,16').split(',').map(Number);
const GEO_ORIGIN = process.env.GEO_ORIGIN || '';  // optional: the same map, chips as geo shards

const sleep = ms => new Promise(r => setTimeout(r, ms));
const idle = p => p.evaluate(() => new Promise(res => {
  const m = window.__szMap;
  if (!m.isMoving() && m.loaded()) setTimeout(res, 50);
  m.once('idle', () => setTimeout(res, 50));
  setTimeout(res, 6000);
}));
const settle = async p => { await sleep(1500); await idle(p); };
const view = p => p.evaluate(() => {
  const m = window.__szMap, b = m.getBounds(), c = m.getCenter();
  return { z: m.getZoom(), w: b.getEast() - b.getWest(), h: b.getNorth() - b.getSouth(),
           lng: c.lng, lat: c.lat };
});
const same = (a, b) => Math.abs(a.z - b.z) < 0.01 && Math.abs(a.w * a.h / (b.w * b.h) - 1) < 0.01
  && Math.abs(a.lng - b.lng) < a.w * 0.01 && Math.abs(a.lat - b.lat) < a.h * 0.01;
// Result pins: how many, and how many the reader cannot see: off the
// screen's sides, with the pin's body (PIN_H px above its tip) off the
// top, at or below the results strip, or with the tip under the search box
// / chip rail, the pill, the layer buttons or MapLibre's top corner
// controls (each element's own rectangle). Computed here rather than with
// the viewer's own helper, so the old viewer is measured the same way.
const PIN_H = 36;
const pins = p => p.evaluate(PIN_H => {
  const m = window.__szMap, cv = m.getCanvas(), cr = cv.getBoundingClientRect();
  const covers = [];
  const els = ['search-container', 'find-search-area-btn', 'controls'].map(id => document.getElementById(id))
    .concat([...document.querySelectorAll(
      '.maplibregl-ctrl-top-left > .maplibregl-ctrl, .maplibregl-ctrl-top-right > .maplibregl-ctrl')]);
  for (const el of els) {
    const r = el && el.getBoundingClientRect();
    if (r && r.height > 0 && r.width > 0) {
      covers.push({ x0: r.left - cr.left, y0: r.top - cr.top, x1: r.right - cr.left, y1: r.bottom - cr.top });
    }
  }
  const strip = document.getElementById('find-results-strip');
  const sr = strip && strip.getBoundingClientRect();
  const bottom = sr && sr.height > 0 ? sr.top - cr.top : cv.clientHeight;
  const mk = ((typeof _findResultsState !== 'undefined' && _findResultsState.markers) || []).filter(Boolean);
  const hidden = mk.filter(k => {
    const q = m.project(k.getLngLat());
    if (q.x < 8 || q.x > cv.clientWidth - 8 || q.y < PIN_H || q.y > bottom) return true;
    return covers.some(c => q.x >= c.x0 && q.x <= c.x1 && q.y >= c.y0 && q.y <= c.y1);
  }).length;
  const label = document.querySelector('#find-results-strip span');
  return { n: mk.length, hidden, label: label ? label.textContent : '' };
}, PIN_H);
const openPage = async (browser, w = 390, h = 844, from = origin) => {
  const p = await browser.newPage();
  await p.setViewport({ width: w, height: h, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  await p.goto(from + '/index.html', { waitUntil: 'load', timeout: 120000 });
  await p.waitForFunction(() => window.__szMap && window.__szMap.loaded()
    && document.querySelector('#find-chips .find-chip[data-chip="food"]'), { timeout: 120000 });
  return p;
};
const jump = (p, c, z) => p.evaluate((c, z) => window.__szMap.jumpTo({ center: c, zoom: z }), c, z);
const tap = (p, chip) => p.evaluate(c => document.querySelector(
  '#find-chips .find-chip[data-chip="' + c + '"]').click(), chip);

const browser = await puppeteer.launch({ executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox', '--disable-dev-shm-usage'], headless: 'new' });
let failed = 0;
const report = (what, errs) => {
  console.log(`${what}: ${errs.length ? 'FAIL ' + errs.join('; ') : 'ok'}`);
  if (errs.length) failed++;
};
try {
  for (const z0 of ZOOMS) {
    const p = await openPage(browser), errs = [];
    await jump(p, START, z0); await idle(p);
    const v0 = await view(p);
    await tap(p, 'food');
    await p.waitForSelector('#find-results-strip', { timeout: 60000 });
    await settle(p);
    const v1 = await view(p), r1 = await pins(p);
    if (/expanded/.test(r1.label)) errs.push('Food & Drink found nothing in view at START (choose a START with food in view)');
    if (!same(v0, v1)) errs.push(`chip tap moved the camera (z${v0.z.toFixed(2)} -> z${v1.z.toFixed(2)})`);
    if (!r1.n) errs.push('chip tap showed no pins');
    if (r1.hidden) errs.push(`${r1.hidden} of ${r1.n} pins are under the chrome or off screen after the chip tap`);
    // The reader moves to another spot, a little closer.
    await jump(p, PAN, z0 + 1); await idle(p);
    const pill = await p.waitForSelector('#find-search-area-btn', { timeout: 15000 }).then(() => true, () => false);
    if (!pill) errs.push('pill not shown after the reader moved');
    else {
      const v2 = await view(p);
      await p.evaluate(() => { document.getElementById('find-results-strip').dataset.old = '1'; });
      await p.evaluate(() => document.getElementById('find-search-area-btn').click());
      await settle(p);
      const v3 = await view(p), r3 = await pins(p);
      const rerendered = await p.evaluate(() => {
        const s = document.getElementById('find-results-strip');
        return !!s && !s.dataset.old;
      });
      if (!same(v2, v3)) errs.push(`Search this area moved the camera (z${v2.z.toFixed(2)} -> z${v3.z.toFixed(2)}, area x${(v3.w * v3.h / (v2.w * v2.h)).toFixed(1)})`);
      if (!rerendered || !r3.n) errs.push('Search this area did not re-render the results');
      if (r3.hidden) errs.push(`${r3.hidden} of ${r3.n} pins are under the chrome or off screen after Search this area`);
    }
    report(`z${z0}`, errs);
    await p.close();
  }
  {
    // A rotated map: getBounds() is the box around the rotated screen, so
    // "in view" by bounds kept pins that were off screen.
    const p = await openPage(browser), errs = [];
    await p.evaluate(c => window.__szMap.jumpTo({ center: c, zoom: 16, bearing: 45 }), START);
    await idle(p);
    await tap(p, 'food');
    await p.waitForSelector('#find-results-strip', { timeout: 60000 });
    await settle(p);
    const r = await pins(p);
    if (!r.n) errs.push('no pins');
    else if (r.hidden) errs.push(`${r.hidden} of ${r.n} pins not visible`);
    report('rotated map (bearing 45)', errs);
    await p.close();
  }
  {
    // Tap Gas (falls back to the nearest stations and flies there), then
    // Food & Drink before that flight ends.
    const p = await openPage(browser), errs = [];
    await jump(p, START, 17); await idle(p);
    // Tap Food & Drink while the Gas camera flight is under way: wait for
    // the Gas results (their flight starts with them), then tap.
    await tap(p, 'fuel');
    await p.waitForFunction(() => /Gas/.test((document.querySelector('#find-results-strip span') || {}).textContent || ''),
      { timeout: 30000 });
    await tap(p, 'food');
    await settle(p);
    const r = await pins(p);
    if (!r.n) errs.push('no food pins');
    else if (r.hidden) errs.push(`${r.hidden} of ${r.n} food pins not visible`);
    report('Gas then Food & Drink 100 ms later', errs);
    await p.close();
  }
  {
    // Another chip's strip is up (without a sub-filter row, shorter than
    // Food & Drink's will be): the food pins must clear the new strip.
    const p = await openPage(browser), errs = [];
    await jump(p, START, 16); await idle(p);
    await tap(p, 'hotels');
    await p.waitForSelector('#find-results-strip', { timeout: 60000 }); await settle(p);
    await jump(p, START, 16); await idle(p);
    await tap(p, 'food'); await settle(p);
    const r = await pins(p);
    if (!r.n) errs.push('no food pins');
    else if (r.hidden) errs.push(`${r.hidden} of ${r.n} food pins under the strip or chrome`);
    report('Hotels then Food & Drink', errs);
    await p.close();
  }
  for (const [w, h] of [[844, 390], [740, 360]]) {
    // A landscape phone: the search box covers the middle of the top, the
    // strip half the height. The chip must still find food in view (not
    // fall back and zoom out), and so must "Search this area".
    const p = await openPage(browser, w, h), errs = [];
    await jump(p, START, 16); await idle(p);
    const v0 = await view(p);
    await tap(p, 'food');
    await p.waitForSelector('#find-results-strip', { timeout: 60000 }); await settle(p);
    const r = await pins(p), v1 = await view(p);
    if (/expanded/.test(r.label)) errs.push('the chip fell back to the nearest (found nothing in view)');
    if (!r.n) errs.push('no pins');
    if (r.hidden) errs.push(`${r.hidden} of ${r.n} pins under the strip or chrome`);
    if (!same(v0, v1)) errs.push('the chip tap moved the camera');
    // A little closer on the same dense spot (enough for the pill). At z17
    // a 360 px landscape screen shows too little map under the strip for
    // food to be certain there.
    await jump(p, START, 16.6); await idle(p);
    const pill = await p.waitForSelector('#find-search-area-btn', { timeout: 15000 }).then(() => true, () => false);
    if (pill) {
      await p.evaluate(() => { document.getElementById('find-results-strip').dataset.old = '1'; });
      await p.evaluate(() => document.getElementById('find-search-area-btn').click());
      await settle(p);
      const r2 = await pins(p);
      const redrawn = await p.evaluate(() => !document.getElementById('find-results-strip').dataset.old);
      if (!redrawn || !r2.n) errs.push('Search this area found nothing in a dense spot');
      if (r2.hidden) errs.push(`${r2.hidden} of ${r2.n} pins hidden after Search this area`);
    } else errs.push('pill not shown');
    report(`landscape ${w}x${h}`, errs);
    await p.close();
  }
  if (GEO_ORIGIN) {
    // The same map with its chips as geographic shards (GEO_ORIGIN) finds
    // as many food pins the reader can see as the single-file layout: the
    // shard loader took the 300 nearest in a box that included the part
    // under the search box, and dropping those afterwards left 46 of 300
    // on a landscape phone.
    const errs = [];
    const q = await openPage(browser, 390, 844, GEO_ORIGIN);
    const layout = await q.evaluate(async () => {
      const r = await fetch('category-index/manifest.json').then(x => x.json(), () => null);
      return r && r.chips && r.chips.food ? r.chips.food.layout || 'single' : 'none';
    });
    await q.close();
    if (layout !== 'geo') errs.push(`GEO_ORIGIN's Food & Drink is not geographic shards (layout ${layout})`);
    else {
      for (const [w, h, z] of [[390, 844, 15], [844, 390, 15], [844, 390, 14], [740, 360, 14]]) {
        const n = [];
        for (const from of [origin, GEO_ORIGIN]) {
          const p = await openPage(browser, w, h, from);
          // A small map has fewer than 300 in view: a cap of 8 makes the
          // nearest (the ones next to the search box) decide the count.
          await p.evaluate(() => { FIND_RESULTS_MAX = 8; });
          await jump(p, START, z); await idle(p);
          await tap(p, 'food');
          await p.waitForSelector('#find-results-strip', { timeout: 60000 }); await settle(p);
          const r = await pins(p);
          if (r.hidden) errs.push(`${w}x${h} z${z} ${from === origin ? 'single-file' : 'geo'}: ${r.hidden} of ${r.n} pins hidden`);
          n.push(r.n);
          await p.close();
        }
        console.log(`  ${w}x${h} z${z}: single-file ${n[0]}, geo ${n[1]}`);
        if (n[1] < Math.min(n[0], 8)) errs.push(`${w}x${h} z${z}: geo shards show ${n[1]} pins, single-file ${n[0]}`);
      }
    }
    report('geographic shards find as many as one file', errs);
  }
  {
    // "Search this area" on a name search's results (no chip): the pill
    // hides the search box while it is up; the pins must clear it anyway.
    const p = await openPage(browser), errs = [];
    await jump(p, START, 14); await idle(p);
    const n0 = await p.evaluate(() => {
      const m = window.__szMap, b = m.getBounds(), items = [];
      for (let i = 0; i < 400; i++) {
        items.push({ n: 'Place ' + i, t: 'poi',
          a: b.getSouth() + (b.getNorth() - b.getSouth()) * ((i * 37) % 400) / 400,
          o: b.getWest() + (b.getEast() - b.getWest()) * ((i * 91) % 400) / 400 });
      }
      sessionStorage.setItem(FIND_RESULTS_STASH_KEY, JSON.stringify({ label: 'Name search', origin: null, items }));
      renderFindResultsFromStash(m);
      return items.length;
    });
    await settle(p);
    await jump(p, START, 16); await idle(p);
    const pill = await p.waitForSelector('#find-search-area-btn', { timeout: 15000 }).then(() => true, () => false);
    if (!pill) errs.push('pill not shown');
    else {
      await p.evaluate(() => document.getElementById('find-search-area-btn').click());
      await settle(p);
      const r = await pins(p);
      if (!r.n || r.n >= n0) errs.push(`Search this area kept ${r.n} of ${n0}`);
      if (r.hidden) errs.push(`${r.hidden} of ${r.n} pins under the search box, chips or strip`);
    }
    report('Search this area on name-search results', errs);
    await p.close();
  }
  {
    // A name search handed over from places.html: one result far away.
    const p = await openPage(browser), errs = [];
    await jump(p, START, 16); await idle(p);
    await p.evaluate(far => {
      sessionStorage.setItem(FIND_RESULTS_STASH_KEY, JSON.stringify({
        label: 'Name search', origin: null,
        items: [{ n: 'Far place', t: 'poi', a: far[1], o: far[0] }] }));
      renderFindResultsFromStash(window.__szMap);
    }, FAR);
    await settle(p);
    const v = await view(p), r = await pins(p);
    if (r.hidden) errs.push('the handed-over result is not visible');
    if (v.z > 14.01) errs.push(`framed at z${v.z.toFixed(2)}, past 14`);
    report('name search hand-off', errs);
    await p.close();
  }
} finally {
  await browser.close();
}
if (failed) { console.log('SEARCH AREA FAILED'); process.exit(1); }
console.log('SEARCH AREA OK');
