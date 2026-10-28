// Search with a photo works end to end (docs/ocr.md).
//
// Loads the viewer from a ZIM built with --ocr, checks the camera button is
// there, then for a few places of the map draws a photo of a street sign
// with the place's name on it -- tilted, unevenly lit, noisy, on a wall
// with other text and the town in small print -- hands it to the button's file input as a phone would,
// and checks that the sign reader reads it (ocr/ocr-worker.js, the PaddleOCR
// models in the ZIM) and that the place comes out first in the results.
// Then picks it and checks the photo sheet closes and the map flies there.
//
//   ZIM_ORIGIN=http://127.0.0.1:8902/content/<book> CHROME_PATH=... \
//     node tools/ocr_check.mjs
//
// NAMES ("A|B|C") are the places to photograph, each "text on the sign" or
// "text on the sign=>its record's name" (an Arabic sign of a place the
// search lists by its Latin name; "=>A/B" when two places share the sign's
// name and either may come first); by default the first streets and POIs
// of the map's own search whose names are 8-30 letters.
// Prints "OCR OK" when every check passed; exits 1 otherwise.
import puppeteer from 'puppeteer-core';

const origin = process.env.ZIM_ORIGIN;
if (!origin) { console.error('ZIM_ORIGIN is required'); process.exit(2); }
const browser = await puppeteer.launch({ executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox', '--disable-dev-shm-usage'], headless: 'new' });
const errs = [];
try {
  const p = await browser.newPage();
  const pageErrors = [];
  p.on('pageerror', e => pageErrors.push(e.message));
  if (process.env.DEBUG) p.on('console', m => console.log('console:', m.text().slice(0, 200)));
  await p.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  await p.goto(origin + '/index.html', { waitUntil: 'load', timeout: 120000 });
  await p.waitForFunction(() => window.__szMap && window.__szMap.loaded() &&
    document.getElementById('search-input').getAttribute('data-sz-search') === 'ready', { timeout: 120000 });
  const shown = await p.evaluate(() => {
    const b = document.getElementById('search-photo');
    return !!b && !b.hidden && b.getBoundingClientRect().width > 0;
  });
  if (!shown) errs.push('no camera button: map-config.json has no `ocr`, or the viewer hid it');

  // Places to photograph: given, or streets and POIs from the map's search.
  let names = (process.env.NAMES || '').split('|').filter(Boolean);
  if (!names.length) {
    names = await p.evaluate(async () => {
      const out = [];
      for (const q of ['av', 'bo', 'ru', 'pl', 'mu', 'ca']) {
        const found = await window.__szMap._queryPlaces(q);
        for (const m of found) {
          const n = m.item.n;
          if ((m.item.t === 'street' || m.item.t === 'poi') && /^[\p{L} '\-.]{8,30}$/u.test(n) &&
              out.indexOf(n) < 0) { out.push(n); break; }
        }
        if (out.length >= 3) break;
      }
      return out;
    });
  }
  if (!names.length) errs.push('no place names to photograph');

  for (const [i, entry] of names.entries()) {
    const [name, want = name] = entry.split('=>');
    const t0 = Date.now();
    // A photo of a sign, made in the page and given to the file input.
    await p.evaluate(async (name, seed) => {
      let s = seed * 9301 + 49297;
      const rnd = () => { s = (s * 9301 + 49297) % 233280; return s / 233280; };
      const c = document.createElement('canvas'); c.width = 1200; c.height = 900;
      const x = c.getContext('2d');
      const g = x.createLinearGradient(0, 0, 1200, 900);
      g.addColorStop(0, '#8a7f72'); g.addColorStop(1, '#5d6670');
      x.fillStyle = g; x.fillRect(0, 0, 1200, 900);
      for (let k = 0; k < 30; k++) {
        x.fillStyle = `hsla(${rnd() * 360},20%,${30 + rnd() * 50}%,0.6)`;
        x.fillRect(rnd() * 1200, rnd() * 900, 40 + rnd() * 240, 10 + rnd() * 120);
      }
      x.font = '28px sans-serif'; x.fillStyle = 'rgba(255,255,255,0.7)';
      x.fillText('OPEN 24H', 80 + rnd() * 200, 120); x.fillText('No Parking', 700, 820);
      x.save();
      x.translate(600, 450); x.rotate((rnd() - 0.5) * 0.12); x.transform(1, (rnd() - 0.5) * 0.1, 0, 1, 0, 0);
      x.font = 'bold 64px sans-serif';
      const w = Math.min(1000, x.measureText(name).width + 80);
      x.fillStyle = '#1d4f91'; x.fillRect(-w / 2, -60, w, 120);
      x.strokeStyle = '#fff'; x.lineWidth = 5; x.strokeRect(-w / 2 + 8, -52, w - 16, 104);
      x.fillStyle = '#fff'; x.textAlign = 'center'; x.textBaseline = 'middle';
      x.fillText(name, 0, 4, w - 50);
      // the town in small print under the plate: the street must still come first
      x.font = '30px sans-serif'; x.fillText('Monaco', 0, 95);
      x.restore();
      // uneven light and sensor noise
      const img = x.getImageData(0, 0, 1200, 900), d = img.data;
      for (let k = 0; k < d.length; k += 4) {
        const px = (k / 4) % 1200, py = Math.floor(k / 4 / 1200);
        const light = 0.75 + 0.35 * (px / 1200) - 0.1 * (py / 900), n = (rnd() - 0.5) * 18;
        for (let ch = 0; ch < 3; ch++) d[k + ch] = Math.max(0, Math.min(255, d[k + ch] * light + n));
      }
      x.putImageData(img, 0, 0);
      const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.75));
      const input = document.getElementById('search-photo-file');
      const dt = new DataTransfer();
      dt.items.add(new File([blob], 'sign.jpg', { type: 'image/jpeg' }));
      input.files = dt.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }, name, i + 1);
    // Read, then searched for: the results list shows real rows.
    // The sheet's state (305-ocr-search.js): reading, lines, then searched,
    // or none / failed.
    const done = await p.waitForFunction(() =>
      /^(searched|none|failed)$/.test(document.getElementById('ocr-sheet').getAttribute('data-sz-ocr') || ''),
    { timeout: 180000 }).then(() => true, () => false);
    const got = await p.evaluate(() => ({
      status: document.getElementById('ocr-sheet').getAttribute('data-sz-ocr'),
      lines: [...document.querySelectorAll('#ocr-lines .ocr-line')].map(b => b.textContent),
      results: [...document.querySelectorAll('#search-results .search-result:not([data-sz-pending])')]
        .map(r => r.getAttribute('data-name')),
    }));
    const ms = Date.now() - t0;
    console.log(`"${name}": ${got.status}: read ${JSON.stringify(got.lines)} in ${ms} ms; results ${JSON.stringify(got.results.slice(0, 3))}`);
    if (!done) { errs.push(`"${name}": nothing read or found in 180 s (${got.status})`); continue; }
    if (want.split('/').indexOf(got.results[0]) < 0) errs.push(`"${name}": first result is ${JSON.stringify(got.results[0])}, not ${JSON.stringify(want)}`);
  }

  // Picking the result closes the sheet and flies the map there.
  if (names.length && !errs.length) {
    const before = await p.evaluate(() => window.__szMap.getCenter().toArray());
    await p.click('#search-results .search-result');
    await new Promise(r => setTimeout(r, 2500));
    const after = await p.evaluate(() => ({
      hidden: document.getElementById('ocr-sheet').hidden,
      center: window.__szMap.getCenter().toArray(),
    }));
    if (!after.hidden) errs.push('the photo sheet stayed open after a result was picked');
    if (Math.abs(after.center[0] - before[0]) + Math.abs(after.center[1] - before[1]) < 1e-6) {
      errs.push('the map did not move to the picked result');
    }
  }
  const sheetErrors = pageErrors.filter(m => !/ResizeObserver/.test(m));
  if (sheetErrors.length) errs.push('page errors: ' + sheetErrors.join(' | '));
} finally {
  await browser.close();
}
if (errs.length) {
  for (const e of errs) console.error('FAIL', e);
  process.exit(1);
}
console.log('OCR OK');
