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
    ' _wikiAlsoIn, openWikiArticle, wikiGeoTitlesInBounds,' +
    ' _wikiYourLang, _wikiIsOther, _wikiLangLabel, _wikiLangTag, _wikiPanelPick, _wikiPanelFilter, _wikiYoursFirst,' +
    ' _wikiPanelRowEl, _wikiMarkerEl, _wikiMarkerOptions, _wikiMarkerOrder, _wikiReadLabel, _wikiSheetLabel,' +
    ' _wikiOnlyYoursSaved, _wikiOnlyYoursRow };');
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

await ok('Q-ID and description: [lat, lon, type, qid, desc] everywhere, an old-order pack read its way', () => {
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
  // A pack in the base's order (what wiki_pack writes), even when its
  // description looks like a Q-ID.
  assert.strictEqual(W._wikiEntryQid([1, 2, 'poi', 'Q1844', 'Q7'], true), 'Q1844');
  assert.strictEqual(W._wikiEntryDesc([1, 2, 'poi', 'Q1844', 'Q7'], true), 'Q7');
  assert.strictEqual(W._wikiEntryQid([1, 2, 'poi', 'Q1844', 'Hauptort'], true), 'Q1844');
  assert.strictEqual(W._wikiEntryDesc([1, 2, 'poi', 'Q1844', 'Hauptort'], true), 'Hauptort');
  // An old-order pack ([.., desc, qid]) only when its Q-ID slot has none.
  assert.strictEqual(W._wikiEntryQid([1, 2, 'poi', 'Hauptort', 'Q1844'], true), 'Q1844');
  assert.strictEqual(W._wikiEntryDesc([1, 2, 'poi', 'Hauptort', 'Q1844'], true), 'Hauptort');
  assert.strictEqual(W._wikiEntryQid([1, 2, 'poi', 'Hauptort', 'Q1844'], false), null);
  assert.strictEqual(W._wikiEntryDesc([1, 2, 'poi', 'Hauptort', null], true), '');
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
// Your language first (100, wiki-yours; 410): on a map with packs the
// map language's articles lead the Wiki panel, the others name their
// language and get a lighter marker. Without packs: exactly as before.
const DE_MAP = { language: 'de', baseLanguage: 'en', hasWikidata: true, hasWikiArticles: true,
  wikiPacks: [{ lang: 'de', prefix: '~w/de/', articles: 3 }] };

await ok('your language: the map\'s, when this file has its Wikipedia', () => {
  const cases = [
    [{ language: 'en' }, ''],                                                       // no packs
    [DE_MAP, 'de'],
    [{ ...DE_MAP, language: 'en' }, 'en'],                                          // the base's language
    [{ ...DE_MAP, language: 'fr' }, ''],                                            // no Wikipedia in it
    [{ ...DE_MAP, language: 'fr', baseLanguage: undefined }, 'fr'],                 // taken for the base's
    [{ ...DE_MAP, language: 'DE', baseLanguage: undefined }, 'de'],
    [{ language: 'zh-Hans', baseLanguage: 'en', wikiPacks: [{ lang: 'zh', prefix: '~w/zh/' }] }, 'zh-hans'],
    [{ ...DE_MAP, language: '' }, ''],
  ];
  for (const [cfg, want] of cases) {
    const W = loadWiki();
    W.wikiPacksInit(cfg);
    assert.strictEqual(W._wikiYourLang(), want, JSON.stringify(cfg));
  }
  const W = loadWiki();
  W.wikiPacksInit({ language: 'zh-Hans', baseLanguage: 'en', wikiPacks: [{ lang: 'zh', prefix: '~w/zh/' }] });
  assert.deepStrictEqual(['zh', 'en', ''].map(W._wikiIsOther), [false, true, true]);
  assert.deepStrictEqual(['zh', 'en', ''].map(W._wikiLangTag), ['', 'English', '']);
});

await ok('which articles are other, and the tag they carry', () => {
  const W = loadWiki();
  W.wikiPacksInit(DE_MAP);
  assert.deepStrictEqual(['de', 'en', 'th', '', undefined].map(W._wikiIsOther), [false, true, true, true, true]);
  assert.deepStrictEqual(['de', 'en', 'th', ''].map(W._wikiLangTag), ['', 'English', 'Thai', '']);
  // A map with no Wikipedia in its language: nothing is put behind, but an
  // article still says which language it is in.
  W.wikiPacksInit({ ...DE_MAP, language: 'fr' });
  assert.deepStrictEqual(['de', 'en'].map(W._wikiIsOther), [false, false]);
  assert.deepStrictEqual(['de', 'en'].map(W._wikiLangTag), ['German', 'English']);
  // No packs: never other, never a tag, whatever the language.
  W.wikiPacksInit({ language: 'de', hasWikiArticles: true });
  assert.deepStrictEqual(['de', 'en', 'th', ''].map(W._wikiIsOther), [false, false, false, false]);
  assert.deepStrictEqual(['de', 'en', 'th', ''].map(W._wikiLangTag), ['', '', '', '']);
});

await ok('language names in the UI language; the code without Intl.DisplayNames', () => {
  const W = loadWiki();
  W.wikiPacksInit(DE_MAP);
  const want = { en: 'English', de: 'Englisch', th: 'อังกฤษ', zh: '英语' };
  for (const [ui, name] of Object.entries(want)) {
    globalThis.SZ_UI_LANG = ui;
    try { assert.strictEqual(W._wikiLangTag('en'), name, ui); } finally { globalThis.SZ_UI_LANG = 'en'; }
  }
  const DN = Intl.DisplayNames;
  try {
    Intl.DisplayNames = undefined;
    assert.strictEqual(W._wikiLangTag('en'), 'EN');
    assert.strictEqual(W._wikiLangLabel('zh-hans'), 'ZH-HANS');
  } finally { Intl.DisplayNames = DN; }
  assert.strictEqual(W._wikiLangLabel('qqx'), 'QQX');
});

// The order the panel always had (410 before packs).
const OLD_ORDER = (a, b) => b.priority - a.priority || a.name.localeCompare(b.name);
function rowsFixture() {
  const r = (name, priority, lang) => ({ name, priority, lang, title: name });
  return [r('Zug', 40, 'en'), r('Aarau', 80, 'de'), r('Bern', 80, 'en'), r('Chur', 40, 'de'),
          r('Aarau', 80, 'en'), r('Thun', 55, undefined), r('Baden', 55, 'de'), r('Ilanz', 35, 'th')];
}

// What 410 did before packs: sort, count, keep the first `cap`.
function oldPick(rows, cap) {
  const items = rows.sort(OLD_ORDER);
  return { total: items.length, items: items.length > cap ? items.slice(0, cap) : items };
}

await ok('panel order: without packs exactly the old sort, count and cap', () => {
  for (const cfg of [{ language: 'de' }, { language: 'en', hasWikiArticles: true }, {}]) {
    const W = loadWiki();
    W.wikiPacksInit(cfg);
    for (const cap of [150, 5, 3]) {
      for (const only of [false, true]) {          // the switch, even saved on, changes nothing
        assert.deepStrictEqual(W._wikiPanelPick(rowsFixture(), only, cap), oldPick(rowsFixture(), cap),
                               JSON.stringify([cfg, cap, only]));
      }
    }
    const rows = rowsFixture();
    assert.strictEqual(W._wikiPanelPick(rows, false, 150).items, rows);   // in place, as before
    assert.strictEqual(W._wikiPanelFilter(rows, true), rows);
    assert.strictEqual(W._wikiYoursFirst(rows), rows);
    assert.strictEqual(W._wikiMarkerOrder(rows), rows);
  }
});

await ok('panel order: your language first, each group in the old order', () => {
  const W = loadWiki();
  W.wikiPacksInit(DE_MAP);
  const { total, items: rows } = W._wikiPanelPick(rowsFixture(), false, 150);
  assert.strictEqual(total, 8);
  assert.deepStrictEqual(rows.map((r) => r.name + ':' + r.lang),
    ['Aarau:de', 'Baden:de', 'Chur:de', 'Aarau:en', 'Bern:en', 'Thun:undefined', 'Zug:en', 'Ilanz:th']);
  const only = W._wikiPanelPick(rowsFixture(), true, 150);
  assert.deepStrictEqual([only.total, only.items.map((r) => r.name)], [3, ['Aarau', 'Baden', 'Chur']]);
  // Markers: the others added first, so yours are drawn on top.
  assert.deepStrictEqual(W._wikiMarkerOrder(rows).map((r) => r.lang),
    ['en', 'en', undefined, 'en', 'th', 'de', 'de', 'de']);
  // No Wikipedia in the map language: the old order, no filter.
  W.wikiPacksInit({ ...DE_MAP, language: 'fr' });
  assert.deepStrictEqual(W._wikiPanelPick(rowsFixture(), true, 150), oldPick(rowsFixture(), 150));
});

await ok('the cap is taken in the old order, then your language goes first within it', () => {
  // A dense, zoomed-out view: 40 English cities (80) and 160 German POIs
  // (40). The old top 150 are the 40 cities and 110 POIs; all of them stay,
  // the German ones listed (and drawn) first.
  const rows = () => {
    const out = [];
    for (let i = 0; i < 160; i++) out.push({ name: 'Poi ' + String(i).padStart(3, '0'), priority: 40, lang: 'de' });
    for (let i = 0; i < 40; i++) out.push({ name: 'City ' + String(i).padStart(2, '0'), priority: 80, lang: 'en' });
    return out;
  };
  const W = loadWiki();
  W.wikiPacksInit(DE_MAP);
  const { total, items } = W._wikiPanelPick(rows(), false, 150);
  const old = oldPick(rows(), 150).items;
  assert.strictEqual(total, 200);
  assert.strictEqual(items.length, 150);
  assert.deepStrictEqual(items.map((r) => r.name).sort(), old.map((r) => r.name).sort());
  assert.strictEqual(items.filter((r) => r.lang === 'en').length, 40);
  assert.deepStrictEqual(items.slice(0, 110).map((r) => r.name), old.filter((r) => r.lang === 'de').map((r) => r.name));
  assert.deepStrictEqual(items.slice(110).map((r) => r.name), old.filter((r) => r.lang === 'en').map((r) => r.name));
  // Only German: the cap counts German places only.
  const only = W._wikiPanelPick(rows(), true, 150);
  assert.deepStrictEqual([only.total, only.items.length, only.items[149].name], [160, 150, 'Poi 149']);
});

// A fake element plain enough to compare: its tag, class, own text,
// attributes, style, a few properties and its children.
function ser(e) {
  if (!e.tag) return { text: e.textContent };
  const o = { tag: e.tag, className: e.className, text: e.textContent, title: e.title, attrs: e.attrs,
              style: { ...e.style }, on: Object.keys(e.on || {}).sort() };
  for (const k of ['type', 'checked']) if (e[k] !== undefined) o[k] = e[k];
  o.children = e.children.map(ser);
  return o;
}

// The elements the panel built before packs (410 at 80f701d), for the same
// rows: what a map without packs must still show.
function oldRow(document, it) {
  const createEl = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text) el.appendChild(document.createTextNode(text));
    return el;
  };
  const desc = it.desc || '';
  const div = createEl('div', 'wiki-item' + (desc ? '' : ' wiki-item-nodesc'));
  div.appendChild(createEl('div', 'wiki-item-name', it.name));
  const meta = createEl('div', 'wiki-item-meta');
  meta.appendChild(createEl('span', 'wiki-item-type', szPlaceType(it.type || 'place')));
  div.appendChild(meta);
  if (desc) div.appendChild(createEl('div', 'wiki-item-desc', desc));
  return div;
}
function oldMarker(document, it) {
  const el = document.createElement('div');
  el.className = 'wiki-map-marker';
  el.textContent = '📖';
  el.title = it.name;
  el.style.cssText = 'font-size:18px;line-height:1;cursor:pointer;'
    + 'text-shadow:0 0 3px #fff,0 1px 2px rgba(255,255,255,0.95);';
  return el;
}

await ok('rows, markers and the article label without packs: the old elements', () => {
  const W = loadWiki();
  W.set(BASE);
  W.wikiPacksInit({ language: 'de', hasWikiArticles: true });
  const doc = fakeDocument();
  const items = W._wikiPanelItems(9.4, 9.7, 47.0, 47.3, (t) => (t === 'place' ? 80 : 40));
  // Rows that name another language's Wikipedia still look as before.
  items.push({ title: 'X', name: 'X', type: 'poi', desc: 'd', lang: 'en', priority: 1 });
  for (const it of items) {
    assert.deepStrictEqual(ser(W._wikiPanelRowEl(it)), ser(oldRow(doc, it)), it.name);
    const m = W._wikiMarkerEl(it);
    assert.deepStrictEqual(ser(m), ser(oldMarker(doc, it)), it.name);
    // The options 410 gave every Marker.
    assert.deepStrictEqual(W._wikiMarkerOptions(m, it), { element: m, anchor: 'center' }, it.name);
  }
  for (const l of [null, { lang: '', prefix: '', title: 'Vaduz' }, { lang: 'en', prefix: '', title: 'Vaduz' },
                   { lang: 'de', prefix: '~w/de/', title: 'Vaduz' }]) {
    assert.strictEqual(W._wikiSheetLabel(l), 'Wikipedia');
  }
  for (const l of [null, { lang: '', prefix: '', title: 'Vaduz' }, { lang: 'en', prefix: '', title: 'Vaduz' },
                   { lang: 'de', prefix: '~w/de/', title: 'Vaduz' }]) {
    assert.strictEqual(W._wikiReadLabel(l), '📖 Read full article');
  }
});

await ok('rows and markers with packs: a tag and a lighter marker for another language', () => {
  const W = merged(DE_MAP, { de: DE });
  const rows = W._wikiPanelPick(W._wikiPanelItems(9.4, 9.7, 47.0, 47.3, (t) => (t === 'place' ? 80 : 40)), false, 150).items;
  assert.deepStrictEqual(rows.map((r) => [r.name, r.lang]), [
    ['Eschen (Liechtenstein)', 'de'], ['Vaduz', 'de'], ['Schloss Vaduz', 'de'],
    ['Malbun', 'en'], ['Eschen', 'en'], ['Old Bridge', 'en']]);
  const de = W._wikiPanelRowEl(rows[1]);
  assert.strictEqual(de.attrs['data-wiki-lang'], 'de');
  assert.ok(!JSON.stringify(ser(de)).includes('wiki-item-lang'));
  const en = W._wikiPanelRowEl(rows[3]);
  assert.strictEqual(en.attrs['data-wiki-lang'], 'en');
  const tag = en.children[1].children[1];
  assert.deepStrictEqual([tag.className, tag.text(), tag.attrs['data-wiki-lang']], ['wiki-item-lang', 'English', 'en']);
  // The row is otherwise the old one: name, type, description.
  assert.deepStrictEqual(en.children.map((c) => c.className), ['wiki-item-name', 'wiki-item-meta']);
  const mDe = W._wikiMarkerEl(rows[1]), mEn = W._wikiMarkerEl(rows[3]);
  assert.strictEqual(mDe.className, 'wiki-map-marker');
  assert.match(mDe.style.cssText, /^font-size:18px;line-height:1;/);
  assert.strictEqual(mDe.title, 'Vaduz');
  assert.strictEqual(mEn.className, 'wiki-map-marker wiki-map-marker-other');
  assert.match(mEn.style.cssText, /^font-size:14px;filter:grayscale\(1\);line-height:1;/);
  // The fading is the Marker's own opacity: MapLibre overwrites an inline one.
  assert.doesNotMatch(mEn.style.cssText, /opacity/);
  assert.deepStrictEqual(W._wikiMarkerOptions(mEn, rows[3]), { element: mEn, anchor: 'center', opacity: '0.6' });
  assert.deepStrictEqual(W._wikiMarkerOptions(mDe, rows[1]), { element: mDe, anchor: 'center' });
  assert.strictEqual(mEn.title, 'Malbun · English');
  assert.deepStrictEqual([mDe.attrs['data-wiki-lang'], mEn.attrs['data-wiki-lang']], ['de', 'en']);
  assert.deepStrictEqual([mDe.attrs['aria-label'], mEn.attrs['aria-label']], ['Vaduz', 'Malbun · English']);
  // Without Intl.DisplayNames the title names the code.
  const DN = Intl.DisplayNames;
  try {
    Intl.DisplayNames = undefined;
    const m = W._wikiMarkerEl(rows[3]);
    assert.deepStrictEqual([m.title, m.attrs['aria-label']], ['Malbun · EN', 'Malbun · EN']);
  } finally { Intl.DisplayNames = DN; }
});

await ok('the article button names another language, in the UI language', () => {
  const W = merged(DE_MAP, { de: DE });
  const en = W._wikiArticleLinks('Q675040', null)[0];               // Malbun: English only
  assert.strictEqual(en.lang, 'en');
  assert.strictEqual(W._wikiReadLabel(en), '📖 Read full article in English');
  assert.strictEqual(W._wikiReadLabel(W._wikiArticleLinks('Q1844', null)[0]), '📖 Read full article');
  assert.strictEqual(W._wikiReadLabel(null), '📖 Read full article');
  const want = { de: '📖 Ganzen Artikel auf Englisch lesen', th: '📖 อ่านบทความฉบับเต็มภาษาอังกฤษ', zh: '📖 阅读英语全文' };
  for (const [lang, label] of Object.entries(want)) {
    globalThis.SZ_I18N = JSON.parse(fs.readFileSync(`${REPO}/resources/viewer/i18n/${lang}.json`, 'utf8'));
    globalThis.SZ_UI_LANG = lang;
    try { assert.strictEqual(W._wikiReadLabel(en), label, lang); }
    finally { globalThis.SZ_I18N = null; globalThis.SZ_UI_LANG = 'en'; }
  }
  // Popups, the panel's detail and the place sheet use it.
  const popup = fs.readFileSync(`${PARTS_DIR}/400-wikidata-popup.js`, 'utf8');
  assert.match(popup, /ab\.textContent = _wikiReadLabel\(link\);/);
  const panel = fs.readFileSync(`${PARTS_DIR}/410-wiki-sidebar.js`, 'utf8');
  assert.match(panel, /createEl\('button', 'wiki-article-btn', _wikiReadLabel\(links \? ap : null\)\)/);
});

await ok('"Only <language> articles": the saved choice, the label, the change', () => {
  const W = merged(DE_MAP, { de: DE });
  const store = { m: {}, getItem(k) { return k in this.m ? this.m[k] : null; }, setItem(k, v) { this.m[k] = v; } };
  assert.strictEqual(W._wikiOnlyYoursSaved(store), false);
  assert.strictEqual(W._wikiOnlyYoursSaved(null), false);
  assert.strictEqual(W._wikiOnlyYoursSaved({ getItem() { throw new Error('SecurityError'); } }), false);
  const seen = [];
  const row = W._wikiOnlyYoursRow(false, store, (on) => seen.push(on));
  assert.strictEqual(row.attrs['data-wiki-only'], 'de');
  assert.strictEqual(row.text(), 'Only German articles');
  const box = row.children[0];
  assert.deepStrictEqual([box.tag, box.type, box.checked], ['input', 'checkbox', false]);
  box.checked = true; box.on.change();
  assert.deepStrictEqual([seen, store.m['streetzim.wikiOnlyYours']], [[true], '1']);
  assert.strictEqual(W._wikiOnlyYoursSaved(store), true);
  box.checked = false; box.on.change();
  assert.deepStrictEqual([seen, store.m['streetzim.wikiOnlyYours']], [[true, false], '0']);
  assert.strictEqual(W._wikiOnlyYoursSaved(store), false);
  // A storage that throws on write still changes the list.
  const bad = { getItem: () => null, setItem() { throw new Error('QuotaExceededError'); } };
  const r2 = W._wikiOnlyYoursRow(true, bad, (on) => seen.push(on));
  assert.strictEqual(r2.children[0].checked, true);
  r2.children[0].checked = false; r2.children[0].on.change();
  assert.deepStrictEqual(seen, [true, false, false]);
  globalThis.SZ_I18N = JSON.parse(fs.readFileSync(`${REPO}/resources/viewer/i18n/de.json`, 'utf8'));
  globalThis.SZ_UI_LANG = 'de';
  try { assert.strictEqual(W._wikiOnlyYoursRow(false, null, () => {}).text(), 'Nur Artikel auf Deutsch'); }
  finally { globalThis.SZ_I18N = null; globalThis.SZ_UI_LANG = 'en'; }
});

// The Wiki panel itself (410, initWikiSidebar) on stubbed browser globals:
// a map, MapLibre markers, the panel's markup, fetch of the geo-indexes.
const SIDEBAR_SRC = slice('// --- Wiki sidebar: show Wikipedia entries', '// BEGIN sz-here');
function panelDocument() {
  function el(tag) {
    const e = {
      tag, children: [], style: {}, attrs: {}, dataset: {}, className: '', title: '', on: {}, _text: '',
      classList: { set: new Set(), add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); } },
      get textContent() { return e._text; },
      set textContent(v) { e._text = v; e.children = []; },
      appendChild(c) { e.children.push(c); return c; },
      insertBefore(c, ref) { const i = e.children.indexOf(ref); e.children.splice(i < 0 ? e.children.length : i, 0, c); return c; },
      setAttribute(k, v) { e.attrs[k] = String(v); },
      getAttribute(k) { return k in e.attrs ? e.attrs[k] : null; },
      addEventListener(t, f) { e.on[t] = f; },
      querySelector(t) { return e.children.find((c) => c.tag === t) || null; },
      text() { return [e._text].concat(e.children.map((c) => (c.text ? c.text() : c.textContent))).join(''); },
    };
    return e;
  }
  const byId = {};
  const mk = (id, tag = 'div') => (byId[id] = el(tag));
  const panel = mk('wiki-panel');
  const refresh = mk('wiki-refresh');
  refresh.appendChild(el('button'));
  for (const c of [mk('wiki-panel-header'), refresh, mk('wiki-panel-loading'), mk('wiki-panel-list')]) panel.appendChild(c);
  mk('wiki-toggle', 'button'); mk('wiki-panel-count', 'span'); mk('wiki-panel-close', 'button');
  return { createElement: el, createTextNode: (t) => ({ textContent: t }), getElementById: (id) => byId[id] || null, byId };
}
async function openPanel(config, files, { storage = null, bounds = [9.4, 9.7, 47.0, 47.3] } = {}) {
  const document = panelDocument();
  const map = {
    markers: [], handlers: {}, bounds,
    on(t, f) { this.handlers[t] = f; },
    getBounds() { const [w, e, s, n] = this.bounds; return { getWest: () => w, getEast: () => e, getSouth: () => s, getNorth: () => n }; },
    getZoom: () => 12, flyTo() {}, getCanvas: () => ({ clientHeight: 600 }),
  };
  class Marker {
    constructor(o) { this.el = o.element; this.options = o; }
    setLngLat(c) { this.lngLat = c; return this; }
    addTo(m) { m.markers.push(this); return this; }
    remove() { map.markers = map.markers.filter((x) => x !== this); }
  }
  const base = 'http://kiwix/content/osm_de_california/';
  const fetch = (url) => Promise.resolve(Object.prototype.hasOwnProperty.call(files, url.slice(base.length))
    ? { ok: true, json: () => Promise.resolve(files[url.slice(base.length)]) } : { ok: false });
  const init = new Function('window', 'document', 'fetch', 'maplibregl', 'setTimeout', 'clearTimeout', '_szStorage',
    '_szFlyToClear', 'history', 'location', 'baseUrl',
    'var WIKI_GEO_INDEX = null, WIKI_GEO_QID = null, WIKI_QID_TITLES = null;\n' + WIKI_SRC + SIDEBAR_SRC +
    '\nreturn initWikiSidebar;')({ innerWidth: 1000 }, document, fetch, { Marker }, () => 0, () => {},
    () => storage, () => {}, {}, { pathname: '/', search: '' }, base);
  const flush = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };
  init(map, config);
  await flush();
  document.byId['wiki-toggle'].on.click();
  await flush();
  const $ = document.byId;
  return { document, map, $, flush, list: $['wiki-panel-list'], count: $['wiki-panel-count'], panel: $['wiki-panel'],
           refresh: async () => { $['wiki-refresh'].children[0].on.click(); await flush(); } };
}

await ok('the Wiki panel without wikiPacks: the old rows, markers and count, element for element', async () => {
  const pri = (t) => (t === 'place' ? 80 : t === 'water' || t === 'peak' ? 55 : t === 'park' ? 50 : t === 'poi' ? 40 : 35);
  for (const cfg of [{ hasWikidata: true, language: 'de' }, { hasWikidata: true, hasWikiArticles: true }]) {
    for (const saved of [null, '1']) {
      const storage = { getItem: () => saved, setItem() {}, removeItem() {} };
      const P = await openPanel(cfg, { 'wiki-geo-index.json': BASE }, { storage });
      // What 410 showed before, from the base's index alone.
      const doc = panelDocument();
      const items = Object.entries(BASE).map(([title, g]) => ({ title, name: title.replace(/_/g, ' '),
        qid: g[3] || null, type: g[2] || '', coords: [g[1], g[0]], desc: g[4] || '', priority: pri(g[2] || '') }))
        .sort(OLD_ORDER);
      assert.deepStrictEqual(P.list.children.map(ser),
        items.map((it) => { const r = ser(oldRow(doc, it)); r.on = ['click']; return r; }), JSON.stringify(cfg));
      assert.deepStrictEqual(P.map.markers.map((m) => ser(m.el)),
        items.map((it) => { const r = ser(oldMarker(doc, it)); r.on = ['click']; return r; }));
      assert.deepStrictEqual(P.map.markers.map((m) => m.lngLat), items.map((it) => it.coords));
      // Each Marker made with the options it always had.
      assert.deepStrictEqual(P.map.markers.map((m) => Object.keys(m.options).sort().join()), items.map(() => 'anchor,element'));
      assert.ok(P.map.markers.every((m) => m.options.element === m.el && m.options.anchor === 'center'));
      assert.strictEqual(P.count.textContent, 4);
      // No switch: the panel is header, refresh bar, loading, list.
      assert.deepStrictEqual(P.panel.children.map((c) => c.className || c.tag), ['div', 'div', 'div', 'div']);
      assert.strictEqual(P.panel.children.length, 4);
    }
  }
});

await ok('the Wiki panel with packs: German first, English tagged, the switch filters', async () => {
  const store = { m: {}, getItem(k) { return k in this.m ? this.m[k] : null; }, setItem(k, v) { this.m[k] = v; }, removeItem() {} };
  const P = await openPanel(DE_MAP, { 'wiki-geo-index.json': BASE, '~w/de/wiki-geo-index.json': DE }, { storage: store });
  const rows = () => P.list.children.map((r) => [r.children[0].text(), r.attrs['data-wiki-lang'],
    r.children[1].children[1] ? r.children[1].children[1].text() : '']);
  assert.deepStrictEqual(rows(), [
    ['Eschen (Liechtenstein)', 'de', ''], ['Vaduz', 'de', ''], ['Schloss Vaduz', 'de', ''],
    ['Malbun', 'en', 'English'], ['Eschen', 'en', 'English'], ['Old Bridge', 'en', 'English']]);
  assert.strictEqual(P.count.textContent, 6);
  // Markers: the English ones first (under) and greyed.
  assert.deepStrictEqual(P.map.markers.map((m) => [m.el.title, m.el.className]), [
    ['Malbun · English', 'wiki-map-marker wiki-map-marker-other'],
    ['Eschen · English', 'wiki-map-marker wiki-map-marker-other'],
    ['Old Bridge · English', 'wiki-map-marker wiki-map-marker-other'],
    ['Eschen (Liechtenstein)', 'wiki-map-marker'], ['Vaduz', 'wiki-map-marker'], ['Schloss Vaduz', 'wiki-map-marker']]);
  assert.ok(P.map.markers.every((m) => m.el.on.click));
  // Faded by the Marker (MapLibre overwrites an inline opacity).
  assert.deepStrictEqual(P.map.markers.map((m) => m.options.opacity), ['0.6', '0.6', '0.6', undefined, undefined, undefined]);
  // The switch sits under the header; on, only the German rows and markers.
  const sw = P.panel.children[1];
  assert.deepStrictEqual([sw.tag, sw.className, sw.attrs['data-wiki-only']], ['label', 'wiki-only-yours', 'de']);
  assert.strictEqual(P.panel.children[2], P.$['wiki-refresh']);
  sw.children[0].checked = true; sw.children[0].on.change();
  await P.flush();
  assert.deepStrictEqual(rows().map((r) => r[1]), ['de', 'de', 'de']);
  assert.deepStrictEqual(P.map.markers.map((m) => m.el.title), ['Eschen (Liechtenstein)', 'Vaduz', 'Schloss Vaduz']);
  assert.strictEqual(P.count.textContent, 3);
  assert.strictEqual(store.m['streetzim.wikiOnlyYours'], '1');
  // Only English in view: say so rather than "nothing here".
  P.map.bounds = [9.60, 9.62, 47.09, 47.11];
  await P.refresh();
  const empty = P.list.children[0];
  assert.strictEqual(empty.attrs['data-wiki-empty'], 'none-yours');
  assert.strictEqual(empty.text(), '🌍No German articles in view; 1 in another language.');
  assert.deepStrictEqual(P.map.markers, []);
  // A new panel starts with the saved choice.
  const Q = await openPanel(DE_MAP, { 'wiki-geo-index.json': BASE, '~w/de/wiki-geo-index.json': DE }, { storage: store });
  assert.strictEqual(Q.panel.children[1].children[0].checked, true);
  assert.strictEqual(Q.list.children.length, 3);
  // Off again: all six.
  Q.panel.children[1].children[0].checked = false; Q.panel.children[1].children[0].on.change();
  await Q.flush();
  assert.strictEqual(Q.list.children.length, 6);
  // ... and the next panel starts with all of them.
  const R = await openPanel(DE_MAP, { 'wiki-geo-index.json': BASE, '~w/de/wiki-geo-index.json': DE }, { storage: store });
  assert.strictEqual(R.panel.children[1].children[0].checked, false);
  assert.strictEqual(R.list.children.length, 6);
});

await ok('the switch while an entry\'s detail is open: the detail stays, the list follows on Back', async () => {
  const P = await openPanel(DE_MAP, { 'wiki-geo-index.json': BASE, '~w/de/wiki-geo-index.json': DE });
  const malbun = P.list.children.find((r) => r.children[0].text() === 'Malbun');
  malbun.on.click();
  await P.flush();
  const detailName = () => {
    const wrap = P.list.children[0];
    const nm = wrap && wrap.children && wrap.children.find((c) => c.className === 'wiki-detail-name');
    return nm ? nm.text() : null;
  };
  assert.strictEqual(detailName(), 'Malbun');
  const btn = P.list.children[0].children.find((c) => c.className === 'wiki-article-btn');
  assert.strictEqual(btn.text(), '📖 Read full article in English');
  const sw = P.panel.children[1].children[0];
  sw.checked = true; sw.on.change();
  await P.flush();
  assert.strictEqual(detailName(), 'Malbun');            // not wiped by the switch
  // Back: the list, German only.
  P.list.children[0].children[0].on.click();
  await P.flush();
  assert.deepStrictEqual(P.list.children.map((r) => r.attrs['data-wiki-lang']), ['de', 'de', 'de']);
});

// The place sheet (210, _showPlaceDetail) on stubbed globals.
const SHEET_SRC = fs.readFileSync(`${PARTS_DIR}/210-place-detail-sheet.js`, 'utf8');
function sheetFor(config, idx, record) {
  const document = panelDocument();
  const body = document.createElement('body');
  document.body = body;
  const fn = new Function('window', 'document', 'history', 'location', 'baseUrl', '_findResultsState',
    '_populateNearby', 'requestAnimationFrame', 'setTimeout',
    'var WIKI_GEO_INDEX = null, WIKI_GEO_QID = null, WIKI_QID_TITLES = null;\n' + WIKI_SRC + SHEET_SRC +
    '\nreturn { show: _showPlaceDetail, set: function(i) { WIKI_GEO_INDEX = i; WIKI_GEO_QID = {};' +
    ' for (var t in i) { var q = i[t] && i[t][3]; if (q) WIKI_GEO_QID[q] = t; } },' +
    ' packs: function() { return WIKI_PACKS; }, wikiPacksInit: wikiPacksInit, wikiPackSetIndex: wikiPackSetIndex };');
  const api = fn({}, document, {}, { pathname: '/', search: '' }, 'http://kiwix/c/', { origin: null },
    () => {}, () => {}, () => 0);
  api.set(BASE);
  api.wikiPacksInit(config);
  for (const pk of api.packs()) if (idx[pk.lang]) api.wikiPackSetIndex(pk, idx[pk.lang]);
  api.show({}, record, 0);
  const panel = body.children.find((c) => c.attrs.role === 'dialog');
  const all = (e) => [e].concat((e.children || []).flatMap(all));
  return {
    title: all(panel).find((e) => e.tag === 'h2').text(),
    buttons: all(panel).filter((e) => e.tag === 'button').map((b) => b.text()).filter((t) => t !== '×'),
    also: (all(panel).find((e) => e.className === 'wiki-also') || null),
  };
}

await ok('the place sheet: "Wikipedia (English)" for another language\'s article, "Also in" for the rest', () => {
  const malbun = { n: 'Malbun', a: 47.10, o: 9.61, q: 'Q675040', t: 'place' };
  const vaduz = { n: 'Vaduz', a: 47.141, o: 9.521, q: 'Q1844', t: 'place' };
  let S = sheetFor(DE_MAP, { de: DE }, malbun);
  assert.deepStrictEqual(S.buttons, ['🧭Directions', '📖Wikipedia (English)']);
  assert.strictEqual(S.also, null);
  S = sheetFor(DE_MAP, { de: DE }, vaduz);
  assert.deepStrictEqual(S.buttons, ['🧭Directions', '📖Wikipedia']);
  assert.strictEqual(S.also.attrs['data-wiki-also'], 'en');
  // Two packs: the same.
  const TWO = { ...DE_MAP, wikiPacks: [...DE_MAP.wikiPacks, { lang: 'th', prefix: '~w/th/' }] };
  assert.deepStrictEqual(sheetFor(TWO, { de: DE, th: TH }, malbun).buttons, ['🧭Directions', '📖Wikipedia (English)']);
  // German UI.
  globalThis.SZ_I18N = JSON.parse(fs.readFileSync(`${REPO}/resources/viewer/i18n/de.json`, 'utf8'));
  globalThis.SZ_UI_LANG = 'de';
  try { assert.deepStrictEqual(sheetFor(DE_MAP, { de: DE }, malbun).buttons, ['🧭Route', '📖Wikipedia (Englisch)']); }
  finally { globalThis.SZ_I18N = null; globalThis.SZ_UI_LANG = 'en'; }
  // No packs, whatever the map language: "Wikipedia", as before.
  for (const cfg of [{ language: 'de', hasWikiArticles: true }, { hasWikiArticles: true }]) {
    for (const r of [malbun, vaduz]) {
      assert.deepStrictEqual(sheetFor(cfg, {}, r).buttons, ['🧭Directions', '📖Wikipedia'], JSON.stringify(cfg));
      assert.strictEqual(sheetFor(cfg, {}, r).also, null);
    }
  }
});

// Names with several values ("Hang Ah Alley;Pagoda Alley"): every view
// shows the first (runtime szFirstName); lookups keep the record's value.
const fnSrc = (start) => {
  const a = HTML.indexOf(start);
  assert.ok(a >= 0, start);
  return HTML.slice(a, HTML.indexOf('\nfunction ', a + 10)) + '\n';
};
const MULTI = { n: 'Hang Ah Alley;Pagoda Alley', a: 37.7944, o: -122.4069, t: 'poi', s: 'street', q: 'Q1', l: 'San Francisco' };

await ok('names with several values: the first in the sheet, Find cards, nearby rows, popups', () => {
  assert.strictEqual(szFirstName('Hang Ah Alley;Pagoda Alley'), 'Hang Ah Alley');
  assert.strictEqual(szFirstName('Oakland Amtrak;Oakland, CA - Jack London Square (OKJ)'), 'Oakland Amtrak');
  assert.strictEqual(szFirstName(';x'), ';x');
  assert.strictEqual(szFirstName('Zug'), 'Zug');
  assert.strictEqual(szFirstName(undefined), undefined);
  assert.strictEqual(szFirstName(''), '');
  // The place sheet.
  assert.strictEqual(sheetFor({ hasWikiArticles: true }, {}, MULTI).title, 'Hang Ah Alley');
  // Find cards, the sheet's nearby rows, a Find marker's popup (200, 240).
  const document = panelDocument();
  let popupDom = null, wikiName = null;
  class Popup { constructor() {} setDOMContent(d) { popupDom = d; return this; } }
  const F = new Function('document', 'window', 'maplibregl', '_formatDistanceStrip', '_haversineMetersStrip',
    '_setActiveResult', '_showPlaceDetail', '_wikiArticleLinks', '_findResultsState',
    fnSrc('function _nearbyRow(') + fnSrc('function _findResultCard(') + fnSrc('function _findResultPopup(') +
    fnSrc('function _szPlacePopupDOM(') +
    '\nreturn { _nearbyRow, _findResultCard, _findResultPopup, _szPlacePopupDOM };')(
    document, {}, { Popup }, () => '1 km', () => 1000, () => {}, () => {}, () => [], { origin: null });
  const texts = (e) => [e.text ? e.text() : e.textContent].concat((e.children || []).flatMap(texts));
  const card = F._findResultCard({}, MULTI, 0);
  assert.ok(texts(card).includes('Hang Ah Alley'), JSON.stringify(texts(card)));
  assert.ok(!texts(card).some((t) => String(t).includes('Pagoda')), JSON.stringify(texts(card)));
  const row = F._nearbyRow({}, { item: MULTI, dist: 120, idx: 0 }, false);
  assert.ok(texts(row).includes('Hang Ah Alley') && !texts(row).some((t) => String(t).includes('Pagoda')), JSON.stringify(texts(row)));
  F._findResultPopup({}, MULTI);
  assert.strictEqual(popupDom.textContent, 'Hang Ah Alley');
  F._findResultPopup({ _buildWikiPopupDOM: (n) => { wikiName = n; return {}; } }, MULTI);
  assert.strictEqual(wikiName, 'Hang Ah Alley');
  const pin = F._szPlacePopupDOM(37.79, -122.40, MULTI.n, {});
  assert.strictEqual(pin.children.find((c) => c.className === 'pin-name').text(), 'Hang Ah Alley');
  // Unnamed stays unnamed.
  F._findResultPopup({}, { a: 1, o: 2 });
  assert.strictEqual(popupDom.textContent, '(unnamed)');
});

await ok('names with several values: a search row shows the first; data-name and the record keep it all', () => {
  // renderResults and escapeHtml as initSearch has them (300-search.js).
  const src = (from, to) => { const a = HTML.indexOf(from); return HTML.slice(a, HTML.indexOf(to, a)) + '\n'; };
  const R = new Function('szPlaceType', 'szT',
    'var activeIdx = -1, lastSearchMatches = [], resultsEl = { style: {}, innerHTML: "" };\n' +
    src('  function renderResults(matches) {', '\n  function doSearch(') +
    src('  function escapeHtml(s) {', '\n  function selectResult(') +
    '\nreturn { renderResults, el: resultsEl, last: function() { return lastSearchMatches; } };')(szPlaceType, szT);
  const rec = { ...MULTI, nn: '杭亞巷;唐人街' };
  R.renderResults([{ item: rec, score: 1, dist: '0.1 mi' }, { item: { n: 'Zug', a: 1, o: 2, t: 'place' }, score: 1 }]);
  const html = R.el.innerHTML;
  assert.match(html, /data-name="Hang Ah Alley;Pagoda Alley"/);                       // what selectResult looks up
  assert.match(html, /<span class="search-result-name">Hang Ah Alley <span class="search-result-native">杭亞巷<\/span>/);
  assert.doesNotMatch(html, /search-result-name">[^<]*Pagoda/);
  assert.match(html, /data-name="Zug"><span class="search-result-name">Zug</);
  // The record itself is untouched (selectResult reads it by index).
  assert.strictEqual(rec.n, 'Hang Ah Alley;Pagoda Alley');
  assert.strictEqual(R.last()[0].item.n, 'Hang Ah Alley;Pagoda Alley');
});

await ok('names with several values: the Find page\'s #dest=…&label= hand-off fills the route with the first', () => {
  // applyHash as the map's load handler has it (120-map-init-and-style.js).
  const a = HTML.indexOf('    function validLatLon(lat, lon) {');
  const b = HTML.indexOf("    map.on('load', applyHash);", a);
  assert.ok(a > 0 && b > a);
  const run = (hash) => {
    const calls = [];
    const win = { location: { hash }, streetzimRouting: { open() {}, setDest: (...x) => calls.push(['dest', ...x]),
                                                           setOrigin: (...x) => calls.push(['origin', ...x]) } };
    const pins = [];
    new Function('window', 'map', 'placeSearchPin', 'SZ_HERE', 'renderFindResultsFromStash',
      HTML.slice(a, b) + '\napplyHash();')(win, { flyTo() {}, fitBounds() {} }, (m, la, lo, name) => pins.push(name),
      'HERE', () => {});
    return { calls, pins };
  };
  let r = run('#dest=37.79,-122.27&label=' + encodeURIComponent('Oakland Amtrak;Oakland, CA - Jack London Square (OKJ)'));
  assert.deepStrictEqual(r.calls, [['dest', 37.79, -122.27, 'Oakland Amtrak']]);
  r = run('#dest=37.79,-122.27&label=Zug');
  assert.deepStrictEqual(r.calls, [['dest', 37.79, -122.27, 'Zug']]);
  r = run('#dest=37.79,-122.27');
  assert.deepStrictEqual(r.calls, [['dest', 37.79, -122.27, '']]);
  // A pin's popup cuts the name itself (_szPlacePopupDOM).
  r = run('#pin=37.79,-122.27&label=' + encodeURIComponent('A;B'));
  assert.deepStrictEqual(r.pins, ['A;B']);
});

await ok('names with several values: route steps and the navigation HUD show the first street name', () => {
  const steps = fs.readFileSync(`${PARTS_DIR}/540-routing-worker-bridge.js`, 'utf8');
  assert.match(steps, /var name = szFirstName\(graph\.getName \? graph\.getName\(r\.nameIdx\) : ''\);/);
  const hud = fs.readFileSync(`${PARTS_DIR}/600-driving-mode.js`, 'utf8');
  assert.match(hud, /name = szFirstName\(graph\.getName\(nextTurn\.nameIdx\) \|\| ''\);/);
  assert.strictEqual((hud.match(/var nm = szFirstName\(graph\.getName\(tt\.nameIdx\)\);/g) || []).length, 2);
  // Every graph name shown goes through it.
  for (const part of fs.readdirSync(PARTS_DIR).filter((n) => /^[1-9].*\.js$/.test(n) && !n.startsWith('510'))) {
    const t = fs.readFileSync(`${PARTS_DIR}/${part}`, 'utf8');
    const all = (t.match(/graph\.getName\(/g) || []).length;
    const cut = (t.match(/szFirstName\(graph\.getName/g) || []).length;
    assert.strictEqual(cut, all, part);
  }
});

await ok('names with several values: search rows and the Find page show the first, lookups keep it all', () => {
  const search = fs.readFileSync(`${PARTS_DIR}/300-search.js`, 'utf8');
  assert.match(search, /'" data-name="' \+ escapeHtml\(m\.n\) \+ '">'/);                 // the lookup's value
  assert.match(search, /'<span class="search-result-name">' \+ escapeHtml\(szFirstName\(m\.n\)\)/);
  assert.match(search, /escapeHtml\(szFirstName\(m\.nn\)\)/);
  assert.match(search, /function _szPlacePopupDOM\(lat, lon, name, enrich\) \{\s*enrich = enrich \|\| \{\};\s*name = szFirstName\(name\);/);
  const pickers = fs.readFileSync(`${PARTS_DIR}/550-routing-pickers.js`, 'utf8');
  assert.match(pickers, /var label = szFirstName\(m\.item\.n\);/);
  assert.match(pickers, /nameSpan\.textContent = szFirstName\(m\.item\.n\);/);
  const sheet = fs.readFileSync(`${PARTS_DIR}/210-place-detail-sheet.js`, 'utf8');
  assert.match(sheet, /setDest\(r\.a, r\.o, szFirstName\(r\.n\) \|\| ''\)/);
  const places = fs.readFileSync(`${REPO}/resources/viewer/places.html`, 'utf8');
  assert.match(places, /h\.textContent = szFirstName\(r\.n\) \|\| szT\('common\.unnamed'/);
  assert.match(places, /nameEl\.textContent = szFirstName\(c\.n\) \|\| szT\('common\.unnamed'/);
  assert.match(places, /state\.originLabel = szFirstName\(r\.n\);\s*showNearPlace\(szFirstName\(r\.n\)\);/);
  // The Map link carries the record's name to the viewer, which cuts it there.
  assert.match(places, /'&label=' \+ encodeURIComponent\(r\.n \|\| ''\)/);
  // Both pages carry the helper (it is in the shared runtime).
  for (const page of ['index.html', 'places.html']) {
    assert.match(fs.readFileSync(`${REPO}/resources/viewer/${page}`, 'utf8'), /function szFirstName\(s\)/, page);
  }
});

await ok('the Wiki panel wires in the order, rows and markers', () => {
  const panel = fs.readFileSync(`${PARTS_DIR}/410-wiki-sidebar.js`, 'utf8');
  assert.match(panel, /var pick = _wikiPanelPick\(all, onlyYours, 150\);\s*var items = pick\.items;\s*countEl\.textContent = pick\.total;/);
  assert.match(panel, /_wikiMarkerOrder\(items\)\.forEach\(function\(it\) \{\s*var el = _wikiMarkerEl\(it\);/);
  assert.match(panel, /new maplibregl\.Marker\(_wikiMarkerOptions\(el, it\)\)/);
  assert.match(panel, /var div = _wikiPanelRowEl\(it\);/);
});

// ---------------------------------------------------------------------
// Bilingual labels (130-lowzoom-lakes.js).
const LBL = new Function(slice('var SZ_LANG = ', 'function makeStyle') +
  '\nreturn { set: (a, b, r) => { SZ_LANG = a; SZ_LANG2 = b || ""; SZ_REGION = !!r; }, field: szLabelField,' +
  ' line: szLineLabelField, langs: szLabelLanguages, region: szRegionLanguages, of: szLabelOf, shown: szDisplayLabelOf };')();

// Enough of MapLibre's expression language to evaluate a label: get, has,
// coalesce, case, all, ==, !=, >, let, var, to-string, index-of, slice and
// format (a formatted value is returned as [[text, options], …]). The same
// expressions gave the same texts in MapLibre 5.23 itself (Chromium,
// layer.layout.get('text-field').evaluate) for the ";" cases below.
function evalExpr(e, props, vars = {}) {
  if (!Array.isArray(e)) return e;
  const [op, ...a] = e;
  const v = (x) => evalExpr(x, props, vars);
  switch (op) {
    case 'let': return evalExpr(a[2], props, { ...vars, [a[0]]: v(a[1]) });
    case 'var': assert.ok(a[0] in vars, 'unbound ' + a[0]); return vars[a[0]];
    case 'to-string': { const r = v(a[0]); return r === null || r === undefined ? '' : String(r); }
    case 'index-of': return v(a[1]).indexOf(v(a[0]));
    case 'slice': return v(a[0]).slice(v(a[1]), a.length > 2 ? v(a[2]) : undefined);
    case '>': return v(a[0]) > v(a[1]);
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
// The local label rules (as before: the name each picks), and the part
// before a ";" of a name (OSM's "洛杉矶;洛杉磯"): every label is FIRST(...).
const RAW_EN = ['coalesce', ['get', 'name:latin'], ['get', 'name_int'], ['get', 'name']];
const RAW_FR = ['coalesce', ['get', 'name:fr'], ['get', 'name'], ['get', 'name_int'], ['get', 'name:latin']];
const FIRST = (e) => ['let', 'sz_name', ['to-string', e],
  ['case', ['>', ['index-of', ';', ['var', 'sz_name']], 0],
    ['slice', ['var', 'sz_name'], 0, ['index-of', ';', ['var', 'sz_name']]], ['var', 'sz_name']]];
const TODAY_EN = FIRST(RAW_EN);
const TODAY_FR = FIRST(RAW_FR);
// The labels before names were cut at ";": what a name without one must
// still give ("" for no name: MapLibre's default for a null text-field).
const OLD = {
  one: (local) => local,
  bilingual: (local, key) => ['case',
    ['all', ['has', key], ['!=', local.concat(['']), ''], ['!=', ['get', key], local.concat([''])]],
    ['format', local, {}, '\n', {}, ['get', key], { 'font-scale': 0.8 }], local.concat([['get', key]])],
};
const shownText = (r) => (r === null ? '' : text(r));

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
  assert.strictEqual(evalExpr(f, {}), '');
  // The local name is the base's own rule: name:latin first for English.
  assert.strictEqual(text(evalExpr(f, { 'name:latin': 'Tokyo', name: '東京', 'name:th': 'โตเกียว' })), 'Tokyo\nโตเกียว');
  // A Greek base shown in English: name:el first, then name:en.
  LBL.set('el', 'en');
  assert.strictEqual(text(evalExpr(LBL.field(), { 'name:el': 'Αθήνα', name: 'Αθήνα', 'name:en': 'Athens' })), 'Αθήνα\nAthens');
  assert.strictEqual(evalExpr(LBL.field(), { name: 'Πλάκα' }), 'Πλάκα');
  LBL.set('en');
});

await ok('labels: a name with several values (";") shows the first; the rest is unchanged', () => {
  LBL.set('en', 'zh');
  const f = LBL.field();
  assert.deepStrictEqual(evalExpr(f, { 'name:latin': 'Los Angeles', 'name:zh': '洛杉矶;洛杉磯' }),
    [['Los Angeles', {}], ['\n', {}], ['洛杉矶', { 'font-scale': 0.8 }]]);
  assert.strictEqual(evalExpr(f, { 'name:latin': 'Biel;Bienne', 'name:zh': 'Biel' }), 'Biel');   // equal once cut
  assert.strictEqual(evalExpr(f, { 'name:zh': '洛杉矶;洛杉磯' }), '洛杉矶');
  assert.strictEqual(evalExpr(f, { 'name:latin': ';x', 'name:zh': ';x' }), ';x');               // a leading ";": whole
  assert.strictEqual(evalExpr(f, { 'name:latin': 'Zug', 'name:zh': 'Zug;楚格' }), 'Zug');          // equal once cut
  LBL.set('en');
  assert.strictEqual(evalExpr(LBL.field(), { 'name:latin': 'Biel;Bienne' }), 'Biel');
  assert.strictEqual(evalExpr(LBL.field(), { name_int: 'A;B;C' }), 'A');
  assert.strictEqual(evalExpr(LBL.line(), { 'name:latin': 'Rue A;Rue B' }), 'Rue A');
  LBL.set('fr');
  assert.strictEqual(evalExpr(LBL.field(), { 'name:fr': 'Bienne;Biel', 'name:latin': 'Biel' }), 'Bienne');
  LBL.set('en', 'th');
  assert.strictEqual(LBL.shown({ 'name:latin': 'Los Angeles', 'name:th': 'ลอสแอนเจลิส;แอลเอ' }), 'Los Angeles · ลอสแอนเจลิส');
  LBL.set('en');
  assert.strictEqual(LBL.shown({ 'name:latin': 'Biel;Bienne' }), 'Biel');
  assert.strictEqual(LBL.shown({ 'name:latin': ';x' }), ';x');
  assert.strictEqual(LBL.of({ 'name:latin': 'Biel;Bienne' }), 'Biel;Bienne');       // search records: as named
  // Names without ";": exactly what the labels gave before, for every
  // combination of present, missing, equal and differing names.
  const vals = [undefined, 'A', 'B', 'Ä b'];
  const keys = ['name:latin', 'name_int', 'name', 'name:fr', 'name:th'];
  const props = [];
  (function walk(i, p) {
    if (i === keys.length) { props.push(p); return; }
    for (const x of vals) walk(i + 1, x === undefined ? p : { ...p, [keys[i]]: x });
  })(0, {});
  for (const [loc, second, raw] of [['en', '', RAW_EN], ['fr', '', RAW_FR], ['en', 'th', RAW_EN], ['fr', 'th', RAW_FR]]) {
    LBL.set(loc, second);
    const want = second ? OLD.bilingual(raw, 'name:' + second) : OLD.one(raw);
    for (const p of props) {
      assert.deepStrictEqual(shownText(evalExpr(LBL.field(), p)), shownText(evalExpr(want, p)), `${loc} ${second} ${JSON.stringify(p)}`);
      assert.deepStrictEqual(evalExpr(LBL.line(), p), shownText(evalExpr(raw, p)));
    }
  }
  assert.ok(props.length === 1024);
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
      ? FIRST(['coalesce', ['get', 'name:' + lang], ['get', 'name'], ['get', 'name_int'], ['get', 'name:latin']])
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
    assert.deepStrictEqual(tf[2], ['format', TODAY_EN, {}, '\n', {}, FIRST(['get', 'name:th']), { 'font-scale': 0.8 }], l.id);
  }
  // Back to a one-language map: the globals are reset by the next style.
  const again = labelLayers(makeStyle({ ...BASE_CFG, language: 'en' }));
  for (const l of again) assert.deepStrictEqual(l.layout['text-field'], TODAY_EN);
});

// Region languages (map-config regionLanguages): the native name, in its own
// script, is the bilingual label's local line, under the map language's.
// Tiles as StreetZim's tilemaker writes them for an English base: name:latin
// (English where OSM has it), name_int (the OSM name when it differs), the
// map language's name:xx; and OpenFreeMap's, which carry name.
const ATHENS = { 'name:latin': 'Athens', name_int: 'Αθήνα', 'name:de': 'Athen', 'name:el': 'Αθήνα' };
const ATHENS_OFM = { name: 'Αθήνα', 'name:latin': 'Athina', 'name:de': 'Athen', 'name:el': 'Αθήνα' };
const PLAKA = { 'name:latin': 'Πλάκα' };                   // no English name: name:latin is the OSM name
const ZURICH = { 'name:latin': 'Zurich', name_int: 'Zürich', 'name:de': 'Zürich' };

await ok('region languages: a German map shows "Athen" over "Αθήνα"; a Greek map just "Αθήνα"', () => {
  LBL.set('en', 'de', true);
  for (const athens of [ATHENS, ATHENS_OFM]) {
    const r = evalExpr(LBL.field(), athens);
    assert.deepStrictEqual(r, [['Athen', {}], ['\n', {}], ['Αθήνα', { 'font-scale': 0.8 }]]);
    assert.strictEqual(LBL.shown(athens), 'Athen · Αθήνα');
  }
  // No German name: the native one alone; the same name: one line.
  assert.strictEqual(evalExpr(LBL.field(), PLAKA), 'Πλάκα');
  assert.strictEqual(LBL.shown(PLAKA), 'Πλάκα');
  assert.strictEqual(evalExpr(LBL.field(), ZURICH), 'Zürich');
  assert.strictEqual(LBL.shown(ZURICH), 'Zürich');
  assert.strictEqual(evalExpr(LBL.field(), {}), '');
  // Several values: the first of each.
  assert.deepStrictEqual(text(evalExpr(LBL.field(), { 'name:latin': 'Athens', name_int: 'Αθήνα;Athína', 'name:de': 'Athen;Athena' })),
    'Athen\nΑθήνα');
  assert.strictEqual(LBL.shown({ 'name:latin': 'Athens', name_int: 'Αθήνα;Athína', 'name:de': 'Athen;Athena' }), 'Athen · Αθήνα');
  // A Greek map of the same English base: name:el is the native name.
  LBL.set('en', 'el', true);
  assert.strictEqual(evalExpr(LBL.field(), ATHENS), 'Αθήνα');
  assert.strictEqual(evalExpr(LBL.field(), PLAKA), 'Πλάκα');
  assert.strictEqual(LBL.shown(ATHENS), 'Αθήνα');
  // Without regionLanguages the same Greek map is as before: the base's
  // local name first (name:latin), the Greek under it.
  LBL.set('en', 'el', false);
  assert.strictEqual(text(evalExpr(LBL.field(), ATHENS)), 'Athens\nΑθήνα');
  assert.strictEqual(LBL.shown(ATHENS), 'Athens · Αθήνα');
  LBL.set('en', 'de', false);
  assert.strictEqual(text(evalExpr(LBL.field(), ATHENS)), 'Athens\nAthen');
  // A map in its base's language: one line, regionLanguages or not; road
  // labels stay the local label; search lookups keep the local name.
  for (const r of [false, true]) {
    LBL.set('en', '', r);
    assert.deepStrictEqual(LBL.field(), TODAY_EN);
    assert.strictEqual(LBL.shown(ATHENS), 'Athens');
    LBL.set('el', '', r);
    assert.strictEqual(evalExpr(LBL.field(), ATHENS_OFM), 'Αθήνα');
    LBL.set('en', 'de', r);
    assert.deepStrictEqual(LBL.line(), TODAY_EN);
    assert.strictEqual(LBL.of(ATHENS), 'Athens');
  }
  LBL.set('en');
});

await ok('regionLanguages: a list of language codes; anything else counts as none', () => {
  assert.deepStrictEqual(LBL.region({ regionLanguages: ['el'] }), ['el']);
  assert.deepStrictEqual(LBL.region({ regionLanguages: ['de', 'fr', 'it', 'rm'] }), ['de', 'fr', 'it', 'rm']);
  assert.deepStrictEqual(LBL.region({ regionLanguages: ['zh-Hant', 7, '', 'x y', null] }), ['zh-Hant']);
  for (const c of [{}, null, { regionLanguages: 'el' }, { regionLanguages: [] }, { regionLanguages: {} }]) {
    assert.deepStrictEqual(LBL.region(c), [], JSON.stringify(c));
  }
});

await ok('the style: regionLanguages changes only a map in another language than its base', () => {
  const makeStyle = makeStyleFn();
  // Absent, or a map in its base's language: byte for byte as without.
  for (const lang of [undefined, 'en', 'de', 'el']) {
    const cfg = lang ? { ...BASE_CFG, language: lang } : BASE_CFG;
    const plain = JSON.stringify(makeStyle(cfg));
    for (const region of [['el'], ['de', 'fr'], [], 'el']) {
      assert.strictEqual(JSON.stringify(makeStyle({ ...cfg, regionLanguages: region })), plain, `${lang} ${region}`);
    }
  }
  const merged = { ...BASE_CFG, language: 'de', baseLanguage: 'en' };
  const plainMerged = JSON.stringify(makeStyle(merged));
  assert.strictEqual(JSON.stringify(makeStyle({ ...merged, regionLanguages: [] })), plainMerged);
  // Present on a German map of an English base: German first, native under.
  const st = makeStyle({ ...merged, regionLanguages: ['el'] });
  const NATIVE = ['coalesce', ['get', 'name'], ['get', 'name_int'], ['get', 'name:latin']];
  for (const l of labelLayers(st)) {
    const tf = l.layout['text-field'];
    if (l.layout['symbol-placement'] === 'line') { assert.deepStrictEqual(tf, TODAY_EN, l.id); continue; }
    assert.deepStrictEqual(tf[2], ['format', FIRST(['get', 'name:de']), {}, '\n', {}, FIRST(NATIVE), { 'font-scale': 0.8 }], l.id);
    assert.deepStrictEqual(tf[3], FIRST(['coalesce', ['get', 'name:de'], ['get', 'name'], ['get', 'name_int'], ['get', 'name:latin']]), l.id);
  }
  // The next style without it is the plain merged one again (globals reset).
  assert.strictEqual(JSON.stringify(makeStyle(merged)), plainMerged);
});

console.log(`\n${pass} passed`);
