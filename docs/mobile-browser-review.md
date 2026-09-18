# Android and iOS browser review of the web code

Scope: the /drive/ PWA (picker, service worker, ZIM reader, viewer,
Find page), the online preview that streams ZIMs off archive.org, and
the catalog page, as they behave in Android Chrome and iOS Safari
(browser tab, "Add to Home Screen" standalone, and the WKWebView inside
Kiwix where relevant). Reviewed 2026-09-18 against `main` at `3dd59f5`.

What this is based on:

- reading the code (`web/drive/index.html`, `sw.js`, `zim-reader.js`,
  `resources/viewer/index.html`, `places.html`, `web/template.html`);
- the preview flow driven in headless Chromium with iPhone 13 and
  Pixel 5 emulation (viewport, pixel ratio, touch, user agent) — layout
  and touch only, the engine is still Chromium; screenshots of picker,
  viewer and catalog at phone size;
- the live site through the deployed Cloudflare worker, on files
  uploaded this week: west-asia (9.9 GB) and east-coast-us (10.5 GB)
  including a real search and the Wiki panel, and Washington DC;
- measurements of what the reader holds in memory, from real ZIMs;
- WebKit/Chromium behaviour that cannot be tested here, from the
  browsers' own documentation and bug trackers (linked inline).

Nothing here was run on a physical phone; the items marked **device
test** are the ones where that matters.

## Summary

The preview and the PWA work on phone-sized layouts and nothing in the
code is Chrome-desktop-only. The issues found are, in order of impact:

1. **Memory in the service worker on phones** — the reader keeps whole
   decompressed clusters and up to 512 blobs by *count*, and current
   ZIMs contain clusters that decompress to 30–45 MB (Wikidata
   buckets), so a browse-and-search session can pin a few hundred MB
   inside the worker. iOS terminates a worker that grows that large.
   Fix: budget the caches by bytes. (medium; device test to size it)
2. **iOS Safari zooms into the picker's URL box** — its font was 13 px
   and the page allows zoom. Fixed in this change (16 px).
3. **Tap targets** — the catalog buttons are 30 px tall and the preview
   banner's × was ~20 px; iOS/Android guidance is 44–48 px. Banner fixed
   in this change; catalog buttons recommended.
4. **Local-file mode on phones is the weak path, not the preview** —
   storing a picked multi-GB File in IndexedDB is a copy on some
   browsers, Safari's 7-day storage cap applies outside standalone
   mode, and Safari and a home-screen app do not share storage. The
   preview needs none of that, which is a good reason to point phone
   users at it.
5. Smaller layout items (build stamp over the search box, banner
   height on phones) listed below.

## Findings

### 1. Service-worker memory (medium, device test)

`web/drive/zim-reader.js` keeps three caches per reader:

| cache | bound | what it holds |
|---|---|---|
| `clusterCache` | 8 entries | whole decompressed clusters |
| `blobCache` | 512 entries, each ≤ 512 KB | individual blobs (tiles, fonts, JSON) |
| `blocks` (HTTP source only) | 256 × 128 KiB = 32 MB | raw file blocks |

Measured on real ZIMs:

- Washington DC: tiles average 192 KB (max 981 KB); the 512 largest
  cacheable tiles total 33 MB, so `blobCache` alone can reach ~33 MB on
  a small region and ~250 MB on one whose tiles are near the 512 KB
  cap.
- Wikidata buckets are single blobs of 6.7 MB on average and up to
  45 MB (DC); on south-america a 7.3 MB-on-disk cluster holds one
  bucket. Every read of such a bucket decompresses its whole cluster
  into `clusterCache`, where up to 8 of them stay — a Wiki-panel
  session can hold 100–300 MB of decompressed clusters.
- Routing cells are fine: south-america packs 400 cells into 4 MB zstd
  clusters, and the cells index (38 MB on disk on the old Europe
  build, ~112 MB decompressed) is read once when Directions starts.

iOS gives the service-worker process a small memory allowance and
terminates it silently when exceeded; the next request restarts it and
the reader starts cold (for the preview that is one range request plus
the cluster pointer table). So the failure mode is slowness and
repeated cold starts rather than a visible error, and it is worst on
older phones. Android Chrome is more forgiving but a low-memory
device will kill the worker too.

Recommended fix (small): make both caches byte-budgeted rather than
count-bounded — e.g. 48 MB of decompressed clusters and 24 MB of blobs
— and never cache a cluster larger than the whole budget (a 45 MB
Wikidata cluster is a one-shot read). `LRU` in the reader already has
the shape for it (track a running byte total, evict oldest until under
budget). Optionally read `navigator.deviceMemory` in the worker where
available (Chrome only) to halve the budgets on ≤ 2 GB devices.

### 2. iOS focus zoom on the picker's URL box (fixed)

`web/drive/index.html` styled `#url-input` at 13 px. iOS Safari zooms
the page into any focused text input with a computed font size under
16 px unless the viewport forbids zoom; the picker's viewport meta
does not (`width=device-width, initial-scale=1, viewport-fit=cover`),
which is the right choice for accessibility. Changed to 16 px. The
viewer's own inputs are 12 px but its viewport carries
`maximum-scale=1.0, user-scalable=no`, which suppresses the focus zoom
(iOS still allows pinch-zoom for accessibility regardless of that
flag); the Find page's search inputs are `1rem`.

### 3. Tap targets (banner fixed; catalog recommended)

- The preview banner's × close was an 18 px glyph with 2 px padding.
  Now a 44 × 44 px hit area around the same glyph.
- Catalog cards (`web/template.html`, `.btn`): Download / Preview /
  Torrent / Info measure 79 × 30, 67 × 30, 62 × 30, 44 × 30 px on an
  iPhone 13 viewport, 8 px apart. They fit one row at 390 px, but
  30 px is below Apple's 44 pt and Android's 48 dp guidance and the
  Info button is narrow. Recommended: on `(pointer: coarse)` raise
  `.btn` padding to give ≥ 40 px height (e.g. `padding: 10px 14px;
  font-size: 13px`) — the row wraps to two lines on the narrowest
  phones, which is fine.

### 4. Local-file mode on phones (the preview sidesteps all of this)

- **Android Chrome** has no `showOpenFilePicker`, so the `<input
  type=file>` fallback is used and the resulting `File` is stored in
  IndexedDB. Chrome copies a stored File into its blob storage rather
  than keeping a reference to the picked file for files that come from
  the system file chooser (**device test** — the behaviour differs from
  desktop Chrome, where the picker's "file may have moved or changed"
  path shows it keeps a reference). A 10 GB ZIM would then be a 10 GB
  copy against the origin's quota and the picker would appear to hang
  while it copies. Worth testing with one large file and
  `navigator.storage.estimate()` before and after.
- **iOS Safari (browser tab)**: script-writable storage (IndexedDB,
  Cache API, the SW registration) is deleted after seven days of
  Safari use without visiting the site; a home-screen install with
  `display: standalone` is exempt. Storage is also separate between
  Safari and the installed app, so a ZIM picked in Safari is not
  available in the home-screen app and vice versa. The picker's hint
  ("on Safari you may need to re-pick it") covers the symptom; the
  cause is worth one sentence there.
- **Private Browsing** on iOS has no service workers; the picker
  already reports "Unsupported browser".

None of this touches the preview: it stores only a URL, and the bytes
come from the network each time.

### 5. Preview on phones — what was checked

- Emulated iPhone 13 and Pixel 5: picker, redirect, viewer, banner all
  render and fit (screenshots in the session). The banner sits above
  the attribution tag and clears the map controls; on a 390 px-wide
  screen it is four lines tall. Recommended: shorter copy on
  `(max-width: 480px)` ("Online preview, streamed from archive.org.
  Download for offline use.") to give the map more room.
- Live site through the Cloudflare worker (Chromium, desktop
  viewport): west-asia 9.9 GB — viewer ready 15.7 s, search index
  22 s, 109 range requests / 15.7 MB; east-coast-us 10.5 GB — viewer
  ready 18 s, 64.5 M-place search index 26 s, "Boston" 15 rows in
  3.4 s, Wiki panel 150 entries, 111 requests / 24 MB; the same with
  iPhone emulation on west-asia: 13.6 s / 19 s. A phone on cellular
  will be slower in proportion to its latency; each cold lookup is a
  chain of ~13 dependent requests on a 10 GB ZIM.
- **Data use**: 15–25 MB for a first look at a large region, more with
  panning, Wiki and Directions. Nothing warns a cellular user.
  Recommended: when `navigator.connection.saveData` is true (Chrome
  Android's Data Saver / Lite mode; not exposed by Safari) show one
  line in the banner, or gate auto-streaming from a catalog link
  behind a tap. Low effort, low risk.
- **CORS and preflights**: the reader sends `Range: bytes=a-b` and
  `?bytes=a-b`. Single-range `Range` values are CORS-safelisted, and
  WebKit implements that (see [MDN](https://developer.mozilla.org/en-US/docs/Glossary/CORS-safelisted_request_header)
  and [WebKit changeset 252047](https://trac.webkit.org/changeset/252047/webkit)),
  so Safari does not preflight each block URL. Verified on Chromium in
  the live run (no OPTIONS traffic to the worker beyond the harness's
  own).
- **Service-worker restarts**: WebKit's idle kill timer for service
  workers is minutes, Chrome's ~30 s, and both kill the worker when the
  app goes to the background on a phone. The preview restarts cleanly
  (one range request, then the cluster table; the browser HTTP cache
  usually answers both because the worker's ranges are aligned and
  marked cacheable). Tiles already on screen stay; the next pan is a
  cold start.

### 6. Smaller layout items (low)

- The build stamp (`#viewer-build-stamp`, top-left, `z-index: 9999`,
  `pointer-events: auto`) overlaps the left end of the search box on
  phone widths (the box is centred at `max-width: calc(100% - 100px)`).
  It can swallow a tap meant for the search box's left edge and it
  reads as clutter. Recommended: hide it under `(max-width: 480px)`
  unless `?debug=1`, or move it below the chip rail.
- `web/template.html` viewport meta lacks `viewport-fit=cover`; on
  notched phones the page background does not extend under the status
  bar. Cosmetic.
- The picker's diagnostics `<pre>` is 11 px; fine for a debug panel.

## Already handled well

- The viewer sizes itself from `visualViewport` and publishes
  `--app-height` / `--bottom-inset`, with `env(safe-area-inset-*)`
  padding on bottom sheets — the WKWebView toolbar problem is solved
  in code, not by `100vh`.
- Touch: `-webkit-overflow-scrolling: touch`, `overscroll-behavior:
  contain`, a body scroll lock, pointer/touch handlers with slop
  thresholds, `pointerdown` on chips.
- Standalone detection (`display-mode: standalone` /
  `navigator.standalone`) with iOS-specific pseudo-fullscreen handling.
- The service worker rebuilds redirected responses before caching
  (the iOS "response served by service worker has redirections"
  failure), uses `updateViaCache: 'none'`, network-first for the shell,
  and validates a ZIM before persisting it.
- The picker's manifest has `display: standalone`, icons, `start_url`
  and `scope` under `/drive/`, so an iOS home-screen install is exempt
  from the 7-day storage cap.
- Optional probe paths answer 200-with-marker instead of 404, keeping
  consoles quiet on both mobile browsers' devtools.

## Changes made in this review

- `web/drive/index.html`: URL box at 16 px (no iOS focus zoom).
- `resources/viewer/index.html` (and the PWA copy): 44 px hit area for
  the banner's ×.
- `cloud/preview_smoke_test.mjs`: `DEVICE="iPhone 13"` / `"Pixel 5"`
  emulation with screenshots of the picker and viewer; `CHROME_ARGS`
  for proxies; `SMOKE_SEARCH=<term>` and `SMOKE_WIKI=1` steps that
  exercise search shards and Wikidata buckets over the stream;
  `SITE_URL=` for the live site.

## Recommended next steps, in order

1. Byte-budget the reader's cluster and blob caches (§1).
2. Catalog `.btn` height ≥ 40 px on coarse pointers (§3).
3. Shorter banner copy under 480 px and a `saveData` line (§5).
4. Hide the build stamp on phone widths unless `?debug=1` (§6).
5. Device test on one Android phone and one iPhone: pick a 5–10 GB
   ZIM locally (storage estimate before/after, time to open), then open
   the same region's Preview button; note memory warnings in
   Safari's Web Inspector / Chrome's `chrome://inspect`.
