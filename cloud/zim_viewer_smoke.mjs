// In-ZIM viewer smoke — tests the copy Kiwix actually runs.
//
// cloud/pwa_smoke_test.mjs drives the website, whose service worker serves
// viewer/index.html, places.html and routing-worker.js from the SITE
// (VIEWER_SHELL_NAMES in web/drive/sw.js). So it exercises the on-disk
// viewer no matter what is inside the ZIM, and cannot detect a stale
// baked-in viewer. Kiwix has no service worker and no site: it runs the
// copy in the archive. This script serves entries straight out of the ZIM
// (cloud/serve_zim_entries.py) and asserts on that copy. CI runs it against
// the Monaco ZIM it builds (.github/workflows/ci.yml).
//
// Usage:
//   ZIM_ORIGIN=http://localhost:8899 node cloud/zim_viewer_smoke.mjs
//   SMOKE_SEARCH=Zurich ZIM_ORIGIN=... node cloud/zim_viewer_smoke.mjs
//   SMOKE_NO_CHIPS=1 ...  # the ZIM was built without --split-find-chips
//   EXPECT_FIXES=0 ...    # invert: assert the viewer is the OLD one
//                         # (the control that proves this test can fail)

import puppeteer from 'puppeteer';

const ORIGIN = process.env.ZIM_ORIGIN || 'http://localhost:8899';
const SMOKE_SEARCH = process.env.SMOKE_SEARCH || 'Zurich';
const EXPECT_FIXES = process.env.EXPECT_FIXES !== '0';
const HEADFUL = process.env.HEADFUL === '1';
// A build made without --split-find-chips: expect no chips and no rail.
const NO_CHIPS = process.env.SMOKE_NO_CHIPS === '1';
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
  // CI runners sometimes take over puppeteer's 30 s default just to start
  // Chrome (seen once: the ZIM had validated and no test had run yet).
  timeout: 120_000,
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
  // The empty states carry data-wiki-empty ("loading" / "none") since
  // 2026-10, when their text became the map's language; the English
  // patterns cover older viewers.
  const wiki = await page.evaluate(() => {
    const txt = (document.body.innerText || '');
    const state = s => !!document.querySelector('[data-wiki-empty="' + s + '"]');
    return {
      falseEmpty: state('none') || /no wikipedia/i.test(txt),
      loadingOrLoaded: state('loading') || /loading wikipedia/i.test(txt) ||
        !!(window.WIKI_GEO_INDEX && Object.keys(window.WIKI_GEO_INDEX).length),
      indexSize: window.WIKI_GEO_INDEX ? Object.keys(window.WIKI_GEO_INDEX).length : -1,
    };
  });
  // A ZIM built without --wikidata has no geo-index to load.
  const hasWikidata = await page.evaluate(() =>
    fetch('map-config.json').then(r => r.json()).then(c => !!c.hasWikidata)
      .catch(() => true));
  if (EXPECT_FIXES) {
    ok('no false "no Wikipedia places" state', !wiki.falseEmpty,
       'sidebar text claims no Wikipedia entries');
    if (hasWikidata) {
      ok('wiki geo-index loaded or loading', wiki.loadingOrLoaded,
         'index size=' + wiki.indexSize);
    } else {
      console.log('skip  wiki geo-index (map-config.json: hasWikidata is false)');
    }
  } else {
    console.log('note  (control) wiki falseEmpty=' + wiki.falseEmpty +
                ' indexSize=' + wiki.indexSize);
  }

  // ---- 4b. credits list only the layers this ZIM has --------------------
  if (EXPECT_FIXES) {
    const credits = await page.evaluate(() =>
      fetch('map-config.json').then(r => r.json()).then(c => {
        const shown = id => {
          const el = document.getElementById(id);
          return el ? el.style.display !== 'none' : null;
        };
        return [
          ['attr-satellite-section', !!c.hasSatellite, shown('attr-satellite-section')],
          ['attr-terrain-section', !!c.hasTerrain, shown('attr-terrain-section')],
          ['attr-wiki-section', !!(c.hasWikidata || c.hasWikiArticles), shown('attr-wiki-section')],
        ];
      }).catch(e => [['map-config.json', true, String(e)]]));
    for (const [id, want, got] of credits) {
      ok(`credits: ${id} ${want ? 'shown' : 'hidden'}`, got === want,
         `map-config says ${want}, section shown=${got}`);
    }
  }

  // ---- 5. the chip rail is the current one -------------------------------
  // The viewer shows exactly the chips the ZIM's category manifest lists
  // with records (count > 0; Food & Drink also stands for a pre-merge
  // restaurants/cafes pair) and hides the whole rail when it lists none.
  // So the expectation comes from the manifest, not a fixed list: the
  // weekly CI build uses live OSM data, and a category that drops to 0
  // there is a data change, not a viewer bug. Both CI builds use
  // --split-find-chips, so a manifest with no chips fails unless the
  // caller says the build has none (SMOKE_NO_CHIPS=1) — that is how the
  // 2026-04 Japan build shipped without chips.
  const counts = await page.evaluate(async () => {
    try {
      const r = await fetch('category-index/manifest.json');
      if (!r.ok) return { status: r.status };
      const m = await r.json();
      if (!m || !m.chips) return { chips: null };
      const out = {};
      for (const [id, meta] of Object.entries(m.chips)) {
        out[id] = meta && typeof meta.count === 'number' ? meta.count : 1;
      }
      return { chips: out };
    } catch (e) { return { error: String(e) }; }
  });
  const hasChips = !!(counts.chips && Object.keys(counts.chips).length);
  if (EXPECT_FIXES) {
    ok(NO_CHIPS ? 'manifest lists no chips (SMOKE_NO_CHIPS=1)'
                : 'manifest lists Find chips (--split-find-chips build)',
       hasChips === !NO_CHIPS, JSON.stringify(counts).slice(0, 300));
    await page.waitForFunction(() => typeof _findCatManifest === 'undefined'
      || _findCatManifest !== null, { timeout: 30_000 }).catch(() => {});
    await sleep(300);
  }
  const rail = await page.evaluate(() => {
    const r = document.getElementById('find-chips');
    const all = [...document.querySelectorAll('#find-chips [data-chip]')];
    return {
      shown: !!r && !r.hidden && r.offsetParent !== null,
      ids: all.map(e => e.getAttribute('data-chip')),
      visible: all.filter(e => e.offsetParent !== null).map(e => e.getAttribute('data-chip')),
    };
  });
  const chips = await page.evaluate(() => {
    const ids = new Set();
    for (const el of document.querySelectorAll(
        '[data-chip],[data-chip-id],.chip,.explore-chip,button')) {
      if (el.offsetParent === null) continue;   // hidden: not offered
      const id = el.getAttribute('data-chip') || el.getAttribute('data-chip-id');
      if (id) ids.add(id.toLowerCase());
      const t = (el.textContent || '').trim().toLowerCase();
      if (t && t.length < 24) ids.add(t);
    }
    return [...ids];
  });
  const chipBlob = chips.join('|');
  if (EXPECT_FIXES) {
    const n = (id) => (counts.chips && counts.chips[id]) || 0;
    const alts = { food: ['food', 'restaurants', 'cafes'] };
    const want = rail.ids.filter(id => (alts[id] || [id]).some(a => n(a) > 0));
    ok('chip rail ' + (want.length ? 'shown' : 'hidden') + ' as the manifest says',
       rail.shown === want.length > 0, 'rail shown=' + rail.shown);
    ok('visible chips are exactly those with records',
       JSON.stringify(rail.visible) === JSON.stringify(want),
       'visible ' + rail.visible.join(',') + ' / expected ' + want.join(','));
    for (const c of REQUIRED_CHIPS) {
      if (n(c) > 0) {
        ok('chip rail offers "' + c + '"', rail.visible.includes(c),
           'chips seen: ' + rail.visible.join(','));
      } else if (hasChips) {
        console.log('skip  chip "' + c + '" (0 records in this build; hidden, as it should be)');
      }
    }
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
      // By chip id first: since 2026-10 the label is in the map's language.
      const els = [...document.querySelectorAll('button,[data-chip],[data-chip-id],[data-cat],.chip')];
      const t = els.find(e => /^(food|restaurants)$/.test(e.getAttribute('data-chip')
                                                          || e.getAttribute('data-cat') || '')) ||
                els.find(e => /food|drink|restaurant/i.test(e.textContent || '') ||
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
