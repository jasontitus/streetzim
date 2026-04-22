# In-ZIM HTML apps (search detail + Find places)

The streetzim ZIM ships two browser-side apps in addition to the
main map viewer at `index.html`. Both work without the LLM, without
Wikipedia, and without any network — same single ZIM, all data
already inside it.

| File | What it is | Triggered when |
| --- | --- | --- |
| `search/<slug>.html` | One detail page per indexed feature (place, airport, peak, park, water). Title, kind, coords, two CTAs. | User taps a Kiwix search result, or visits the title-index entry. |
| `places.html` | Search-and-browse mini-app. Search box, category chips (Restaurants, Cafés, Bars, Museums, Parks, Libraries, Shops, Gas), optional GPS-distance sort. | User taps the **Find** link in the main viewer's controls strip, or hits `places.html` directly. |

Both compose into the main viewer through a small URL-fragment
protocol the viewer parses on load and on every `hashchange`.

## URL fragment protocol (`index.html#…`)

`applyHash()` in `resources/viewer/index.html` recognises three
independent fragments. They can mix freely:

| Fragment | Behaviour |
| --- | --- |
| `map=<zoom>/<lat>/<lon>` | Fly the map to that view. Legacy "show this on the map" link — also produced by the auto-redirect search detail pages used to do. |
| `dest=<lat>,<lon>` | Open the routing panel via a programmatic `route-toggle` click, then call `setDestFromLatLon` with the supplied coords. The panel queues the pick if the routing graph hasn't loaded yet, so timing isn't an issue. |
| `origin=<lat>,<lon>` | Same as `dest=` but for the origin slot. Optional — usually paired with `dest=` when one app wants to dictate both endpoints. |
| `label=<text>` | URL-encoded display label for the destination pin/input. Optional. |

Search detail pages emit `dest=lat,lon&label=name` from their
**Directions to here** CTA. The Find-places mini-app emits the same
shape from each result row's primary CTA. Anything else that wants
to deep-link into routing (a kiwix bookmark, a custom search
engine, a third-party sidebar) only has to construct that one
fragment.

The `places.html` page ALSO understands `q=<text>` and `cat=<id>`
fragments, so the Firebase `/drive` PWA shell can open it with a
specific query or category pre-selected.

## Search detail pages

Generated in `create_osm_zim.py` by `search_detail_html(name,
kind_label, lat, lon, map_hash)`. Properties:

* No `<meta refresh>` — the previous behaviour was to instantly
  redirect to the map; that swallowed any chance to act on the
  page (e.g. tap a Directions button) and made search results
  unreadable when the user arrived via the title index instead of
  a tap.
* HTML-escapes the place name; URL-encodes the label for the hash
  payload.
* Two stacked CTAs: **Directions to here** (primary, blue) →
  `index.html#dest=lat,lon&label=…`, and **View on map** →
  `index.html#map=zoom/lat/lon`.
* Inline CSS, dark-mode media query, mobile viewport meta. No
  external assets.

Both build paths in `create_osm_zim.py` (the streamed-Xapian path
and the non-chunked search-features path) call the same helper, so
the two emit byte-identical pages for equivalent input.

## Find-places mini-app (`places.html`)

Pure vanilla JS, single file, no dependencies. Lives at
`resources/viewer/places.html` and is added to the ZIM by
`create_osm_zim.py` next to `index.html`.

Data sources (all read with `cache: 'force-cache'`):

* `search-data/manifest.json` — chunk index keyed by 2-char prefix.
* `search-data/<prefix>.json` — name-search chunks; lazy-loaded as
  the user types.
* `category-index/manifest.json` — optional category-keyed index
  (older builds may not have it; the app degrades gracefully).
* `category-index/<slug>.json` — full list of features for one
  OSM top-level type. Loaded once per chip tap and cached for the
  session.

Behaviour:

* **Name search** kicks in at 2 characters. The query's prefixes
  (computed the same way the build script chunks names) drive a
  small set of chunk fetches; substring matches are filtered
  client-side. Capped at 300 visible rows so big indices stay
  snappy on phones.
* **Category chip** loads the matching category index (one fetch,
  cached). Some chips further filter by subtype — e.g. **Cafés**
  loads the `poi` index and keeps `s == "cafe"`. Defined by the
  `CATEGORIES` table at the top of the file.
* **GPS toggle** asks the browser for a one-shot location, then
  switches the sort key from name to haversine distance and adds
  the distance to each row. Toggling off restores name sort.
* Each result row carries two CTAs styled the same as the search
  detail pages: **Directions** (writes the `dest=…&label=…`
  fragment) and **Map** (writes `map=…`).

The `Find` link in the main viewer (`#places-link` in the controls
strip) opens this app. Styled to match the satellite/3D/explore/
route buttons next to it; rendered as `<a>` rather than `<button>`
so middle-click and right-click open it in a new tab.

## Firebase `/drive` PWA integration

The drive PWA (`web/drive/`) precaches the viewer shell so it works
offline once installed. `places.html` is treated the same as
`index.html`:

* Listed in `SHELL_URLS` (`web/drive/sw.js`), so the SW pulls it
  from network on install and serves it from the shell cache
  thereafter.
* Listed in `VIEWER_SHELL_NAMES`, so the SW's request router
  short-circuits before reaching the IDB-backed ZIM reader (which
  doesn't have it under the `viewer/` prefix the SW expects).
* Copied alongside `index.html` by
  `scripts/sync-drive-viewer.sh`, the predeploy hook firebase.json
  invokes.

When you change either viewer file, bump `SHELL_CACHE` in
`sw.js` (e.g. `streetzim-drive-shell-vN` → `vN+1`) so existing
installs invalidate the old cache on next visit.

## What's not here yet

* **"Everything within 5 km" spatial browse.** GPS sort works on
  whatever results are already loaded (name search or category
  chip) but there's no spatial index that lets the app fetch all
  features in a bounding box without scanning every chunk. A
  geohash-keyed parallel index (e.g. `geo-data/<geohash5>.json`)
  in the build script would unlock that without changing the
  consumer protocol.
* **PWA manifest inside the ZIM.** Kiwix doesn't honour in-ZIM
  manifests, and the Firebase `/drive` shell already has its own.
  Skipped to keep the ZIM lean.
