import puppeteer from 'puppeteer';
const CSS = process.env.TRY_CSS || '';
const b = await puppeteer.launch({ headless: true, executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox','--disable-dev-shm-usage'] });
const out = [];
for (const W of [320, 390, 430]) {
  const p = await b.newPage();
  await p.setViewport({ width: W, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await p.goto(process.env.ZIM_ORIGIN + '/index.html', { waitUntil: 'domcontentloaded', timeout: 90000 });
  await p.waitForSelector('.maplibregl-canvas', { timeout: 90000 });
  await new Promise(r => setTimeout(r, 8000));
  if (CSS) await p.addStyleTag({ content: CSS });
  await new Promise(r => setTimeout(r, 1200));
  const res = await p.evaluate(() => {
    const SEL = ['#search-container','#find-chips','#controls','#info','#attr-btn',
      '.maplibregl-ctrl-top-right > .maplibregl-ctrl','.maplibregl-ctrl-bottom-left > .maplibregl-ctrl',
      '.maplibregl-ctrl-bottom-right > .maplibregl-ctrl','.maplibregl-ctrl-attrib'];
    const items = [], seen = new Set();
    for (const s of SEL) document.querySelectorAll(s).forEach((el, i) => {
      if (seen.has(el)) return; seen.add(el);
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return;
      items.push({ n: s + (i ? '#' + i : ''), x: Math.round(r.left), y: Math.round(r.top),
                   r: Math.round(r.right), b: Math.round(r.bottom) });
    });
    const ov = [];
    for (let i = 0; i < items.length; i++) for (let j = i + 1; j < items.length; j++) {
      const a = items[i], c = items[j];
      if ((a.n === '#search-container' && c.n === '#find-chips') || (c.n === '#search-container' && a.n === '#find-chips')) continue;
      const ox = Math.min(a.r, c.r) - Math.max(a.x, c.x), oy = Math.min(a.b, c.b) - Math.max(a.y, c.y);
      if (ox > 0 && oy > 0) ov.push(`${a.n} x ${c.n} = ${ox}x${oy}`);
    }
    return { items: items.map(i => `${i.n} y=${i.y}..${i.b}`), ov };
  });
  out.push(`--- ${W}px: ${res.ov.length ? 'OVERLAPS: ' + res.ov.join(' | ') : 'NO OVERLAPS'}`);
  if (W === 390) res.items.forEach(i => out.push('      ' + i));
  await p.close();
}
await b.close();
console.log(out.join('\n'));
