// UI gate (review S7): what a visitor sees in the in-ZIM viewer.
//
//   node ui_gate.mjs ORIGIN LANG REGION_JSON [base]
//
// ORIGIN = http://localhost:PORT/content/<book>. REGION_JSON:
//   {"base_lang": "en", "city": {"qid": "Q65", "lon": -118.24, "lat": 34.05, "zoom": 7},
//    "search": {"native": "Sacramento", "english": "Los Angeles", "map_lang": {"th": "...", ...}}}
// Checks (each recorded with its evidence):
//   html_lang     the UI is in LANG: the viewer chose LANG's table (SZ_UI_LANG), <html lang>
//                 says so and a translated string is on screen; a language with no UI
//                 table FAILS, unless the viewer declares a fallback for it
//                 (SZ_UI_FALLBACK: rm -> de) and shows that (ui_verdict.mjs);
//                 right-to-left languages need <html dir="rtl"> where the
//                 viewer knows them (SZ_RTL_LANGS). English (base, en entry): English UI
//   labels        text-field reads name:<LANG> (merged, LANG != base); the city's label
//                 as displayed (szDisplayLabelOf) is what the label rule says (with
//                 regionLanguages: name:<LANG> over the native name), at the first
//                 zoom that renders it; every rendered place whose names differ shows
//                 both, and some does (unless LANG is one of the region's languages)
//   popup_article click the city's label -> popup -> "read article" button in the
//                 preferred Wikipedia -> the article page (prefix ~w/<LANG>/ when the
//                 pack has it) -> its images load (lazy ones too) -> back-to-map returns
//                 to the map
//   search_native / search_english  the map's own search box finds results
//   search_map_lang  (reported, not gated) the place's name in the map language
// Prints one JSON line; exit 0 = PASS.
// UI_GATE_PREOPEN: a URL to open first, the /drive/ picker with ?zim=<a
// language version> (tools/langpack/drive_preview_e2e.mjs), which hands
// over to the viewer; ORIGIN is then <site>/drive/viewer.
// PLAYWRIGHT_CORE: path to playwright-core's index.mjs (else the installed package);
// CHROME_PATH: the Chromium to drive; BROWSER_LIBS: extra LD_LIBRARY_PATH for it.
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
import fs from 'node:fs';
import { labelsVerdict, searchFound, uiVerdict } from './ui_verdict.mjs';

const [ORIGIN, LANG, REGION, MODE] = process.argv.slice(2);
const R = JSON.parse(fs.readFileSync(REGION, 'utf8'));
const isBase = MODE === 'base';
if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS
    + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = { origin: ORIGIN, lang: LANG, mode: isBase ? 'base' : 'merged', checks: {} };
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
const ready = () => page.waitForFunction(() => window.__szMap && window.__szMap.loaded() && typeof WIKI_PACKS !== 'undefined'
  && WIKI_PACKS.every((p) => p.index) && typeof WIKI_GEO_INDEX !== 'undefined' && WIKI_GEO_INDEX, null, { timeout: 180000, polling: 500 });
async function step(name, fn) {
  try { const r = await fn(); out[name] = r; out.checks[name] = !!r.pass; }
  catch (e) { out[name] = { error: String(e && e.message || e).slice(0, 300) }; out.checks[name] = false; }
}
try {
  if (process.env.UI_GATE_PREOPEN) {
    await page.goto(process.env.UI_GATE_PREOPEN, { timeout: 120000 });
    await page.waitForURL(/\/drive\/viewer\//, { timeout: 300000 });
  }
  await page.goto(ORIGIN + '/index.html', { timeout: 120000 });
  await ready();

  // The UI must be in the map's language: a map language without a UI table
  // FAILS (the viewer shows English: a Thai map with an English UI is not a
  // Thai map). Read through the viewer's own runtime, whatever the table
  // format inlined in index.html (JSON on main, {v:2,k,t,m} compressed on
  // viewer-i18n-core): SZ_UI_LANG / SZ_I18N after it chose the language,
  // szUiLanguages() where the viewer has it. Right-to-left languages
  // (SZ_RTL_LANGS, where the viewer has it) need <html dir="rtl">.
  await step('html_lang', async () => {
    const f = await page.evaluate((lang) => {
    const tables = typeof szUiLanguages === 'function' ? szUiLanguages() : null;
    const table = typeof SZ_I18N !== 'undefined' && SZ_I18N && typeof SZ_I18N === 'object' ? SZ_I18N : null;
    const uiLang = typeof SZ_UI_LANG !== 'undefined' ? SZ_UI_LANG : null;
    const shown = [];
    if (table) {
      for (const el of document.querySelectorAll('[data-i18n]')) {
        const v = table[el.getAttribute('data-i18n')];
        if (typeof v === 'string' && el.textContent.trim() === v.trim() && /[^\s\d.,:;!?()-]/.test(v)) shown.push(v);
        if (shown.length >= 3) break;
      }
    }
    const htmlLang = document.documentElement.lang || '';
    const dir = document.documentElement.dir || '';
    const rtl = typeof SZ_RTL_LANGS === 'object' && SZ_RTL_LANGS && !!SZ_RTL_LANGS[lang];
    const fallback = typeof SZ_UI_FALLBACK === 'object' && SZ_UI_FALLBACK ? SZ_UI_FALLBACK[lang] || null : null;
    return { lang, htmlLang, dir, uiLang, hasTable: !!table, shownCount: shown.length, rtl, fallback,
             viewerLanguages: Array.isArray(tables) ? tables : null, translatedShown: shown };
    }, LANG);
    const v = uiVerdict(f);
    return { htmlLang: f.htmlLang, dir: f.dir, uiLang: f.uiLang, hasTable: f.hasTable && f.uiLang === v.ui,
             fallback: v.fallbackUsed, viewerLanguages: f.viewerLanguages, translatedShown: f.translatedShown,
             rtlRequired: f.rtl, why: v.why, pass: v.pass };
  });

  // The city's label at the first zoom of ZOOMS that renders it (labels
  // collide: a city drawn at z7 can be hidden at z9); the popup check
  // clicks it at the same zoom.
  const ZOOMS = [...new Set([R.city.zoom, 7, 8, 6, 9, 10, 5].filter((z) => typeof z === 'number'))];
  await step('labels', async () => { const r = await page.evaluate(async ({ lang, city, base, isBase, zooms }) => {
    const map = window.__szMap;
    const key = 'name:' + lang;
    const settle = async (center, z) => {
      map.jumpTo({ center, zoom: z });
      for (let i = 0; i < 60 && !map.areTilesLoaded(); i++) await new Promise((r) => setTimeout(r, 500));
      await new Promise((r) => setTimeout(r, 1500));
    };
    // The viewer's rule (main 40f06b4, docs/language-matrix-prototype.md),
    // stated here on its own so a changed viewer fails the gate: names are
    // cut at the first ';' (szFirstName). On a map in another language than
    // its base's (SZ_LANG2): with regionLanguages, the map language's name
    // over the native one (OSM's name in its own script: name, else
    // name_int, else name:latin), one line when they agree or the tile has
    // no name in the map language; without, "local · map language's".
    const first = (v) => { if (typeof v !== 'string') return ''; const i = v.indexOf(';'); return i > 0 ? v.slice(0, i) : v; };
    let regionLangs = [], tileLangs = null;
    try {
      const cfg = await (await fetch('map-config.json')).json();
      regionLangs = Array.isArray(cfg.regionLanguages) ? cfg.regionLanguages.filter((x) => typeof x === 'string') : [];
      tileLangs = Array.isArray(cfg.tileLanguages) ? cfg.tileLanguages : null;
    } catch (e) { /* none */ }
    const region = typeof SZ_REGION !== 'undefined' && !!SZ_REGION;
    const want = (p) => {
      const local = first(szLabelOf(p));
      if (!SZ_LANG2) return local;
      if (region) {
        const mine = first(p[key]); const nat = first(p.name || p.name_int || p['name:latin'] || '');
        if (!mine) return nat || local;
        return !nat || nat === mine ? mine : mine + ' · ' + nat;
      }
      const other = first(p[key]);
      if (!other) return local;
      if (!local) return other;
      return other === local ? local : local + ' · ' + other;
    };
    // a label that must be bilingual: the map language's name differs from
    // the one it is shown with (the native name, or the local one)
    const twoNames = (p) => {
      const mine = first(p[key]);
      const other = region ? first(p.name || p.name_int || p['name:latin'] || '') : first(szLabelOf(p));
      return !!(SZ_LANG2 && mine && other && mine !== other);
    };
    const tfs = map.getStyle().layers.filter((l) => l.type === 'symbol').map((l) => JSON.stringify(l.layout && l.layout['text-field']) || '');
    const usesKey = tfs.some((t) => t.includes('"' + key + '"'));
    let cf = null, zoomUsed = null;
    for (const z of zooms) {
      await settle([city.lon, city.lat], z);
      cf = map.queryRenderedFeatures().find((f) => f.layer.type === 'symbol' && /^place/.test(f.layer.id)
        && f.properties.wikidata === city.qid) || null;
      if (cf) { zoomUsed = z; break; }
    }
    const cityShown = cf ? szDisplayLabelOf(cf.properties) : null;
    const cityWant = cf ? want(cf.properties) : null;
    const local = cf ? szLabelOf(cf.properties) : null;
    const other = cf ? cf.properties[key] : null;
    // some rendered labels that must be bilingual, shown as the rule says:
    // look at the city's view, then wider
    let bil = [], bilBad = [];
    for (const z of [zoomUsed || city.zoom, 5, 4, 3]) {
      await settle([city.lon, city.lat], z);
      const fs = map.queryRenderedFeatures().filter((f) => f.layer.type === 'symbol' && /^place/.test(f.layer.id)
        && twoNames(f.properties)).slice(0, 5);
      bil = fs.map((f) => f.layer.id + ': ' + szDisplayLabelOf(f.properties));
      bilBad = fs.filter((f) => szDisplayLabelOf(f.properties) !== want(f.properties) || !szDisplayLabelOf(f.properties).includes(' · '))
        .map((f) => ({ shown: szDisplayLabelOf(f.properties), want: want(f.properties) }));
      if (bil.length) break;
    }
    const second = lang !== base && !isBase;
    // A map language that is one of the region's own: its places' names are
    // the native ones, so bilingual labels may not be in view at all.
    const ownLang = regionLangs.includes(lang);
    return { SZ_LANG, SZ_LANG2, SZ_REGION: region, regionLanguages: regionLangs, tileLanguages: tileLangs,
             textFieldUsesKey: usesKey, second, ownLang,
             cityRendered: !!cf, zoom: zoomUsed, cityLocal: local, cityLocalFirst: local === null ? null : first(local),
             cityNameInLang: other || null, cityShown, cityWant, bilingualRendered: bil, bilingualWrong: bilBad };
  }, { lang: LANG, city: R.city, base: R.base_lang, isBase, zooms: ZOOMS });
    const v = labelsVerdict({ second: r.second, usesKey: r.textFieldUsesKey, lang2: r.SZ_LANG2, lang: LANG,
                              cityRendered: r.cityRendered, cityShown: r.cityShown, cityWant: r.cityWant,
                              cityLocal: r.cityLocalFirst, bilingual: r.bilingualRendered.length,
                              bilingualWrong: r.bilingualWrong.length, ownLang: r.ownLang, tileLanguages: r.tileLanguages });
    return Object.assign(r, { bilingualVacuous: v.vacuous, pass: v.pass });
  });
  const cityZoom = (out.labels && out.labels.zoom) || R.city.zoom;

  await step('popup_article', async () => {
    const r = { };
    const pt = await page.evaluate(async ({ city, zoom }) => {
      const map = window.__szMap;
      map.jumpTo({ center: [city.lon, city.lat], zoom });
      for (let i = 0; i < 60 && !map.areTilesLoaded(); i++) await new Promise((x) => setTimeout(x, 500));
      await new Promise((x) => setTimeout(x, 1500));
      const f = map.queryRenderedFeatures().find((x) => x.properties && x.properties.wikidata === city.qid && x.layer.type === 'symbol');
      if (!f) return null;
      const p = map.project(f.geometry.coordinates);
      const rect = map.getCanvas().getBoundingClientRect();
      const links = _wikiArticleLinks(city.qid, null).map((l) => ({ lang: l.lang, prefix: l.prefix, title: l.title }));
      return { x: rect.left + p.x, y: rect.top + p.y, links,
               packHasCity: WIKI_PACKS.filter((pk) => pk.qid && Object.prototype.hasOwnProperty.call(pk.qid, city.qid)).map((pk) => pk.lang) };
    }, { city: R.city, zoom: cityZoom });
    r.packHasCity = pt ? pt.packHasCity : [];
    if (!pt) return { pass: false, why: 'city label not rendered' };
    r.expected = pt.links[0] || null;
    await page.mouse.click(pt.x, pt.y);
    const btn = await page.waitForSelector('.wiki-article-btn', { timeout: 20000 });
    r.button = await btn.evaluate((e) => ({ lang: e.getAttribute('data-wiki-lang'), text: e.textContent.trim(),
      also: (e.parentNode.querySelector('.wiki-also') || {}).textContent || null }));
    await Promise.all([page.waitForNavigation({ timeout: 30000 }), btn.click()]);
    await page.waitForLoadState('load');
    await sleep(1000);
    // Articles carry up to 12 pictures, loading=lazy: those below the fold
    // would never load in a headless window. Load them all, then look.
    await page.evaluate(async () => {
      const imgs = [...document.images];
      imgs.forEach((i) => { if (i.loading === 'lazy') i.loading = 'eager'; });
      await Promise.all(imgs.map((i) => (i.complete ? null : new Promise((res) => {
        i.addEventListener('load', res, { once: true }); i.addEventListener('error', res, { once: true });
        setTimeout(res, 15000);
      }))));
    });
    r.article = await page.evaluate(() => ({ path: decodeURIComponent(location.pathname), title: document.title,
      images: [...document.images].map((i) => ({ src: i.getAttribute('src'), ok: i.complete && i.naturalWidth > 0 })),
      back: [...document.querySelectorAll('a')].map((a) => a.getAttribute('href') || '').filter((h) => /index\.html|^\/drive\/viewer\/$/.test(h)).slice(0, 2) }));
    // in the /drive/ viewer the service worker's bar links /drive/viewer/ (sw.js backBarHtml)
    const back = await page.$('a[href*="index.html"]') || (process.env.UI_GATE_PREOPEN ? await page.$('nav.sz-back a') : null);
    if (back) {
      await Promise.all([page.waitForNavigation({ timeout: 30000 }), back.click()]);
      await page.waitForFunction(() => window.__szMap && window.__szMap.loaded(), null, { timeout: 120000 });
      r.backTo = decodeURIComponent(new URL(page.url()).pathname);
    }
    const want = r.expected ? r.expected.prefix + 'wiki-article/' : null;
    const packLang = !isBase && LANG !== R.base_lang;
    // the map language's pack has the city -> its article must be the one offered and opened
    const mustPack = packLang && r.packHasCity.includes(LANG);
    r.pass = !!r.expected && !!want && r.article.path.includes('/' + want)
      && (!mustPack || (r.expected.prefix === `~w/${LANG}/` && r.button.lang === LANG
                        && r.article.path.includes(`/~w/${LANG}/wiki-article/`)))
      && (!packLang || r.expected.prefix === '' || r.expected.prefix === `~w/${LANG}/`)
      && r.article.images.every((i) => i.ok) && !!r.backTo
      && (r.backTo.endsWith('/index.html') || (!!process.env.UI_GATE_PREOPEN && r.backTo.endsWith('/drive/viewer/')));
    r.mapLangArticle = packLang ? r.expected && r.expected.prefix === `~w/${LANG}/` : null;
    return r;
  });
  await ready();

  // The map's own search: wait until it has FINISHED (no "Searching…" row,
  // data-sz-pending), count only real results (data-idx), and require the
  // expected place among them (review r2 #1: the placeholder passed a no-match query).
  async function search(spec) {
    const q = typeof spec === 'string' ? spec : spec.q;
    const expect = typeof spec === 'string' ? spec : (spec.expect || spec.q);
    await page.fill('#search-input', '');
    await page.fill('#search-input', q);
    await page.dispatchEvent('#search-input', 'input');
    let st = null;
    for (let i = 0; i < 60; i++) {
      await sleep(500);
      st = await page.evaluate(() => {
        const box = document.getElementById('search-results');
        const pending = !!box.querySelector('[data-sz-pending]');
        const none = !!box.querySelector('.search-no-results');
        const rows = [...box.querySelectorAll('.search-result[data-idx]')];
        const names = rows.map((e) => {
          const n = e.querySelector('.search-result-name');
          const first = n && n.firstChild && n.firstChild.nodeType === 3 ? n.firstChild.textContent : (n ? n.textContent : e.textContent);
          return first.trim();
        });
        const records = rows.map((e) => e.getAttribute('data-name') || '');
        return { pending, none, n: rows.length, names, records };
      });
      if (!st.pending && (st.n > 0 || st.none)) break;
    }
    const found = searchFound(st.names.map((shown, i) => ({ shown, name: st.records[i] })), expect);
    return { q, expect, finished: !st.pending && (st.n > 0 || st.none), n: st.n, noResults: st.none,
             names: st.names.slice(0, 5), records: st.records.slice(0, 5), found, pass: !st.pending && st.n > 0 && found };
  }
  await step('search_native', () => search(R.search.native));
  await step('search_english', () => search(R.search.english));
  const ml = R.search.map_lang && R.search.map_lang[LANG];
  if (ml) {
    try { out.search_map_lang = await search(ml); } catch (e) { out.search_map_lang = { error: String(e.message || e) }; }
  }
} catch (e) {
  out.error = String(e && e.message || e).slice(0, 300);
  out.checks.load = false;
}
out.pageErrors = errors.slice(0, 3);
out.pass = Object.values(out.checks).length > 0 && Object.values(out.checks).every(Boolean);
await browser.close();
console.log(JSON.stringify(out));
process.exit(out.pass ? 0 : 1);
