# PWA smoke test

Headless Puppeteer harness that exercises the live `streetzim.web.app/drive/`
PWA against a locally-served ZIM. Run after every change that touches the
service worker, viewer JS, places page, Firebase config, or the ZIM build —
silent regressions in any of those land as user-visible breakage and the
manual hand-test cycle is too slow.

## Run

```sh
# Default ZIM (silicon-valley, ~300 MB, fast)
node cloud/pwa_smoke_test.mjs

# Custom ZIM
ZIM_URL=http://localhost:8765/osm-canada-2026-04-25.zim node cloud/pwa_smoke_test.mjs

# Large ZIMs (anything past a few GB): pass the file itself. Without ZIM_FILE
# the test fetches ZIM_URL into a browser blob, which Chrome cannot do at
# 12 GB ("Failed to fetch"); with it the service worker gets an on-disk File,
# exactly as the picker gives a real user.
ZIM_FILE=/storage/streetzim/osm-east-coast-us-2026-09-11.zim \
  ZIM_URL=http://localhost:8765/osm-east-coast-us-2026-09-11.zim \
  SMOKE_ROUTE="40.7128,-74.0060;39.9526,-75.1652" SMOKE_SEARCH="Boston" \
  node cloud/pwa_smoke_test.mjs
# SMOKE_ROUTE and SMOKE_SEARCH come from cloud/regions.tsv (smoke_src,
# smoke_dst, smoke_search); ship-region.sh and build-refresh-queue.sh pass them.

# Watch it in a real Chrome window
HEADFUL=1 node cloud/pwa_smoke_test.mjs
```

The script needs:
- ZIMs served at `localhost:8765` (a `python -m http.server 8765` over the
  repo root works).
- System Chrome at `/Applications/Google Chrome.app/Contents/MacOS/Google
  Chrome` — Puppeteer's bundled Chromium has its sandbox blocked from
  reaching localhost, so we use the OS install.

## What it checks

| # | Step                  | Asserts                                                      |
|---|-----------------------|--------------------------------------------------------------|
| 1 | SW load               | Picker page → `set-zim` round-trip succeeds                  |
| 2 | Viewer ready          | `window.streetzimRouting.open` exists after viewer load       |
| 3 | Top-bar search        | Waits for the search manifest, types `SMOKE_SEARCH` (default "Palo Alto"), and requires at least one real result row. "Searching…" and "No results found" rows do not count. |
| 4 | Find chip             | `places.html` Restaurants chip → `#results` has rows         |
| 5 | Directions handoff    | Click Directions on first result → `#routing-dest-input` fills |
| 6 | Origin typeahead      | Type "Mount" in origin → `#routing-origin-results` populates  |

It is also strict about:
- **Console errors** — anything that lands as `console.error` or
  `pageerror` fails the run, attributed to the step that was active.
- **`/drive/*` 404s** — any 404 against the PWA scope (or the test ZIM)
  fails. Off-origin 404s (favicons on other CDNs, etc.) only log.

## Why each guardrail exists

These pieces of the PWA bit us hard enough that the smoke harness now
covers each by default:

- **Firebase `cleanUrls` + `trailingSlash`** make `places.html` canonical
  at `/drive/viewer/places/`. Relative URLs from there resolve against the
  trailing-slashed base — `index.html#dest=…` becomes
  `/drive/viewer/places/index.html` (404). All cross-page links must use
  `../index.html` (or absolute `/drive/viewer/`).
- **OSM `type` is singular** (`place`, not `places`) — the build emits
  `category-index/place.json`. Reverse-geocode gated this on the
  manifest's `categories.place` entry to avoid a hardcoded 404.
- **Optional probe paths** — the viewer fetches both
  `routing-data/graph-cells-index.bin` (v10+ spatial) and
  `routing-data/graph.bin` + `graph-chunk-manifest.json` (v8/v9
  monolithic), expecting the layout that wasn't built to be absent. The
  SW now returns `200 + X-Streetzim-Absent: 1 + empty body` for known
  optional probes instead of 404 — quiet in DevTools, easy for JS to
  detect via the response header. The JS drains the body before
  throwing, otherwise Chrome flags the response as
  `net::ERR_ABORTED`.
- **`loadGraph` re-entrance** — `applyHash` plus the origin/dest queue
  callers can each invoke `loadGraph` in the same tick. Without the
  in-flight latch, the loser of the parse race clobbers `graph` and
  the `.catch` fires the monolithic fallback even when the spatial
  load succeeded — surfacing as `Failed to load routing graph` after
  the route already drew.
- **Favicon** — browsers auto-fetch `/favicon.ico` from the origin
  root which isn't in our SW scope. `<link rel="icon" href="data:…">`
  on every PWA page suppresses the probe.

## Online-preview harness

`cloud/preview_smoke_test.mjs` covers the streaming path the same way:
it serves `web/drive/` locally, drives the picker with `?zim=<url>` and
checks the redirect into the viewer, the search index coming through the
service worker, the SW's own range-request tally, the preview banner and
console cleanliness. Two scenarios — `same-origin` (a local ZIM served
with Range by `scripts/serve-web-local.py`) and `proxy` (the real
archive.org file through `preview-proxy/serve-local.mjs`):

```sh
ZIM_FILE=/path/osm-washington-dc-2026-09-08.zim \
  CHROME_PATH=... node cloud/preview_smoke_test.mjs all
```

`SHOT_DIR=/tmp` saves a screenshot per scenario. The unit tests next to
it: `node tests/test_zim_http_source.mjs [zim]` (the range source) and
`node tests/test_preview_proxy.mjs [zim]` (the proxy, needs network).

## When to run

The standing rule: every diff that touches any of these triggers a
mandatory smoke run before the change is considered done.

- `web/drive/sw.js`
- `web/drive/zim-reader.js` (also `node tests/test_zim_http_source.mjs`)
- `web/drive/preview-config.js`, `preview-proxy/` (also
  `cloud/preview_smoke_test.mjs`)
- `resources/viewer/index.html`
- `resources/viewer/places.html`
- `web/drive/index.html`
- `firebase.json`
- `scripts/sync-drive-viewer.sh`
- `cloud/deploy_pwa.sh`
- `create_osm_zim.py` (anything touching `search-data`, `category-index`,
  `routing-data` layouts)

For ZIM-build changes, also run against a large ZIM (Canada is the
canonical large fixture — 28 GB, chunked spatial routing graph,
`split-find-chips` on, full Overture merge).

## Common diagnosis

A failing run prints `[FAIL] step — message` lines and per-step `! 404`
/ `! error` / `! reqfail` lines. The `[step]` tag on each error lets
you correlate which functional check it broke.

If a step times out, the harness prints a small `init-diag` JSON dump
with title, chip count, status text, derived HERE path, and which
manifests loaded — usually enough to tell whether the SW served the
right HTML, the page reached its bootstrapping init, and the manifest
fetches went through.
