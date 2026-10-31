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
// A Cyrillic reader gives Latin or Greek letters that look the same in a
// Cyrillic word ("BYЛИЦЯ", "KPACИЛИBCЬKA", "YΛИЦА", "КОСbIГИНА"): in a word
// with Cyrillic letters, or one made only of such look-alikes on a line
// that is otherwise Cyrillic, they are read as the Cyrillic ones.
var SZ_SIGN_CYR = { A: 'А', B: 'В', C: 'С', E: 'Е', H: 'Н', I: 'І', K: 'К', M: 'М', O: 'О', P: 'Р', T: 'Т', X: 'Х',
                    Y: 'У', a: 'а', c: 'с', e: 'е', i: 'і', o: 'о', p: 'р', x: 'х', y: 'у', k: 'к',
                    'Α': 'А', 'Β': 'В', 'Ε': 'Е', 'Η': 'Н', 'Ι': 'І', 'Κ': 'К', 'Μ': 'М', 'Ο': 'О', 'Ρ': 'Р',
                    'Τ': 'Т', 'Χ': 'Х', 'Υ': 'У', 'Λ': 'Л', 'Π': 'П', 'Φ': 'Ф', 'Γ': 'Г', 'Δ': 'Д' };
function _szSignCyrillic(text, wordsOnly) {
  var tokens = String(text).split(/(\s+)/);
  var cyr = function (w) { return /\p{Script=Cyrillic}/u.test(w); };
  var lookalike = function (w) { return /\p{L}/u.test(w) && Array.from(w).every(function (c) { return !/\p{L}/u.test(c) || SZ_SIGN_CYR[c]; }); };
  var words = tokens.filter(function (w) { return /\p{L}/u.test(w); });
  var mostly = words.filter(cyr).length * 2 > words.length;
  return tokens.map(function (w) {
    if (!(cyr(w) || (mostly && !wordsOnly && lookalike(w)))) return w;
    w = w.replace(/[bЬь][Il1|]/g, function (m) { return m.charAt(0) === 'ь' ? 'ы' : 'Ы'; });
    return Array.from(w).map(function (c) { return SZ_SIGN_CYR[c] || c; }).join('');
  }).join('');
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
      // On a line of mostly Cyrillic words, beside one of its words with
      // letters: a house number spanning a Moscow plate's two rows ("2" by
      // "КОСЫГИНА" over "18 ◂ УЛИЦА") does not glue the rows together.
      // Elsewhere beside the line's outline, which keeps a tilted line with
      // a number between its words whole ("خیابان ۱۵ خرداد", "Via 4 Novembre").
      var lettered = L.items.filter(function (o) { return SZ_SIGN_LETTER.test(o.text); });
      var cyr = lettered.filter(function (o) { return /\p{Script=Cyrillic}/u.test(o.text); }).length * 2 > lettered.length;
      var beside = (cyr ? lettered : [{ box: L }]).some(function (o) {
        var overlap = Math.min(o.box.y + o.box.height, b.y + b.height) - Math.max(o.box.y, b.y);
        return overlap >= 0.5 * Math.min(o.box.height, b.height);
      });
      if (beside) line = L;
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
    var cyrText = _szSignCyrillic(text), asRead = _szSignCyrillic(text, true);
    return { text: cyrText, box: { x: L.x, y: L.y, width: L.width, height: L.height }, rtl: rtl,
             size: L.vertical ? L.width : L.height, asRead: asRead !== cyrText ? asRead : '' };
  });
}
// The lines the search looks for, each weighing as tall as its letters
// against the sign's biggest; a line whose words of look-alike letters alone
// were read as Cyrillic ("OBI" on "СТРОИТЕЛЬНЫЙ ГИПЕРМАРКЕТ OBI": "ОВІ") also
// as read, so a Latin brand on a Russian sign is still found.
function _szSignWeighted(lines, tallest) {
  var out = [];
  lines.forEach(function (L) {
    var w = 0.5 + 0.5 * L.size / tallest;
    out.push({ text: L.text, weight: w });
    if (L.asRead) out.push({ text: L.asRead, weight: w, extra: true });
  });
  return out;
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
// Bahrain's "Avenue" and elsewhere a street. Russian and Ukrainian types as
// one word each ("ул." and "вулиця" are "улица"; "узвіз" is "спуск", as old
// Kyiv signs say it), as the search folds them (ї as і: "проізд").
var SZ_SIGN_TYPES = { road: 'road', rd: 'road', street: 'street', st: 'street', avenue: 'avenue', ave: 'avenue',
                      av: 'avenue', lane: 'lane', ln: 'lane', drive: 'drive', dr: 'drive', boulevard: 'boulevard',
                      blvd: 'boulevard', way: 'way', 'طريق': 'road', 'ممر': 'lane', 'شارع': 'شارع', 'جادة': 'جادة',
                      'улица': 'улица', 'ул': 'улица', 'вулиця': 'улица', 'вул': 'улица',
                      'переулок': 'переулок', 'пер': 'переулок', 'провулок': 'переулок', 'пров': 'переулок',
                      'проспект': 'проспект', 'просп': 'проспект', 'бульвар': 'бульвар', 'бульв': 'бульвар',
                      'площадь': 'площадь', 'площа': 'площадь', 'пл': 'площадь', 'шоссе': 'шоссе', 'шосе': 'шоссе',
                      'набережная': 'набережная', 'набережна': 'набережная', 'наб': 'набережная',
                      'проезд': 'проезд', 'проізд': 'проезд', 'тупик': 'тупик', 'спуск': 'спуск', 'узвіз': 'спуск',
                      'аллея': 'аллея', 'алея': 'аллея' };
var SZ_SIGN_FILLER = ['building', 'bldg', 'bldng', 'bld', 'no', 'house', 'مبنى', 'منزل'];
var SZ_SIGN_SUFFIX = ['號', '号', '番地', '번지'];
// Words of a speed sign: a number beside them is a limit, never a building.
var SZ_SIGN_SPEED = ['speed', 'limit', 'mph', 'kmh', 'km', 'zone', 'tempo', 'سرعة', '限速', '速度'];
function _szSignIsNum(w) { return /^\d+$/.test(w); }
// A house's letter after its number ("12a", "12-А", "8Β"): one letter of an alphabet, not a Chinese
// character ("23 巷" is a lane).
var SZ_SIGN_HOUSE_LETTER = /^[\p{Script=Latin}\p{Script=Cyrillic}\p{Script=Greek}]$/u;
// Cyrillic words: the rules for Russian and Ukrainian plates below (a type
// on a line of its own or anywhere in the line, one letter of a type
// misread, the neighbours' numbers in small print) apply to Cyrillic
// streets only, never to an address in another script.
function _szSignCyr(w) { return /\p{Script=Cyrillic}/u.test(Array.isArray(w) ? w.join(' ') : w); }
// Whether a and b are one edit apart (a letter changed, added or dropped).
function _szSignOneEdit(a, b) {
  a = Array.from(a); b = Array.from(b);
  if (Math.abs(a.length - b.length) > 1) return false;
  var i = 0, j = 0, edits = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { i++; j++; continue; }
    if (++edits > 1) return false;
    if (a.length > b.length) i++; else if (b.length > a.length) j++; else { i++; j++; }
  }
  return edits + (a.length - i) + (b.length - j) <= 1;
}
// A Cyrillic street name's words without its type, when the sign carries
// that type elsewhere ("Басовская улица" as "БАСОВСКАЯ" over "УЛИЦА"): null otherwise.
function _szSignBare(ws, plate) {
  var t = ws.filter(function (w) { return plate.isType(w); });
  if (t.length !== 1 || !_szSignCyr(t[0]) || plate.signTypes.indexOf(plate.types[t[0]]) < 0) return null;
  var bare = ws.filter(function (w) { return !plate.isType(w); });
  return bare.length ? bare : null;
}
// A name of the map and a line of the sign: the same words in order, or
// the name's words but its type when the type is elsewhere on the sign.
function _szSignNameLine(L, ws, plate) {
  if (_szSignSameLine(L, ws)) return true;
  var bare = _szSignBare(ws, plate);
  return !!bare && _szSignSameLine(L, bare);
}
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
  var plate = { types: types, filler: fold(SZ_SIGN_FILLER), suffix: fold(SZ_SIGN_SUFFIX), marks: fold(SZ_SIGN_MARKS) };
  // An ordinal is one word ("13th", "21st": not 21 and a "st" for Street).
  // A Cyrillic word one letter from a Cyrillic type of five letters or more
  // is read as that type ("УЛУЦА", "нереулок": a letter misread), on the
  // sign and in names alike.
  var canon = Object.keys(types).map(function (k) { return types[k]; })
    .filter(function (c, i, a) { return a.indexOf(c) === i && Array.from(c).length >= 5 && _szSignCyr(c); });
  var near = {};
  var typeOf = function (w) {
    if (Object.prototype.hasOwnProperty.call(types, w)) return types[w];
    if (Object.prototype.hasOwnProperty.call(near, w)) return near[w];
    var t = null;
    if (Array.from(w).length >= 5 && /^\p{L}+$/u.test(w)) {
      var c = canon.filter(function (x) { return _szSignOneEdit(x, w); });
      if (c.length === 1) t = c[0];
    }
    return (near[w] = t);
  };
  plate.norm = function (ws) {
    var out = [];
    for (var i = 0; i < ws.length; i++) {
      if (_szSignIsNum(ws[i]) && /^(st|nd|rd|th)$/.test(ws[i + 1] || '')) { out.push(ws[i] + ws[i + 1]); i++; continue; }
      out.push(typeOf(ws[i]) || ws[i]);
    }
    return out;
  };
  plate.isType = function (w) { return Object.prototype.hasOwnProperty.call(types, w); };
  var building = function (ws) {
    return _szSignIsNum(ws[0] || '') && (ws.length === 1 ||
      (ws.length === 2 && (SZ_SIGN_HOUSE_LETTER.test(ws[1]) || plate.suffix.indexOf(ws[1]) >= 0)));
  };
  var rows = ls.map(function (L) {
    return { ws: plate.norm(words(L.text)), size: L.size || 0, box: L.box, text: String(L.text).trim() };
  });
  plate.lines = rows.map(function (r) { return r.ws; });
  plate.texts = rows.map(function (r) { return r.text; });
  plate.sizes = rows.map(function (r) { return r.size; });
  // The street types the sign carries, in any line (a Russian plate's
  // "УЛИЦА" under its name).
  plate.signTypes = [];
  rows.forEach(function (r) {
    r.ws.forEach(function (w) { if (plate.isType(w) && plate.signTypes.indexOf(types[w]) < 0) plate.signTypes.push(types[w]); });
  });
  var top = rows.filter(function (r) { return building(r.ws); }).sort(function (x, y) { return y.size - x.size; })[0];
  plate.building = top ? top.ws : null;
  var speed = fold(SZ_SIGN_SPEED);
  var onePlate = function (r) {
    var A = top && top.box, B = r.box;
    if (!A || !B) return false;
    var over = Math.min(A.x + A.width, B.x + B.width) - Math.max(A.x, B.x);
    var gap = Math.max(A.y, B.y) - Math.min(A.y + A.height, B.y + B.height);
    // A likely speed value (two digits, a multiple of 5, taller than the
    // street's text) beside a street with no number of its own must span
    // half the street line: a roundel under a long blade does not; a house
    // plaque's "105" under "North Taaffe Street" need not.
    var v = +top.ws[0], speedish = top.ws.length === 1 && /^\d{2}$/.test(top.ws[0]) && v % 5 === 0 && top.size > r.size;
    var span = !r.ws.some(_szSignIsNum) && speedish ? Math.max(A.width, B.width) : Math.min(A.width, B.width);
    if (!(over >= 0.5 * span && gap < 1.5 * Math.max(A.height, B.height))) return false;
    var lo = Math.min(A.y + A.height, B.y + B.height), hi = Math.max(A.y, B.y);
    return !rows.some(function (o) {          // nothing between them ("SPEED", "LIMIT")
      var C = o.box;
      return o !== top && o !== r && C && C.y + C.height / 2 > lo && C.y + C.height / 2 < hi &&
        Math.min(C.x + C.width, A.x + A.width) - Math.max(C.x, A.x) > 0;
    });
  };
  var street = function (r) { return r !== top && r.ws.some(plate.isType); };
  var onSpeedSign = rows.some(function (r) { return r.ws.some(function (w) { return speed.indexOf(w) >= 0; }); });
  plate.buildingSize = top ? top.size : 0;
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
  var raw = String(rec.n || '').split(',')[0];
  var ws = plate.norm(words(raw));
  while (ws.length && plate.filler.indexOf(ws[0]) >= 0) ws = ws.slice(1);
  if (!_szSignIsNum(ws[0] || '')) return null;
  // A letter after the number is the building's ("12a Main St", "12-А") only
  // when the rest still names a street ("1018 B St" is on B Street), and
  // not a letter with a full stop after it, an initial or an abbreviation
  // ("12 N. Main Street", "12 Λ. Κηφισίας", Λεωφόρος, "12 ø."), unless a
  // small a-z or Cyrillic one ("12 a.", "12 а.": the house's).
  var named = function (r) { return r.some(function (w) { return !_szSignIsNum(w) && !plate.isType(w); }); };
  var initial = /^\D*?\d+\s+(?![a-zа-яёіїєґ]\.)\p{L}\./u.test(raw);
  var n = ws[1] && (plate.suffix.indexOf(ws[1]) >= 0 || !initial && SZ_SIGN_HOUSE_LETTER.test(ws[1]) && named(ws.slice(2))) ? 2 : 1;
  var rest = ws.slice(n).filter(function (w) { return plate.filler.indexOf(w) < 0; });
  return rest.length ? { b: ws.slice(0, n), rest: rest } : null;
}
// The record's street (`rest`) and a street line of the sign, word for word:
// the same words and types; a record naming the street without a type
// ("366 116") matches the line without its type word ("Road 116"). A
// record with a type word matches a line without it only when the sign
// carries that type elsewhere (_szSignBare).
function _szSignStreetIs(rest, line, plate) {
  if (_szSignNameLine(line, rest, plate)) return true;
  var bare = line.filter(function (w) { return !plate.isType(w); });
  return bare.length > 0 && _szSignSameLine(bare, rest);
}
// What an address record is to the sign, given its street lines (`streets`:
// the lines that are a street of the map), word for word and in order:
//   'address'  its building number is the sign's (`plate.building`) and it
//              may be named (`plate.claim`), and its street is one of
//              `streets`; or one line holds its street and number, either
//              way round, with a name in the street part ("Schulweg 10",
//              "三和路四段20巷101號"; not "1225 Road 1029"); either way
//              unless another line looks like a building's number and is
//              another one (_szSignOtherBuilding: 'similar');
//   'similar'  its number and street are on the sign but the number may not
//              be the building's (a "30" under "Schulweg"): listed, to check;
//   'drop'     anything else: not listed.
// With `streets` null (while the search runs): whether it may be, cheaply.
function _szSignAddressKind(rec, plate, words, streets, places, pois) {
  if (!rec || rec.t !== 'addr') return 'drop';
  var p = _szSignAddressParts(rec, plate, words);
  if (!p) return 'drop';
  var named = p.rest.some(function (w) { return !_szSignIsNum(w) && !plate.isType(w); });
  var onLine = function (rest) {
    return plate.lines.filter(function (L) {
      if (_szSignSameLine(L, rest.concat(p.b)) || _szSignSameLine(L, p.b.concat(rest))) return true;
      // the same Cyrillic type word elsewhere in the line ("ВУЛИЦЯ СЕРПОВА
      // 12-А" for "12-А Серпова вулиця"; "УЛИЦА КОСЫГИНА 2" for "2 улица Косыгина")
      var lt = L.filter(plate.isType), rt = rest.filter(plate.isType);
      if (!lt.length || lt.join(' ') !== rt.join(' ') || !_szSignCyr(lt)) return false;
      var Lb = L.filter(function (w) { return !plate.isType(w); }), Rb = rest.filter(function (w) { return !plate.isType(w); });
      return _szSignSameLine(Lb, Rb.concat(p.b)) || _szSignSameLine(Lb, p.b.concat(Rb));
    })[0];
  };
  var bare = _szSignBare(p.rest, plate);
  var one = named && (onLine(p.rest) || (bare && onLine(bare)));
  if (one) {
    if (_szSignOtherBuilding(plate, p, [one], plate.sizes[plate.lines.indexOf(one)])) return 'similar';
    return _szSignTownOk(rec, plate, words, [one], places, pois) ? 'address' : 'similar';
  }
  // a Cyrillic street without its type, and no type anywhere on the sign
  // ("18 СЕРПОВА" for "18 Серпова вулиця"): which street of that name it is,
  // the sign does not say (a sign that says "ПЕРЕУЛОК" says it is not this one)
  var untyped = p.rest.filter(function (w) { return !plate.isType(w); });
  if (named && _szSignCyr(p.rest) && !plate.signTypes.length && untyped.length < p.rest.length && onLine(untyped)) return 'similar';
  if (!plate.building || !_szSignSame(plate.building, p.b)) return 'drop';
  // A street line, or one with the line under it (Taipei's "忠孝東路三段"
  // over "217巷7弄"), but not the building's line.
  var cands = [];
  (streets || plate.lines).forEach(function (S) {
    cands.push({ ws: S, used: [S] });
    var i = plate.lines.indexOf(S), next = plate.lines[i + 1];
    if (i >= 0 && next && next !== plate.building && !_szSignSame(next, plate.building)) {
      cands.push({ ws: S.concat(next), used: [S, next] });
    }
  });
  var on = cands.filter(function (c) { return _szSignStreetIs(p.rest, c.ws, plate); })[0];
  if (!on) return 'drop';
  if (!plate.claim) return 'similar';
  var usedP = on.used.concat([plate.building]);
  if (streets && _szSignOtherBuilding(plate, p, usedP, plate.buildingSize)) return 'similar';
  return !streets || _szSignTownOk(rec, plate, words, usedP, places, pois) ? 'address' : 'similar';
}
// Whether another line of the sign (besides `skip`, the address's own)
// looks like a building's number and is not the address's (`p`): a number
// alone, or with "No.", 號, 番地, 호, 門牌 or one stray mark ("133",
// "No. 133", "133番地", "福 133"), a second number after it ("133之1",
// "22-4": the first is the building's), a letter ("12b"). Not a floor or
// the hours ("3F", "B1", "24H"), a unit ("#200"), a postcode or a phone
// (5 digits or more), a year after ©, a contact line, nor the street's own
// number. A stray "4" glued to "中山北路一段" while the plate's "133" was
// read apart would otherwise make an exact address of house 4. Nor, for a
// Cyrillic street, a number in print under 60% of the address's number's
// (`size`, when the lines have sizes): a Moscow plate's "◂ 80" and "12 ▸",
// its neighbours' numbers.
var SZ_SIGN_MARKS = ['no', 'nr', 'num', 'number', '門牌', '门牌', 'رقم', '之', 'の', '-', '호'];
function _szSignOtherBuilding(plate, p, skip, size) {
  var mine = p.b.join('');
  var chars = function (w) { return Array.from(w.normalize('NFC')).length; };
  var markChars = plate.suffix.concat(plate.marks).map(function (w) { return w.normalize('NFC'); })
    .filter(function (c) { return Array.from(c).length === 1; });
  return plate.lines.some(function (L, i) {
    if (!L || skip.indexOf(L) >= 0) return false;
    if (size > 0 && _szSignCyr(p.rest) && (plate.sizes || [])[i] > 0 && plate.sizes[i] < 0.6 * size) return false;
    var raw = String(plate.texts[i] || '').normalize('NFKC');
    if (SZ_SIGN_CONTACT.test(raw) || /[#©]/.test(raw)) return false;
    var ws = L.filter(function (w) {
      return plate.filler.indexOf(w) < 0 && plate.suffix.indexOf(w) < 0 && plate.marks.indexOf(w) < 0 &&
        !Array.from(w.normalize('NFC')).every(function (c) { return markChars.indexOf(c) >= 0; });   // "號之"
    });
    // a floor, a basement, the hours: that number is no building's ("1F" after "133號")
    for (var k = 0; k < ws.length; k++) {
      if (_szSignIsNum(ws[k]) && /^(f|fl|h)$/.test(ws[k + 1] || '')) { ws.splice(k, 2); k--; }
      else if (ws[k] === 'b' && _szSignIsNum(ws[k + 1] || '')) { ws.splice(k, 2); k--; }
    }
    if (!ws.length || ws.length > 4) return false;
    var nums = ws.filter(_szSignIsNum);
    if (nums.length < 1 || nums.length > 2 || nums.some(function (n) { return n.length >= 5; })) return false;
    var at = _szSignIsNum(ws[0]) ? 0 : (_szSignIsNum(ws[1] || '') ? 1 : -1);     // a stray mark before it: one character (below)
    if (at < 0) return false;
    if (!ws.every(function (w, j) { return j === at || _szSignIsNum(w) || chars(w) <= 1; })) return false;
    var b = ws[at] + (SZ_SIGN_HOUSE_LETTER.test(ws[at + 1] || '') ? ws[at + 1] : '');
    return b !== mine && p.rest.indexOf(ws[at]) < 0;
  });
}
// Whether the sign's other lines allow the address's town. Each line
// besides the address's own (3 letters or more: not a screw read as "a";
// not a block, flat or floor; not a phone, web or mail line) is
//   confirming  when it holds the address's town (the first part after its
//               comma; 臺 and 台 alike, Chinese and Japanese by containment:
//               "臺北市大安區" holds 台北市), or names an area of the map
//               (`places`, also within the line: "Kingdom of Bahrain") that
//               holds the address: inside its box, or near
//               its point for a town without one (1 to 8 km by its size; a
//               box when it has both);
//   a conflict  when it names an area of the map that does not ("San Jose"
//               against Campbell, "20095 Hamburg" against Husum), has a
//               postcode, or is a short name ending in 市, 區, 縣... (台中市);
//   neutral     when it names a place of the map (`pois`: a hotel's name);
//   unknown     otherwise ("Denver", a slogan).
// A conflict, or unknown lines and none confirming (a card from a city the
// map does not have, whether or not the address names its town), and the
// address is only to check.
var SZ_SIGN_NEUTRAL = ['block', 'blk', 'مجمع', 'flat', 'apt', 'floor'];
var SZ_SIGN_TOWN_END = /[市縣县區区町村郡省鎮镇鄉乡]$/;
var SZ_SIGN_CONTACT = /@|www\.|https?:|\.(com|net|org|co)\b|\b(tel|fax|phone|mobile)\b|☎/i;
var SZ_SIGN_PLACE_KM = { city: 8, town: 4, village: 2, suburb: 2, hamlet: 1, neighbourhood: 1, quarter: 1 };
function _szSignTownOk(rec, plate, words, used, places, pois) {
  var neutral = plate.neutral || (plate.neutral = [].concat.apply([], SZ_SIGN_NEUTRAL.map(words)));
  var han = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;
  var fold = function (t) { return t.replace(/臺/g, '台'); };
  var n = String(rec.n || ''), parts = n.split(',');
  var townWs = parts.length > 1 ? words(parts[1]).filter(function (w) { return !_szSignIsNum(w); }) : [];
  var town = fold(townWs.join(' ')), townC = town.replace(/ /g, '');
  var holds = function (p) {
    if (p.bb) return rec.o >= p.bb[0] && rec.o <= p.bb[2] && rec.a >= p.bb[1] && rec.a <= p.bb[3];
    var dy = (p.a - rec.a) * 111.2, dx = (p.o - rec.o) * 111.2 * Math.cos(rec.a * Math.PI / 180);
    return Math.sqrt(dx * dx + dy * dy) <= (SZ_SIGN_PLACE_KM[p.s] || 2);
  };
  var confirmed = false, unknown = false;
  var conflict = plate.lines.some(function (L, i) {
    if (used.indexOf(L) >= 0 || SZ_SIGN_CONTACT.test(String(plate.texts[i] || ''))) return false;
    var named = L.filter(function (w) {
      return !_szSignIsNum(w) && !plate.isType(w) && plate.filler.indexOf(w) < 0 && neutral.indexOf(w) < 0;
    });
    if (Array.from(named.join('')).length < 3) return false;          // nothing to read in it
    var txt = fold(named.join(' ')), txtC = txt.replace(/ /g, '');
    if (town && ((' ' + txt + ' ').indexOf(' ' + town + ' ') >= 0 || han.test(townC) && txtC.indexOf(townC) >= 0)) {
      confirmed = true;
      return false;
    }
    // an area named by the line, or within it ("Kingdom of Bahrain")
    var area = (places || []).filter(function (p) {
      return p.keys.some(function (k) {
        return k && ((' ' + txt + ' ').indexOf(' ' + k + ' ') >= 0 || han.test(k) && txtC.indexOf(k) >= 0);
      });
    });
    if (area.length) {
      // by its box; else by its point if it is a settlement (a country or a
      // region known by a point alone is too big to say either way)
      var boxed = area.filter(function (p) { return p.bb; });
      var judged = boxed.length ? boxed : area.filter(function (p) { return SZ_SIGN_PLACE_KM[p.s]; });
      if (!judged.length) return false;
      if (judged.some(holds)) { confirmed = true; return false; }
      return true;
    }
    if (L.some(function (w) { return /^\d{4,6}$/.test(w); })) return true;              // a postcode line
    if (SZ_SIGN_TOWN_END.test(txtC) && Array.from(txtC).length <= 3) return true;   // 台中市, 中山區
    if ((pois || []).some(function (k) { return k === txt || k === txtC; })) return false;
    unknown = true;
    return false;
  });
  return !conflict && (confirmed || !unknown);
}
// The map's areas among the records found, for _szSignTownOk: their names
// as folded words (臺 as 台) and their box or point.
function _szSignPlaces(records, words) {
  return records.filter(function (it) { return it && (it.t === 'place' || it.t === 'admin'); }).map(function (it) {
    var keys = [];
    _szSignNames(it).forEach(function (nm) {
      var k = words(nm).join(' ').replace(/臺/g, '台');
      keys.push(k, k.replace(/ /g, ''));
    });
    return { keys: keys, bb: Array.isArray(it.bb) && it.bb.length === 4 ? it.bb : null, a: it.a, o: it.o, s: it.s };
  });
}
// The map's other places among the records found (a hotel, a shop), their
// names as folded words: a line naming one is not a town.
function _szSignPois(records, words) {
  var keys = [];
  records.forEach(function (it) {
    if (!it || it.t !== 'poi') return;
    _szSignNames(it).forEach(function (nm) {
      var k = words(nm).join(' ').replace(/臺/g, '台');
      keys.push(k, k.replace(/ /g, ''));
    });
  });
  return keys;
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
      return it && it.t === 'street' && _szSignNames(it).some(function (n) { return _szSignNameLine(L, plate.norm(words(n)), plate); });
    });
  });
}
// A name too short to be a sign's without being read exactly: "IG" in
// "IGHTHAM", "aa", "10", "注意" (3 letters, 2 for Chinese, Japanese or
// Korean): a near miss of it is noise, not a place to check. Read exactly
// (`exact`), a line of 2 letters or 1 Chinese character ("MT" from a
// logo, "日") is still too short to name a place: only to check.
function _szSignTooShort(name, words, exact) {
  var t = words(name).join('');
  var cjk = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(t);
  // characters as written: the folded words split a Hangul syllable into its letters
  return Array.from(t.normalize('NFC')).length < (exact ? (cjk ? 2 : 3) : (cjk ? 3 : 4));
}
// Each match's kind, for its label:
//   'address', 'drop', or 'similar' for an address: _szSignAddressKind;
//   'street'   a street whose name is one line of the sign, in order:
//              `building` the plate's number when it may be named and its
//              address was not found; `plate` when the sign has a number
//              line at all ("Street only, not the address");
//   'area'     likewise a place or an administrative area (a town);
//   'name'     likewise anything else (a shop, a station); 'similar' when
//              its name is too short even read exactly ("MT");
//   'similar'  everything else: read inexactly, part of a line, glued from
//              two lines ("Road 1012" from "Road 101" and "2"); 'drop' when
//              its name is too short to mean anything (_szSignTooShort). A
//              short name read exactly ("KFC", "全家") is the name.
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
  var streets = _szSignStreets(plate, words, records), places = _szSignPlaces(records, words);
  var pois = _szSignPois(records, words);
  // A town on a direction sign may carry its distance ("Ely 12", "Ulm 35 km").
  var units = [].concat.apply([], ['km', 'mi', 'm', 'كم', 'км', '公里'].map(words));
  var lineOf = function (it) {
    var area = it.t === 'place' || it.t === 'admin';
    return _szSignNames(it).some(function (n) {
      var ws = plate.norm(words(n));
      return plate.lines.some(function (L) {
        if (!L.some(function (w) { return !_szSignIsNum(w); })) return false;
        if (_szSignNameLine(L, ws, plate)) return true;
        var bare = L.filter(function (w) { return !_szSignIsNum(w) && units.indexOf(w) < 0; });
        return area && bare.length < L.length && _szSignSameLine(bare, ws);
      });
    });
  };
  var addrKind = matches.map(function (m) { return m.item.t === 'addr' ? _szSignAddressKind(m.item, plate, words, streets, places, pois) : ''; });
  var found = addrKind.indexOf('address') >= 0;
  var out = matches.map(function (m, i) {
    var t = m.item.t;
    if (t === 'addr') return { kind: addrKind[i] };
    if (!lineOf(m.item)) return { kind: _szSignTooShort(m.item.n || '', words) ? 'drop' : 'similar' };
    if (t === 'street') {
      return { kind: 'street', building: found ? '' : plate.claim, plate: !!plate.building };
    }
    if (t === 'place' || t === 'admin') return { kind: 'area' };
    return { kind: _szSignTooShort(m.item.n || '', words, true) ? 'similar' : 'name' };
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
      if (top.t === 'addr' && _szSignAddressKind(it, plate, words, streets, places, pois) === 'drop') return false;
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

// BEGIN sign-view
// The photo in its frame (#ocr-frame): fitted whole at first, zoomed and
// moved by a pinch, a drag, a double tap or the wheel. A view is {s, tx,
// ty}: the photo fitted (`fit`: k CSS px per canvas pixel, its size w x h
// and offsets ox, oy), then scaled by s from the frame's top left and moved
// by tx, ty (CSS px). A part of the photo is [x0, y0, x1, y1], fractions of
// it: [0, 0, 1, 1] the whole.
function _szSignFit(fw, fh, cw, ch) {
  var k = Math.min(fw / cw, fh / ch);
  return { k: k, w: cw * k, h: ch * k, ox: (fw - cw * k) / 2, oy: (fh - ch * k) / 2 };
}
// The view kept in bounds: the photo covers the frame along a side where it
// is bigger than the frame, and is centred along one where it is smaller.
function _szSignClamp(v, fw, fh, fit) {
  var w = fit.w * v.s, h = fit.h * v.s;
  return { s: v.s, tx: w <= fw ? (fw - w) / 2 : Math.min(0, Math.max(fw - w, v.tx)),
           ty: h <= fh ? (fh - h) / 2 : Math.min(0, Math.max(fh - h, v.ty)) };
}
// The canvas pixels the frame shows: {x, y, width, height}.
function _szSignVisible(v, fw, fh, fit, cw, ch) {
  var kk = fit.k * v.s;
  var x0 = Math.max(0, -v.tx / kk), y0 = Math.max(0, -v.ty / kk);
  var x1 = Math.min(cw, (fw - v.tx) / kk), y1 = Math.min(ch, (fh - v.ty) / kk);
  return { x: x0, y: y0, width: Math.max(0, x1 - x0), height: Math.max(0, y1 - y0) };
}
// A point of the frame (CSS px) as a canvas pixel.
function _szSignAt(v, fit, px, py) {
  var kk = fit.k * v.s;
  return { x: (px - v.tx) / kk, y: (py - v.ty) / kk };
}
// The view scaled to s, the frame's point (px, py) staying where it is.
function _szSignZoomAt(v, s, px, py) {
  return { s: s, tx: px - (px - v.tx) * s / v.s, ty: py - (py - v.ty) * s / v.s };
}
// The line whose box holds the canvas pixel (x, y), within `tol` pixels;
// the smallest such box when they nest. Null when none does.
function _szSignHit(lines, x, y, tol) {
  var best = null, area = Infinity;
  (lines || []).forEach(function (L) {
    var b = L && L.box;
    if (!b || x < b.x - tol || x > b.x + b.width + tol || y < b.y - tol || y > b.y + b.height + tol) return;
    var a = b.width * b.height;
    if (a < area) { best = L; area = a; }
  });
  return best;
}
// The part of the photo a rectangle of the canvas (canvas pixels) shows,
// when the canvas shows `part` of it.
function _szSignSubPart(part, r, cw, ch) {
  var w = part[2] - part[0], h = part[3] - part[1];
  return [part[0] + r.x / cw * w, part[1] + r.y / ch * h,
          part[0] + (r.x + r.width) / cw * w, part[1] + (r.y + r.height) / ch * h];
}
function _szSignIsWhole(part) {
  return !part || (part[0] <= 0 && part[1] <= 0 && part[2] >= 1 && part[3] >= 1);
}
// END sign-view

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
  var frame = document.getElementById('ocr-frame');
  var zoomHint = document.getElementById('ocr-zoom');
  var zoombar = document.getElementById('ocr-zoombar');
  var partBtn = document.getElementById('ocr-part');
  var wholeBtn = document.getElementById('ocr-whole');
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
  // A part the reader frames is drawn from the photo itself, up to twice its
  // size there: small letters a whole photo shrinks away are read sharp.
  var PART_UP = 2;
  var MAX_ZOOM = 8;
  var WHOLE = [0, 0, 1, 1];
  // The photo ({img, url, w, h}: its own pixels), the part the canvas shows,
  // the lines read there, and the frame's view of it (sign-view).
  var photo = null, curPart = WHOLE, curLines = [], view = { s: 1, tx: 0, ty: 0 }, fit = null, loadSeq = 0;
  // Each reading's turn: a photo or part read, then another one started, a
  // new photo picked or the photo let go, and the first one's answer is
  // dropped (its lines, boxes, search and state are not the sheet's now).
  var readSeq = 0;
  var IDLE_MS = 60000;
  var manifest = null, worker = null, ready = null, idleTimer = null, reqSeq = 0, waiting = {}, busy = 0;
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

  // BEGIN sign-worker
  function release() {
    clearTimeout(idleTimer);
    if (worker) { try { worker.terminate(); } catch (e) {} }
    worker = null; ready = null;
    for (var k in waiting) waiting[k].reject(new Error('stopped'));
    waiting = {};
  }
  // A reading; the worker goes a minute after the last one ends, never
  // while one is under way (a photo read just before the minute was up,
  // after a long search on a big map, was stopped), and at once when a
  // reading has had no answer for three minutes (a worker that hangs).
  function read(image) {
    busy++;
    clearTimeout(idleTimer);
    var done = function () { if (--busy <= 0) { busy = 0; idleTimer = setTimeout(release, IDLE_MS); } };
    return start().then(function (w) {
      var id = ++reqSeq;
      return new Promise(function (resolve, reject) {
        waiting[id] = { resolve: resolve, reject: reject };
        w.postMessage({ type: 'read', id: id, image: image }, [image.data.buffer]);
        setTimeout(function () { if (waiting[id]) release(); }, 3 * IDLE_MS);
      });
    }).then(function (items) { done(); return items; }, function (err) { done(); throw err; });
  }
  // END sign-worker

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
    readSeq++; loadSeq++;
    if (photo && photo.url) URL.revokeObjectURL(photo.url);
    photo = null;
    curLines = [];
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
    frame.hidden = false;
    resetView();
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
      return;     // a read under way goes on: the picker may be cancelled
    }
    readSeq++; loadSeq++;     // the tiles replace the sheet: no reading or photo of before shows under them
    sheet.hidden = false;
    frame.hidden = zoomHint.hidden = zoombar.hidden = true;
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

  // The photo, decoded and kept (its own pixels: a part is drawn from them).
  // A photo decoded after a later one was picked is not kept: false.
  function loadPhoto(f, seq) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(f);
      var img = new Image();
      img.onload = function () {
        if (seq !== loadSeq) { URL.revokeObjectURL(url); resolve(false); return; }
        if (photo && photo.url) URL.revokeObjectURL(photo.url);
        photo = { img: img, url: url, w: img.naturalWidth, h: img.naturalHeight };
        resolve(true);
      };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('not an image')); };
      img.src = url;
    });
  }
  // `part` of the photo drawn on the sheet's canvas: the whole scaled to
  // MAX_SIDE at most, a part up to PART_UP times its own size; its pixels.
  function drawPart(part) {
    var sw = (part[2] - part[0]) * photo.w, sh = (part[3] - part[1]) * photo.h;
    var s = Math.min(_szSignIsWhole(part) ? 1 : PART_UP, MAX_SIDE / Math.max(sw, sh));
    canvas.width = Math.max(1, Math.round(sw * s));
    canvas.height = Math.max(1, Math.round(sh * s));
    var ctx = canvas.getContext('2d');
    ctx.drawImage(photo.img, part[0] * photo.w, part[1] * photo.h, sw, sh, 0, 0, canvas.width, canvas.height);
    var d = ctx.getImageData(0, 0, canvas.width, canvas.height);
    return { width: d.width, height: d.height, data: new Uint8Array(d.data.buffer) };
  }

  // The frame's view (sign-view): fitted whole, then pinched, dragged,
  // double-tapped or wheeled; "Search this part" once it shows less than
  // nearly all of the canvas, "Whole photo" while the canvas shows a part.
  function frameSize() { return { w: frame.clientWidth || 1, h: frame.clientHeight || 1 }; }
  function resetView() {
    if (frame.hidden) return;
    var f = frameSize();
    fit = _szSignFit(f.w, f.h, canvas.width, canvas.height);
    canvas.style.width = fit.w + 'px';
    canvas.style.height = fit.h + 'px';
    view = _szSignClamp({ s: 1, tx: fit.ox, ty: fit.oy }, f.w, f.h, fit);
    frameW = f.w; frameH = f.h;
    applyView();
  }
  // The frame resized (a scroll bar came, the phone turned): fitted again,
  // the same zoom about the same point of the photo.
  var frameW = 0, frameH = 0;
  function refit() {
    if (frame.hidden || !fit) return;
    var f = frameSize();
    if (f.w === frameW && f.h === frameH) return;
    var c = _szSignAt(view, fit, frameW / 2, frameH / 2), s = view.s;
    fit = _szSignFit(f.w, f.h, canvas.width, canvas.height);
    canvas.style.width = fit.w + 'px';
    canvas.style.height = fit.h + 'px';
    frameW = f.w; frameH = f.h;
    setView({ s: s, tx: f.w / 2 - c.x * fit.k * s, ty: f.h / 2 - c.y * fit.k * s });
  }
  function applyView() {
    canvas.style.transform = 'translate(' + view.tx + 'px,' + view.ty + 'px) scale(' + view.s + ')';
    frame.classList.toggle('zoomed', view.s > 1.01);
    showZoomBar();
  }
  // The hint (on a touch screen) and the bar's two buttons keep their room
  // while the photo shows, shown or not (two rows where the buttons wrap),
  // so the photo does not move under the fingers.
  var touch = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  function showZoomBar() {
    var off = frame.hidden || sheet.hidden;
    var reading = sheet.getAttribute('data-sz-ocr') === 'reading';
    var r = !off && fit ? _szSignVisible(view, frameSize().w, frameSize().h, fit, canvas.width, canvas.height) : null;
    var noPart = off || reading || !r || (r.width >= 0.92 * canvas.width && r.height >= 0.92 * canvas.height);
    partBtn.style.visibility = noPart ? 'hidden' : '';
    wholeBtn.style.visibility = off || reading || _szSignIsWhole(curPart) ? 'hidden' : '';
    zoombar.hidden = zoomHint.hidden = off;
    zoomHint.style.visibility = !touch || reading || !noPart ? 'hidden' : '';
    zoomHint.hidden = off || !touch;
  }
  function searchLine(text) {
    input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus({ preventScroll: true });
  }
  var ptrs = {}, gesture = null, lastTap = null, tapTimer = null;
  function framePoint(e) {
    var r = frame.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }
  function setView(v) {
    var f = frameSize();
    view = _szSignClamp(v, f.w, f.h, fit);
    applyView();
  }
  frame.addEventListener('pointerdown', function (e) {
    if (!fit) return;
    clearTimeout(tapTimer);     // a box tapped, then a pinch: no search
    try { frame.setPointerCapture(e.pointerId); } catch (x) {}
    ptrs[e.pointerId] = framePoint(e);
    startGesture(Object.keys(ptrs).length === 1 ? false : true);
  });
  // A pan of the one finger down, or a pinch of the first two (moved: no
  // tap when it ends); started again whenever a finger comes or goes.
  function startGesture(moved) {
    var ids = Object.keys(ptrs);
    if (ids.length === 1) {
      gesture = { kind: 'pan', id: ids[0], start: ptrs[ids[0]], v0: view, moved: moved };
    } else if (ids.length >= 2) {
      var a = ptrs[ids[0]], b = ptrs[ids[1]];
      gesture = { kind: 'pinch', ids: [ids[0], ids[1]], d0: Math.hypot(a.x - b.x, a.y - b.y) || 1,
                  m0: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, v0: view, moved: true };
    } else {
      gesture = null;
    }
  }
  frame.addEventListener('pointermove', function (e) {
    if (!gesture || !ptrs[e.pointerId]) return;
    ptrs[e.pointerId] = framePoint(e);
    if (gesture.kind === 'pinch') {
      var a = ptrs[gesture.ids[0]], b = ptrs[gesture.ids[1]];
      if (!a || !b) return;
      var d = Math.hypot(a.x - b.x, a.y - b.y) || 1, m = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      var v = _szSignZoomAt(gesture.v0, Math.max(1, Math.min(MAX_ZOOM, gesture.v0.s * d / gesture.d0)), gesture.m0.x, gesture.m0.y);
      setView({ s: v.s, tx: v.tx + m.x - gesture.m0.x, ty: v.ty + m.y - gesture.m0.y });
    } else if (gesture.kind === 'pan' && String(e.pointerId) === gesture.id) {
      var p = ptrs[e.pointerId], dx = p.x - gesture.start.x, dy = p.y - gesture.start.y;
      if (Math.abs(dx) + Math.abs(dy) > 8) gesture.moved = true;
      if (gesture.moved) setView({ s: gesture.v0.s, tx: gesture.v0.tx + dx, ty: gesture.v0.ty + dy });
    }
  });
  function pointerUp(e) {
    var p = ptrs[e.pointerId];
    if (!p) return;
    delete ptrs[e.pointerId];
    var ids = Object.keys(ptrs);
    if (gesture && gesture.kind === 'pan' && !gesture.moved && !ids.length && e.type === 'pointerup') tapAt(p);
    startGesture(true);   // the fingers left drag or pinch on from where they are
  }
  frame.addEventListener('pointerup', pointerUp);
  frame.addEventListener('pointercancel', pointerUp);
  frame.addEventListener('wheel', function (e) {
    if (!fit || (e.deltaY > 0 && view.s <= 1.001)) return;   // out at the whole photo: the sheet scrolls
    e.preventDefault();
    var p = framePoint(e);
    setView(_szSignZoomAt(view, Math.max(1, Math.min(MAX_ZOOM, view.s * Math.exp(-e.deltaY / 300))), p.x, p.y));
  }, { passive: false });
  // A tap on a box searches its line; a double tap elsewhere zooms in there
  // (or back out to the whole frame).
  // (A box's search waits a moment for a second tap: a double tap zooms
  // there too, and does not open the keyboard first.)
  function tapAt(p) {
    var now = Date.now();
    clearTimeout(tapTimer);
    if (lastTap && now - lastTap.t < 350 && Math.hypot(p.x - lastTap.x, p.y - lastTap.y) < 30) {
      lastTap = null;
      setView(view.s > 1.01 ? { s: 1, tx: fit.ox, ty: fit.oy } : _szSignZoomAt(view, 2.5, p.x, p.y));
      return;
    }
    lastTap = { t: now, x: p.x, y: p.y };
    var c = _szSignAt(view, fit, p.x, p.y), seq = readSeq;
    var hit = sheet.getAttribute('data-sz-ocr') !== 'reading' && _szSignHit(curLines, c.x, c.y, 6 / (fit.k * view.s));
    if (hit) tapTimer = setTimeout(function () { if (seq === readSeq) searchLine(hit.text); }, 360);
  }
  // The keyboard: + and - zoom about the middle, the arrows move the photo.
  frame.addEventListener('keydown', function (e) {
    if (!fit) return;
    var f = frameSize(), step = 0.15 * Math.min(f.w, f.h), k = e.key;
    if (k === '+' || k === '=') setView(_szSignZoomAt(view, Math.min(MAX_ZOOM, view.s * 1.5), f.w / 2, f.h / 2));
    else if (k === '-' || k === '_') setView(_szSignZoomAt(view, Math.max(1, view.s / 1.5), f.w / 2, f.h / 2));
    else if (k === 'ArrowLeft') setView({ s: view.s, tx: view.tx + step, ty: view.ty });
    else if (k === 'ArrowRight') setView({ s: view.s, tx: view.tx - step, ty: view.ty });
    else if (k === 'ArrowUp') setView({ s: view.s, tx: view.tx, ty: view.ty + step });
    else if (k === 'ArrowDown') setView({ s: view.s, tx: view.tx, ty: view.ty - step });
    else return;
    e.preventDefault();
  });
  partBtn.addEventListener('click', function () {
    if (frame.hidden || !fit || !photo) return;
    var f = frameSize();
    readPart(_szSignSubPart(curPart, _szSignVisible(view, f.w, f.h, fit, canvas.width, canvas.height),
                            canvas.width, canvas.height));
  });
  wholeBtn.addEventListener('click', function () { if (photo) readPart(WHOLE); });
  if (typeof ResizeObserver === 'function') new ResizeObserver(refit).observe(frame);
  else window.addEventListener('resize', refit);

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
      // Addresses that are not the sign's go before the list is cut, and the
      // ones it confirms (its town too) come first.
      prune: function (records, words) {
        var p = plateOf(words), streets = _szSignStreets(p, words, records), places = _szSignPlaces(records, words);
        var pois = _szSignPois(records, words);
        var kind = function (r) { return r.t === 'addr' ? _szSignAddressKind(r, p, words, streets, places, pois) : ''; };
        var kept = records.map(function (r) { return { r: r, k: kind(r) }; }).filter(function (x) { return x.k !== 'drop'; });
        return kept.filter(function (x) { return x.k === 'address'; }).concat(kept.filter(function (x) { return x.k !== 'address'; }))
          .map(function (x) { return x.r; });
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
        // Where it is, on a line of its own: a photo of a hotel key or a
        // poster is of a place anywhere, and its town tells whether it can be
        // the one. Not when other places share the name: each row says where
        // it is, and one town here would read as the answer.
        var it0 = found[0] && found[0].item, comma = it0 ? String(it0.n || '').indexOf(',') : -1;
        var where = it0 && (it0.l || (it0.t === 'addr' && comma >= 0 ? String(it0.n).slice(comma + 1).trim() : ''));
        if (where && !kinds[0].several) {
          verdictEl.appendChild(document.createTextNode(' '));
          var at = document.createElement('span');
          at.className = 'ocr-verdict-where';
          at.textContent = '\ud83d\udccd ' + where;
          verdictEl.appendChild(at);
        }

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
      b.addEventListener('click', function () { searchLine(L.text); });
      linesEl.appendChild(b);
    });
  }

  function handle(f) {
    if (!f) return;
    sheet.hidden = false;
    pickRow.hidden = true;
    againBtn.hidden = false;
    linesEl.innerHTML = '';
    forgetVerdict();
    forget();
    setStatus(szT('search.photo_reading', 'Reading the photo…'), 'reading');
    var seq = ++loadSeq, rs = ++readSeq;
    loadPhoto(f, seq).then(function (kept) { if (kept && seq === loadSeq) readPart(WHOLE); },
                           function (err) { if (seq === loadSeq) failed(err, rs); });
  }
  // Reads `part` of the photo (WHOLE, or what the frame showed: "Search this
  // part") and searches its lines, as a photo of that part alone would be.
  function readPart(part) {
    frame.hidden = false;
    linesEl.innerHTML = '';
    forgetVerdict();
    forget();
    // The last photo's places are not this one's.
    resultsEl.innerHTML = '';
    resultsEl.style.display = 'none';
    setStatus(szT('search.photo_reading', 'Reading the photo…'), 'reading');
    var t0 = Date.now(), rs = ++readSeq;
    curPart = part;
    curLines = [];
    var image = drawPart(part);
    resetView();
    return readBoth(image).then(function (items) {
      if (rs !== readSeq) return;   // another photo or part since: not this sheet's
      var lines = _szSignLineGroups(items);
      curLines = lines;
      dbg('sign reader: ' + lines.length + ' lines in ' + (Date.now() - t0) + ' ms');
      outline(lines);
      if (!lines.length) {
        setStatus(szT('search.photo_none', 'No text found in the photo'), 'none');
        showZoomBar();
        return;
      }
      setStatus(szT('search.photo_lines', 'Text in the photo: tap a line to search for it'), 'lines');
      showZoomBar();
      showLines(lines);
      // Each line weighs as tall as its letters, against the sign's biggest.
      var tallest = Math.max.apply(null, lines.map(function (L) { return L.size; }));
      var weighted = _szSignWeighted(lines, tallest).concat(_szSignAddress(lines, tallest));
      // In the field: the sign's biggest line now, the line the first place
      // matched once found -- what was searched for, to edit and search as
      // typed, not everything the photo holds (a status bar, a watermark).
      var biggest = lines.filter(function (L) { return L.size === tallest; })[0].text;
      input.value = biggest;
      document.getElementById('search-clear').style.display = 'block';
      var opts = signOpts(lines);
      return map._searchSign(weighted, opts).then(function (found) {
        if (opts.photo !== photoSeq || rs !== readSeq) return;   // let go meanwhile: its answer is not shown
        var query = (found[0] && found[0].signLine) || biggest;
        input.value = query;
        keep(weighted, query, lines);
        sheet.setAttribute('data-sz-ocr', 'searched');
      });
    }).catch(function (err) { failed(err, rs); });
  }
  function failed(err, rs) {
    if (rs !== readSeq) return;
    dbg('sign reader failed', describeError(err));
    setStatus(szT('search.photo_failed', 'Could not read the photo'), 'failed');
    showZoomBar();
    // Why, in small print: on a phone there is no console to read it in.
    var why = document.createElement('small');
    why.className = 'ocr-why';
    why.textContent = describeError(err);
    statusEl.appendChild(why);
  }
  // tools/ocr_photos.mjs: a part of the photo read as "Search this part" would.
  window.__szSignReadPart = function (part) { return photo ? readPart(part) : null; };

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
