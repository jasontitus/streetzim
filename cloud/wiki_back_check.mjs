// Back from a Wikipedia article, the map must work as it did before.
//
// Opens a bundled article the way a reader does, goes back, and checks the
// map page. Chromium runs with its back/forward cache ON: Playwright turns
// it off by default (--disable-back-forward-cache), and then every Back is
// a fresh load and the bug cannot show. WebKit (Kiwix on iOS and macOS,
// WebKitGTK) keeps the map page in that cache too.
//
//   sheet: Museums chip -> a card whose place has an article -> place sheet
//          -> "Wikipedia" -> history.back()
//   panel: Wiki panel -> first entry -> "Read full article" -> page.goBack()
//
// After each Back:
//   cache   the browser did hand the page back from its cache (a pageshow
//           with persisted=true happened): else the check tested nothing
//   fresh   the page was then loaded again (pageshow persisted=false in the
//           newest document), not left resumed
//   rail    nothing covers the chip rail, and a horizontal swipe scrolls it
//   pan     a drag in the middle of the map moves it
//   view    the map is where it was when the article was opened (#map=)
//   errors  no page errors
//
// A map whose map-config.json says hasWikiArticles: false is skipped, and
// passes; one with articles whose index never loads fails. A scenario with
// nothing to open (no article near the opening view for the panel; no
// Museums chip, or no Museums result with an article, for the sheet) is
// skipped.
//
//   ZIM_ORIGIN=http://127.0.0.1:8931 MODE=mobile|desktop node cloud/wiki_back_check.mjs
// PLAYWRIGHT_CORE, CHROME_PATH, BROWSER_LIBS as cloud/viewer_scroll_check.mjs.
// Prints one JSON object; exit 0 = PASS. tools/langpack/fresh_viewer_gates.sh
// runs it (mobile) on every map composed with a new viewer.
if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const ORIGIN = process.env.ZIM_ORIGIN || 'http://127.0.0.1:8931';
const MODE = process.env.MODE || 'mobile';
const mobile = MODE === 'mobile';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || undefined, headless: true,
  ignoreDefaultArgs: ['--disable-back-forward-cache'],
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const report = { pass: true, mode: MODE, origin: ORIGIN, runs: [] };

async function scenario(name) {
  const ctx = await browser.newContext(mobile
    ? { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2, userAgent: UA }
    : { viewport: { width: 1280, height: 800 } });
  await ctx.addInitScript(() => {
    window.__szShows = [];
    addEventListener('pageshow', (e) => {
      window.__szShows.push(e.persisted);
      if (e.persisted) try { sessionStorage.setItem('szBfcache', '1'); } catch (x) {}
    });
  });
  const pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', (e) => errors.push(e.message));
  const cdp = await ctx.newCDPSession(pg);
  const tap = (h) => (mobile ? h.tap() : h.click());
  const touchDrag = async (x0, y0, x1, y1) => {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: x0, y: y0 }] });
    for (let i = 1; i <= 12; i++) {
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x0 + (x1 - x0) * i / 12, y: y0 + (y1 - y0) * i / 12 }] });
      await sleep(16);
    }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await sleep(700);
  };
  const run = { name, checks: {}, info: {} };
  try {
    await pg.goto(ORIGIN + '/index.html');
    await pg.waitForFunction(() => window.__szMap && window.__szMap.loaded(), null, { timeout: 120000 });
    const cfg = await pg.evaluate(() => fetch('map-config.json').then((r) => r.json()).catch(() => ({})));
    if (cfg.hasWikiArticles === false) throw new Error('SKIP: the map has no bundled articles (hasWikiArticles: false)');
    await pg.waitForFunction(() => typeof WIKI_GEO_INDEX !== 'undefined' && WIKI_GEO_INDEX, null, { timeout: 120000 });
    await pg.waitForSelector('#find-chips [data-chip]', { timeout: 60000 });
    await sleep(2000);
    let btn;
    if (name === 'sheet') {
      const chip = await pg.$('#find-chips [data-chip="museums"]');
      if (!chip) throw new Error('SKIP: no Museums chip');
      await tap(chip);
      await pg.waitForFunction(() => _findResultsState.items && _findResultsState.items.length
        && document.querySelectorAll('#find-results-strip-track > *').length, null, { timeout: 60000 });
      await sleep(1000);
      const idx = await pg.evaluate(() => _findResultsState.items.findIndex((r) => _wikiArticleLinks(r.q, r.w).length));
      run.info.card = idx;
      if (idx < 0) throw new Error('SKIP: no Museums result in view has a bundled article');
      const card = await pg.evaluateHandle((i) => document.querySelectorAll('#find-results-strip-track > *')[i], idx);
      await card.evaluate((c) => c.scrollIntoView({ inline: 'center', block: 'nearest' }));
      await sleep(500);
      // on the card's name, clear of its Directions / call / web buttons
      const pos = { position: { x: 24, y: 14 } };
      await (mobile ? card.tap(pos) : card.click(pos));
      await pg.waitForSelector('#place-detail', { timeout: 10000 });
      await sleep(800);
      btn = await pg.evaluateHandle(() => [...document.querySelectorAll('#place-detail button')].find((b) => b.textContent.indexOf('\u{1F4D6}') >= 0));
    } else {
      await tap(await pg.$('#wiki-toggle'));
      if (!(await pg.waitForSelector('#wiki-panel-list .wiki-item', { timeout: 30000 }).then(() => true, () => false))) {
        throw new Error('SKIP: no article near the opening view');
      }
      await tap(await pg.$('#wiki-panel-list .wiki-item'));
      await pg.waitForSelector('#wiki-panel-list .wiki-article-btn', { timeout: 30000 });
      await sleep(1500);
      btn = await pg.$('#wiki-panel-list .wiki-article-btn');
    }
    await Promise.all([pg.waitForURL(/wiki-article\//, { timeout: 30000 }), tap(btn)]);
    run.info.article = decodeURIComponent(pg.url().replace(/^.*wiki-article\//, ''));
    await sleep(1000);
    await pg.evaluate(() => scrollTo(0, 400));   // a reader scrolls the article
    await sleep(300);
    if (name === 'sheet') await pg.evaluate(() => history.back());
    else await pg.goBack({ waitUntil: 'commit' });
    await pg.waitForURL((u) => !/wiki-article\//.test(u.href), { timeout: 30000, waitUntil: 'commit' });
    await pg.waitForFunction(() => window.__szMap && window.__szMap.loaded(), null, { timeout: 120000 });
    await sleep(2500);

    const st = await pg.evaluate(() => {
      let cached = false;
      try { cached = sessionStorage.getItem('szBfcache') === '1'; } catch (x) {}
      const rail = document.getElementById('find-chips');
      const r = rail.getBoundingClientRect();
      const hits = [0.2, 0.5, 0.8].map((f) => {
        const h = document.elementFromPoint(r.left + r.width * f, r.top + r.height / 2);
        return h ? (rail.contains(h) ? 'rail' : (h.id || h.className || h.tagName)) : 'none';
      });
      const m = (location.hash.match(/map=([\d.]+)\/(-?[\d.]+)\/(-?[\d.]+)/) || []).slice(1).map(Number);
      const c = window.__szMap.getCenter();
      return { cached, shows: window.__szShows.slice(), hits, railBox: [r.left, r.top, r.width, r.height],
               hash: location.hash, center: [c.lng, c.lat], zoom: window.__szMap.getZoom(), hashView: m };
    });
    run.info.pageshow = st.shows;
    run.info.railHits = st.hits;
    run.checks.cache = { pass: st.cached, handedBack: st.cached };
    run.checks.fresh = { pass: st.shows.length === 1 && st.shows[0] === false, persisted: st.shows };
    // view: the camera stamped into #map= when the article was opened
    const dz = st.hashView.length ? Math.abs(st.zoom - st.hashView[0]) : 99;
    const dc = st.hashView.length ? Math.abs(st.center[0] - st.hashView[2]) + Math.abs(st.center[1] - st.hashView[1]) : 99;
    run.checks.view = { pass: dz < 0.05 && dc < 0.001, hash: st.hash, zoom: +st.zoom.toFixed(2), center: st.center.map((v) => +v.toFixed(5)) };
    // rail: not covered, and a swipe moves it
    await pg.evaluate(() => { document.getElementById('find-chips').scrollLeft = 0; });
    const [rl, rt, rw, rh] = st.railBox;
    if (mobile) await touchDrag(rl + rw - 25, rt + rh / 2, rl + 25, rt + rh / 2);
    else { await pg.mouse.move(rl + rw / 2, rt + rh / 2); await pg.mouse.wheel(300, 0); await sleep(700); }
    const sl = await pg.evaluate(() => document.getElementById('find-chips').scrollLeft);
    run.checks.rail = { pass: st.hits.every((h) => h === 'rail') && sl > 50, hits: st.hits, scrollLeft: sl };
    // pan: a drag in the middle of the map
    const c0 = await pg.evaluate(() => window.__szMap.getCenter().toArray());
    const vw = mobile ? 390 : 1280, vh = mobile ? 844 : 800;
    const x0 = Math.round(vw / 2), y0 = Math.round(vh * 0.4);
    const under = await pg.evaluate(([x, y]) => { const h = document.elementFromPoint(x, y); return h ? (h.className || h.id || h.tagName) : 'none'; }, [x0, y0]);
    if (mobile) await touchDrag(x0, y0, x0 - 80, y0 - 80);
    else { await pg.mouse.move(x0, y0); await pg.mouse.down(); await pg.mouse.move(x0 - 80, y0 - 80, { steps: 10 }); await pg.mouse.up(); await sleep(700); }
    const px = await pg.evaluate((c0) => { const m = window.__szMap; const a = m.project(c0), b = m.project(m.getCenter()); return Math.hypot(a.x - b.x, a.y - b.y); }, c0);
    run.checks.pan = { pass: px > 30, movedPx: +px.toFixed(1), under };
    run.checks.errors = { pass: errors.length === 0, errors: errors.slice(0, 5) };
  } catch (e) {
    if (/^SKIP/.test(e.message)) run.skip = e.message;
    else run.checks.ran = { pass: false, error: e.message.split('\n')[0] };
  }
  run.pass = !!run.skip || Object.values(run.checks).every((c) => c.pass);
  if (!run.pass) report.pass = false;
  report.runs.push(run);
  await ctx.close();
}

await scenario('sheet');
await scenario('panel');
await browser.close();
for (const r of report.runs) {
  if (r.skip) { console.error(`SKIP ${MODE}/${r.name}: ${r.skip}`); continue; }
  for (const [k, c] of Object.entries(r.checks)) {
    const { pass, ...why } = c;
    console.error(`${pass ? 'PASS' : 'FAIL'} ${MODE}/${r.name} ${k} ${JSON.stringify(why)}`);
  }
}
console.log(JSON.stringify(report));
process.exit(report.pass ? 0 : 1);
