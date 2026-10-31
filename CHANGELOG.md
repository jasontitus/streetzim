# Changelog

What StreetZim can do and when it arrived, newest first, grouped by month and
by feature area rather than by commit. Dates (YYYY-MM-DD) are when the work was
committed; a few items reached `main` a day or two later through a merge. The hashes after each item are its key commits; `git show <hash>`
has the details. Fixes are listed when someone using the maps would have
noticed them.

## At a glance

| Period | What arrived |
|---|---|
| October 2026 | Search with a photo (offline sign reading, `--ocr`), with results that say what they are, zoom into the photo, Russian and Ukrainian plates and city maps to try it on; the library page; maps with today's viewer without a new upload; walking and cycling routing with turn restrictions; maps in other languages, German first; border clip; search in Indic, Thai and native scripts; builds that fit a 16 GB container |
| Late September 2026 | The `streetzim` command and Docker image for Zimfarm; dark theme, POI icons, right-to-left and more scripts; search for administrative areas; maps open on the region's main city; areas across the antimeridian |
| Early September 2026 | The 2026-09 data refresh; blank terrain fixed at its root; search and Find sharded for locality; Wikipedia articles with pictures; Preview streams any map into the browser; viewer slots; 11 European regional ZIMs and Light variants |
| May to August 2026 | Single-pass build; Find as a maps app (chip rail, carousel, place sheet); routing in a Web Worker; bundled Wikipedia articles; about 22 new regions; per-region torrents |
| April 2026 | Offline routing and Drive mode; Overture addresses and places; the Find page; archive.org, the streetzim.web.app catalog and the /drive PWA; the first published regions; a validator and a browser smoke test |
| March 2026 | Vector maps in a ZIM; offline search; 3D terrain; satellite imagery; Wikidata popups |

## Late October 2026

**Highlights:** search with a photo grew from reading a sign to telling a
traveller what it found: every result says whether it is the exact address,
the street, an area or only a similar name, the photo can be zoomed and part
of it searched, and Russian and Ukrainian plates are read; small city maps in
eight scripts to try it on; a library page in the Kiwix library's style; and
maps that carry today's viewer without a new upload.

### Viewer
- About names the viewer a map carries, "viewer: streetzim 1.0.0 (de480cd1)": the first 8 hex digits of the SHA-256 of the viewer's three files, so a phone's older copy of a map can be told from one with today's viewer (2026-10-30; ce28a92, 3fc7d2b)
- "Search this area" sits just below the search box instead of hiding it until the results close, and hides where it would cover the dropdown or has no room; the result strip no longer scrolls the page, which on a phone could push the search bar off screen with no way back (2026-10-29; 9cf14fa, 5590ad0)
- **The view is in the URL:** each move the reader makes writes `#map=zoom/lat/lon` into the address (no history entry), and a map opened with one starts there instead of flying in from its opening view. A bookmark, a reload or a reader around the viewer gets the same view back; the fragment is the supported way to drive the map from outside (docs/in-zim-apps.md) (2026-10-28)
- The Satellite button keeps its name and is lit while satellite shows, like 3D and Wiki. It used to read "Map", lit up over the satellite picture. The layer buttons report their state to screen readers (2026-10-28)
- While the search dropdown covers the Satellite / 3D / Wiki row (all of it on a 568x320 landscape phone, a strip in portrait), the row steps aside instead of showing through under the results (2026-10-28)

### Building
- `streetzim` downloads on Python 3.12 (no zimscraperlib) try a timeout, a dropped connection, a 429 or a 5xx again, and on any Python a body that ends early, three tries in all, 5 and then 10 s apart, resuming where the server allows; CI's Monaco build failed on a Geofabrik read timeout now and then. A server that cannot be reached at all is not waited on (2026-10-28)

### Search
- **Search with a photo** (`--ocr`, off by default): a camera button in the search box reads the text of a photo (a street sign, a shop front, an advert, a landmark's plate) offline with PaddleOCR models on ONNX Runtime's WebAssembly, in a worker, and searches the map for it with a fuzzy match that survives OCR slips; the lines read are listed under the photo to search for by hand. The builder adds a PP-OCRv6 tiny detector and a recognizer per script the map's names use (PP-OCRv6 small for Latin and CJK; PP-OCRv5 for Korean, Thai, Arabic, Cyrillic, Greek, Devanagari, Tamil, Telugu), pinned in the lock file; Monaco grows from 2.9 to 25.6 MB. Chosen by measurement across 42 languages against Tesseract.js and the other PP-OCR models (docs/ocr.md) (2026-10-28)
- Fixed: on a map served by kiwix-serve, the first photo could wait 90-115 s for the reader's files (a third of the time). The viewer asked for all of them at once beside the map's tiles, and kiwix-serve 3.8.2 sometimes held them. It now fetches them one at a time: 20 of 20 first photos read in 12-14 s (2026-10-28)
- The camera button opens a choice: on a phone or tablet, **Take a photo** (the camera straight away) or **Choose a photo**, side by side; in Kiwix on iPhone, where taking a photo closes the app, Take a photo is shown off and Choose a photo comes first; in Kiwix on a Mac, a note to drag or paste one; on a desktop the file picker opens at once, as before (2026-10-29)
- **Every photo result says what it is:** Exact address (green), Street only (with whether the address was found), Area, not an exact spot, Same name as on the sign, or Not an exact match: check the sign; the sheet says it of the first result, where that result is (📍 Manama) and when other places share the name ("the sign doesn't say which"). The labels compare whole words in order with the lines read, not the search's fuzzy match. An address is exact only when its building number belongs with a street of the map (on one plate, stacked, with no speed sign; or street and number in order on one line), no other line looks like another building's number, and, when the sign says more than the address, the sign confirms its town ("San Jose" or "20095 Hamburg" against an address elsewhere leaves it to check). Street types count ("Abbott Dr" is "Abbott Drive", never "Abbott Ave"; "طريق 105" is "Road 105"). Near misses of 3 letters or fewer are not listed; a short name read whole is a name ("KFC", "全家"), but "MT" from a logo is only to check. Someone who cannot read the sign cannot check the results, so they never look surer than they are (docs/ocr.md, "What a result is") (2026-10-30 to 10-31; 925e312, f2a87db, 1eca7e9, 7f163db, 3472942, 6c381b5, d888289, 8e682ca, 9b60f81, 70c4094, fabc15d)
- **Zoom into the photo and search part of it:** the photo has a frame of its own; a pinch, a double tap, the mouse wheel, or + and − and the arrow keys zoom in and move it, and **Search this part** reads only the part shown, enlarged up to twice, so a plate among other text or small lettering is read by itself; **Whole photo** goes back. Tapping a word's box searches its line (a double tap zooms instead) (2026-10-31; d1a04df, 9c1c0c3, 4261b86)
- **Russian and Ukrainian street plates:** ул., вул. and вулиця are one street type, as are пер. and провулок, узвіз and спуск, and the rest; Ukrainian і, є, ґ compare as и, е, г; a Cyrillic word read with Latin or Greek look-alike letters ("BYЛИЦЯ", "КОСbIГИНА") is read as Cyrillic; the type may stand on its own line (a Moscow plate's "УЛИЦА" under "БАСОВСКАЯ 2") or anywhere in the address's line, and still counts read with one letter wrong ("УЛУЦА"); a house letter may be Cyrillic ("12-А"); the neighbours' numbers in small print ("◂ 80", "12 ▸") are not another building's; a street's name is also searched without its type when that is most of the line ("КРАСИЛИВСЬКА" finds Красилівська вулиця). These rules apply to Cyrillic streets only: a test checks that addresses in other scripts get production's answers. On the Moscow and Kyiv photos, a plate framed with Search this part is the exact address "2 улица Косыгина" where nothing was found before, and Krasylivska Street and Desiatynnyi Lane are found (2026-10-31; 10094da, 5a45ad8, 28a644c)
- On a Cyrillic line, a word joins the line only by overlapping one of its words with letters, not the line's whole outline: a house number two rows tall beside a Moscow plate's name no longer glues the row under it to the name. Other scripts keep the outline, which holds a tilted "خیابان ۱۵ خرداد" or "Via 4 Novembre" together. A Latin brand on a Russian sign ("OBI", read as Cyrillic "ОВІ") is searched as read too (2026-10-31; 5a45ad8, 4f7d1ae)
- A letter with a full stop after a house number is an initial or an abbreviation, not the house's letter: "12 Λ. Κηφισίας" (Λεωφόρος) and "12 N. Main Street" are exact against "Λ. ΚΗΦΙΣΙΑΣ 12" and "N. MAIN STREET 12" (2026-10-31; 4f7d1ae)
- Big lettering and vertical lines are read: the photo is read a second time at 384 px, where letters a sixth of the photo tall or more are the size the detector finds, and a tall, narrow box is a vertical line of its own (a Tainan plate's street and the district down its side). Address plates are read whole: each recognizer's reading of a box is kept, and a box read best as a number takes the other reading's words ("طريق 2912", as the map names the road); Arabic-Indic and Persian digits read as 0-9 on both sides; a tilted street sign stays one line (2026-10-29; 0e0559c, 2f067c4, 2947c38)
- "Could not read the photo" says why, in small print under it: a phone has no console to look in (2026-10-29; 2d53475)

### Regions & published ZIMs
- **City maps to try search with a photo in each script:** Taipei (Chinese, with about 2.4 M Overture addresses), Osaka (Japanese), Tehran (Persian), Cairo (Arabic), Athens (Greek), Moscow (Russian), Kyiv (Ukrainian) and Seoul (Korean). Each is cut from its country's extract, builds and downloads in a fraction of its country's time, and carries the recognizers its names' scripts need; the countries' maps were built before the reader (2026-10-29 to 10-31; 5eb5515, 9fcf792, e40595f)

### Website & distribution
- **The library** (`/library/`), a second way into the maps laid out like the Kiwix library: a language picker, then continent, UN M49 region, country and area, a search over all of it, and one Download per card, the archive.org file or the language version. The filters live in the address, so a filtered view can be shared; on a phone the language, the search and one Places button come first (2026-10-29; 2a4cd69, eb5e8e5)
- **The builder page in three steps:** the region, the language, and Download, with the file's name and the viewer code its About shows above the button; the rest folds away. On a phone the language version downloads as one ordinary file from the build worker, after a check that the worker can serve it: the browser's own download keeps going in the background and can resume, where Firefox on Android stopped a file built in the page (2026-10-29 to 10-30; 7102dcc, d56bd85, 185a56c)
- **Maps with today's viewer without a new upload:** `tools/langpack/fresh_viewer.py` composes, from a map's live archive.org file (its MD5 and ETag checked), the map with the current viewer, a fresh UUID so Kiwix sees a new book, gated as Kiwix runs it (routing included); the build worker serves it from archive.org's bytes and half a MB of its own. The site's Download points at it, and falls back to archive.org's file when the worker cannot serve it. `ops/langpack/fresh_all.py` does every live map and deploys (docs/fresh-viewer.md) (2026-10-29 to 10-30; 56cdf4b, 43ac661, 13b35d3)
- **Language versions carry today's viewer:** their file names end in `.v<code>`, the code About shows, and a viewer merged while the language queue runs ships every map's versions again, only their recipes redone (2026-10-30; 79e51c7)
- Fixed: no merged language version suggested the map's places in Kiwix's search (Bahrain's English version found nothing for "Manama"; the gates had held back 13 of its 23 versions). Every page and redirect of the base now goes in the title index, and every published map ships again (2026-10-29; f85142a, 0b44e04)
- Fixed: a viewer swap wrote every page as no front article, so Kiwix counted no articles in the map (Taipei had 5,276 of its 5,963 pages front before a swap and none after). Swaps keep each entry's flag and give the build's flags back to maps an earlier swap left without (2026-10-29 to 10-30; 739eac6, d798fe6, 73cd4ab)

### Quality
- The sign reader is scored on real photos: `tools/ocr_photos.py` runs a labelled photo set (docs/ocr-test-set.md) through each photo's own map, served with today's viewer and the reader's models on top, so a map built before the reader is scored as one built now; it scores the lines read, the line searched and the place among the first results, by name or by coordinates (2026-10-29 to 10-30; b75e4de, 24562d7, fb4b71b, 42a0132)
- python-libzim is pinned below 4: 4.0, on PyPI from 2026-10-29, renamed functions and reordered arguments, which turned CI red and stopped the Docker image's publication (2026-10-31; 3a2a82a)

## October 2026 (to 10-14)

**Highlights:** walking and cycling routing with turn restrictions; StreetZim in other languages (German first: per-language builds, a translated viewer, a German site page and three German Switzerland ZIMs); border clip, cutting builds to a country's outline; search for Indic, Thai and native-script names, and far smaller CJK search manifests; builds that fit a 16 GB container.

### Maps & tiles
- **The Docker image builds tilemaker 3.2.0** (was 3.0.0), the version that made the world tiles every published ZIM uses, so a Docker or Zimfarm build of a region now gets the same kind of tiles: a large polygon's label (an island, a glacier) is no longer copied into every tile under it, points with the same name and kind in a tile become one MultiPoint (search still makes one result per point), and cities and peaks are thinned at low zoom. Names written as several values (`name:zh=洛杉矶;洛杉磯`) keep the first, and `name:zh` prefers OSM's `name:zh-Hans` (2026-10-14; 275236c, a937153)
- **Trails in Light and Ultralight maps:** builds capped at zoom 13 copy each z13 tile's z14 footways, cycleways, bridleways, paths, steps and pedestrian ways, with their names, into the z13 tile, so the map draws trails again (routing already used them). Switzerland: 1.7 M paths in 11,000 tiles; the German border-cut Ultralight grows 3.7%. `--no-fold-paths` (Zimfarm "No paths at zoom 13") leaves them out (2026-10-13; faf7151, 8ff6a0d, 98fa004)
- **Border clip.** A build can be cut to a region's outline (a `.poly`, widened by `--clip-buffer-km`, default 10 km) instead of its bounding box: map tiles above `--clip-min-zoom`, satellite and terrain tiles, search records, every osmium extract and the Wikidata scan stop at it, and road pieces left past the border are closed to cars, so a route never snaps onto a road it cannot reach (the viewer says when a route starts or ends 2 km or more from the picked point). Outside the border the viewer shows zoom-10 context under a grey mask, and the terrain-coverage gate no longer counts clipped-away tiles as missing land. Outlines across the antimeridian are refused (2026-10-09; b93edf4, f6177af, b9b5680)

### Search
- Smaller search manifests for Chinese, Japanese and Korean regions: small neighbouring index files are grouped by character range. China's manifest went from 6.47 MB (over the validator's 4 MB limit) to 0.94 MB; older viewers still find every record (2026-10-03; ad2a619, e983835, b8c047a)
- Places can be found by their name in their own script as well as in English ("Peking University" / 北京大学). Results show both names, and Kiwix pages are titled like "Haidian · 海淀区" (2026-10-05; 755530d, c710dc8)
- Fixed: search in Indic and Thai scripts. The viewer folded names differently from the index writer (it stripped vowel signs the writer keeps), so it looked for index files that were never written: "พัทยา" (Pattaya) found nothing on Southeast Asia. The viewer now uses the writer's fold, generated from a Unicode table (2026-10-01; dab2fce, 77b9409)
- Vowel signs continue a word instead of splitting it in the search index: typing พัทยา went from 70 index files (7.1 MB) per keystroke to 3 (2.4 MB) with the same top results. The index records which rule it was built with, so older ZIMs are read as before (2026-10-01; b9ee937, 2881635)
- Fixed: Kiwix's own search returned dead links in every `--xapian=builder` ZIM built since 2026-05-08 (kiwix-serve `/suggest` 404s, `/search` 500s). Builder mode now writes and indexes the same `search/` pages as libzim mode, Wikipedia articles included (2026-10-02; 82b8f62, b76358d, 21f65b9)
- Search retrofits for published ZIMs without a rebuild (`swap_viewer_rust.py`): `--rebuild-search` re-keys the index under the new word rule (all 26,732,284 Southeast Asia records recovered), `--add-admin-areas` adds administrative-area search and `--rebuild-xapian` repairs Kiwix's search in older builder-mode ZIMs (2026-10-01; dfb9bbf, 8d83bf3, e11812b, 86ec391)

### Places & Wikipedia/Wikidata
- Fixed: maps without Wikipedia articles requested the missing `wiki-geo-index.json` 12 times on load. The writer records `hasWikiArticles: false` and the viewer and gates respect it (2026-10-06; d24967c, 0c637e7)

### Routing & navigation
- **Walking and cycling routes** alongside driving. The graph records foot and bike access, cycle lanes and tracks, surface, sidewalks, dismount sections and private access, with reverse records so walkers and contraflow cyclists can go against one-way streets; bikes may use steps at three times the cost. Directions has a Drive / Walk / Bike picker (shown when the ZIM's `routingModes` include them) with each mode's travel time, navigation pace and off-route trigger. Car routes are unchanged (2026-10-05; 744b244, 6707c7c, 260d694)
- OSM turn restrictions (`no_*` and `only_*`, via nodes or via ways) are honoured for cars and bikes, in the browser worker and the Python router; U-turns carry a penalty, and `?turns=off` disables restrictions. On D.C., 0 of 60 car routes are slower than the exact optimum (2026-10-05; 5fdc7a7, c710dc8, 53dac14)
- Long walking and cycling routes are found exactly instead of giving up: a 100 km ride from San Jose to Pescadero takes 1.2 s and is optimal (2026-10-05; 988143a, 4a331ac)
- Fixed: "No route" when an end snapped onto a road stub cut off at the extract's edge. The router snaps again beyond the cut-off stub (up to 3 tries) and the viewer says "Destination moved to the nearest reachable road" (2026-10-05; c5f2bbe, 5c511de, 9da7213)
- Fixed: every walk or bike route from Zürich HB said "No route found" because the nearest vertices were isolated platform and escalator pieces; both snappers now take a second, wider pass (48 candidates within 150 m) (2026-10-06; 3a73932)

### Terrain & satellite
- The lowest terrain zooms are drawn in a worker process (China's z0-z2 had peaked at 6.5 GB), and GDAL's cache defaults to 256 MB instead of 5% of RAM per process (2026-10-03; 0053561)

### Addresses & Overture
- Overture merging in non-Latin regions keeps its fast path: escaped non-ASCII lines had all gone through the JSON parser, with no change in output (2026-10-01; fe15499)

### Viewer & apps
- In-map light/dark switch in the Home group: each tap cycles Auto → Light → Dark, the choice is remembered (`?theme=` overrides it for one load), routes, pins and layers are kept, and the Find page and other open tabs follow it (2026-10-01; b70930e, 9349582)
- The compass shows only while the map is rotated or tilted (2026-10-01; 5ecf198, 1f4078e)
- The viewer page is 30% smaller (698 KB → 488 KB): comments and indentation are stripped at build time, and a token-stream check proves the shipped code is unchanged (2026-10-06; f82b836)

### Languages
- **Per-language builds** (`create_osm_zim --language xx`, `streetzim --language`, and a Zimfarm option). Map labels use `name:xx` and fall back to the local name; search shows and indexes the name in the chosen language, the native name and the English name; Kiwix page titles, the ZIM's Language metadata and full-text stemming follow the language (2026-10-05; e1090ac, c710dc8, 477203a)
- Wikipedia and Wikidata in the build's language: labels, descriptions and articles come from xx.wikipedia.org (or a Wikipedia ZIM in that language) through per-language caches. Article cleanup removes banners, hatnotes and portal boxes in other editions too: across 884 French articles, hatnotes went from 213 to 0 and stub notices from 625 to 0 (2026-10-05; 41ce6d1, c710dc8, 53dac14)
- **Translatable viewer UI, German first:** all 297 UI strings, the Find chip names and about 260 place types. The table follows the map's language; `?uilang=` overrides it, and a pseudo-locale (`qps`) finds untranslated text. English output is unchanged (2026-10-05; 6cadd9d, 7a7e1f1, 1c600a2)

### Regions & published ZIMs
- German Switzerland, "Schweiz und die Alpen": the first German-language StreetZim, with German place names, German Wikipedia and the German UI (2026-10-06; 4528d1a)
- German Ultralight Switzerland: no satellite, map tiles to z13, no Wikipedia articles and no Kiwix full-text index, with walk, bike and drive routing; 1.18 GB, down from 1.72 GB for the first cut (2026-10-06 to 10-07; 6d2d2bb, b9ba358)
- German border-cut Ultralight "Schweiz und Liechtenstein": cut to those two countries plus 10 km, 636 MB against 1.18 GB for the Alps edition (2026-10-13; ff8d111)
- Production builds are cut to the borders in `cloud/region-outlines.tsv`: the United States, Canada, Russia, Africa and Europe first, then Egypt and 15 European regions. The Carpathians, Austria-Czechia and Greece boxes were widened because they had cut off parts of their own countries (60% of Bulgaria, 57% of Slovakia, Kastellorizo) (2026-10-09 to 10-12; 4f9c1c3, 4e6dda7)
- Rebuild round shipped: Himalayas, Southeast Asia and Indian Subcontinent (10-02), China (10-03), English Switzerland (10-06), then Brazil, South America, Canada, the United States, Egypt, Africa and Central Asia (to 10-12); the United States, Africa and Egypt were built cut to their borders (Egypt 1.13 GB against 1.43 GB by box). Stale Torrent buttons for Switzerland, Silicon Valley and the German items were fixed (2026-10-02; ed8b485, d862193, 0c65f52, f8a8141)

### Website & distribution
- German site page (`/de/`) with an English · Deutsch picker, hreflang alternates and German cards, dates and sizes; a region can be listed on one language's page only (2026-10-06; 4528d1a, 61f4856)
- Old ZIMs are pruned from archive.org without leaving permanent `history/` copies; 2.1 TB of copies left by earlier prunes were removed (2026-10-09; 4d1b2fa)

### Build pipeline & packaging
- Fixed: a finished ZIM could be deleted on filesystems without hard links (FAT, exFAT, some network mounts). Leftovers from killed builds are swept only after their owner's lock is free, safely across containers and hosts (2026-10-01; eea10c5, 2cf043b, 8ff45f2, b2e7df0)
- Short downloads resume, and a corrupt cached file is downloaded again once instead of failing every later build (2026-10-01; 80f1844)
- Building the routing graph fits a 16 GB container for China with byte-identical output: 8.6 GB in 49 min, where it thrashed before (the old builder needed 23.4 GB) (2026-10-03; 8ce88f9, 408c742, a7c0e3c)
- A full China build completes in a 16 GB container (the openZIM recipe), producing a 22.8 GB ZIM: routing extraction and routing-cell writing run in child processes stopped with the build, and Find chips are split one at a time instead of holding every POI in memory (2026-10-03; 0053561, de5e0d6, a0689bc, 29ec7da)
- More memory cuts with identical output: China's spatial-cell step 8.67 GB → 0.84 GB, loading its Wikidata selection 4.4 GB → 1.2 GB (2026-10-04; dff450a, de1f5b4, 39b63ad)
- Shared Wikidata caches give clear permission errors with suggested fixes and clean up staging files left by killed writers (2026-10-05; 16b8e11, c10283f)

### Zimfarm/openZIM & Docker CLI
- `--tilemaker-store auto|memory|disk`: tilemaker's store goes to disk for large extracts (the Netherlands' peak 4.0 GB → 0.65 GB in the same time), after a free-disk check (2026-10-02; 6cdb530, fd7f36e)
- A product primer for openZIM and Kiwix (`docs/primer.md`) explains how StreetZim works and is built (2026-10-05; ab1e6fb, a04cc96, bf8c35e)
- `streetzim --include-poly` builds are cut to the polygon itself, not just its box (the Mexico box reaches San Diego); `--no-clip` (Zimfarm "No border clip") keeps the box (2026-10-12; 1d91711, f8a8141, 8ecad14)

### Quality
- `validate_zim` fails a ZIM whose Kiwix search results do not open (suggestions, redirect titles, article titles); a pre-fix builder-mode ZIM had 112 of 112 results dead (2026-10-02; 5e97d64, ad2ed5b)
- Tests check the viewer's search folding and word splitting against the Python writer for every code point; CI fails if the combining-marks table drifts (2026-10-01; e2f346c, 77b9409, ef3c822)
- Smoke gates no longer depend on English UI text, and a pseudo-locale browser check in CI fails on any untranslated visible string (2026-10-05; 8ad1ad0, 8a26212, bd614f8)

## Late September 2026 (09-25 to 09-30)

**Highlights:** the openZIM/Zimfarm track: a `streetzim` command, a Docker image published to ghcr.io, `--profile full|basic`, `--mbtiles-url`, licensed opt-in satellite, a wheel and CI, all on stock libzim; a big viewer update (dark theme, Maki POI icons, right-to-left shaping and Noto fallback glyphs, last-view memory with Home and About); search for administrative areas; maps open over each region's anchor city; areas across the antimeridian; about 40 regions rebuilt to drop the global Wikidata set.

### Maps & tiles
- Maps open over the region's anchor city instead of the bbox centre, which was empty for some regions (open Pacific for Hawaii, the Sahara for North Africa, the Gulf of Bothnia for the Nordics). The anchor is the hand-picked city in `cloud/regions.tsv` (Hawaii → Honolulu, Pacific Islands → Suva); other areas fall back to the median place position (2026-09-25; e789a52, 423f65f)
- Fixed: Australia's salt lakes (Eyre, Torrens, Gairdner, …) never rendered; the low-zoom lakes layer now reaches z6. The Natural Earth credit moved to the Sources dialog so the attribution bar no longer wraps over the controls at 320 px (2026-09-25; 58c9305, 86262f3)
- Fixed: builds could silently ship without some of their 768 glyph ranges after font-CDN errors, so those labels never drew. Downloads retry with backoff and a range that still fails stops the build (2026-09-28; 134c8f3)
- Builds from OpenFreeMap's Planetiler OpenMapTiles MBTiles (the tiles openzim/maps uses) work with no tilemaker, search and all Find chips included; on Monaco, tilemaker tiles carry about 31% more street names and about twice the named places (mostly named buildings; classed POIs only +8%), OpenFreeMap tiles are about 12% smaller (2026-09-28; 7cd7846, 865cb88)
- Maki POI icons (CC0) on category-coloured discs beside POI labels, inlined in the viewer so in-place viewer patches bring them to every region; unnamed POIs show from z16 (2026-09-29; a8c5db6)
- Areas across the antimeridian (Fiji, Chukotka, Kiribati): osmium extracts and tilemaker per side, tile selection, search, Overture, terrain, satellite, and the viewer's bounds, route lines and search distances (2026-09-29; b9f8087, 5668271)
- Fixed: a blank strip past the edge of every map's box, most visible over the sea; the camera now stays inside the box (2026-09-29; 2db0acb, 739c6c6)

### Search
- **Search for administrative areas:** one record per `boundary=administrative` relation (levels 2-10) and per named `boundary=place` city, town or suburb, with its other names, box, level and a type label that follows the country's conventions. The search box and Find page match `<name> <type>` and `<type> of <name>`; an area ranks above a same-named POI and picking it fits the map to its box. Each area also gets a Kiwix page "Name (type)" with redirects for its other names. Cost: about +0.2-0.4% ZIM size (2026-09-30; 20841e6, cf39c98, 632f1bb)
- A street that crosses several z14 tiles is one search result instead of one per tile (Monaco's "Avenue Saint-Martin" had appeared 27 times); long streets stay split near their parts (2026-09-28; 19f7aef, 134c8f3)
- Fixed: Kiwix's search bar suggested nothing, because search pages were not marked as articles and repackaging dropped even `index.html`; Monaco's Kiwix article count went from 1 to 18 (2026-09-28; 4ab753b, 946427f)
- `--kiwix-poi-pages`: every named POI gets a search page, so Kiwix's own full-text search finds shops and venues too (Monaco "Casino": 1 hit → 11). Off by default (about 440 B per POI) (2026-09-29; 29c320e, 9f061fa)
- Search coordinates are rounded to 5 decimals (about 1.1 m), about 10% off the search payload (2026-09-25; 7aa5592)
- Fixed: the result subline capitalised every word ("1 Mi"); it now prints as written and its distance follows the scale bar's units (2026-09-29; bd17545)

### Find & categories
- Fixed: on tilemaker builds, pharmacies, fuel stations, restaurants and other POIs whose tag has no OpenMapTiles class fell out of the Find chips; Monaco Food 45 → 177, Gas 0 → 3, Health 6 → 28 (2026-09-29; a052a87, 477afa4, 04c0d2c)
- The chip rail hides chips a ZIM cannot serve, and an empty or failed chip always says so ("No parks in this map", "Couldn't load …") (2026-09-29; 1b47635, 329a53c, ab3068e)
- Fixed: tapping a chip or "Search this area" no longer zooms the map out (a reader at z16 was pulled back to z14 on every tap) (2026-09-30; cfaa3ae, 3daa516, 2836ed1)
- Results count only what is visible: the search box, chip rail, results strip and controls are treated as holes, so the 300-result cap means the 300 nearest the reader can see (2026-09-30; ba27ae9, e663d9a)
- Fixed: repackaging an older ZIM dropped its pre-merge restaurants and cafés chips, leaving no food chip (2026-09-28; b39e743, 1f4720e)

### Places & Wikipedia/Wikidata
- Tapping any named place on the map (shops, restaurants, parks, water, peaks) opens a popup with its name, type, "Directions to here" and details from its search record (website, phone, brand). Before, only places with a Wikidata ID responded (2026-09-30; b98a602, 0220a05, 7c52687)
- Wikidata is filtered to the region in every build again: the region filter had silently stopped running for builds not streamed from world tiles, which carried the whole global set (3.3 M entries, about 108 MB, about 9% of each ZIM; Hawaii and Iceland still carried 640 MB) (2026-09-25; 396ad50, 36aa88d)
- Non-English OSM `wikipedia=` tags (`nl:Utrecht (stad)`) resolve through Wikidata to the English article instead of a same-titled English lookup: in a 500-place Dutch sample, correct articles went from about 127 to 204 (2026-09-30; c61074a, a377f8e)
- Tags whose item has no English article can still be bundled when English Wikipedia redirects that title to an article already in the map ("Aalten (dorp)" → "Aalten"), stored as ZIM redirects (2026-09-30; 04f8acb, 8646424, f39d500)
- Administrative areas' Wikipedia and Wikidata tags are resolved and their articles bundled (2026-09-30; 9bfcd5c, 7fa1789)
- The Wikipedia credit and `hasWikiArticles` reflect the articles actually stored; Wikipedia text is credited CC BY-SA 4.0 (2026-09-28; c88038a, 7f29877, 684dae9)
- Wikimedia requests follow its rate-limit etiquette: a shared pacer (at most 120 requests a minute, Retry-After honoured, gzip) replaced a fixed 1 s pause per article that took 25 of a 39-minute D.C. build; a rate limit is no longer cached as "no article" (a cold D.C. build had lost 177 articles that way); `STREETZIM_REQUIRE_WIKI=1` makes unfetched articles fatal (2026-09-29; f081607, 172b019, 8d9d31c)
- Fixed: Wikidata title resolution stopped during maxlag episodes and one bad Q-ID could abort a run; maxlag now waits within a budget and bad IDs are isolated (2026-09-30; e2d7dfb, 2a72c6b, 17c82fc)
- Fixed: the in-map Wikipedia button appeared for articles the ZIM does not contain (7,519 of 10,767 linked records in a Netherlands map) (2026-09-30; ef20a6d)

### Routing & navigation
- Routes cross the 180° meridian: a road OSM splits at ±180 becomes one vertex (tested on Taveuni, Fiji) (2026-09-29; b9f8087)
- The builder writes only the current routing formats; readers for the older formats in published ZIMs stay, and `docs/formats.md` gives a byte-level spec (2026-09-28; 056261e, b39e743)

### Terrain & satellite
- Satellite is capped at z13 for regions centred above 45° latitude, where z14 only interpolates the 10 m Sentinel-2 source and was 63% of the satellite payload: about 16 GB saved across Europe, Russia, Canada, Alaska and others (2026-09-25; 9d46490)
- Terrain built without a world DEM (every `streetzim` and Zimfarm build) no longer has 0 m cliffs where the DEM stopped: low zooms are filled from a GLO-30 + GLO-90 mosaic and audited against it, with much less DEM downloaded (2026-09-29; ce0e2e9, 85e1538)
- Satellite as an opt-in on the `streetzim` command with each source's licence: `--satellite` adds EOX's 2016 Sentinel-2 cloudless (CC BY 4.0); the sharper 2021 layer (CC BY-NC-SA) needs `--satellite-accept-noncommercial` and labels the ZIM non-commercial. The viewer shows a linked credit while imagery is visible (2026-09-29; 718447d, 9976554, 7daa157)
- Satellite and terrain caches survive interrupted builds: partial mosaics are not cached, corrupt tiles are fetched again, and DEM/VRT files are published atomically under exact box and zoom keys (2026-09-30; 17c82fc)

### Addresses & Overture
- The Overture downloader defaults to `--release latest` (resolved through Overture's STAC catalog, skipping releases still uploading), reads over HTTPS only the files whose box meets the area, and records the release used. Production keeps a pinned release (2026-09-29; bf007f2, 85de147, bc41443)
- Overture's new taxonomy is mapped back to the legacy category names (607 renamed names from all 73.6 M places, plus a nearest-ancestor fallback), so chips and cuisine filters keep working (2026-09-29; bf007f2, 217c5d1)
- Fixed: address and POI conflation erased Chinese, Japanese and Cyrillic names and dropped letters such as ß, Ł and Ø, merging distinct addresses; street normalisation now keeps every script (2026-09-30; 17c82fc, 4e18f85)

### Viewer & apps
- **Dark theme:** the map and all UI chrome follow `prefers-color-scheme` or `?theme=dark|light` and switch live; it stays light when the host already inverts the page (Kiwix JS) (2026-09-29; a8c5db6, de4dde8, 5348df9)
- The viewer reopens each map where the reader left it; a Home button returns to the opening view, an About panel shows title, description, build date and viewer version, and start-up failures (no WebGL, missing browser APIs, unreadable map config) get a full-page explanation instead of a blank map (2026-09-29; 44d99be, 05e6fc8, d1d8d2b)
- Every distance (Find cards, Nearby, the place sheet, routing, places.html) follows the scale bar's mi/km choice, which persists; with no saved choice the browser locale decides (miles only for US, Liberia and Myanmar locales) (2026-09-29; 7ae6170, 8da7cea)
- Fixed: a result near the map's edge landed under the results sheet; the viewer zooms in until the pin is clear (2026-09-29; 7bbf442)
- The credits dialog shows satellite, terrain, Wikipedia and tile-source sections only for layers the ZIM has (2026-09-28; 7f29877, b40645e)
- Fixed: the layer panel covered the Home button; on landscape phones it is a row (2026-09-30; 960a928, f4a96c9)
- Fixed: under kiwix-serve every page load made two failing requests for PWA-only files (2026-09-29; ab913f9)
- The 11,000-line viewer is edited as feature parts and joined byte-identically by `tools/build_viewer.py`; CI checks the shipped file matches its parts (2026-09-28; 7fa35a1, 1cc1798)

### Languages
- Arabic, Hebrew and other right-to-left labels are shaped by the vendored mapbox-gl-rtl-text plugin, loaded only when the map has RTL text (2026-09-29; a8c5db6, 5e614ed, 999c06f)
- Arabic, Armenian, Georgian, Hebrew, Lao and Thai labels render offline: the builder merges pinned Noto Sans glyphs into the font ranges, only for scripts the map's labels use (about 0.5 MB for all six) (2026-09-29; 5beb90e, 222a833, 9dbfecb)

### Regions & published ZIMs
- New: Horn of Africa and Central Africa; North Africa and East Polynesia got site cards (2026-09-25; 0d93c58, 1059c56, 54abea1)
- About 40 regions rebuilt from 09-26 to 09-29 to drop the global Wikidata set, smallest first, from NYC and D.C. to East Coast US and the Indian Subcontinent (2026-09-25; 36aa88d)
- Light variants (Switzerland Light, Africa Light) are rebuilt by one wrapper driven by `cloud/region-variants.tsv`, from the parent's inputs, aborting if one is missing (2026-09-26; 273184a, fe02ced)
- Alaska's box crosses the antimeridian to include the western Aleutians out to Attu (2026-09-29; 6168fb7, ea96a49, d22f909)

### Website & distribution
- Fixed: the site's filter box never filtered (its CSS was nested in another rule), and typed text was white on white (2026-09-27; 2fdf194, ba03dfc)
- Feature badges come from each ZIM's own metadata; the Light variants had wrongly shown a Satellite badge (2026-09-27; 3725db7)
- Torrents are regenerated by hashing the local ZIM instead of re-downloading about 700 GB; every region's torrent names its live ZIM again (eight had drifted) (2026-09-27; fb9cfc4, 961018f)

### Build pipeline & packaging
- The 8,000-line builder became an importable `streetzim/` package (tiles, satellite, terrain, addresses, routing, search, ZIM writer), with golden builds showing identical output at each step (2026-09-28; 5d9cd89, 42fc5c5, 1cb5f91)
- Identical tiles are stored once as ZIM aliases (2026-09-29; 78c4839, 0572835, a05cf96)
- Viewer assets are pinned: MapLibre GL JS 5.23.0 and the RTL plugin are vendored and hash-checked, the 768 glyph ranges sha256-pinned, so offline and Zimfarm builds fetch nothing from CDNs (2026-09-29; 4b552e3, 999c06f, e8c3d2c)
- Missing coastline shapefiles warn instead of silently dropping the ocean; `STREETZIM_CACHE_DIR` moves download caches out of the checkout (2026-09-28; 46649cf, 1cc1798)
- Operations scripts moved under `ops/` (symlinks keep old paths working; CI checks the boundary); 31 dead scripts retired (2026-09-28; 33d38aa, a436aa8, 6911f93, df3a208)
- A pure-Python manifest packer (`--zim-builder manifest`), so builds no longer need Cargo or a zimru checkout; archives are published atomically (2026-09-30; 17c82fc, 4e5a775, 454e0b4)
- The extract is cut to the build area once instead of four times (about 3.7 GB each) (2026-09-30; fc316a6, 2bdda0a)
- `szim` (inspect / plan / trim / verify / sim) makes a light variant from a full ZIM without rebuilding (Argentina 3.43 → 2.17 GB in 47 s) and inventories an archive.org ZIM from its tables alone (2026-09-30; 130cbdd, 89d27dd, 20aaf5f)
- The legacy Leaflet raster variant was removed (2026-09-29; c1750f3)

### Zimfarm/openZIM & Docker CLI
- **New `streetzim` command** shaped like openZIM's scrapers (flag names match maps2zim's): required openZIM metadata validated before any download, the area from `--area`, `--include-poly` or `--bbox`, input from `--pbf-url` or `--mbtiles`, a task-progress file, a generated `offliner-definition.json` for Zimfarm, atomic output (2026-09-28; c88038a, 7f29877)
- Docker image with tilemaker 3, osmium and the Python stack (Python 3.14, zimscraperlib), published to `ghcr.io/jasontitus/streetzim` after CI passes (`dev` and `sha-*` from main, `X.Y.Z` and `latest` from tags) (2026-09-28; 4ec4c7f, 93463a9, 89c6e80)
- `--profile full|basic`: `full` (default) adds Wikidata, Wikipedia articles, Overture, terrain and Kiwix POI pages; `basic` fetches only OSM data; each feature has an on/off override (2026-09-29; 5647c5d, 7f76a29, 1c562bc)
- `--mbtiles-url` builds from ready-made OpenMapTiles MBTiles such as OpenFreeMap's, resumable and checksum-verified (2026-09-29; a0eeb01, fa5b232, b40645e)
- Fixed: every bbox west of Greenwich failed (a value starting with "-" read as a flag); polygons with far-apart parts build their largest land group (2026-09-29; 4f1437d)
- Verified on stock python-libzim, zimcheck and kiwix-serve; zimru and the external Xapian builder are optional accelerators. An installable wheel carries every runtime resource (2026-09-28; 4ab753b, a56fb5e, 4eb84b1)
- One CPU budget for the build (`--cpus`, the container's CPU quota, one core per 2 GB of memory): a 16 GB task now uses 4.7 GB instead of 11.9 GB for the Netherlands' tiles (2026-09-30; b938119, d3d807a, f17a15c)
- Measured build costs in `docs/zimfarm.md` (Netherlands 92 min, 10.8 GB, a 1.22 GB ZIM), and the response to openZIM's review with an integration and adoption plan (2026-09-29; 3d8e7d7, cc648ee, bd5a1c6)

### Quality
- GitHub Actions CI: ruff, pytest, Node viewer tests, a real Monaco build checked with zimcheck and a browser smoke through kiwix-serve, an OpenFreeMap-tiles build, a Docker build, an offline full-profile build, a weekly live run (2026-09-28; 46649cf, 5bbabc9, c764cdc)
- pyright (strict on pure modules, baseline gate for the rest), wider ruff rules, pre-commit, chip-rule parity between Python and the viewer, and `offliner-definition.json` checked against Zimfarm's schema (2026-09-28; 6cf7c28, b39e743, 684dae9)
- `pytest tests/` runs in 32 s instead of over 40 minutes; slow real-ZIM tests are opt-in (2026-09-28; d6a4cda, 644e408)
- Golden builds check that a refactor changes no ZIM output (2026-09-29; ac27bd1, ab870b4, ffeacf1)
- The render gate probes at each region's anchor city; search integrity tests cover coordinates, collisions, full text and accent folding; the browser smoke expects exactly the chips the manifest lists; CI runs a layout-overlap check at 7 phone sizes (2026-09-25; 5e3fe72, 65ad5c1, 58fcb35, 960a928)

## Early September 2026 (09-01 to 09-24)

**Highlights:** the 2026-09 data refresh rebuilt the regions on the 2026-08-31 planet and Overture 2026-08-19.0; blank terrain traced to its root cause; the streaming Rust packer made the continents shippable again; search and Find rebuilt for locality (10-100× less data per query); offline Wikipedia articles with pictures in every region; a Preview button that streams any map from archive.org into the browser; viewer slots that patch a ZIM's viewer in seconds; new regions: Switzerland, 11 European regional ZIMs, Light variants.

### Maps & tiles
- Lakes show at zoom 0-5: the viewer embeds Natural Earth 1:50m lakes (409 polygons, 36 KB) below z6, where the tiles carry no water (2026-09-23; f78011a, abade88)
- Trails styling: tracks and paths are drawn and recoloured in satellite mode, with a phased trails plan (styling, foot profile, named trails in search, route relations) (2026-09-23; f78011a, cfdbef3)
- Fixed: hillshade order after toggling satellite, stale paint values when satellite is turned off, and the label font stack (2026-09-03; c4ac437, 62513a6)

### Search
- **Search re-sharded for locality:** hot prefixes split adaptively by the characters that follow and by record tier (places, POIs, streets, addresses), so a query reads only the leaf for what was typed, and addresses only when the query has a digit. "Gimpo" on Korea-Mongolia went from 16 files / 21.2 MB to 3 files / 0.3 MB (before, "Caracas" on South America fetched 736 files / 320 MB) (2026-09-16; 689f220, aab3334, 1363c15)
- The "Search near" typeahead uses the same targeted leaf path (a Chicago dropdown had taken ~40 s); ranking folds accents ("Cancun" finds Cancún) (2026-09-19; e648d87)
- Fixed: accented, CJK and punctuation queries on the Find page returned nothing (2026-09-03; c4ac437, d498584)
- Fixed: an address containing U+2028 split a search record in two; the writer now checks every record round-trips (2026-09-16; 6a1bbad, 780cfa5)

### Find & categories
- **Find chips in geographic shards:** chips over 2 MiB are cut into ~1-2 MiB shards with bounding boxes, and the viewer fetches the nearest first for an exact nearest-300 under a device-sized byte budget. Shops on East Coast US went from a 147.5 MB file to 1.1 MB (2026-09-15; 08324e8, e5fc0f7)
- Restaurants and Cafés merged into one **Food & Drink** chip; older ZIMs load both, and `?chip=` links alias both ways (2026-09-16; aab3334, 2a50a49, 3dd59f5)
- The Find page's "nearest city" no longer downloads the whole place category (109 MB on China, 480 MB on Europe); any oversized category is now sharded (2026-09-16; c2a7653, 99c6034, caff72a)
- Fixed: the chip rail was missing Health, Landmarks and Libraries (2026-09-18; e27ea36, b0a9cc9)
- Fixed: a chip tapped over an empty view returned the first 300 records in the file (in Kaunas, places 546 km away); it now returns the 300 nearest (2026-09-23; fb85341)
- Fixed: on iPhone, chip taps were cancelled as the start of a scroll (2026-09-20; f883db8)

### Places & Wikipedia/Wikidata
- **Offline Wikipedia articles with pictures in every region:** WebP thumbnails from the Kiwix maxi ZIM (lead image under the title, a gallery, Commons licence credits, each image stored once). On California, lead images add 4% and all images 37%; continents default to lead images only (2026-09-05; 720dfd6, c31fd0d)
- Every article has a sticky "← Back to map" bar returning to the same camera (home-screen web apps on iOS have no Back button); the service worker adds it to articles in older ZIMs (2026-09-18; 45a3f0f)
- Fixed: the Wikipedia panel said "No Wikipedia places in view" while its index was still loading on large ZIMs (2026-09-18; cd33a8b, b861cc4)
- Fixed: escaped link markup in some articles' wikitext showed as garbage text; Wikidata title resolution retries server errors instead of silently losing ~60% of titles (2026-09-05; b27d280, c31fd0d)

### Routing & navigation
- **Optimal routes again, ~10× faster worker:** the A* heuristic assumed 80 km/h while the graph has 100 km/h motorways, so it returned slower routes; the inner loop was rewritten on typed arrays (~42 B per node instead of ~440 B) with results byte-identical to the Python reference (2026-09-03; c4ac437, 6223071)
- Long routes run a chain of searches: optimal to 200 km, then weighted, then greedy; Asheville → Wilmington (442 km) returns the exact optimum in 2.0 s. Unreachable destinations are detected fast (Charlotte → Ocracoke 43 s → 0.3 s) (2026-09-03; 9a07a4c, d498584)
- Car routing no longer uses footways, paths, steps, cycleways or `access=no/private` ways (a 3 km/h staircase used to beat any detour over ~200 m); roundabouts and motorways are one-way (2026-09-03; 6223071, f67c217)
- Taps snap to a drivable, connected road instead of a footpath, pier or private-drive fragment, which had caused "No route found" (2026-09-03; f67c217, d498584, c4ac437)
- Fixed: long routes on large regions never finished: the worker held only ~33 routing cells where NYC → Philadelphia needs ~62. The budget scales with device memory; that route now takes 9 s (2026-09-10; c81feac, e9ebd4f)
- Driving mode holds a Screen Wake Lock (2026-09-18; 4b5b310, fa0eac7)
- Fixed: in Kiwix on iOS, the Directions origin field and close button could not be tapped (2026-09-20; aee4a80)

### Terrain & satellite
- **Fixed: the root cause of blank terrain.** With no `gdalbuildvrt` on the build host, builds had fallen back to a DEM mosaic that covered only Hispaniola since March, so terrain elsewhere was rasterised against nodata (Mexico's 4,491 blank tiles, gaps in Argentina, Alaska and the Himalayas). Builds now write the DEM VRT directly and check it covers the box; a bug that skipped z9-z12 for every region was fixed too (2026-09-10; c81feac)
- Blank or missing cached terrain tiles are regenerated instead of reused, so a region heals on its next build (2026-09-07; 7f6d545, 764d7a1, f5f205f)
- Fixed: the Caucasus and the Caspian rendered flat (stale "ocean" markers hid real DEM files) (2026-09-11; e9ebd4f, f53e3a1)
- More robust DEM and terrain: atomic downloads with TIFF checks, transient errors never recorded as ocean, a 2 px halo against seams, no zero bands on 1° lines (2026-09-03; 6223071, 16ed167)
- Fixed: zero-byte cached satellite tiles became empty ZIM entries; satellite at z13+ stalled ~12 s retrying 404s (2026-09-14; bb161ea, ff65474, 6223071)

### Addresses & Overture
- **OSM addresses on buildings are no longer dropped** (osmium exports buildings as MultiPolygons, which the extractor discarded): Hawaii 3,937 → 17,473 addresses, Iran 24,231 → 206,488, the Baltics 446k → 1.9M. This matters most where Overture has no addresses (it covers 39 countries) (2026-09-14; 846952b, cc70802)
- The Overture address merge uses far less memory (Baltics 1.5 GB → 437 MB, byte-identical output) (2026-09-14; 846952b)
- `OVERTURE_RELEASE` picks the Overture release; the September refresh used 2026-08-19.0 (2026-09-04; d1ebe8c)
- Opt-in `STREETZIM_URL_DEAD_STATUSES` drops only truly dead websites (404/410/DNS) instead of bot-blocked live businesses (2026-09-04; d1ebe8c, 95f05c8)

### Viewer & apps
- **Streaming ZIMs over HTTP:** the PWA can open a ZIM from a URL with range requests through the same reader that serves a picked file, so visitors can pan, search and open places on a map they haven't downloaded. Washington D.C. is ready in 1.6 s; Europe (62 GiB) reaches the map in ~16 s (2026-09-18; 63f723f, efe93a0)
- Low-memory phones: the reader's caches have byte budgets (halved on 2 GB devices) and entries of 4 MiB or more are streamed (2026-09-18; 45a3f0f, 4b5b310)
- Phone layout and touch: 44 px targets, 16 px inputs (no iOS zoom on focus), safe-area insets, a landscape layout, a PWA manifest for /drive/ (2026-09-18; 6359a3e, 4b5b310, fa0eac7)
- Safer PWA picker and streaming: picks are validated and checked against storage quota, and a stalled stream shows "Try again" (2026-09-18; 4b5b310, fa0eac7, 555de61)
- Fixed: iOS layout problems: actions under Safari's toolbar, the search bar under the status bar or Dynamic Island in Kiwix, overlapping controls at phone widths, popups painting under the search box (2026-09-06 to 09-20; fdc2067, 49672cc, a8a2f9d, aee4a80)
- Fixed: the PWA could not read ZIMs whose clusters are out of file order (as zimru writes them), and over-read the last cluster on every ZIM (2026-09-05; 629a6ba, 3c16420)
- Fixed: a batch of PWA bugs (a bad picked file could not be removed, wrong Range answers, deploys never installing a new service worker) and malformed `#pin=` / `#dest=` links aborting start-up (2026-09-03; c4ac437, 62513a6, d498584)

### Regions & published ZIMs
- **The 2026-09 data refresh:** regions rebuilt from the 2026-08-31 OSM planet and Overture 2026-08-19.0, each from its own extract (all 49 extracted in one run); uploads began 09-06 (2026-09-06; b814b75, a59c029, cfc151a)
- The largest regions (Brazil, South America, Australia & NZ, Russia, Canada, China, Africa, the United States) shipped again once the streaming packer landed (2026-09-14; a65e418, dfd2362, d50a9f2)
- East Africa published (built in May, never uploaded); the Carolinas got a site card (2026-09-04 to 09-07; 720dfd6, 9ce0122, 61c36f8)
- New: Switzerland & the Alps (2.25 GB), the first region with the merged Food & Drink chip (2026-09-18; 792f8b4, 22112c8, da71893)
- New Light variants for low-storage devices: Switzerland Light (no satellite, tiles to z13; about 1.4 GB against 2.1 GB) and Africa Light; z14 vector tiles, not satellite, turned out to be most of the weight (2026-09-19; 35c30c1, 9a68c5b, 1349f78)
- 11 new European ZIMs: Benelux, Greece, Romania/Hungary/Bulgaria (Carpathians), Balkans, Austria/Czechia/Slovakia, Poland, Iberia, Nordics, Germany, Britain & Ireland, Italy; France registered (2026-09-22; 5ac22f7, 1a58bf5, b2b289c)

### Website & distribution
- **Preview button** on every card: opens the region in the browser, streamed from archive.org (as above) through a small Cloudflare Worker range proxy that stores nothing (2026-09-18; 63f723f, efe93a0, 8376c3e)
- Cards show `Updated <date>`, the vintage of the data (2026-09-07; a2ebc15)
- The catalog lists every region in alphabetical sections with an instant filter, and site generation fails if a live archive.org item has no card (2026-09-22; 1a58bf5, 61c36f8, 22112c8)
- Fixed: catalog links to ZIMs the prune had deleted; Torrent buttons hide when the torrent names a different file (2026-09-03; c4ac437, 25546d6, d498584)
- Upload pipeline: a failed upload is fatal, the remote size is verified, each torrent is regenerated from the local file and committed, the prune never deletes the file a torrent seeds, and re-runs use checksums instead of re-sending gigabytes (2026-09-03; 6223071, fb50fda, dc1d43b)

### Build pipeline & packaging
- The Rust packer streams its manifest: Brazil's 93 GB manifest had been killed for memory twice and no continent could be packed; the manifest is also written zstd-compressed (93 GB → ~25 GB) (2026-09-11; 7a44564, 3f5b579, ca0e04e)
- **Viewer slots:** viewer files sit uncompressed in fixed-size slots, so a viewer update patches a ZIM in place in 3-10 s instead of an 8-22 minute re-pack, keeping its Kiwix UUID (2026-09-20; 7d7d18c, 768aa7a, 466b7e4, fb19ee9)
- Shipped ZIMs can be retrofitted without a rebuild: Find chips, search data and oversized categories re-sharded, with Kiwix's indexes kept and an equivalence check before upload (2026-09-15; e5fc0f7, aab3334, caff72a)
- Refresh tooling: the region registry `cloud/regions.tsv` (bbox, tier, smoke route, search term), a build → gates → upload queue, `ship-region.sh`, and a planet extractor that cuts every region in four osmium passes (~5 h for 49) (2026-09-04; d1ebe8c, 7396fdf, 95f05c8)
- World vector tiles and the search cache are built once for the planet and sliced per region (a regional tile phase went from ~120 to 1,600+ tiles/s); world search extraction streams instead of holding ~120 M features (it had hit 106.5 GB) (2026-09-05; e9bb8a6, f63a11d, 7a51cbc)
- Fixed: `--max-zoom` was ignored for regions with MBTiles of 5 GB or less (2026-09-18; 3910bfc)

### Quality
- Every upload passes gates: terrain coverage, the validator, live routing, search, Find and a browser smoke, with timeouts scaled to ZIM size (2026-09-04; d1ebe8c, 95f05c8, f47faac)
- The terrain-coverage gate samples the DEM under each tile, so flat real land passes and blank tiles fail with a reason (2026-09-05; 7396fdf, c930de4)
- A stricter validator (missing search chunks, fulltext claimed but absent, oversized category, leaf and manifest files) and a browser smoke that can actually fail (it had passed a "No results" row) (2026-09-03; 6223071, b7d7a0a, f47faac)
- New gates: Kiwix (the in-ZIM viewer under real kiwix-serve), layout overlap at 320/390/430 px, and a device matrix of seven iPhone viewports (2026-09-18; b37e12d, a8a2f9d, 89d4bc3)
- A differential routing test checks the JS worker against the Python reference (0 mismatches on 20 Silicon Valley routes) (2026-09-03; c4ac437, 16ed167)

## May to August 2026

Almost all of this landed between 04-28 and 06-06; the only later change is a review batch on 08-10.

**Highlights:** a single-pass build (with the Rust packer and xapianbuilder) replaced a ~14 h repack chain; Find became a maps-app experience (an on-map chip rail, a swipeable result carousel, a place sheet, "Search this area"); routing moved to a Web Worker with finer cells and the SZCI v2/v3 formats; offline Wikipedia articles bundled in the ZIM; about 22 regions added, each with its own webseed torrent; Overture POIs with dead websites dropped.

### Maps & tiles
- `--map-center LON,LAT` and `--map-zoom Z` set the opening view where the bbox centre is empty; Hawaii had opened on open ocean (2026-05-12; ae28350, 6358949)

### Search
- Oversized search chunks split recursively (the US had one 1.5 M-record, 183 MB chunk), so continent ZIMs stay under the per-entry cap (2026-05-03; ff7fa28, f38cfb4)
- Typeahead ranks large places first and skips addresses unless the query has a digit, so "san" finds San Diego rather than nearby streets (2026-05-09; 15a5ca8)
- Fixed: typing a common prefix such as "san " crashed Safari on iPhone (~1.5 GB of heap); typeahead fetches at most 6 chunks at a time and filters as they arrive (2026-05-09; 6454ce8)
- Fixed: scrolling a typeahead list on touch devices picked the row under the finger (2026-05-09; f355a9e)
- Rust-packed ZIMs ship a Kiwix title (suggestion) index as well as full text; in `--xapian builder` ZIMs their results were dead links until 2026-10-02 (see October) (2026-05-08; f38cfb4)

### Find & categories
- **On-map category chip rail** under the search box (Restaurants, Cafés, Bars, Shops, Museums, Parks, Gas, Hotels): a tap loads results straight onto the map (2026-05-10; 51254f8, 3291a31, d1f1dd8)
- Results on the map in a **swipeable card carousel**: swiping flies to the result, the active pin turns blue, tapping a pin scrolls to its card, keyboard works (2026-05-08; fdc125e, 9fd796e, a2c486a)
- **Place-detail sheet:** name, category, brand and distance; Directions, Call and Website; phone, socials and coordinates; a Wikipedia extract with facts; up to 5 places within 1.5 km; a walk/drive time estimate (2026-05-09; e81908b, cb344d2, e43356c)
- "Search this area" after panning, a "Limit to map area" filter, sub-category chips (top 8 subtypes such as Italian restaurant), recent searches, Distance/Name/Category sort and Call/Website buttons on cards (2026-05-09; a2c486a, 9fd796e, e43356c, 8e332e9)
- "Map view" on the Find page puts its results on the main map (2026-05-08; 2525942, 69cfebc)
- Fixed: after picking a "Near" city, Find still filtered to the stale viewport (Gas near Cancún showed a first result 941 km away; now 26 m) (2026-05-11; db560e1, 4791b05)

### Places & Wikipedia/Wikidata
- **Bundled Wikipedia articles** (`--bundle-wiki-articles`): full articles stored in the ZIM as compact reader pages (about 8× smaller), with source link and CC BY-SA footer; on California they take about 0.2-1% of the file (2026-05-29; c36bb13, b849743)
- `--resolve-wikidata-titles` resolves POI Wikidata IDs to Wikipedia titles through sitelinks: directly linkable articles on California went from ~2.3k to ~5.5k (2026-05-29; 2f8a0dd)
- Offline Wikipedia browsing: a nearby "📖 Wiki" list and map markers at any zoom from a build-time geo-index, with "Read full article" from popups, the list and the place sheet; articles open as native ZIM pages and Back returns to the map (2026-06-02; d27dac0, c71fe76, a25b050)
- Fixed: browsing nearby Wikipedia over a wide area crashed mobile and Kiwix WebViews (~130 MB of Wikidata prefetched); short descriptions are now baked into the geo-index (2026-06-02; 8533e61, cda1bbc)

### Routing & navigation
- Route search runs in a **Web Worker** away from map rendering, with a main-thread fallback (2026-05-09; ca710f9, 1ca4886)
- Routing cells are prefetched along the straight line at route start, at each GPS fix and for every carousel card (2026-05-09; 6ad1c09, 5212b60, c574804)
- Finer routing cells (0.1°, ~500 KB, instead of 1°, ~51 MB) cut cold short-route latency, the default from 06-02 (2026-05-10; 7395606, 2e141a6)
- Routing data in smaller pieces: the node table was split into parts (the US 90 M-node graph became a ~50 MB index plus 18 parts), then each routing cell got its own node coordinates, so routing no longer loads a ~60 MB region-wide table (fixing an out-of-memory crash on a local Palo Alto route); the worker ships in every ZIM so Kiwix uses it too (2026-05-03 to 06-02; ff7fa28, f2edb21, 2e141a6)
- Routes longer than 800 km skip the full-optimal pass (San Diego → Arcata had spent ~12 s finding nothing) (2026-05-09; 45e1469)
- A stage label and progress bar replace the spinner; focusing a route field cancels a route in progress; the directions panel folds itself when it would cover half the screen (2026-05-09; e46b501, 5fe81b3, bc259cc)
- Fixed: Ukraine and other large regions had no routing in the apps because the embedded viewer rejected the split routing format (2026-05-05; b4e1000, d4bb9a4)
- Fixed: a late GPS fix overwrote the origin being typed; the camera snapped back to the reader's location during search (2026-05-09; 855451b, b51f3c8)

### Terrain & satellite
- The DEM downloader writes atomically and re-downloads cached tiles that aren't valid TIFFs; preflight checks the DEM before calling a terrain tile missing (2026-08-10; 557f971)

### Addresses & Overture
- **Dead-website filtering:** an async checker crawled California's 1.29 M Overture URLs (64.5% alive), and Overture POIs whose website is dead are dropped at build time; OSM POIs only lose the dead field (Palo Alto: 28.6% of Overture-only POIs dropped) (2026-05-10; d0b30f5, c950da8, de21259)
- Overture credits restored on salvage builds, with a tool to repair shipped ZIMs (2026-05-05; 3fd1a2a)
- Overture defaults to the 2026-05-20.0 release (2026-06-02; b5f15e2)

### Viewer & apps
- The locate (GPS) button appears and works in Kiwix (2026-06-02; a0ae975, ed87dcc, 15bf973)
- Fixed: on iOS Kiwix, the locate button, carousel, place sheet and lower Wikipedia panel sat ~184 px below the visible area; the viewer now sizes to the visible window (2026-06-06; 79f3fb7)
- Fixed: in the Kiwix WebView, panning could drag the whole page and leave it stuck (2026-06-03; 99e84a3, eef0446, 7da37bb)

### Regions & published ZIMs
- New: Ukraine, Moldova & Western Russia (2026-05-05); Brazil, Argentina, South America and the Indian Subcontinent (05-07); Southeast Asia (05-08); China (05-10); then Russia, Iceland, Korea & Mongolia, Caucasus, Turkey, South Korea, Hawaii, NYC, Chicago, Greater LA, New York State, Florida and Alaska (05-11); Central America & Caribbean and Himalayas got site cards (2026-04-30 to 05-12; 00f0353, 2718bc8, 754937a, de21259, 4791b05)
- The United States, Europe, California and Silicon Valley rebuilt with every feature (2026-05-05; 1ee03b0, 1f4d241, fdcb1e5)

### Website & distribution
- **Per-region torrents** at `/torrents/<region>.torrent`: one file each, downloading from archive.org over HTTP (BEP 19 webseed), with trackers and DHT; archive.org's own torrents bundled every old build, 3-5× more data (2026-05-02; 622df5c, 08c3a09)
- Cards grouped into tiers: continents, multi-country regions, countries, US sub-regions, then states, cities and islands (2026-05-08; 849ac96)
- Fixed: one network timeout during site generation published a California download that 404'd; the generator retries and refuses to deploy with a known item missing (2026-05-10; 2a827a6)

### Build pipeline & packaging
- **Single-pass build:** `create_osm_zim.py` writes the final layout directly (spatial routing cells, split search, chip files), replacing repackage and upgrade passes that took ~14 h at Europe scale; California went from 1h55m to 1h28m (2026-05-08; f38cfb4, a6a69ec)
- `--xapian builder` (Rust packer) builds Kiwix's full-text and title databases in seconds instead of hours and skips per-entry stub pages (13-15 GB saved on Europe); skipping those pages is what left Kiwix's own search results dead until 2026-10-02 (2026-05-08; f38cfb4)
- Fixed: the Rust packer silently used zstd level 3 instead of 22, and its window size made in-browser decompression allocate 128 MiB per chunk (California's route load 16 s → 7.5 s) (2026-05-08; f38cfb4, a6a69ec)
- Repackaging streams through disk: the US ZIM's peak went from ~124 GB (out of memory) to ~5-12 GB (2026-05-03; 1f771e4)
- `swap_viewer_rust.py` replaces only the viewer in a Rust-built ZIM and keeps its search index (Hawaii in 22 s; re-shipping 11 ZIMs ~38 h → ~2.5 h) (2026-05-29; bd7e003)
- `build-region-and-upload.sh` runs build → validate → smoke → upload and refuses to upload if the smoke fails (2026-05-05; 1f4d241, 2718bc8)

### Quality
- Smoke route pairs for many more regions, so search and routing tests stay inside each region (2026-04-28 to 05-11; 51293fd, 3793dbc, b4e1000)
- Validator: no false failures on zimru's link-check output; new checks for a referenced but missing `routing-worker.js` and for zoom levels that are mostly empty tiles; a Playwright smoke of locate, the Wikipedia list and markers (2026-05-03 to 08-10; ff7fa28, 557f971, b5a2760)

## April 2026

**Highlights:** offline routing (Drive mode with a turn-by-turn HUD, then continent-scale graphs on phones); Overture addresses and places; the Find page with category chips; distribution went live (archive.org items, the streetzim.web.app catalog, the /drive PWA that runs a local ZIM offline); the first published regions, Canada among them; a pre-upload validator, zimcheck and a browser smoke test.

### Search
- Search matches any word in a name ("cathedral" finds Washington National Cathedral) and ignores accents ("cafe" finds Café) (2026-04-19; c5b781e)
- Fixed: wrong "City, State" labels (Dupont Circle as Silver Spring, Toronto as Pennsylvania); labels now come from GeoNames through `reverse_geocoder` (2026-04-19; 107ba0b, fc3973d, e843b70)
- Search result pages became real pages with "Directions to here" and "View on map", mobile layout and dark mode (2026-04-22; 9ca021a)
- Non-Latin names get their own chunk keys, and oversized prefixes split into hashed sub-files (Japan's single 349 MB chunk had crashed Kiwix Desktop) (2026-04-23; 48ce0a6, ca2abdd)
- Fixed: the result-page buttons returned 404 in every host (38,084 broken links per regional ZIM), with a retrofit for shipped ZIMs (2026-04-26; 2e81975)

### Find & categories
- **Find page** (`places.html`) in every ZIM: a search box and category chips (restaurants, cafés, bars, museums, parks, libraries, shops, gas), distance sort, Directions and Map buttons, `q=`/`cat=` links; Hotels and Health chips followed, and Museums and Landmarks split (2026-04-22; 6e81bca, 6485510, 47c981d)
- Per-category index files, so a nearby lookup reads one file instead of every search chunk (2026-04-19; a485ce3)
- Find starts from the map's centre, named after the nearest city, when GPS is denied (2026-04-24; ca2abdd)
- Each chip gets its own file, split again into ≤10 MB parts, so Find no longer loads a full `poi.json` (up to 1 GB on Japan, which crashed Chrome and iOS) (2026-04-25; ba3150c, 81050a1)

### Places & Wikipedia/Wikidata
- POI search records carry Wikipedia and Wikidata references from OSM tags (2026-04-19; a485ce3)

### Routing & navigation
- **Offline routing:** the road network is extracted at build time (`--routing`) and embedded in the ZIM; the viewer runs A* between two points and draws the route with distance and time (2026-04-15; fe5d023, c55e102)
- A compact binary graph (SZRG) with road names; the routing panel has origin and destination fields with typeahead, a current-location button and a list of roads (2026-04-18; 8c722af, 3748528)
- **Drive mode:** a tilted GPS follow camera and a HUD with the next turn, remaining distance and time, ETA and off-route detection, with roundabout and ramp cues and a Re-route button. Walk and Bike buttons used the same car route with their own camera and pace (2026-04-21; 22b903b, 77aadae, 91e40df)
- Routing starts from a place: "Directions to here" on result pages, Find rows, pins and popups, through `#dest=lat,lon` links (2026-04-22; 9ca021a, abb1899, ba3150c)
- **Continent-sized graphs:** graphs of 500 MB or more ship as geographic cells loaded on demand (Iran's 5.95 M-node graph peaks at 183 MB); long routes finish on iOS Safari through an optimal → greedy → highway two-pass chain (2026-04-23; 48ce0a6, ca2abdd, ba3150c)
- Very long routes skip the slow optimal pass (Toronto → Vancouver 146 s → 23 s); a progress indicator shows elapsed and remaining time (2026-04-26; 776b6c7, 42215ec)
- Fixed: phantom straight lines on large graphs (SZRG v3 removed a 16.7 M-geometry limit); picks before the graph loaded did nothing; "Failed to load routing graph" in the PWA (2026-04-19 to 04-26; 8311df3, 42b0503, 675b3bf)

### Terrain & satellite
- `--satellite-download-zoom` limits new satellite downloads while still using cached deeper tiles (2026-04-12; 1e0c007)
- Copernicus GLO-90 fills areas where GLO-30 is restricted (Georgia, Armenia, Azerbaijan, parts of Russia) (2026-04-17; c2d4989)
- Fixed: terrain gaps and seams between regions, stripes of 0 m terrain across the Mountain West, a terrain cliff at 33°N in Iran (2026-04-10 to 04-23; 07dd23b, 5ad468f, 48ce0a6)

### Addresses & Overture
- OSM house-number addresses are searchable and routable ("45 Main St, Riga") (2026-04-18; 8c722af)
- **Overture addresses** fill gaps in OSM's (Silicon Valley +1.68 M addresses) and **Overture places** add website, phone, social links, brand and category to POIs and add unmatched places (Silicon Valley: 224,141 added), with dataset credits in the ZIM and the viewer (2026-04-22; a8d2b66, 6485510, abb1899)
- Fixed: Overture addresses dropped when the same number and street existed in another city (1029 Ramona St, Palo Alto) (2026-04-28; e4c388e, 938acad)

### Viewer & apps
- **Offline PWA** at streetzim.web.app/drive: pick a local `.zim` and the full viewer (map, search, routing, Find, Drive mode) runs from it through a service worker and an in-browser ZIM reader, with no network (2026-04-21; e29cf95, 8c03088)
- `window.streetzimRouting`, a control API for host apps (route, Drive mode, camera, pins, a search-radius ring) (2026-04-21; 226d511, 69c96e4, 2e59d63)
- A "you are here" dot, a scale bar that switches miles/km on click, the build date in the info box; Kiwix library icon and Name/Flavour/Tags metadata (2026-04-02 to 04-10; 97c15b9, 5336ad2, 73c4b8f)
- `streetzim-meta.json` records a ZIM's bbox, features, counts and routing version (2026-04-19; a485ce3)
- Fixed: the map rendered only in the upper-left corner of the Kiwix WebView; deep links failed on iOS (2026-04-02 to 04-22; 8ecf000, abb1899)

### Languages
- Every glyph range the font CDN serves is bundled (punctuation, Tifinagh and more); Arabic, Hebrew and some other scripts still drew nothing until 2026-09-29 (2026-04-09; 9e64938)

### Regions & published ZIMs
- Publishing began: an archive.org upload script with full metadata and a torrent per item (04-03), then the first regions on archive.org, listed by the generated site (04-09) (2026-04-03; c73748c, 90f718f)
- Japan, Texas, West Coast and East Coast US, Australia & New Zealand, Africa, the Baltics, Silicon Valley and Central US (2026-04-10 to 04-21; 14b6f77, 1e0c007, f665a0b, 8c722af)
- Canada published (26 GB, its routing split into 1,437 cells); Central Asia got a site card (2026-04-26 to 04-27; 946d6d2, 7cc7248)

### Website & distribution
- archive.org items with full metadata, and the streetzim.web.app site generated from them, with download statistics (2026-04-03; c73748c, 90f718f)
- A LICENSE covering code and every data source, attribution in the map and a ZIM License field (2026-04-03; 0764c85)
- Dated ZIM filenames give each upload a fresh torrent; each item keeps its two newest ZIMs (2026-04-12; 5a14daa, 0098ae6)
- Links to the Kiwix apps by platform, and feature badges on each card (2026-04-15 to 04-22; 962fdce, 4544380)

### Build pipeline & packaging
- Cloud builds on Google Cloud spot VMs with cache pushes and automatic relaunch (2026-04-09; 90f718f, a9c788d)
- Routing extraction cuts the bbox first and streams in two passes: Europe peaks ~40 GB instead of ~500 GB (2026-04-17; 64d0ba8, 3748528)
- `repackage_zim.py` fixes a shipped ZIM in ~15 minutes instead of a 1-2 h rebuild (viewer swap, routing layouts, search splits, terrain); `build_region.sh` builds and validates with every feature on (2026-04-23; 48ce0a6, ca2abdd)
- Bundles used only by LLM tools are left out by default (US regional ZIMs 10-15% smaller) (2026-04-27; 81050a1)
- Optional Rust packer `streetzim-pack` (`--zim-builder=rust`): 25% faster and 2% smaller than the libzim path (2026-04-27; c31cc2b, 3b7fc97, 0d5a30b)
- Continent builds on a 128 GB host: streamed DEM merge, sequential world MBTiles reads (~475× faster), a streamed Overture places merge (~24 GB → ~1 GB) (2026-04-30; 941b988, c92bec7, 07526d9)

### Upstream (libzim, Kiwix)
- Proposed a Kiwix Desktop fix for `fetch()` on `zim://` URLs (the map did not load on Windows); our own workaround was withdrawn the next day (2026-04-11; 8273332, 3ae69ba)

### Quality
- **Pre-upload validator** `cloud/validate_zim.py` (20+ checks: tiles, terrain, fonts, search, routing compatibility, Find chips, Overture fields) behind `upload_validated.sh`, plus preflight checks and golden-route corpora (2026-04-23; 48ce0a6, ca2abdd)
- A browser smoke test of the PWA (picker, search, Find, directions), failing on any console error or 404 (2026-04-26; 675b3bf, d7a39d9)
- zimcheck runs in the validator, with a faster zimru path for ZIMs over 15 GB (2026-04-26; 2e81975, bbaa798)
- A terrain audit stops a build if blank tiles remain after repair (2026-04-20; 4bad920, 4a1f271)

## March 2026

StreetZim began on 2026-03-10.

**Highlights:** OSM vector maps in a ZIM with an embedded MapLibre viewer, 50-200× smaller than raster; offline chunked search; 3D terrain and hillshade; Sentinel-2 satellite imagery; Wikidata place popups and the Wiki Explore sidebar; world-scale builds.

### Maps & tiles
- **Offline vector maps in a ZIM:** OSM PBF → tilemaker → MBTiles → ZIM, rendered on the device by MapLibre GL JS; 50-200× smaller than raster tiles (Monaco 0.7 MB, Washington D.C. 17.4 MB). A Leaflet raster prototype was built alongside (2026-03-10; ea5bdc4, 424adf3)
- Real SDF fonts for labels; deep zoom to z20 by overzooming vector tiles; POI labels appear by rank as you zoom in (2026-03-10; b05d825, c1a4978, bd0b258)
- Fixed: blank vector maps under kiwix-serve and in the Kiwix JS PWA (font paths, relative URLs in MapLibre workers); a clear message when WebGL is missing (2026-03-10; 4e7a28f, 0ae741b)

### Search
- **Offline place search** over an index chunked by 2-character prefix, loaded on demand (about 100 MB of browser memory even for planet-scale builds), with typeahead, keyboard navigation, a fly-to marker and `#map=` links; important features also get pages for Kiwix's full-text search (2026-03-10; f186d2d, 089fbdf)
- Results show where they are ("street · Washington"), rank nearby first with a distance, and include waterways, buildings and land use (2026-03-10 to 04-02; 25efaf0, f3d5728, 97c15b9)

### Places & Wikipedia/Wikidata
- **Wikidata popups** (`--wikidata`): tapping a place shows population, area, elevation, country and a Wikipedia extract (2026-03-13; 66eb983, bd0b258)
- The **Wiki Explore** sidebar lists the Wikipedia entries in view, as a bottom sheet on mobile (2026-03-24; f3d5728)

### Terrain & satellite
- **Sentinel-2 cloudless satellite** layer (`--satellite`) with a toggle; labels get halos over imagery (2026-03-10; e045c83, ee61bb8)
- **3D terrain and hillshade** from Copernicus GLO-30 (2026-03-11; 35fd2d6)
- Smaller imagery: terrain rounded to 10 m (tiles 74-81% smaller), satellite as AVIF (61% smaller than WebP) (2026-03-12; 6630dea, 08920b6, f066034)
- Terrain tiles generated in the cloud from Copernicus DEMs on S3 (2026-03-24; f3d5728)

### Viewer & apps
- Tile and font requests go through a retrying `zimtile://` handler for reliable loading in Kiwix (2026-03-10; 5f1c3f8)
- A frosted-glass control strip (Satellite, 3D, Explore) and a Sources dialog (2026-03-24; f3d5728)

### Languages
- Labels fall back from `name:latin` to `name_int` to `name`, preferring English names (2026-03-12; 299f8f8)

### Regions & published ZIMs
- First test ZIMs: Monaco, Washington D.C. (30k searchable features) and Virginia (2026-03-10; a216fbd, a94bde4, b924a38)

### Build pipeline & packaging
- Zstandard on every entry, vector tiles included: ZIMs about 2.3× smaller (D.C. 18.3 → 7.8 MB); multi-core compression and parallel search extraction (2026-03-10; fe840dc, 271e4ed, 089fbdf)
- World-scale builds stream 345 M+ tiles from MBTiles into the ZIM (2026-03-11; 35fd2d6)
- Regional builds reuse world-scale work: a search cache (~30 min → ~1 min), bbox-filtered tiles and only the region's Wikidata (2026-03-15; 713ac9d, f3d5728)

### Upstream (libzim, Kiwix)
- libzim fixes contributed upstream: a compressor infinite loop (openzim/libzim#1047) and a work-queue spin lock, both in libzim 9.6 (2026-03-15; 713ac9d, d797383, d80ee90)
