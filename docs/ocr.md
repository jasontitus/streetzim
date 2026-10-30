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

1. **The photo.** On a phone or tablet the button opens a choice in the
   photo sheet: **Take a photo** (`<input type="file" accept="image/*"
   capture="environment">`: the camera straight away) or **Choose a photo**
   (the same input without `capture`: the library or files). On a desktop
   it opens the file picker at once. A photo can also be dropped
   anywhere on the page or pasted (an image pasted into a text field with
   text alongside stays text). The photo is scaled to at most
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
   tried with their words reversed. On a line of several words, a name of
   five letters or fewer must be one of them ("Mace" is not on a sign
   reading "Manama Central Market", though "mana-mace-ntral" holds it).
   Records are ranked by similarity (to the tenth power, so a near miss
   on screen does not beat an exact name further away), times the line's
   prominence (its letters' height against the sign's tallest: the street
   name before the town in small print), times how much of the line the
   name covers ("Manama Central Market" before "Manama"), plus a type
   bonus, then by proximity, as typed searches are. The results show in
   the search list, and the search field holds the line the first one
   matched, as read: not the photo's other text (a status bar, a
   watermark). Tapping a line in the sheet runs an ordinary search for it.
4. **Going back.** Picking a result hides the sheet but keeps the photo:
   the camera button shows it, and tapping it brings back the photo, its
   lines and its places, for when the first pick was the wrong one. The
   sheet's own camera button takes another photo; its × lets the photo go.

## What a result is

A photo's results never look surer than they are: someone who cannot read
the sign cannot check them. The labels compare whole words, in order, with
the lines as read (`_szSignKinds`, `_szSignIsAddress`, 305-ocr-search.js),
not the search's fuzzy match, which also finds a name that is part of a
line or a line glued to another. A right-to-left line may also match read
backwards (a recognizer may give its words in visual order). Each result
row says what it is, and the photo sheet says it of the first one:

| label | when |
|---|---|
| Exact address (green, first) | an address whose building number (first, as the build names addresses: "366 116", "23號 忠孝東路三段", "10 Schulweg, Husum"; "Bldng" and the like before it aside) is the sign's tallest number line ("366", "12a", "23號"), when that number belongs with a street (with 號, or on one plate with a street line that has a type word or a number, their boxes stacked with no other line between them; never on a sign with "speed", "limit", "km" and the like), and whose rest is another line that is a street of the map, word for word with its type ("Abbott Drive" is not "Abbott Avenue"; "Dr" is "Drive", "طريق" "Road"; a record naming the street by its number alone, "366 116", matches "Road 116"); or one line holding the street and the number in order, either way round, with a name in the street part ("Schulweg 10", "三和路四段20巷101號"; not "1225 Road 1029", which reads either way). Any other address is not listed; the same address in several places says so on its row and the sheet, in amber; when the sign says more than the address, it must confirm the address's town and contradict nothing: a line holding that town (the first part after the comma; 臺 and 台 alike) or an area of the map that holds the address (inside its box; near the point of a town, 1 to 8 km by its size) confirms; an area that does not ("San Jose" against Campbell, "20095 Hamburg" against Husum), a postcode line or a short name ending in 市, 區, 縣 contradicts; a line the map knows nothing of ("Denver", a city it does not cover) leaves it only to check unless another line confirms; a phone, web or mail line, a place of the map (a hotel) or a country known by its point alone changes nothing |
| Street only: no address N found | a street whose name is one line of the sign, when the sign has a building number beside a numbered street ("366" over "Road 116") or with 號, and that address was not found. Found, not "in the map": an address written another way may be missed. Most of Bahrain has no building addresses in any open data |
| Street only, not the address | the same street when its address was found, or when the sign has a number line that may not be a building (a speed limit beside "Königstraße") |
| Area, not an exact spot | a town, district or other area whose name is one line of the sign |
| Same name as on the sign | anything else whose name (or own-script name, or another language's) is one line of the sign, on a sign with no number line (a street sign, a shop, a station) |
| Not an exact match: check the sign | everything else: a name read inexactly, part of a line ("Grand Mosque" on "Al Noor Grand Mosque"), a line glued to another ("Road 1012" from "Road 101" and "2"), the line's words in another order, an address whose number may not be the building's (a "30" under "Schulweg": a speed limit?) |

On the sheet, "Other places have this name: the sign doesn't say which"
when another record of the first one's name is more than 150 m away (2 km
for a street) among every record found; for an address, the same address
before its place ("10 Schulweg" in 692 German towns). The sheet is green
only for an exact address that is the only one, and says where its first
result is (its town or locality), for a photo of a place elsewhere. A line with no letters
matches no record by itself, and an address is weighed only when its first
number is on the sign. The labels belong to the photo: a line tapped, the
field cleared, a new photo or a later search takes them away.

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
| Kiwix for iOS | the photo library only. The app has no camera permission (no `NSCameraUsageDescription` in its Info.plist), and iOS closes an app that opens the camera without one: the file picker's Take Photo crashed Kiwix (reported 2026-10-28). So there (the `zim:` scheme on an iPhone or iPad, the `sign-env` block) the camera's choice shows Take a photo off, with a note saying why, and Choose a photo as the blue one (`_szSignPickMode`). The fix belongs in the app: one line, `INFOPLIST_KEY_NSCameraUsageDescription` in kiwix-apple's `project.yml` |
| Kiwix for macOS | drop a photo on the map, or paste one with ⌘V. The app does not answer the web view's open-panel request (`WKUIDelegate` `runOpenPanelWith`, absent from kiwix-apple as of 2026-10-23), so the file picker never opens: the camera button first shows a note saying so (the `zim:` scheme on a Mac, `sign-env`). The fix belongs in the app |
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
  input and checks the place comes out first and the search field holds
  its line; then that picking it hides the sheet, moves the map and leaves
  the photo on the camera button, which brings the photo and its places
  back, with its lines, after the sheet's camera opened the tiles; that
  photos given by Take a photo's camera input (while the tiles are open,
  which must give way), by Choose a photo's input, dropped on the map and
  pasted are all read, in turn; and that the camera's choice is right on a
  phone (both tiles, Take a photo blue, each opening its own input), in
  Kiwix on iOS (Take a photo off) and on macOS (the drop note, no tiles). `NAMES` photographs given text instead
  ("text=>record name" for a sign in another script than the record's
  name). With `SCRIPT` and `QUERIES` it photographs the own-script names
  of places the map's search finds: CI runs it on Manama
  (`--bbox 50.50,26.15,50.64,26.27`, which gets the `latin-cjk` and
  `arabic` recognizers) with Arabic signs of streets, mosques and markets.
