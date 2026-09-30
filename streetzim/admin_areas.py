"""Administrative areas (countries, states, counties, cities, wards...) for
search, from the OSM boundary relations in the build's own PBF.

One search record per area (`t: "admin"`, docs/search-records.md): its
name and other names, a representative point, its bounding box, its
admin_level and a type label ("county", "city", "state", ...) read with
the OSM admin_level conventions of its country.

    extract_admin_areas(pbf, bbox)      -> list of search features
    append_admin_areas(pbf, jsonl, bbox) -> count appended to the JSONL

Which areas: `boundary=administrative` relations (and named closed ways)
with an `admin_level` of 2-10 and a name, and named `boundary=place`
relations of a city, town, village, borough or suburb (the City of
Washington), whose representative point lies
inside the build box. That is the rule every other search record follows,
and the one maps2zim applies to GeoNames' ADM points; an area that only
touches the box (the United States in a D.C. build) is left out.

The representative point is the relation's `label` node, else its
`admin_centre` node, when either lies inside the polygon; else the
centroid of its largest outer ring when that is inside; else the middle
of the widest span of the polygon along the centroid's latitude.

Areas the extract cuts: a Geofabrik extract keeps the relations of its
neighbours but only their members inside its own polygon, so Arlington
County has no polygon in the D.C. extract. Such an area (admin_level 5 or
more; a clipped country or state is never "in" the map) is kept only when
a point for it can be found without its geometry: its label node, or its
admin_centre node inside the members the extract has, else (see
place_clipped for the checks against twin border towns) a GeoNames populated place
of the same name (or, for "X County", the place X in the GeoNames second-
level division "X County") within a few km of the members the extract
does have. The GeoNames table is the one `reverse_geocoder` ships (cities
with over 1,000 people, CC BY 4.0), already used for location labels, so
this downloads nothing. Such areas have no bounding box.
"""
from __future__ import annotations

import csv
import json
import math
import os
import shutil
import subprocess
import tempfile
import unicodedata
from array import array
from collections.abc import Iterable, Sequence
from typing import Any

from streetzim import area as _area

Point = tuple[float, float]           # (lon, lat)
Ring = list[Point]
BBox = tuple[float, float, float, float]

MIN_LEVEL, MAX_LEVEL = 2, 10
# A clipped area at or below this level (country, state) is never kept.
MAX_CLIPPED_SKIP_LEVEL = 4
# How far a GeoNames place may lie from the members the extract has of the
# area it stands for, by admin_level (counties are large, towns are not).
GEONAMES_MAX_KM = {5: 40.0, 6: 40.0, 7: 25.0}
GEONAMES_DEFAULT_MAX_KM = 12.0
MAX_ALT_NAMES = 6
# The region (`location`) of an area is the deepest enclosing area up to
# this level.
MAX_PARENT_LEVEL = 6
# Every polygon is kept simplified (Douglas-Peucker, this tolerance in
# metres), as flat float arrays, for the region lookup and to check GeoNames
# placements. Thinning by vertex count cut across river borders: at 256
# vertices a ring, Echternacherbrueck and Oberbillig (Germany, on the Sauer
# and the Moselle) fell inside Luxembourg's polygon.
SIMPLIFY_M = 50.0
# Margin around the build box when cutting the admin relations out of a
# larger extract (osmium extract -s smart completes them): the box's own
# size, clamped to these degrees, so an area around the box with a border
# within that distance is kept.
EXTRACT_MARGIN_DEG = (0.5, 5.0)
# ... when the filtered boundaries are at least this big (a country's are
# a few MB; the planet's are GBs).
EXTRACT_MIN_BYTES = 16 * 1024 * 1024
COORD_DP = 5
# boundary=place relations kept, and the admin_level they rank as: OSM
# maps the City of Washington as one, coterminous with the District.
PLACE_BOUNDARY_LEVELS = {"city": 8, "town": 8, "borough": 9, "village": 9,
                         "suburb": 10}

# The type label, by country and admin_level (English; after the OSM wiki's
# admin_level table). A `border_type` on the relation wins; a `place` tag is
# used only where the table has no entry.
GENERIC_LABELS = {2: "country", 3: "region", 4: "region", 5: "region",
                  6: "district", 7: "district", 8: "municipality",
                  9: "district", 10: "neighbourhood"}
COUNTRY_LABELS: dict[str, dict[int, str]] = {
    "US": {4: "state", 5: "region", 6: "county", 7: "township", 8: "city",
           9: "ward", 10: "neighborhood"},
    "CA": {4: "province", 5: "region", 6: "county", 8: "municipality",
           9: "ward", 10: "neighbourhood"},
    "GB": {4: "country", 5: "region", 6: "county", 8: "district", 9: "ward",
           10: "parish"},
    "IE": {5: "province", 6: "county", 7: "municipal district", 10: "townland"},
    "FR": {3: "region", 4: "region", 6: "department", 7: "arrondissement",
           8: "commune", 9: "municipal arrondissement", 10: "quarter"},
    "DE": {4: "state", 5: "government region", 6: "district",
           7: "municipal association", 8: "municipality", 9: "borough",
           10: "locality"},
    "AT": {4: "state", 6: "district", 8: "municipality", 9: "district",
           10: "locality"},
    "CH": {4: "canton", 6: "district", 8: "municipality", 10: "locality"},
    "IT": {4: "region", 6: "province", 8: "municipality", 10: "locality"},
    "ES": {4: "autonomous community", 6: "province", 7: "comarca",
           8: "municipality", 9: "district", 10: "neighbourhood"},
    "PT": {4: "autonomous region", 6: "district", 7: "municipality",
           8: "parish"},
    "NL": {3: "country", 4: "province", 8: "municipality", 10: "town"},
    "BE": {4: "region", 6: "province", 7: "arrondissement", 8: "municipality",
           9: "section"},
    "LU": {6: "canton", 8: "commune", 9: "locality"},
    "MC": {8: "municipality", 10: "quarter"},
    "JP": {4: "prefecture", 7: "city", 8: "municipality", 9: "district",
           10: "neighbourhood"},
}
# `border_type` / `place` values used as the label, spelling normalized.
TAG_LABELS = {
    "nation": "country", "country": "country", "state": "state",
    "province": "province", "region": "region", "county": "county",
    "district": "district", "city": "city", "town": "town",
    "village": "village", "municipality": "municipality",
    "borough": "borough", "ward": "ward", "departement": "department",
    "department": "department", "arrondissement": "arrondissement",
    "canton": "canton", "commune": "commune", "parish": "parish",
    "township": "township", "prefecture": "prefecture",
    "civil_parish": "parish", "city_county": "city", "suburb": "suburb",
}


# ---------------------------------------------------------------- tags

def admin_level(value: str | None) -> int | None:
    """The admin_level as an int in 2-10, else None."""
    try:
        lvl = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return lvl if MIN_LEVEL <= lvl <= MAX_LEVEL else None


def country_of(tags: dict[str, str]) -> str | None:
    """ISO 3166-1 alpha-2 from the area's own tags, when it has one."""
    for k in ("ISO3166-1:alpha2", "ISO3166-1", "country_code"):
        v = (tags.get(k) or "").strip().upper()
        if len(v) == 2 and v.isalpha():
            return v
    v = (tags.get("ISO3166-2") or "").strip().upper()
    if len(v) > 3 and v[2] == "-" and v[:2].isalpha():
        return v[:2]
    return None


def type_label(tags: dict[str, str], level: int, cc: str | None) -> str:
    """"county", "city", "state", ...: the relation's `border_type`, else
    the country's admin_level convention, else its `place` tag, else a
    generic label. The convention comes before `place`, which says what
    the settlement is rather than the unit: Luxembourg's cantons carry
    place=county, Washington's wards place=borough."""
    if tags.get("boundary") == "place":
        return TAG_LABELS.get((tags.get("place") or "").strip().lower(), "place")
    bt = (tags.get("border_type") or "").strip().lower()
    if bt in TAG_LABELS:
        return TAG_LABELS[bt]
    if level == 2:
        return "country"
    by_cc = COUNTRY_LABELS.get((cc or "").upper(), {})
    if level in by_cc:
        return by_cc[level]
    pl = (tags.get("place") or "").strip().lower()
    if pl in TAG_LABELS:
        return TAG_LABELS[pl]
    return GENERIC_LABELS.get(level, "area")


def _is_latin(s: str) -> bool:
    for ch in s:
        if ch.isalpha() and not unicodedata.name(ch, "").startswith("LATIN"):
            return False
    return True


def names(tags: dict[str, str]) -> tuple[str, list[str]]:
    """(display name, other names). The display name is `name` when it is
    in Latin script, else `name:en`, else `name` (the search records' rule
    is "Latin where there is one"). Other names: the rest of name,
    name:en, official_name(:en), alt_name (;-separated), short_name and
    loc_name, deduplicated, at most MAX_ALT_NAMES."""
    name = (tags.get("name") or "").strip()
    en = (tags.get("name:en") or "").strip()
    display = name if (name and _is_latin(name)) or not en else en
    cands: list[str] = [name, en]
    for k in ("official_name:en", "official_name", "alt_name:en", "alt_name",
              "short_name:en", "short_name", "loc_name"):
        cands += [p.strip() for p in (tags.get(k) or "").split(";")]
    seen = {display.casefold()}
    alts: list[str] = []
    for c in cands:
        if c and len(c) <= 100 and c.casefold() not in seen:
            seen.add(c.casefold())
            alts.append(c)
    return display, alts[:MAX_ALT_NAMES]


# ------------------------------------------------------------ geometry

def ring_area_centroid(ring: Sequence[Point]) -> tuple[float, float, float]:
    """Signed shoelace area (in degrees squared) and centroid of a ring."""
    a = cx = cy = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        f = x0 * y1 - x1 * y0
        a += f
        cx += (x0 + x1) * f
        cy += (y0 + y1) * f
    a *= 0.5
    if abs(a) < 1e-15:
        xs = [p[0] for p in ring] or [0.0]
        ys = [p[1] for p in ring] or [0.0]
        return 0.0, sum(xs) / len(xs), sum(ys) / len(ys)
    return a, cx / (6 * a), cy / (6 * a)


def point_in_rings(rings: Iterable[Sequence[Point]], x: float, y: float) -> bool:
    """Even-odd rule over every ring (outer and inner) of a polygon."""
    inside = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def _widest_span(rings: Sequence[Sequence[Point]], y: float) -> Point | None:
    xs: list[float] = []
    for ring in rings:
        n = len(ring)
        for i in range(n):
            (x0, y0), (x1, y1) = ring[i], ring[(i + 1) % n]
            if (y0 > y) != (y1 > y):
                xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
    xs.sort()
    best: Point | None = None
    width = -1.0
    for i in range(0, len(xs) - 1, 2):
        if xs[i + 1] - xs[i] > width:
            width = xs[i + 1] - xs[i]
            best = ((xs[i] + xs[i + 1]) / 2, y)
    return best


def representative_point(outers: Sequence[Sequence[Point]],
                         inners: Sequence[Sequence[Point]],
                         hints: Iterable[Point | None] = ()) -> Point:
    """A point inside the polygon: the first hint inside it, else the
    centroid of the largest outer ring when inside, else the middle of the
    widest span along that centroid's latitude (then the ring's middle
    latitude)."""
    rings = list(outers) + list(inners)
    for h in hints:
        if h is not None and point_in_rings(rings, h[0], h[1]):
            return h
    big = max(outers, key=lambda r: abs(ring_area_centroid(r)[0]))
    _, cx, cy = ring_area_centroid(big)
    if point_in_rings(rings, cx, cy):
        return cx, cy
    lats = [p[1] for p in big]
    for y in (cy, (min(lats) + max(lats)) / 2):
        # Nudge off vertex latitudes so each crossing is counted once.
        p = _widest_span(rings, y + 1e-9)
        if p is not None:
            return p
    return big[0]


def _thin(ring: Sequence[Point], tol_m: float = SIMPLIFY_M) -> array:
    """A ring simplified with Douglas-Peucker (no vertex moves the outline
    by more than tol_m metres), as a flat array x0, y0, x1, y1, ...
    (16 bytes a vertex). Distances in a local equirectangular frame."""
    n = len(ring)
    out = array("d")
    if n <= 3 or tol_m <= 0:
        for x, y in ring:
            out.append(x)
            out.append(y)
        return out
    kx = math.cos(math.radians(sum(p[1] for p in ring[:: max(1, n // 64)])
                               / len(ring[:: max(1, n // 64)]))) * 111320.0
    ky = 110540.0
    tol2 = tol_m * tol_m
    keep = bytearray(n)
    keep[0] = keep[n - 1] = 1
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = ring[i][0] * kx, ring[i][1] * ky
        dx, dy = ring[j][0] * kx - ax, ring[j][1] * ky - ay
        ll = dx * dx + dy * dy
        best, far = -1.0, -1
        for k in range(i + 1, j):
            px, py = ring[k][0] * kx - ax, ring[k][1] * ky - ay
            if ll > 0:
                t = max(0.0, min(1.0, (px * dx + py * dy) / ll))
                qx, qy = px - t * dx, py - t * dy
            else:
                qx, qy = px, py
            d = qx * qx + qy * qy
            if d > best:
                best, far = d, k
        if best > tol2:
            keep[far] = 1
            stack.append((i, far))
            stack.append((far, j))
    for k in range(n):
        if keep[k]:
            out.append(ring[k][0])
            out.append(ring[k][1])
    return out


def point_in_flat(rings: Iterable[array], x: float, y: float) -> bool:
    """point_in_rings over _thin's flat rings."""
    inside = False
    for r in rings:
        n = len(r) // 2
        j = n - 1
        for i in range(n):
            xi, yi, xj, yj = r[2 * i], r[2 * i + 1], r[2 * j], r[2 * j + 1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def bbox_of(rings: Iterable[Sequence[Point]]) -> BBox:
    """The box of the rings. One spanning over 180 degrees of longitude is
    tried the other way round the globe: an area across the antimeridian
    (Chukotka, Fiji) gets west in [-180, 180) and east past 180, the
    unwrapped form streetzim/area.py uses (and the viewer accepts)."""
    xs: list[float] = []
    ys: list[float] = []
    for r in rings:
        xs += [p[0] for p in r]
        ys += [p[1] for p in r]
    w, e = min(xs), max(xs)
    if e - w > 180:
        shifted = [x + 360 if x < 0 else x for x in xs]
        w2, e2 = min(shifted), max(shifted)
        if e2 - w2 < e - w:
            w, e = w2, e2
    return w, min(ys), e, max(ys)


class Grid:
    """Areas by the grid cells their box covers, to find the polygons
    around a point without scanning them all. Each area goes in a grid
    whose cell (a power of two degrees, 1/64 to 64) is at least a quarter
    of its box, so it covers a handful of cells whatever its size, and a
    cell holds few small areas (a 1-degree grid put ~2,500 municipalities
    in each cell of a dense country)."""

    def __init__(self, areas: Iterable[dict[str, Any]]):
        self.cells: dict[tuple[float, int, int], list[dict[str, Any]]] = {}
        self.sizes: set[float] = set()
        for a in areas:
            w, s, e, n = a["bbox"]
            deg = 1 / 64
            while deg < 64 and deg * 4 < max(e - w, n - s):
                deg *= 2
            self.sizes.add(deg)
            for cx in range(math.floor(w / deg), math.floor(e / deg) + 1):
                for cy in range(math.floor(s / deg), math.floor(n / deg) + 1):
                    self.cells.setdefault((deg, cx, cy), []).append(a)

    def holding(self, x: float, y: float) -> list[dict[str, Any]]:
        """The areas whose polygon holds (x, y)."""
        out: list[dict[str, Any]] = []
        for deg in self.sizes:
            for px in (x, x + 360.0):     # a box past 180 holds x + 360
                for a in self.cells.get((deg, math.floor(px / deg), math.floor(y / deg)), ()):
                    w, s, e, n = a["bbox"]
                    if w <= px <= e and s <= y <= n and point_in_flat(a["rings"], x, y):
                        out.append(a)
        return out


def fit_zoom(bb: Sequence[float], width: int = 1024, height: int = 768) -> float:
    """The web-mercator zoom (512 px tiles, as MapLibre counts) at which
    the box fills a width x height viewport, with a little margin;
    clamped to 2-16."""
    w, s, e, n = bb
    def merc_y(lat: float) -> float:
        lat = max(-85.0, min(85.0, lat))
        r = math.radians(lat)
        return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2
    fx = max((e - w) / 360.0, 1e-9)
    fy = max(abs(merc_y(s) - merc_y(n)), 1e-9)
    z = min(math.log2(width / (512 * fx)), math.log2(height / (512 * fy))) - 0.25
    return round(max(2.0, min(16.0, z)), 1)


def _km(a: Point, b: Point) -> float:
    (lo1, la1), (lo2, la2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    h = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _km_to_box(p: Point, bb: Sequence[float]) -> float:
    q = (min(max(p[0], bb[0]), bb[2]), min(max(p[1], bb[1]), bb[3]))
    return _km(p, q)


# ------------------------------------------------------------ GeoNames

def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold().strip()


# GeoNames' admin1 names for the US state codes of ISO3166-2 / is_in tags.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "Washington, D.C.", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}


def expected_region(tags: dict[str, str]) -> tuple[str | None, str | None]:
    """(country code, GeoNames admin1 name) an area's own tags state:
    ISO3166-2 ("US-VA"), is_in:state / is_in:state_code, is_in:country_code."""
    cc = country_of(tags) or ((tags.get("is_in:country_code") or "").strip().upper() or None)
    state = (tags.get("is_in:state") or "").strip() or None
    code = (tags.get("is_in:state_code") or "").strip().upper()
    iso2 = (tags.get("ISO3166-2") or "").strip().upper()
    if not code and iso2.startswith("US-"):
        code = iso2[3:]
    if not state and code and (cc in (None, "US")) and code in US_STATES:
        state, cc = US_STATES[code], "US"
    return cc, state


class GeoNamesPlaces:
    """reverse_geocoder's rg_cities1000.csv (GeoNames, CC BY 4.0), indexed
    by folded place name and by folded second-level division name."""

    def __init__(self, rows: Iterable[dict[str, str]]):
        self.by_name: dict[str, list[dict[str, Any]]] = {}
        self.by_admin2: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            try:
                rec: dict[str, Any] = {"name": r["name"], "admin1": r.get("admin1", ""),
                                       "admin2": r.get("admin2", ""), "cc": r.get("cc", ""),
                                       "pt": (float(r["lon"]), float(r["lat"]))}
            except (KeyError, ValueError):
                continue
            self.by_name.setdefault(_fold(rec["name"]), []).append(rec)
            if rec["admin2"]:
                self.by_admin2.setdefault(_fold(rec["admin2"]), []).append(rec)

    @classmethod
    def load(cls) -> GeoNamesPlaces | None:
        try:
            import reverse_geocoder
        except ImportError:
            return None
        path = os.path.join(os.path.dirname(reverse_geocoder.__file__), "rg_cities1000.csv")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8", newline="") as f:
            return cls(csv.DictReader(f))

    def candidates(self, name: str, near: Sequence[float], max_km: float) -> list[dict[str, Any]]:
        """The places that could stand for area `name`, nearest first,
        within max_km of box `near` (the members the extract has): a place
        of that name, or place X of division "X <word>" ("Arlington" in
        "Arlington County")."""
        key = _fold(name)
        cands = list(self.by_name.get(key, []))
        for r in self.by_admin2.get(key, []):
            core = _fold(r["name"])
            if (key.startswith(core + " ") or key.endswith(" " + core)) and r not in cands:
                cands.append(r)
        scored = [(_km_to_box(r["pt"], near), r) for r in cands]
        return [r for d, r in sorted(scored, key=lambda t: t[0]) if d <= max_km]

    def locate(self, name: str, near: Sequence[float], max_km: float) -> dict[str, Any] | None:
        """The nearest of candidates(), or None."""
        c = self.candidates(name, near, max_km)
        return c[0] if c else None


def place_clipped(gn: GeoNamesPlaces, tags: dict[str, str], level: int,
                  near: Sequence[float], grid: Grid | None,
                  stats: dict[str, int] | None = None) -> dict[str, Any] | None:
    """The GeoNames place standing for a clipped area, or None. A miss is
    better than a wrong pin, so (twin border towns: Bristol TN/VA,
    Texarkana, Delmar) a candidate is dropped when:
    - the area's tags name a country or state (ISO3166-2, is_in:*) it is not in;
    - it lies inside a polygon the extract has, of the same or a lower
      admin_level (that polygon is another area: Bristol TN for Bristol VA);
    and the area is left out when the candidates left are in more than one
    GeoNames region."""
    max_km = GEONAMES_MAX_KM.get(level, GEONAMES_DEFAULT_MAX_KM)
    want_cc, want_state = expected_region(tags)
    for nm in dict.fromkeys(filter(None, (tags.get("name"), tags.get("name:en")))):
        cands = gn.candidates(nm, near, max_km)
        if want_cc:
            cands = [c for c in cands if (c["cc"] or "").upper() == want_cc]
        if want_state:
            cands = [c for c in cands if _fold(c["admin1"]) == _fold(want_state)]
        if grid is not None:
            cands = [c for c in cands
                     if not any(a["level"] <= level for a in grid.holding(*c["pt"]))]
        if not cands:
            continue
        if len({(c["cc"], c["admin1"]) for c in cands}) > 1:
            if stats is not None:
                stats["geonames ambiguous"] = stats.get("geonames ambiguous", 0) + 1
            return None
        return cands[0]
    return None


def region_of_name(gn: GeoNamesPlaces, tags: dict[str, str], level: int,
                   near: Sequence[float]) -> dict[str, Any] | None:
    """A GeoNames place of the area's name near its members, for its region
    and country only (not its position), when all such places (those the
    area's tags allow) are in one region; else None."""
    max_km = GEONAMES_MAX_KM.get(level, GEONAMES_DEFAULT_MAX_KM)
    want_cc, want_state = expected_region(tags)
    for nm in dict.fromkeys(filter(None, (tags.get("name"), tags.get("name:en")))):
        cands = [c for c in gn.candidates(nm, near, max_km)
                 if (not want_cc or (c["cc"] or "").upper() == want_cc)
                 and (not want_state or _fold(c["admin1"]) == _fold(want_state))]
        if cands:
            return cands[0] if len({(c["cc"], c["admin1"]) for c in cands}) == 1 else None
    return None


# ------------------------------------------------------------- reading

def _osmium() -> Any:
    import osmium  # pyosmium, a builder dependency
    return osmium


def _relation_level(tags: dict[str, str]) -> int | None:
    """The admin_level an admin or place boundary ranks as, else None."""
    b = tags.get("boundary")
    if b == "administrative":
        return admin_level(tags.get("admin_level"))
    if b == "place":
        return PLACE_BOUNDARY_LEVELS.get((tags.get("place") or "").strip().lower())
    return None


def _collect_relations(path: str) -> dict[int, dict[str, Any]]:
    """Pass 1: the admin (and place) relations, their tags and members."""
    osmium = _osmium()
    rels: dict[int, dict[str, Any]] = {}

    class H(osmium.SimpleHandler):
        def relation(self, r: Any) -> None:
            tags = {t.k: t.v for t in r.tags}
            lvl = _relation_level(tags)
            if lvl is None or not (tags.get("name") or tags.get("name:en")):
                return
            label = centre = None
            ways: list[int] = []
            for m in r.members:
                if m.type == "n" and m.role == "label" and label is None:
                    label = m.ref
                elif m.type == "n" and m.role == "admin_centre" and centre is None:
                    centre = m.ref
                elif m.type == "w":
                    ways.append(m.ref)
            rels[r.id] = {"tags": tags, "level": lvl, "label": label,
                          "centre": centre, "ways": ways}

    H().apply_file(path)
    return rels


def _collect_geometry(path: str, rels: dict[int, dict[str, Any]]) -> tuple[
        dict[str, dict[str, Any]], dict[int, Point], dict[int, list[float]]]:
    """Pass 2: assembled areas (summarized as they come; each keeps only
    a copy of its rings simplified to SIMPLIFY_M), the label/admin_centre node locations,
    and for each relation the box of the member ways the file has."""
    osmium = _osmium()
    want_nodes = {v for r in rels.values() for v in (r["label"], r["centre"]) if v}
    way_owner: dict[int, list[int]] = {}
    for rid, r in rels.items():
        for w in r["ways"]:
            way_owner.setdefault(w, []).append(rid)
    nodes: dict[int, Point] = {}
    present: dict[int, list[float]] = {}
    areas: dict[str, dict[str, Any]] = {}

    def summarize(key: str, tags: dict[str, str], lvl: int, a: Any,
                  hints: list[Point | None]) -> None:
        outers: list[Ring] = []
        inners: list[Ring] = []
        for outer in a.outer_rings():
            outers.append([(n.lon, n.lat) for n in outer])
            for inner in a.inner_rings(outer):
                inners.append([(n.lon, n.lat) for n in inner])
        outers = [r for r in outers if len(r) >= 3]
        if not outers:
            return
        areas[key] = {"tags": tags, "level": lvl, "bbox": bbox_of(outers),
                      "pt": representative_point(outers, inners, hints),
                      "rings": [_thin(r) for r in outers + inners]}

    class H(osmium.SimpleHandler):
        def node(self, n: Any) -> None:
            if n.id in want_nodes and n.location.valid():
                nodes[n.id] = (n.location.lon, n.location.lat)

        def way(self, w: Any) -> None:
            owners = way_owner.get(w.id)
            if not owners:
                return
            # The box of each member's ends: enough to say where the part
            # the extract has lies, without reading every node in Python.
            nds = w.nodes
            if not len(nds):
                return
            pts = [(nd.lon, nd.lat) for nd in (nds[0], nds[len(nds) // 2], nds[len(nds) - 1])
                   if nd.location.valid()]
            if not pts:
                return
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            for rid in owners:
                cur = present.get(rid)
                present[rid] = [min(xs), min(ys), max(xs), max(ys)] if cur is None else [
                    min(cur[0], *xs), min(cur[1], *ys), max(cur[2], *xs), max(cur[3], *ys)]

        def area(self, a: Any) -> None:
            if a.from_way():
                tags = {t.k: t.v for t in a.tags}
                lvl = admin_level(tags.get("admin_level"))
                if (tags.get("boundary") != "administrative" or lvl is None
                        or not (tags.get("name") or tags.get("name:en"))):
                    return
                summarize(f"w{a.orig_id()}", tags, lvl, a, [])
                return
            r = rels.get(a.orig_id())
            if r is None:
                return
            summarize(f"r{a.orig_id()}", r["tags"], r["level"], a,
                      [nodes.get(r["label"]) if r["label"] else None,
                       nodes.get(r["centre"]) if r["centre"] else None])

    H().apply_file(path, locations=True, idx="flex_mem")
    return areas, nodes, present


def _filtered_input(pbf_path: str, tmp: str, bbox: Sequence[float] | None = None) -> str:
    """The admin and place boundaries with their members, cut out with
    the osmium CLI; then, for a build box, when that is big (a planet or
    continent input), only those around the box: `osmium extract -s smart`
    keeps each relation with a member in the box plus a margin (the box's
    size, clamped to EXTRACT_MARGIN_DEG) and
    completes it, so what Python reads stays small. Not for a country:
    extract's ID sets take ~3.8 GB whatever the input, and Belgium's
    boundaries are 6 MB, read in ~13 s."""
    out = os.path.join(tmp, "admin.osm.pbf")
    subprocess.run(["osmium", "tags-filter", pbf_path,
                    "wr/boundary=administrative", "r/boundary=place",
                    "-o", out, "--overwrite", "--no-progress"], check=True)
    if bbox is None or os.path.getsize(out) < EXTRACT_MIN_BYTES:
        return out
    w, s, e, n = _area.normalize(bbox)
    m = min(max(e - w, n - s, EXTRACT_MARGIN_DEG[0]), EXTRACT_MARGIN_DEG[1])
    box = _area.normalize([w - m, max(-90.0, s - m), min(e + m, w - m + 360.0),
                           min(90.0, n + m)])
    cut = os.path.join(tmp, "admin-box.osm.pbf")
    subprocess.run(["osmium", "extract", *_area.osmium_extract_args(box, tmp),
                    "-s", "smart", "-S", "types=boundary,multipolygon",
                    out, "-o", cut, "--overwrite", "--no-progress"], check=True)
    return cut


def _rg_lookup(points: list[Point]) -> list[dict[str, str]]:
    """reverse_geocoder's nearest populated place for each (lon, lat)."""
    if not points:
        return []
    try:
        import reverse_geocoder as rg
        res = rg.search([(p[1], p[0]) for p in points], mode=1, verbose=False)
        return [dict(r) for r in res]
    except Exception:
        return [{} for _ in points]


def extract_admin_areas(pbf_path: str, bbox: Sequence[float] | None = None, *,
                        geonames: GeoNamesPlaces | None | bool = True,
                        stats: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Admin areas of `pbf_path` whose representative point is in `bbox`
    (any when None), as search features (docs/search-records.md, `admin`).
    `geonames`: True loads reverse_geocoder's table when a clipped area
    needs it, False never uses it, or pass a GeoNamesPlaces. Needs the
    osmium CLI (as the address extraction does)."""
    tmp = tempfile.mkdtemp(prefix="streetzim_admin_")
    try:
        src = _filtered_input(pbf_path, tmp, bbox)
        rels = _collect_relations(src)
        areas, nodes, present = _collect_geometry(src, rels)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    grid = Grid(areas.values())
    if stats is None:
        stats = {}

    found: list[dict[str, Any]] = []
    for key, a in areas.items():
        found.append({"key": key, "tags": a["tags"], "level": a["level"],
                      "pt": a["pt"], "bbox": a["bbox"], "how": "polygon"})
    gn: GeoNamesPlaces | None = geonames if isinstance(geonames, GeoNamesPlaces) else None
    gn_tried = geonames is not True
    for rid, r in rels.items():
        if f"r{rid}" in areas or r["level"] <= MAX_CLIPPED_SKIP_LEVEL:
            continue
        # Clipped by the extract: no polygon, so no box either. Its label
        # node stands for it; its admin_centre only inside the members the
        # extract has (a capital can lie far from the part in the box).
        box = present.get(rid)
        pt = nodes.get(r["label"]) if r["label"] else None
        how = "label"
        centre = nodes.get(r["centre"]) if r["centre"] else None
        if pt is None and centre is not None and box is not None and \
                box[0] <= centre[0] <= box[2] and box[1] <= centre[1] <= box[3]:
            pt, how = centre, "admin_centre"
        if pt is not None and any(a["level"] == r["level"] for a in grid.holding(*pt)):
            # The node lies in another area of the same level that the
            # extract has whole (a label mapped across a border river):
            # not where this area is. (One of a lower level, its country,
            # may well hold it.)
            stats["node in another area"] = stats.get("node in another area", 0) + 1
            pt = None
        hit = None
        if box is not None:
            if not gn_tried:
                gn, gn_tried = GeoNamesPlaces.load(), True
            if gn is not None:
                hit = place_clipped(gn, r["tags"], r["level"], box, grid, stats)
        if pt is None and hit:
            pt, how = hit["pt"], "geonames"
        if pt is None:
            continue
        # The GeoNames place also names its region and country. Without one
        # (a label node stood for the area), the places of its name near its
        # members still do when they agree on one region: the nearest place
        # to a label on a border river is often across it (Oberbillig's is
        # Wasserbillig, Luxembourg).
        region = hit
        if region is None and gn is not None and box is not None:
            region = region_of_name(gn, r["tags"], r["level"], box)
        found.append({"key": f"r{rid}", "tags": r["tags"], "level": r["level"],
                      "pt": pt, "bbox": None, "how": how,
                      "admin1": region["admin1"] if region else "",
                      "cc": region["cc"] if region else "", "gn": bool(region)})

    if bbox is not None:
        found = [f for f in found if _area.contains(bbox, f["pt"][0], f["pt"][1])]

    # Region and country: the areas (level <= MAX_PARENT_LEVEL) whose
    # polygon holds the point, else the GeoNames place above, else the
    # nearest GeoNames place (reverse_geocoder).
    for f in found:
        f["parents"] = sorted(
            (a for a in grid.holding(*f["pt"]) if a["level"] < f["level"]
             and _encloses(a["bbox"], f["bbox"])),
            key=lambda a: -a["level"])
    lookup = [f for f in found if not f["parents"] and not f.get("admin1")]
    for f, g in zip(lookup, _rg_lookup([f["pt"] for f in lookup])):
        f["admin1"], f["cc"], f["gn"] = g.get("admin1", ""), g.get("cc", ""), bool(g)
    feats: list[dict[str, Any]] = []
    for f in found:
        tags = f["tags"]
        lvl = f["level"]
        name, alts = names(tags)
        parents: list[dict[str, Any]] = f["parents"]
        cc = country_of(tags)
        for pa in parents:
            cc = cc or country_of(pa["tags"])
        cc = cc or (f.get("cc") or "").upper() or None
        region = [pa for pa in parents if pa["level"] <= MAX_PARENT_LEVEL]
        gn_region = False
        if lvl <= 2:
            loc = ""
        elif region:
            loc = names(region[0]["tags"])[0]
        elif lvl > MAX_CLIPPED_SKIP_LEVEL and f.get("admin1") and _fold(f["admin1"]) != _fold(name):
            loc, gn_region = f["admin1"], bool(f.get("gn"))
        else:
            loc = _country_name(cc)
        feat: dict[str, Any] = {
            "name": name, "type": "admin", "subtype": type_label(tags, lvl, cc),
            "lat": round(f["pt"][1], COORD_DP), "lon": round(f["pt"][0], COORD_DP),
            "location": loc, "admin_level": lvl, "osm": f["key"],
        }
        if alts:
            feat["alt"] = alts
        if f["bbox"]:
            feat["bbox"] = [round(v, COORD_DP) for v in f["bbox"]]
        if tags.get("wikidata", "").startswith("Q"):
            feat["wikidata"] = tags["wikidata"]
        if tags.get("wikipedia"):
            feat["wikipedia"] = tags["wikipedia"]
        if gn_region or f["how"] == "geonames":
            # For the GeoNames credit on the Kiwix page.
            feat["geonames"] = True
        feats.append(feat)
    feats = dedupe(feats)
    stats["relations"] = len(rels)
    for f in found:
        stats[f["how"]] = stats.get(f["how"], 0) + 1
    return feats


def _encloses(outer: Sequence[float], inner: Sequence[float] | None) -> bool:
    """Whether box `outer` holds box `inner` (within 2% of `inner`'s size):
    a parent holds its child whole, where a neighbour across a river whose
    polygon happens to hold the child's point (Oberbillig's label, on the
    Moselle, in a Luxembourg canton) does not. True without an inner box."""
    if inner is None:
        return True
    tol = 0.02 * max(inner[2] - inner[0], inner[3] - inner[1])
    return (outer[0] - tol <= inner[0] and outer[1] - tol <= inner[1]
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def dedupe(feats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per (name, box): the same area mapped at two levels
    (Monaco the country and Monaco the municipality) keeps the lower
    level. Boxes match when every edge is within 1% of the larger span.
    Names are folded once and only records of one name are compared."""
    by_name: dict[str, list[Sequence[float]]] = {}
    out: list[dict[str, Any]] = []
    for f in sorted(feats, key=lambda f: (f["admin_level"], f["osm"])):
        bb = f.get("bbox")
        same = by_name.setdefault(_fold(f["name"]), [])
        dup = False
        if bb:
            for gb in same:
                tol = 0.01 * max(bb[2] - bb[0], bb[3] - bb[1], gb[2] - gb[0], gb[3] - gb[1])
                if all(abs(x - y) <= tol for x, y in zip(bb, gb)):
                    dup = True
                    break
        if dup:
            continue
        if bb:
            same.append(bb)
        out.append(f)
    return out


_COUNTRY_NAMES = {
    "US": "United States", "CA": "Canada", "GB": "United Kingdom",
    "IE": "Ireland", "FR": "France", "DE": "Germany", "AT": "Austria",
    "CH": "Switzerland", "IT": "Italy", "ES": "Spain", "PT": "Portugal",
    "NL": "Netherlands", "BE": "Belgium", "LU": "Luxembourg", "MC": "Monaco",
    "JP": "Japan", "LI": "Liechtenstein", "AD": "Andorra", "SM": "San Marino",
    "VA": "Vatican City", "MX": "Mexico", "AU": "Australia", "NZ": "New Zealand",
}


def _country_name(cc: str | None) -> str:
    return _COUNTRY_NAMES.get((cc or "").upper(), (cc or "").upper())


def _admin_ids_in(search_jsonl: str) -> set[str]:
    """The `osm` ids of the admin records already in a search JSONL (a
    search cache from an earlier build may carry them)."""
    ids: set[str] = set()
    with open(search_jsonl, encoding="utf-8") as f:
        for line in f:
            if '"admin"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "admin" and rec.get("osm"):
                ids.add(rec["osm"])
    return ids


def append_admin_areas(pbf_path: str, search_jsonl: str,
                       bbox: Sequence[float] | None = None) -> int:
    """Append the admin areas to a search-feature JSONL, skipping any
    already in it; returns the count added. Skipped, with a message, when
    the osmium CLI is not installed."""
    from streetzim.common import print  # the builder's flushing print
    print("  Extracting administrative areas from OSM data...")
    if not shutil.which("osmium"):
        print("    Skipping: osmium CLI not found on PATH")
        return 0
    stats: dict[str, int] = {}
    feats = extract_admin_areas(pbf_path, bbox, stats=stats)
    have = (_admin_ids_in(search_jsonl)
            if os.path.exists(search_jsonl) and os.path.getsize(search_jsonl) else set())
    added = 0
    with open(search_jsonl, "a", encoding="utf-8") as f:
        for feat in feats:
            if feat["osm"] in have:
                continue
            f.write(json.dumps(feat, ensure_ascii=False) + "\n")
            added += 1
    how = ", ".join(f"{k} {v}" for k, v in sorted(stats.items()) if k != "relations")
    print(f"    {added} administrative areas in the box"
          + (f" ({len(feats) - added} already in the search cache)" if added < len(feats) else "")
          + f" ({stats.get('relations', 0)} admin relations read; points from: {how or 'none'})")
    stats["added"] = added
    return added
