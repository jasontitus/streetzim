// Real photos through the sign reader, as a phone hands them in: the map's
// page served from its ZIM (cloud/serve_zim_entries.py, with --overlay for
// today's viewer and the reader's models), each photo given to the photo
// picker's input, and what the reader did with it: the lines read, the line
// searched for, the first results. Scoring is tools/ocr_photos.py's, which
// runs this once per map.
//
//   ZIM_ORIGIN=http://127.0.0.1:PORT PHOTOS=photos.json node tools/ocr_photos.mjs
// PHOTOS: a JSON list of {id, path}. Prints one JSON line per photo:
// {id, status, lines, field, results: [{name, shown}], ms, errors}; status
// is the reader's (searched, none, failed) or "timeout".
// PLAYWRIGHT_CORE, CHROME_PATH, BROWSER_LIBS: as tools/langpack/builder_e2e.mjs.
import fs from 'node:fs';

if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const ORIGIN = (process.env.ZIM_ORIGIN || '').replace(/\/$/, '');
const PHOTO_MS = +(process.env.PHOTO_TIMEOUT_MS || 180000);
if (!ORIGIN || !process.env.PHOTOS) {
  console.error('usage: ZIM_ORIGIN=... PHOTOS=photos.json node tools/ocr_photos.mjs');
  process.exit(2);
}
const photos = JSON.parse(fs.readFileSync(process.env.PHOTOS, 'utf8'));
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true });
let rc = 0;
try {
  // A phone: the reader works on the photo as a phone's browser decodes it.
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
                                         deviceScaleFactor: 2 });
  const pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', (e) => errors.push(e.message));
  await pg.goto(ORIGIN + '/index.html', { waitUntil: 'load', timeout: 120000 });
  await pg.waitForFunction(() => window.__szMap && window.__szMap.loaded() &&
    document.getElementById('search-input').getAttribute('data-sz-search') === 'ready', null, { timeout: 180000 });
  const button = await pg.evaluate(() => {
    const b = document.getElementById('search-photo');
    return !!b && !b.hidden;
  });
  if (!button) {
    console.log(JSON.stringify({ error: 'no camera button: map-config.json has no ocr key' }));
    process.exit(1);
  }
  for (const ph of photos) {
    errors.length = 0;
    const t0 = Date.now();
    // The last photo let go (its state cleared), then this one picked.
    await pg.evaluate(() => document.getElementById('ocr-close').click());
    await pg.setInputFiles('#search-photo-file', ph.path);
    const done = await pg.waitForFunction(() =>
      /^(searched|none|failed)$/.test(document.getElementById('ocr-sheet').getAttribute('data-sz-ocr') || ''),
    null, { timeout: PHOTO_MS, polling: 250 }).then(() => true, () => false);
    const got = await pg.evaluate(() => ({
      status: document.getElementById('ocr-sheet').getAttribute('data-sz-ocr'),
      why: ((document.querySelector('#ocr-sheet .ocr-why') || {}).textContent || ''),
      lines: [...document.querySelectorAll('#ocr-lines .ocr-line')].map((b) => b.textContent),
      field: document.getElementById('search-input').value,
      results: [...document.querySelectorAll('#search-results .search-result:not([data-sz-pending])')].slice(0, 5)
        .map((r) => ({ name: r.getAttribute('data-name') || '',
                       shown: ((r.querySelector('.search-result-name') || {}).textContent || '').trim() })),
    }));
    // The field is the line searched for only after a search (else the last photo's).
    if (got.status !== 'searched') got.field = '';
    const out = { id: ph.id, ...got, status: done ? got.status : 'timeout', ms: Date.now() - t0,
                  errors: errors.filter((m) => !/ResizeObserver/.test(m)).slice(0, 3) };
    console.log(JSON.stringify(out));
    // A photo still being read would answer for the next one: stop here.
    if (!done) { rc = 1; break; }
  }
} catch (e) {
  console.log(JSON.stringify({ error: String(e && e.message || e).slice(0, 300) }));
  rc = 1;
} finally {
  await browser.close();
}
process.exit(rc);
