# In-ZIM HTML apps (search detail + Find places)

The streetzim ZIM ships two browser-side apps in addition to the
main map viewer at `index.html`. Both work without the LLM, without
Wikipedia, and without any network — same single ZIM, all data
already inside it.

| File | What it is | Triggered when |
| --- | --- | --- |
| `search/<slug>.html` | One detail page per indexed feature (place, airport, peak, park, water). Title, kind, coords, two CTAs. | User taps a Kiwix search result, or visits the title-index entry. |
| `index.html` (chip rail) | On-map chip rail under the search input. Tapping a chip fetches the matching `category-index/chip-<id>.json` and renders the result set as pins + carousel directly on the map (no navigation). Also exposes a *"Search this area"* pill once the user pans/zooms. | Default UX for chip-based browsing as of 2026-05-10. |
| `places.html` | Search-and-browse mini-app — full list view, sort options, sub-filter chips, recent-searches dropdown, "Limit to map area" toggle. | User taps the **Find** link in the main viewer's controls strip, or hits `places.html` directly. Now a secondary surface; the on-map chip rail covers the common case. |

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

> **Chip rules are duplicated** — they live in two files that must
> stay in sync:
> - `cloud/chip_rules.py` — `CHIP_RULES` list, consumed at build time
>   by `record_matches_chip` to slice records into per-chip JSONs.
> - `resources/viewer/places.html` — `CATEGORIES` const, consumed at
>   runtime by the Find UI.
>
> A smaller hand-curated subset lives in `EXPLORE_CHIPS` at the top
> of `resources/viewer/index.html` (the map's "Explore" menu). Any
> chip change has to touch all relevant sites; the validator
> (`cloud/validate_zim.py` ~line 424) also lists chip IDs in its
> declared-count warnings.

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

Distance sort is implicit: the first chip tap or typed query asks
for a one-shot `navigator.geolocation.getCurrentPosition`
(`maybeFetchOriginFirst` → `requestGPS`), feeds each row's haversine
distance, and sorts by it. If the fix is denied, unsupported, or the
permission prompt is left unanswered (12 s), the pending query still
runs, name-sorted. "Search near …" lets the user pick a named place
as the origin instead. (The old on/off GPS toggle is gone.)

## Queued for next rebuild — merge Restaurants + Cafés

Apply this change in any region rebuilt 2026-05-09 or later.

**What:** Replace the separate "Restaurants" and "Cafés" chips with
a single **"Food & Drink"** chip (`id: "food"`).

**Why:** End users can't intuit the line between sit-down meal vs
coffee/pastry/bakery/tea — the labels alone don't communicate it. The
sub-filter chip UI already shipped (top-N `r.s` subtypes inside a
chip's results) surfaces "Italian restaurant · Bakery · Café · Fast
food" automatically, so a single Food & Drink chip with in-page
sub-filter is the better Google-Maps-style UX. Also fixes a real bug:
today `ice_cream` lands in Restaurants while `ice_cream_parlor` lands
in Cafés — same thing, different bucket.

**How — three files in lock-step:**

1. `cloud/chip_rules.py` — drop the `restaurants` and `cafes` rules,
   add:
   ```python
   ChipRule(id="food", label="Food & Drink", from_cat="poi",
            subtypes=("restaurant", "fast_food", "food_court",
                      "ice_cream", "ice_cream_parlor",
                      "cafe", "coffee_shop", "bakery", "tea_room"),
            include_regex=re.compile(
                r"_restaurant$|^food_|^coffee_|_bakery$|_cafe$"))
   ```
2. `resources/viewer/places.html` — same merge in the `CATEGORIES`
   const so `expandPrefix` / chip rendering matches.
3. `resources/viewer/index.html` — replace the two food entries in
   `EXPLORE_CHIPS` with `{ id: 'food', label: 'Food & Drink',
   emoji: '🍴' }`.
4. `cloud/validate_zim.py` ~line 424 — update the
   `("restaurants", "cafes", "shops")` declared==0 allowlist to
   `("food", "shops")`.
5. Update the chip table earlier in this doc.

**Migration order — important.** The viewer is baked into each ZIM,
so old ZIMs keep their old chip files. The PWA viewer at
`streetzim.web.app/drive` serves whatever ZIM the user loads, so:

- If you change the PWA `places.html` to query `chip-food.json` but
  the user loads an old ZIM with only `chip-restaurants.json` +
  `chip-cafes.json`, the PWA will 404 and show "0 matches" for Food
  & Drink.
- Safe path: hold the PWA + repo edits until the next regional
  rebuild wave is staged, then ship them in lockstep with the new
  ZIMs. Older ZIMs continue to work because *they* still bake the
  old chips.
- Alternative: put a fallback in `runChipQuery` that fetches
  `chip-restaurants.json` + `chip-cafes.json` and concatenates when
  `chip-food.json` 404s. Costs a one-line check + a second fetch on
  legacy ZIMs; lets the PWA viewer change ride ahead of rebuilds.

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

The mini-app reads `cat`, `ws` (website — `w` is the OSM Wikipedia
title), `p`, `soc`, and `brand` and renders them as a small "rich"
row below each result (see `.rich .brand` + `.rich .links` styles in
`places.html`). Only `http(s)://` values of `ws` / `soc` are linked.

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

## On-map Find chip rail (in `index.html`)

The primary chip-based find UX lives ON the map. A horizontal
scroll-snap chip rail is rendered just under the map's
`#search-input` (Restaurants · Cafés · Bars · Shops · Museums ·
Parks · Gas · Hotels). Tapping a chip:

1. Fetches `category-index/manifest.json` (cached after first
   load) to know whether the chip is sub-bucketed.
2. Fetches `category-index/chip-<id>.json` (or fans out the
   `chip-<id>-<NN>.json` sub-buckets in parallel and concatenates
   when the chip is split).
3. Filters records to the current map viewport. Initial chip-rail
   tap auto-falls-back to the unfiltered chip data when the
   viewport has 0 matches (better to surface what's around than
   show nothing).
4. Caps to top 300 records.
5. Pulls cached GPS as the carousel's distance-label origin.
6. Stashes a `{label, origin, items, chipId}` object and calls
   `renderFindResultsFromStash` so the existing pin/carousel/
   detail-panel stack lights up identically to the
   "On map" hand-off from `places.html`.

The stash carries `chipId` so `renderFindResultsFromStash` can
restore `_findResultsState.activeChip` on the new state — the
*"Search this area"* pill that appears after the user pans/zooms
needs this to know which chip's full dataset to re-fetch (vs.
filtering the in-memory items, which only cover the prior
viewport's matches).

When the pill is clicked, `loadChipOnMap` runs with
`requireInBounds: true`. If the new viewport has zero matches,
a "No <chip> in this area" toast fires and the previous carousel
stays put — the user explicitly asked for spatial constraint, so
we don't surface unrelated results from elsewhere.

`EXPLORE_CHIPS` in `index.html` is the chip-rail's source of truth
(must match `cloud/chip_rules.py` IDs — see
`project_chip_rules_duplicated.md`). The chip rail uses a CSS
`mask-image` gradient on its left/right edges so the horizontal-
scroll affordance is visually obvious.

### Sub-filter chip row inside the carousel

When a chip's results land, the carousel header is followed by a
second chip row built from the result set's `r.s` subtype
histogram (top 8 by count, count ≥ 2; "All" prepended when
narrowing). Tapping a sub-chip narrows the displayed pins +
carousel cards via `_applySubFilter`, which:

1. Re-stashes `_findResultsState.allItems` (the full chip dataset
   captured at render time) under `stash.items` plus the new
   `subFilter` field.
2. Calls `renderFindResultsFromStash`. That function captures
   `stash.items` as `_findResultsState.allItems` BEFORE applying
   the filter in place, so the histogram remains stable as the
   user narrows.

This was previously a places.html-only feature; folded into the
on-map carousel after the **Find** link was retired (see
"What's no longer in the chrome" below).

### Two popup systems on one map (find-result ↔ Wikidata)

There are two independent popup systems on the map:

- **Find-result marker popups.** Created by
  `_findResultPopup` for each pin in a chip's result set;
  `closeOnClick:false` so a stray map tap doesn't dismiss them
  mid-read.
- **Wikidata feature popups.** `initWikidataPopups` opens a
  popup anchored to the click point when the user taps any
  vector-tile feature whose `wikidata=Q*` tag matches an entry
  in our cached Wikidata. Singleton `currentPopup` inside that
  function's closure.

Without a cross-system bridge, alternating taps between the two
kinds of pin would stack both popups on screen. The bridge:

- `initWikidataPopups` exposes `map._closeWikidataPopup()` that
  closes its `currentPopup`. Its own click handler also walks
  `_findResultsState.markers` and closes any open find-result
  popups before opening a new Wikidata popup.
- Find-result marker click handlers (in
  `renderFindResultsFromStash`) and `_setActiveResult` (carousel
  scroll-snap) both call `map._closeWikidataPopup()` before
  MapLibre's default `togglePopup` opens this marker's popup.

Net: at most one popup of either kind is ever on screen. Add
similar bridge logic if a third popup system is added later.

### What's no longer in the chrome

- The `Find` link in the controls strip was removed 2026-05-10.
  The chip rail covers chip browsing inline; if a user wants
  full-list view + sort + recent-searches, they hit
  `/drive/viewer/places.html` directly.
- The `⊕ Explore` corner FAB was removed the same day. It
  duplicated the chip rail and conflicted with the Wikipedia
  panel button (now `📖 Wiki`, `#wiki-toggle`).

## Find-places mini-app (`places.html`)

Pure vanilla JS, single file, no dependencies. Lives at
`resources/viewer/places.html` and is added to the ZIM by
`create_osm_zim.py` next to `index.html`. Now a secondary surface
— the on-map chip rail covers the common case; this page is for
users who want the full controls (sort, sub-filter, recent
searches).

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
* **Origin / distance sort** — see "Distance sort is implicit"
  above. The sort row offers name / category / distance once a
  result set is showing.
* Each result row carries two CTAs styled the same as the search
  detail pages: **Directions** (writes the `dest=…&label=…`
  fragment, plus `origin=` only when the origin is a real GPS fix)
  and **Map** (writes `map=…&pin=…`).

The main viewer no longer has a dedicated `Find` link — the on-map
chip rail covers chip browsing, and `places.html` is reached
directly (Kiwix title index, the `/drive/viewer/places/` URL in the
PWA, or a host container).

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
