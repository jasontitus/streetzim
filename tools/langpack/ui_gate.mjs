// UI gate (review S7): what a visitor sees in the in-ZIM viewer.
//
//   node ui_gate.mjs ORIGIN LANG REGION_JSON [base]
//
// ORIGIN = http://localhost:PORT/content/<book>. REGION_JSON:
//   {"base_lang": "en", "city": {"qid": "Q65", "lon": -118.24, "lat": 34.05, "zoom": 7},
//    "search": {"native": "Sacramento", "english": "Los Angeles", "map_lang": {"th": "...", ...}}}
// Checks (each recorded with its evidence):
//   html_lang     <html lang> is the UI language, and a translated string is on screen
//                 (languages without a UI table: English UI, lang stays 'en')
//   labels        text-field reads name:<LANG> (merged, LANG != base); the city's label
//                 as displayed (szDisplayLabelOf) carries name:<LANG> when it differs;
//                 some rendered place shows a bilingual label
//   popup_article click the city's label -> popup -> "read article" button in the
//                 preferred Wikipedia -> the article page (prefix ~w/<LANG>/ when the
//                 pack has it) -> its images load -> back-to-map returns to the map
//   search_native / search_english  the map's own search box finds results
//   search_map_lang  (reported, not gated) the place's name in the map language
// Prints one JSON line; exit 0 = PASS.
// PLAYWRIGHT_CORE: path to playwright-core's index.mjs (else the installed package);
// CHROME_PATH: the Chromium to drive; BROWSER_LIBS: extra LD_LIBRARY_PATH for it.
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
import fs from 'node:fs';

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
  await page.goto(ORIGIN + '/index.html', { timeout: 120000 });
  await ready();

  await step('html_lang', () => page.evaluate((lang) => {
    let tables = {};
    try { tables = JSON.parse(document.getElementById('sz-i18n').textContent) || {}; } catch (e) { /* none */ }
    const has = Object.prototype.hasOwnProperty.call(tables, lang);
    const shown = [];
    if (has && SZ_I18N) {
      for (const el of document.querySelectorAll('[data-i18n]')) {
        const v = SZ_I18N[el.getAttribute('data-i18n')];
        if (typeof v === 'string' && el.textContent.trim() === v.trim() && /[^\s\d.,:;!?()-]/.test(v)) shown.push(v);
        if (shown.length >= 3) break;
      }
    }
    const htmlLang = document.documentElement.lang;
    return { htmlLang, uiLang: SZ_UI_LANG, hasTable: has, translatedShown: shown,
             pass: has ? (htmlLang.split('-')[0] === lang && SZ_UI_LANG === lang && shown.length > 0)
                       : (SZ_UI_LANG === 'en') };
  }, LANG));

  await step('labels', () => page.evaluate(async ({ lang, city, base, isBase }) => {
    const map = window.__szMap;
    const key = 'name:' + lang;
    const tfs = map.getStyle().layers.filter((l) => l.type === 'symbol').map((l) => JSON.stringify(l.layout && l.layout['text-field']) || '');
    const usesKey = tfs.some((t) => t.includes('"' + key + '"'));
    map.jumpTo({ center: [city.lon, city.lat], zoom: city.zoom });
    for (let i = 0; i < 60 && !map.areTilesLoaded(); i++) await new Promise((r) => setTimeout(r, 500));
    await new Promise((r) => setTimeout(r, 1500));
    const rendered = map.queryRenderedFeatures().filter((f) => f.layer.type === 'symbol' && /^place/.test(f.layer.id));
    const cf = rendered.find((f) => f.properties.wikidata === city.qid);
    const cityProps = cf ? cf.properties : null;
    const cityShown = cf ? szDisplayLabelOf(cf.properties) : null;
    const local = cf ? szLabelOf(cf.properties) : null;
    const other = cf ? cf.properties[key] : null;
    // some rendered label in a bilingual form: look at the city's view, then wider
    let bil = [];
    for (const z of [city.zoom, 5, 4, 3]) {
      if (z !== city.zoom) {
        map.jumpTo({ center: [city.lon, city.lat], zoom: z });
        for (let i = 0; i < 60 && !map.areTilesLoaded(); i++) await new Promise((r) => setTimeout(r, 500));
        await new Promise((r) => setTimeout(r, 1000));
      }
      bil = map.queryRenderedFeatures().filter((f) => f.layer.type === 'symbol' && f.properties[key]
        && f.properties[key] !== szLabelOf(f.properties) && szLabelOf(f.properties))
        .slice(0, 3).map((f) => f.layer.id + ': ' + szDisplayLabelOf(f.properties));
      if (bil.length) break;
    }
    const second = lang !== base && !isBase;
    let pass;
    if (!second) pass = SZ_LANG2 === '' && !!cf && cityShown === local;
    else pass = usesKey && SZ_LANG2 === lang && !!cf
      && (other && other !== local ? cityShown.includes(other.split(';')[0]) || cityShown.includes(other) : cityShown === local)
      && bil.length > 0 && bil.every((t) => t.includes(' · '));
    return { SZ_LANG, SZ_LANG2, textFieldUsesKey: usesKey, cityRendered: !!cf, cityLocal: local, cityNameInLang: other || null,
             cityShown, bilingualRendered: bil, pass };
  }, { lang: LANG, city: R.city, base: R.base_lang, isBase }));

  await step('popup_article', async () => {
    const r = { };
    const pt = await page.evaluate(async (city) => {
      const map = window.__szMap;
      map.jumpTo({ center: [city.lon, city.lat], zoom: city.zoom });
      for (let i = 0; i < 60 && !map.areTilesLoaded(); i++) await new Promise((x) => setTimeout(x, 500));
      await new Promise((x) => setTimeout(x, 1500));
      const f = map.queryRenderedFeatures().find((x) => x.properties && x.properties.wikidata === city.qid && x.layer.type === 'symbol');
      if (!f) return null;
      const p = map.project(f.geometry.coordinates);
      const rect = map.getCanvas().getBoundingClientRect();
      const links = _wikiArticleLinks(city.qid, null).map((l) => ({ lang: l.lang, prefix: l.prefix, title: l.title }));
      return { x: rect.left + p.x, y: rect.top + p.y, links,
               packHasCity: WIKI_PACKS.filter((pk) => pk.qid && Object.prototype.hasOwnProperty.call(pk.qid, city.qid)).map((pk) => pk.lang) };
    }, R.city);
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
    r.article = await page.evaluate(() => ({ path: decodeURIComponent(location.pathname), title: document.title,
      images: [...document.images].map((i) => ({ src: i.getAttribute('src'), ok: i.complete && i.naturalWidth > 0 })),
      back: [...document.querySelectorAll('a')].map((a) => a.getAttribute('href') || '').filter((h) => /index\.html/.test(h)).slice(0, 2) }));
    const back = await page.$('a[href*="index.html"]');
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
      && r.article.images.every((i) => i.ok) && !!r.backTo && r.backTo.endsWith('/index.html');
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
        return { pending, none, n: rows.length, names };
      });
      if (!st.pending && (st.n > 0 || st.none)) break;
    }
    const found = st.names.some((n) => n === expect || n.startsWith(expect + ' '));
    return { q, expect, finished: !st.pending && (st.n > 0 || st.none), n: st.n, noResults: st.none,
             names: st.names.slice(0, 5), found, pass: !st.pending && st.n > 0 && found };
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
