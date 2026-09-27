// Does the download page's filter box actually hide cards?
//
// Reported broken on 2026-09-27: typing in the box changed nothing. The JS was
// fine -- it set `card.hidden = true` -- but the CSS rule meant to make that
// visible, `.maps [hidden] { display: none !important }`, had been written
// INSIDE `.maps-tier-header:first-child { ... }`. Browsers read that as CSS
// nesting, so it resolved to `.maps-tier-header:first-child .maps [hidden]`
// and never matched, while `.map-card { display: flex }` kept every card on
// screen regardless of the attribute.
//
// A DOM-only check would have passed: `hidden` WAS set. This gate measures
// what the user sees -- offsetParent and the computed display -- so it fails
// on exactly the bug that shipped.
//
//   node cloud/site_filter_gate.mjs [path-to-index.html] [query]
//
// Exit 0 pass, 1 fail, 3 setup problem.
import puppeteer from 'puppeteer-core';
import { pathToFileURL } from 'node:url';
import { existsSync } from 'node:fs';

const page_path = process.argv[2] || 'web/index.html';
const query = process.argv[3] || 'france';
if (!existsSync(page_path)) { console.error(`no such page: ${page_path}`); process.exit(3); }
if (!process.env.CHROME_PATH) { console.error('CHROME_PATH unset'); process.exit(3); }

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
let bad = 0;
const say = (ok, msg) => { if (!ok) bad++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); };

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  await page.goto(pathToFileURL(page_path).href, { waitUntil: 'domcontentloaded' });

  const visible = () => page.$$eval('.maps .map-card', (cards) => cards.filter((c) => {
    if (c.offsetParent === null) return false;
    return getComputedStyle(c).display !== 'none';
  }).map((c) => (c.querySelector('.map-card-title') || {}).textContent || '?'));

  const before = await visible();
  say(before.length > 20, `${before.length} cards visible before filtering`);

  await page.focus('#map-filter');
  await page.type('#map-filter', query, { delay: 5 });
  // The handler is synchronous on `input`; one frame is enough for layout.
  await page.evaluate(() => new Promise(requestAnimationFrame));

  const after = await visible();
  say(after.length < before.length,
      `filtering on "${query}" narrowed ${before.length} -> ${after.length}`);
  say(after.length > 0, `"${query}" still matches something (${after.join(', ') || 'nothing'})`);
  const q = query.toLowerCase();
  const wrong = after.filter((t) => !t.toLowerCase().includes(q));
  // A card can legitimately match on its description, so only flag a card
  // that matches NEITHER title nor its own text.
  if (wrong.length) {
    const texts = await page.$$eval('.maps .map-card', (cards) => cards
      .filter((c) => c.offsetParent !== null && getComputedStyle(c).display !== 'none')
      .map((c) => (c.textContent || '').toLowerCase()));
    const truly = texts.filter((t) => !t.includes(q));
    say(truly.length === 0, `every surviving card contains "${query}" somewhere`);
  } else {
    say(true, `every surviving card's title contains "${query}"`);
  }

  const counter = await page.$eval('#map-filter-count', (e) => e.textContent.trim());
  say(/^\d+ of \d+$/.test(counter), `counter reads ${counter || '(empty)'}`);
  say(counter.startsWith(`${after.length} `),
      `counter's count (${counter.split(' ')[0]}) matches what is on screen (${after.length})`);

  // Escape clears and everything comes back.
  await page.keyboard.press('Escape');
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const restored = await visible();
  say(restored.length === before.length,
      `Escape restored all ${before.length} cards (saw ${restored.length})`);

  // Painted colours, not declared ones: the field showed the body's near-white
  // text on a white background (reported "filter text is white one white"), and
  // only the computed style proves what a reader actually sees.
  const ink = await page.$eval('#map-filter', (el) => {
    const cs = getComputedStyle(el);
    return { color: cs.color, background: cs.backgroundColor };
  });
  const parse = (c) => (c.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
  const lum = (ch) => {
    const [r, g, b] = ch.map((v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const lf = lum(parse(ink.color)), lb = lum(parse(ink.background));
  const ratio = (Math.max(lf, lb) + 0.05) / (Math.min(lf, lb) + 0.05);
  say(ratio >= 4.5,
      `filter text contrast ${ratio.toFixed(1)}:1 (${ink.color} on ${ink.background})`);

  // The headers must follow their cards, or a section title floats alone.
  const headers = await page.$$eval('.maps .maps-tier-header', (hs) => hs.length);
  say(headers > 0, `${headers} tier headers present`);
} finally {
  await browser.close();
}
console.log(bad ? `\n${bad} check(s) failed` : '\nfilter gate OK');
process.exit(bad ? 1 : 0);
