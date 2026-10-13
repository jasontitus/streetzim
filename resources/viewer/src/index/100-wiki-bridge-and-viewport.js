<script>
// Compute base URL for absolute resource paths. Strip `#` fragment and
// `?` query first — deep-links like `index.html#map=16/37.4/-122.1`
// contain `/` inside the fragment, which used to leak into baseUrl and
// produce `.../index.html#map=16/37.4/map-config.json`, corrupting every
// subsequent fetch. Same for `?q=…` used by the Find mini-app.
var baseUrl = (function() {
  var href = window.location.href;
  var hashAt = href.indexOf('#');
  if (hashAt !== -1) href = href.substring(0, hashAt);
  var queryAt = href.indexOf('?');
  if (queryAt !== -1) href = href.substring(0, queryAt);
  return href.substring(0, href.lastIndexOf('/') + 1);
})();

// Optional Q-ID -> Wikipedia-title bridge for full-article links. Hosted at
// /drive/wiki-qid-titles.json (served from the web, NOT the ZIM, so it sits
// one level above the viewer base). When present, wiki popups gain a
// "Read full article" button that opens the in-ZIM wiki-article/<Title> page
// (served by the drive service worker). Absent (e.g. plain Kiwix) -> popups
// just show the wikidata blurb as before. This is a transitional side-load,
// requested only inside the PWA (SZ_ON_DRIVE_PWA); ZIMs with bundled
// articles carry wiki-geo-index.json, which supersedes it.
var WIKI_QID_TITLES = null;   // optional side-load bridge (transitional)
var WIKI_GEO_INDEX = null;    // {title:[lat,lon,type,qid]} from the ZIM's wiki-geo-index.json
var WIKI_GEO_QID = null;      // reverse {qid:title} built from WIKI_GEO_INDEX
// BEGIN wiki-geo-bounds
var _WIKI_GEO_BUCKETS = { buckets: null, src: null };  // the base index's grid

function wikiGeoBucketKey(lat, lon) {
  return (Math.floor(lat) + 90) * 1000 + (Math.floor(lon) + 180);
}

// Titles whose point lies inside [W,E]×[S,N]. Uses a lazily built 1°
// bucket grid so a pan/zoom only touches the cells under the viewport
// instead of rescanning every placed article in the ZIM (hundreds of
// thousands on country-scale builds) on every debounced moveend.
function wikiGeoTitlesInBounds(W, E, S, N) {
  if (!WIKI_GEO_INDEX) return [];
  return _wikiTitlesInBounds(WIKI_GEO_INDEX, _WIKI_GEO_BUCKETS, W, E, S, N);
}
// The same for any geo-index (the base's, a Wikipedia pack's): `grid`
// keeps that index's buckets ({buckets, src}), rebuilt when it changes.
function _wikiTitlesInBounds(index, grid, W, E, S, N) {
  if (grid.src !== index) {
    grid.buckets = new Map();
    for (var t in index) {
      var g = index[t];
      if (!g || typeof g[0] !== 'number' || typeof g[1] !== 'number') continue;
      var k = wikiGeoBucketKey(g[0], g[1]);
      var arr = grid.buckets.get(k);
      if (!arr) grid.buckets.set(k, arr = []);
      arr.push(t);
    }
    grid.src = index;
  }
  var out = [];
  var la0 = Math.max(-90, Math.floor(S)), la1 = Math.min(89, Math.floor(N));
  var lo0 = Math.max(-180, Math.floor(W)), lo1 = Math.min(179, Math.floor(E));
  for (var la = la0; la <= la1; la++) {
    for (var lo = lo0; lo <= lo1; lo++) {
      var b = grid.buckets.get(wikiGeoBucketKey(la, lo));
      if (!b) continue;
      for (var i = 0; i < b.length; i++) {
        var g2 = index[b[i]];
        if (g2[1] < W || g2[1] > E || g2[0] < S || g2[0] > N) continue;
        out.push(b[i]);
      }
    }
  }
  return out;
}
// END wiki-geo-bounds
(function loadWikiTitleBridge() {
  // The bridge sits beside the PWA, never in a ZIM: under kiwix-serve this
  // fetch was a 404 on every page load.
  if (typeof SZ_ON_DRIVE_PWA === 'undefined' || !SZ_ON_DRIVE_PWA) return;
  try {
    fetch(baseUrl + '../wiki-qid-titles.json')
      .then(function(r) { return r && r.ok ? r.json() : null; })
      .then(function(m) { if (m && typeof m === 'object') WIKI_QID_TITLES = m; })
      .catch(function() {});
  } catch (e) {}
})();

// BEGIN wiki-article-path
// The title a `w` tag names, as the ZIM stores it (wiki-article/<Title>):
// its language prefix (2-3 letters before the first colon, any case: "en:",
// "NL:") dropped, spaces as underscores. The same rule as the build's
// (cloud/wiki_articles._strip_lang, the geo-index in zim_writer).
function _wikiTagTitle(w) {
  var s = String(w), ci = s.indexOf(':');
  var t = (ci >= 2 && ci <= 3 && /^\p{L}+$/u.test(s.slice(0, ci))) ? s.slice(ci + 1) : s;
  return t.replace(/ /g, '_');
}

// Resolve the in-ZIM article path for a place, or null when this ZIM has
// no article for it. wiki-geo-index.json (WIKI_GEO_INDEX) lists every
// bundled article a record links to, so a title outside it has no page:
// most `w` tags of a non-English country name an article English Wikipedia
// does not have (7,519 of 10,767 records with `w` in the Netherlands map
// showed a Wikipedia button that opened a missing page). Until the index
// has loaded (or in a ZIM without bundled articles) there is no button:
// the index is what says an article exists. Prefer the OSM `wikipedia` tag
// (`w`, "en:Golden Gate Bridge"), else the geo-index reverse map, else the
// side-load bridge.
function _wikiArticlePath(qid, w) {
  if (!WIKI_GEO_INDEX) return null;
  var t = null;
  if (w) {
    t = _wikiTagTitle(w);
  } else if (qid) {
    t = (WIKI_GEO_QID && WIKI_GEO_QID[qid])
        || (WIKI_QID_TITLES && WIKI_QID_TITLES[qid]) || null;
    t = t ? String(t).replace(/ /g, '_') : null;
  }
  return t && Object.prototype.hasOwnProperty.call(WIKI_GEO_INDEX, t) ? t : null;
}
// END wiki-article-path

// BEGIN wiki-packs
// Several Wikipedias in one map (docs/language-matrix-prototype.md). A
// merged ZIM is a base plus one "Wikipedia pack" per further language, each
// under its own prefix: ~w/<xx>/wiki-article/<Title> and
// ~w/<xx>/wiki-geo-index.json ({title: [lat, lon, type, desc, qid]}).
// map-config.json lists them as wikiPacks [{lang, prefix, articles}]; the
// base's own Wikipedia stays where it was (wiki-article/,
// wiki-geo-index.json, [lat, lon, type, qid, desc]). Articles about one
// place are joined by its Wikidata Q-ID. A ZIM without wikiPacks keeps
// WIKI_PACKS empty, and every function here then gives exactly the single
// link _wikiArticlePath gives.
var WIKI_PACKS = [];      // [{lang, prefix, index, qid, grid}], in map-config order
var WIKI_MAP_LANG = '';   // map-config "language": the map's (and UI's) language
var WIKI_BASE_LANG = '';  // the base Wikipedia's language; '' when not known

// Packs come from map-config.json; a prefix is part of every URL built
// from it, so only the documented shape is taken: ~w/<lang>/, lower case,
// the pack's own language (a language code may come in any case).
var _WIKI_LANG = /^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$/;
function _wikiLangCode(l) {
  return typeof l === 'string' ? l.toLowerCase() : '';
}
function _wikiPacksFromConfig(config) {
  var out = [], seen = {};
  var list = config && config.wikiPacks;
  if (!Array.isArray(list)) return out;
  for (var i = 0; i < list.length; i++) {
    var p = list[i];
    if (!p || typeof p.prefix !== 'string') continue;
    var lang = _wikiLangCode(p.lang);
    if (!_WIKI_LANG.test(lang) || p.prefix !== '~w/' + lang + '/') continue;
    if (seen[p.prefix]) continue;
    seen[p.prefix] = true;
    out.push({ lang: lang, prefix: p.prefix });
  }
  return out;
}

// Is a Wikipedia in language `wiki` the one of map language `map`? The
// same code, or the map's primary subtag (map zh-Hans, Wikipedia zh).
function _wikiLangIs(wiki, map) {
  return !!map && (wiki === map || wiki === map.split('-')[0]);
}

// Called once map-config.json has loaded. The base's language is
// "baseLanguage" when the merge wrote it; else, when no pack is in the map
// language, the map language itself (a menu's packs never repeat the
// base's language); else unknown.
function wikiPacksInit(config) {
  var packs = _wikiPacksFromConfig(config);
  WIKI_PACKS = packs.map(function(p) {
    return { lang: p.lang, prefix: p.prefix, index: null, qid: null,
             grid: { buckets: null, src: null } };
  });
  var lang = _wikiLangCode(config && config.language);
  var base = _wikiLangCode(config && config.baseLanguage);
  if (!base && !packs.some(function(p) { return _wikiLangIs(p.lang, lang); })) base = lang;
  WIKI_MAP_LANG = lang;
  WIKI_BASE_LANG = base;
  return WIKI_PACKS;
}

// A geo-index entry's Q-ID and description. The base and the packs write
// [lat, lon, type, qid, desc] (zim_writer, streetzim/wiki_pack.py). A pack
// written in the first spec's order, [lat, lon, type, desc, qid], is read
// that way only when its Q-ID slot (g[3]) holds no Q-ID. A description
// that looks like a Q-ID ("Q7") is never taken for one where the Q-ID
// slot has one.
var _WIKI_QID = /^Q\d+$/;
function _wikiIsQid(v) {
  return typeof v === 'string' && _WIKI_QID.test(v);
}
function _wikiEntryQid(g, pack) {
  if (!g) return null;
  if (_wikiIsQid(g[3])) return g[3];
  return pack && _wikiIsQid(g[4]) ? g[4] : null;
}
function _wikiEntryDesc(g, pack) {
  if (!g) return '';
  var d = (pack && !_wikiIsQid(g[3]) && _wikiIsQid(g[4])) ? g[3] : g[4];
  return typeof d === 'string' ? d : '';
}

// Take a pack's loaded wiki-geo-index.json. false when it is not one. A
// Q-ID that two titles carry maps to the later one, the rule the base's
// reverse map (WIKI_GEO_QID, 410) has always had.
function wikiPackSetIndex(pack, m) {
  if (!m || typeof m !== 'object' || Array.isArray(m)) return false;
  var q = {};
  for (var t in m) {
    if (!Object.prototype.hasOwnProperty.call(m, t)) continue;
    var id = _wikiEntryQid(m[t], true);
    if (id) q[id] = t;
  }
  pack.index = m;
  pack.qid = q;
  return true;
}

// The Wikipedias in the order a place's article is offered: the map
// language's pack, then the base's own (prefix ''), then the other packs
// in map-config order. When the map is in the base's language the base
// comes first.
function _wikiSources() {
  var base = { lang: WIKI_BASE_LANG, prefix: '', base: true,
               index: WIKI_GEO_INDEX, qid: WIKI_GEO_QID, grid: _WIKI_GEO_BUCKETS };
  var out = [];
  var mapFirst = WIKI_MAP_LANG && !_wikiLangIs(WIKI_BASE_LANG, WIKI_MAP_LANG);
  WIKI_PACKS.forEach(function(p) { if (mapFirst && _wikiLangIs(p.lang, WIKI_MAP_LANG)) out.push(p); });
  out.push(base);
  WIKI_PACKS.forEach(function(p) { if (out.indexOf(p) < 0) out.push(p); });
  return out;
}

// A place's bundled articles, preferred first: [{lang, prefix, title}],
// one per Wikipedia. Every Wikipedia is asked by the place's Q-ID first.
// A pack is then asked for the `w` tag's title when the tag is in its
// language; the base, as _wikiArticlePath asks it (tag, then Q-ID), but a
// tag's title whose entry carries another Q-ID is a different place (an
// "de:Eschen" tag names the village; English "Eschen" may be the tree).
// Without packs: the one link _wikiArticlePath gives, or none.
function _wikiArticleLinks(qid, w) {
  var has = Object.prototype.hasOwnProperty;
  if (!WIKI_PACKS.length) {
    var only = _wikiArticlePath(qid, w);
    return only ? [{ lang: WIKI_BASE_LANG, prefix: '', title: only }] : [];
  }
  var wt = null, wl = null;
  if (w) {
    wt = _wikiTagTitle(w);
    var s = String(w), ci = s.indexOf(':');
    if (ci >= 2 && ci <= 3) wl = s.slice(0, ci).toLowerCase();
  }
  var out = [];
  _wikiSources().forEach(function(src) {
    if (!src.index) return;
    var t = null;
    if (qid && src.qid && has.call(src.qid, qid)) {
      t = src.qid[qid];
    } else if (src.base) {
      t = _wikiArticlePath(qid, w);
      var tq = t && _wikiEntryQid(src.index[t], false);
      if (qid && tq && tq !== qid) t = null;
    } else if (wt && wl === src.lang && has.call(src.index, wt)) {
      var wq = _wikiEntryQid(src.index[wt], true);
      t = (qid && wq && wq !== qid) ? null : wt;
    }
    if (t && has.call(src.index, t)) out.push({ lang: src.lang, prefix: src.prefix, title: t });
  });
  return out;
}

// The links for the Wiki panel's detail of one entry: the place's articles
// by Q-ID, and the entry's own (title, prefix) when that did not find it.
function _wikiDetailLinks(qid, title, prefix) {
  var links = qid ? _wikiArticleLinks(qid, null) : [];
  if (!title) return links;
  prefix = prefix || '';
  for (var i = 0; i < links.length; i++) {
    if (links[i].prefix === prefix && links[i].title === title) return links;
  }
  var lang = WIKI_BASE_LANG;
  WIKI_PACKS.forEach(function(p) { if (p.prefix === prefix) lang = p.lang; });
  return [{ lang: lang, prefix: prefix, title: title }]
    .concat(links.filter(function(l) { return l.prefix !== prefix; }));
}

// The Wiki panel's rows in [W,E]×[S,N]: {title, prefix, name, qid, type,
// coords, desc, priority}. Without packs, one per base entry, exactly as
// before. With packs, one per place: its articles are joined by Q-ID (an
// entry without one is a place of its own), and the row shows the
// preferred Wikipedia's entry.
function _wikiPanelItems(W, E, S, N, priority) {
  var items = [];
  if (!WIKI_PACKS.length) {
    var titles = wikiGeoTitlesInBounds(W, E, S, N);
    for (var ti = 0; ti < titles.length; ti++) {
      var title = titles[ti];
      var g = WIKI_GEO_INDEX[title];
      var lat = g[0], lon = g[1];
      items.push({ title: title, name: title.replace(/_/g, ' '),
                   qid: g[3] || null, type: g[2] || '', coords: [lon, lat],
                   desc: g[4] || '', priority: priority(g[2] || '') });
    }
    return items;
  }
  var byKey = {};
  _wikiSources().forEach(function(src) {
    if (!src.index) return;
    _wikiTitlesInBounds(src.index, src.grid, W, E, S, N).forEach(function(t) {
      var e = src.index[t];
      var q = _wikiEntryQid(e, !src.base);
      var key = q ? 'q:' + q : 't:' + src.prefix + t;
      if (Object.prototype.hasOwnProperty.call(byKey, key)) return;
      byKey[key] = true;
      items.push({ title: t, prefix: src.prefix, lang: src.lang, name: t.replace(/_/g, ' '),
                   qid: q, type: e[2] || '', coords: [e[1], e[0]],
                   desc: _wikiEntryDesc(e, !src.base), priority: priority(e[2] || '') });
    });
  });
  return items;
}

// An article's address, relative to the viewer. Keep '/' literal: a title
// like Expo_Park/USC_station is stored two levels deep and its images are
// linked ../../wiki-image/…, which only resolves if the browser sees the
// real depth (%2F collapses it to one segment).
function _wikiArticleHref(title, prefix) {
  return (prefix || '') + 'wiki-article/' + encodeURIComponent(title).replace(/%2F/g, '/');
}

// A Wikipedia's name in its own language ("Deutsch", "ไทย"), as the
// language links of Wikipedia itself show it.
function _wikiLangName(lang) {
  if (!lang) return szT('wiki.base_wikipedia', 'Wikipedia');
  try {
    if (typeof Intl !== 'undefined' && Intl.DisplayNames) {
      var n = new Intl.DisplayNames([lang], { type: 'language' }).of(lang);
      if (n && n !== lang) return n.charAt(0).toLocaleUpperCase(lang) + n.slice(1);
    }
  } catch (e) {}
  return lang.toUpperCase();
}

// "Also in: Deutsch · English" under a place's article button: a link to
// each of its other articles. null when there is only one.
function _wikiAlsoIn(links) {
  if (!links || links.length < 2) return null;
  var row = document.createElement('div');
  row.className = 'wiki-also';
  row.setAttribute('data-wiki-also', links.slice(1).map(function(l) { return l.lang; }).join(','));
  row.style.cssText = 'font-size:12.5px;line-height:1.6;margin:2px 0 8px;color:var(--szd-fg-3, #666);';
  var label = document.createElement('span');
  label.textContent = szT('wiki.also_in', 'Also in:') + ' ';
  row.appendChild(label);
  links.slice(1).forEach(function(l, i) {
    if (i) row.appendChild(document.createTextNode(' · '));
    var a = document.createElement('a');
    a.className = 'wiki-also-link';
    a.href = baseUrl + _wikiArticleHref(l.title, l.prefix);
    a.setAttribute('data-wiki-lang', l.lang);
    a.setAttribute('data-wiki-path', l.prefix + 'wiki-article/' + l.title);
    if (l.lang) a.setAttribute('lang', l.lang);
    a.style.cssText = 'color:var(--szd-link, #2a4a7a);font-weight:600;text-decoration:none;';
    a.textContent = _wikiLangName(l.lang);
    a.addEventListener('click', function(e) {
      e.preventDefault();
      e.stopPropagation();
      openWikiArticle(l.title, l.prefix);
    });
    row.appendChild(a);
  });
  return row;
}
// END wiki-packs

// BEGIN wiki-yours
// Your language first. On a map with Wikipedia packs the articles in the
// map's language (which is also the UI's) are "yours"; the others, usually
// the base's English, come after them in the Wiki panel, name their
// language there and on the article buttons, and get a lighter map marker.
// Without packs there are no others: every function here then gives
// exactly what the viewer showed before.

// The map language when this file has a Wikipedia in it, else '' (no
// packs; or, say, a French map of an English base with a German pack,
// where no article is yours and none is put behind).
function _wikiYourLang() {
  if (!WIKI_PACKS.length || !WIKI_MAP_LANG) return '';
  var has = _wikiLangIs(WIKI_BASE_LANG, WIKI_MAP_LANG)
    || WIKI_PACKS.some(function(p) { return _wikiLangIs(p.lang, WIKI_MAP_LANG); });
  return has ? WIKI_MAP_LANG : '';
}

// Is an article in `lang` put behind yours? '' (the base's, language not
// known) is: it is not the map language's, or _wikiYourLang would say so.
function _wikiIsOther(lang) {
  var y = _wikiYourLang();
  return !!y && !_wikiLangIs(lang || '', y);
}

// A language's name in the UI language ("Englisch" in a German UI); its
// code in capitals where Intl.DisplayNames is missing.
function _wikiLangLabel(lang) {
  try {
    if (typeof Intl !== 'undefined' && Intl.DisplayNames) {
      var n = new Intl.DisplayNames([SZ_UI_LANG], { type: 'language' }).of(lang);
      if (n && n !== lang) return n;
    }
  } catch (e) {}
  return String(lang).toUpperCase();
}

// The language an article names: '' for one in the map language, of a
// language not known, or on a map without packs.
function _wikiLangTag(lang) {
  if (!WIKI_PACKS.length || !lang || _wikiLangIs(lang, WIKI_MAP_LANG)) return '';
  return _wikiLangLabel(lang);
}

// The Wiki panel's rows: { total, items }. As always, the places in view
// by priority, then name, and the first `cap` of them; on a map with
// packs, your language's places first within those. The cap is taken
// before your language is put first, so a dense, zoomed-out view keeps its
// most important places in every language (the cities an English article
// has stay; a German view of Los Angeles at zoom 10 kept none of them).
function _wikiPanelCompare(a, b) {
  return b.priority - a.priority || a.name.localeCompare(b.name);
}
function _wikiPanelPick(all, onlyYours, cap) {
  var items = _wikiPanelFilter(all, onlyYours).sort(_wikiPanelCompare);
  var total = items.length;
  if (items.length > cap) items = items.slice(0, cap);
  return { total: total, items: _wikiYoursFirst(items) };
}
// Your language's first, each part in its order; the same array without
// packs or without a Wikipedia in the map language.
function _wikiYoursFirst(items) {
  if (!_wikiYourLang()) return items;
  return items.filter(function(it) { return !_wikiIsOther(it.lang); })
    .concat(items.filter(function(it) { return _wikiIsOther(it.lang); }));
}

// "Only <language> articles": the rows in your language when the reader
// asked for that and the map has a Wikipedia in it; otherwise all of them.
function _wikiPanelFilter(items, onlyYours) {
  if (!onlyYours || !_wikiYourLang()) return items;
  return items.filter(function(it) { return !_wikiIsOther(it.lang); });
}

function _wikiEl(tag, className, text) {
  var el = document.createElement(tag);
  if (className) el.className = className;
  if (text) el.appendChild(document.createTextNode(text));
  return el;
}

// One row of the Wiki panel (410 adds its click): name; type and, for an
// article in another language, that language; the description.
function _wikiPanelRowEl(it) {
  var desc = it.desc || '';
  var div = _wikiEl('div', 'wiki-item' + (desc ? '' : ' wiki-item-nodesc'));
  div.appendChild(_wikiEl('div', 'wiki-item-name', it.name));
  var meta = _wikiEl('div', 'wiki-item-meta');
  meta.appendChild(_wikiEl('span', 'wiki-item-type', szPlaceType(it.type || 'place')));
  var tag = _wikiLangTag(it.lang);
  if (tag) {
    var lt = _wikiEl('span', 'wiki-item-lang', tag);
    lt.setAttribute('data-wiki-lang', it.lang);
    meta.appendChild(lt);
  }
  div.appendChild(meta);
  if (desc) div.appendChild(_wikiEl('div', 'wiki-item-desc', desc));
  // data-wiki-lang: which Wikipedia the row opens, for the gates.
  if (WIKI_PACKS.length) div.setAttribute('data-wiki-lang', it.lang || '');
  return div;
}

// A Wiki panel place's map marker (410 adds its click): a 📖 (DOM, not a
// symbol layer: the bundled glyphs have no emoji). A place whose article is
// in another language than yours gets a smaller, greyed-out one, faded by
// its Marker (_wikiMarkerOptions). With packs, a screen reader reads the
// title, not MapLibre's "Map marker".
function _wikiMarkerEl(it) {
  var el = document.createElement('div');
  el.className = 'wiki-map-marker';
  el.textContent = '📖';
  el.title = it.name;
  var other = _wikiIsOther(it.lang);
  el.style.cssText = (other ? 'font-size:14px;filter:grayscale(1);' : 'font-size:18px;')
    + 'line-height:1;cursor:pointer;'
    + 'text-shadow:0 0 3px #fff,0 1px 2px rgba(255,255,255,0.95);';
  if (other) {
    el.className += ' wiki-map-marker-other';
    var tag = _wikiLangTag(it.lang);
    if (tag) el.title = it.name + ' · ' + tag;
  }
  if (WIKI_PACKS.length) {
    el.setAttribute('data-wiki-lang', it.lang || '');
    el.setAttribute('aria-label', el.title);
  }
  return el;
}

// Its maplibregl.Marker options. The fading is the Marker's `opacity`: a
// Marker sets its element's style.opacity on every move (MapLibre 5,
// _updateOpacity), which overwrote an inline one.
var WIKI_OTHER_OPACITY = '0.6';
function _wikiMarkerOptions(el, it) {
  var o = { element: el, anchor: 'center' };
  if (_wikiIsOther(it.lang)) o.opacity = WIKI_OTHER_OPACITY;
  return o;
}

// The order the markers are added in: the others first, so the ones in
// your language are drawn over them where they overlap.
function _wikiMarkerOrder(items) {
  if (!_wikiYourLang()) return items;
  return items.filter(function(it) { return _wikiIsOther(it.lang); })
    .concat(items.filter(function(it) { return !_wikiIsOther(it.lang); }));
}

// The article button's label; for an article in another language than the
// map's, one that names it ("Read full article in English").
function _wikiReadLabel(link) {
  var tag = link ? _wikiLangTag(link.lang) : '';
  return tag ? szT('popup.read_article_in', '📖 Read full article in {language}', { language: tag })
    : szT('popup.read_article', '📖 Read full article');
}
// The place sheet's Wikipedia button (210): "Wikipedia", or
// "Wikipedia (English)" for an article in another language.
function _wikiSheetLabel(link) {
  var tag = link ? _wikiLangTag(link.lang) : '';
  return tag ? szT('place.wikipedia_in', 'Wikipedia ({language})', { language: tag })
    : szT('place.wikipedia', 'Wikipedia');
}

// The panel's "Only <language> articles" switch (410), shown when the map
// has packs and a Wikipedia in its language. The choice is kept on this
// device (`storage`: _szStorage()'s, or null).
var WIKI_ONLY_YOURS_KEY = 'streetzim.wikiOnlyYours';
function _wikiOnlyYoursSaved(storage) {
  try { return !!storage && storage.getItem(WIKI_ONLY_YOURS_KEY) === '1'; } catch (e) { return false; }
}
function _wikiOnlyYoursRow(checked, storage, onChange) {
  var row = _wikiEl('label', 'wiki-only-yours');
  row.setAttribute('data-wiki-only', _wikiYourLang());
  var box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = !!checked;
  row.appendChild(box);
  row.appendChild(_wikiEl('span', '', szT('wiki.only_lang', 'Only {language} articles',
                                          { language: _wikiLangLabel(_wikiYourLang()) })));
  box.addEventListener('change', function() {
    try { if (storage) storage.setItem(WIKI_ONLY_YOURS_KEY, box.checked ? '1' : '0'); } catch (e) {}
    onChange(!!box.checked);
  });
  return row;
}
// END wiki-yours

// Open a bundled Wikipedia article in a full-screen in-app overlay with a
// "← Back to map" bar, instead of window.open('_blank'). In the Kiwix WebView
// '_blank' replaces the view with the chrome-less article and leaves no way
// back (no close/back button). An iframe overlay keeps the map underneath and
// the back bar is TOP-anchored, so it renders in the Kiwix WebView. Internal
// article links stay inside the iframe; the bar always returns to the map.
// `prefix` is a Wikipedia pack's (~w/<xx>/), '' or absent for the base's.
function openWikiArticle(path, prefix) {
  if (!path) return;
  var url = baseUrl + _wikiArticleHref(path, prefix);
  // Navigate the whole view to the article as a native ZIM page. We do NOT use
  // an in-page iframe overlay: in the iOS/Kiwix WebView an <iframe> expands to
  // its full content height and escapes any scroll container, so the article —
  // and its chrome — could be scrolled off-screen. A native page navigation
  // gives proper native scrolling and the app's own Back button. First stamp
  // the current map view into the URL hash so Back returns to the same place
  // (applyHash restores `#map=z/lat/lon` on the way back).
  try {
    var mp = window.__szMap;
    if (mp) {
      var c = mp.getCenter();
      var z = Math.round(mp.getZoom() * 100) / 100;
      var h = '#map=' + z + '/' + (Math.round(c.lat * 1e5) / 1e5)
            + '/' + (Math.round(c.lng * 1e5) / 1e5);
      if (history && history.replaceState) {
        history.replaceState(null, '', location.pathname + location.search + h);
      } else {
        location.hash = h;
      }
    }
  } catch (e) {}
  window.location.href = url;
}

// Publish the REAL visible window size as CSS variables. In the Kiwix
// WKWebView the layout viewport is the full device screen (client/scrollHeight
// = 956 on an iPhone 16) while only window.innerHeight = 772 is actually
// visible between the app's chrome (measured with the ruler diagnostic;
// visualViewport agrees) — so percentage/vh layout dropped every
// bottom-anchored control ~184px below the fold. body{height:var(--app-height)}
// plus the fixed sheets' bottom:var(--bottom-inset) anchor the whole UI to
// what is really on screen. On desktop/PWA the inset is 0 and nothing changes.
function setViewportVars() {
  try {
    var de = document.documentElement;
    // iOS Safari: window.innerHeight still spans under the bottom toolbar
    // and the home-indicator band, so anything anchored to bottom:0 sits
    // behind them (the "Directions to here" / "Show in this area" buttons
    // were unreachable on an iPhone). visualViewport is what is really
    // visible; size the app to it and push bottom sheets up by the gap.
    var vv = window.visualViewport;
    // A pinch-zoom (iOS ignores user-scalable=no) shrinks the visual
    // viewport without changing what the layout has to fit; leave the
    // variables alone rather than squeeze --app-height to the zoomed
    // area (docs/mobile-browser-review.md §B5).
    if (vv && vv.scale && Math.abs(vv.scale - 1) > 0.01) return;
    var visible = (vv && vv.height) ? vv.height : window.innerHeight;
    var inset = Math.max(0, de.clientHeight - window.innerHeight);
    if (vv && vv.height) {
      inset = Math.max(inset, window.innerHeight - vv.height - (vv.offsetTop || 0));
    }
    de.style.setProperty('--app-height', Math.round(visible) + 'px');
    de.style.setProperty('--bottom-inset', Math.round(inset) + 'px');
  } catch (e) {}
}
setViewportVars();
window.addEventListener('resize', setViewportVars);
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', setViewportVars);
  window.visualViewport.addEventListener('scroll', setViewportVars);
}
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', setViewportVars);
}
window.addEventListener('orientationchange', function() {
  setTimeout(setViewportVars, 250);
});
window.addEventListener('load', function() { setTimeout(setViewportVars, 300); });

