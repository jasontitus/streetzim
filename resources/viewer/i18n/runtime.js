// BEGIN sz-i18n-runtime
// The viewer's UI strings in the map's language (docs/i18n.md). ES5: the
// fatal page uses it in browsers too old for anything newer.
//
// Source: resources/viewer/i18n/runtime.js. tools/build_i18n.py copies it,
// with the translation tables, into index.html (part 005) and places.html;
// edit the source, then run python tools/build_viewer.py.
//
// The tables are inlined because a published ZIM can never gain a file
// (docs/viewer-slots.md). English is not a table: each call site carries
// its English text, szT('key', 'English', vars), and tools/build_i18n.py
// collects those into resources/viewer/i18n/en.json for translators.
// A language without a table, or a key a table lacks, shows the English.
var SZ_UI_LANG = 'en';      // the language the UI is shown in
var SZ_I18N = null;         // its table, or null for English
var SZ_I18N_PSEUDO = false; // ?uilang=qps: every string as [text], to find misses

// Pick the UI language once map-config.json has loaded: its `language`
// (create_osm_zim --language), unless the page URL says ?uilang=xx.
function szSetUiLanguage(lang, search) {
  var m = /[?&]uilang=([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)?)/.exec(String(search || ''));
  if (m) lang = m[1];
  var base = String(lang || 'en').toLowerCase().split('-')[0];
  var tables = {};
  try {
    var el = document.getElementById('sz-i18n');
    if (el) tables = JSON.parse(el.textContent) || {};
  } catch (e) { tables = {}; }
  SZ_I18N_PSEUDO = base === 'qps';
  SZ_I18N = (base !== 'en' && !SZ_I18N_PSEUDO &&
             Object.prototype.hasOwnProperty.call(tables, base) && tables[base]) || null;
  SZ_UI_LANG = SZ_I18N ? base : 'en';
  try { if (SZ_I18N || SZ_I18N_PSEUDO) document.documentElement.lang = SZ_UI_LANG; } catch (e) {}
  return SZ_UI_LANG;
}

// '{name} is closed' + { name: 'Zoo' }. An unknown {x} is left as is.
function szFill(s, vars) {
  if (!vars) return s;
  return String(s).replace(/\{(\w+)\}/g, function(all, k) {
    return Object.prototype.hasOwnProperty.call(vars, k) && vars[k] != null ? String(vars[k]) : all;
  });
}

// The UI text for `key`: the table's, else `en` (never the bare key).
function szT(key, en, vars) {
  var s = SZ_I18N && typeof SZ_I18N[key] === 'string' ? SZ_I18N[key] : en;
  s = szFill(s, vars);
  return SZ_I18N_PSEUDO ? '[' + s + ']' : s;
}

// A count: forms is { one: '{n} link', other: '{n} links' } (English),
// a table entry the same with the language's plural categories. {n} is
// String(n) unless vars gives it.
function szPluralCategory(n) {
  if (SZ_I18N) {
    try { return new Intl.PluralRules(SZ_UI_LANG).select(n); } catch (e) {}
  }
  return n === 1 ? 'one' : 'other';
}
function szTn(key, n, forms, vars) {
  var tab = SZ_I18N && SZ_I18N[key] && typeof SZ_I18N[key] === 'object' ? SZ_I18N[key] : null;
  var cat = szPluralCategory(n);
  var s = tab ? (tab[cat] != null ? tab[cat] : tab.other) : null;
  if (s == null) s = forms[n === 1 ? 'one' : 'other'];
  var v = { n: SZ_I18N ? szLocaleNum(n) : String(n) };
  for (var k in vars) if (Object.prototype.hasOwnProperty.call(vars, k)) v[k] = vars[k];
  s = szFill(s, v);
  return SZ_I18N_PSEUDO ? '[' + s + ']' : s;
}

// Numbers in the UI language. English keeps exactly what the viewer
// printed before translation: x.toFixed(d) and x.toLocaleString().
function szFixed(x, d) {
  x = Number(x);
  if (SZ_I18N) {
    try {
      return new Intl.NumberFormat(SZ_UI_LANG, { minimumFractionDigits: d,
        maximumFractionDigits: d, useGrouping: false }).format(x);
    } catch (e) {}
  }
  return x.toFixed(d);
}
function szLocaleNum(x) {
  x = Number(x);
  if (SZ_I18N) {
    try { return x.toLocaleString(SZ_UI_LANG); } catch (e) {}
  }
  return x.toLocaleString();
}
// "July 2026" for (2026, 6).
var SZ_MONTHS_EN = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                    'August', 'September', 'October', 'November', 'December'];
function szMonthYear(year, month0) {
  if (SZ_I18N) {
    try {
      return new Intl.DateTimeFormat(SZ_UI_LANG, { month: 'long', year: 'numeric', timeZone: 'UTC' })
        .format(new Date(Date.UTC(year, month0, 1)));
    } catch (e) {}
  }
  return SZ_MONTHS_EN[month0] ? SZ_MONTHS_EN[month0] + ' ' + year : '';
}

// Static markup: <b data-i18n="key">English</b>, and data-i18n-title /
// -placeholder / -aria-label for those attributes. One pass, after
// szSetUiLanguage; English leaves the page untouched. (Text that code
// fills in with szT is marked data-i18n-code; text that stays as written,
// such as a licence, translate="no". tests/test_viewer_i18n.py checks that
// all visible markup text is one of the three.)
var SZ_I18N_ATTRS = ['title', 'placeholder', 'aria-label'];
function szApplyI18n(root) {
  if (!SZ_I18N && !SZ_I18N_PSEUDO) return;
  root = root || document;
  var sel = '[data-i18n],[data-i18n-title],[data-i18n-placeholder],[data-i18n-aria-label]';
  var els = root.querySelectorAll(sel);
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    var key = el.getAttribute('data-i18n');
    var text = String(el.textContent).replace(/\s+/g, ' ').replace(/^ | $/g, '');
    if (key) el.textContent = szT(key, text);  // i18n-dynamic
    for (var j = 0; j < SZ_I18N_ATTRS.length; j++) {
      var a = SZ_I18N_ATTRS[j];
      var ak = el.getAttribute('data-i18n-' + a);
      if (ak) el.setAttribute(a, szT(ak, el.getAttribute(a) || ''));  // i18n-dynamic
    }
  }
}
// The one call a page makes once it knows the language: pick the table,
// translate the static markup, and tell code that rendered text before
// this (it listens for 'sz-ui-language') to render it again.
function szUseLanguage(lang, search) {
  szSetUiLanguage(lang, search);
  szApplyI18n(document);
  try {
    var ev = document.createEvent('Event');
    ev.initEvent('sz-ui-language', false, false);
    window.dispatchEvent(ev);
  } catch (e) {}
  return SZ_UI_LANG;
}

// "1.2K" / "3.4M" / "1.1B" (Wikidata population and the like).
function szCompactNum(n) {
  if (n >= 1e9) return szT('num.billions', '{n}B', { n: szFixed(n / 1e9, 1) });
  if (n >= 1e6) return szT('num.millions', '{n}M', { n: szFixed(n / 1e6, 1) });
  if (n >= 1e3) return szT('num.thousands', '{n}K', { n: szFixed(n / 1e3, 1) });
  return SZ_I18N ? szLocaleNum(n) : n.toString();
}

// A Find chip's label by its id (cloud/chip_rules.py). English shows the
// label the ZIM's manifest or viewer gave, exactly as before; a translated
// UI shows the table's word for that id, else that label.
function szChipLabel(id, label) {
  if (!SZ_I18N) return SZ_I18N_PSEUDO ? '[' + label + ']' : label;
  switch (id) {
    case 'food': return szT('chip.food', 'Food & Drink');
    case 'bars': return szT('chip.bars', 'Bars');
    case 'shops': return szT('chip.shops', 'Shops');
    case 'health': return szT('chip.health', 'Health');
    case 'museums': return szT('chip.museums', 'Museums');
    case 'landmarks': return szT('chip.landmarks', 'Landmarks');
    case 'libraries': return szT('chip.libraries', 'Libraries');
    case 'parks': return szT('chip.parks', 'Parks');
    case 'fuel': return szT('chip.fuel', 'Gas');
    case 'hotels': return szT('chip.hotels', 'Hotels');
    // Chips of ZIMs built before the 2026-09-16 Food & Drink merge.
    case 'restaurants': return szT('chip.restaurants', 'Restaurants');
    case 'cafes': return szT('chip.cafes', 'Cafés');
  }
  return label;
}

// A chip's name inside a sentence ("No food & drink in this map"): English
// lower-cases the label; a translation keeps its word as the table spells
// it (German nouns are capitalised).
function szChipInSentence(id, label) {
  var l = szChipLabel(id, label);
  return SZ_I18N ? l : l.toLowerCase();
}

// A place type (OSM/Overture key such as fast_food, the record's s / cat)
// for display: the table's "type.<key>" (inlined as type: { key: text })
// when it has one, else the key with spaces for underscores (all English
// ever showed).
function szPlaceType(kind) {
  var k = String(kind == null ? '' : kind);
  var types = SZ_I18N && SZ_I18N.type;
  var s = types && Object.prototype.hasOwnProperty.call(types, k) && typeof types[k] === 'string'
        ? types[k] : k.replace(/_/g, ' ');
  return SZ_I18N_PSEUDO ? '[' + s + ']' : s;
}
// END sz-i18n-runtime
