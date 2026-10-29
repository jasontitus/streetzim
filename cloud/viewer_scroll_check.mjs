// The in-ZIM viewer must never scroll the page itself: html and body are
// overflow:hidden, but scrollIntoView(), focus() and the iOS keyboard can
// still move them programmatically, and then the search bar is off screen
// with no way to scroll back; and the "Search this area" button that comes
// up when the map is panned away from results used to hide the search bar
// until the results were closed (a user on Taipei, 2026-10-29). This drives
// a phone-sized viewer: a Find chip, taps on result pins and the arrow keys
// (each moves the result strip and flies the map to the result, which
// brings up that button), and a search, and reports the page's scroll
// offsets and where the search box is after each step.
//
//   ZIM_ORIGIN=http://127.0.0.1:8899 node cloud/viewer_scroll_check.mjs
//   (cloud/serve_zim_entries.py serves the ZIM's own copy of the viewer)
// PASS: every offset 0, and the search box shown and fully on screen after
// every step, with the button (when up) below it, not over it.
// PLAYWRIGHT_CORE, CHROME_PATH, BROWSER_LIBS: as tools/langpack/builder_e2e.mjs.
if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const ORIGIN = process.env.ZIM_ORIGIN || 'http://127.0.0.1:8899';
const SEARCH = process.env.SMOKE_SEARCH || '';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true });
const ctx = await browser.newContext({
  viewport: { width: 390, height: 664 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2,
  userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
});
const pg = await ctx.newPage();
const errors = [];
pg.on('pageerror', (e) => errors.push(e.message));
const steps = [];
const measure = (step) => pg.evaluate((step) => {
  const de = document.documentElement, b = document.body, box = document.getElementById('search-container');
  const r = box ? box.getBoundingClientRect() : null;
  const pill = document.getElementById('find-search-area-btn');
  const pr = pill ? pill.getBoundingClientRect() : null;
  return { step, winX: window.scrollX, winY: window.scrollY, htmlTop: de.scrollTop, htmlLeft: de.scrollLeft,
           bodyTop: b.scrollTop, bodyLeft: b.scrollLeft,
           box: r && { top: Math.round(r.top), left: Math.round(r.left), right: Math.round(r.right), bottom: Math.round(r.bottom),
                       shown: r.width > 0 && r.height > 0 },
           pill: pr && { top: Math.round(pr.top) }, width: innerWidth };
}, step);
await pg.goto(ORIGIN + '/');
await pg.waitForFunction(() => window.__szMap && window.__szMap.loaded(), null, { timeout: 120000 }).catch(() => {});
await sleep(3000);
steps.push(await measure('loaded'));

// A Find chip: the result strip and a pin per result
const chip = await pg.$('#find-chips [data-chip]');
if (chip) {
  await chip.tap();
  await pg.waitForFunction(() => document.querySelectorAll('#find-results-strip-track > *').length > 1, null, { timeout: 60000 }).catch(() => {});
  await sleep(1500);
  steps.push(await measure('chip results'));
  const pins = await pg.$$('.maplibregl-marker');
  for (const [i, pin] of pins.slice(0, 6).entries()) {
    try { await pin.tap({ timeout: 3000 }); } catch (e) { continue; }
    await sleep(700);
    steps.push(await measure(`pin ${i}`));
  }
  const track = await pg.$('#find-results-strip-track');
  if (track) {
    await track.focus().catch(() => {});
    for (let i = 0; i < 4; i++) { await pg.keyboard.press('ArrowRight'); await sleep(500); }
    steps.push(await measure('arrow keys'));
  }
}
if (SEARCH) {
  await pg.goto(ORIGIN + '/');
  await sleep(4000);
  await pg.fill('#search-input', SEARCH);
  await sleep(2500);
  for (let i = 0; i < 6; i++) { await pg.keyboard.press('ArrowDown'); await sleep(150); }
  steps.push(await measure('search list'));
}
const bad = steps.filter((s) => s.winX || s.winY || s.htmlTop || s.htmlLeft || s.bodyTop || s.bodyLeft
  || !s.box || !s.box.shown || s.box.top < 0 || s.box.left < 0 || s.box.right > s.width
  || (s.pill && s.pill.top < s.box.bottom));
if (!steps.some((s) => s.pill)) bad.push({ step: '-', why: 'the "Search this area" button never came up' });
console.log(JSON.stringify({ pass: bad.length === 0 && steps.length >= 3, steps: steps.length, bad, all: steps, errors }));
await browser.close();
process.exit(bad.length === 0 && steps.length >= 3 ? 0 : 1);
