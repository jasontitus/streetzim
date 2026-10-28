// Every recognizer of the sign reader reads its script (docs/ocr.md).
//
// For each pinned recognizer, draws real place names in its script as
// signs -- a clean plate, and a tilted, unevenly lit, noisy photo of one
// with other text around it and a line of small print -- and reads them
// with the reader as a ZIM ships it: ocr/ocr-worker.js with the engine,
// the WebAssembly runtime and the models, loaded as a map of that script
// gets them (its recognizer and the Latin/CJK one). The lines read go
// through the viewer's own code (the sign-lines block of 305-ocr-search.js
// and the sign-match block of 300-search.js), and each sign must match its
// place's record. No map is needed: the files come from
//
//   python -m streetzim.ocr export DIR          # every recognizer
//   OCR_DIR=DIR CHROME_PATH=... node tools/ocr_scripts_check.mjs
//
// The signs are drawn with the system's fonts (CI installs fonts-noto-core
// and fonts-noto-cjk). Prints "OCR SCRIPTS OK" when every sign matched;
// exits 1 otherwise.
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer-core';

const DIR = process.env.OCR_DIR;
if (!DIR || !fs.existsSync(path.join(DIR, 'ocr/manifest.json'))) {
  console.error('OCR_DIR must hold ocr/manifest.json (python -m streetzim.ocr export DIR)');
  process.exit(2);
}
const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const PARTS_DIR = `${REPO}/resources/viewer/src/index`;
const HTML = fs.readdirSync(PARTS_DIR).filter((n) => /^\d{3}-[\w-]+\.(html|js)$/.test(n)).sort()
  .map((n) => fs.readFileSync(`${PARTS_DIR}/${n}`, 'utf8')).join('');
function block(name) {
  const a = HTML.indexOf(`// BEGIN ${name}`), b = HTML.indexOf(`// END ${name}`, a);
  if (a < 0 || b < 0) throw new Error(`block ${name} not found`);
  return HTML.slice(a, b);
}
// The viewer's code under test, as one script for the page.
const VIEWER_CODE = [block('search-shards'), block('admin-search'), block('sign-match'), block('sign-lines')]
  .join('\n') + '\nwindow.__sz = { fold: SEARCH_SHARDS.fold, _szSignLineGroups, _szSignLines, _szSignScore, SZ_SIGN_MIN };';

// Place names per recognizer: [on the sign, the record's `n`]. A record
// with a Latin `n` has the sign's text as its own-script name `nn`.
const SIGNS = {
  'latin-cjk': [['Königstraße'], ['İstiklal Caddesi'], ['北京大学', 'Peking University'],
                ['渋谷駅', 'Shibuya Station'], ['忠孝東路', 'Zhongxiao East Road']],
  korean: [['경복궁', 'Gyeongbokgung'], ['광화문광장', 'Gwanghwamun Square']],
  thai: [['ถนนสีลม', 'Si Lom Road'], ['วัดพระแก้ว', 'Wat Phra Kaew']],
  arabic: [['شارع الملك فهد', 'King Fahd Road'], ['خیابان ولیعصر', 'Valiasr Street']],
  cyrillic: [['Невский проспект', 'Nevsky Prospect'], ['Кнез Михаилова', 'Knez Mihailova']],
  greek: [['Πλατεία Συντάγματος', 'Syntagma Square'], ['Ακρόπολη', 'Acropolis']],
  devanagari: [['लाल किला', 'Red Fort'], ['ठमेल', 'Thamel']],
  tamil: [['தியாகராய நகர்', 'Thyagaraya Nagar'], ['மயிலாப்பூர்', 'Mylapore']],
  telugu: [['చార్మినార్', 'Charminar'], ['ట్యాంక్ బండ్', 'Tank Bund']],
};

const manifest = JSON.parse(fs.readFileSync(path.join(DIR, 'ocr/manifest.json'), 'utf8'));
const recs = Object.fromEntries(manifest.rec.map((r) => [r.name, r]));
const missing = Object.keys(SIGNS).filter((k) => !recs[k]);
const untested = Object.keys(recs).filter((k) => !SIGNS[k]);
const errs = [];
if (missing.length) errs.push(`${DIR} has no recognizer ${missing.join(', ')} (export them all)`);
if (untested.length) errs.push(`no signs to test recognizer ${untested.join(', ')} with: add them to SIGNS`);

const ORIGIN = 'http://ocr.test';
const browser = await puppeteer.launch({ executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox', '--disable-dev-shm-usage'], headless: 'new' });
try {
  const p = await browser.newPage();
  const pageErrors = [];
  p.on('pageerror', (e) => pageErrors.push(e.message));
  if (process.env.DEBUG) p.on('console', (m) => console.log('console:', m.text().slice(0, 200)));
  // The exported files, served as a ZIM reader would.
  await p.setRequestInterception(true);
  p.on('request', (req) => {
    const u = new URL(req.url());
    if (u.origin !== ORIGIN) return req.abort();
    if (u.pathname === '/') return req.respond({ status: 200, contentType: 'text/html', body: '<!doctype html><meta charset="utf-8"><body>' });
    const f = path.join(DIR, decodeURIComponent(u.pathname));
    if (!f.startsWith(path.resolve(DIR)) || !fs.existsSync(f)) return req.respond({ status: 404, body: '' });
    return req.respond({ status: 200, contentType: 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await p.goto(ORIGIN + '/', { waitUntil: 'load' });
  await p.addScriptTag({ content: VIEWER_CODE });
  await p.evaluate(() => {
    const get = (u) => fetch(u).then((r) => { if (!r.ok) throw new Error(u + ' ' + r.status); return r.arrayBuffer(); });
    const text = (u) => fetch(u).then((r) => r.text());
    // The page side of 305-ocr-search.js: start the worker with these recognizers.
    window.__start = async (manifest, names) => {
      const w = new Worker(URL.createObjectURL(new Blob([await text(manifest.worker)], { type: 'application/javascript' })));
      const rec = manifest.rec.filter((r) => names.includes(r.name));
      const msg = { type: 'load', engine: await get(manifest.engine), wasm: await get(manifest.wasm),
        det: { model: await get(manifest.det.model), preset: manifest.det.preset, options: manifest.det.options }, rec: [] };
      for (const r of rec) msg.rec.push({ script: r.scripts[0], model: await get(r.model), dict: await text(r.dict), reverse: !!r.reverse });
      const done = await new Promise((ok) => { w.onmessage = (e) => ok(e.data); w.postMessage(msg); });
      if (done.error) throw new Error(done.error);
      window.__worker = w;
    };
    // One sign: drawn, read, grouped into lines, matched against its record.
    window.__sign = async (text, rough, seed) => {
      let s = seed * 9301 + 49297;
      const rnd = () => { s = (s * 9301 + 49297) % 233280; return s / 233280; };
      const c = document.createElement('canvas'); c.width = 1000; c.height = 700;
      const x = c.getContext('2d');
      x.fillStyle = '#6f747a'; x.fillRect(0, 0, 1000, 700);
      if (rough) {
        for (let k = 0; k < 25; k++) {
          x.fillStyle = `hsla(${rnd() * 360},20%,${30 + rnd() * 50}%,0.6)`;
          x.fillRect(rnd() * 1000, rnd() * 700, 40 + rnd() * 200, 10 + rnd() * 100);
        }
        x.font = '26px sans-serif'; x.fillStyle = 'rgba(255,255,255,0.75)'; x.fillText('OPEN 24H', 60, 90);
      }
      x.save(); x.translate(500, 350);
      if (rough) { x.rotate((rnd() - 0.5) * 0.12); x.transform(1, (rnd() - 0.5) * 0.1, 0, 1, 0, 0); }
      x.font = 'bold 60px sans-serif';
      const w = Math.min(900, x.measureText(text).width + 80);
      x.fillStyle = '#1d4f91'; x.fillRect(-w / 2, -60, w, 120);
      x.strokeStyle = '#fff'; x.lineWidth = 5; x.strokeRect(-w / 2 + 8, -52, w - 16, 104);
      x.fillStyle = '#fff'; x.textAlign = 'center'; x.textBaseline = 'middle';
      x.fillText(text, 0, 4, w - 50);
      if (rough) { x.font = '26px sans-serif'; x.fillText('City Centre', 0, 95); }
      x.restore();
      const img = x.getImageData(0, 0, 1000, 700);
      if (rough) {
        const d = img.data;
        for (let k = 0; k < d.length; k += 4) {
          const light = 0.75 + 0.35 * ((k / 4) % 1000) / 1000, n = (rnd() - 0.5) * 18;
          for (let ch = 0; ch < 3; ch++) d[k + ch] = Math.max(0, Math.min(255, d[k + ch] * light + n));
        }
      }
      const t0 = performance.now();
      const items = await new Promise((ok, no) => {
        window.__worker.onmessage = (e) => (e.data.error ? no(new Error(e.data.error)) : ok(e.data.lines));
        window.__worker.postMessage({ type: 'read', id: seed, image: { width: 1000, height: 700, data: new Uint8Array(img.data.buffer) } });
      });
      const lines = window.__sz._szSignLineGroups(items);
      return { ms: Math.round(performance.now() - t0), lines: lines.map((L) => L.text),
               boxes: lines.map((L) => L.box.height) };
    };
    window.__matches = (lines, heights, rec) => {
      const tallest = Math.max(...heights);
      const sl = window.__sz._szSignLines(lines.map((t, i) => ({ text: t, weight: 0.5 + 0.5 * heights[i] / tallest })),
        window.__sz.fold, 12);
      return window.__sz._szSignScore(rec, sl, window.__sz.fold);
    };
  });

  let seed = 1;
  for (const [name, signs] of Object.entries(SIGNS)) {
    if (!recs[name]) continue;
    const load = [...new Set([name, 'latin-cjk'])];
    const t0 = Date.now();
    await p.evaluate((m, l) => window.__start(m, l), manifest, load);
    console.log(`${name}: loaded ${load.join(' + ')} in ${Date.now() - t0} ms`);
    for (const [text, n = text] of signs) {
      const rec = n === text ? { n, t: 'poi' } : { n, nn: text, t: 'poi' };
      for (const rough of [false, true]) {
        const got = await p.evaluate((t, r, sd) => window.__sign(t, r, sd), text, rough, seed++);
        const m = await p.evaluate((l, h, r) => window.__matches(l, h, r), got.lines, got.boxes, rec);
        const how = rough ? 'photo' : 'plate';
        console.log(`  ${m ? 'ok  ' : 'FAIL'} ${how} "${text}": read ${JSON.stringify(got.lines)} in ${got.ms} ms` +
                    (m ? ` (similarity ${m.sim.toFixed(2)})` : ''));
        if (!m) errs.push(`${name}: the ${how} of "${text}" read ${JSON.stringify(got.lines)}, which does not match it`);
      }
    }
    await p.evaluate(() => window.__worker.terminate());
  }
  if (pageErrors.length) errs.push('page errors: ' + pageErrors.join(' | '));
} finally {
  await browser.close();
}
if (errs.length) {
  for (const e of errs) console.error('FAIL', e);
  process.exit(1);
}
console.log('OCR SCRIPTS OK');
