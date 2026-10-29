// The in-ZIM viewer must keep its search bar on screen. html and body are
// overflow:hidden, but scrollIntoView(), focus() and the iOS keyboard can
// still move them programmatically, and then the search bar is off screen
// with no way to scroll back; and the "Search this area" button that comes
// up when the map moves away from results used to hide the search bar until
// the results were closed (a user on Taipei, 2026-10-29).
//
// Drives the viewer at a phone in portrait (390x664) and a small phone in
// landscape (568x320): a Find chip, moving between results with the arrow
// keys (each flies the map to the result, which brings up the button), taps
// on result pins, typing a search while the button is up, and closing the
// dropdown again. After each step it measures the page's scroll offsets,
// the search box, the chip rail, the button, the result strip and the
// dropdown. FAIL when:
//   - the page is scrolled;
//   - the search box is hidden or off screen;
//   - the button, when shown, is not below the input and the chip rail, or
//     covers the result strip (it hides where there is no room: the small
//     landscape phone);
//   - a row of the open dropdown does not take a tap at its centre (the
//     button sat on the results);
//   - the result strip calls scrollIntoView (it scrolls the page too; in
//     Chromium the page happens not to move, in WebKit it can);
//   - the button never came up.
//
//   ZIM_ORIGIN=http://127.0.0.1:8899 node cloud/viewer_scroll_check.mjs
//   (cloud/serve_zim_entries.py serves the ZIM's own copy of the viewer)
// SMOKE_SEARCH: the text typed (default "Tai"). Prints one JSON object.
// PLAYWRIGHT_CORE, CHROME_PATH, BROWSER_LIBS: as tools/langpack/builder_e2e.mjs.
if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const ORIGIN = process.env.ZIM_ORIGIN || 'http://127.0.0.1:8899';
const SEARCH = process.env.SMOKE_SEARCH || 'Tai';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true });
const report = { pass: true, runs: [] };

async function run(name, viewport) {
  const ctx = await browser.newContext({ viewport, isMobile: true, hasTouch: true, deviceScaleFactor: 2, userAgent: UA });
  await ctx.addInitScript(() => {
    const own = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (...a) {
      if (this.closest && this.closest('#find-results-strip-track')) window.__stripScrollIntoView = (window.__stripScrollIntoView || 0) + 1;
      return own.apply(this, a);
    };
  });
  const pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', (e) => errors.push(e.message));
  const steps = [];
  const measure = (step) => pg.evaluate((step) => {
    const q = (id) => document.getElementById(id);
    const rect = (el) => {
      if (!el || !el.getClientRects().length) return null;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 ? { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), right: Math.round(r.right) } : null;
    };
    const de = document.documentElement, b = document.body, box = q('search-container'), pill = q('find-search-area-btn');
    const results = q('search-results');
    let rowsHit = null;
    if (results && results.style.display === 'block') {
      // the rows the dropdown shows (its own scroll clips the rest)
      const box = results.getBoundingClientRect();
      const rows = [...results.children].filter((r) => {
        if (!r.getClientRects().length) return false;
        const c = r.getBoundingClientRect(), y = c.top + c.height / 2;
        return y > box.top && y < box.bottom && y < innerHeight;
      }).slice(0, 6);
      rowsHit = rows.map((r) => {
        const c = r.getBoundingClientRect();
        const hit = document.elementFromPoint(c.left + c.width / 2, c.top + c.height / 2);
        return !!(hit && results.contains(hit));
      });
    }
    return {
      step, scroll: [window.scrollX, window.scrollY, de.scrollTop, de.scrollLeft, b.scrollTop, b.scrollLeft],
      box: rect(box),
      input: rect(q('search-input')), chips: rect(q('find-chips')), strip: rect(q('find-results-strip')),
      pill: rect(pill), pillShown: !!(pill && getComputedStyle(pill).visibility !== 'hidden'),
      rowsHit, stripScrollIntoView: window.__stripScrollIntoView || 0, width: innerWidth, height: innerHeight,
    };
  }, step);
  await pg.goto(ORIGIN + '/');
  await pg.waitForFunction(() => window.__szMap && window.__szMap.loaded(), null, { timeout: 120000 }).catch(() => {});
  await sleep(2500);
  steps.push(await measure('loaded'));
  const chip = await pg.$('#find-chips [data-chip]');
  if (chip) {
    await chip.tap();
    await pg.waitForFunction(() => document.querySelectorAll('#find-results-strip-track > *').length > 1, null, { timeout: 60000 }).catch(() => {});
    await sleep(1500);
    steps.push(await measure('chip results'));
    const track = await pg.$('#find-results-strip-track');
    if (track) {
      await track.focus().catch(() => {});
      for (let i = 0; i < 4; i++) { await pg.keyboard.press('ArrowRight'); await sleep(600); }
      await pg.waitForFunction(() => !!document.getElementById('find-search-area-btn'), null, { timeout: 15000 }).catch(() => {});
      await sleep(400);
      steps.push(await measure('between results'));
    }
    // pins in view, clicked as a tap would (their handlers are click handlers)
    const pins = await pg.$$eval('.maplibregl-marker', (els) => els.map((e, i) => {
      const r = e.getBoundingClientRect();
      return r.top > 0 && r.bottom < innerHeight && r.left > 0 && r.right < innerWidth ? i : -1;
    }).filter((i) => i >= 0).slice(0, 4));
    for (const i of pins) {
      await pg.evaluate((i) => document.querySelectorAll('.maplibregl-marker')[i].click(), i);
      await sleep(700);
      steps.push(await measure(`pin ${i}`));
    }
    // a search typed while the button is up, then the dropdown closed
    try { await pg.fill('#search-input', SEARCH, { timeout: 3000 }); } catch (e) { /* hidden: measured below */ }
    await pg.waitForFunction(() => document.getElementById('search-results').style.display === 'block', null, { timeout: 15000 }).catch(() => {});
    await sleep(800);
    steps.push(await measure('typing'));
    await pg.keyboard.press('Escape');
    await sleep(600);
    steps.push(await measure('dropdown closed'));
  }
  const bad = [];
  for (const s of steps) {
    const why = [];
    if (s.scroll.some(Boolean)) why.push('page scrolled ' + s.scroll.join(','));
    if (!s.box) why.push('search box hidden');
    if (s.box && (s.box.top < 0 || s.box.left < 0 || s.box.right > s.width)) why.push('search box off screen');
    const above = Math.max(s.input && s.box ? s.input.bottom : 0, s.chips && s.box ? s.chips.bottom : 0);
    if (s.pill && s.pillShown && s.box && s.pill.top < above) why.push(`button at ${s.pill.top} over the input/chips (bottom ${above})`);
    if (s.pill && s.pillShown && s.strip && s.pill.bottom > s.strip.top) why.push(`button (bottom ${s.pill.bottom}) on the strip (top ${s.strip.top})`);
    if (s.rowsHit && s.rowsHit.some((h) => !h)) why.push('a dropdown row does not take its tap: ' + JSON.stringify(s.rowsHit));
    if (s.stripScrollIntoView) why.push(`the strip called scrollIntoView ${s.stripScrollIntoView}x`);
    if (why.length) bad.push({ step: s.step, why });
  }
  if (!steps.some((s) => s.pill)) bad.push({ step: '-', why: ['the "Search this area" button never came up'] });
  if (!steps.some((s) => s.step === 'typing' && s.rowsHit && s.rowsHit.length)) bad.push({ step: 'typing', why: ['no dropdown rows to tap'] });
  const ok = bad.length === 0 && steps.length >= 5 && errors.length === 0;
  report.runs.push({ name, viewport, pass: ok, bad, errors, steps });
  if (!ok) report.pass = false;
  await ctx.close();
}
await run('portrait', { width: 390, height: 664 });
await run('landscape', { width: 568, height: 320 });
console.log(JSON.stringify(report));
await browser.close();
process.exit(report.pass ? 0 : 1);
