// Every named place on the map can be tapped.
//
// A tapped label without a Wikidata ID (most shops, restaurants and
// services), or any label on a ZIM without Wikidata, used to do nothing.
// This loads the viewer from a ZIM served by kiwix-serve, finds a named POI
// label near the middle of the screen, taps it, and checks that a popup
// opens with its name and "Directions to here"; then opens the routing panel
// and checks that a tap there picks a point instead of opening a popup; a
// tap on the label at either edge of the screen opens a popup that fits; a
// double-tap zoom on the label leaves no popup; and a tap on a search pin
// dropped on the label opens the pin's popup only.
//
//   ZIM_ORIGIN=http://127.0.0.1:8902/content/<book> CHROME_PATH=... \
//     node tools/tap_label_check.mjs
//
// CENTER ("lon,lat") and ZOOM default to Monaco (the CI ZIM). Prints
// "TAP LABEL OK" when every check passed; exits 1 otherwise.
import puppeteer from 'puppeteer-core';

const origin = process.env.ZIM_ORIGIN;
if (!origin) { console.error('ZIM_ORIGIN is required'); process.exit(2); }
const CENTER = (process.env.CENTER || '7.4246,43.7396').split(',').map(Number);
const ZOOM = Number(process.env.ZOOM || 17);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const idle = p => p.evaluate(() => new Promise(res => {
  const m = window.__szMap;
  if (!m.isMoving() && m.loaded()) setTimeout(res, 50);
  m.once('idle', () => setTimeout(res, 50));
  setTimeout(res, 6000);
}));

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
  await p.waitForFunction(() => window.__szMap && window.__szMap.loaded(), { timeout: 120000 });
  await p.evaluate((c, z) => window.__szMap.jumpTo({ center: c, zoom: z }), CENTER, ZOOM);
  await idle(p); await sleep(500);
  // A named POI label without a Wikidata ID, in the middle band of the
  // screen (clear of the search box, chips and the bottom controls).
  const target = await p.evaluate(() => {
    const m = window.__szMap, W = m.getCanvas().clientWidth, H = m.getCanvas().clientHeight;
    const layers = ['poi-label'].filter(id => m.getLayer(id));
    const fs = m.queryRenderedFeatures([[W * 0.15, H * 0.3], [W * 0.85, H * 0.7]], { layers });
    for (const f of fs) {
      const pr = f.properties || {};
      if (!(pr.name || pr['name:latin']) || pr.wikidata || !f.geometry || f.geometry.type !== 'Point') continue;
      // Tap where only this label is drawn (its icon or its name), found by
      // hit-testing a column through the anchor, as a finger would land.
      const name = pr['name:latin'] || pr.name, pt = m.project(f.geometry.coordinates);
      for (let dy = -30; dy <= 40; dy += 2) {
        const hits = m.queryRenderedFeatures([pt.x, pt.y + dy], { layers })
          .map(h => h.properties['name:latin'] || h.properties.name).filter(Boolean);
        if (hits.length && hits.every(h => h === name)) return { name, x: pt.x, y: pt.y + dy };
      }
    }
    return null;
  });
  if (!target) {
    errs.push('no named POI label without a Wikidata ID in view at ' + CENTER + ' z' + ZOOM);
  } else {
    if (process.env.DEBUG) await p.evaluate(() => { window.__tapClicks = 0; window.__szMap.on('click', () => { window.__tapClicks++; }); });
    await p.mouse.click(target.x, target.y);
    if (process.env.DEBUG) console.log('map click events:', await p.evaluate(() => window.__tapClicks), 'page errors:', pageErrors);
    const popup = await p.waitForSelector('.maplibregl-popup', { timeout: 8000 }).then(() => true, () => false);
    if (!popup) {
      errs.push(`tapping "${target.name}" opened no popup`);
    } else {
      const got = await p.evaluate(() => {
        const pop = document.querySelector('.maplibregl-popup');
        return { text: pop.textContent, directions: !!pop.querySelector('.pin-directions'),
                 routing: !!(window.streetzimRouting && window.streetzimRouting.open) };
      });
      if (got.text.indexOf(target.name) < 0) errs.push(`popup does not name "${target.name}": ${got.text.slice(0, 80)}`);
      if (got.routing && !got.directions) errs.push('popup has no "Directions to here"');
      console.log(`tapped "${target.name}": popup ok${got.directions ? ', with Directions' : ''}`);
      const closeAll = () => p.evaluate(() =>
        document.querySelectorAll('.maplibregl-popup-close-button').forEach(b => b.click()));
      const popups = () => p.evaluate(() => document.querySelectorAll('.maplibregl-popup').length);
      // A double-tap (zoom) on the label must not leave a popup open, fast
      // or slow: MapLibre takes taps up to 500 ms apart as a double-tap.
      // Also with the CPU slowed 4x (a phone): there the zoom used to start
      // after the popup had opened. The second tap must not press anything
      // in a popup either (it used to open routing via "Directions").
      const cdp = await p.createCDPSession();
      for (const rate of [1, 4]) {
        await cdp.send('Emulation.setCPUThrottlingRate', { rate });
        for (const gap of [80, 300, 420]) {
          await p.evaluate((c, z) => window.__szMap.jumpTo({ center: c, zoom: z }), CENTER, ZOOM);
          await idle(p); await closeAll(); await sleep(300);
          const z0 = await p.evaluate(() => window.__szMap.getZoom());
          await p.touchscreen.tap(target.x, target.y); await sleep(gap);
          await p.touchscreen.tap(target.x, target.y); await sleep(1500 * rate); await idle(p);
          const zoomed = await p.evaluate(z => window.__szMap.getZoom() > z + 0.5, z0);
          const routing = await p.evaluate(() => !!(window.streetzimRouting && window.streetzimRouting.panelActive));
          const n = await popups();
          // A zoom means MapLibre took it as a double-tap: no popup. Two
          // taps it did not take as one (too far apart) are two single
          // taps: a popup is right. Either way the second tap must not
          // press anything in a popup.
          if (routing) errs.push(`a double-tap ${gap} ms apart (CPU x${rate}) opened routing`);
          else if (zoomed && n) errs.push(`a double-tap ${gap} ms apart (CPU x${rate}) zoomed and left a popup open`);
          else console.log(`double-tap ${gap} ms, CPU x${rate}: ${zoomed ? 'zoomed, no popup' : 'two single taps, ' + n + ' popup'}`);
          if (routing) await p.evaluate(() => window.streetzimRouting.close());
        }
      }
      await cdp.send('Emulation.setCPUThrottlingRate', { rate: 1 });
      // A label near either edge of the screen: its popup stays on screen
      // (a fixed 'bottom' anchor put half of it off the side).
      const tapLL = await p.evaluate((c, z, t) => {
        const m = window.__szMap; m.jumpTo({ center: c, zoom: z });
        const ll = m.unproject([t.x, t.y]); return [ll.lng, ll.lat];
      }, CENTER, ZOOM, target);
      for (const side of ['left', 'right']) {
        await p.evaluate((c, z) => window.__szMap.jumpTo({ center: c, zoom: z }), CENTER, ZOOM);
        await idle(p); await closeAll();
        const at = await p.evaluate((ll, side) => {
          const m = window.__szMap, W = m.getCanvas().clientWidth, q = m.project(ll);
          m.panBy([q.x - (side === 'left' ? 14 : W - 14), 0], { duration: 0 });
          const r = m.project(ll); return { x: r.x, y: r.y, W };
        }, tapLL, side);
        await idle(p); await sleep(300);
        await p.touchscreen.tap(at.x, at.y); await sleep(1500);
        const box = await p.evaluate(() => {
          const el = document.querySelector('.maplibregl-popup');
          if (!el) return null;
          const r = el.getBoundingClientRect(); return { left: r.left, right: r.right };
        });
        if (!box) errs.push(`a tap on "${target.name}" at the ${side} edge (x=${Math.round(at.x)}) opened no popup`);
        else if (box.left < 0 || box.right > at.W) errs.push(`the popup for a tap at the ${side} edge runs off the screen: x ${Math.round(box.left)}..${Math.round(box.right)} of ${at.W}`);
        else console.log(`${side} edge: popup on screen (x ${Math.round(box.left)}..${Math.round(box.right)} of ${at.W})`);
      }
      await p.evaluate((c, z) => window.__szMap.jumpTo({ center: c, zoom: z }), CENTER, ZOOM);
      await idle(p); await closeAll();
      // With the routing panel open, a tap picks a route point instead.
      // (Before the pin below: a tap on the pin would open its own popup.)
      if (got.routing) {
        await p.evaluate(() => {
          document.querySelectorAll('.maplibregl-popup-close-button').forEach(b => b.click());
          window.streetzimRouting.open();
        });
        await sleep(800);
        await p.mouse.click(target.x, target.y);
        await sleep(1500);
        const n = await popups();
        if (n) errs.push('a tap with the routing panel open still opened a place popup');
        else console.log('routing panel open: the tap opened no popup');
        await p.evaluate(() => { window.streetzimRouting.clear(); window.streetzimRouting.close(); });
        await sleep(500); await closeAll();
      }
      // A search pin dropped on the label: tapping the pin opens its own
      // popup only, not a second one for the label under it.
      const pinPt = await p.evaluate(t => {
        const m = window.__szMap, ll = m.unproject([t.x, t.y]);
        placeSearchPin(m, ll.lat, ll.lng, 'Test pin');
        document.querySelectorAll('.maplibregl-popup-close-button').forEach(b => b.click());
        const r = document.querySelector('.maplibregl-marker').getBoundingClientRect();
        // Near the tip, over the label: the spot that used to open both.
        return { x: r.left + r.width / 2, y: r.top + r.height * 0.85 };
      }, target);
      await sleep(300);
      await p.touchscreen.tap(pinPt.x, pinPt.y); await sleep(1000);
      const n = await popups();
      if (n !== 1) errs.push(`tapping a search pin opened ${n} popups, not 1`);
      else console.log('search pin tap: one popup');
      await closeAll(); await sleep(300);
      // A label tap, then the pin within the popup delay: the pin's popup only.
      const other = await p.evaluate(t => {
        const m = window.__szMap, W = m.getCanvas().clientWidth, H = m.getCanvas().clientHeight;
        const fs = m.queryRenderedFeatures([[W * 0.15, H * 0.3], [W * 0.85, H * 0.7]], { layers: ['poi-label'] });
        for (const f of fs) {
          const nm = f.properties['name:latin'] || f.properties.name;
          if (!nm || nm === t.name || f.geometry.type !== 'Point') continue;
          const q = m.project(f.geometry.coordinates);
          for (let dy = -30; dy <= 40; dy += 2) {
            const hits = m.queryRenderedFeatures([q.x, q.y + dy], { layers: ['poi-label'] })
              .map(h => h.properties['name:latin'] || h.properties.name);
            if (hits.length && hits.every(h => h === nm)) return { x: q.x, y: q.y + dy };
          }
        }
        return null;
      }, target);
      if (other) {
        await p.touchscreen.tap(other.x, other.y); await sleep(100);
        await p.touchscreen.tap(pinPt.x, pinPt.y); await sleep(1000);
        const n2 = await popups();
        if (n2 !== 1) errs.push(`a label tap then a pin tap 100 ms later left ${n2} popups, not 1`);
        else console.log('label then pin: one popup');
        await closeAll();
      }
    }
  }
} finally {
  await browser.close();
}
if (errs.length) { errs.forEach(e => console.log('FAIL ' + e)); console.log('TAP LABEL FAILED'); process.exit(1); }
console.log('TAP LABEL OK');
