// Quick worker-test: load /drive/, post the local CA ZIM into the SW,
// navigate to /drive/viewer/, watch all console output for ~10s.
import puppeteer from 'puppeteer';

const browser = await puppeteer.launch({
  executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  headless: true,
  args: ['--no-sandbox', '--disable-web-security'],
});
const page = await browser.newPage();
page.on('console', m => {
  console.log('[' + m.type() + ']', m.text().slice(0, 240));
});
page.on('pageerror', e => console.log('[pageerror]', e.message));

await page.goto('https://streetzim.web.app/drive/?bust=' + Date.now(),
  { waitUntil: 'domcontentloaded' });
await page.evaluate(() => navigator.serviceWorker.ready);

const setOk = await page.evaluate(async () => {
  const resp = await fetch('http://localhost:8765/osm-california-2026-05-09.zim');
  const blob = await resp.blob();
  const ch = new MessageChannel();
  const reply = new Promise(r => { ch.port1.onmessage = e => r(e.data); });
  navigator.serviceWorker.controller.postMessage(
    { type: 'set-zim', blob, name: 'osm-california-2026-05-09.zim' }, [ch.port2]);
  return await reply;
});
console.log('---ZIM loaded:', JSON.stringify(setOk).slice(0, 100));

await page.goto('https://streetzim.web.app/drive/viewer/?bust=' + Date.now(),
  { waitUntil: 'domcontentloaded' });

// Trigger a route to force loadGraph to run.
await page.waitForFunction(
  () => typeof window.streetzimRouting !== 'undefined'
    && typeof window.streetzimRouting.setOrigin === 'function');
await page.evaluate(() => {
  window.streetzimRouting.setOrigin(37.7749, -122.4194, 'San Francisco');
  window.streetzimRouting.setDest(34.0522, -118.2437, 'Los Angeles');
});
await new Promise(r => setTimeout(r, 60000));

await browser.close();
