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
var SZ_UI_RTL = false;      // the UI language is written right to left
var SZ_NUM_LANG = 'en';     // the locale numbers and dates are formatted in
// Right-to-left languages: the page gets <html dir="rtl"> (and its CSS
// mirrors the panels) when the UI is in one of them.
var SZ_RTL_LANGS = { ar: 1, fa: 1, he: 1, ur: 1, ps: 1, ckb: 1, yi: 1, dv: 1, sd: 1, ug: 1 };

// The tables are inlined compressed (tools/build_i18n.py encode_tables):
// { v: 2, k: KEYS, t: { lang: TEXTS }, m: { lang: settings } }, where KEYS
// and TEXTS are JSON arrays, raw DEFLATE, base64. These decode them, in
// ES5 and synchronously, for the one language the page shows.
var SZ_B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
function szB64Bytes(s) {
  var out = [], buf = 0, n = 0, i, v;
  for (i = 0; i < s.length; i++) {
    if (s.charAt(i) === '=') break;
    v = SZ_B64.indexOf(s.charAt(i));
    if (v < 0) throw new Error('i18n: bad base64');
    buf = (buf << 6) | v; n += 6;
    if (n >= 8) { n -= 8; out.push((buf >> n) & 255); }
  }
  return out;
}
// Raw DEFLATE (RFC 1951), after Mark Adler's puff.c: stored, fixed and
// dynamic Huffman blocks. Throws on a malformed or truncated stream.
var SZ_LBASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99,
                115, 131, 163, 195, 227, 258];
var SZ_LEXT = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0];
var SZ_DBASE = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025,
                1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577];
var SZ_DEXT = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12,
               13, 13];
var SZ_CLORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15];
function szInflate(src) {
  var out = [], pos = 0, bitbuf = 0, bitcnt = 0;
  function bits(need) {
    while (bitcnt < need) {
      if (pos >= src.length) throw new Error('i18n: truncated stream');
      bitbuf |= src[pos++] << bitcnt; bitcnt += 8;
    }
    var v = bitbuf & ((1 << need) - 1);
    bitbuf >>>= need; bitcnt -= need;
    return v;
  }
  // A canonical Huffman code from its code lengths: count[len], then the
  // symbols in code order. An over-subscribed set is an error.
  function huff(lengths, off, n) {
    var count = [], sym = [], offs = [0, 0], len, i, left = 1;
    for (len = 0; len < 16; len++) count[len] = 0;
    for (i = 0; i < n; i++) count[lengths[off + i]]++;
    for (len = 1; len < 16; len++) { left = left * 2 - count[len]; if (left < 0) throw new Error('i18n: bad code'); }
    for (len = 1; len < 15; len++) offs[len + 1] = offs[len] + count[len];
    for (i = 0; i < n; i++) if (lengths[off + i]) sym[offs[lengths[off + i]]++] = i;
    return { count: count, sym: sym };
  }
  function decode(h) {
    var code = 0, first = 0, index = 0, len, c;
    for (len = 1; len < 16; len++) {
      code |= bits(1);
      c = h.count[len];
      if (code - c < first) return h.sym[index + (code - first)];
      index += c; first += c; first <<= 1; code <<= 1;
    }
    throw new Error('i18n: bad code');
  }
  function codes(lc, dc) {
    var sym, len, dist, i;
    for (;;) {
      sym = decode(lc);
      if (sym < 256) { out.push(sym); continue; }
      if (sym === 256) return;
      sym -= 257;
      if (sym >= 29) throw new Error('i18n: bad length');
      len = SZ_LBASE[sym] + bits(SZ_LEXT[sym]);
      sym = decode(dc);
      if (sym >= 30) throw new Error('i18n: bad distance');
      dist = SZ_DBASE[sym] + bits(SZ_DEXT[sym]);
      if (dist > out.length) throw new Error('i18n: distance too far');
      for (i = 0; i < len; i++) out.push(out[out.length - dist]);
    }
  }
  var last, type, i, n, lengths, lc, dc, nlen, ndist, ncode, sym, rep, prev;
  do {
    last = bits(1);
    type = bits(2);
    if (type === 0) {
      bitbuf = 0; bitcnt = 0;
      if (pos + 4 > src.length) throw new Error('i18n: truncated stream');
      n = src[pos] | (src[pos + 1] << 8);
      if (src[pos + 2] !== (~n & 255) || src[pos + 3] !== ((~n >> 8) & 255)) throw new Error('i18n: bad stored block');
      pos += 4;
      if (pos + n > src.length) throw new Error('i18n: truncated stream');
      for (i = 0; i < n; i++) out.push(src[pos++]);
    } else if (type === 1) {
      lengths = [];
      for (i = 0; i < 288; i++) lengths[i] = i < 144 ? 8 : i < 256 ? 9 : i < 280 ? 7 : 8;
      for (i = 0; i < 30; i++) lengths[288 + i] = 5;
      codes(huff(lengths, 0, 288), huff(lengths, 288, 30));
    } else if (type === 2) {
      nlen = bits(5) + 257; ndist = bits(5) + 1; ncode = bits(4) + 4;
      if (nlen > 286 || ndist > 30) throw new Error('i18n: bad counts');
      lengths = [];
      for (i = 0; i < 19; i++) lengths[i] = 0;
      for (i = 0; i < ncode; i++) lengths[SZ_CLORDER[i]] = bits(3);
      lc = huff(lengths, 0, 19);
      lengths = [];
      i = 0;
      while (i < nlen + ndist) {
        sym = decode(lc);
        if (sym < 16) { lengths[i++] = sym; continue; }
        prev = 0;
        if (sym === 16) {
          if (i === 0) throw new Error('i18n: repeat with no length');
          prev = lengths[i - 1]; rep = 3 + bits(2);
        } else if (sym === 17) rep = 3 + bits(3);
        else rep = 11 + bits(7);
        if (i + rep > nlen + ndist) throw new Error('i18n: too many lengths');
        while (rep--) lengths[i++] = prev;
      }
      if (!lengths[256]) throw new Error('i18n: no end-of-block code');
      lc = huff(lengths, 0, nlen);
      dc = huff(lengths, nlen, ndist);
      codes(lc, dc);
    } else throw new Error('i18n: bad block type');
  } while (!last);
  return out;
}
// UTF-8 bytes to a string (TextDecoder is not ES5).
function szUtf8(b) {
  var s = '', parts = [], i = 0, c, cp;
  while (i < b.length) {
    c = b[i++];
    if (c < 128) cp = c;
    else if (c >= 240) { cp = ((c & 7) << 18) | ((b[i] & 63) << 12) | ((b[i + 1] & 63) << 6) | (b[i + 2] & 63); i += 3; }
    else if (c >= 224) { cp = ((c & 15) << 12) | ((b[i] & 63) << 6) | (b[i + 1] & 63); i += 2; }
    else { cp = ((c & 31) << 6) | (b[i] & 63); i += 1; }
    if (cp > 0xffff) { cp -= 0x10000; s += String.fromCharCode(0xd800 + (cp >> 10), 0xdc00 + (cp & 1023)); }
    else s += String.fromCharCode(cp);
    if (s.length > 4096) { parts.push(s); s = ''; }
  }
  parts.push(s);
  return parts.join('');
}
function szUnpack(s) {
  return JSON.parse(szUtf8(szInflate(szB64Bytes(String(s)))));
}
// One language's table from the inlined block, as the lookups read it:
// { key: text, …, type: { osm_key: text }, _machine: … }; null when the
// block has no such language or does not decode.
function szTableFrom(block, lang) {
  if (!block || block.v !== 2 || !block.t || !Object.prototype.hasOwnProperty.call(block.t, lang)) return null;
  var keys = szUnpack(block.k), texts = szUnpack(block.t[lang]);
  if (!keys || !texts || keys.length !== texts.length) return null;
  var t = {}, types = null, meta = (block.m && block.m[lang]) || {}, i, k;
  for (k in meta) if (Object.prototype.hasOwnProperty.call(meta, k)) t[k] = meta[k];
  for (i = 0; i < keys.length; i++) {
    if (texts[i] === 0) continue;
    if (keys[i].indexOf('type.') === 0) (types = types || {})[keys[i].slice(5)] = texts[i];
    else t[keys[i]] = texts[i];
  }
  if (types) t.type = types;
  return t;
}
// The UI languages this page has a table for (besides English).
function szUiLanguages() {
  try {
    var el = document.getElementById('sz-i18n');
    var block = el ? JSON.parse(el.textContent) : null;
    return block && block.t ? Object.keys(block.t).sort() : [];
  } catch (e) { return []; }
}

// Pick the UI language once map-config.json has loaded: its `language`
// (create_osm_zim --language; a merged map's language), unless the page
// URL says ?uilang=xx. A language without a table is English.
function szSetUiLanguage(lang, search) {
  var m = /[?&]uilang=([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)?)/.exec(String(search || ''));
  if (m) lang = m[1];
  var base = String(lang || 'en').toLowerCase().split('-')[0];
  SZ_I18N_PSEUDO = base === 'qps';
  SZ_I18N = null;
  if (base !== 'en' && !SZ_I18N_PSEUDO) {
    try {
      var el = document.getElementById('sz-i18n');
      SZ_I18N = el ? szTableFrom(JSON.parse(el.textContent), base) : null;
    } catch (e) { SZ_I18N = null; }
  }
  SZ_UI_LANG = SZ_I18N ? base : 'en';
  SZ_UI_RTL = !!(SZ_I18N && SZ_RTL_LANGS[SZ_UI_LANG] === 1);
  // A table's _nu ("latn") picks the digits numbers are written in.
  SZ_NUM_LANG = SZ_I18N && typeof SZ_I18N._nu === 'string' ? SZ_UI_LANG + '-u-nu-' + SZ_I18N._nu : SZ_UI_LANG;
  // <html lang> is the table's _lang when it gives one (zh-Hans: the
  // device then draws Han characters with its Simplified Chinese font).
  var tag = SZ_I18N && typeof SZ_I18N._lang === 'string' ? SZ_I18N._lang : SZ_UI_LANG;
  try {
    var root = document.documentElement;
    if (SZ_I18N || SZ_I18N_PSEUDO) root.lang = tag;
    if (SZ_UI_RTL) root.dir = 'rtl';
    else if (root.dir === 'rtl') root.dir = 'ltr';
  } catch (e) {}
  return SZ_UI_LANG;
}

// A number, distance, coordinate or phone number set in right-to-left
// text: an isolated left-to-right run (U+2066 ... U+2069), so "3.2 km" does
// not show as "km 3.2" and "37.97, 23.73" keeps its order. A left-to-right
// UI gets the text unchanged.
function szLtr(s) {
  return SZ_UI_RTL && s !== '' && s != null ? '\u2066' + s + '\u2069' : s;
}

// Is the UI in a machine-translated table (its "_machine": true)? Then
// elements marked data-i18n-machine (the "translated by machine" note in
// About and on the Find page) are shown; otherwise they stay hidden.
function szMachineTranslated() {
  return !!(SZ_I18N && SZ_I18N._machine === true);
}

// '{name} is closed' + { name: 'Zoo' }. An unknown {x} is left as is.
// In a right-to-left UI each value is a first-strong isolate (U+2068 ...
// U+2069): a Latin name, a number or "25 de Mayo" keeps its own order
// inside the Arabic or Hebrew sentence instead of being reordered with it.
function szFill(s, vars) {
  if (!vars) return s;
  return String(s).replace(/\{(\w+)\}/g, function(all, k) {
    if (!Object.prototype.hasOwnProperty.call(vars, k) || vars[k] == null) return all;
    var v = String(vars[k]);
    return SZ_UI_RTL && v !== '' ? '\u2068' + v + '\u2069' : v;
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
      return new Intl.NumberFormat(SZ_NUM_LANG, { minimumFractionDigits: d,
        maximumFractionDigits: d, useGrouping: false }).format(x);
    } catch (e) {}
  }
  return x.toFixed(d);
}
function szLocaleNum(x) {
  x = Number(x);
  if (SZ_I18N) {
    try { return x.toLocaleString(SZ_NUM_LANG); } catch (e) {}
  }
  return x.toLocaleString();
}
// "July 2026" for (2026, 6).
var SZ_MONTHS_EN = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                    'August', 'September', 'October', 'November', 'December'];
function szMonthYear(year, month0) {
  if (SZ_I18N) {
    try {
      return new Intl.DateTimeFormat(SZ_NUM_LANG, { month: 'long', year: 'numeric', timeZone: 'UTC' })
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
// all visible markup text is one of the three.) An element that also has
// data-i18n-machine starts hidden (style="display:none") and is shown only
// in a machine-translated table (szMachineTranslated).
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
    if (el.getAttribute('data-i18n-machine') != null && el.style) {
      el.style.display = szMachineTranslated() ? '' : 'none';
    }
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
// A name as the pages show it: the part before its first ";". OSM puts
// variants in one value ("Hang Ah Alley;Pagoda Alley", "洛杉矶;洛杉磯"); the
// map's labels (130, _szFirstName), popups, search rows, Find cards, the
// place sheet and the Find page show the first. A value that starts with
// ";" is kept whole. Lookups and search matching keep the record's value.
function szFirstName(s) {
  if (typeof s !== 'string') return s;
  var i = s.indexOf(';');
  return i > 0 ? s.slice(0, i) : s;
}

// Japanese, Korean and Chinese count large numbers in ten thousands (万 / 億,
// 만 / 억, 万 / 亿), which num.* (thousands, millions) cannot say. Their
// tables set "_compact": "intl": 10,000 and up then go through Intl's
// compact notation in the UI language ("1.2万", "560만", "11亿"); smaller
// numbers are written out ("1,234"), and so is any number where Intl has no
// compact notation. '' for every other table: the caller's own form.
function szCompactIntl(n) {
  if (!SZ_I18N || SZ_I18N._compact !== 'intl') return '';
  if (n >= 1e4) {
    try {
      var f = new Intl.NumberFormat(SZ_NUM_LANG, { notation: 'compact', maximumFractionDigits: 1 });
      if (f.resolvedOptions().notation === 'compact') return f.format(n);
    } catch (e) {}
  }
  return szLocaleNum(n);
}
function szCompactNum(n) {
  var c = szCompactIntl(n);
  if (c) return c;
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
