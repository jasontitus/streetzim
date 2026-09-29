// Report every pairwise overlap between visible UI overlays at phone size.
import puppeteer from 'puppeteer';
const ORIGIN = process.env.ZIM_ORIGIN;
const W = +(process.env.VW || 390), H = +(process.env.VH || 844);
const b = await puppeteer.launch({ headless: true, executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox','--disable-dev-shm-usage'] });
try {
  const p = await b.newPage();
  await p.setViewport({ width: W, height: H, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await p.goto(ORIGIN + '/index.html', { waitUntil: 'domcontentloaded', timeout: 90000 });
  await p.waitForSelector('canvas.maplibregl-canvas, .maplibregl-canvas', { timeout: 90000 });
  await new Promise(r => setTimeout(r, 10000));
  const res = await p.evaluate(() => {
    // Measure REAL controls, not MapLibre's positioning wrappers: the
    // .maplibregl-ctrl-bottom-right container spans the full viewport width
    // (390x108) while painting almost nothing, which manufactures fake
    // overlaps. Its children (.maplibregl-ctrl, .maplibregl-ctrl-attrib) are
    // the boxes that actually draw.
    const SEL = ['#search-container','#find-chips','#controls','#info','#attr-btn',
      '#viewer-build-stamp','#wiki-panel','#routing-panel','#search-results',
      '.maplibregl-ctrl-top-right > .maplibregl-ctrl',
      '.maplibregl-ctrl-top-left > .maplibregl-ctrl',
      '.maplibregl-ctrl-bottom-left > .maplibregl-ctrl',
      '.maplibregl-ctrl-bottom-right > .maplibregl-ctrl',
      '.maplibregl-ctrl-attrib'];
    const items = [];
    const seen = new Set();
    for (const s of SEL) {
      document.querySelectorAll(s).forEach((el, i) => {
        if (seen.has(el)) return;
        seen.add(el);
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return;
        if (r.bottom < 0 || r.top > innerHeight) return;
        items.push({ name: s + (i ? '#' + i : ''),
          x: Math.round(r.left), y: Math.round(r.top),
          w: Math.round(r.width), h: Math.round(r.height),
          r: Math.round(r.right), b: Math.round(r.bottom) });
      });
    }
    const overlaps = [];
    for (let i = 0; i < items.length; i++)
      for (let j = i + 1; j < items.length; j++) {
        const a = items[i], c = items[j];
        if (a.name.startsWith('#search-container') && c.name === '#find-chips') continue; // child
        if (c.name.startsWith('#search-container') && a.name === '#find-chips') continue;
        const ox = Math.min(a.r, c.r) - Math.max(a.x, c.x);
        const oy = Math.min(a.b, c.b) - Math.max(a.y, c.y);
        if (ox > 0 && oy > 0) overlaps.push({ a: a.name, b: c.name, ox, oy });
      }
    return { viewport: { w: innerWidth, h: innerHeight }, items, overlaps };
  });
  console.log('viewport', JSON.stringify(res.viewport));
  for (const it of res.items)
    console.log(`  ${it.name.padEnd(30)} x=${String(it.x).padStart(4)} y=${String(it.y).padStart(4)} w=${String(it.w).padStart(4)} h=${String(it.h).padStart(4)} right=${it.r} bottom=${it.b}`);
  console.log(res.overlaps.length ? 'OVERLAPS:' : 'NO OVERLAPS');
  for (const o of res.overlaps) console.log(`  ${o.a}  x  ${o.b}   overlap ${o.ox}x${o.oy}px`);
} finally { await b.close(); }
console.log('OVERLAP-CHECK-DONE');
