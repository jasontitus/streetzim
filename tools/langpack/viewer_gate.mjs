// Viewer gate: does the in-ZIM viewer actually USE the Wikipedia packs?
//
//   node viewer_gate.mjs ORIGIN LANG NAMED_QIDS COMMON_QIDS [base]
// NAMED_QIDS: the places named for the region (Sacramento, Los Angeles,
// Athens): the first one with articles must offer the map language's pack
// article first. COMMON_QIDS: Q-IDs with articles in the base AND the pack:
// the first must offer the pack's article first and the base's too.
//
// ORIGIN = http://localhost:PORT/content/<book> (kiwix-serve). Opens
// ORIGIN/index.html in headless Chromium, waits for map-config and every
// geo-index (base + packs) to load, then asserts:
//   merged:  WIKI_PACKS.length >= 1; the map language is first in
//            _wikiSources() (unless it is the base's language); for the first
//            Q-ID in QIDS that has articles, _wikiArticleLinks() offers the
//            map-language pack's article first (prefix ~w/<LANG>/), also the
//            base's, and the offered href resolves (HTTP 200, text/html).
//   base:    WIKI_PACKS.length == 0 and the base's own article is offered
//            (prefix '', wiki-article/...) and resolves.
// Prints one JSON line; exit 0 = PASS.
// PLAYWRIGHT_CORE: path to playwright-core's index.mjs (else the installed package);
// CHROME_PATH: the Chromium to drive; BROWSER_LIBS: extra LD_LIBRARY_PATH for it.
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');

const [ORIGIN, LANG, NAMED, COMMON, MODE] = process.argv.slice(2);
const isBase = MODE === 'base';
if (process.env.BROWSER_LIBS) {
  process.env.LD_LIBRARY_PATH = process.env.BROWSER_LIBS
    + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
}
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, headless: true });
const out = { origin: ORIGIN, lang: LANG, mode: isBase ? 'base' : 'merged' };
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(ORIGIN + '/index.html', { timeout: 120000 });
  await page.waitForFunction(() => typeof WIKI_PACKS !== 'undefined' && typeof _wikiSources === 'function'
    && typeof WIKI_GEO_INDEX !== 'undefined' && WIKI_GEO_INDEX
    && WIKI_PACKS.every((p) => p.index), null, { timeout: 180000, polling: 500 });
  const r = await page.evaluate(({ named, common }) => {
    const res = { packs: WIKI_PACKS.map((p) => p.lang), mapLang: WIKI_MAP_LANG, baseLang: WIKI_BASE_LANG,
                  sources: _wikiSources().map((s) => (s.base ? 'base:' : '') + (s.lang || '?')) };
    const pick = (qids, want) => {
      for (const q of qids) {
        const links = _wikiArticleLinks(q, null);
        if (want(links)) {
          return { qid: q, links: links.map((l) => ({ lang: l.lang, prefix: l.prefix, title: l.title })),
                   href: new URL(_wikiArticleHref(links[0].title, links[0].prefix), location.href).href };
        }
      }
      return null;
    };
    res.named = pick(named, (l) => l.length > 0);
    res.both = pick(common, (l) => l.length > 0 && (WIKI_PACKS.length === 0 || l.some((x) => x.prefix === '')));
    return res;
  }, { named: NAMED.split(',').filter(Boolean), common: COMMON.split(',').filter(Boolean) });
  Object.assign(out, r);
  for (const k of ['named', 'both']) {
    if (out[k] && out[k].href) {
      const resp = await page.request.get(out[k].href);
      out[k].status = resp.status();
      out[k].type = resp.headers()['content-type'];
    }
  }
  out.pageErrors = errors.slice(0, 3);
  const checks = {};
  const okHref = (x, prefix) => !!x && x.status === 200 && /html/.test(x.type || '')
    && (prefix ? decodeURIComponent(x.href).includes(`/${prefix}wiki-article/`)
               : /\/wiki-article\//.test(x.href) && !decodeURIComponent(x.href).includes('/~w/'));
  if (isBase) {
    checks.noPacks = r.packs.length === 0;
    const b = r.both || r.named;
    checks.baseArticleOffered = !!b && b.links[0].prefix === '';
    checks.hrefResolves = okHref(b, '');
  } else {
    // EXPECT_PACKS (the recipe's pack count) when given: exactly that many loaded;
    // a menu entry in the base's own language may have none
    const expectPacks = process.env.EXPECT_PACKS !== undefined ? +process.env.EXPECT_PACKS : null;
    checks.packsLoaded = expectPacks === null ? r.packs.length >= 1 : r.packs.length === expectPacks;
    out.expectPacks = expectPacks;
    const mapIsBase = !!r.baseLang && (r.baseLang === LANG || r.baseLang === LANG.split('-')[0]);
    const want = mapIsBase ? '' : `~w/${LANG}/`;
    checks.mapLangFirstSource = mapIsBase ? r.sources[0].startsWith('base:') : r.sources[0] === LANG;
    // the named place: when the map language's Wikipedia has it, it must come first
    const namedHasWant = !!r.named && r.named.links.some((l) => l.prefix === want);
    out.namedCheck = !r.named ? 'no article anywhere' : namedHasWant ? 'map-language article exists' : 'n/a: no map-language article';
    checks.namedPlaceOffersMapLang = !!r.named && (!namedHasWant || r.named.links[0].prefix === want);
    checks.namedHrefResolves = okHref(r.named, r.named ? r.named.links[0].prefix : want);
    checks.bothPlaceOffersMapLangFirst = !!r.both && r.both.links[0].prefix === want;
    checks.bothPlaceAlsoOffersBase = !!r.both && r.both.links.some((l) => l.prefix === '');
    checks.bothHrefResolves = okHref(r.both, want);
  }
  out.checks = checks;
  out.pass = Object.values(checks).every(Boolean);
} catch (e) {
  out.error = String(e && e.message || e).slice(0, 300);
  out.pass = false;
}
await browser.close();
console.log(JSON.stringify(out));
process.exit(out.pass ? 0 : 1);
