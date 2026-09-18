# Android and iOS browser review of the web code

Scope: the `/drive/` PWA (picker, service worker, ZIM reader, viewer,
Find page), the online preview that streams ZIMs off archive.org, the
Wikipedia article pages, and the catalog page, as they behave in Android
Chrome and iOS Safari (browser tab, "Add to Home Screen" standalone, and
the WKWebView / Android WebView inside Kiwix where relevant). Reviewed
2026-09-18 against `main` at `3dd59f5`, with the emphasis the maintainer
asked for: **low-end Android phones**.

What this is based on:

- two full read-throughs of the code (one of the viewer and page UI, one
  of the service-worker/reader plumbing), with the browser behaviours
  they depend on checked against WebKit and Chromium sources;
- the viewer rendered headlessly at iPhone 13, iPhone SE, Pixel 7 and
  landscape sizes with element rectangles and hit-tests measured, plus
  the preview flow driven with iPhone 13 / Pixel 5 emulation (viewport,
  pixel ratio, touch, user agent — the engine is still Chromium);
- the live site through the deployed Cloudflare worker on files
  uploaded this week (west-asia 9.9 GB, east-coast-us 10.5 GB with a
  real search and the Wiki panel) and on Washington DC, including a
  "slow phone" profile (CPU 4× slower, Fast 3G / Fast 4G throttling of
  the page and the worker);
- the service worker instrumented to report what its caches hold, on
  real ZIMs;
- nothing on a physical phone. Items marked **device test** are where
  that matters.

## Summary

The PWA and the preview work at phone size and nothing is
desktop-Chrome-only. What limits a low-end Android phone is memory and
data, not layout; what limits iOS is its aggressive process and storage
management plus a few standalone-mode gaps. Ranked by impact:

1. **Memory inside the service worker** — count-bounded caches held
   57 MB after one east-coast-us search (52 MB of decompressed
   clusters), and a Wikidata bucket is a 30–45 MB blob in a cluster of
   its own. **Fixed here: byte budgets** (64 MB clusters / 32 MB blobs,
   halved on devices reporting ≤ 2 GB). Routing entries of 100–200 MB
   were buffered whole; **fixed in the second pass: streamed** (§A2).
2. **Memory on the page** — the search chunk cache keeps 20 parsed
   chunks (300 MB of JSON on Japan), the routing index is resident
   twice, old-style chip files are cached without bound (§A3).
3. **A picked local file is copied into IndexedDB** — on Android a full
   copy against quota inside a service-worker event Chromium kills after
   five minutes; on iOS two copies. The preview stores only a URL and is
   the right path for phones (§A4).
4. **Wikipedia articles were a dead end** in an iOS home-screen app (no
   browser chrome, no back). **Fixed here**: every article gets a sticky
   "Back to map" bar, baked in at build time and injected by the PWA for
   existing ZIMs (§B4).
5. **iOS standalone layout** ignores the status bar inset; the "Sources"
   button overlaps the locate control by 9 px on every phone viewport;
   several controls are under 20 px; the build stamp intercepts taps on
   the search box (§C). Two small ones fixed here (§Changes).
6. **Data on cellular** — 15–25 MB for a first look at a large region and
   no Save-Data awareness; the shell re-downloads 1.6 MB per deploy (§A5).

## Part A — low-end Android

### A1. Service-worker cache memory (fixed: byte budgets)

`web/drive/zim-reader.js` keeps decompressed clusters and individual
blobs in LRUs that were bounded by count only (8 clusters, 512 blobs of
up to 512 KB). Measured with the worker reporting its own caches
(`status` → `cache`):

| session | clusters | blobs | blocks | total |
|---|---|---|---|---|
| Washington DC, first view | 6 · 9.4 MB | 8 · 0.7 MB | 10 · 1.3 MB | 11.3 MB |
| east-coast-us, first view + "Boston" search + Wiki panel | 7 · 51.8 MB | 10 · 0.8 MB | 32 · 4.0 MB | 56.6 MB |

Blob sizes on real ZIMs: tiles average 192 KB (max 981 KB, DC); search
shards live in 0.9 MB clusters; category chips in 1.3 MB clusters; a
**Wikidata bucket is one blob of 6.7 MB average and up to 45 MB**, in a
cluster of its own (south-america: 7.3 MB on disk, one entry). Every
popup that reads a bucket decompressed 30–45 MB into the cluster cache,
where eight of them could sit. Android's low-memory killer and iOS
jetsam end a worker that grows like that; the map then stalls while it
restarts cold.

Fix applied: `LRU` gained a byte budget — clusters 64 MB, blobs 32 MB,
halved when `navigator.deviceMemory` reports ≤ 2 GB (Chrome exposes it
to workers; WebKit does not and gets the full budget) — and an entry
larger than its whole budget is never kept (a one-shot read). Reads are
unchanged; the unit, reader and end-to-end tests pass.

### A2. Whole-entry buffering of routing data (fixed: streamed)

`serveFromZim` reads an entry completely before building the
`Response`. The routing cells index is 5 MB (Hispaniola) to 212 MB
(Midwest) and v8/v9 routing chunks are up to 100 MB; each is one raw
cluster, so a route start materialises the whole thing in the worker,
and the viewer requests the cells index twice at once (main thread
`resources/viewer/index.html:7249` and `routing-worker.js:233`). On a
slow link Chromium also ends a fetch event that has not answered within
five minutes, which a 100 MB read on cellular can exceed. Current
split builds keep cells in ~4 MB zstd clusters of 400 cells
(south-america), so day-to-day routing is fine; the peak is at route
start.

Recommended: stream big raw blobs instead of buffering — for a local
file respond with `blob.slice(start, end)` as the body, for a streamed
file with the ranged fetch's own body — and pass the cells index from
the main thread to the worker as a transferable instead of fetching it
twice. The byte budget above already stops such clusters from being
retained.

### A3. Page-side memory (fixed: byte and count caps)

- Search: `fetchChunk` keeps up to 20 whole parsed chunks
  (`resources/viewer/index.html:4702–4733`); with 15 MB chunks that is
  ~300 MB of JSON, and the streaming filter's "eligible for GC" comment
  does not hold while the cache references them. Bound it by bytes
  (~30–50 MB) or keep only filtered matches.
- Routing: the cells index and a cell cache exist on the main thread
  and again in the worker (`index.html:7240–7300`); on WebKit (no
  `deviceMemory`) both default to 192 MB budgets. Drop the main-thread
  cells once the worker is ready, or transfer the buffer.
- Older ZIMs' chips and `places.html` caches (`_findChipCache`,
  `state.cache.*`) grow without bound; only the geo-sharded path is
  budgeted. Reuse that budget.

### A4. Local-file mode copies the file (fixed: page-side write with a quota check)

Every mobile browser lacks `showOpenFilePicker`, so a pick goes through
`<input type=file>` and the `File` is stored in IndexedDB
(`web/drive/index.html:345–367`, `sw.js:229–233`). Chromium writes IDB
blobs with a file copy guarded by a modification-time check
(`storage/browser/blob/write_blob_to_file.cc`); WebKit's document
picker imports a copy first and IDB stores a second one. So a 3.5 GB
region needs several GB of free storage and minutes of copying — inside
a service-worker message event that Chromium terminates after five
minutes (`kRequestTimeout`), while the picker waits ten. The hint text
("the file handle is remembered") is not what the code does: it stores
the File, not the handle. **Device test** for Android `content://` picks
(the mtime the check sees may not match, failing the write).

Recommended: write the record from the page (no event time limit), check
`navigator.storage.estimate()` against the size first and refuse with a
clear message, call `navigator.storage.persist()`, and correct the hint.
For phones, point users at the Preview button instead: it stores a URL
and nothing else.

### A5. Network and data (fixed)

- A first look at a large region costs 15–25 MB (109–111 range
  requests); panning, Wiki and Directions add to it. Nothing consults
  `navigator.connection.saveData` / `effectiveType` (Chrome Android,
  Samsung Internet). Recommended: when Save-Data is on, ask before
  auto-streaming from a catalog link and say the size; show the running
  total in the banner (the worker already reports `stats.bytes`).
- Retries give up after ~3 s of backoff, and MapLibre does not retry an
  errored tile until the source reloads, so a tunnel leaves holes until
  the user pans. Lengthen the backoff (~10 s with jitter) and reload
  errored sources on `online`.
- `sw.js` precaches the whole shell with `cache: 'reload'` on every new
  deploy, re-downloading the 1 MB MapLibre bundle although it is
  immutable per version; use `no-cache` for versioned assets.

### A6. CPU, GPU, WebView (mostly fine)

- MapLibre asks for `webgl2` and falls back to `webgl`, so devices with
  OpenGL ES 2.0 still render.
- zstd decompression of a 2 MiB cluster in JS is the per-tile CPU cost;
  under a 4× slower CPU on Fast 3G (page and worker throttled) DC's map
  was ready 18.9 s after the click, all of it network. On Fast 4G with
  the same CPU, live east-coast-us: viewer ready 14.6 s, search index
  20.9 s, "Boston" 4.1 s, Wiki 0.4 s.
- Eight overlays use `backdrop-filter` over an animating WebGL canvas —
  GPU cost on weak devices; drop it under `(hover: none)`.
- Language floor: numeric separators in the viewer and worker need
  Chrome 75 / Safari 13. Android WebView updates through Play, so only
  devices without Play Services are at risk; `sw.js` and
  `zim-reader.js` use arrow functions and classes (Chrome 49+).

## Part B — iOS Safari and home-screen apps

### B1. Standalone layout ignores the top inset (fixed)

The picker declares `apple-mobile-web-app-status-bar-style:
black-translucent` and the viewer uses `viewport-fit=cover`, but
`safe-area-inset-top` is used nowhere in the viewer: the search box
(`top: 10px`), navigation control, routing panel, driving HUD and build
stamp sit under the status bar / Dynamic Island (47–59 px) once the app
is installed. Bottom controls (locate button, "Sources", `#info`) add no
`safe-area-inset-bottom` either; the Find page does it right.
Recommended: a `--top-inset: env(safe-area-inset-top, 0px)` variable on
the top-anchored containers and MapLibre's top-right control, or drop
`black-translucent`.

### B2. Service-worker lifetime (partly fixed: keep-alive ping)

WebKit ends an idle worker after 10 s (immediately under memory
pressure); Chrome after 30 s; both when the app is backgrounded. All
reader state is process-local, so the preview restarts cold: one range
request for the header, the cluster pointer table (640 KiB on Europe),
then cold lookups. The browser HTTP cache usually answers the aligned
ranges again — but not in Private Browsing, which has no disk cache.
Recommended: persist the header, MIME list, sorted cluster offsets and
the memoised top-round dirents with the record, and keep the worker
warm with a light `status` ping while the viewer is visible.

### B3. Storage rules (fixed: hint, persist())

Seven days of Safari use without a visit deletes IndexedDB, the Cache
API and the registration for a site used in a tab; a home-screen app is
exempt but has its own storage, so a ZIM picked in Safari is not visible
in the installed app. `navigator.storage.persist()` is never called.
Recommended: fix the picker hint and, in Safari, suggest Add to Home
Screen to keep a map.

### B4. Wikipedia articles were a dead end (fixed)

`openWikiArticle` navigates the whole page to `wiki-article/<Title>`
after stamping the map camera into the hash, relying on "the app's own
Back button" — which a home-screen app does not have, and which is easy
to miss in Kiwix. Fix applied in two layers: `cloud/wiki_articles.py`
bakes a sticky "← Back to map" bar into every article (44 px tall,
padded under the status bar, `history.back()` when there is history so
the stamped camera is restored, otherwise a link to the map at the right
depth for slash titles), and `sw.js` injects the same bar into articles
from ZIMs built before this change and points the link at the absolute
viewer URL (a relative `index.html` would come back from Firebase as a
redirect, which iOS refuses for a navigation served through a worker).
Verified end to end on Washington DC: open "Adams Morgan", tap the bar,
back on the map with the routing API ready in 191 ms.

### B5. Other iOS items (fixed)

- Driving mode never requests a Screen Wake Lock, so the phone locks
  mid-navigation; request it on enter and re-request on
  `visibilitychange` (also Android).
- The Find page's "Search near" input is 15.2 px, under WebKit's 16 px
  focus-zoom threshold on a page that allows zoom (the main search input
  is 16 px).
- `user-scalable=no` is ignored by iOS; a pinch on a panel zooms the page
  and `setViewportVars` then shrinks `--app-height` to the zoomed
  viewport. Add `touch-action: pan-x pan-y` on panel roots and ignore
  viewport updates while `visualViewport.scale ≠ 1`.
- The attribution dialog is sized with `80vh`; inside the Kiwix
  WKWebView (layout 956 px, visible 772 px) its bottom is under the
  toolbar. Use `--app-height`.
- The wiki sheet uses `--bottom-inset` while `position: absolute` in a
  body already sized to the visible height, so in Kiwix it floats 184 px
  above the bottom; `bottom: 0` is right there.
- Viewer pages carry no manifest link or Apple metas, so Add to Home
  Screen from the viewer makes a plain bookmark; a standalone launch
  lands on the picker and needs a tap; a viewer with no working ZIM shows
  "HTTP 503" instead of bouncing to the picker (only `HTTP 404` does).

## Part C — layout and touch targets (both platforms; fixed)

Measured on the rendered viewer:

- **"Sources" overlaps the locate control** by 9 px across its width on
  every phone viewport (`#attr-btn` at `bottom: 36px` vs MapLibre's
  bottom-right stack); the lower third of the button opens the sources
  dialog. Move `#attr-btn`/`#info` above the control stack.
- **The build stamp** (`top: 6px; left: 8px; z-index: 9999;
  pointer-events: auto`) covers the top band of the search input on
  phone widths; a tap there copies a build id. Hide it under 480 px
  unless `?debug=1`, or move it.
- **Small targets**: routing minimise/close 13–15 × 18 px, 4.5 px apart;
  `#routing-gps-btn` 28 × 24; wiki-panel close 13 × 16; search clear
  ~16 × 18; control-strip buttons 32 px; chips 32 px; routing inputs
  26 px tall at 12 px. Catalog buttons 30 px tall. Give icon buttons a
  44 px box (padding or negative margins) and raise rows under
  `@media (pointer: coarse)`.
- Catalog grid `minmax(300px, 1fr)` with 24 px side padding overflows
  below 348 px viewports (iPhone SE first generation, small Androids);
  `minmax(min(300px, 100%), 1fr)` fixes it.
- Landscape phones: wiki and routing panels cover the search box; the
  preview banner covers the search dropdown's lower rows. `@media
  (max-height: 500px)` variants.
- Search and routing inputs lack `autocorrect="off" autocapitalize="off"
  spellcheck="false" enterkeyhint="search"` (the Find page has them).

## Part D — the online preview on phones

- Emulated iPhone 13 and Pixel 5: picker, redirect, viewer and banner
  render and fit; the banner clears the attribution and the controls.
  It is four lines tall at 390 px; shorter copy under 480 px would give
  the map more room.
- Live site through the Cloudflare worker (desktop / iPhone emulation):
  west-asia 9.9 GB — viewer 15.7 s / 13.6 s, search index 22 s / 19 s,
  109 requests, 15.7 MB; east-coast-us 10.5 GB — viewer 18 s / 24 s,
  64.5 M-place index 26 s / 41 s, "Boston" 15 rows in 3.4 s / 5.6 s,
  Wiki 150 entries, 111 requests, 20–24 MB. On a real phone add its
  own latency: each cold lookup is a chain of ~13 dependent requests.
- Single-range `Range` values are CORS-safelisted in WebKit as in
  Chromium/Firefox ([MDN](https://developer.mozilla.org/en-US/docs/Glossary/CORS-safelisted_request_header),
  [WebKit r252047](https://trac.webkit.org/changeset/252047/webkit)), so
  Safari does not preflight each block URL.
- The banner never appears for a locally picked file (`source: 'file'`)
  or inside Kiwix (different path, no worker) — verified both ways, and
  the local-file smoke test now asserts it.
- The Cloudflare free tier (100k requests/day) is a few hundred mobile
  sessions; past it the worker answers 429 and tiles fail silently.
  Worth mapping to a "preview temporarily unavailable" line.

## Already handled well

- Viewport sizing from `visualViewport` with `--app-height` and
  `--bottom-inset`, safe-area padding on the bottom sheets, the Kiwix
  dead-band lift for the locate button.
- Touch: `-webkit-overflow-scrolling`, `overscroll-behavior`, a body
  scroll lock that still allows in-page scrollers and multi-touch,
  slop-threshold touch handling in dropdowns, `blur()` after picks.
- Standalone detection and iOS fullscreen handling; geolocation without
  `navigator.permissions` in Kiwix; storage access wrapped in try/catch;
  `QuotaExceededError` surfaced on the Find page.
- Service worker: redirected responses rebuilt before caching,
  `updateViaCache: 'none'`, absolute scope, clean-URL aliases,
  network-first shell, validate-before-persist, non-memoised failed
  opens, quiet optional probes.
- Reader: blob copies instead of subarrays so cached tiles don't pin
  clusters, piecemeal raw-cluster reads and in-flight dedupe on the
  streaming path, the `_clusterEnd` fixes; 206 verified, 200 refused.
- Memory discipline elsewhere: streaming per-chunk search filtering,
  byte-budgeted geo-shard cache cleared on `pagehide`, sparse-state A*,
  `compact()` after routes, `deviceMemory` fallbacks, worker-crash
  fallback.
- `places.html`: top safe-area padding, 16 px search input,
  `autocapitalize`/`spellcheck` attributes.

## Changes made in this review

- `web/drive/zim-reader.js`: byte-budgeted cluster and blob caches
  (64/32 MB, halved on ≤ 2 GB devices); `cacheStats` for diagnostics.
- `web/drive/sw.js`: `status` reports `cache`; Wikipedia articles get
  the "Back to map" bar (injected, or re-pointed if baked in).
- `cloud/wiki_articles.py`: every built article carries the bar; test in
  `tests/test_wiki_articles.py`.
- `web/drive/index.html`: URL box at 16 px (no iOS focus zoom).
- `resources/viewer/index.html`: 44 px hit area for the banner's ×.
- `cloud/preview_smoke_test.mjs`: `DEVICE` emulation with screenshots,
  `CPU`/`NETWORK` throttling of page and worker, `CHROME_ARGS`,
  `SITE_URL`, `SMOKE_SEARCH`, `SMOKE_WIKI=1`, `SMOKE_ARTICLE=1`, and the
  worker's cache report. `cloud/pwa_smoke_test.mjs` asserts the banner
  stays hidden for a local file.

## Changes made in the second pass (2026-09-18)

Everything in the order below was implemented, then reviewed
adversarially by three independent read-throughs (service worker and
reader; viewer and Find page; picker) whose findings were fixed before
the commit. Verified in headless Chromium with `tests/test_sw_streaming.mjs`
(41 checks over a synthetic ZIM, URL and File sources),
`tests/test_picker.mjs`, a phone-emulation layout check (iPhone 13
portrait and landscape), and the existing smoke suites on the
washington-dc fixture and the 11.3 GB east-coast-us 2026-09-16 file.

- **§A2 — streaming.** `web/drive/sw.js` answers any entry of 4 MiB or
  more that sits in a raw cluster with a streamed body: `Blob.slice()`
  for a local file, the range request's own body for a streamed one,
  with correct 200/206/416 handling. Nothing of that size is held in
  the worker any more. Concurrent reads of one path share a read, and
  the viewer hands the parsed routing cells index to its worker as a
  transferable instead of fetching it twice. Raw-cluster blobs are read
  on their own for local files too, so a small entry next to a 100 MB
  routing chunk no longer pulls the chunk in.
- **§A3 — page memory.** The search chunk cache is budgeted by bytes
  (32 MB, 12 MB on ≤ 2 GB devices); the map's chip cache keeps 3 (1)
  chips; the Find page keeps 3 (1) chip / chunk / category arrays.
- **§A4 — local pick.** The picker validates the file through the
  worker (`check-zim`, header only), checks `navigator.storage.estimate()`
  against the file size and refuses with the numbers, writes the record
  from the page (no message-event time limit), has the worker reopen it
  (`reload-zim`) and calls `navigator.storage.persist()`. The hint says
  what really happens. A saved record that no longer opens reports the
  reason (`openError`).
- **§A5 — data.** `?zim=` links do not auto-stream under Data Saver
  (the URL is filled in, Stream starts it); the banner shows a running
  byte count; the range source retries five times over ~8 s and tile
  fetches back off for ~7.5 s; a tile source is reloaded on `online`;
  the shell precache revalidates scripts (`no-cache`) instead of
  re-downloading them.
- **§A6** — `backdrop-filter` dropped on coarse pointers.
- **§B1, §C — layout.** `--top-inset` / `--safe-bottom` on every top- and
  bottom-anchored element and on MapLibre's control corners; "Sources"
  sits left of the locate control; the build stamp is hidden under
  480 px unless `?debug=1` and no longer stacks above the search box;
  under `(pointer: coarse)` every icon button has a 44 px box, inputs are
  16 px and 44 px tall, chips and control buttons 44 px, MapLibre buttons
  44 px; landscape phones put the side panels below the search box and
  cap the dropdown; the catalog grid no longer overflows narrow phones
  and its buttons are taller on touch; search and routing inputs carry
  `autocorrect`/`autocapitalize`/`spellcheck`/`enterkeyhint`.
- **§B2 — worker lifetime.** The banner pings the worker every 8 s while
  the page is visible (`ping`: no IndexedDB, just the byte tally), which
  keeps WebKit from idle-killing it between tile requests. Persisting
  the parsed header/pointer tables is still open (below).
- **§B5 — iOS.** Screen Wake Lock in driving mode (re-acquired on
  return); Find page input 16 px; viewport variables ignore pinch-zoom
  states; `touch-action: pan-x pan-y` on panels; attribution dialog
  sized from `--app-height`; the wiki sheet sits at `bottom: 0`; the
  viewer and Find page carry the manifest and Apple metas on the
  `/drive/` origin, so Add to Home Screen installs the app; a standalone
  launch forwards to the loaded map; the viewer bounces to the picker on
  404/500/503-without-source and shows "not answering" with Try again
  when the streamed source fails; `#info` links "Change map".
- **§D — preview.** The banner has one line of copy on phones, the byte
  counter, and a note when the source stops answering (HTTP 5xx, no
  connection, or the proxy's 429 daily limit); the worker posts the
  same notice to every open page.
- **Find chips.** A merged chip (Food & Drink) on a ZIM built before the
  merge loads its backing files instead of failing with "category 'poi'
  not in the index"; a chip whose category the ZIM lacks says "No … in
  this map" instead of an error.

## What remains

1. Device test on one low-end Android (2 GB) and one iPhone: the preview
   of a 10 GB region with search, Wiki and a route, watching
   `chrome://inspect` / Web Inspector for worker restarts. Two things
   only a device can answer: whether Chromium's IndexedDB blob write
   accepts an Android `content://` pick (its modification-time check),
   and whether WebKit keeps a service worker alive while a streamed
   response body (a 100 MB routing chunk over cellular) is still being
   read — if not, the viewer's retry covers a dropped tile but a routing
   chunk would fail once and need a second tap.
2. Persist the parsed header, MIME list and cluster pointer table with
   the record so a cold worker restart in Private Browsing (no HTTP
   cache) costs one request instead of a chain (§B2).
