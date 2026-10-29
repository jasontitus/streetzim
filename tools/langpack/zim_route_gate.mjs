// A route in the map's own viewer, as Kiwix runs it: the page served from
// the ZIM (kiwix-serve, or cloud/serve_zim_entries.py), its routing worker
// and graph, a route between two points through window.streetzimRouting (the
// viewer's routing seam: open, setOrigin, setDest), and a distance in
// #route-distance within the time allowed. The routing code is part of the
// viewer (routing-worker.js, the panel), so a viewer swap must pass it
// (tools/langpack/fresh_viewer_gates.sh).
//
//   ZIM_ORIGIN=http://127.0.0.1:PORT/content/BOOK node zim_route_gate.mjs LAT,LON LAT,LON
// Prints one JSON line {pass, distance, time, status, ms}; exit 0 on a route.
// PLAYWRIGHT_CORE, CHROME_PATH, BROWSER_LIBS: as tools/langpack/builder_e2e.mjs.
if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const ORIGIN = process.env.ZIM_ORIGIN;
const pt = (s) => String(s || '').split(',').map(Number);
const [src, dst] = [pt(process.argv[2]), pt(process.argv[3])];
const TIMEOUT_MS = +(process.env.ROUTE_TIMEOUT_MS || 120000);
if (!ORIGIN || src.length !== 2 || dst.length !== 2 || [...src, ...dst].some((x) => !Number.isFinite(x))) {
  console.log(JSON.stringify({ pass: false, error: 'usage: ZIM_ORIGIN=... zim_route_gate.mjs LAT,LON LAT,LON' }));
  process.exit(2);
}
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true });
const out = { pass: false, src, dst };
try {
  const pg = await browser.newPage({ viewport: { width: 1024, height: 768 } });
  const errors = [];
  pg.on('pageerror', (e) => errors.push(e.message));
  const t0 = Date.now();
  await pg.goto(ORIGIN.replace(/\/$/, '') + '/');
  await pg.waitForFunction(() => window.streetzimRouting && typeof window.streetzimRouting.open === 'function', null, { timeout: 60000 });
  await pg.evaluate(([s, d]) => {
    window.streetzimRouting.open();
    window.streetzimRouting.setOrigin(s[0], s[1], 'src');
    window.streetzimRouting.setDest(d[0], d[1], 'dst');
  }, [src, dst]);
  const got = await pg.waitForFunction(() => {
    const d = (document.getElementById('route-distance') || {}).textContent || '';
    const st = document.getElementById('routing-status');
    const state = st ? st.getAttribute('data-state') || '' : '';
    if (d.trim()) return { distance: d.trim(), time: ((document.getElementById('route-time') || {}).textContent || '').trim(), state };
    if (/error|fail|none/i.test(state)) return { distance: '', state, status: st.textContent };
    return null;
  }, null, { timeout: TIMEOUT_MS, polling: 500 }).then((h) => h.jsonValue()).catch((e) => ({ distance: '', error: String(e.message || e).slice(0, 200) }));
  Object.assign(out, got, { ms: Date.now() - t0, errors: errors.slice(0, 3) });
  out.pass = !!got.distance && errors.length === 0;
} catch (e) {
  out.error = String(e && e.message || e).slice(0, 300);
}
console.log(JSON.stringify(out));
await browser.close();
process.exit(out.pass ? 0 : 1);
