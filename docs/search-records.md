# Search records and category files (data contract)

The search box, the Find page and the category chips all read the same
compact JSON records. This page is the contract for anyone who produces or
consumes them, including a port into openzim/maps
([openzim-integration.md](openzim-integration.md)). The binary routing
formats are in [formats.md](formats.md).

## Record

One JSON object per feature. Short keys, because a continent has tens of
millions of records.

| key | type | meaning |
|---|---|---|
| `n` | string | display name (`name:latin` when the tile has it, else `name`) |
| `t` | string | type, see below |
| `s` | string | subtype: the tile's `class` or `subclass` (for example `restaurant`, `cafe`, `primary`); `subclass` when `class` is only the raw OSM key tilemaker writes for values it has no class for (`amenity`, `tourism`, …; see `feature_subtype` in `streetzim/search_extract.py`); may be `""` |
| `a` | number | latitude, rounded to 5 decimal places (about 1 m; `SEARCH_COORD_DP` changes it) |
| `o` | number | longitude, likewise |
| `l` | string | location label: the nearest OSM place at extraction time (for example "Monte-Carlo"); records without one get "City, Region" from GeoNames via `reverse_geocoder` when the ZIM is written; may be `""` |

Optional keys (absent when empty):

| key | meaning |
|---|---|
| `w` | Wikipedia title from the OSM `wikipedia` tag, for example `en:Lincoln_Memorial` |
| `wsrc` | `"wd"` when `w` was backfilled from the Wikidata Q-ID instead |
| `q` | Wikidata Q-ID |
| `ws`, `p`, `soc`, `brand`, `wd` | website, phone, socials, brand name, brand Q-ID (Overture places) |
| `cat` | Overture's normalized category |
| `source` | `"overture"` for places added from Overture rather than OSM |
| `al` | admin_level, 2-10 (`admin` records) |
| `bb` | `[west, south, east, north]`, 5 dp: the area's bounding box (`admin` records whose polygon the extract has) |
| `alt` | other names: `name`/`name:en` (whichever is not `n`), `official_name`, `alt_name`, `short_name`, `loc_name`; at most 6 (`admin` records) |
| `osm` | the OSM object, `r<id>` or `w<id>` (`admin` records) |

Readers must ignore keys they don't know.

### `t` values

| `t` | from (OpenMapTiles layer) |
|---|---|
| `place` | `place` |
| `poi` | `poi` |
| `street` | `transportation_name` |
| `water` | `water_name`, `waterway` |
| `park` | `park` |
| `peak` | `mountain_peak` |
| `airport` | `aerodrome_label` |
| `building`, `area` | `building`, `landuse` (named features only; OpenFreeMap tiles carry no names in these) |
| `addr` | addresses from the OSM PBF (and Overture), not from tiles |
| `admin` | administrative areas: OSM boundary relations from the PBF, not from tiles ([below](#administrative-areas-t-admin)) |

Extraction: `extract_searchable_features` in `streetzim/search_extract.py`
(importable on its own; `tests/test_search_extract.py` has a fixture) reads the
z14 tiles of any OpenMapTiles MBTiles, which covers tilemaker output and
OpenFreeMap's Planetiler builds. Each feature becomes one point (the point
itself, a MultiPoint's mean, a line's middle vertex, or a polygon ring's
mean). Duplicates of (name, type, position to 4 dp) are dropped.

A street crossing several tiles is decoded once per tile. Just before the
ZIM is written, after any region cut, `merge_streets_in_file` merges pieces
that have the same name and the same location label and lie within 3 km of
each other. A merged street never spans more than 6 km. The survivor is the
piece nearest the middle, and it carries every piece's point in a private
`_pts` field, which is used for Wikipedia matching and never written to the
ZIM. The result is deterministic. `STREETZIM_MERGE_STREETS=0` turns it off.

Extraction output and search caches are not merged. In Monaco this takes
458 street records down to 319; every street name and every
Wikipedia-linked street is kept.

Known gap: only the Latin-script name is indexed (admin areas also carry
and are indexed under their other names, `alt`).

## Administrative areas (`t: "admin"`)

Countries, states, counties, cities, municipalities, wards: one record per
area, from `streetzim/admin_areas.py`, added after the addresses and from
the same OSM extract (added in 2026-09; older ZIMs have none).

- **Source.** `boundary=administrative` relations (and named closed ways)
  with an `admin_level` of 2-10 and a name, cut out with `osmium
  tags-filter` and assembled into polygons by pyosmium. The OpenMapTiles
  `boundary` layer has lines with no names below countries and the `place`
  layer has no counties or districts, so the tiles cannot give this; the
  PBF can, and the build already has it on both tile paths (tilemaker,
  and `--mbtiles`/`--mbtiles-url` with `--pbf`/`--pbf-url`, which the
  routing graph and the addresses read too). The extract before any bbox
  cut is read, so an area the cut clips keeps its whole polygon. A build
  from tiles alone (no PBF) has no admin areas, as it has no addresses.
  `--no-admin-areas` leaves them out.
- **Which.** An area whose representative point lies inside the build
  box, the rule every other record follows (and maps2zim's for GeoNames
  ADM points). An area that only touches the box is left out: the United
  States, Maryland and Virginia in a D.C. build.
- **Point** (`a`, `o`): the relation's `label` node, else its
  `admin_centre` node, when either lies inside the polygon; else the
  centroid of the largest outer ring when inside; else the middle of the
  polygon's widest span along that latitude.
- **Box** (`bb`): of the outer rings. The viewer fits it when the area is
  picked; the Kiwix page's "View on map" links `bounds=w,s,e,n` (fitted by
  this viewer) beside a `map=z/lat/lon` computed for a 1024x768 view (for
  older viewers), and a pin on the point.
- **Type** (`s`): the relation's `border_type` when it is a known type
  word; else the admin_level convention of its country (US 6 = county,
  US 9 = ward, FR 8 = commune, DE 6 = district, LU 6 = canton, ...
  `COUNTRY_LABELS`); else its `place` tag (which describes the settlement
  more than the unit: Luxembourg's cantons carry place=county); else a
  generic label (2 country, 4 region, 8 municipality, 10 neighbourhood).
  The country comes from the area's own or an enclosing area's
  `ISO3166-*` tag, else from GeoNames.
- **Region** (`l`): the name of the deepest enclosing area of level 6 or
  less (a D.C. ward: "District of Columbia"; a state: its country), else
  the GeoNames region of the place below or nearest to the point; empty
  for a country.
- **`w`/`q`**: the relation's own `wikipedia` / `wikidata` tags (not the
  name-and-point match other records get, which would pick the place node
  at the area's admin_centre).
- **Areas the extract clips.** A Geofabrik extract keeps its neighbours'
  relations but only the members inside its polygon, so the D.C. extract
  has Arlington County and Alexandria without a polygon. Such an area of
  admin_level 5 or more (a clipped country or state is never "in" the map)
  is kept when a point can be found without its geometry: its `label` or
  `admin_centre` node when the extract has it, else a GeoNames populated
  place of the same name (or place X in the second-level division
  "X County") within 40 km (levels 5-6), 25 km (7) or 12 km (8-10) of the
  members the extract has. The GeoNames table is the one the
  `reverse_geocoder` dependency ships (places over 1,000 people, CC BY 4.0,
  credited in the viewer's About panel), so nothing is downloaded. These
  records have no `bb`.
- **Duplicates.** The same name with the same box (within 1%) at two
  levels is kept once, at the lower level.
- **Search.** The record is written under the prefixes of its other names
  too (and `cloud/search_shards.py` plans its leaves from them), in tier
  `c`. The map's search box matches an admin record on any of its names,
  on "<name> <type>" and on "<type> of <name>" ("City of Alexandria"),
  skipping "of"/"de"/"la"/... for these records only, and ranks it with a
  type bonus of 25 plus an admin_level bonus (country 200, state 120,
  county 80, municipality 40, ward 15), so "Arlington" lists the county
  above a shop called Arlington. The Find page matches the same forms.
- **Kiwix.** Every admin record gets a page (`search/<slug>.html`, a front
  article, `<body data-type="admin">`), titled `Name (type)` unless the
  name already says the type ("Alexandria (city)", "Arlington County"),
  with its other names in the text for full-text search. Front-article
  redirects `search/<slug>~<k>.html` carry the other titles Kiwix should
  suggest: the other names, and "<Type> of <Name>" for city, town, village,
  borough, municipality, commune, canton, province and state ("City of
  Alexandria"), since Kiwix's title search wants every typed word in the
  title. `--xapian=builder` builds get the page title and the other names in
  the full text, not the redirects.
- **Cost** (measured 2026-09-30, same flags with and without, no routing):
  D.C. 18 areas (9 with a polygon, 9 clipped and placed from GeoNames),
  +40 KB ZIM (+0.2%), +5.8 KB search-data; Luxembourg 368 areas, +232 KB
  (+0.4%), +81 KB search-data. The step takes 3 s (D.C.) and 8 s
  (Luxembourg), most of it `osmium tags-filter`, whose ~1.5 GB peak is below
  the build's own (~3.7 GB, unchanged); build times were within noise.
- **Older readers.** A viewer from before this ignores `al`, `bb`, `alt`
  and `osm`, scores the record with no type bonus, shows its type and
  region, and flies to its point at zoom 15; `map=` in the Kiwix page takes
  it to the fitted view. This viewer on an older ZIM finds no `admin`
  records and behaves as before.

## `search-data/`

- `search-data/manifest.json`:
  ```json
  {"total": 1397, "chunks": {"mo": 12, "u5927": 3},
   "sub_chunks": {"de": ["de-0", "de-1"]},
   "char_split": {"ca": ["ca~r~c", "ca~s~p"]}}
  ```
  `chunks` maps a prefix to its record count. `sub_chunks` and `char_split`
  appear only when big prefixes were split.
- `search-data/{prefix}.json` is an array of records.
- **Prefix rule**: `cloud/search_shards.py` `prefix_key`. Normalize the
  word (NFKD, strip combining marks, lower-case, spaces become `_`).
  - If the first character is non-ASCII, the key is `u<hex>` of that
    character.
  - Otherwise the key is the first two characters, with anything that isn't
    alphanumeric mapped to `_`. A missing or non-ASCII second character also
    becomes `_`.
- **Splits**: hash splits `{prefix}-{0..f}`, and character/tier leaves
  `{prefix}~{chars}~{tier}`, where tier is `c` places (and admin areas),
  `p` POIs, `s` streets or `a` addresses. The viewer's `search-shards`
  block implements the reader.
  `tests/search_shards_js.test.mjs` checks it against the Python planner.

## `category-index/`

- `category-index/manifest.json`:
  `{"total": n, "categories": {"poi": n, …}, "category_shards"?: …, "chips"?: {…}}`
- `category-index/{t}.json` holds every record of one type. Builds with
  `--no-llm-bundle` omit these.
- `category-index/chip-{id}.json` holds one Find chip. Chip ids, labels and
  matching rules come from `cloud/chip_rules.py`; `rules_as_json()`
  exports them. Large chips are geographic shards
  `chip-{id}-g{hex}.json`, described in the chip's manifest entry
  `{label, count, bytes, sub_chunks?, layout?, shards?}`
  (`cloud/chip_shards.py`, `docs/find-chip-shards.md`).
