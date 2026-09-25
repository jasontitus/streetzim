import puppeteer from 'puppeteer-core';
import { readFileSync } from 'fs';
const origin=process.env.ZIM_ORIGIN, tag=process.env.TAG||'x';

// Where to point the camera before counting features.
//
// This used to probe wherever the viewer opens, which is the centre of the
// region's bbox. That is a fine proxy for germany or poland and a useless one
// for anything whose middle is empty: north-africa's bbox centre is open
// Sahara (14 features), east-polynesia's is open Pacific (94), hawaii's is sea
// between the islands (72) -- all three FAILED a >=100 threshold while
// rendering perfectly over land. A gate that fires on correct maps trains you
// to ignore it.
//
// cloud/regions.tsv column 5 already carries an anchor city per region -- the
// place a user actually opens. Probe there instead. The threshold is
// unchanged: a genuinely blank map still renders nothing at its own city.
const ANCHOR_ZOOM = 11;
let anchorWhy = 'default view';
function anchorFor(tagName) {
  // tag is the zim basename, e.g. osm-hawaii-2026-09-20 (a trailing build
  // letter like -2026-09-19d is possible).
  const m = /^osm-(.+)-\d{4}-\d{2}-\d{2}[a-z]?$/.exec(tagName);
  if (!m) { anchorWhy = `default view (tag ${tagName} is not osm-<id>-YYYY-MM-DD)`; return null; }
  const id = m[1];
  let tsv;
  try { tsv = readFileSync(new URL('../cloud/regions.tsv', import.meta.url), 'utf8'); }
  catch (e) {
    // The dangerous one: an unreadable regions.tsv reverts EVERY region to
    // the pre-5e3fe72 probe, reinstating the false FAILs this exists to stop
    // (north-africa 14 features, east-polynesia 94, hawaii 72). That is a
    // global regression, so say so loudly rather than as a per-region note.
    anchorWhy = `default view (COULD NOT READ cloud/regions.tsv: ${e.message})`;
    console.error('[map-health] ' + anchorWhy);
    return null;
  }
  for (const line of tsv.split('\n')) {
    if (!line || line[0] === '#') continue;
    const c = line.split('\t');
    if (c[0] !== id || !c[4]) continue;
    const [lat, lon] = c[4].split(',').map(Number);
    if (Number.isFinite(lat) && Number.isFinite(lon)) return { id, lat, lon, label: c[1] || id };   // c[1] is the region name; c[6] is the search term
    anchorWhy = `default view (${id} has no usable anchor in column 5)`;
    return null;
  }
  anchorWhy = `default view (${id} has no row in cloud/regions.tsv)`;
  return null;
}
const anchor = anchorFor(tag);
const b=await puppeteer.launch({executablePath:process.env.CHROME_PATH,
  args:['--no-sandbox','--disable-dev-shm-usage','--enable-unsafe-swiftshader'],headless:'new'});
const p=await b.newPage();
await p.setViewport({width:600,height:700});
// Wrap maplibregl.Map BEFORE the viewer's scripts run. The map instance is
// function-scoped in index.html, so this is the only way to reach it.
await p.evaluateOnNewDocument(() => {
  window.__sz = { errors: [], ctorArgs: null, map: null };
  let real = undefined;
  Object.defineProperty(window, 'maplibregl', {
    configurable: true,
    get() { return real; },
    set(v) {
      if (v && v.Map && !v.__szWrapped) {
        const RealMap = v.Map;
        function Wrapped(opts) {
          window.__sz.ctorArgs = { hasStyle: !!(opts && opts.style),
                                   styleType: typeof (opts && opts.style) };
          const m = new RealMap(opts);
          window.__sz.map = m;
          m.on('error', e => window.__sz.errors.push(
            String((e && e.error && e.error.message) || (e && e.message) || e).slice(0, 300)));
          return m;
        }
        Wrapped.prototype = RealMap.prototype;
        Object.setPrototypeOf(Wrapped, RealMap);
        v.Map = Wrapped;
        v.__szWrapped = true;
      }
      real = v;
    },
  });
});
await p.goto(`${origin}/index.html`, { waitUntil:'domcontentloaded', timeout:60000 });
await new Promise(r => setTimeout(r, 20000));
// Move to the anchor city and let its tiles settle before counting. No anchor
// (unknown region, odd filename) => probe where it opened, as before.
let movedTo = null, openingView = null;
if (anchor) {
  // Keep what the opening view showed. After 5e3fe72 the gate jumps away
  // from it, and no other gate counts features at all (overlap-check and
  // device-matrix only waitForSelector on the canvas), so low-zoom-only
  // breakage -- a truncated tile pyramid, a throw in _szOpeningView, the
  // lakes layer, which is active only below z6 -- had no observer.
  openingView = await p.evaluate(() => {
    const m = window.__sz && window.__sz.map;
    if (!m) return null;
    try {
      const c = m.getCenter();
      return { zoom: +m.getZoom().toFixed(2),
             center: [+c.lng.toFixed(4), +c.lat.toFixed(4)],
             features: m.queryRenderedFeatures().length };
    } catch (e) { return null; }
  }).catch(() => null);

  const ok = await p.evaluate(([lat, lon, z]) => {
    const m = window.__sz && window.__sz.map;
    if (!m) return false;
    m.jumpTo({ center: [lon, lat], zoom: z });
    return true;
  }, [anchor.lat, anchor.lon, ANCHOR_ZOOM]).catch(() => false);
  if (!ok) anchorWhy = 'default view (map never constructed, so no jump)';
  if (ok) {
    movedTo = anchor;

    await p.waitForFunction(
      () => { const m = window.__sz && window.__sz.map;
              return m && m.areTilesLoaded() && !m.isMoving(); },
      { timeout: 60000 }).catch(() => {});
    await new Promise(r => setTimeout(r, 4000));
  }
}
const out = await p.evaluate(() => {
  const s = window.__sz || {};
  const m = s.map;
  const r = { sawCtor: !!s.ctorArgs, errors: (s.errors||[]).slice(0,5) };
  if (!m) return { ...r, map: 'NEVER CONSTRUCTED' };
  try {
    r.styleLoaded = m.isStyleLoaded();
    r.loaded = m.loaded();
    const st = m.getStyle();
    r.layers = st ? st.layers.length : 0;
    r.sources = st ? Object.keys(st.sources) : [];
    r.zoom = +m.getZoom().toFixed(2);
    // Where the camera ACTUALLY ended up. jumpTo is silently clamped by
    // maxBounds (built from the ZIM's own config.bounds, which for an
    // in-place-patched old ZIM can differ from today's regions.tsv row), and
    // the viewer's _szOpeningView can move the camera asynchronously inside
    // our settle window. Reporting the requested anchor instead of this is
    // how a PASS gets labelled with a place it never looked at.
    const _c = m.getCenter();
    r.center = [+_c.lng.toFixed(4), +_c.lat.toFixed(4)];
    r.sourceLoaded = {};
    for (const sid of (r.sources||[])) {
      try { r.sourceLoaded[sid] = m.isSourceLoaded(sid); } catch (e) { r.sourceLoaded[sid] = 'err'; }
    }
    const f = m.queryRenderedFeatures();
    r.renderedFeatures = f.length;
    const byLayer = {};
    for (const ff of f) byLayer[ff.layer.id] = (byLayer[ff.layer.id]||0)+1;
    r.topLayers = Object.entries(byLayer).sort((a,b)=>b[1]-a[1]).slice(0,6);
  } catch (e) { r.probeError = String(e).slice(0,200); }
  return r;
});
// Name the discriminator beside the numbers: a PASS/FAIL whose probe point is
// invisible is how a wrong gate goes unnoticed for three regions.
// Report the point the count came from, and how far it is from the point we
// asked for -- a clamped or hijacked camera is otherwise invisible.
let driftKm = null;
if (movedTo && out && out.center) {
  const dLat = (out.center[1] - movedTo.lat) * 111.32;
  const dLon = (out.center[0] - movedTo.lon) * 111.32 * Math.cos(movedTo.lat * Math.PI / 180);
  driftKm = +Math.sqrt(dLat * dLat + dLon * dLon).toFixed(1);
}
console.log(JSON.stringify({ tag,
  probeRequested: movedTo ? `${movedTo.label || movedTo.id} ${movedTo.lat},${movedTo.lon} z${ANCHOR_ZOOM}`
                          : anchorWhy,
  probeActual: out && out.center ? `${out.center[1]},${out.center[0]} z${out.zoom}` : null,
  probeDriftKm: driftKm,
  openingView,
  ...out }));
await b.close();
