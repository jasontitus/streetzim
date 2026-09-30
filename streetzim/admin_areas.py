"""Administrative areas (countries, states, counties, cities, wards...) for
search, from the OSM boundary relations in the build's own PBF.

One search record per area (`t: "admin"`, docs/search-records.md): its
name and other names, a representative point, its bounding box, its
admin_level and a type label ("county", "city", "state", ...) read with
the OSM admin_level conventions of its country.

    extract_admin_areas(pbf, bbox)      -> list of search features
    append_admin_areas(pbf, jsonl, bbox) -> count appended to the JSONL

Which areas: `boundary=administrative` relations (and named closed ways)
with an `admin_level` of 2-10 and a name, whose representative point lies
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
a point for it can be found without its geometry: its label or
admin_centre node when the extract has it, else a GeoNames populated place
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
# Areas up to this level keep a thinned polygon, to find the region
# (`location`) and the country of the areas inside them.
MAX_PARENT_LEVEL = 6
COORD_DP = 5

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
    "civil_parish": "parish", "city_county": "city",
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


def _thin(ring: Sequence[Point], most: int = 1000) -> Ring:
    step = max(1, math.ceil(len(ring) / most))
    return list(ring[::step])


def bbox_of(rings: Iterable[Sequence[Point]]) -> BBox:
    xs: list[float] = []
    ys: list[float] = []
    for r in rings:
        xs += [p[0] for p in r]
        ys += [p[1] for p in r]
    return min(xs), min(ys), max(xs), max(ys)


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

    def locate(self, name: str, near: Sequence[float], max_km: float) -> dict[str, Any] | None:
        """The place standing for area `name`, nearest to box `near`
        (the members the extract has) and within max_km of it: a place of
        that name, or place X of division "X <word>" ("Arlington" in
        "Arlington County")."""
        key = _fold(name)
        cands = list(self.by_name.get(key, []))
        for r in self.by_admin2.get(key, []):
            core = _fold(r["name"])
            if key.startswith(core + " ") or key.endswith(" " + core):
                cands.append(r)
        best: dict[str, Any] | None = None
        best_d = max_km
        for r in cands:
            d = _km_to_box(r["pt"], near)
            if d <= best_d:
                best, best_d = r, d
        return best


# ------------------------------------------------------------- reading

def _osmium() -> Any:
    import osmium  # pyosmium, a builder dependency
    return osmium


def _collect_relations(path: str) -> dict[int, dict[str, Any]]:
    """Pass 1: the admin relations, their tags and interesting members."""
    osmium = _osmium()
    rels: dict[int, dict[str, Any]] = {}

    class H(osmium.SimpleHandler):
        def relation(self, r: Any) -> None:
            tags = {t.k: t.v for t in r.tags}
            if tags.get("boundary") != "administrative":
                return
            lvl = admin_level(tags.get("admin_level"))
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
    """Pass 2: assembled areas (summarized as they come, so no polygon is
    kept), the label/admin_centre node locations, and for each relation the
    box of the member ways the file has."""
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
        bb = bbox_of(outers)
        areas[key] = {"tags": tags, "level": lvl, "bbox": bb,
                      "pt": representative_point(outers, inners, hints)}
        if lvl <= MAX_PARENT_LEVEL:
            # Kept, thinned, to name the region an area lies in.
            areas[key]["rings"] = [_thin(r) for r in outers + inners]

    class H(osmium.SimpleHandler):
        def node(self, n: Any) -> None:
            if n.id in want_nodes and n.location.valid():
                nodes[n.id] = (n.location.lon, n.location.lat)

        def way(self, w: Any) -> None:
            owners = way_owner.get(w.id)
            if not owners:
                return
            pts = [(nd.lon, nd.lat) for nd in w.nodes if nd.location.valid()]
            if not pts:
                return
            bb = bbox_of([pts])
            for rid in owners:
                cur = present.get(rid)
                present[rid] = list(bb) if cur is None else [
                    min(cur[0], bb[0]), min(cur[1], bb[1]),
                    max(cur[2], bb[2]), max(cur[3], bb[3])]

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


def _filtered_input(pbf_path: str, tmp: str) -> str:
    """The admin relations with their members, cut out with the osmium
    CLI when it is installed (fast, C++); else the file itself."""
    if not shutil.which("osmium"):
        return pbf_path
    out = os.path.join(tmp, "admin.osm.pbf")
    subprocess.run(["osmium", "tags-filter", pbf_path,
                    "wr/boundary=administrative", "-o", out, "--overwrite",
                    "--no-progress"], check=True)
    return out


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
    needs it, False never uses it, or pass a GeoNamesPlaces."""
    tmp = tempfile.mkdtemp(prefix="streetzim_admin_")
    try:
        src = _filtered_input(pbf_path, tmp)
        rels = _collect_relations(src)
        areas, nodes, present = _collect_geometry(src, rels)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    found: list[dict[str, Any]] = []
    for key, a in areas.items():
        found.append({"key": key, "tags": a["tags"], "level": a["level"],
                      "pt": a["pt"], "bbox": a["bbox"], "how": "polygon"})
    gn: GeoNamesPlaces | None = geonames if isinstance(geonames, GeoNamesPlaces) else None
    gn_tried = geonames is not True
    for rid, r in rels.items():
        if f"r{rid}" in areas or r["level"] <= MAX_CLIPPED_SKIP_LEVEL:
            continue
        # Clipped by the extract: no polygon, so no box either.
        pt = nodes.get(r["label"]) if r["label"] else None
        how = "label"
        if pt is None and r["centre"]:
            pt, how = nodes.get(r["centre"]), "admin_centre"
        hit = None
        if rid in present:
            if not gn_tried:
                gn, gn_tried = GeoNamesPlaces.load(), True
            if gn is not None:
                max_km = GEONAMES_MAX_KM.get(r["level"], GEONAMES_DEFAULT_MAX_KM)
                for nm in dict.fromkeys(filter(None, (r["tags"].get("name"),
                                                      r["tags"].get("name:en")))):
                    hit = gn.locate(nm, present[rid], max_km)
                    if hit:
                        break
        if pt is None and hit:
            pt, how = hit["pt"], "geonames"
        if pt is None:
            continue
        # The GeoNames place also names its region and country.
        found.append({"key": f"r{rid}", "tags": r["tags"], "level": r["level"],
                      "pt": pt, "bbox": None, "how": how,
                      "admin1": hit["admin1"] if hit else "",
                      "cc": hit["cc"] if hit else ""})

    if bbox is not None:
        found = [f for f in found if _area.contains(bbox, f["pt"][0], f["pt"][1])]

    # Region and country: the areas (level <= MAX_PARENT_LEVEL) whose
    # polygon holds the point, else the GeoNames place above, else the
    # nearest GeoNames place (reverse_geocoder).
    holders = [a for a in areas.values() if "rings" in a]
    for f in found:
        x, y = f["pt"]
        f["parents"] = sorted(
            (a for a in holders if a["level"] < f["level"]
             and a["bbox"][0] <= x <= a["bbox"][2] and a["bbox"][1] <= y <= a["bbox"][3]
             and point_in_rings(a["rings"], x, y)),
            key=lambda a: -a["level"])
    lookup = [f for f in found if not f["parents"] and not f.get("admin1")]
    for f, g in zip(lookup, _rg_lookup([f["pt"] for f in lookup])):
        f["admin1"], f["cc"] = g.get("admin1", ""), g.get("cc", "")
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
        if lvl <= 2:
            loc = ""
        elif parents:
            loc = names(parents[0]["tags"])[0]
        elif lvl > MAX_CLIPPED_SKIP_LEVEL and f.get("admin1") and _fold(f["admin1"]) != _fold(name):
            loc = f["admin1"]
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
        feats.append(feat)
    feats = dedupe(feats)
    if stats is not None:
        stats["relations"] = len(rels)
        for f in found:
            stats[f["how"]] = stats.get(f["how"], 0) + 1
    return feats


def dedupe(feats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per (name, box): the same area mapped at two levels
    (Monaco the country and Monaco the municipality) keeps the lower
    level. Boxes match when every edge is within 1% of the larger span."""
    out: list[dict[str, Any]] = []
    for f in sorted(feats, key=lambda f: (f["admin_level"], f["osm"])):
        bb = f.get("bbox")
        dup = False
        for g in out:
            gb = g.get("bbox")
            if _fold(g["name"]) != _fold(f["name"]) or not bb or not gb:
                continue
            tol = 0.01 * max(bb[2] - bb[0], bb[3] - bb[1], gb[2] - gb[0], gb[3] - gb[1])
            if all(abs(x - y) <= tol for x, y in zip(bb, gb)):
                dup = True
                break
        if not dup:
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


def append_admin_areas(pbf_path: str, search_jsonl: str,
                       bbox: Sequence[float] | None = None) -> int:
    """Append the admin areas to a search-feature JSONL; returns the
    count. Needs pyosmium; prints what it found."""
    from streetzim.common import print  # the builder's flushing print
    print("  Extracting administrative areas from OSM data...")
    stats: dict[str, int] = {}
    feats = extract_admin_areas(pbf_path, bbox, stats=stats)
    with open(search_jsonl, "a", encoding="utf-8") as f:
        for feat in feats:
            f.write(json.dumps(feat, ensure_ascii=False) + "\n")
    how = ", ".join(f"{k} {v}" for k, v in sorted(stats.items()) if k != "relations")
    print(f"    {len(feats)} administrative areas in the box "
          f"({stats.get('relations', 0)} admin relations read; points from: {how or 'none'})")
    return len(feats)
