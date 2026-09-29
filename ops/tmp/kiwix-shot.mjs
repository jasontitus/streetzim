// Screenshot the in-ZIM viewer as Kiwix serves it. No service worker, no picker.
import puppeteer from 'puppeteer';
const ORIGIN = process.env.ZIM_ORIGIN;
const OUT_FIXED = process.env.SHOT_FIXED || '/storage/streetzim/tmp/shot-fixed.png';
const OUT_SIM   = process.env.SHOT_SIM   || '/storage/streetzim/tmp/shot-simulated-old.png';
const b = await puppeteer.launch({ headless: true, executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox', '--disable-dev-shm-usage'] });
try {
  const p = await b.newPage();
  await p.setViewport({ width: 390, height: 844, deviceScaleFactor: 3, isMobile: true, hasTouch: true });
  await p.goto(ORIGIN + '/index.html', { waitUntil: 'domcontentloaded', timeout: 90000 });
  await p.waitForSelector('canvas.maplibregl-canvas, .maplibregl-canvas', { timeout: 90000 });
  await new Promise(r => setTimeout(r, 10000));
  const read = () => p.evaluate(() => {
    const cs = getComputedStyle(document.documentElement);
    const el = document.getElementById('search-container');
    const r = el ? el.getBoundingClientRect() : null;
    return { topInset: cs.getPropertyValue('--top-inset').trim(),
             searchTopPx: r ? Math.round(r.top) : null };
  });
  console.log('GUARDED (what Kiwix now gets):', JSON.stringify(await read()));
  await p.screenshot({ path: OUT_FIXED });

  // Simulate the pre-fix behaviour: Kiwix's WKWebView reported a non-zero inset
  // and the old rule piped it straight into --top-inset. Desktop Chrome reports
  // 0, so force a representative value to show the layout consequence.
  await p.addStyleTag({ content: ':root { --top-inset: 122px !important; }' });
  await new Promise(r => setTimeout(r, 1200));
  console.log('SIMULATED pre-fix           :', JSON.stringify(await read()));
  await p.screenshot({ path: OUT_SIM });
} finally { await b.close(); }
console.log('KIWIX-SHOT-DONE');
