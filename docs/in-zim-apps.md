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

## Category chips (current set)

The Find-places mini-app's chip row is driven by the `CATEGORIES`
table at the top of `resources/viewer/places.html`. Each chip
filters the `category-index/poi.json` records (or its own named
index for `parks`) by one or more of these selectors:

- **`subtypes`** — always-include when `r.s` (record subtype) exactly matches.
- **`includeRegex`** — always-include when `r.s` matches a regex. Catches
  Overture's `<noun>_<modifier>` conventions without enumerating every
  permutation (`_restaurant$|^food_`, `_museum$|_gallery$`, `_store$|^store$`).
- **`nameInclude`** — include when `r.s` is in a fallback bucket AND
  `r.n` matches a name regex. Used for "Museums" and "Landmarks" to
  pull items OMT collapsed into generic `tourism`/`attraction` buckets.

Current chips (order matters — left-to-right priority for horizontal space):

| Chip | subtypes / regex | Notes |
|---|---|---|
| Restaurants | `restaurant`, `fast_food`, `food_court`, `ice_cream` + `/_restaurant$|^food_/` | pulls in Overture's `italian_restaurant`, `thai_restaurant`, `food_court`, … |
| Cafés | `cafe`, `coffee_shop`, `bakery`, `tea_room`, `ice_cream_parlor` | — |
| Bars | `bar`, `pub`, `biergarten`, `nightclub`, `beer`, `alcohol_shop`, `wine_bar`, `sports_bar`, `cocktail_bar`, `dive_bar`, `beer_bar`, `brewery`, `wine_store`, `liquor_store` | liquor retail lumped in alongside drinking establishments |
| Hotels | `hotel`, `motel`, `hostel`, `bed_and_breakfast`, `lodging`, `inn`, `guest_house`, `resort`, `campsite` | — |
| Museums | `museum`, `art_gallery`, `planetarium`, `observatory` + `/_museum$|_gallery$/` + `nameInclude` over `tourism`/`attraction` | separates from Landmarks below; people conflate museums + galleries |
| Landmarks | `historic`, `castle`, `monument`, `historical_landmark`, `landmark_and_historical_building`, `memorial` + `nameInclude` over `tourism`/`attraction` | pulls the historic-rich subset of OMT's `tourism` bucket |
| Parks | — (uses the `park` category index directly) | |
| Libraries | `library`, `public_library` | |
| Health | `hospital`, `pharmacy`, `clinic`, `doctors`, `dentist`, `urgent_care_clinic`, `veterinary` | |
| Shops | `shop`, `supermarket`, `mall`, `marketplace`, `department_store`, `convenience`, `grocery`, `clothing_store`, `jewelry_store` + `/_store$|^store$/` | retail catchall |
| Gas | `fuel`, `charging_station`, `gas_station`, `ev_charging_station` | includes EV |

"Sort by distance" (GPS) is **on by default** — one-shot
`navigator.geolocation.getCurrentPosition` feeds each row's
haversine distance, and the result list sorts by that. Toggle off
to fall back to name sort.

## Overture places enrichment (per-record fields)

When the ZIM was built with `--overture-places <parquet>`, POI
records gain cleaner categories + website / phone / socials /
brand data. See `merge_overture_places` in `create_osm_zim.py`.

Two-pass enrichment:

1. **Pass 1 — enrich**: for each Overture row, look up an OSM POI by
   `(round(lat,4), round(lon,4), normalized_name)`. If hit, merge the
   Overture fields in place and rewrite `subtype` from noisy OMT
   buckets (`tourism`, `amenity`, `shop`, `attraction`, `leisure`,
   `car`, `historic`, `landuse`) to Overture's `categories.primary`.
   Specific OSM subtypes like `restaurant` survive unchanged.
2. **Pass 2 — add-new**: Overture rows with no OSM match become fresh
   `type: "poi"` records tagged `source: "overture"` and
   `subtype` = Overture primary category. Rows without a primary
   category are dropped (no useful chip assignment).

Extra fields the merge writes onto enriched / new records:

| Key | Value |
|---|---|
| `cat` | Overture primary category (`museum`, `hotel`, `ramen_restaurant`, …) |
| `w` | first website URL |
| `p` | first phone |
| `soc` | first 3 social URLs (array) |
| `brand` | brand primary name (string) |
| `wd` | brand Wikidata Q-ID. Never overwrites an OSM-supplied `wd` (the OSM one is entity-level, Overture's is brand-level — different Q-IDs) |
| `source` | `"overture"` only on newly-added (pass-2) records |

Empty enrichment fields are deliberately omitted from the JSON —
bloating every search-data chunk with `"w": ""` would kill the size
budget at continent scale.

The mini-app reads `cat`, `w`, `p`, `soc`, and `brand` and renders
them as a small "rich" row below each result (see `.rich .brand` +
`.rich .links` styles in `places.html`).

## Tests

`tests/test_overture.py` covers all of the above:

- 27 `_normalize_street` cases + idempotence guard,
- 27 `_STREET_ABBREV` canary entries + "no shadowed canonicals" invariant,
- 8 `merge_overture_addresses` end-to-end tests (pass-1 ID join,
  pass-2 coord / attr match, bbox, orphan rejection, empty parquet,
  append-only guarantee),
- 8 `merge_overture_places` end-to-end tests (enrich-existing,
  specific-subtype preservation, add-new with provenance,
  unnamed/uncategorized rejection, empty-field pruning, non-POI
  pass-through, OSM-wikidata vs Overture-brand-wikidata precedence).

Run with:

```sh
./venv312/bin/python3 -m pytest tests/test_overture.py -q
```

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
