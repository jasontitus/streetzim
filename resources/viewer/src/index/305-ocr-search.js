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
// lines in reading order: boxes that overlap vertically by half the
// smaller one's height are one line, joined left to right (right to left
// for Arabic), with no space between Chinese, Japanese or Thai words. A
// box of two or more letters at least 1.5 times as tall as wide is a
// vertical line of its own (Chinese and Japanese signs: 安平區 down the
// side of a house-number plate), never joined to the lines beside it.
// Each line's size is its letters' height (a vertical line's width).
var SZ_SIGN_RTL = { Arab: 1, Hebr: 1 };
var SZ_SIGN_NOSPACE = /[฀-๿぀-ヿ㐀-䶿一-鿿豈-﫿]/;
function _szSignVertical(it) {
  return it.box.height >= 1.5 * it.box.width && Array.from(String(it.text).trim()).length >= 2;
}
function _szSignLineGroups(items) {
  var boxes = (items || []).filter(function (it) {
    return it && it.text && it.box && it.box.height > 0;
  }).slice().sort(function (a, b) {
    return (a.box.y + a.box.height / 2) - (b.box.y + b.box.height / 2);
  });
  var lines = [];
  boxes.forEach(function (it) {
    var b = it.box, line = null, vertical = _szSignVertical(it);
    for (var i = 0; i < lines.length && !line && !vertical; i++) {
      var L = lines[i];
      if (L.vertical) continue;
      var overlap = Math.min(L.y + L.height, b.y + b.height) - Math.max(L.y, b.y);
      if (overlap >= 0.5 * Math.min(L.height, b.height)) line = L;
    }
    if (!line) {
      line = { x: b.x, y: b.y, width: b.width, height: b.height, items: [], vertical: vertical };
      lines.push(line);
    } else {
      var x2 = Math.max(line.x + line.width, b.x + b.width), y2 = Math.max(line.y + line.height, b.y + b.height);
      line.x = Math.min(line.x, b.x); line.y = Math.min(line.y, b.y);
      line.width = x2 - line.x; line.height = y2 - line.y;
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
    var c = { text: it.text, confidence: it.confidence, script: it.script,
              box: { x: it.box.x * k, y: it.box.y * k, width: it.box.width * k, height: it.box.height * k } };
    if (!(fine || []).some(function (f) { return f && f.box && overlaps(f.box, c.box); })) out.push(c);
  });
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
      return _szInTurn([function () { return getText(m.worker); },
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
  function keep(lines, query) {
    last = { lines: lines, query: query };
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
    showLines(last.lines);   // the tiles, opened since, emptied them
    setStatus(szT('search.photo_lines', 'Text in the photo: tap a line to search for it'), 'lines');
    input.value = last.query;
    document.getElementById('search-clear').style.display = 'block';
    map._searchSign(last.lines).then(function () { sheet.setAttribute('data-sz-ocr', 'searched'); });
  }
  // A new photo, as _szSignPickMode says: the two tiles (the camera off in
  // Kiwix on iOS), the note to drop or paste in Kiwix on macOS, or the
  // file picker at once on a desktop.
  function pick() {
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
      });
      // In the field: the sign's biggest line now, the line the first place
      // matched once found -- what was searched for, to edit and search as
      // typed, not everything the photo holds (a status bar, a watermark).
      var biggest = lines.filter(function (L) { return L.size === tallest; })[0].text;
      input.value = biggest;
      document.getElementById('search-clear').style.display = 'block';
      return map._searchSign(weighted).then(function (found) {
        var query = (found[0] && found[0].signLine) || biggest;
        input.value = query;
        keep(weighted, query);
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
  // A picked result flies the map there; the photo stays for the way back.
  resultsEl.addEventListener('click', function (ev) {
    if (!sheet.hidden && ev.target && ev.target.closest && ev.target.closest('.search-result')) hide();
  });
  window.__szSignSearch = { read: handle, release: release };
  return true;
}
