/* StreetZim sign reader: text detection and recognition off the main
   thread (docs/ocr.md). The builder writes this file into the ZIM as
   ocr/ocr-worker.js with --ocr (streetzim/ocr.py). The page
   (305-ocr-search.js) fetches every file itself -- this worker runs from a
   blob: URL, as routing-worker.js does, and a ZIM reader may not serve a
   worker's own requests -- and posts:
     {type: 'load', engine, wasm, det: {model, preset}, rec: [{script, model, dict, reverse}]}
     {type: 'read', id, image: {width, height, data}}
   engine is the text of ocr/ocr-engine.js (ONNX Runtime Web and the
   paddleocr runtime, vendored: tools/pin_viewer_assets.py --ocr), wasm the
   bytes of ONNX Runtime's WebAssembly, the models' bytes and dictionaries
   as ocr/manifest.json names them. It answers {type: 'loaded'} (with
   error on failure), then {id, lines: [{text, confidence, box, script,
   readings}]} per photo, one per text box the detector found, or {id,
   error}: the most confident reading, and in `readings` every recognizer's
   readable one ({text, confidence, script}), which the page puts together
   where they read different parts of a line (_szSignCompose: the digits
   of "2912 طريق" from the Latin model, the word from the Arabic one).
   The page runs its own copy of this file (tools/build_viewer.py inlines it
   as part 026), so a viewer swap updates it; ocr/ocr-worker.js in the ZIM
   is what an older page runs. */
/* global SZOCR */
'use strict';
var engine = null;   // {det, recs: [{script, svc}]} once loaded
var loading = null;  // Promise of engine

function load(m) {
  var url = URL.createObjectURL(new Blob([m.engine], { type: 'application/javascript' }));
  try { self.importScripts(url); } finally { URL.revokeObjectURL(url); }
  var ort = SZOCR.ort;
  ort.env.wasm.numThreads = 1;        // no cross-origin isolation in a ZIM reader
  ort.env.wasm.wasmBinary = m.wasm;   // compiled from these bytes, never fetched
  ort.env.logLevel = 'error';         // not the models' unused-initializer warnings
  var opts = { executionProviders: ['wasm'], graphOptimizationLevel: 'all' };
  var preset = SZOCR.getModelPresetOptions(m.det.preset);
  var det = ort.InferenceSession.create(new Uint8Array(m.det.model), opts).then(function (s) {
    return new SZOCR.DetectionService(ort, s, Object.assign({}, preset.detection, m.det.options || {}));
  });
  // One session at a time: creating them all at once peaks higher.
  return det.then(function (d) {
    var recs = [];
    return m.rec.reduce(function (p, r) {
      return p.then(function () {
        return ort.InferenceSession.create(new Uint8Array(r.model), opts).then(function (s) {
          var dict = String(r.dict).replace(/\n$/, '').split('\n').concat([' ']);
          recs.push({ script: r.script, svc: new SZOCR.RecognitionService(ort, s,
            Object.assign({}, preset.recognition, { charactersDictionary: dict, reverseText: !!r.reverse })) });
        });
      });
    }, Promise.resolve()).then(function () { return { det: d, recs: recs }; });
  });
}

function boxKey(box) { return JSON.stringify(box); }

// A reading worth keeping: some letter or digit. A recognizer shown text
// in a script it does not know may answer spaces or punctuation, and do it
// confidently (PP-OCRv6's Latin/CJK model on Cyrillic): that must never
// beat the recognizer that read the letters.
var READABLE = /[\p{L}\p{N}]/u;

// Each detected box is read by every recognizer; the most confident
// readable reading wins, and every readable one goes along with it.
function read(e, input) {
  var image = SZOCR.normalizeInputToRgb(input);
  return e.det.run(image).then(function (boxes) {
    var best = {}, all = {};
    return e.recs.reduce(function (p, r) {
      return p.then(function () {
        if (!boxes.length) return null;
        return r.svc.run(image, boxes, { ordering: { sortByReadingOrder: false } }).then(function (res) {
          for (var i = 0; i < res.length; i++) {
            var k = boxKey(res[i].box), cur = best[k];
            if (!READABLE.test(res[i].text || '')) continue;
            var reading = { text: res[i].text.trim(), confidence: res[i].confidence, script: r.script };
            (all[k] = all[k] || []).push(reading);
            if (!cur || res[i].confidence > cur.confidence) {
              best[k] = { text: reading.text, confidence: reading.confidence, box: res[i].box, script: r.script };
            }
          }
        });
      });
    }, Promise.resolve()).then(function () {
      return boxes.map(function (b) {
        var k = boxKey(b);
        if (best[k]) best[k].readings = all[k];
        return best[k];
      }).filter(Boolean);
    });
  });
}

self.onmessage = function (ev) {
  var m = ev.data || {};
  if (m.type === 'load') {
    try { loading = load(m); } catch (err) { loading = Promise.reject(err); }
    loading.then(function (e) { engine = e; self.postMessage({ type: 'loaded' }); },
      function (err) { loading = null; self.postMessage({ type: 'loaded', error: String(err && err.message || err) }); });
  } else if (m.type === 'read') {
    (engine ? Promise.resolve(engine) : loading || Promise.reject(new Error('not loaded')))
      .then(function (e) { return read(e, m.image); })
      .then(function (lines) { self.postMessage({ id: m.id, lines: lines }); },
        function (err) { self.postMessage({ id: m.id, error: String(err && err.message || err) }); });
  }
};
