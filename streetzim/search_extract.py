"""Searchable features from OpenMapTiles vector tiles.

Reads the z14 tiles of any OpenMapTiles MBTiles (tilemaker output, or
OpenFreeMap's Planetiler builds that openzim/maps uses) and turns every
named place, POI, street, water, park, peak and airport into one search
record. The record format is in docs/search-records.md.

Entry points:
  * extract_searchable_features(mbtiles_path=..., output_dir=...): stream
    to a sorted JSONL on disk (what builds use), or tiles=dict in memory.
  * build_location_index(mbtiles_path): (lat, lon) -> "City, Region".

Moved out of create_osm_zim.py (which re-exports these names) so it can be
imported without the builder. Worker functions stay at module level because
they run in spawn-started processes, which pickle them by module path. For
the same reason a script calling this needs the usual
``if __name__ == "__main__":`` guard (see tests/test_search_extract.py).
Streaming mode sorts with GNU ``sort`` (coreutils).
"""
import builtins
import functools
import gzip
import json
import multiprocessing
import os
import sqlite3
import time

from streetzim.cpus import build_cpus

# Line-buffered like create_osm_zim's logging, so progress shows up live in
# build logs.
print = functools.partial(builtins.print, flush=True)  # noqa: A001


def tile_to_lnglat(z, x, y, px, py, extent=4096):
    """Convert vector tile pixel coordinates to lng/lat.

    Args:
        z, x, y: Tile coordinates (XYZ scheme)
        px, py: Pixel coordinates within the tile (0..extent)
        extent: Tile extent (typically 4096)

    Returns:
        (longitude, latitude) tuple
    """
    import math
    n = 2.0 ** z
    lon = (x + px / extent) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (y + py / extent) / n)))
    lat = math.degrees(lat_rad)
    return lon, lat


# OSM keys that tilemaker's profile (resources/tilemaker/
# process-openmaptiles.lua, GetPOIRank: ``class = poiClasses[v] or k``)
# writes as a POI's ``class`` when it has no OpenMapTiles class for the
# value, e.g. amenity=pharmacy becomes class "amenity", subclass
# "pharmacy". None of them is an OpenMapTiles class, so a Planetiler tile
# (OpenFreeMap) never carries one. The profile's other fallback keys are
# real OpenMapTiles classes that Planetiler writes too (shop, railway,
# aerialway) and are kept; tests/test_search_extract.py checks this list
# against the profile's poiTags.
RAW_OSM_KEY_CLASSES = frozenset({
    "amenity", "barrier", "building", "highway", "historic", "landuse",
    "leisure", "sport", "tourism", "waterway",
})


def feature_subtype(props):
    """A search record's ``s`` from a tile feature's properties: its
    ``class``, or ``subclass`` when there is no class or the class is only
    the raw OSM key (``RAW_OSM_KEY_CLASSES``). So a tilemaker pharmacy is
    ``pharmacy``, as on OpenFreeMap, not ``amenity``. This brings the two
    tile sources closer, not level: where tilemaker's profile falls back to
    a real OpenMapTiles class, that class stays (a bakery is ``shop`` on
    tilemaker tiles; on OpenFreeMap it is ``bakery``)."""
    cls = props.get("class", "")
    sub = props.get("subclass", "")
    if cls in RAW_OSM_KEY_CLASSES and sub:
        return sub
    return cls or sub


# Schema of the search-features JSONL: 2 = features may carry
# ``name_native`` (the name in its own script). Recorded beside the file
# (``<path>.schema``) so a --search-cache made before can be recognised.
SEARCH_SCHEMA = 2


def mark_schema(features_path, languages=None):
    """Write <features_path>.schema: the schema, and the name:xx languages
    the features carry in ``names`` (STREETZIM_TILE_LANGUAGES when the
    extraction ran)."""
    langs = sorted(_languages() if languages is None else languages)
    with open(str(features_path) + ".schema", "w") as f:
        json.dump({"search_schema": SEARCH_SCHEMA, "languages": langs}, f)


def _schema(features_path) -> dict:
    try:
        with open(str(features_path) + ".schema") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def schema_of(features_path) -> int:
    try:
        return int(_schema(features_path).get("search_schema", 1))
    except (TypeError, ValueError):
        return 1


def schema_languages(features_path) -> list:
    """The name:xx languages a features file carries (none when unknown)."""
    return list(_schema(features_path).get("languages") or [])


def cache_language_error(cache_path, lang):
    """Why a build in ``lang`` cannot use this search cache, or None."""
    if lang == "en" or lang in schema_languages(cache_path):
        return None
    return (f"--language {lang} needs search features with name:{lang}, and this "
            f"search cache has none ({cache_path}.schema lists "
            f"{schema_languages(cache_path) or 'no languages'}): every result would "
            "be named in the place's own language. Build without --search-cache, or "
            f"extract a cache with STREETZIM_TILE_LANGUAGES={lang}.")


def copy_schema(src_path, dst_path):
    """Carry a features file's schema marker to a copy or cut of it."""
    import shutil
    if os.path.exists(str(src_path) + ".schema"):
        shutil.copyfile(str(src_path) + ".schema", str(dst_path) + ".schema")


def native_name(props, name):
    """The feature's name in its own script when the search name ``name``
    is another one (an English name:latin): OpenFreeMap tiles carry it as
    ``name``, StreetZim's tilemaker tiles as ``name_int`` (written only
    when OSM's name differs from name:latin). None when it is the same
    name once folded, or shorter than two characters."""
    from cloud.search_shards import norm
    nat = (props.get("name") or props.get("name_int") or "").strip()
    # An OSM ";" list ("東九州自動車道;延岡道路"): its first name.
    nat = nat.split(";")[0].strip()
    a, b = norm(nat), norm(name)
    # The same name, or one holding the other ("HOTEL ITAMI（ホテル伊丹）"):
    # nothing a search would miss, and the result line would repeat it.
    if len(nat) < 2 or a == b or (b and b in a) or (a and a in b):
        return None
    return nat


def _languages():
    """The name:xx languages a build keeps (create_osm_zim --language sets
    STREETZIM_TILE_LANGUAGES); none for English builds."""
    return [x for x in os.environ.get("STREETZIM_TILE_LANGUAGES", "").replace(" ", "").split(",")
            if x]


def search_record(name, feature_type, props, lat, lon):
    """The raw search-feature dict for one tile feature. When
    ``feature_subtype`` replaced a raw-key class by the subclass, the key
    is kept in ``osm_key``, so a later pass can still tell a generic
    bucket (``--overture-places`` refines ``amenity`` POIs to Overture's
    finer category, see streetzim/addresses.py). ``osm_key`` is internal
    to the feature JSONL: the ZIM's search records are built field by
    field and never carry it. OpenFreeMap tiles never produce it."""
    subtype = feature_subtype(props)
    rec = {"name": name, "type": feature_type, "subtype": subtype}
    cls = props.get("class", "")
    if cls and subtype != cls and cls in RAW_OSM_KEY_CLASSES:
        rec["osm_key"] = cls
    nat = native_name(props, name)
    if nat:
        rec["name_native"] = nat
    names = {lang: props[f"name:{lang}"].strip() for lang in _languages()
             if (props.get(f"name:{lang}") or "").strip()}
    if names:
        rec["names"] = names
    rec["lat"] = lat
    rec["lon"] = lon
    return rec


def build_location_index(mbtiles_path):
    """Build a spatial index that maps (lat, lon) to "City, State".

    Prefer the `reverse_geocoder` package (built on GeoNames data, ships a
    ~30 MB city/admin1 dataset, KNN for fast lookup). It handles the nasty
    cases the OMT-place-layer-based fallback can't — federal districts
    (D.C.), cross-country proximity (Yokohama → Kanagawa, not Sakhalin),
    subnational boundaries (NYC → New York, not New Jersey) — because the
    GeoNames data has the right admin1 for every populated place.

    Falls back to the original MVT-nearest-point approach if the package
    isn't installed, so offline/stripped environments still get a best-
    effort label.
    """
    try:
        import reverse_geocoder as _rg
        # Country code → name lookup. GeoNames returns ISO 3166-1 alpha-2;
        # we prefer the full name for the last-resort fallback.
        _COUNTRY_NAMES = {
            "US": "United States", "JP": "Japan", "CA": "Canada", "GB": "United Kingdom",
            "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy",
            "MX": "Mexico", "BR": "Brazil", "AR": "Argentina", "CN": "China",
            "IN": "India", "RU": "Russia", "AU": "Australia", "NZ": "New Zealand",
            "KR": "South Korea", "KP": "North Korea", "VN": "Vietnam", "TH": "Thailand",
            "ID": "Indonesia", "PH": "Philippines", "MY": "Malaysia", "SG": "Singapore",
            "PL": "Poland", "NL": "Netherlands", "BE": "Belgium", "CH": "Switzerland",
            "AT": "Austria", "CZ": "Czechia", "SE": "Sweden", "NO": "Norway",
            "FI": "Finland", "DK": "Denmark", "IE": "Ireland", "PT": "Portugal",
            "GR": "Greece", "HU": "Hungary", "RO": "Romania", "BG": "Bulgaria",
            "UA": "Ukraine", "TR": "Turkey", "IL": "Israel", "IR": "Iran",
            "SA": "Saudi Arabia", "EG": "Egypt", "ZA": "South Africa", "NG": "Nigeria",
            "KE": "Kenya", "MA": "Morocco", "LV": "Latvia", "LT": "Lithuania",
            "EE": "Estonia", "HK": "Hong Kong", "TW": "Taiwan",
        }
        # Pre-load once — reverse_geocoder is lazy but has a noisy first-call
        # log ("Loading formatted geocoded file..."), so trigger it here.
        _ = _rg.search([(0.0, 0.0)], mode=1)

        def _compose(entry):
            """Produce 'City, State' (or 'City' when the city IS its own admin region)."""
            if not entry:
                return ""
            name = (entry.get("name") or "").strip()
            admin1 = (entry.get("admin1") or "").strip()
            cc = (entry.get("cc") or "").strip()
            if name and admin1:
                # Collapse redundant "Tokyo, Tokyo" / "Moscow, Moscow" /
                # "Mexico City, Mexico City". If admin1 is already contained
                # in name (e.g. name="Washington, D.C.", admin1="Washington, D.C.")
                # or equal to name, just use the name.
                if admin1 == name or admin1 in name:
                    return name
                return f"{name}, {admin1}"
            if name:
                # Fall back to country when admin1 missing
                country = _COUNTRY_NAMES.get(cc, cc)
                return f"{name}, {country}" if country else name
            return ""

        def lookup(lat, lon):
            results = _rg.search([(lat, lon)], mode=1)
            return _compose(results[0]) if results else ""

        print("    Location index: reverse_geocoder (GeoNames)")
        return lookup
    except ImportError:
        # Fall through to the MVT-place-layer-based fallback below.
        pass


    import mapbox_vector_tile
    import math

    places = []  # [(lat, lon, name, class)]

    conn = sqlite3.connect(str(mbtiles_path))
    for z in range(0, 9):
        rows = conn.execute(
            "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = ?",
            (z,),
        )
        for col, tms_row, data in rows:
            y = (1 << z) - 1 - tms_row
            tile_data = data
            if data[:2] == b"\x1f\x8b":
                try:
                    tile_data = gzip.decompress(data)
                except Exception:
                    continue
            try:
                decoded = mapbox_vector_tile.decode(tile_data, y_coord_down=True)
            except Exception:
                continue
            layer = decoded.get("place")
            if not layer:
                continue
            extent = layer.get("extent", 4096)
            for feat in layer.get("features", []):
                props = feat.get("properties", {})
                cls = props.get("class", "")
                if cls not in ("state", "country", "city"):
                    continue
                name = props.get("name:latin") or props.get("name", "")
                if not name:
                    continue
                geom = feat.get("geometry", {})
                coords = geom.get("coordinates")
                if not coords:
                    continue
                gtype = geom.get("type", "")
                try:
                    if gtype == "Point":
                        px, py = coords[0], coords[1]
                    else:
                        continue
                except (IndexError, TypeError):
                    continue
                n = 2.0 ** z
                lon = (col + px / extent) / n * 360.0 - 180.0
                lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (y + py / extent) / n)))
                lat = math.degrees(lat_rad)
                places.append((lat, lon, name, cls))
    conn.close()

    if not places:
        print("    No state/country places found for location index")
        return None

    # Separate by class
    states = [(lat, lon, name) for lat, lon, name, cls in places if cls == "state"]
    countries = [(lat, lon, name) for lat, lon, name, cls in places if cls == "country"]
    cities = [(lat, lon, name) for lat, lon, name, cls in places if cls == "city"]

    # Deduplicate by (coord, name) only — NOT by name alone. Many city names
    # collide across regions (there are ~11 Washingtons, ~50 Springfields, etc.);
    # dedup-by-name would drop all but the first occurrence, which would fool
    # the nearest-neighbor lookup into labeling Dupont Circle as "Silver Spring,
    # Maryland" just because the first Washington encountered happened to be
    # in a different state. Keep one entry per physical place (coord rounded
    # to ~11 m so near-duplicate tile entries across zoom levels collapse).
    def _dedup(items):
        seen = set()
        result = []
        for lat, lon, name in items:
            key = (name, round(lat, 4), round(lon, 4))
            if key not in seen:
                seen.add(key)
                result.append((lat, lon, name))
        return result

    states = _dedup(states)
    countries = _dedup(countries)
    cities = _dedup(cities)

    print(f"    Location index: {len(states)} states, {len(countries)} countries, {len(cities)} cities")

    # Grid-based spatial index for fast nearest-neighbor (no scipy needed).
    # Bucket places into 1-degree grid cells for O(1) average lookup.
    def _build_grid(items):
        grid = {}
        for lat, lon, name in items:
            key = (int(lat), int(lon))
            grid.setdefault(key, []).append((lat, lon, name))
        return grid

    def _nearest_grid(lat, lon, grid):
        best = None
        best_dist = float("inf")
        cell_lat, cell_lon = int(lat), int(lon)
        # Search 5x5 grid neighborhood (handles items near cell boundaries)
        for dlat in range(-2, 3):
            for dlon in range(-2, 3):
                for plat, plon, name in grid.get((cell_lat + dlat, cell_lon + dlon), []):
                    d = (plat - lat) ** 2 + (plon - lon) ** 2
                    if d < best_dist:
                        best_dist = d
                        best = name
        return best

    # Cities are dense (~40k worldwide) — grid indexing pays off. States and
    # countries are sparse (a few thousand each) and their label points are
    # often far from the feature's actual coverage (e.g. California's point
    # is in Madera County, 4° east of Palo Alto — outside a 5×5 cell grid),
    # so we scan them linearly.
    city_grid = _build_grid(cities) if cities else {}

    def _nearest_linear(lat, lon, items):
        best = None
        best_dist = float("inf")
        for plat, plon, name in items:
            dlat = plat - lat
            dlon = plon - lon
            d = dlat * dlat + dlon * dlon
            if d < best_dist:
                best_dist = d
                best = name
        return best

    # Country bounding boxes for the geographies we serve. One country may
    # contribute multiple rectangles — a single bbox per country captures
    # ocean gaps (e.g. Japan's single rectangle would sweep in Primorsky Krai
    # and Sakhalin because they fall in the Sea of Japan between Japan's
    # island chain). Each row is
    #   (min_lat, min_lon, max_lat, max_lon, country_name_as_in_OMT).
    # Proper fix is admin boundary polygons; this table is the pragmatic 95%.
    _COUNTRY_BBOXES = [
        # Japan — archipelago, needs three rectangles to skip the Sea of Japan.
        (30.0,  130.0,  41.6,  142.1,  "Japan"),   # Honshu + Kyushu + Shikoku
        (41.0,  139.5,  45.6,  146.0,  "Japan"),   # Hokkaido
        (24.0,  122.9,  30.0,  131.5,  "Japan"),   # Ryukyu (Okinawa)
        # Korea — peninsula
        (33.0,  125.0,  38.7,  131.9,  "South Korea"),
        (37.5,  124.0,  43.0,  130.7,  "North Korea"),
        # China — main landmass (Tibet on the south, Inner Mongolia top, etc.)
        (18.0,   73.0,  54.0,  135.0,  "China"),
        (22.1,  113.8,  22.6,  114.5,  "Hong Kong"),
        # Russia — main landmass excludes Japanese exclusion zones by lat-split
        (50.0,   19.0,  82.0,  180.0,  "Russia"),  # most of Russia
        (41.0,   19.0,  50.0,  102.0,  "Russia"),  # southwest Russia, skirts China
        (45.6,  131.5,  50.0,  180.0,  "Russia"),  # Far East mainland (Primorsky, Khabarovsk)
        (45.6,  141.5,  54.5,  146.0,  "Russia"),  # Sakhalin
        # North America
        (24.0, -125.0,  49.5,  -66.5,  "United States"),
        (49.0, -141.0,  72.0,  -52.0,  "Canada"),  # main landmass (south. Ontario overlaps US bbox; see note below)
        (14.5, -118.5,  33.0,  -86.5,  "Mexico"),
        # Europe
        (41.0,   -5.5,  51.5,    9.8,  "France"),
        (36.0,   -9.6,  44.0,    3.4,  "Spain"),
        (36.0,    6.5,  47.2,   18.6,  "Italy"),
        (47.2,    5.8,  55.1,   15.1,  "Germany"),
        (49.8,   -7.7,  55.9,    1.9,  "United Kingdom"),
        (51.5,    3.3,  53.8,    7.3,  "Netherlands"),
        (49.5,    2.5,  51.6,    6.4,  "Belgium"),
        (45.7,    5.9,  47.9,   10.6,  "Switzerland"),
        (46.4,    9.5,  49.1,   17.2,  "Austria"),
        (49.0,   14.0,  54.9,   24.2,  "Poland"),
        (55.3,   20.8,  58.1,   28.3,  "Latvia"),
        (57.5,   21.8,  59.8,   28.3,  "Estonia"),
        (53.9,   20.9,  56.5,   26.9,  "Lithuania"),
        # Asia additional
        (6.0,    68.0,  37.1,   97.5,  "India"),
        (23.5,   59.0,  38.0,   78.2,  "Iran"),
        (22.0,   34.0,  31.7,   35.9,  "Egypt"),
        (20.3,  102.0,  28.7,  109.5,  "Vietnam"),
    ]
    def _country_by_bbox(lat, lon):
        for mn_lat, mn_lon, mx_lat, mx_lon, cname in _COUNTRY_BBOXES:
            if mn_lat <= lat <= mx_lat and mn_lon <= lon <= mx_lon:
                return cname
        return None

    # Pre-classify each state to its country (bbox lookup first, nearest-
    # country fallback). Bucketing states per country means we only ever
    # consider in-country candidates at lookup — that's what prevents
    # Yokohama → Sakhalin Oblast even if Sakhalin's label point is closer.
    states_by_country = {}
    if states:
        for s_lat, s_lon, s_name in states:
            sc = _country_by_bbox(s_lat, s_lon)
            if not sc and countries:
                sc = _nearest_linear(s_lat, s_lon, countries)
            states_by_country.setdefault(sc, []).append((s_lat, s_lon, s_name))
    # Same treatment for cities — a point on the Russia/Ukraine border
    # should pick up in-country cities even if another city is closer across
    # the line. Grid lookup inside this dict keeps city lookups fast.
    cities_by_country_grid = {}
    if cities:
        raw = {}
        for c_lat, c_lon, c_name in cities:
            cc = _country_by_bbox(c_lat, c_lon)
            if not cc and countries:
                cc = _nearest_linear(c_lat, c_lon, countries)
            raw.setdefault(cc, []).append((c_lat, c_lon, c_name))
        cities_by_country_grid = {k: _build_grid(v) for k, v in raw.items()}

    # City-state / federal-district bindings: places where the MVT `place`
    # layer doesn't carry a matching state-class entry, so nearest-state
    # would otherwise fall back to a neighboring US state, Russian oblast,
    # etc. Keyed on the nearest-city name PLUS a bbox, so Silver Spring or
    # Arlington (whose nearest city is themselves, not Washington) don't get
    # mislabeled as D.C.
    #
    # `label` None means "this city IS its own admin region — suppress the
    # state part entirely" (output "Tokyo" instead of "Tokyo, Tokyo").
    _CITY_STATE_BINDINGS = {
        # (min_lat, min_lon, max_lat, max_lon, state_label)
        # DC is a federal district not in our state-class tiles.
        "Washington": (38.79, -77.13, 39.00, -76.90, "D.C."),
        # Each of these is a municipality / metro prefecture that is its own
        # admin region; OMT doesn't carry a matching state entry.
        "Tokyo":       (35.45, 138.95, 35.95, 139.95, None),
        "Beijing":     (39.40, 115.40, 41.10, 117.50, None),
        "Shanghai":    (30.60, 120.85, 31.90, 122.20, None),
        "Hong Kong":   (22.15, 113.80, 22.58, 114.45, None),
        "Delhi":       (28.40, 76.80, 28.90, 77.35, None),
    }

    def lookup(lat, lon):
        # Pick country first so we can filter city/state candidates to only
        # those whose label points are in the same country — that's what
        # prevents Yokohama → Sakhalin Oblast / Primorsky Krai and similar
        # cross-border bugs. Bbox table wins over nearest-country-label.
        country = _country_by_bbox(lat, lon)
        if not country and countries:
            country = _nearest_linear(lat, lon, countries)
        city_grid_local = cities_by_country_grid.get(country, city_grid)
        city = _nearest_grid(lat, lon, city_grid_local) if city_grid_local else None
        state = None
        state_suppressed = False
        if city in _CITY_STATE_BINDINGS:
            mn_lat, mn_lon, mx_lat, mx_lon, label = _CITY_STATE_BINDINGS[city]
            if mn_lat <= lat <= mx_lat and mn_lon <= lon <= mx_lon:
                if label is None:
                    state_suppressed = True
                else:
                    state = label
        if state is None and not state_suppressed:
            in_country_states = states_by_country.get(country, [])
            state = _nearest_linear(lat, lon, in_country_states) if in_country_states else None
        # Format: "City, State" when both are known (best disambiguation).
        # If state is missing or suppressed but we have city + country, use
        # "City, Country" — still more informative than country alone. Avoids
        # "Yokohama" collapsing to just "Japan" because no Japanese prefecture
        # is tagged class=state in OMT.
        if city and state:
            return f"{city}, {state}"
        elif city and state_suppressed:
            return city
        elif city and country:
            return f"{city}, {country}"
        elif city:
            return city
        elif state:
            return state
        elif country:
            return country
        return ""

    return lookup


def _process_tile_partition(args):
    """Worker: read a tile_column range from SQLite, extract and dedup search features.

    Writes deduplicated features to a temp file (JSON lines) to avoid sending
    huge lists through multiprocessing IPC pipes."""
    mbtiles_path, col_start, col_end, search_layers, output_file = args[:5]
    # The five-field worker argument remains accepted by older library callers.
    search_zoom = args[5] if len(args) > 5 else 14
    import mapbox_vector_tile
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(mbtiles_path))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT zoom_level, tile_column, tile_row, tile_data "
        "FROM tiles WHERE zoom_level = ? AND tile_column >= ? AND tile_column < ?",
        (search_zoom, col_start, col_end),
    )

    seen = set()
    count = 0
    feat_count = 0
    out_f = open(output_file, "w")
    for z, x, tms_y, data in cursor:
        y = (1 << z) - 1 - tms_y
        tile_data = data
        if data[:2] == b"\x1f\x8b":
            try:
                tile_data = gzip.decompress(data)
            except Exception:
                count += 1
                continue

        try:
            decoded = mapbox_vector_tile.decode(tile_data, y_coord_down=True)
        except Exception:
            count += 1
            continue

        for layer_name, feature_type in search_layers.items():
            layer = decoded.get(layer_name)
            if not layer:
                continue
            extent = layer.get("extent", 4096)
            for feature in layer.get("features", []):
                props = feature.get("properties", {})
                name = props.get("name:latin") or props.get("name", "")
                if not name or len(name) < 2:
                    continue
                geom = feature.get("geometry", {})
                coords = geom.get("coordinates")
                if not coords:
                    continue
                geom_type = geom.get("type", "")
                try:
                    if geom_type == "Point":
                        px, py = coords[0], coords[1]
                    elif geom_type == "MultiPoint":
                        px = sum(c[0] for c in coords) / len(coords)
                        py = sum(c[1] for c in coords) / len(coords)
                    elif geom_type == "LineString":
                        mid = coords[len(coords) // 2]
                        px, py = mid[0], mid[1]
                    elif geom_type == "MultiLineString":
                        longest = max(coords, key=len)
                        mid = longest[len(longest) // 2]
                        px, py = mid[0], mid[1]
                    elif geom_type in ("Polygon", "MultiPolygon"):
                        ring = coords[0] if geom_type == "Polygon" else coords[0][0]
                        px = sum(c[0] for c in ring) / len(ring)
                        py = sum(c[1] for c in ring) / len(ring)
                    else:
                        continue
                except (IndexError, ZeroDivisionError, TypeError):
                    continue
                lon, lat = tile_to_lnglat(z, x, y, px, py, extent)
                dedup_key = (name.lower(), feature_type, round(lat, 4), round(lon, 4))
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                json.dump(search_record(name, feature_type, props, lat, lon),
                          out_f, separators=(",", ":"))
                out_f.write("\n")
                feat_count += 1
        count += 1

    out_f.close()
    conn.close()
    return output_file, count, feat_count


def _process_tile_for_search(args):
    """Worker function for parallel search feature extraction."""
    import mapbox_vector_tile
    z, x, y, data, search_layers = args

    tile_data = data
    if data[:2] == b"\x1f\x8b":
        try:
            tile_data = gzip.decompress(data)
        except Exception:
            return []

    try:
        decoded = mapbox_vector_tile.decode(tile_data, y_coord_down=True)
    except Exception:
        return []

    results = []
    for layer_name, feature_type in search_layers.items():
        layer = decoded.get(layer_name)
        if not layer:
            continue

        extent = layer.get("extent", 4096)

        for feature in layer.get("features", []):
            props = feature.get("properties", {})
            name = props.get("name:latin") or props.get("name", "")
            if not name or len(name) < 2:
                continue

            geom = feature.get("geometry", {})
            coords = geom.get("coordinates")
            if not coords:
                continue

            geom_type = geom.get("type", "")
            try:
                if geom_type == "Point":
                    px, py = coords[0], coords[1]
                elif geom_type == "MultiPoint":
                    px = sum(c[0] for c in coords) / len(coords)
                    py = sum(c[1] for c in coords) / len(coords)
                elif geom_type == "LineString":
                    mid = coords[len(coords) // 2]
                    px, py = mid[0], mid[1]
                elif geom_type == "MultiLineString":
                    longest = max(coords, key=len)
                    mid = longest[len(longest) // 2]
                    px, py = mid[0], mid[1]
                elif geom_type in ("Polygon", "MultiPolygon"):
                    ring = coords[0] if geom_type == "Polygon" else coords[0][0]
                    px = sum(c[0] for c in ring) / len(ring)
                    py = sum(c[1] for c in ring) / len(ring)
                else:
                    continue
            except (IndexError, ZeroDivisionError, TypeError):
                continue

            lon, lat = tile_to_lnglat(z, x, y, px, py, extent)
            results.append(search_record(name, feature_type, props,
                                         round(lat, 6), round(lon, 6)))

    return results


# Module-level helpers for multiprocessing location assignment
_place_grid = None


def _init_location_worker(grid_dict):
    global _place_grid
    _place_grid = grid_dict


def _assign_location_batch(batch):
    """Assign nearest place to a batch of features (module-level for pickling)."""
    results = []
    for f in batch:
        if f["type"] == "place":
            results.append(None)
            continue
        gx = int(f["lon"] * 2)
        gy = int(f["lat"] * 2)
        best_name = None
        best_dist = float("inf")
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                for p in _place_grid.get((gx + dx, gy + dy), []):
                    d = (p["lat"] - f["lat"]) ** 2 + (p["lon"] - f["lon"]) ** 2
                    if d < best_dist:
                        best_dist = d
                        best_name = p["name"]
        results.append(best_name)
    return results




def _annotate_lines_batch(lines, type_order):
    """Worker: parse a batch of raw jsonl lines, assign location context
    from the process-global place grid, and return the keyed output lines
    plus stats. Doing the JSON decode/encode here rather than in the
    parent is what makes the pool worth having — measured with the parent
    doing it, 32 workers sat at ~30% CPU while the parent capped the whole
    pass at ~20k lines/s."""
    feats = [json.loads(l) for l in lines]
    locs = _assign_location_batch(feats)
    out = []
    assigned = 0
    counts = {}
    for f, loc in zip(feats, locs):
        if loc:
            f["location"] = loc
            assigned += 1
        counts[f["type"]] = counts.get(f["type"], 0) + 1
        key = f["name"].replace("\t", " ").replace("\n", " ")
        out.append(f'{type_order.get(f["type"], 99)}\t{key}\t'
                   f'{json.dumps(f, separators=(",", ":"))}\n')
    return "".join(out), assigned, counts


def _finish_features_streaming(raw_path, output_dir, n_unique):
    """Location-context + sort + write-out without holding every feature.

    The in-memory version of this tail annotated a list of ~1.2e8 feature
    dicts and then `list.sort()`ed it. On the 2026-09-05 world build that
    reached 106 GB RSS before the location pass even started, filled a
    125 GB host's swap and had to be killed. Everything here is bounded
    by the place grid (the `place` subset only, ~1e6) plus one batch.

    Three passes over the file, which is cheap next to the tile scan that
    produced it:
      1. collect `place` features -> spatial grid (the only resident set)
      2. annotate in batches, emitting "<type_ord>\t<name>\t<json>"
      3. `sort` (external, spills to disk) then strip the key prefix
    """
    import subprocess
    from collections import defaultdict

    type_order = {"place": 0, "airport": 1, "peak": 2, "park": 3,
                  "water": 4, "poi": 5, "street": 6}

    print("    Assigning location context to features...", flush=True)
    place_grid = defaultdict(list)
    n_places = 0
    with open(raw_path, encoding="utf-8") as fin:
        for line in fin:
            f = json.loads(line)
            if f.get("type") == "place":
                place_grid[(int(f["lon"] * 2), int(f["lat"] * 2))].append(f)
                n_places += 1
    print(f"      {n_places} place features form the lookup grid", flush=True)

    global _place_grid
    _place_grid = dict(place_grid)
    del place_grid

    keyed_path = os.path.join(output_dir, "search_features.keyed")
    type_counts = {}
    assigned = 0
    BATCH = 50_000
    # Annotate in parallel, as the in-memory version did: one core over
    # 1.2e8 features took ~7 h (measured 4.35 GB of 14.5 GB in 2 h), the
    # pool takes tens of minutes. Batches are submitted with a bounded
    # in-flight window so memory stays at ~window x batch, and results
    # are consumed in submission order so the keyed file is deterministic.
    from collections import deque
    from concurrent.futures import ProcessPoolExecutor
    workers = min(32, build_cpus())
    window = workers * 2

    t_ann = time.time()
    done_rows = 0
    try:
        with open(raw_path, encoding="utf-8") as fin, \
                open(keyed_path, "w", encoding="utf-8") as fout, \
                ProcessPoolExecutor(max_workers=workers,
                                    mp_context=multiprocessing.get_context("spawn"),
                                    initializer=_init_location_worker,
                                    initargs=(_place_grid,)) as pool:
            inflight = deque()          # (batch, future) in submission order
            batch = []

            def drain(all_of_them=False):
                nonlocal done_rows, assigned
                while inflight and (all_of_them or len(inflight) >= window or inflight[0][1].done()):
                    n_lines, fut = inflight.popleft()
                    text, n_assigned, counts = fut.result()
                    fout.write(text)
                    assigned += n_assigned
                    for k, v in counts.items():
                        type_counts[k] = type_counts.get(k, 0) + v
                    done_rows += n_lines
                    if done_rows % (BATCH * 40) == 0:
                        rate = done_rows / max(1e-9, time.time() - t_ann)
                        print(f"      annotated {done_rows:,}/{n_unique:,} ({rate:,.0f}/s, "
                              f"~{(n_unique - done_rows) / rate / 60:.0f} min left)", flush=True)

            # The parent only moves text: raw lines out to the workers, keyed
            # lines back to disk. All parsing and serialising happens in the pool.
            for line in fin:
                batch.append(line)
                if len(batch) >= BATCH:
                    inflight.append((len(batch), pool.submit(_annotate_lines_batch, batch, type_order)))
                    batch = []
                    drain()
            if batch:
                inflight.append((len(batch), pool.submit(_annotate_lines_batch, batch, type_order)))
            drain(all_of_them=True)
    finally:
        # The place grid can hold millions of dicts. It is only needed by
        # annotation; do not retain it through sorting or later build phases.
        _place_grid = None
        # A shut-down executor still owns its initializer arguments.
        pool = None
    print(f"    Assigned location to {assigned}/{n_unique} features "
          f"in {(time.time() - t_ann) / 60:.0f} min on {workers} workers", flush=True)
    os.unlink(raw_path)

    print("    Sorting (external, disk-backed)...", flush=True)
    sorted_path = os.path.join(output_dir, "search_features.sorted")
    env = dict(os.environ, LC_ALL="C")
    # -S bounds sort's own buffer; -T keeps its spill next to the data
    # rather than on a small /tmp.
    subprocess.run(["sort", "-t", "\t", "-k1,1n", "-k2,2",
                    "-S", os.environ.get("STREETZIM_SORT_MEM") or sort_buffer(),
                    "-T", output_dir, "-o", sorted_path, keyed_path],
                   check=True, env=env)
    os.unlink(keyed_path)

    features_path = os.path.join(output_dir, "search_features.jsonl")
    with open(sorted_path, encoding="utf-8") as fin, \
            open(features_path, "w", encoding="utf-8") as fout:
        for line in fin:
            parts = line.split("\t", 2)
            if len(parts) == 3:
                fout.write(parts[2])
    os.unlink(sorted_path)

    print(f"    Extracted {n_unique} searchable features")
    for t, c in sorted(type_counts.items()):
        print(f"      {t}: {c}")
    size_mb = os.path.getsize(features_path) / (1024 * 1024)
    print(f"    Wrote {n_unique} features to disk ({size_mb:.0f} MB)", flush=True)
    mark_schema(features_path)
    return features_path


def sort_buffer(limit: int | None = None) -> str:
    """sort -S for the feature sort: 4 GiB, or a quarter of the cgroup
    memory limit when that is less (4 GiB is half of an 8 GiB container).
    STREETZIM_SORT_MEM overrides it."""
    from streetzim.cpus import memory_limit
    limit = memory_limit() if limit is None else limit
    if limit is None or limit >= 16 << 30:
        return "4G"
    return f"{max(limit // 4, 1 << 20) >> 10}K"


def extract_searchable_features(tiles=None, mbtiles_path=None, output_dir=None):
    """Extract named features from z14 vector tiles for search indexing.

    Decodes the highest-zoom tiles and extracts features with names from
    the place, poi, transportation_name, water_name, park, mountain_peak,
    and aerodrome_label layers.

    Can operate in two modes:
    - tiles=dict: legacy mode, filters z14 from in-memory dict
    - mbtiles_path=str: streaming mode, reads z14 directly from SQLite

    If output_dir is set, writes features to a JSONL file on disk and returns
    the file path (freeing the in-memory list). Otherwise returns a list of dicts.
    """
    import mapbox_vector_tile  # noqa: F401 -- fail before forking workers if missing

    print("  Extracting searchable features from tiles...")

    # Layers that contain searchable named features
    search_layers = {
        "place": "place",
        "poi": "poi",
        "transportation_name": "street",
        "water_name": "water",
        "waterway": "water",
        "park": "park",
        "mountain_peak": "peak",
        "aerodrome_label": "airport",
        "building": "building",
        "landuse": "area",
    }

    if mbtiles_path:
        # Streaming mode: each worker reads its own partition from SQLite
        conn = sqlite3.connect(str(mbtiles_path))
        search_zoom = 14
        total_tiles = conn.execute(
            "SELECT COUNT(*) FROM tiles WHERE zoom_level = 14"
        ).fetchone()[0]
        if total_tiles == 0:
            search_zoom = conn.execute("SELECT MAX(zoom_level) FROM tiles").fetchone()[0]
            if search_zoom is not None:
                total_tiles = conn.execute(
                    "SELECT COUNT(*) FROM tiles WHERE zoom_level = ?", (search_zoom,)
                ).fetchone()[0]
                print(f"    No z14 tiles found, using z{search_zoom}")
        if not total_tiles:
            conn.close()
            print("    No tiles found in mbtiles")
            if output_dir:
                features_path = os.path.join(output_dir, "search_features.jsonl")
                open(features_path, "w").close()
                return features_path
            return []

        # Balanced partitioning: query tile counts per column and split evenly
        print("    Querying tile distribution for balanced partitioning...")
        col_counts = conn.execute(
            "SELECT tile_column, COUNT(*) FROM tiles WHERE zoom_level = ? "
            "GROUP BY tile_column ORDER BY tile_column", (search_zoom,)
        ).fetchall()
        conn.close()

        import tempfile
        from contextlib import ExitStack
        num_workers = min(build_cpus(), len(col_counts))
        # Use 4x more partitions than workers for dynamic load balancing —
        # dense urban partitions take longer per tile, so small partitions let
        # idle workers pick up the next chunk instead of waiting on one straggler.
        num_partitions = min(num_workers * 4, len(col_counts))
        print(f"    Processing {total_tiles} z{search_zoom} tiles across {len(col_counts)} columns "
              f"with {num_workers} workers, {num_partitions} partitions...")

        # Split columns into partitions with roughly equal tile counts
        tiles_per_partition = total_tiles / num_partitions
        partitions = []
        # Own worker scratch for the entire scan/dedup lifetime. The pool
        # exits before cleanup, so failed workers cannot leave large files or
        # continue writing into a removed directory.
        with tempfile.TemporaryDirectory(prefix="streetzim_search_", dir=output_dir) as tmp_dir, \
                ExitStack() as streams:
            current_start = col_counts[0][0]
            current_count = 0
            part_idx = 0

            for col, cnt in col_counts:
                current_count += cnt
                if current_count >= tiles_per_partition and part_idx < num_partitions - 1:
                    tmp_file = os.path.join(tmp_dir, f"features_{part_idx}.jsonl")
                    partitions.append((mbtiles_path, current_start, col + 1, search_layers,
                                       tmp_file, search_zoom))
                    part_idx += 1
                    current_start = col + 1
                    current_count = 0

            # Last partition gets the rest
            if part_idx < num_partitions:
                tmp_file = os.path.join(tmp_dir, f"features_{part_idx}.jsonl")
                last_col = col_counts[-1][0]
                partitions.append((mbtiles_path, current_start, last_col + 1, search_layers,
                                   tmp_file, search_zoom))

            processed = 0
            total_features = 0
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(num_workers) as pool:
                for _output_file, batch_count, batch_feats in pool.imap_unordered(
                    _process_tile_partition, partitions
                ):
                    processed += batch_count
                    total_features += batch_feats
                    print(f"\r    Processed {processed}/{total_tiles} tiles, {total_features} features (pre-dedup)...", end="", flush=True)

            print()

            # Stream features from temp JSONL files for cross-worker dedup
            print(f"    Cross-worker deduplication from {len(partitions)} temp files...")
            # Memory shape matters here: at planet scale this pass sees ~1.2e8
            # features. Keeping the dedup keys as tuples of
            # (str, str, float, float) AND accumulating every unique feature
            # dict in a list drove this process to 106 GB RSS on a 125 GB host
            # (2026-09-05, world tiles v3) — deep into swap, with the OOM
            # killer one allocation away.
            #
            # Two changes keep it bounded:
            #   * the dedup key becomes a 64-bit blake2b digest as a Python
            #     int (~50 B in a set, versus several hundred for the tuple).
            #     Expected collisions across 1.2e8 keys are ~4e-4, i.e. none
            #     in practice, and a collision would drop one duplicate-
            #     looking feature from a search index.
            #   * when we are writing to disk anyway (output_dir set, which is
            #     how every real caller runs), features stream straight to the
            #     jsonl instead of piling up in a list.
            import hashlib
            stream_out = None
            if output_dir:
                raw_path = os.path.join(output_dir, "search_features.raw.jsonl")
                stream_out = streams.enter_context(open(raw_path, "w", encoding="utf-8"))
            features = []
            seen_global = set()
            n_unique = 0
            for part_args in partitions:
                tmp_file = part_args[4]
                if not os.path.exists(tmp_file):
                    continue
                with open(tmp_file) as f:
                    for line in f:
                        feat = json.loads(line)
                        dedup_key = int.from_bytes(hashlib.blake2b(
                            ("%s\x00%s\x00%.4f\x00%.4f" % (
                                feat["name"].lower(), feat["type"],
                                feat["lat"], feat["lon"])).encode("utf-8"),
                            digest_size=8).digest(), "big")
                        if dedup_key not in seen_global:
                            seen_global.add(dedup_key)
                            n_unique += 1
                            if stream_out is not None:
                                stream_out.write(json.dumps(feat, separators=(",", ":")) + "\n")
                            else:
                                features.append(feat)
                os.unlink(tmp_file)
            del seen_global
        print(f"    {n_unique} unique features after cross-worker dedup")
        if stream_out is not None:
            # Location context and the final sort both used to require every
            # feature resident. Do them over the file instead.
            return _finish_features_streaming(raw_path, output_dir, n_unique)
    else:
        # Legacy mode: filter from in-memory dict
        z14_tiles = {(z, x, y): data for (z, x, y), data in tiles.items() if z == 14}
        if not z14_tiles:
            max_z = max(z for z, x, y in tiles.keys())
            z14_tiles = {(z, x, y): data for (z, x, y), data in tiles.items() if z == max_z}
            print(f"    No z14 tiles found, using z{max_z}")

        features = []
        num_workers = build_cpus()
        total_tiles = len(z14_tiles)
        print(f"    Processing {total_tiles} z14 tiles with {num_workers} workers...")

        tile_iter = (
            (z, x, y, data, search_layers)
            for (z, x, y), data in z14_tiles.items()
        )
        chunk_size = max(1, total_tiles // (num_workers * 4))
        processed = 0

        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(num_workers) as pool:
            for batch_features in pool.imap_unordered(
                _process_tile_for_search,
                tile_iter,
                chunksize=chunk_size,
            ):
                features.extend(batch_features)
                processed += 1
                if processed % 5000 == 0:
                    print(f"\r    Processed {processed}/{total_tiles} tiles, {len(features)} features so far...", end="", flush=True)

        if processed > 5000:
            print()  # Newline after progress

    # Ensure multiprocessing cleanup before libzim
    import gc
    gc.collect()

    # Deduplicate across tiles (only needed for legacy path; mbtiles path dedups inline)
    if not mbtiles_path:
        seen = set()
        deduped = []
        for f in features:
            dedup_key = (f["name"].lower(), f["type"], round(f["lat"], 4), round(f["lon"], 4))
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            deduped.append(f)
        features = deduped

    # Assign location context (nearest city/town) to each feature
    print("    Assigning location context to features...")
    places = [f for f in features if f["type"] == "place"]
    if places:
        # Build a coarse spatial grid of places for fast nearest-neighbor lookup
        # Grid cells are ~0.5 degrees (~50km)
        from collections import defaultdict
        place_grid = defaultdict(list)
        for p in places:
            gx = int(p["lon"] * 2)
            gy = int(p["lat"] * 2)
            place_grid[(gx, gy)].append(p)

        # Convert to regular dict for pickling (multiprocessing)
        place_grid_dict = dict(place_grid)

        # For small feature sets, run directly; for large ones, use multiprocessing
        if len(features) > 100_000:
            from concurrent.futures import ProcessPoolExecutor
            batch_size = max(10_000, len(features) // build_cpus())
            batches = [features[i:i + batch_size] for i in range(0, len(features), batch_size)]
            num_workers = min(build_cpus(), len(batches))

            assigned = 0
            with ProcessPoolExecutor(
                max_workers=num_workers,
                # Match the tile workers: Python 3.14's forkserver default
                # needs a Unix socket, which Docker bind-mounted scratch
                # directories (and long --tmp paths) may not support.
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_location_worker,
                initargs=(place_grid_dict,),
            ) as pool:
                for batch_idx, locs in enumerate(pool.map(_assign_location_batch, batches)):
                    start_idx = batch_idx * batch_size
                    for j, loc in enumerate(locs):
                        if loc:
                            features[start_idx + j]["location"] = loc
                            assigned += 1
        else:
            # For small sets, set the global directly and run in-process
            global _place_grid
            _place_grid = place_grid_dict
            try:
                assigned = 0
                locs = _assign_location_batch(features)
                for j, loc in enumerate(locs):
                    if loc:
                        features[j]["location"] = loc
                        assigned += 1
            finally:
                _place_grid = None

        print(f"    Assigned location to {assigned}/{len(features)} features")

    # Sort by type priority then name
    type_order = {"place": 0, "airport": 1, "peak": 2, "park": 3, "water": 4, "poi": 5, "street": 6}
    features.sort(key=lambda f: (type_order.get(f["type"], 99), f["name"]))

    print(f"    Extracted {len(features)} searchable features")
    type_counts = {}
    for f in features:
        type_counts[f["type"]] = type_counts.get(f["type"], 0) + 1
    for t, c in sorted(type_counts.items()):
        print(f"      {t}: {c}")

    if output_dir:
        features_path = os.path.join(output_dir, "search_features.jsonl")
        with open(features_path, "w") as fout:
            for feat in features:
                fout.write(json.dumps(feat, separators=(",", ":")) + "\n")
        count = len(features)
        del features
        import gc; gc.collect()
        size_mb = os.path.getsize(features_path) / (1024 * 1024)
        print(f"    Wrote {count} features to disk ({size_mb:.0f} MB)")
        mark_schema(features_path)
        return features_path

    return features


# --- Names in the build's language (create_osm_zim --language) ---------

def localize_feature(feat, lang):
    """Add the names a ``lang`` build shows: ``display_name`` (OSM's
    name:lang, else the place's own name), ``display_native`` (its own
    name when that differs) and ``display_latin`` (the English / Latin
    name when it differs from both). ``name`` itself stays: dedup, street
    merging and the Wikipedia cross-references key on it. The writer
    emits records from the display fields (docs/search-records.md)."""
    from cloud.search_shards import norm
    name = feat.get("name") or ""
    native = feat.get("name_native") or name
    disp = ((feat.get("names") or {}).get(lang) or native).strip()
    feat["display_name"] = disp
    if norm(native) != norm(disp):
        feat["display_native"] = native
    if feat.get("name_native") and norm(name) not in (norm(disp), norm(native)):
        feat["display_latin"] = name
    return feat


def localize_features_file(path, lang):
    """localize_feature over a features JSONL, in place. English builds
    (``lang`` "en") are left untouched."""
    if lang == "en":
        return 0
    tmp = str(path) + ".lang"
    n = 0
    with open(path, encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            fout.write(json.dumps(localize_feature(json.loads(line), lang),
                                  ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    os.replace(tmp, path)
    return n


# --- Street merging (runs on the final search-features file, see
# merge_streets_in_file; NOT during extraction, so extraction output and
# search caches stay exactly as they were) -----------------------------------
#
# A street crossing several z14 tiles is decoded once per tile, so search
# listed "Avenue Saint-Martin" 27 times across Monaco's chunks. Pieces of one
# street sit about one tile apart (<= 2.45 km at z14).
STREET_MERGE_KM = 3.0      # link pieces this close (single linkage)
STREET_MAX_EXTENT_KM = 6.0 # never grow a merged street beyond this box, so a
                           # search result still lands near the part you want
STREET_MERGE_MAX_GROUP = 2000  # same name + same place above this: leave as is


def _km(a_lat, a_lon, b_lat, b_lon):
    import math
    kx = 111.32 * math.cos(math.radians((a_lat + b_lat) / 2))
    return math.hypot((a_lat - b_lat) * 111.32, (a_lon - b_lon) * kx)


def merge_street_records(records):
    """Merge street records that are pieces of one street.

    ``records`` all share a name. Pieces with the same ``location`` label
    that lie within STREET_MERGE_KM of each other are merged (closest pairs
    first), as long as the merged group stays within a STREET_MAX_EXTENT_KM
    box. Each group keeps one existing record, the piece nearest the group's
    centre, with the coordinates of every piece in ``_pts`` so later steps
    (Wikipedia matching, region filters) can still see them. ``_pts`` is not
    written into the ZIM.

    Deterministic: the result depends only on the records' content, not on
    their order. The location label keeps same-named streets in neighbouring
    places apart.
    """
    import math
    n = len(records)
    if n < 2:
        return list(records)
    by_loc = {}
    for i, r in enumerate(records):
        by_loc.setdefault(r.get("location") or "", []).append(i)

    parent = list(range(n))
    box = [(r["lat"], r["lat"], r["lon"], r["lon"]) for r in records]

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    deg_lat = STREET_MERGE_KM / 111.32
    for idxs in by_loc.values():
        if len(idxs) < 2 or len(idxs) > STREET_MERGE_MAX_GROUP:
            continue
        max_lat = max(abs(records[i]["lat"]) for i in idxs)
        deg_lon = deg_lat / max(math.cos(math.radians(min(max_lat, 89.0))), 0.01)
        cells = {}
        for i in idxs:
            r = records[i]
            cells.setdefault((int(r["lat"] // deg_lat), int(r["lon"] // deg_lon)), []).append(i)
        pairs = []
        for (cy, cx), members in cells.items():
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for j in cells.get((cy + dy, cx + dx), ()):
                        for i in members:
                            if i >= j:
                                continue
                            a, b = records[i], records[j]
                            d = _km(a["lat"], a["lon"], b["lat"], b["lon"])
                            if d <= STREET_MERGE_KM:
                                ka = (a["lat"], a["lon"], a.get("subtype") or "")
                                kb = (b["lat"], b["lon"], b.get("subtype") or "")
                                pairs.append((d, min(ka, kb), max(ka, kb), i, j))
        pairs.sort(key=lambda p: p[:3])
        for _d, _ka, _kb, i, j in pairs:
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            a, b = box[ri], box[rj]
            merged = (min(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3]))
            if _km(merged[0], merged[2], merged[1], merged[3]) > STREET_MAX_EXTENT_KM:
                continue
            parent[rj] = ri
            box[ri] = merged

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    keep = {}
    for members in clusters.values():
        if len(members) == 1:
            continue
        pts = sorted((records[i]["lat"], records[i]["lon"]) for i in members)
        clat = math.fsum(p[0] for p in pts) / len(pts)
        clon = math.fsum(p[1] for p in pts) / len(pts)
        best = min(members, key=lambda i: (
            (records[i]["lat"] - clat) ** 2 + (records[i]["lon"] - clon) ** 2,
            records[i]["lat"], records[i]["lon"], records[i].get("subtype") or ""))
        keep[best] = [[lat, lon] for lat, lon in pts]
    out = []
    for i in range(n):
        root_members = clusters[find(i)]
        if len(root_members) == 1:
            out.append(records[i])
        elif i in keep:
            rec = dict(records[i])
            rec["_pts"] = keep[i]
            out.append(rec)
    return out


def merge_streets_in_file(path):
    """Merge street pieces in a search-features JSONL, in place.

    Same-named street records must be adjacent (extraction and search caches
    are sorted by type, then name). Runs that are not adjacent are simply not
    merged. Everything else is copied through byte for byte.
    STREETZIM_MERGE_STREETS=0 skips it. Returns (streets_in, streets_out).
    """
    if os.environ.get("STREETZIM_MERGE_STREETS", "1") == "0":
        return (0, 0)
    tmp = path + ".merging"
    n_in = n_out = 0
    with open(path, encoding="utf-8") as fin, \
            open(tmp, "w", encoding="utf-8") as fout:
        group, group_name = [], None

        def flush():
            nonlocal n_in, n_out
            if not group:
                return
            recs = [json.loads(line) for line in group]
            kept = merge_street_records(recs)
            n_in += len(recs)
            n_out += len(kept)
            if len(kept) == len(recs):
                fout.writelines(group)          # untouched: original bytes
            else:
                by_id = {id(r): line for r, line in zip(recs, group)}
                for r in kept:
                    line = by_id.get(id(r))
                    fout.write(line if line is not None else
                               json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")
            group.clear()

        for line in fin:
            if '"type":"street"' in line or '"type": "street"' in line:
                rec_name = json.loads(line).get("name")
                if rec_name != group_name:
                    flush()
                    group_name = rec_name
                group.append(line if line.endswith("\n") else line + "\n")
            else:
                flush()
                group_name = None
                fout.write(line)
        flush()
    os.replace(tmp, path)
    if n_in != n_out:
        print(f"    Merged per-tile street pieces: {n_in:,} -> {n_out:,} street records",
              flush=True)
    return (n_in, n_out)
