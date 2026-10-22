# Which label wins

MapLibre places a style's symbol layers from the last one down, so when
two labels collide the one in the later layer stays and the other is
dropped. Within a layer, a lower `symbol-sort-key` is placed first. The
viewer's style (`makeStyle` in `resources/viewer/src/index/130-lowzoom-lakes.js`)
orders its point labels, highest priority first:

| priority | layer | holds |
|---|---|---|
| 1 | `place-country` | countries (to z6) |
| 2 | `place-city` | cities (from z5) |
| 3 | `place-town` | towns (from z8) |
| 4 | `place-state` | regions: states, provinces (z4–z8) |
| 5 | `poi-label` | POIs (from z14) |
| 6 | `place-village` | villages (from z10) |
| 7 | `place-suburb` | suburbs, quarters, neighbourhoods (from z12) |

Below them come the water and road labels. Inside every one of these
layers the OpenMapTiles `rank` orders the features (`_SZ_PLACE_SORT`: a
populous city or a capital before a small one, an important POI before
a minor one; no rank goes last).

Why this order (2026-10-21/22):

- **Cities and towns win.** The layers used to run the other way round,
  so POIs, villages and the "Attica" region label hid Athens and
  Thessaloniki from z6 to z8: 2 of 16 checks (2 cities × z6–z9 × English
  and Greek) showed the city on osm-greece, 16 of 16 now.
- **POIs win over villages and neighbourhoods**, as they always had. With
  the neighbourhoods on top, Athens at z14 lost a fifth of its POIs, and a
  neighbourhood label hid Saint Francis Memorial Hospital in San Francisco.
  Suburbs share the neighbourhoods' layer, so they lose to POIs too.

  POIs placed at Athens z14 (37.975, 23.735) in a 390×844 phone viewport,
  counted with `queryRenderedFeatures` once the map was idle: 51 with the
  order before 2026-10-21, 41 with neighbourhoods above POIs, 54 now. The
  code reviewer's run of the same view counted 56 / 45 / 58; the counts
  move by a few between runs (tile timing, the label sort), the ratios do
  not. San Francisco z14 (37.7895, -122.4153), same viewport: 51 / 38 / 49.

`tests/viewer_style_js.test.mjs` pins the order and the sort key.

## Place types are OpenMapTiles classes

A search record's type is the tile's OpenMapTiles class where it has one
(`streetzim/search_extract.py` `feature_subtype`), and some classes lump
OSM values together (`resources/tilemaker/process-openmaptiles.lua`):
`beer` is pubs and beer gardens, `car` dealers, garages and taxi stands,
`college` colleges and universities, `railway` rail, tram and metro
stops. The UI tables name the whole class, and English shows "pub", "car
services" and "college or university" for the first three (`SZ_TYPE_EN`
in `resources/viewer/i18n/runtime.js`).
