// In-ZIM viewer smoke — tests the copy Kiwix actually runs.
//
// cloud/pwa_smoke_test.mjs drives the website, whose service worker serves
// viewer/index.html, places.html and routing-worker.js from the SITE
// (VIEWER_SHELL_NAMES in web/drive/sw.js). So it exercises the on-disk
// viewer no matter what is inside the ZIM, and cannot detect a stale
// baked-in viewer. Kiwix has no service worker and no site: it runs the
// copy in the archive. This script serves entries straight out of the ZIM
// (cloud/serve_zim_entries.py) and asserts on that copy.
//
// Usage:
//   ZIM_ORIGIN=http://localhost:8899 node cloud/zim_viewer_smoke.mjs
//   SMOKE_SEARCH=Zurich ZIM_ORIGIN=... node cloud/zim_viewer_smoke.mjs
//   EXPECT_FIXES=0 ...    # invert: assert the viewer is the OLD one
//                         # (the control that proves this test can fail)

import puppeteer from 'puppeteer';

const ORIGIN = process.env.ZIM_ORIGIN || 'http://localhost:8899';
const SMOKE_SEARCH = process.env.SMOKE_SEARCH || 'Zurich';
const EXPECT_FIXES = process.env.EXPECT_FIXES !== '0';
const HEADFUL = process.env.HEADFUL === '1';
const CHROME_PATH = process.env.CHROME_PATH ||
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

// The chips the current rail must offer, and the ones the old rail had
// that must be gone (three separate food buttons collapsed into one).
const REQUIRED_CHIPS = ['health', 'landmarks', 'libraries'];
const RETIRED_CHIPS = ['restaurants', 'cafes'];

let pass = 0, fail = 0;
function ok(name, cond, detail) {
  if (cond) { console.log('ok    ' + name); pass++; }
  else { console.log('FAIL  ' + name + (detail ? '\n        ' + detail : '')); fail++; }
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const browser = await puppeteer.launch({
  headless: !HEADFUL,
  executablePath: CHROME_PATH,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--allow-file-access-from-files'],
});

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e.message || e)));
  page.on('console', m => {
    if (m.type() === 'error' && !/404|Failed to load resource/.test(m.text()))
      errors.push('console: ' + m.text());
  });

  // ---- 1. the viewer boots at all, from inside the archive ----------------
  const resp = await page.goto(ORIGIN + '/index.html', {
    waitUntil: 'domcontentloaded', timeout: 60_000,
  });
  ok('index.html served from the ZIM', resp && resp.ok(),
     resp ? 'status ' + resp.status() : 'no response');

  const html = await page.content();

  // ---- 2. the two fixes are present in the served source -----------------
  const hasWikiFix = /retryWikiGeoIndex/.test(html) || await page.evaluate(
    () => typeof window.retryWikiGeoIndex === 'function');
  const hasChipFix = /_findResolveChipDef/.test(html);
  if (EXPECT_FIXES) {
    ok('in-ZIM viewer has the wiki geo-index retry', hasWikiFix);
    ok('in-ZIM viewer has the merged chip resolver', hasChipFix);
  } else {
    ok('CONTROL: in-ZIM viewer is the OLD one (no wiki retry)', !hasWikiFix);
    ok('CONTROL: in-ZIM viewer is the OLD one (no chip resolver)', !hasChipFix);
  }

  // ---- 3. the map actually renders ---------------------------------------
  let mapOk = false;
  try {
    await page.waitForFunction(
      () => document.querySelector('canvas.maplibregl-canvas') ||
            document.querySelector('.maplibregl-canvas'),
      { timeout: 45_000 });
    mapOk = true;
  } catch { /* reported below */ }
  ok('map canvas renders', mapOk);

  // ---- 4. the Wikipedia panel does not falsely claim "none" --------------
  // The bug: one-shot fetch of wiki-geo-index.json; if it lost the race the
  // sidebar said there were no Wikipedia places and never recovered. A fixed
  // viewer either shows the loading state or fills in.
  await sleep(6000);
  const wiki = await page.evaluate(() => {
    const txt = (document.body.innerText || '');
    return {
      falseEmpty: /no wikipedia/i.test(txt),
      loadingOrLoaded: /loading wikipedia/i.test(txt) ||
        !!(window.WIKI_GEO_INDEX && Object.keys(window.WIKI_GEO_INDEX).length),
      indexSize: window.WIKI_GEO_INDEX ? Object.keys(window.WIKI_GEO_INDEX).length : -1,
    };
  });
  if (EXPECT_FIXES) {
    ok('no false "no Wikipedia places" state', !wiki.falseEmpty,
       'sidebar text claims no Wikipedia entries');
    ok('wiki geo-index loaded or loading', wiki.loadingOrLoaded,
       'index size=' + wiki.indexSize);
  } else {
    console.log('note  (control) wiki falseEmpty=' + wiki.falseEmpty +
                ' indexSize=' + wiki.indexSize);
  }

  // ---- 5. the chip rail is the current one -------------------------------
  const chips = await page.evaluate(() => {
    const ids = new Set();
    for (const el of document.querySelectorAll(
        '[data-chip],[data-chip-id],.chip,.explore-chip,button')) {
      const id = el.getAttribute('data-chip') || el.getAttribute('data-chip-id');
      if (id) ids.add(id.toLowerCase());
      const t = (el.textContent || '').trim().toLowerCase();
      if (t && t.length < 24) ids.add(t);
    }
    return [...ids];
  });
  const chipBlob = chips.join('|');
  if (EXPECT_FIXES) {
    for (const c of REQUIRED_CHIPS)
      ok('chip rail offers "' + c + '"', chipBlob.includes(c),
         'chips seen: ' + chipBlob.slice(0, 300));
    const foodCount = RETIRED_CHIPS.filter(c => chipBlob.includes(c)).length;
    ok('old separate food chips are gone', foodCount === 0,
       'still present: ' + RETIRED_CHIPS.filter(c => chipBlob.includes(c)));
  }

  // ---- 6. Find page works from inside the ZIM ----------------------------
  let findOk = false, findDetail = '';
  try {
    await page.goto(ORIGIN + '/places.html', {
      waitUntil: 'domcontentloaded', timeout: 60_000 });
    await sleep(3000);
    const clicked = await page.evaluate(() => {
      const els = [...document.querySelectorAll('button,[data-chip],[data-chip-id],.chip')];
      const t = els.find(e => /food|drink|restaurant/i.test(e.textContent || '') ||
                              /food/i.test(e.getAttribute('data-chip') || ''));
      if (t) { t.click(); return (t.textContent || '').trim(); }
      return null;
    });
    findDetail = 'clicked=' + clicked;
    if (clicked) {
      await page.waitForFunction(
        () => document.querySelectorAll(
          '#results li, .near-result').length > 0,
        { timeout: 60_000 });
      const n = await page.evaluate(() => document.querySelectorAll(
        '#results li, .near-result').length);
      findDetail += ' results=' + n;
      findOk = n > 0;
    }
  } catch (e) { findDetail += ' ' + String(e.message || e).slice(0, 120); }
  ok('Find page returns places from the ZIM', findOk, findDetail);

  ok('no uncaught page errors', errors.length === 0, errors.slice(0, 5).join('\n        '));
} finally {
  await browser.close();
}

console.log('\n' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
