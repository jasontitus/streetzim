// --- Search with a photo: the sign reader (docs/ocr.md) ---
// A camera button in the search box when the builder added the sign reader
// (--ocr; map-config's `ocr` names ocr/manifest.json). A photo -- a street
// sign, a shop front, a landmark's name -- is read in a worker
// (ocr/ocr-worker.js: PaddleOCR models on ONNX Runtime's WebAssembly), its
// text lines are searched for (map._searchSign, 300-search.js), and the
// lines are listed under the photo to search for one by hand. Older ZIMs
// lack the key (in-place patching cannot add the files): no button.
// Picking a place hides the sheet but keeps the photo: the camera button
// shows it, and a tap goes back to it, its lines and its places (the sheet's
// own camera takes another; its x lets it go). The search field holds the
// line the first place matched. A photo can also be dropped on the page or
// pasted. The camera button opens the choice _szSignPickMode gives: on a
// phone or tablet, Take a photo (the camera at once) or Choose a photo; in
// Kiwix on iOS Take a photo is off and the library comes first; in Kiwix
// on macOS a note to drop or paste; on a desktop, the file picker at once.
// Nothing is fetched before the first photo;
// the worker, a few hundred MB while it reads, is let go a minute after
// the last one.

// BEGIN sign-lines
// The detector's boxes ({text, box: {x, y, width, height}, script}) as text
// lines in reading order: boxes of comparable height (each at least 0.4 of
// the tallest box already in the line: small print inside a big line's
// band, a watermark, a screw read as a letter, is a line of its own) that
// overlap vertically by half the smaller one's height are one line, joined left to right (right to left
// for Arabic), with no space between Chinese, Japanese or Thai words. A
// box of two or more letters at least 1.5 times as tall as wide is a
// vertical line of its own (Chinese and Japanese signs: 安平區 down the
// side of a house-number plate), never joined to the lines beside it.
// Each line's size is its letters' height (a vertical line's width).
var SZ_SIGN_RTL = { Arab: 1, Hebr: 1 };
var SZ_SIGN_NOSPACE = /[฀-๿぀-ヿ㐀-䶿一-鿿豈-﫿]/;
// A box the recognizers read in parts: the Latin model reads the digits of
// "2912 طريق" and garbles the Arabic, the Arabic model reads طريق and
// garbles the digits (a Bahraini address plate, 2026-10-29). When the most
// confident reading has digits and no letters, and another (at least half
// as sure) has letters, the box reads as that one's words and this one's
// number: "طريق 2912", as the map's records name the road; when that
// reading already holds the number ("23號", "42nd St"), it is taken as it
// is. Arabic-Indic and Persian digits read as 0-9.
function _szSignDigits(s) {
  return String(s).replace(/[\u0660-\u0669]/g, function (d) { return String.fromCharCode(d.charCodeAt(0) - 0x0630); })
                  .replace(/[\u06F0-\u06F9]/g, function (d) { return String.fromCharCode(d.charCodeAt(0) - 0x06C0); });
}
var SZ_SIGN_LETTER = /\p{L}/u;
function _szSignCompose(it) {
  var text = _szSignDigits(it.text).trim();
  if (!SZ_SIGN_LETTER.test(text) && /\d/.test(text)) {
    var words = null;
    (it.readings || []).forEach(function (r) {
      if (r && r.confidence >= 0.5 * it.confidence && SZ_SIGN_LETTER.test(r.text) && (!words || r.confidence > words.confidence)) words = r;
    });
    var whole = words ? _szSignDigits(words.text).trim() : '';
    if (whole && whole.replace(/\s+/g, '').indexOf(text.replace(/\s+/g, '')) >= 0) return { text: whole, script: words.script };
    var w = whole.replace(/[\d\s.,:;'"()\-\u2013\u2014_\/\\]+/g, ' ').trim();
    if (w) return { text: w + ' ' + text, script: words.script };
  }
  return { text: text, script: it.script };
}
function _szSignVertical(it) {
  return it.box.height >= 1.5 * it.box.width && Array.from(String(it.text).trim()).length >= 2;
}
function _szSignLineGroups(items) {
  var boxes = (items || []).filter(function (it) {
    return it && it.text && it.box && it.box.height > 0;
  }).map(function (it) {
    var c = _szSignCompose(it);
    return { text: c.text, box: it.box, script: c.script, confidence: it.confidence };
  }).filter(function (it) { return it.text; }).sort(function (a, b) {
    return (a.box.y + a.box.height / 2) - (b.box.y + b.box.height / 2);
  });
  var lines = [];
  boxes.forEach(function (it) {
    var b = it.box, line = null, vertical = _szSignVertical(it);
    for (var i = 0; i < lines.length && !line && !vertical; i++) {
      var L = lines[i];
      if (L.vertical || b.height < 0.4 * L.tallest || L.tallest < 0.4 * b.height) continue;
      var overlap = Math.min(L.y + L.height, b.y + b.height) - Math.max(L.y, b.y);
      if (overlap >= 0.5 * Math.min(L.height, b.height)) line = L;
    }
    if (!line) {
      line = { x: b.x, y: b.y, width: b.width, height: b.height, items: [], vertical: vertical, tallest: b.height };
      lines.push(line);
    } else {
      var x2 = Math.max(line.x + line.width, b.x + b.width), y2 = Math.max(line.y + line.height, b.y + b.height);
      line.x = Math.min(line.x, b.x); line.y = Math.min(line.y, b.y);
      line.width = x2 - line.x; line.height = y2 - line.y; line.tallest = Math.max(line.tallest, b.height);
    }
    line.items.push(it);
  });
  return lines.sort(function (a, b) { return a.y - b.y; }).map(function (L) {
    var rtl = L.items.filter(function (it) { return SZ_SIGN_RTL[it.script]; }).length * 2 > L.items.length;
    L.items.sort(function (a, b) { return rtl ? b.box.x - a.box.x : a.box.x - b.box.x; });
    var text = '';
    L.items.forEach(function (it, i) {
      var t = String(it.text).trim();
      if (i && !(SZ_SIGN_NOSPACE.test(text.slice(-1)) && SZ_SIGN_NOSPACE.test(t.charAt(0)))) text += ' ';
      text += t;
    });
    return { text: text, box: { x: L.x, y: L.y, width: L.width, height: L.height }, rtl: rtl,
             size: L.vertical ? L.width : L.height };
  });
}

// The boxes of the photo's second, coarse reading (readBoth) that add to
// the fine one's: scaled by k back to the photo, only where the fine
// reading found nothing (a box overlapping a fine one by 30% of the smaller
// of the two is the same text, read again, perhaps spelt otherwise).
function _szSignMerge(fine, coarse, k) {
  var overlaps = function (a, b) {
    var w = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
    var h = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
    return w > 0 && h > 0 && w * h >= 0.3 * Math.min(a.width * a.height, b.width * b.height);
  };
  var out = (fine || []).slice();
  (coarse || []).forEach(function (it) {
    if (!it || !it.box) return;
    var c = { text: it.text, confidence: it.confidence, script: it.script, readings: it.readings,
              box: { x: it.box.x * k, y: it.box.y * k, width: it.box.width * k, height: it.box.height * k } };
    if (!(fine || []).some(function (f) { return f && f.box && overlaps(f.box, c.box); })) out.push(c);
  });
  return out;
}

// A building number on its own line (no letters) beside a street that has
// a number (letters and digits: "طريق 2912"), as on a Bahraini address
// plate: the two as one more line, which an address record of that
// building names ("Building 460 - مبنى 460 طريق 2912 - Road 2912"), weighed
// as the street's line. A number alone matches no record (map._searchSign). The building
// number goes beside the street's own number, never beside its word: the
// street as read left to right ("116 طريق") followed by "366" would hold
// "طريق 366", another road, read exactly.
function _szSignAddress(lines, tallest) {
  var nums = lines.filter(function (L) { return /^\d+[A-Za-z]?$/.test(L.text); });
  var streets = lines.filter(function (L) { return SZ_SIGN_LETTER.test(L.text) && /\d/.test(L.text); });
  var out = [];
  nums.forEach(function (N) {
    streets.forEach(function (S) {
      var text = /^\d/.test(S.text.trim()) ? N.text + ' ' + S.text : S.text + ' ' + N.text;
      out.push({ text: text, weight: 0.5 + 0.5 * S.size / (tallest || S.size) });
    });
  });
  return out.slice(0, 4);
}

// What each result of a photo is, said plainly: a street of the sign's
// name is not the address on it, a town is not a spot, and a name that is
// only part of a line ("Grand Mosque" on "Al Noor Grand Mosque") is not
// the sign's. Someone who cannot read the sign must not be handed one as
// the other, so the labels compare whole words in order (`words`:
// map._searchSign's _szSignWords) with the lines as read, never the
// search's fuzzy match. docs/ocr.md, "What a result is".
// Street types, abbreviations and translations as one word: "Abbott Dr" is
// "Abbott Drive", never "Abbott Avenue"; "طريق 105" is "Road 105" and
// "ممر 366" "Lane 366" (Bahrain's bilingual names). شارع stays itself: it is
// Bahrain's "Avenue" and elsewhere a street.
var SZ_SIGN_TYPES = { road: 'road', rd: 'road', street: 'street', st: 'street', avenue: 'avenue', ave: 'avenue',
                      av: 'avenue', lane: 'lane', ln: 'lane', drive: 'drive', dr: 'drive', boulevard: 'boulevard',
                      blvd: 'boulevard', way: 'way', 'طريق': 'road', 'ممر': 'lane', 'شارع': 'شارع', 'جادة': 'جادة' };
var SZ_SIGN_FILLER = ['building', 'bldg', 'bldng', 'bld', 'no', 'house', 'مبنى', 'منزل'];
var SZ_SIGN_SUFFIX = ['號', '号', '番地', '번지'];
// Words of a speed sign: a number beside them is a limit, never a building.
var SZ_SIGN_SPEED = ['speed', 'limit', 'mph', 'kmh', 'km', 'zone', 'tempo', 'سرعة', '限速', '速度'];
function _szSignIsNum(w) { return /^\d+$/.test(w); }
function _szSignSame(a, b) { return a.length === b.length && a.join(' ') === b.join(' '); }
// A line read in either direction (right-to-left text may come in visual order).
function _szSignSameLine(line, ws) { return _szSignSame(line, ws) || _szSignSame(line.slice().reverse(), ws); }
// The sign's lines as words (street types as their word); its building
// number: the tallest line that is a number (with a letter or 號 at most:
// "366", "12a", "23號"); and `claim`, that number when it belongs with a
// street: with 號, or on one plate with a street line that has a type word
// or a number ("366" over "Road 116", "1121" over "Abbott Drive"): their
// boxes stacked, no other line between them. A number elsewhere in the
// photo, or on a sign with "speed", "limit", "km"..., may be a speed limit
// ("40" beside "Road 1601", "25" under "SPEED LIMIT").
function _szSignPlate(lines, words) {
  var ls = (lines || []).map(function (L) { return L && typeof L === 'object' ? L : { text: L }; });
  var fold = function (list) { return [].concat.apply([], list.map(words)); };
  var types = {};
  Object.keys(SZ_SIGN_TYPES).forEach(function (k) { types[words(k)[0]] = words(SZ_SIGN_TYPES[k])[0]; });
  var plate = { types: types, filler: fold(SZ_SIGN_FILLER), suffix: fold(SZ_SIGN_SUFFIX) };
  plate.norm = function (ws) { return ws.map(function (w) { return types[w] || w; }); };
  plate.isType = function (w) { return Object.prototype.hasOwnProperty.call(types, w); };
  var building = function (ws) {
    return _szSignIsNum(ws[0] || '') && (ws.length === 1 ||
      (ws.length === 2 && (/^[a-z]$/.test(ws[1]) || plate.suffix.indexOf(ws[1]) >= 0)));
  };
  var rows = ls.map(function (L) {
    return { ws: plate.norm(words(L.text)), size: L.size || 0, box: L.box, text: String(L.text).trim() };
  });
  plate.lines = rows.map(function (r) { return r.ws; });
  plate.texts = rows.map(function (r) { return r.text; });
  var top = rows.filter(function (r) { return building(r.ws); }).sort(function (x, y) { return y.size - x.size; })[0];
  plate.building = top ? top.ws : null;
  var speed = fold(SZ_SIGN_SPEED);
  var onePlate = function (r) {
    var A = top && top.box, B = r.box;
    if (!A || !B) return false;
    var over = Math.min(A.x + A.width, B.x + B.width) - Math.max(A.x, B.x);
    var gap = Math.max(A.y, B.y) - Math.min(A.y + A.height, B.y + B.height);
    if (!(over >= 0.5 * Math.min(A.width, B.width) && gap < 1.5 * Math.max(A.height, B.height))) return false;
    var lo = Math.min(A.y + A.height, B.y + B.height), hi = Math.max(A.y, B.y);
    return !rows.some(function (o) {          // nothing between them ("SPEED", "LIMIT")
      var C = o.box;
      return o !== top && o !== r && C && C.y + C.height / 2 > lo && C.y + C.height / 2 < hi &&
        Math.min(C.x + C.width, A.x + A.width) - Math.max(C.x, A.x) > 0;
    });
  };
  var street = function (r) { return r !== top && r.ws.some(plate.isType); };
  var onSpeedSign = rows.some(function (r) { return r.ws.some(function (w) { return speed.indexOf(w) >= 0; }); });
  var claims = top && !onSpeedSign && (top.ws.length === 2 && plate.suffix.indexOf(top.ws[1]) >= 0 ||
    rows.some(function (r) { return (street(r) || r !== top && r.ws.some(_szSignIsNum) && r.ws.some(function (w) { return !_szSignIsNum(w); })) && onePlate(r); }));
  plate.claim = claims ? top.text : '';
  return plate;
}
// An address record's words: its building number first, filler words
// before it aside ("Bldng 100 Road 3901, Riffa"), as the build names an
// address ("366 116", "23號 忠孝東路三段", "10 Schulweg, Husum"; the place
// after a comma left out), and the rest, street types as their word.
function _szSignAddressParts(rec, plate, words) {
  var ws = plate.norm(words(String(rec.n || '').split(',')[0]));
  while (ws.length && plate.filler.indexOf(ws[0]) >= 0) ws = ws.slice(1);
  if (!_szSignIsNum(ws[0] || '')) return null;
  // A letter after the number is the building's ("12a Main St") only when
  // the rest still names a street ("1018 B St" is on B Street).
  var named = function (r) { return r.some(function (w) { return !_szSignIsNum(w) && !plate.isType(w); }); };
  var n = ws[1] && (plate.suffix.indexOf(ws[1]) >= 0 || /^[a-z]$/.test(ws[1]) && named(ws.slice(2))) ? 2 : 1;
  var rest = ws.slice(n).filter(function (w) { return plate.filler.indexOf(w) < 0; });
  return rest.length ? { b: ws.slice(0, n), rest: rest } : null;
}
// The record's street (`rest`) and a street line of the sign, word for word:
// the same words and types; a record naming the street without a type
// ("366 116") matches the line without its type word ("Road 116"). A
// record with a type word never matches a line stripped of its own.
function _szSignStreetIs(rest, line, plate) {
  if (_szSignSameLine(line, rest)) return true;
  var bare = line.filter(function (w) { return !plate.isType(w); });
  return bare.length > 0 && _szSignSameLine(bare, rest);
}
// What an address record is to the sign, given its street lines (`streets`:
// the lines that are a street of the map), word for word and in order:
//   'address'  its building number is the sign's (`plate.building`) and it
//              may be named (`plate.claim`), and its street is one of
//              `streets`; or one line holds its street and number, either
//              way round, with a name in the street part ("Schulweg 10",
//              "三和路四段20巷101號"; not "1225 Road 1029");
//   'similar'  its number and street are on the sign but the number may not
//              be the building's (a "30" under "Schulweg"): listed, to check;
//   'drop'     anything else: not listed.
// With `streets` null (while the search runs): whether it may be, cheaply.
function _szSignAddressKind(rec, plate, words, streets) {
  if (!rec || rec.t !== 'addr') return 'drop';
  var p = _szSignAddressParts(rec, plate, words);
  if (!p) return 'drop';
  var named = p.rest.some(function (w) { return !_szSignIsNum(w) && !plate.isType(w); });
  if (named && plate.lines.some(function (L) {
    return _szSignSameLine(L, p.rest.concat(p.b)) || _szSignSameLine(L, p.b.concat(p.rest));
  })) return 'address';
  if (!plate.building || !_szSignSame(plate.building, p.b)) return 'drop';
  // A street line, or one with the line under it (Taipei's "忠孝東路三段"
  // over "217巷7弄"), but not the building's line.
  var cands = [];
  (streets || plate.lines).forEach(function (S) {
    cands.push(S);
    var i = plate.lines.indexOf(S), next = plate.lines[i + 1];
    if (i >= 0 && next && next !== plate.building && !_szSignSame(next, plate.building)) cands.push(S.concat(next));
  });
  var on = cands.some(function (S) { return _szSignStreetIs(p.rest, S, plate); });
  if (!on) return 'drop';
  return plate.claim ? 'address' : 'similar';
}
// Whether an address may be the sign's, before its words are taken (a big
// map's leaves hold millions, Germany's "10" ones 1.8 million): before its
// comma, its first number is the sign's building number or on a line with
// a name, and it holds a word of the sign (as read or folded), or only
// numbers on the sign, street types and filler.
function _szSignAsciiDigits(d) {
  if (/^[0-9]+$/.test(d)) return d;
  return d.replace(/[０-９٠-٩۰-۹]/g, function (c) {
    var k = c.charCodeAt(0);
    return String(k - (k >= 0xFF10 ? 0xFF10 : k >= 0x06F0 ? 0x06F0 : 0x0660));
  });
}
function _szSignMayBeAddress(rec, plate, words) {
  var q = plate.quick;
  if (!q) {
    var nums = [], toks = [], plain = [];
    plate.lines.forEach(function (L, i) {
      var hasName = L.some(function (w) { return !_szSignIsNum(w) && !plate.isType(w); });
      L.forEach(function (w) {
        if (_szSignIsNum(w) && (hasName || (plate.building && plate.building[0] === w))) nums.push(w);
        else if (!_szSignIsNum(w) && !plate.isType(w) && w.length > 1) toks.push(w);
      });
      String(plate.texts[i] || '').toLowerCase().split(' ').forEach(function (w) {
        if (w.length > 1 && !/^\d+$/.test(w) && !plate.isType(words(w)[0] || '')) toks.push(w);
      });
    });
    Object.keys(SZ_SIGN_TYPES).concat(SZ_SIGN_FILLER, ['طریق', 'مبنی']).forEach(function (w) {
      plain.push(w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    });
    var all = [];
    plate.lines.forEach(function (L) { L.forEach(function (w) { if (_szSignIsNum(w)) all.push(w); }); });
    q = plate.quick = { nums: nums, toks: toks, all: all,
                        plain: new RegExp(plain.sort(function (x, y) { return y.length - x.length; }).join('|'), 'g') };
  }
  var n = String(rec.n || ''), comma = n.indexOf(',');
  if (comma >= 0) n = n.slice(0, comma);
  var first = /[0-9０-９٠-٩۰-۹]+/.exec(n);
  if (!first || q.nums.indexOf(_szSignAsciiDigits(first[0])) < 0) return false;
  var low = n.toLowerCase();
  for (var i = 0; i < q.toks.length; i++) if (low.indexOf(q.toks[i]) >= 0) return true;
  if (/\p{L}/u.test(low.replace(q.plain, ' ').replace(/(^|\s)[a-z](?=\s|$)/g, ' '))) return false;
  return n.match(/[0-9０-９٠-٩۰-۹]+/g).every(function (d) { return q.all.indexOf(_szSignAsciiDigits(d)) >= 0; });
}
// A record's names: its name, its own-script name, its other languages'.
function _szSignNames(it) {
  var out = [];
  [it.n, it.nn].concat(String(it.n || '').split(';'),
                       (typeof _szLnPairs === 'function' ? _szLnPairs(it) : []).map(function (x) { return x[0]; }))
    .forEach(function (n) { if (n && out.indexOf(n) < 0) out.push(n); });
  return out;
}
// The sign's lines that are a street the search found (a "Block 727" line is no street).
function _szSignStreets(plate, words, records) {
  return plate.lines.filter(function (L) {
    return records.some(function (it) {
      return it && it.t === 'street' && _szSignNames(it).some(function (n) { return _szSignSameLine(L, plate.norm(words(n))); });
    });
  });
}
// A name too short to be a sign's without being read exactly: "IG" in
// "IGHTHAM", "aa", "10", "注意" (3 letters, 2 for Chinese, Japanese or
// Korean): a near miss of it is noise, not a place to check.
function _szSignTooShort(name, words) {
  var t = words(name).join('');
  return Array.from(t).length < (/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(t) ? 3 : 4);
}
// Each match's kind, for its label:
//   'address', 'drop', or 'similar' for an address: _szSignAddressKind;
//   'street'   a street whose name is one line of the sign, in order:
//              `building` the plate's number when it may be named and its
//              address was not found; `plate` when the sign has a number
//              line at all ("Street only, not the address");
//   'area'     likewise a place or an administrative area (a town);
//   'name'     likewise anything else (a shop, a station);
//   'similar'  everything else: read inexactly, part of a line, glued from
//              two lines ("Road 1012" from "Road 101" and "2"); 'drop' when
//              its name is too short to mean anything (_szSignTooShort).
// `several` on the first, and on every address when the first is one, when
// another record of its name (`all`, every record found; for an address,
// the same address before its place) is more than 150 m away, or 2 km for
// a street: the sign alone does not say which. No "the only one":
// a name found once in the map is not the sign's place when the sign's own
// is missing from the map or the reader misread it (a kebab shop 471 km
// away, a road one character off), so the verdict says where it is instead.
function _szSignKinds(matches, lines, words, all) {
  var plate = _szSignPlate(lines, words);
  var records = all || matches.map(function (m) { return m.item; });
  var streets = _szSignStreets(plate, words, records);
  var lineOf = function (it) {
    return _szSignNames(it).some(function (n) {
      var ws = plate.norm(words(n));
      return plate.lines.some(function (L) { return L.some(function (w) { return !_szSignIsNum(w); }) && _szSignSameLine(L, ws); });
    });
  };
  var addrKind = matches.map(function (m) { return m.item.t === 'addr' ? _szSignAddressKind(m.item, plate, words, streets) : ''; });
  var found = addrKind.indexOf('address') >= 0;
  var out = matches.map(function (m, i) {
    var t = m.item.t;
    if (t === 'addr') return { kind: addrKind[i] };
    if (!lineOf(m.item)) return { kind: _szSignTooShort(m.item.n || '', words) ? 'drop' : 'similar' };
    if (t === 'street') {
      return { kind: 'street', building: found ? '' : plate.claim, plate: !!plate.building };
    }
    if (t === 'place' || t === 'admin') return { kind: 'area' };
    // A shop or the like of 3 letters or fewer ("Rng") is noise even read exactly.
    return { kind: _szSignTooShort(m.item.n || '', words) ? 'drop' : 'name' };
  });
  var first = out.filter(function (k) { return k.kind !== 'drop'; })[0];
  if (first) {
    var top = matches[out.indexOf(first)].item, rad = Math.PI / 180;
    var km = top.t === 'street' ? 2 : 0.15;
    var key = function (it) {
      if (it.t === 'addr') return [words(String(it.n || '').split(',')[0]).join(' ')];
      return _szSignNames(it).map(function (n) { return words(n).join(' '); });
    };
    var topKeys = key(top);
    first.several = records.some(function (it) {
      if (!it || it === top || (it.t === 'addr') !== (top.t === 'addr')) return false;
      if (top.t === 'addr' && _szSignAddressKind(it, plate, words, streets) === 'drop') return false;
      if (!key(it).some(function (k) { return topKeys.indexOf(k) >= 0; })) return false;
      var dlng = it.o - top.o;
      dlng -= 360 * Math.round(dlng / 360);
      var dy = (it.a - top.a) * 111.2, dx = dlng * 111.2 * Math.cos((top.a || 0) * rad);
      return Math.sqrt(dx * dx + dy * dy) > km;
    });
    if (first.several && top.t === 'addr') out.forEach(function (k) { if (k.kind === 'address') k.several = true; });
  }
  return out;
}
// END sign-lines

// BEGIN sign-env
// Kiwix's Apple apps serve the in-ZIM viewer on their zim: scheme, and
// neither opens the file picker as a browser does (kiwix-apple, 2026-10):
// - 'ios' (an iPhone or iPad, as 000-head.html tells it): the system
//   picker opens, but the app has no camera permission (no
//   NSCameraUsageDescription) and iOS closes an app that opens the camera
//   without one, so its Take Photo crashes Kiwix: the reader is pointed at
//   the photo library first.
// - 'mac': the app does not answer the web view's open-panel request
//   (WKUIDelegate runOpenPanelWith), so the picker never opens: the reader
//   is told to drop or paste a photo instead.
// '' anywhere else.
function _szSignKiwixApp(protocol, nav) {
  if (protocol !== 'zim:' || !nav) return '';
  var ua = nav.userAgent || '';
  if (/iPad|iPhone|iPod/.test(ua) || (nav.platform === 'MacIntel' && nav.maxTouchPoints > 1)) return 'ios';
  if (/Macintosh|Mac OS X/.test(ua)) return 'mac';
  return '';
}
// END sign-env

// BEGIN sign-pick
// What the camera button offers, from sign-env's app and whether the
// pointer is coarse (a finger): 'drop' in Kiwix for macOS, where no picker
// opens; 'library' in Kiwix on iOS, where Take Photo closes the app (the
// camera tile shows, off); 'both' on a phone or tablet, the camera at once
// or a photo; 'picker' on a desktop, the file picker straight away.
function _szSignPickMode(app, coarse) {
  if (app === 'mac') return 'drop';
  if (app === 'ios') return 'library';
  return coarse ? 'both' : 'picker';
}
// END sign-pick

// BEGIN sign-load
// The reader's files, one at a time: each job (a function returning a
// promise) starts when the one before has ended; the results in order.
// kiwix-serve 3.8.2 sometimes holds a download for ~100 s when the seven
// files (46 MB for Manama) are asked for at once beside the map's tiles: 6
// of 18 first photos waited 90-115 s (CI's 180 s budget ran out twice),
// none of 20 in turn, and a plain HTTP server never did. In turn costs
// nothing measurable (12-14 s for the whole first photo either way).
function _szInTurn(jobs) {
  var out = [];
  return jobs.reduce(function (prev, job) {
    return prev.then(job).then(function (v) { out.push(v); });
  }, Promise.resolve()).then(function () { return out; });
}
// END sign-load

function initSignSearch(map, config) {
  var path = config && config.ocr;
  var btn = document.getElementById('search-photo');
  var file = document.getElementById('search-photo-file');
  var sheet = document.getElementById('ocr-sheet');
  if (!btn || !file || !sheet || typeof path !== 'string' || /^[a-z]+:|^\/|\.\./i.test(path)) return false;
  if (typeof Worker !== 'function' || typeof WebAssembly !== 'object' ||
      typeof map._searchSign !== 'function') return false;
  // An Android app's WebView opens no file chooser unless the app wires one
  // (Kiwix for Android does not): a button that does nothing is worse.
  if (/; wv\)/.test(navigator.userAgent || '')) return false;
  var input = document.getElementById('search-input');
  var resultsEl = document.getElementById('search-results');
  var statusEl = document.getElementById('ocr-status');
  var canvas = document.getElementById('ocr-canvas');
  var linesEl = document.getElementById('ocr-lines');
  var verdictEl = document.getElementById('ocr-verdict');
  var chooseBtn = document.getElementById('ocr-choose');
  var takeBtn = document.getElementById('ocr-take');
  var takeFile = document.getElementById('search-photo-take');
  var pickRow = document.getElementById('ocr-pick');
  // The sheet's own camera, for another photo: not beside the tiles.
  var againBtn = document.getElementById('ocr-again');
  var MAX_SIDE = 1600;     // the detector works at 960; crops are read sharper
  // At 960 px the detector finds no text at all in letters a sixth of the
  // photo tall or more (the street name on a Tainan house-number plate,
  // 2026-10-29): a second reading of the photo shrunk to COARSE_SIDE, where
  // such letters are the size it finds, and the lines of both are kept.
  var COARSE_SIDE = 384;
  var IDLE_MS = 60000;
  var manifest = null, worker = null, ready = null, idleTimer = null, reqSeq = 0, waiting = {};
  // window.__szSignKiwixApp forces it, for tools/ocr_check.mjs (no zim: there).
  function kiwixApp() { return window.__szSignKiwixApp || _szSignKiwixApp(location.protocol, navigator); }
  // The last photo's search, kept when a result is picked so the reader can
  // go back to it (the camera button shows the photo): {lines, query}, or null.
  var last = null;

  btn.hidden = false;
  document.getElementById('search-container').classList.add('has-photo');

  function getBytes(rel) {
    // Kiwix drops requests under load: the zimtile retry helper, as the RTL
    // plugin is fetched (137).
    var fetcher = window.__szFetchWithRetry;
    if (fetcher && typeof AbortController === 'function') {
      return fetcher(baseUrl + rel, new AbortController().signal).then(function (res) { return res.data; });
    }
    return fetch(baseUrl + rel).then(function (r) {
      if (!r.ok) throw new Error(rel + ': HTTP ' + r.status);
      return r.arrayBuffer();
    });
  }
  function getText(rel) {
    return getBytes(rel).then(function (b) { return new TextDecoder().decode(b); });
  }

  function release() {
    clearTimeout(idleTimer);
    if (worker) { try { worker.terminate(); } catch (e) {} }
    worker = null; ready = null;
    for (var k in waiting) waiting[k].reject(new Error('stopped'));
    waiting = {};
  }

  // The worker, loaded with the manifest's files (fetched here, sent as bytes).
  function start() {
    if (ready) return ready;
    ready = (manifest ? Promise.resolve(manifest) : getText(path).then(function (t) {
      manifest = JSON.parse(t);
      return manifest;
    })).then(function (m) {
      var recs = m.rec || [];
      // the worker: the page's own copy (part 026), else the ZIM's
      var own = document.getElementById('sz-ocr-worker');
      return _szInTurn([function () { return own ? Promise.resolve(own.textContent) : getText(m.worker); },
                        function () { return getBytes(m.engine); },
                        function () { return getBytes(m.wasm); },
                        function () { return getBytes(m.det.model); }]
        .concat(recs.map(function (r) {
          return function () { return _szInTurn([function () { return getBytes(r.model); },
                                                 function () { return getText(r.dict); }]); };
        })))
        .then(function (got) {
          var url = URL.createObjectURL(new Blob([got[0]], { type: 'application/javascript' }));
          worker = new Worker(url);
          URL.revokeObjectURL(url);
          var msg = { type: 'load', engine: got[1], wasm: got[2],
                      det: { model: got[3], preset: m.det.preset, options: m.det.options },
                      rec: recs.map(function (r, i) {
                        return { script: (r.scripts || [r.name])[0], model: got[4 + i][0],
                                 dict: got[4 + i][1], reverse: !!r.reverse };
                      }) };
          var transfer = [msg.engine, msg.wasm, msg.det.model].concat(msg.rec.map(function (r) { return r.model; }));
          return new Promise(function (resolve, reject) {
            worker.onmessage = function (ev) {
              var d = ev.data || {};
              if (d.type === 'loaded') {
                if (d.error) reject(new Error(d.error)); else resolve(worker);
                return;
              }
              var w = waiting[d.id];
              if (!w) return;
              delete waiting[d.id];
              if (d.error) w.reject(new Error(d.error)); else w.resolve(d.lines || []);
            };
            worker.onerror = function (ev) {
              var err = new Error((ev && ev.message) || 'worker error');
              reject(err);
              release();
            };
            worker.postMessage(msg, transfer);
          });
        });
    });
    ready.catch(release);
    return ready;
  }

  function read(image) {
    return start().then(function (w) {
      var id = ++reqSeq;
      return new Promise(function (resolve, reject) {
        waiting[id] = { resolve: resolve, reject: reject };
        w.postMessage({ type: 'read', id: id, image: image }, [image.data.buffer]);
      });
    }).then(function (items) {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(release, IDLE_MS);
      return items;
    });
  }

  // The text shown, and the state for the smoke gates (tools/ocr_check.mjs),
  // which must not read the (translatable) text: reading, searched, none, failed.
  function setStatus(text, state) {
    statusEl.textContent = text || '';
    sheet.setAttribute('data-sz-ocr', state);
  }

  // The sheet hides; the photo, its lines and its search stay for the way
  // back (a result was picked).
  function hide() {
    sheet.hidden = true;
  }
  // The photo is let go: the sheet closes and the camera button is a camera.
  function discard() {
    sheet.hidden = true;
    linesEl.innerHTML = '';
    forgetVerdict();
    forget();
    sheet.setAttribute('data-sz-ocr', '');
  }
  function forget() {
    last = null;
    btn.classList.remove('has-last');
    btn.style.backgroundImage = '';
    btn.title = szT('search.photo', 'Search with a photo');
    btn.setAttribute('aria-label', btn.title);
  }
  // The camera button shows the photo, small: the way back to it.
  function keep(lines, query, raw) {
    last = { lines: lines, query: query, raw: raw };
    try {
      var t = document.createElement('canvas'), side = 96;
      var s = side / Math.min(canvas.width, canvas.height);
      t.width = t.height = side;
      t.getContext('2d').drawImage(canvas, (side - canvas.width * s) / 2, (side - canvas.height * s) / 2,
                                   canvas.width * s, canvas.height * s);
      btn.style.backgroundImage = 'url(' + t.toDataURL('image/jpeg', 0.7) + ')';
    } catch (e) {}
    btn.classList.add('has-last');
    btn.title = szT('search.photo_back', 'Back to the photo');
    btn.setAttribute('aria-label', btn.title);
  }
  // Back to the photo: its sheet, its lines and its places again.
  function reopen() {
    sheet.hidden = false;
    canvas.hidden = false;
    pickRow.hidden = true;
    againBtn.hidden = false;
    // The tiles, opened since, emptied them: the lines read again, not the
    // search's joined ones ("366 116 طريق").
    showLines(last.raw || last.lines);
    setStatus(szT('search.photo_lines', 'Text in the photo: tap a line to search for it'), 'lines');
    input.value = last.query;
    document.getElementById('search-clear').style.display = 'block';
    forgetVerdict();
    var opts = signOpts(last.raw);
    map._searchSign(last.lines, opts).then(function () {
      if (opts.photo === photoSeq) sheet.setAttribute('data-sz-ocr', 'searched');
    });
  }
  // A new photo, as _szSignPickMode says: the two tiles (the camera off in
  // Kiwix on iOS), the note to drop or paste in Kiwix on macOS, or the
  // file picker at once on a desktop.
  function pick() {
    forgetVerdict();
    var coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
    var mode = _szSignPickMode(kiwixApp(), coarse);
    if (mode === 'picker') {
      file.value = '';
      file.click();
      return;
    }
    sheet.hidden = false;
    canvas.hidden = true;
    linesEl.innerHTML = '';
    pickRow.hidden = mode === 'drop';
    againBtn.hidden = true;
    takeBtn.disabled = mode !== 'both';
    takeBtn.classList.toggle('primary', mode === 'both');
    chooseBtn.classList.toggle('primary', mode === 'library');
    if (mode === 'library') {
      setStatus(szT('search.photo_kiwix_ios', 'In the Kiwix app, choose a photo from your library: taking a new photo here closes the app, which cannot use the camera yet.'), 'pick');
    } else if (mode === 'drop') {
      setStatus(szT('search.photo_drop', 'Drag a photo onto the map, or paste one with ⌘V.'), 'drop');
    } else {
      setStatus(szT('search.photo', 'Search with a photo'), 'choose');
    }
  }
  // The first image of a drop or a paste (DataTransfer), or null.
  function imageOf(dt) {
    var files = (dt && dt.files) || [];
    for (var i = 0; i < files.length; i++) if (/^image\//.test(files[i].type || '')) return files[i];
    var items = (dt && dt.items) || [];
    for (var j = 0; j < items.length; j++) {
      if (items[j].kind === 'file' && /^image\//.test(items[j].type || '')) return items[j].getAsFile();
    }
    return null;
  }
  function carriesFiles(dt) {
    var types = (dt && dt.types) || [];
    for (var i = 0; i < types.length; i++) if (types[i] === 'Files') return true;
    return false;
  }

  // The photo, scaled to MAX_SIDE, drawn on the sheet's canvas; its pixels.
  function loadPhoto(f) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(f);
      var img = new Image();
      img.onload = function () {
        URL.revokeObjectURL(url);
        var s = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
        canvas.width = Math.max(1, Math.round(img.naturalWidth * s));
        canvas.height = Math.max(1, Math.round(img.naturalHeight * s));
        var ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        var d = ctx.getImageData(0, 0, canvas.width, canvas.height);
        resolve({ width: d.width, height: d.height, data: new Uint8Array(d.data.buffer) });
      };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('not an image')); };
      img.src = url;
    });
  }

  // The photo on the sheet's canvas at COARSE_SIDE ({k, image}: k its scale),
  // or null when it is not much bigger than that (one reading finds it all).
  function coarseCopy() {
    var k = COARSE_SIDE / Math.max(canvas.width, canvas.height);
    if (k > 0.6) return null;
    var c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(canvas.width * k));
    c.height = Math.max(1, Math.round(canvas.height * k));
    var ctx = c.getContext('2d');
    ctx.drawImage(canvas, 0, 0, c.width, c.height);
    var d = ctx.getImageData(0, 0, c.width, c.height);
    return { k: c.width / canvas.width, image: { width: d.width, height: d.height, data: new Uint8Array(d.data.buffer) } };
  }
  // Both readings of a photo: as drawn, then shrunk (copied before any box
  // is drawn on the canvas); the coarse one's lines join the fine one's.
  function readBoth(image) {
    var coarse = coarseCopy();
    return read(image).then(function (fine) {
      if (!coarse) return fine;
      return read(coarse.image).then(function (big) { return _szSignMerge(fine, big, 1 / coarse.k); },
                                     function () { return fine; });
    });
  }

  function outline(lines) {
    var ctx = canvas.getContext('2d');
    ctx.lineWidth = Math.max(2, canvas.width / 300);
    ctx.strokeStyle = '#1a73e8';
    lines.forEach(function (L) { ctx.strokeRect(L.box.x, L.box.y, L.box.width, L.box.height); });
  }

  // Each result's kind (_szSignKinds) as its row's label, and the first
  // one's, with whether other places share its name, above the lines. An
  // exact address goes first. `photo` is the photo the search is for: a
  // later photo, or a line tapped (a typed search), and it says nothing.
  var photoSeq = 0;
  function signLabel(k) {
    if (k.kind === 'address') return szT('search.sign_address', 'Exact address');
    if (k.kind === 'street' && k.building) {
      return szT('search.sign_street_no_address', 'Street only: no address {n} found', { n: k.building });
    }
    if (k.kind === 'street' && k.plate) return szT('search.sign_street_only', 'Street only, not the address');
    if (k.kind === 'area') return szT('search.sign_area', 'Area, not an exact spot');
    if (k.kind === 'similar') return szT('search.sign_similar', 'Not an exact match: check the sign');
    return szT('search.sign_same_name', 'Same name as on the sign');
  }
  function signOpts(lines) {
    var photo = photoSeq, plate = null;
    var plateOf = function (words) { return plate || (plate = _szSignPlate(lines, words)); };
    return {
      photo: photo,
      admit: function (rec, words) {
        var p = plateOf(words);
        return _szSignMayBeAddress(rec, p, words) && _szSignAddressKind(rec, p, words, null) !== 'drop';
      },
      // Addresses that are not the sign's go before the list is cut.
      prune: function (records, words) {
        var p = plateOf(words), streets = _szSignStreets(p, words, records);
        return records.filter(function (r) { return r.t !== 'addr' || _szSignAddressKind(r, p, words, streets) !== 'drop'; });
      },
      label: function (found, all, words) {
        if (photo !== photoSeq) return false;
        // Any address left that is not the plate's goes; the plate's comes first.
        var kinds = _szSignKinds(found, lines, words, all);
        var rank = function (k) { return k.kind === 'address' ? 0 : 1; };
        var order = found.map(function (m, i) { return i; })
          .filter(function (i) { return kinds[i].kind !== 'drop'; })
          .sort(function (x, y) { return rank(kinds[x]) - rank(kinds[y]) || x - y; });
        if (order.length !== found.length || order.some(function (v, i) { return v !== i; })) {
          var kept = order.map(function (i) { return found[i]; });
          found.length = 0;
          Array.prototype.push.apply(found, kept);
          kinds = _szSignKinds(found, lines, words, all);
        }
        found.forEach(function (m, i) {
          var k = kinds[i];
          // An address found in several places is not the one: said on its row too.
          m.signBadge = k.kind === 'address' && k.several
            ? { kind: 'several', text: szT('search.sign_several', "Other places have this name: the sign doesn't say which") }
            : { kind: k.kind, text: signLabel(k) };
        });
        verdictEl.innerHTML = '';
        verdictEl.hidden = !kinds.length;
        if (!kinds.length) return true;
        // Green only for an address that is the only one of its words.
        verdictEl.setAttribute('data-kind', kinds[0].several ? 'several' : kinds[0].kind);
        var main = document.createElement('strong');
        main.textContent = signLabel(kinds[0]);
        verdictEl.appendChild(main);
        // Where it is: a photo of a hotel key or a poster is of a place
        // anywhere, and its town tells whether it can be the one.
        var where = found[0] && found[0].item && found[0].item.l;
        if (where) verdictEl.appendChild(document.createTextNode(' \u00b7 ' + where));

        if (kinds[0].several) {
          verdictEl.appendChild(document.createTextNode(' '));
          var more = document.createElement('span');
          more.className = 'ocr-verdict-several';
          more.textContent = szT('search.sign_several', "Other places have this name: the sign doesn't say which");
          verdictEl.appendChild(more);
        }
        return true;
      }
    };
  }
  function forgetVerdict() {
    photoSeq++;
    verdictEl.hidden = true;
  }

  function showLines(lines) {
    linesEl.innerHTML = '';
    lines.forEach(function (L) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'ocr-line';
      b.dir = 'auto';
      b.textContent = L.text;
      b.addEventListener('click', function () {
        input.value = L.text;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus({ preventScroll: true });
      });
      linesEl.appendChild(b);
    });
  }

  function handle(f) {
    if (!f) return;
    sheet.hidden = false;
    canvas.hidden = false;
    pickRow.hidden = true;
    againBtn.hidden = false;
    linesEl.innerHTML = '';
    forgetVerdict();
    forget();
    // The last photo's places are not this one's.
    resultsEl.innerHTML = '';
    resultsEl.style.display = 'none';
    setStatus(szT('search.photo_reading', 'Reading the photo…'), 'reading');
    var t0 = Date.now();
    loadPhoto(f).then(readBoth).then(function (items) {
      var lines = _szSignLineGroups(items);
      dbg('sign reader: ' + lines.length + ' lines in ' + (Date.now() - t0) + ' ms');
      outline(lines);
      if (!lines.length) {
        setStatus(szT('search.photo_none', 'No text found in the photo'), 'none');
        return;
      }
      setStatus(szT('search.photo_lines', 'Text in the photo: tap a line to search for it'), 'lines');
      showLines(lines);
      // Each line weighs as tall as its letters, against the sign's biggest.
      var tallest = Math.max.apply(null, lines.map(function (L) { return L.size; }));
      var weighted = lines.map(function (L) {
        return { text: L.text, weight: 0.5 + 0.5 * L.size / tallest };
      }).concat(_szSignAddress(lines, tallest));
      // In the field: the sign's biggest line now, the line the first place
      // matched once found -- what was searched for, to edit and search as
      // typed, not everything the photo holds (a status bar, a watermark).
      var biggest = lines.filter(function (L) { return L.size === tallest; })[0].text;
      input.value = biggest;
      document.getElementById('search-clear').style.display = 'block';
      var opts = signOpts(lines);
      return map._searchSign(weighted, opts).then(function (found) {
        if (opts.photo !== photoSeq) return;   // let go meanwhile: its answer is not shown
        var query = (found[0] && found[0].signLine) || biggest;
        input.value = query;
        keep(weighted, query, lines);
        sheet.setAttribute('data-sz-ocr', 'searched');
      });
    }).catch(function (err) {
      dbg('sign reader failed', describeError(err));
      setStatus(szT('search.photo_failed', 'Could not read the photo'), 'failed');
      // Why, in small print: on a phone there is no console to read it in.
      var why = document.createElement('small');
      why.className = 'ocr-why';
      why.textContent = describeError(err);
      statusEl.appendChild(why);
    });
  }

  // The camera: back to the last photo while there is one, else a new one.
  btn.addEventListener('click', function () {
    if (last && sheet.hidden) reopen(); else pick();
  });
  againBtn.addEventListener('click', pick);
  chooseBtn.addEventListener('click', function () {
    file.value = '';
    file.click();
  });
  takeBtn.addEventListener('click', function () {
    if (takeBtn.disabled) return;
    takeFile.value = '';
    takeFile.click();
  });
  file.addEventListener('change', function () { handle(file.files && file.files[0]); });
  takeFile.addEventListener('change', function () { handle(takeFile.files && takeFile.files[0]); });
  // A photo dropped anywhere on the page, or pasted (not into a text field
  // pasting text): the way in where no picker opens (Kiwix for macOS), and
  // a shortcut on any desktop. Without preventDefault on dragover a web
  // view opens the dropped file in place of the map.
  document.addEventListener('dragover', function (ev) {
    if (carriesFiles(ev.dataTransfer)) { ev.preventDefault(); ev.dataTransfer.dropEffect = 'copy'; }
  });
  document.addEventListener('drop', function (ev) {
    if (!carriesFiles(ev.dataTransfer)) return;
    ev.preventDefault();
    var f = imageOf(ev.dataTransfer);
    if (f) handle(f);
  });
  document.addEventListener('paste', function (ev) {
    var t = ev.target;
    if (t && /^(INPUT|TEXTAREA)$/.test(t.tagName || '') && ev.clipboardData &&
        ev.clipboardData.getData('text/plain')) return;   // text into a field stays text
    var f = imageOf(ev.clipboardData);
    if (!f) return;
    ev.preventDefault();
    handle(f);
  });
  document.getElementById('ocr-close').addEventListener('click', discard);
  // A search typed, a line tapped or the field cleared: what follows is not the photo's.
  input.addEventListener('input', forgetVerdict);
  document.getElementById('search-clear').addEventListener('click', forgetVerdict);
  // A picked result flies the map there; the photo stays for the way back.
  resultsEl.addEventListener('click', function (ev) {
    if (!sheet.hidden && ev.target && ev.target.closest && ev.target.closest('.search-result')) hide();
  });
  window.__szSignSearch = { read: handle, release: release };
  return true;
}
