# Search with a photo (the sign reader, `--ocr`)

A map built with `--ocr` has a camera button in its search box. Photograph
a street sign, a shop front, an advert or a landmark's name plate (or pick a
photo already taken) and the viewer reads its text, offline, and searches
the map for it: the place comes out at the top of the results, and every line
of text read is listed under the photo to search for by hand. It reads
Chinese, Japanese, Korean, Thai, Arabic and Persian, Cyrillic, Greek,
Devanagari, Tamil and Telugu as well as Latin.

```
python create_osm_zim.py --area monaco --ocr -o osm-monaco.zim
streetzim --area japan --ocr ...                   # the same flag
python create_osm_zim.py ... --ocr --ocr-scripts Thai,Latn   # choose the scripts
```

It is off by default: it adds the text-recognition models to the ZIM (below).
Older ZIMs show no button. In-place viewer patching cannot add the
models (docs/viewer-slots.md), so a ZIM gets the reader only when it is
built with the flag.

## What a map gets

| part | ZIM entry | size | where it comes from |
|---|---|---|---|
| worker | `ocr/ocr-worker.js` | 4 KB | `resources/viewer/ocr-worker.js` |
| engine | `ocr/ocr-engine.js` | 117 KB | ONNX Runtime Web 1.31.1 (WebAssembly build) and paddleocr 1.2.0, bundled; vendored in `resources/vendor/ocr-engine/` |
| runtime | `ocr/ort-wasm-simd-threaded.wasm` | 14.8 MB (2.6 MB compressed in the ZIM) | ONNX Runtime Web, fetched at build time |
| text detector | `ocr/det.onnx` | 1.8 MB | PP-OCRv6 tiny detection |
| one recognizer per script the map's names use | `ocr/<name>.onnx` + `ocr/<name>.txt` | 7.6–21 MB each | below |
| manifest, licences | `ocr/manifest.json`, `ocr/LICENSES.txt` | | written by the builder |

`map-config.json`'s `ocr` key names the manifest; the viewer shows the button
only when it is there.

| recognizer | reads (ISO 15924) | model | size |
|---|---|---|---|
| `latin-cjk` | Latin, Chinese (simplified and traditional), Japanese | PP-OCRv6 small | 21.2 MB |
| `korean` | Hangul | PP-OCRv5 mobile, Korean | 13.4 MB |
| `thai` | Thai | PP-OCRv5 mobile, Thai | 7.9 MB |
| `arabic` | Arabic, Persian, Urdu, Pashto (Naskh lettering) | PP-OCRv5 mobile, Arabic | 8.0 MB |
| `cyrillic` | Cyrillic (Russian, Ukrainian, Serbian, Bulgarian, Kazakh, Mongolian, ...) | PP-OCRv5 mobile, Cyrillic | 8.0 MB |
| `greek` | Greek | PP-OCRv5 mobile, Greek | 7.8 MB |
| `devanagari` | Devanagari (Hindi, Nepali, Marathi, ...) | PP-OCRv5 mobile, Devanagari | 7.9 MB |
| `tamil` | Tamil | PP-OCRv5 mobile, Tamil | 7.9 MB |
| `telugu` | Telugu | PP-OCRv5 mobile, Telugu | 7.9 MB |

Every recognizer except `latin-cjk` also reads unaccented Latin, so the
English line of a bilingual sign is read in any of them.

**Which recognizers.** `streetzim/ocr.py` counts the scripts of the map's
own names: each search record's `name_native`, else its `name`, for the
first 200,000 records. A script gets its recognizer when at least 5% of
the names use it, or when it is the most used. Latin needs 25% when another
script leads, because the other recognizers read plain Latin already: brand
names in Bangkok do not add 21 MB to Thailand. Scripts with no recognizer
(Hebrew, Georgian, Armenian, Ethiopic, Bengali, Gurmukhi, Gujarati, Oriya,
Kannada, Malayalam, Sinhala, Khmer, Lao, Burmese, Tibetan, Thaana,
Tifinagh) are named in the build log. Signs in them are read only where they
also carry Latin text. `--ocr-scripts` replaces the count with a list.
The models barely compress (about 10%). Monaco gets `latin-cjk`: its ZIM
grows from 2.9 MB to 25.6 MB. Each other recognizer adds about 7 MB (Korean
12 MB).

## How it works

1. **The photo.** The button opens the device's file picker
   (`<input type="file" accept="image/*">`, no `capture`), which on a phone
   offers the camera and the photo library. The photo is scaled to at most
   1600 px on its longest side and drawn on the sheet that opens over the
   map.
2. **Reading.** At the first photo the page fetches the worker, the engine,
   the runtime and the models named by the manifest (through the zimtile
   retry helper, as Kiwix drops requests under load) and starts the worker
   from a `blob:` URL, as the routing worker is. The worker fetches
   nothing: ONNX Runtime compiles the WebAssembly from the bytes it is
   given. It runs on one thread, since a ZIM reader gives no cross-origin
   isolation. The detector finds the text boxes in the photo (input scaled
   to 960 px at most); each box is read by every recognizer and the most
   confident reading is kept. The page groups the boxes into lines in
   reading order (right to left for Arabic; no space between Chinese,
   Japanese or Thai words). The worker is stopped a minute after the last
   photo, which gives its memory back.
3. **Searching.** `map._searchSign` (300-search.js) reads the search
   index leaves for each line word's first two letters, so an OCR slip
   later in a word still finds the leaf. It keeps the records one of whose
   names (`n`, `nn`, `nl`, `alt`, `ln`) is close to part of a line: the
   edits needed to find the name in the line are at most a quarter of its
   letters, names of three letters or fewer must be exact, and a name's
   numbers must be on the line whole ("Road 7" and "Road 717" are not
   "Road 719"). Names and lines are compared folded as the search folds
   them (accents and other combining marks off),
   with letters that only differ by code point made alike (Arabic and
   Persian yeh and kaf, ɓ/ɗ/ƙ and b/d/k, ...). Right-to-left lines are also
   tried with their words reversed. Records are ranked by similarity (to
   the tenth power, so a near miss on screen does not beat an exact name
   further away), times the line's prominence (its letters' height against
   the sign's tallest: the street name before the town in small print),
   plus a type bonus, then by proximity, as typed searches are. The
   results show in the search list; picking one closes the photo sheet.
   Tapping a line in the sheet runs an ordinary search for it.

## Choosing the models

Measured in headless Chromium with ONNX Runtime Web on one thread. The test
set was 8 sign names per language, each drawn as a sign in five ways:
clean; tilted with uneven light, noise and JPEG; small in a cluttered
scene; and both of those cropped to the sign. That is 40 photos per
language, and two real photos (a Beijing hotel signpost, a Cairo street
plate). "Found" means the search would find the name: its exact text after
the search's fold and the letter-variant fold.

**Paddle, not Tesseract.** Tesseract.js 6 found 5–70% of names (most
scripts 15–45%; it reads clean signs and fails on photos), against 78–100%
for PaddleOCR in every script both support. On the real photos Tesseract
read nothing usable. It is also missing from the scripts PaddleOCR lacks:
it read 0–52% there. Tesseract used about 150 MB less memory at peak.

**Detector: PP-OCRv6 tiny at v5's input size.** On 15 languages it matched
or beat PP-OCRv5 mobile: Russian 88% → 100%, Persian 92% → 100%, Hindi 85% →
92%, Turkish 95% → 100%, the rest equal. It ran 0.37–0.42 s a photo against
0.62–0.63 s, with a peak 30–40 MB lower, and its file is 1.8 MB against
4.8 MB. With v6's own input size (shortest side enlarged to at least
736 px) the same model was slower and peaked 150–350 MB higher, and v6
small was worse still (Arabic 92%, Japanese 92%).

**Recognizers.** PP-OCRv6 small reads Latin and CJK as well as the
best PP-OCRv5 model in each (100% on German, Chinese, Japanese, traditional
Chinese, Turkish) and better where PP-OCRv5's Latin model lacks a
letter (Azerbaijani ə: 22% → 100%). PP-OCRv6 tiny is weaker (Azerbaijani
12%, Turkish 92%). PP-OCRv6 has no other scripts, so they use PP-OCRv5's
language models. PP-OCRv5 Kannada does not exist and the PP-OCRv3 one read
15% (Tesseract 45%), so Kannada has no recognizer. Converting the models to
8-bit integers cut their files by three quarters but broke them (0–9 of 12
found) and saved no memory.

**Cost on a phone.** The worker adds about 290 MB at its peak while reading a
photo with one recognizer (about 160 MB once loaded), and 0.4 s a photo on
the test machine's CPU; a phone is perhaps 2–3 times slower. The first
photo also loads the models (about 1–2 s). The memory is held only while the
worker lives.

**Gaps.** Names whose letters a recognizer lacks lose them:
Vietnamese stacked tone marks (ườ, ễ, ệ) and Yoruba underdots
(ọ, ẹ, ṣ) are dropped, so "Đường Nguyễn Huệ" reads "Đưng Nguyn Hu".
The fuzzy match still finds most of these. The Tamil model misreads the
vowel sign ை (drawn before its consonant) even on a clean sign:
"அண்ணா சாலை" reads "அண்ணா சா2ல", just found (75% similar). Urdu in Nastaliq lettering
(most Pakistani signs) is not read by any engine we tried; Urdu in Naskh is.

## Where it works

| reader | camera / photo |
|---|---|
| a browser (kiwix-serve, the `/drive/` PWA, kiwix-js) | yes: camera and photo library on a phone, file picker on a desktop |
| Kiwix for iOS | the file picker's photo library; the app does not ask for the camera permission (no `NSCameraUsageDescription`), so taking a photo there depends on the app |
| Kiwix for Android | no: its WebView opens no file chooser, so the viewer hides the button there (an Android WebView's user agent says `; wv)`) |

## Pinning and updating

`resources/viewer-assets.lock.json` pins everything
(`tools/pin_viewer_assets.py --ocr`, docs/viewer-supply-chain.md):

- `ocr-engine`: the npm tarballs of onnxruntime-web and paddleocr (checked
  against the registry's sha512), the esbuild version that bundles them
  into `resources/vendor/ocr-engine/ocr-engine.js` (a classic script setting
  `self.SZOCR`; the build is reproducible), the bundle's and licences'
  SHA-256, and the WebAssembly file's URL and SHA-256 (fetched at build
  time, cached with the font glyphs).
- `ocr-models`: each model of `OCR_MODELS` (in the tool) by Hugging Face
  repository, commit, URL and SHA-256, fetched at build time and cached;
  the dictionaries, read from each model's `inference.yml`, vendored in
  `resources/vendor/ocr-models/` with PaddleOCR's licence (Apache-2.0).

`--check` (CI) verifies the vendored files and that the lock names the tool's
models. A build with `--ocr` fetches the runtime and detector when it starts,
so a missing network fails in seconds; the recognizers come at the end, once
the names are known. `--prefetch-ocr DIR` fills a cache with all of them for
offline builds. To change a model, edit `OCR_MODELS` and run `--ocr`.

## Tests

- `tests/test_ocr.py`: script counting, model choice, the entries and
  manifest, map-config, the lock, and the flag's way to `create_zim`.
- `tests/viewer_ocr_js.test.mjs`: line grouping and the fuzzy match, as
  they ship.
- `tools/ocr_scripts_check.mjs` (CI): every recognizer reads its script.
  Real place names in each (Chinese, Japanese, Korean, Thai, Arabic,
  Persian, Cyrillic, Greek, Devanagari, Tamil, Telugu, Latin) are drawn as
  signs, a clean plate and a rough photo with other text and small print,
  read by the worker and models as a ZIM ships them (each script's
  recognizer loaded beside the Latin/CJK one, as a map gets them), and
  matched by the viewer's own code. It needs no map:
  `python -m streetzim.ocr export DIR` writes the `ocr/` files with every
  recognizer. It found a recognizer answering blank text confidently for a
  script it does not read (now ignored by the worker).
- `tools/ocr_check.mjs` (CI, on the Monaco ZIM built with `--ocr`):
  draws photos of signs of the map's own streets and shops (tilted, noisy,
  with other text and the town in small print), gives them to the file
  input and checks the place comes out first, then that picking it closes
  the sheet and moves the map. `NAMES` photographs given text instead
  ("text=>record name" for a sign in another script than the record's
  name). With `SCRIPT` and `QUERIES` it photographs the own-script names
  of places the map's search finds: CI runs it on Manama
  (`--bbox 50.50,26.15,50.64,26.27`, which gets the `latin-cjk` and
  `arabic` recognizers) with Arabic signs of streets, mosques and markets.
