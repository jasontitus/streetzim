// Search with a photo works end to end (docs/ocr.md).
//
// Loads the viewer from a ZIM built with --ocr, checks the camera button is
// there, then for a few places of the map draws a photo of a street sign
// with the place's name on it -- tilted, unevenly lit, noisy, on a wall
// with other text and the town in small print -- hands it in as a phone's
// picker would, or drops it on the map, or pastes it (in turn),
// and checks that the sign reader reads it (ocr/ocr-worker.js, the PaddleOCR
// models in the ZIM) and that the place comes out first in the results.
// That the search field holds the line the place matched, not the whole
// photo's text. Then picks it and checks the photo sheet hides, the map flies
// there and the camera button shows the photo; and that tapping it brings
// back the photo and its places.
//
//   ZIM_ORIGIN=http://127.0.0.1:8902/content/<book> CHROME_PATH=... \
//     node tools/ocr_check.mjs
//
// NAMES ("A|B|C") are the places to photograph, each "text on the sign" or
// "text on the sign=>its record's name" (an Arabic sign of a place the
// search lists by its Latin name; "=>A/B" when two places share the sign's
// name and either may come first); by default the first streets and POIs
// of the map's own search whose names are 8-30 letters. With SCRIPT (a
// Unicode script name: Arabic, Thai, Han, ...) and QUERIES ("شا|مس"), the
// signs are in that script instead: the own-script names (`nn`, else `n`)
// of the first streets, POIs and places those searches find, 5-25 letters
// of that script and spaces, and the photo passes when its place, or another
// of exactly the sign's name, comes first.
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
  // Where the time goes when a photo is slow: requests over 5 s are
  // printed as they end, and the ones still open when a photo times out.
  const T0 = Date.now(), open = new Map();
  const since = (t) => `${((t - T0) / 1000).toFixed(1)} s`;
  p.on('request', r => open.set(r, Date.now()));
  const ended = (how) => (r) => {
    const t = open.get(r); open.delete(r);
    if (t !== undefined && Date.now() - t > 5000) {
      console.log(`slow request (${how}): ${r.url().replace(origin, '')} took ${Date.now() - t} ms, from ${since(t)}`);
    }
  };
  p.on('requestfinished', ended('finished'));
  p.on('requestfailed', ended('failed'));
  p.on('workercreated', w => console.log(`worker started at ${since(Date.now())}: ${w.url().slice(0, 40)}`));
  await p.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  await p.goto(origin + '/index.html', { waitUntil: 'load', timeout: 120000 });
  await p.waitForFunction(() => window.__szMap && window.__szMap.loaded() &&
    document.getElementById('search-input').getAttribute('data-sz-search') === 'ready', { timeout: 120000 });
  const shown = await p.evaluate(() => {
    const b = document.getElementById('search-photo');
    return !!b && !b.hidden && b.getBoundingClientRect().width > 0;
  });
  if (!shown) errs.push('no camera button: map-config.json has no `ocr`, or the viewer hid it');

  // In Kiwix's Apple apps (forced: no zim: scheme here) the camera opens a
  // note, not the file picker: on iOS with a "choose a photo" button (its
  // Take Photo closes the app), on macOS without (it opens no picker; drop
  // or paste instead). The x lets it go.
  for (const app of ['ios', 'mac']) {
    const note = await p.evaluate((a) => {
      window.__szSignKiwixApp = a;
      document.getElementById('search-photo').click();
      const sheet = document.getElementById('ocr-sheet');
      const got = { open: !sheet.hidden, state: sheet.getAttribute('data-sz-ocr'),
                    choose: !document.getElementById('ocr-choose').hidden,
                    photo: !document.getElementById('ocr-canvas').hidden };
      document.getElementById('ocr-close').click();
      window.__szSignKiwixApp = '';
      return got;
    }, app);
    const want = app === 'ios' ? { state: 'pick', choose: true } : { state: 'drop', choose: false };
    if (!(note.open && note.state === want.state && note.choose === want.choose && !note.photo)) {
      errs.push(`Kiwix ${app}: the camera did not open its note first: ` + JSON.stringify(note));
    }
  }

  // Places to photograph: given, or streets and POIs from the map's search.
  let names = (process.env.NAMES || '').split('|').filter(Boolean);
  if (!names.length && process.env.SCRIPT) {
    names = await p.evaluate(async (script, queries) => {
      const own = new RegExp(`^[\\p{Script=${script}} ]{5,25}$`, 'u');
      const out = [], seen = {};
      for (const q of queries) {
        const found = await window.__szMap._queryPlaces(q);
        for (const m of found) {
          const it = m.item, text = (it.nn || it.n || '').trim();
          if (['street', 'poi', 'place'].indexOf(it.t) < 0 || !own.test(text) || seen[text]) continue;
          seen[text] = 1;
          out.push(text + '=>' + it.n);
          break;
        }
        if (out.length >= 4) break;
      }
      return out;
    }, process.env.SCRIPT, (process.env.QUERIES || '').split('|').filter(Boolean));
  } else if (!names.length) {
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
      // As a phone's picker gives it, then dropped on the map, then pasted:
      // the ways in where no picker opens (Kiwix for macOS).
      const dt = new DataTransfer();
      dt.items.add(new File([blob], 'sign.jpg', { type: 'image/jpeg' }));
      const how = seed % 3;
      if (how === 1) {
        const input = document.getElementById('search-photo-file');
        input.files = dt.files;
        input.dispatchEvent(new Event('change', { bubbles: true }));
      } else if (how === 2) {
        const target = document.getElementById('map');
        target.dispatchEvent(new DragEvent('dragover', { dataTransfer: dt, bubbles: true, cancelable: true }));
        target.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
      } else {
        document.body.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
      }
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
      field: document.getElementById('search-input').value,
      results: [...document.querySelectorAll('#search-results .search-result:not([data-sz-pending])')]
        .map(r => r.getAttribute('data-name')),
      // the first row's names as shown: its name and own-script name
      shown: [...document.querySelectorAll('#search-results .search-result:not([data-sz-pending]) .search-result-name')]
        .slice(0, 1).map(e => e.textContent),
    }));
    const ms = Date.now() - t0;
    console.log(`"${name}": ${got.status}: read ${JSON.stringify(got.lines)} in ${ms} ms; results ${JSON.stringify(got.results.slice(0, 3))}`);
    if (!done) {
      for (const [r, t] of open) console.log(`still open: ${r.url().replace(origin, '')}, since ${since(t)}`);
      // Its answer would land on the next photo: stop here.
      errs.push(`"${name}": nothing read or found in 180 s (${got.status}), at ${since(Date.now())}`);
      break;
    }
    // The line the place matched, as read ("Bab AI Bahrain" for "Bab Al
    // Bahrain" is still that line), not every line of the photo.
    if (got.lines.indexOf(got.field) < 0 || got.field.indexOf('OPEN') >= 0) {
      errs.push(`"${name}": the search field says ${JSON.stringify(got.field)}, not the sign's line`);
    }
    // Another place of exactly the sign's name may come first (two records
    // of one street): its row shows that name.
    const sameName = got.shown[0] && got.shown[0].split(' ').join('').indexOf(name.split(' ').join('')) >= 0;
    if (want.split('/').indexOf(got.results[0]) < 0 && !sameName) errs.push(`"${name}": first result is ${JSON.stringify(got.results[0])}, not ${JSON.stringify(want)}`);
  }

  // Picking the result hides the sheet, flies the map there and keeps the
  // photo on the camera button; tapping it brings the photo and its places back.
  if (names.length && !errs.length) {
    const before = await p.evaluate(() => window.__szMap.getCenter().toArray());
    const first = await p.evaluate(() =>
      document.querySelector('#search-results .search-result:not([data-sz-pending])').getAttribute('data-name'));
    await p.click('#search-results .search-result');
    await new Promise(r => setTimeout(r, 2500));
    const after = await p.evaluate(() => ({
      hidden: document.getElementById('ocr-sheet').hidden,
      center: window.__szMap.getCenter().toArray(),
      kept: document.getElementById('search-photo').classList.contains('has-last') &&
            /^url\(/.test(document.getElementById('search-photo').style.backgroundImage),
    }));
    if (!after.hidden) errs.push('the photo sheet stayed open after a result was picked');
    if (Math.abs(after.center[0] - before[0]) + Math.abs(after.center[1] - before[1]) < 1e-6) {
      errs.push('the map did not move to the picked result');
    }
    if (!after.kept) errs.push('the camera button does not show the photo after a result was picked');
    await p.click('#search-photo');
    const back = await p.waitForFunction(() => {
      const sheet = document.getElementById('ocr-sheet');
      const row = document.querySelector('#search-results .search-result:not([data-sz-pending])');
      return !sheet.hidden && sheet.getAttribute('data-sz-ocr') === 'searched' &&
        document.getElementById('search-results').style.display !== 'none' && row ? row.getAttribute('data-name') : null;
    }, { timeout: 30000 }).then(h => h.jsonValue(), () => null);
    if (back !== first) errs.push(`back to the photo: the sheet and its places did not come back (first result ${JSON.stringify(back)})`);
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
