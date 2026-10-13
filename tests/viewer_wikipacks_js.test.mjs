// Several Wikipedias in one map (docs/language-matrix-prototype.md): the
// Q-ID join of the base's and each pack's wiki-geo-index.json, the order the
// articles are offered in, the article links, and the bilingual map labels
// of a map shown in another language than its base's
// (100-wiki-bridge-and-viewport.js, 130-lowzoom-lakes.js). Without
// wikiPacks everything must be exactly what it was.
//
//   node tests/viewer_wikipacks_js.test.mjs
import assert from 'node:assert';
import fs from 'node:fs';

const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
// The UI-string runtime (szT, …) as globals, as index.html has it.
(0, eval)(fs.readFileSync(`${REPO}/resources/viewer/i18n/runtime.js`, 'utf8'));
const PARTS_DIR = `${REPO}/resources/viewer/src/index`;
const HTML = fs.readdirSync(PARTS_DIR).filter((n) => /^\d{3}-[\w-]+\.(html|js)$/.test(n)).sort()
  .map((n) => fs.readFileSync(`${PARTS_DIR}/${n}`, 'utf8')).join('');

let pass = 0;
async function ok(name, fn) {
  try { await fn(); console.log('ok   ', name); pass++; }
  catch (e) { console.error('FAIL ', name, '\n      ', e.stack || e.message); process.exitCode = 1; }
}
function slice(from, to) {
  const a = HTML.indexOf(from), b = HTML.indexOf(to, a);
  assert.ok(a >= 0 && b > a, `${from} .. ${to} not found in the viewer parts`);
  return HTML.slice(a, b);
}

// ---------------------------------------------------------------------
// The wiki code: geo-index bounds, the base's article path, the packs,
// openWikiArticle. Globals the page has are parameters; the indexes are
// set through `set`.
const WIKI_SRC = slice('// BEGIN wiki-geo-bounds', '// END wiki-geo-bounds')
  + slice('// BEGIN wiki-article-path', '// Publish the REAL visible window size');

function fakeDocument() {
  function el(tag) {
    const e = {
      tag, children: [], style: {}, attrs: {}, textContent: '', className: '', href: '', on: {},
      appendChild(c) { e.children.push(c); return c; },
      setAttribute(k, v) { e.attrs[k] = String(v); },
      getAttribute(k) { return k in e.attrs ? e.attrs[k] : null; },
      addEventListener(t, f) { e.on[t] = f; },
      text() { return [e.textContent].concat(e.children.map((c) => c.text())).join(''); },
    };
    return e;
  }
  return { createElement: el, createTextNode: (t) => ({ textContent: t, text: () => t }) };
}

function loadWiki() {
  const nav = { href: null, replaced: null };
  const window = { location: { set href(v) { nav.href = v; }, get href() { return nav.href; } } };
  const history = { replaceState: (a, b, u) => { nav.replaced = u; } };
  const fn = new Function('window', 'document', 'history', 'location', 'baseUrl',
    'var WIKI_GEO_INDEX = null, WIKI_GEO_QID = null, WIKI_QID_TITLES = null;\n' + WIKI_SRC +
    '\nreturn { set: function(idx, bridge) { WIKI_GEO_INDEX = idx; WIKI_GEO_QID = null;' +
    '  if (idx) { WIKI_GEO_QID = {}; for (var t in idx) { var q = idx[t] && idx[t][3]; if (q) WIKI_GEO_QID[q] = t; } }' +
    '  WIKI_QID_TITLES = bridge || null; },' +
    ' packs: function() { return WIKI_PACKS; }, langs: function() { return [WIKI_MAP_LANG, WIKI_BASE_LANG]; },' +
    ' _wikiArticlePath, _wikiPacksFromConfig, wikiPacksInit, wikiPackSetIndex, _wikiEntryQid, _wikiEntryDesc,' +
    ' _wikiSources, _wikiArticleLinks, _wikiDetailLinks, _wikiPanelItems, _wikiArticleHref, _wikiLangName,' +
    ' _wikiAlsoIn, openWikiArticle, wikiGeoTitlesInBounds };');
  const api = fn(window, fakeDocument(), history, { pathname: '/C/index.html', search: '' },
                 'http://kiwix/content/osm_th_liechtenstein/');
  return { ...api, nav };
}

// A base (English, [lat, lon, type, qid, desc] as zim_writer writes it) and
// two packs ([lat, lon, type, desc, qid] as the pack spec has it).
const BASE = {
  'Vaduz': [47.141, 9.521, 'place', 'Q1844', 'capital of Liechtenstein'],
  'Eschen': [47.21, 9.52, 'poi', 'Q156907', 'genus of plants'],          // the tree
  'Malbun': [47.10, 9.61, 'place', 'Q675040', ''],
  'Old_Bridge': [47.16, 9.50, 'poi', null, ''],                         // no Q-ID
};
const DE = {
  'Vaduz': [47.141, 9.521, 'place', 'Hauptort von Liechtenstein', 'Q1844'],
  'Eschen_(Liechtenstein)': [47.212, 9.522, 'place', 'Gemeinde', 'Q4540'],
  'Schloss_Vaduz': [47.139, 9.524, 'poi', 'Schloss', 'Q685893'],
};
const TH = {
  'วาดุซ': [47.141, 9.521, 'place', 'เมืองหลวง', 'Q1844'],
  'Expo_Park/USC_station': [47.13, 9.52, 'poi', '', 'Q999'],
};
const MERGED = { language: 'th', baseLanguage: 'en', hasWikiArticles: true,
  wikiPacks: [{ lang: 'de', prefix: '~w/de/', articles: 3 }, { lang: 'th', prefix: '~w/th/', articles: 2 }] };

function merged(config = MERGED, idx = { de: DE, th: TH }) {
  const W = loadWiki();
  W.set(BASE);
  W.wikiPacksInit(config);
  for (const p of W.packs()) if (idx[p.lang]) assert.ok(W.wikiPackSetIndex(p, idx[p.lang]));
  return W;
}
const langs = (links) => links.map((l) => l.lang);

await ok('wikiPacks are read as documented; anything else is ignored', () => {
  const W = loadWiki();
  assert.deepStrictEqual(W._wikiPacksFromConfig(MERGED),
    [{ lang: 'de', prefix: '~w/de/' }, { lang: 'th', prefix: '~w/th/' }]);
  assert.deepStrictEqual(W._wikiPacksFromConfig({}), []);
  assert.deepStrictEqual(W._wikiPacksFromConfig({ wikiPacks: 'th' }), []);
  // A prefix goes into every article URL: only ~w/<lang>/ of the pack's
  // own language, lower case, once each.
  const bad = ['../', '~w/de', '~w/de/x/', 'w/de/', '~w/DE/', 'https://x/', '~w/../', '', '~w/th/'];
  assert.deepStrictEqual(W._wikiPacksFromConfig({ wikiPacks: bad.map((p) => ({ lang: 'de', prefix: p })) }), []);
  assert.deepStrictEqual(W._wikiPacksFromConfig({ wikiPacks: [
    { lang: 'de', prefix: '~w/de/' }, { lang: 'de', prefix: '~w/de/' }, { lang: 'x y', prefix: '~w/fr/' },
    null, { prefix: '~w/es/' }, { lang: 'zh-yue', prefix: '~w/zh-yue/' }, { lang: 'TH', prefix: '~w/th/' }] }),
    [{ lang: 'de', prefix: '~w/de/' }, { lang: 'zh-yue', prefix: '~w/zh-yue/' }, { lang: 'th', prefix: '~w/th/' }]);
});

await ok('language codes in any case; a map in zh-Hans takes the zh Wikipedia first', () => {
  let W = merged({ ...MERGED, language: 'TH', baseLanguage: 'EN' });
  assert.deepStrictEqual(W.langs(), ['th', 'en']);
  assert.deepStrictEqual(langs(W._wikiArticleLinks('Q1844', null)), ['th', 'en', 'de']);
  W = merged({ language: 'zh-Hans', baseLanguage: 'en',
               wikiPacks: [{ lang: 'de', prefix: '~w/de/' }, { lang: 'ZH', prefix: '~w/zh/' }] },
             { de: DE, zh: { '瓦杜茲': [47.141, 9.521, 'place', '', 'Q1844'] } });
  assert.deepStrictEqual(langs(W._wikiArticleLinks('Q1844', null)), ['zh', 'en', 'de']);
  W = merged({ ...MERGED, language: 'EN' });                       // the base's language
  assert.deepStrictEqual(langs(W._wikiArticleLinks('Q1844', null)), ['en', 'de', 'th']);
});

await ok('the base language: baseLanguage, else the map language when no pack has it', () => {
  const W = loadWiki();
  W.wikiPacksInit(MERGED);
  assert.deepStrictEqual(W.langs(), ['th', 'en']);
  W.wikiPacksInit({ language: 'en', wikiPacks: MERGED.wikiPacks });     // en map of an en base
  assert.deepStrictEqual(W.langs(), ['en', 'en']);
  W.wikiPacksInit({ language: 'th', wikiPacks: MERGED.wikiPacks });     // unknown: not guessed
  assert.deepStrictEqual(W.langs(), ['th', '']);
  W.wikiPacksInit({ language: 'de' });
  assert.deepStrictEqual(W.packs(), []);
});

await ok('Q-ID and description: each source in its own slots, a pack also the base\'s way round', () => {
  const W = loadWiki();
  assert.strictEqual(W._wikiEntryQid(DE.Vaduz, true), 'Q1844');
  assert.strictEqual(W._wikiEntryDesc(DE.Vaduz, true), 'Hauptort von Liechtenstein');
  assert.strictEqual(W._wikiEntryQid(BASE.Vaduz, false), 'Q1844');
  assert.strictEqual(W._wikiEntryDesc(BASE.Vaduz, false), 'capital of Liechtenstein');
  assert.strictEqual(W._wikiEntryQid(BASE.Old_Bridge, false), null);
  assert.strictEqual(W._wikiEntryQid([1, 2, 'poi', 'Q-ish text', ''], false), null);
  // A description that looks like a Q-ID does not fool either reader.
  assert.strictEqual(W._wikiEntryQid([1, 2, 'poi', 'Q1844', 'Q7'], false), 'Q1844');
  assert.strictEqual(W._wikiEntryDesc([1, 2, 'poi', 'Q1844', 'Q7'], false), 'Q7');
  assert.strictEqual(W._wikiEntryQid([1, 2, 'poi', null, 'Q7'], false), null);
  assert.strictEqual(W._wikiEntryQid([1, 2, 'poi', 'Q7', 'Q1844'], true), 'Q1844');
  assert.strictEqual(W._wikiEntryDesc([1, 2, 'poi', 'Q7', 'Q1844'], true), 'Q7');
  // A pack written in the base's order.
  assert.strictEqual(W._wikiEntryQid([1, 2, 'poi', 'Q1844', 'Hauptort'], true), 'Q1844');
  assert.strictEqual(W._wikiEntryDesc([1, 2, 'poi', 'Q1844', 'Hauptort'], true), 'Hauptort');
  assert.strictEqual(W._wikiEntryDesc([1, 2, 'poi', 'Hauptort', null], true), 'Hauptort');
  // The base's Q-ID slot drives the join: a base entry whose description is
  // a Q-ID string is not joined under it.
  const W2 = loadWiki();
  W2.set({ 'Vaduz': [47.141, 9.521, 'place', 'Q1844', 'Q49657'] });
  W2.wikiPacksInit(MERGED);
  W2.packs().forEach((pk) => W2.wikiPackSetIndex(pk, { de: DE, th: TH }[pk.lang]));
  assert.deepStrictEqual(langs(W2._wikiArticleLinks('Q1844', null)), ['th', 'en', 'de']);
  assert.deepStrictEqual(langs(W2._wikiArticleLinks('Q49657', null)), []);
  const rows = W2._wikiPanelItems(9.4, 9.7, 47.0, 47.3, () => 1);
  assert.deepStrictEqual(rows.map((r) => [r.title, r.qid]).sort(),
    [['Eschen_(Liechtenstein)', 'Q4540'], ['Expo_Park/USC_station', 'Q999'], ['Schloss_Vaduz', 'Q685893'], ['วาดุซ', 'Q1844']]);
  assert.ok(!W.wikiPackSetIndex({}, null) && !W.wikiPackSetIndex({}, [1]) && !W.wikiPackSetIndex({}, 'x'));
});

await ok('order: the map language, then the base, then the other packs', () => {
  let W = merged();
  assert.deepStrictEqual(W._wikiSources().map((s) => s.prefix), ['~w/th/', '', '~w/de/']);
  assert.deepStrictEqual(langs(W._wikiArticleLinks('Q1844', null)), ['th', 'en', 'de']);
  // A German map of the same file: German, English, Thai.
  W = merged({ ...MERGED, language: 'de' });
  assert.deepStrictEqual(langs(W._wikiArticleLinks('Q1844', null)), ['de', 'en', 'th']);
  // The map in the base's language: the base first.
  W = merged({ ...MERGED, language: 'en' });
  assert.deepStrictEqual(langs(W._wikiArticleLinks('Q1844', null)), ['en', 'de', 'th']);
  // A map language with no pack: the base, then the packs as listed.
  W = merged({ ...MERGED, language: 'fr' });
  assert.deepStrictEqual(langs(W._wikiArticleLinks('Q1844', 'en:Vaduz')), ['en', 'de', 'th']);
});

await ok('articles are joined by Q-ID, also when the record has a `w` tag', () => {
  const W = merged();
  // The tag names the German article; the Q-ID finds the Thai and German ones.
  assert.deepStrictEqual(W._wikiArticleLinks('Q4540', 'de:Eschen (Liechtenstein)'),
    [{ lang: 'de', prefix: '~w/de/', title: 'Eschen_(Liechtenstein)' }]);
  // English "Eschen" is another place (the tree): its Q-ID differs.
  assert.deepStrictEqual(W._wikiArticleLinks('Q4540', 'en:Eschen'),
    [{ lang: 'de', prefix: '~w/de/', title: 'Eschen_(Liechtenstein)' }]);
  // A pack's title for the tag is refused when its entry is another place.
  assert.deepStrictEqual(W._wikiArticleLinks('Q99', 'de:Schloss Vaduz'), []);
  assert.deepStrictEqual(W._wikiArticleLinks('Q685893', 'de:Schloss Vaduz'),
    [{ lang: 'de', prefix: '~w/de/', title: 'Schloss_Vaduz' }]);
  // Without a Q-ID the tag is taken in its own language only.
  assert.deepStrictEqual(W._wikiArticleLinks(null, 'de:Schloss Vaduz'),
    [{ lang: 'de', prefix: '~w/de/', title: 'Schloss_Vaduz' }]);
  assert.deepStrictEqual(W._wikiArticleLinks(null, 'th:Schloss Vaduz'), []);
  assert.deepStrictEqual(W._wikiArticleLinks(null, 'en:Vaduz'), [{ lang: 'en', prefix: '', title: 'Vaduz' }]);
  // A base article with no Q-ID in the index still answers its tag.
  assert.deepStrictEqual(W._wikiArticleLinks('Q5', 'en:Old Bridge'), [{ lang: 'en', prefix: '', title: 'Old_Bridge' }]);
  assert.deepStrictEqual(W._wikiArticleLinks('Q12345', null), []);
  assert.deepStrictEqual(W._wikiArticleLinks(null, null), []);
  // Nothing but own properties: "constructor" is no title or Q-ID.
  assert.deepStrictEqual(W._wikiArticleLinks('constructor', 'de:constructor'), []);
});

await ok('a pack whose index has not loaded is skipped', () => {
  const W = merged(MERGED, { de: DE });
  assert.deepStrictEqual(langs(W._wikiArticleLinks('Q1844', null)), ['en', 'de']);
  const B = loadWiki();                                  // nothing loaded at all
  B.wikiPacksInit(MERGED);
  assert.deepStrictEqual(B._wikiArticleLinks('Q1844', 'en:Vaduz'), []);
});

await ok('without wikiPacks the link is exactly _wikiArticlePath\'s', () => {
  const W = loadWiki();
  W.set(BASE, { Q9: 'Not_Bundled' });
  W.wikiPacksInit({ language: 'en', hasWikiArticles: true });
  const cases = [['Q1844', 'en:Vaduz'], [null, 'en:Vaduz'], ['Q1844', null], ['Q4540', 'en:Eschen'],
                 ['Q9', null], ['Q1', 'de:Vaduz'], [null, null], [null, 'constructor']];
  for (const [q, w] of cases) {
    const t = W._wikiArticlePath(q, w);
    assert.deepStrictEqual(W._wikiArticleLinks(q, w), t ? [{ lang: 'en', prefix: '', title: t }] : [], `${q} ${w}`);
  }
  // The tree's article for the village's tag: as before (no packs, no change).
  assert.deepStrictEqual(W._wikiArticleLinks('Q4540', 'en:Eschen'), [{ lang: 'en', prefix: '', title: 'Eschen' }]);
});

await ok('article links: <prefix>wiki-article/<Title>, slashes kept', () => {
  const W = merged();
  assert.strictEqual(W._wikiArticleHref('Vaduz', ''), 'wiki-article/Vaduz');
  assert.strictEqual(W._wikiArticleHref('Vaduz'), 'wiki-article/Vaduz');
  assert.strictEqual(W._wikiArticleHref('วาดุซ', '~w/th/'), '~w/th/wiki-article/' + encodeURIComponent('วาดุซ'));
  assert.strictEqual(W._wikiArticleHref('Expo_Park/USC_station', '~w/th/'), '~w/th/wiki-article/Expo_Park/USC_station');
  assert.strictEqual(W._wikiArticleHref('AT&T Park?', '~w/de/'), '~w/de/wiki-article/AT%26T%20Park%3F');
  W.openWikiArticle('Schloss_Vaduz', '~w/de/');
  assert.strictEqual(W.nav.href, 'http://kiwix/content/osm_th_liechtenstein/~w/de/wiki-article/Schloss_Vaduz');
  W.openWikiArticle('Vaduz');
  assert.strictEqual(W.nav.href, 'http://kiwix/content/osm_th_liechtenstein/wiki-article/Vaduz');
});

await ok('"Also in": one link per other language, named in its own language', () => {
  const W = merged();
  const links = W._wikiArticleLinks('Q1844', null);
  const row = W._wikiAlsoIn(links);
  assert.strictEqual(row.attrs['data-wiki-also'], 'en,de');
  const a = row.children.filter((c) => c.tag === 'a');
  assert.deepStrictEqual(a.map((x) => x.attrs['data-wiki-path']), ['wiki-article/Vaduz', '~w/de/wiki-article/Vaduz']);
  assert.deepStrictEqual(a.map((x) => x.href), ['http://kiwix/content/osm_th_liechtenstein/wiki-article/Vaduz',
    'http://kiwix/content/osm_th_liechtenstein/~w/de/wiki-article/Vaduz']);
  assert.match(row.text(), /^Also in: English · Deutsch$/);
  let stopped = 0;
  a[1].on.click({ preventDefault() { stopped++; }, stopPropagation() { stopped++; } });
  assert.strictEqual(stopped, 2);
  assert.strictEqual(W.nav.href, 'http://kiwix/content/osm_th_liechtenstein/~w/de/wiki-article/Vaduz');
  assert.strictEqual(W._wikiAlsoIn(links.slice(0, 1)), null);
  assert.strictEqual(W._wikiAlsoIn([]), null);
  assert.strictEqual(W._wikiLangName('th'), 'ไทย');
  assert.strictEqual(W._wikiLangName('el'), 'Ελληνικά');
  assert.strictEqual(W._wikiLangName(''), 'Wikipedia');
});

await ok('the Wiki panel: one row per place, in the preferred language', () => {
  const W = merged();
  const pri = (t) => (t === 'place' ? 80 : 40);
  const rows = W._wikiPanelItems(9.4, 9.7, 47.0, 47.3, pri);
  const by = Object.fromEntries(rows.map((r) => [r.qid || r.title, r]));
  assert.strictEqual(rows.length, 7, JSON.stringify(rows.map((r) => r.title)));
  assert.deepStrictEqual([by.Q1844.title, by.Q1844.prefix, by.Q1844.lang, by.Q1844.desc], ['วาดุซ', '~w/th/', 'th', 'เมืองหลวง']);
  assert.deepStrictEqual(by.Q1844.coords, [9.521, 47.141]);
  assert.deepStrictEqual([by.Q685893.title, by.Q685893.prefix, by.Q685893.desc], ['Schloss_Vaduz', '~w/de/', 'Schloss']);
  assert.deepStrictEqual([by.Q675040.prefix, by.Q675040.priority], ['', 80]);
  assert.deepStrictEqual([by.Old_Bridge.prefix, by.Old_Bridge.qid], ['', null]);
  // The tree and the village have different Q-IDs: two rows.
  assert.ok(by.Q156907 && by.Q4540);
  // Out of view: nothing.
  assert.deepStrictEqual(W._wikiPanelItems(0, 1, 0, 1, pri), []);
  // A row's detail: the place's articles by Q-ID, the row's own first when
  // the Q-ID does not find it.
  assert.deepStrictEqual(langs(W._wikiDetailLinks('Q1844', 'วาดุซ', '~w/th/')), ['th', 'en', 'de']);
  assert.deepStrictEqual(W._wikiDetailLinks(null, 'Old_Bridge', ''), [{ lang: 'en', prefix: '', title: 'Old_Bridge' }]);
  assert.deepStrictEqual(W._wikiDetailLinks('Q77', 'Schloss_Vaduz', '~w/de/'), [{ lang: 'de', prefix: '~w/de/', title: 'Schloss_Vaduz' }]);
});

await ok('the Wiki panel without wikiPacks: the old rows, field for field', () => {
  const W = loadWiki();
  W.set(BASE);
  W.wikiPacksInit({ language: 'en' });
  const pri = (t) => (t === 'place' ? 80 : 40);
  const rows = W._wikiPanelItems(9.4, 9.7, 47.0, 47.3, pri);
  // What updateSidebarFromGeo built before (410), from the same titles.
  const old = W.wikiGeoTitlesInBounds(9.4, 9.7, 47.0, 47.3).map((title) => {
    const g = BASE[title];
    return { title, name: title.replace(/_/g, ' '), qid: g[3] || null, type: g[2] || '',
             coords: [g[1], g[0]], desc: g[4] || '', priority: pri(g[2] || '') };
  });
  assert.deepStrictEqual(rows, old);
  assert.strictEqual(rows.length, 4);
});

await ok('the viewer wires the links in everywhere an article opens', () => {
  // Every caller of the old single-path API now goes through the links.
  for (const part of ['200-find-results-view.js', '210-place-detail-sheet.js', '400-wikidata-popup.js']) {
    const src = fs.readFileSync(`${PARTS_DIR}/${part}`, 'utf8');
    assert.doesNotMatch(src, /_wikiArticlePath\(/, part);
    assert.match(src, /_wikiArticleLinks\(/, part);
  }
  const panel = fs.readFileSync(`${PARTS_DIR}/410-wiki-sidebar.js`, 'utf8');
  assert.match(panel, /wikiPacksInit\(config\)/);
  assert.match(panel, /pack\.prefix \+ 'wiki-geo-index\.json'/);
  assert.match(panel, /_wikiPanelItems\(/);
});

await ok('a Q-ID two titles carry: the later one, in a pack as in the base', () => {
  const W = loadWiki();
  W.set({ 'Old': [47.1, 9.5, 'place', 'Q1844', ''], 'New': [47.1, 9.5, 'place', 'Q1844', ''] });
  W.wikiPacksInit({ language: 'en', wikiPacks: [{ lang: 'de', prefix: '~w/de/' }] });
  W.wikiPackSetIndex(W.packs()[0], { 'Alt': [47.1, 9.5, 'place', '', 'Q1844'], 'Neu': [47.1, 9.5, 'place', '', 'Q1844'] });
  assert.deepStrictEqual(W._wikiArticleLinks('Q1844', null).map((l) => l.title), ['New', 'Neu']);
});

await ok('a detail\'s links: its own entry first when the Q-ID finds another, the rest in order', () => {
  const W = merged();
  // The Q-ID finds the row's article: the order stays the preference order.
  assert.deepStrictEqual(W._wikiDetailLinks('Q1844', 'Vaduz', '~w/de/').map((l) => l.prefix + l.title),
    ['~w/th/วาดุซ', 'Vaduz', '~w/de/Vaduz']);
  // It does not (another title in the same pack): the row's first, that
  // pack's other link dropped, the others in order.
  assert.deepStrictEqual(W._wikiDetailLinks('Q1844', 'Schloss_Vaduz', '~w/de/').map((l) => [l.lang, l.prefix + l.title]),
    [['de', '~w/de/Schloss_Vaduz'], ['th', '~w/th/วาดุซ'], ['en', 'Vaduz']]);
  assert.deepStrictEqual(W._wikiDetailLinks('Q1844', 'Other', '').map((l) => [l.lang, l.prefix + l.title]),
    [['en', 'Other'], ['th', '~w/th/วาดุซ'], ['de', '~w/de/Vaduz']]);
  assert.deepStrictEqual(W._wikiDetailLinks('Q1844', null, null).map((l) => l.lang), ['th', 'en', 'de']);
});

await ok('language names: endonyms, capitalised; an unknown code in capitals', () => {
  const W = loadWiki();
  assert.strictEqual(W._wikiLangName('es'), 'Español');
  assert.strictEqual(W._wikiLangName('de'), 'Deutsch');
  assert.strictEqual(W._wikiLangName('zh'), '中文');
  assert.strictEqual(W._wikiLangName('qqx'), 'QQX');
});

await ok('"Also in" in the UI language: German, Thai, Chinese tables', () => {
  const W = merged();
  for (const [lang, want] of [['de', 'Auch auf:'], ['th', 'ภาษาอื่น:'], ['zh', '其他语言：']]) {
    const table = JSON.parse(fs.readFileSync(`${REPO}/resources/viewer/i18n/${lang}.json`, 'utf8'));
    assert.strictEqual(table['wiki.also_in'], want, lang);
    globalThis.SZ_I18N = table;
    try {
      const row = W._wikiAlsoIn(W._wikiArticleLinks('Q1844', null));
      assert.strictEqual(row.children[0].textContent, want + ' ', lang);
    } finally { globalThis.SZ_I18N = null; }
  }
  assert.strictEqual(W._wikiAlsoIn(W._wikiArticleLinks('Q1844', null)).children[0].textContent, 'Also in: ');
});

// ---------------------------------------------------------------------
// Bilingual labels (130-lowzoom-lakes.js).
const LBL = new Function(slice('var SZ_LANG = ', 'function makeStyle') +
  '\nreturn { set: (a, b) => { SZ_LANG = a; SZ_LANG2 = b || ""; }, field: szLabelField, line: szLineLabelField,' +
  ' langs: szLabelLanguages, of: szLabelOf, shown: szDisplayLabelOf };')();

// Enough of MapLibre's expression language to evaluate a label: get, has,
// coalesce, case, all, ==, != and format (a formatted value is returned as
// [[text, options], …]).
function evalExpr(e, props) {
  if (!Array.isArray(e)) return e;
  const [op, ...a] = e;
  const v = (x) => evalExpr(x, props);
  switch (op) {
    case 'get': return Object.prototype.hasOwnProperty.call(props, a[0]) ? props[a[0]] : null;
    case 'has': return Object.prototype.hasOwnProperty.call(props, a[0]);
    case 'coalesce': for (const x of a) { const r = v(x); if (r !== null && r !== undefined) return r; } return null;
    case 'case': for (let i = 0; i + 1 < a.length; i += 2) if (v(a[i])) return v(a[i + 1]); return v(a[a.length - 1]);
    case 'all': return a.every((x) => v(x) === true);
    case '!=': return v(a[0]) !== v(a[1]);
    case '==': return v(a[0]) === v(a[1]);
    case 'format': {
      const out = [];
      for (let i = 0; i < a.length; i += 2) out.push([String(v(a[i]) ?? ''), a[i + 1]]);
      return out;
    }
    default: throw new Error('no ' + op);
  }
}
const text = (r) => (Array.isArray(r) ? r.map((s) => s[0]).join('') : r);
const TODAY_EN = ['coalesce', ['get', 'name:latin'], ['get', 'name_int'], ['get', 'name']];
const TODAY_FR = ['coalesce', ['get', 'name:fr'], ['get', 'name'], ['get', 'name_int'], ['get', 'name:latin']];

await ok('labels: one language as before when the map is in the base\'s', () => {
  LBL.set('en'); assert.deepStrictEqual(LBL.field(), TODAY_EN);
  LBL.set('fr'); assert.deepStrictEqual(LBL.field(), TODAY_FR);
  assert.deepStrictEqual(LBL.langs({ language: 'fr' }), ['fr', '']);
  assert.deepStrictEqual(LBL.langs({ language: 'fr', baseLanguage: 'fr' }), ['fr', '']);
  assert.deepStrictEqual(LBL.langs({}), ['en', '']);
  assert.deepStrictEqual(LBL.langs(null), ['en', '']);
  assert.deepStrictEqual(LBL.langs({ language: 'th', baseLanguage: 'en' }), ['en', 'th']);
  assert.deepStrictEqual(LBL.langs({ baseLanguage: 'el' }), ['el', 'en']);
});

await ok('labels: local name first, the map language\'s under it, smaller', () => {
  LBL.set('en', 'th');
  const f = LBL.field();
  // Both names: two lines, the second at 0.8.
  const both = evalExpr(f, { 'name:latin': 'Vaduz', name: 'Vaduz', 'name:th': 'วาดุซ' });
  assert.deepStrictEqual(both, [['Vaduz', {}], ['\n', {}], ['วาดุซ', { 'font-scale': 0.8 }]]);
  // No name:th: the local name alone (plain text, as before).
  assert.strictEqual(evalExpr(f, { 'name:latin': 'Malbun', name_int: 'Malbun' }), 'Malbun');
  // name:th equal to the local name: no second line.
  assert.strictEqual(evalExpr(f, { 'name:latin': 'Vaduz', 'name:th': 'Vaduz' }), 'Vaduz');
  // No local name at all: the map language's name rather than nothing.
  assert.strictEqual(evalExpr(f, { 'name:th': 'วาดุซ' }), 'วาดุซ');
  assert.strictEqual(evalExpr(f, {}), null);
  // The local name is the base's own rule: name:latin first for English.
  assert.strictEqual(text(evalExpr(f, { 'name:latin': 'Tokyo', name: '東京', 'name:th': 'โตเกียว' })), 'Tokyo\nโตเกียว');
  // A Greek base shown in English: name:el first, then name:en.
  LBL.set('el', 'en');
  assert.strictEqual(text(evalExpr(LBL.field(), { 'name:el': 'Αθήνα', name: 'Αθήνα', 'name:en': 'Athens' })), 'Αθήνα\nAthens');
  assert.strictEqual(evalExpr(LBL.field(), { name: 'Πλάκα' }), 'Πλάκα');
  LBL.set('en');
});

await ok('popup and list names: "local · visitor\'s" on a merged map, else the local name', () => {
  LBL.set('en', 'th');
  assert.strictEqual(LBL.shown({ 'name:latin': 'Vaduz', 'name:th': 'วาดุซ' }), 'Vaduz · วาดุซ');
  assert.strictEqual(LBL.shown({ 'name:latin': 'Vaduz', 'name:th': 'Vaduz' }), 'Vaduz');
  assert.strictEqual(LBL.shown({ 'name:latin': 'Malbun' }), 'Malbun');
  assert.strictEqual(LBL.shown({ 'name:th': 'วาดุซ' }), 'วาดุซ');
  assert.strictEqual(LBL.shown({}), '');
  // Search records are named by the local name: lookups keep it.
  assert.strictEqual(LBL.of({ 'name:latin': 'Vaduz', 'name:th': 'วาดุซ' }), 'Vaduz');
  assert.deepStrictEqual(LBL.line(), TODAY_EN);
  LBL.set('en');
  assert.strictEqual(LBL.shown({ 'name:latin': 'Vaduz', 'name:th': 'วาดุซ' }), 'Vaduz');
  LBL.set('fr');
  assert.strictEqual(LBL.shown({ 'name:fr': 'Tour Eiffel', name: 'Tour Eiffel', 'name:th': 'x' }), 'Tour Eiffel');
  assert.deepStrictEqual(LBL.line(), TODAY_FR);
  LBL.set('en');
  // The popups and the tile-scan list show it; the search lookup does not.
  const popup = fs.readFileSync(`${PARTS_DIR}/400-wikidata-popup.js`, 'utf8');
  assert.match(popup, /var shown = szDisplayLabelOf\(props\);/);
  assert.match(popup, /_szPlacePopupDOM\(lat, lon, shown, enrich\)/);
  assert.match(popup, /__streetzimLookupPlace\(name, lat, lon\)/);
});

// The whole style, through makeStyle (130-137), as viewer_style_js does.
const STYLE_SRC = HTML.slice(HTML.indexOf('var _SZ_LAKES'), HTML.indexOf('// END rtl-text'));
function makeStyleFn() {
  const window = { matchMedia: () => ({ matches: false, addEventListener() {} }), devicePixelRatio: 1,
                   addEventListener() {} };
  Object.defineProperty(window, 'localStorage', { get() { return null; } });
  const document = { documentElement: { classList: { add() {}, remove() {}, contains() { return false; } } },
                     createElement: () => ({ getContext: () => null }) };
  return new Function('window', 'document', 'location', 'getComputedStyle', 'baseUrl', 'dbg', 'describeError',
    'Path2D', 'fetch', STYLE_SRC + '\nreturn makeStyle;')(window, document, { search: '' },
    () => ({ filter: 'none' }), 'http://zim/C/', () => {}, String, undefined, undefined);
}
const BASE_CFG = { minZoom: 0, maxZoom: 14, bounds: [9.47, 47.04, 9.64, 47.27] };
const labelLayers = (st) => st.layers.filter((l) => l.layout && l.layout['text-field'] &&
  JSON.stringify(l.layout['text-field']).includes('name:latin'));

await ok('the style is unchanged unless the map is in another language than its base', () => {
  const makeStyle = makeStyleFn();
  for (const lang of [undefined, 'en', 'de', 'th']) {
    const cfg = lang ? { ...BASE_CFG, language: lang } : BASE_CFG;
    const plain = JSON.stringify(makeStyle(cfg));
    assert.strictEqual(JSON.stringify(makeStyle({ ...cfg, baseLanguage: lang || 'en' })), plain, String(lang));
    assert.strictEqual(JSON.stringify(makeStyle({ ...cfg, wikiPacks: [{ lang: 'de', prefix: '~w/de/' }] })), plain);
    const want = lang && lang !== 'en'
      ? ['coalesce', ['get', 'name:' + lang], ['get', 'name'], ['get', 'name_int'], ['get', 'name:latin']]
      : TODAY_EN;
    const layers = labelLayers(makeStyle(cfg));
    assert.ok(layers.length >= 9, String(layers.length));
    for (const l of layers) assert.deepStrictEqual(l.layout['text-field'], want, l.id);
  }
});

await ok('a merged map in another language: point labels bilingual, road labels one line', () => {
  const makeStyle = makeStyleFn();
  const st = makeStyle({ ...BASE_CFG, language: 'th', baseLanguage: 'en' });
  const layers = labelLayers(st);
  const ids = layers.map((l) => l.id);
  for (const id of ['place-city', 'place-town', 'place-village', 'place-suburb', 'road-label', 'poi-label', 'water-label']) {
    assert.ok(ids.includes(id), id + ' in ' + ids);
  }
  for (const l of layers) {
    const tf = l.layout['text-field'];
    if (l.layout['symbol-placement'] === 'line') {
      assert.deepStrictEqual(tf, TODAY_EN, l.id);
      continue;
    }
    assert.strictEqual(tf[0], 'case', l.id);
    assert.deepStrictEqual(tf[2], ['format', TODAY_EN, {}, '\n', {}, ['get', 'name:th'], { 'font-scale': 0.8 }], l.id);
  }
  // Back to a one-language map: the globals are reset by the next style.
  const again = labelLayers(makeStyle({ ...BASE_CFG, language: 'en' }));
  for (const l of again) assert.deepStrictEqual(l.layout['text-field'], TODAY_EN);
});

console.log(`\n${pass} passed`);
