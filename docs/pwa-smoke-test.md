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
| 3 | Top-bar search        | Typing "Palo Alto" populates `#search-results`               |
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

## When to run

The standing rule: every diff that touches any of these triggers a
mandatory smoke run before the change is considered done.

- `web/drive/sw.js`
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
