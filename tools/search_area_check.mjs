// Search this area must search the view the reader chose, not move it.
//
// A chip tap, then "Search this area", each re-framed the results with
// fitBounds(maxZoom 14): a reader at zoom 16 was pulled out to 14 on every
// tap (16 times the area). This loads the viewer from a ZIM served by
// kiwix-serve, taps the Food & Drink chip at zoom Z, pans to another spot
// one zoom level closer, taps the pill, and checks that neither tap moved
// the camera and that the pill appeared.
//
//   ZIM_ORIGIN=http://127.0.0.1:8902/content/<book> CHROME_PATH=... \
//     node tools/search_area_check.mjs
//
// START / PAN ("lon,lat") and ZOOMS ("15,16") default to Monaco (the CI ZIM).
// Prints one line per zoom and "SEARCH AREA OK" when every check passed;
// exits 1 otherwise.
import puppeteer from 'puppeteer-core';

const origin = process.env.ZIM_ORIGIN;
if (!origin) { console.error('ZIM_ORIGIN is required'); process.exit(2); }
const pt = (s, d) => (s || d).split(',').map(Number);
const START = pt(process.env.START, '7.4246,43.7396');   // Monte Carlo
const PAN = pt(process.env.PAN, '7.4200,43.7355');       // towards the port
const ZOOMS = (process.env.ZOOMS || '15,16').split(',').map(Number);
const CHIP = process.env.CHIP || 'food';

const sleep = ms => new Promise(r => setTimeout(r, ms));
const idle = p => p.evaluate(() => new Promise(res => {
  const m = window.__szMap;
  if (!m.isMoving() && m.loaded()) setTimeout(res, 50);
  m.once('idle', () => setTimeout(res, 50));
  setTimeout(res, 6000);
}));
const view = p => p.evaluate(() => {
  const m = window.__szMap, b = m.getBounds();
  return { z: m.getZoom(), w: b.getEast() - b.getWest(), h: b.getNorth() - b.getSouth() };
});
const same = (a, b) => Math.abs(a.z - b.z) < 0.01 && Math.abs(a.w * a.h / (b.w * b.h) - 1) < 0.01;

const browser = await puppeteer.launch({ executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox', '--disable-dev-shm-usage'], headless: 'new' });
let failed = 0;
try {
  for (const z0 of ZOOMS) {
    const p = await browser.newPage();
    await p.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
    await p.goto(origin + '/index.html', { waitUntil: 'load', timeout: 120000 });
    await p.waitForFunction(c => window.__szMap && window.__szMap.loaded()
      && document.querySelector('#find-chips .find-chip[data-chip="' + c + '"]'), { timeout: 120000 }, CHIP);
    await p.evaluate((c, z) => window.__szMap.jumpTo({ center: c, zoom: z }), START, z0);
    await idle(p);
    const v0 = await view(p);
    await p.evaluate(c => document.querySelector('#find-chips .find-chip[data-chip="' + c + '"]').click(), CHIP);
    await p.waitForSelector('#find-results-strip', { timeout: 60000 });
    await sleep(1200); await idle(p);
    const v1 = await view(p);
    const expanded = await p.evaluate(() => /expanded/.test(
      (document.querySelector('#find-results-strip span') || {}).textContent || ''));
    // The reader moves to another spot, a little closer.
    await p.evaluate((c, z) => window.__szMap.jumpTo({ center: c, zoom: z }), PAN, z0 + 1);
    await idle(p);
    const pill = await p.waitForSelector('#find-search-area-btn', { timeout: 15000 }).then(() => true, () => false);
    const v2 = await view(p);
    let v3 = null;
    if (pill) {
      await p.evaluate(() => document.getElementById('find-search-area-btn').click());
      await sleep(1500); await idle(p);
      v3 = await view(p);
    }
    const errs = [];
    if (!expanded && !same(v0, v1)) errs.push(`chip tap moved the camera (z${v0.z.toFixed(2)} -> z${v1.z.toFixed(2)})`);
    if (!pill) errs.push('pill not shown after the reader moved');
    if (v3 && !same(v2, v3)) errs.push(`Search this area moved the camera (z${v2.z.toFixed(2)} -> z${v3.z.toFixed(2)}, area x${(v3.w * v3.h / (v2.w * v2.h)).toFixed(1)})`);
    console.log(`z${z0}: ${errs.length ? 'FAIL ' + errs.join('; ') : 'ok'}${expanded ? ' (chip fell back to the nearest; its camera move is expected)' : ''}`);
    failed += errs.length ? 1 : 0;
    await p.close();
  }
} finally {
  await browser.close();
}
if (failed) { console.log('SEARCH AREA FAILED'); process.exit(1); }
console.log('SEARCH AREA OK');
