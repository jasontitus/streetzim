"""Administrative areas for search (streetzim/admin_areas.py): which OSM
boundary relations become `admin` search records, their point, box, names,
type label and region, on a small synthetic extract (docs/search-records.md)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import admin_areas as A  # noqa: E402

# A U-shaped county: its centroid falls in the notch, outside the polygon.
U_RING = [(1.0, 2.2), (3.0, 2.2), (3.0, 3.8), (2.6, 3.8), (2.6, 2.6),
          (1.4, 2.6), (1.4, 3.8), (1.0, 3.8)]


class Osm:
    """Builds a small .osm file: closed or open ways from coordinates."""

    def __init__(self):
        self.nodes, self.ways, self.rels = [], [], []
        self.nid = 1000

    def node(self, lon, lat, tags=None, nid=None):
        if nid is None:
            self.nid += 1
            nid = self.nid
        self.nodes.append((nid, lon, lat, tags or {}))
        return nid

    def way(self, wid, coords, closed=True, tags=None):
        ids = [self.node(x, y) for x, y in coords]
        if closed:
            ids.append(ids[0])
        self.ways.append((wid, ids, tags or {}))
        return wid

    def rel(self, rid, members, tags):
        self.rels.append((rid, members, dict({"type": "boundary",
                                               "boundary": "administrative"}, **tags)))

    def write(self, path):
        def t(tags):
            return "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
        out = ['<?xml version="1.0" encoding="UTF-8"?><osm version="0.6">']
        for nid, lon, lat, tags in sorted(self.nodes):
            out.append(f'<node id="{nid}" version="1" lat="{lat}" lon="{lon}">{t(tags)}</node>')
        for wid, ids, tags in sorted(self.ways):
            out.append(f'<way id="{wid}" version="1">'
                       + "".join(f'<nd ref="{i}"/>' for i in ids) + t(tags) + "</way>")
        for rid, members, tags in sorted(self.rels):
            out.append(f'<relation id="{rid}" version="1">'
                       + "".join(f'<member type="{ty}" ref="{ref}" role="{role}"/>'
                                 for ty, ref, role in members) + t(tags) + "</relation>")
        out.append("</osm>")
        Path(path).write_text("\n".join(out), encoding="utf-8")
        return str(path)


def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


@pytest.fixture
def extract(tmp_path):
    o = Osm()
    o.way(1, square(0, 0, 4, 4))
    o.rel(1, [("w", 1, "outer")], {"name": "Testland", "admin_level": "2",
                                   "ISO3166-1": "TL", "name:fr": "Testlande",
                                   "official_name": "Republic of Testland"})
    o.way(2, square(0, 2, 4, 4))
    o.rel(2, [("w", 2, "outer")], {"name": "North State", "admin_level": "4",
                                   "ISO3166-2": "US-NS", "wikidata": "Q2"})
    o.way(3, U_RING)
    o.rel(3, [("w", 3, "outer")], {"name": "Upper County", "admin_level": "6",
                                   "wikipedia": "en:Upper County"})
    o.way(4, square(1.05, 2.9, 1.35, 3.5))
    lab = o.node(1.2, 3.0, nid=100)
    o.rel(4, [("w", 4, "outer"), ("n", lab, "label")],
          {"name": "Ward 1", "admin_level": "9", "alt_name": "First Ward;W1"})
    # Clipped by the extract: w51 / w61 / w71 / w81 are missing.
    o.way(50, [(3.5, 0.5), (3.9, 0.5)], closed=False)
    clab = o.node(3.5, 1.0, nid=101)
    o.rel(5, [("w", 50, "outer"), ("w", 51, "outer"), ("n", clab, "label")],
          {"name": "Clip Town", "admin_level": "8"})
    o.way(60, [(5.0, 1.0), (5.0, 1.5)], closed=False)
    o.rel(6, [("w", 60, "outer"), ("w", 61, "outer")],
          {"name": "Arlington County", "admin_level": "6", "border_type": "county",
           "wikidata": "Q107126"})
    o.way(70, [(3.9, 3.9), (3.95, 3.95)], closed=False)
    centre = o.node(3.8, 3.8, nid=102)
    o.rel(7, [("w", 70, "outer"), ("w", 71, "outer"), ("n", centre, "admin_centre")],
          {"name": "Big State", "admin_level": "4"})
    o.way(80, [(0.1, 0.1), (0.2, 0.1)], closed=False)
    o.rel(8, [("w", 80, "outer"), ("w", 81, "outer")],
          {"name": "Far Town", "admin_level": "8"})
    # A named closed way that is itself an area.
    o.way(9, square(3.1, 0.1, 3.3, 0.3), tags={"boundary": "administrative",
                                               "admin_level": "8", "name": "Way Town"})
    # Testland mapped a second time, at level 8: kept once, at level 2.
    o.way(10, square(0, 0, 4, 4))
    o.rel(10, [("w", 10, "outer")], {"name": "Testland", "admin_level": "8"})
    o.way(11, square(0.5, 0.5, 0.6, 0.6))
    o.rel(11, [("w", 11, "outer")], {"name": "School Zone", "admin_level": "11"})
    # boundary=place: a city is kept (ranked as level 8), a hamlet is not.
    o.way(12, square(2.0, 0.5, 2.5, 1.0))
    o.rels.append((12, [("w", 12, "outer")], {"type": "boundary", "boundary": "place",
                                              "place": "city", "name": "Placeville"}))
    o.way(13, square(2.6, 0.5, 2.7, 0.6))
    o.rels.append((13, [("w", 13, "outer")], {"type": "boundary", "boundary": "place",
                                              "place": "hamlet", "name": "Tiny"}))
    # Clipped, with an admin_centre: kept only when the node lies in the box
    # of the members the extract has.
    o.way(140, [(0.1, 3.4), (0.3, 3.6)], closed=False)
    c1 = o.node(0.2, 3.5, nid=103)
    o.rel(14, [("w", 140, "outer"), ("w", 141, "outer"), ("n", c1, "admin_centre")],
          {"name": "Centre Town", "admin_level": "8"})
    o.way(150, [(0.1, 1.4), (0.3, 1.6)], closed=False)
    c2 = o.node(1.5, 1.5, nid=104)
    o.rel(15, [("w", 150, "outer"), ("w", 151, "outer"), ("n", c2, "admin_centre")],
          {"name": "Far Centre", "admin_level": "8"})
    return o.write(tmp_path / "x.osm")


GEONAMES = A.GeoNamesPlaces([
    {"name": "Arlington", "admin1": "Virginia", "admin2": "Arlington County",
     "cc": "US", "lat": "1.2", "lon": "5.1"},
    # Far Town: 0.8 deg (~90 km) from its members, too far to stand for it.
    {"name": "Far Town", "admin1": "Nowhere", "admin2": "", "cc": "US",
     "lat": "0.9", "lon": "0.9"},
])


@pytest.fixture
def no_rg(monkeypatch):
    monkeypatch.setattr(A, "_rg_lookup", lambda pts: [{"admin1": "Rg Region", "cc": "US"}
                                                     for _ in pts])


needs_osmium = pytest.mark.skipif(not A.shutil.which("osmium"),
                                  reason="osmium CLI not installed")


@needs_osmium
def test_extract(extract, no_rg):
    pytest.importorskip("osmium")
    stats = {}
    feats = {f["name"]: f for f in A.extract_admin_areas(extract, geonames=GEONAMES,
                                                         stats=stats)}
    assert sorted(feats) == ["Arlington County", "Centre Town", "Clip Town", "North State",
                             "Placeville", "Testland", "Upper County", "Ward 1", "Way Town"]
    assert stats["polygon"] == 7 and stats["label"] == 1 and stats["geonames"] == 1
    assert stats["admin_centre"] == 1
    assert feats["Placeville"]["subtype"] == "city" and feats["Placeville"]["admin_level"] == 8
    assert (feats["Centre Town"]["lon"], feats["Centre Town"]["lat"]) == (0.2, 3.5)

    land = feats["Testland"]
    assert land["admin_level"] == 2 and land["subtype"] == "country"
    assert land["bbox"] == [0, 0, 4, 4] and land["location"] == ""
    assert land["alt"] == ["Republic of Testland"] and land["osm"] == "r1"

    state = feats["North State"]
    assert state["subtype"] == "state" and state["location"] == "Testland"
    assert state["wikidata"] == "Q2"

    county = feats["Upper County"]
    # The centroid is in the notch; the point is on the polygon.
    assert A.point_in_rings([U_RING], county["lon"], county["lat"])
    assert county["subtype"] == "county"       # US (from the state's ISO3166-2), level 6
    assert county["location"] == "North State"
    assert county["wikipedia"] == "en:Upper County"

    ward = feats["Ward 1"]
    assert (ward["lon"], ward["lat"]) == (1.2, 3.0)   # its label node
    assert ward["location"] == "Upper County" and ward["subtype"] == "ward"
    assert ward["alt"] == ["First Ward", "W1"]

    clip = feats["Clip Town"]
    assert (clip["lon"], clip["lat"]) == (3.5, 1.0) and "bbox" not in clip

    arl = feats["Arlington County"]
    assert (arl["lon"], arl["lat"]) == (5.1, 1.2) and "bbox" not in arl
    assert arl["location"] == "Virginia" and arl["subtype"] == "county"
    assert arl["osm"] == "r6" and arl["wikidata"] == "Q107126"
    assert arl["geonames"] is True and "geonames" not in ward

    assert feats["Way Town"]["osm"] == "w9"
    assert feats["Way Town"]["bbox"] == [3.1, 0.1, 3.3, 0.3]


@needs_osmium
@pytest.mark.parametrize("cut", [False, True])
def test_bbox_keeps_areas_whose_point_is_inside(extract, no_rg, monkeypatch, cut):
    """`cut`: the boundaries cut to the box with osmium extract first, as
    for a planet or continent input."""
    pytest.importorskip("osmium")
    if cut:
        monkeypatch.setattr(A, "EXTRACT_MIN_BYTES", 0)
    names = {f["name"] for f in A.extract_admin_areas(
        extract, (-1, -1, 4.5, 5), geonames=GEONAMES)}
    assert "Arlington County" not in names       # its point is at lon 5.1
    assert "Testland" in names and "Ward 1" in names


@needs_osmium
def test_clipped_areas_need_geonames_or_a_node(extract, no_rg):
    pytest.importorskip("osmium")
    names = {f["name"] for f in A.extract_admin_areas(extract, geonames=False)}
    assert "Arlington County" not in names and "Clip Town" in names


@needs_osmium
def test_append_admin_areas(extract, no_rg, tmp_path, monkeypatch):
    pytest.importorskip("osmium")
    monkeypatch.setattr(A.GeoNamesPlaces, "load", classmethod(lambda cls: GEONAMES))
    out = tmp_path / "search.jsonl"
    out.write_text('{"name": "Cafe", "type": "poi", "lat": 1, "lon": 1}\n')
    assert A.append_admin_areas(extract, str(out)) == 9
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines[0]["name"] == "Cafe" and len(lines) == 10
    assert all(f["type"] == "admin" for f in lines[1:])
    # A search cache that already has them (reused with --search-cache).
    assert A.append_admin_areas(extract, str(out)) == 0
    assert len(out.read_text().splitlines()) == 10


def test_append_skips_without_osmium(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(A.shutil, "which", lambda name: None)
    out = tmp_path / "search.jsonl"
    out.write_text("")
    assert A.append_admin_areas("/nonexistent.pbf", str(out)) == 0
    assert "osmium CLI not found" in capsys.readouterr().out


def test_names():
    assert A.names({"name": "Wien", "name:en": "Vienna"}) == ("Wien", ["Vienna"])
    assert A.names({"name": "東京都", "name:en": "Tokyo"}) == ("Tokyo", ["東京都"])
    assert A.names({"name": "Москва"}) == ("Москва", [])
    display, alts = A.names({"name": "District of Columbia", "short_name": "D.C.",
                             "loc_name": "The District", "alt_name": "D.C.;DC"})
    assert display == "District of Columbia" and alts == ["D.C.", "DC", "The District"]


@pytest.mark.parametrize("tags,level,cc,label", [
    ({"border_type": "county"}, 6, "US", "county"),
    ({"border_type": "nation"}, 2, None, "country"),
    ({"border_type": "departement"}, 6, "FR", "department"),
    ({"border_type": "territorial"}, 3, "FR", "region"),    # unknown value: the table
    ({"place": "city"}, 8, "ZZ", "city"),                   # no convention: place
    ({"place": "city"}, 8, "DE", "municipality"),           # the convention first
    ({"place": "county"}, 6, "LU", "canton"),               # Luxembourg's cantons
    ({"place": "borough"}, 9, "US", "ward"),                # Washington's wards
    ({}, 6, "FR", "department"),
    ({}, 8, "FR", "commune"),
    ({}, 6, "DE", "district"),
    ({}, 4, "US", "state"),
    ({}, 6, "LU", "canton"),
    ({}, 7, "ZZ", "district"),                              # generic
    ({}, 2, "ZZ", "country"),
])
def test_type_label(tags, level, cc, label):
    assert A.type_label(tags, level, cc) == label


def test_admin_level_and_country():
    assert [A.admin_level(v) for v in ("2", " 10 ", "11", "1", "x", None)] == [
        2, 10, None, None, None, None]
    assert A.country_of({"ISO3166-1": "lu"}) == "LU"
    assert A.country_of({"ISO3166-2": "US-DC"}) == "US"
    assert A.country_of({"ISO3166-2": "garbage"}) is None


def test_representative_point():
    # Hints first, when inside; holes count.
    outer = [square(0, 0, 10, 10)]
    hole = [square(4, 4, 6, 6)]
    assert A.representative_point(outer, [], [(1, 1)]) == (1, 1)
    assert A.representative_point(outer, [], [(20, 20), (2, 2)]) == (2, 2)
    p = A.representative_point(outer, hole)
    assert A.point_in_rings(outer + hole, *p)
    p = A.representative_point([U_RING], [])
    assert A.point_in_rings([U_RING], *p)


def test_fit_zoom():
    assert A.fit_zoom((-180, -85, 180, 85)) == 2.0
    dc = A.fit_zoom((-77.11979, 38.79163, -76.90937, 38.99597))
    assert 10.5 <= dc <= 11.5
    assert A.fit_zoom((7.0, 43.0, 7.0, 43.0)) == 16.0


def test_dedupe_is_not_quadratic():
    import time
    feats = [{"name": f"Area {i % 20000}", "admin_level": 8, "osm": f"r{i}",
              "bbox": [i, 0, i + 1, 1]} for i in range(40000)]
    t = time.time()
    assert len(A.dedupe(feats)) == 40000
    assert time.time() - t < 2


def test_bbox_across_the_antimeridian():
    ring = [(179.0, 0.0), (-179.0, 0.0), (-179.0, 1.0), (179.0, 1.0)]
    assert A.bbox_of([ring]) == (179.0, 0.0, 181.0, 1.0)
    assert A.bbox_of([square(0, 0, 1, 1)]) == (0, 0, 1, 1)


def test_grid():
    rings = [A._thin(square(0, 0, 2, 2))]
    a = {"bbox": (0, 0, 2, 2), "rings": rings, "level": 4}
    b = {"bbox": (179.0, 0, 181.0, 1), "level": 6,
         "rings": [A._thin([(179.0, 0.0), (180.0, 0.0), (180.0, 1.0), (179.0, 1.0)])]}
    g = A.Grid([a, b])
    assert g.holding(1, 1) == [a] and g.holding(3, 1) == []
    assert g.holding(179.5, 0.5) == [b]


# Bristol, Tennessee and Bristol, Virginia: twin towns across a state line.
BRISTOLS = A.GeoNamesPlaces([
    {"name": "Bristol", "admin1": "Tennessee", "admin2": "Sullivan County", "cc": "US",
     "lat": "1.7", "lon": "3.25"},
    {"name": "Bristol", "admin1": "Virginia", "admin2": "City of Bristol", "cc": "US",
     "lat": "2.1", "lon": "3.3"},
])
BORDER = (3.2, 1.9, 3.6, 1.95)      # the members of Bristol VA a TN extract has
BRISTOL_VA = {"name": "Bristol", "admin_level": "6", "border_type": "city"}


def test_twin_towns_without_polygons_are_left_out():
    stats = {}
    assert A.place_clipped(BRISTOLS, BRISTOL_VA, 6, BORDER, None, stats) is None
    assert stats["geonames ambiguous"] == 1


@pytest.mark.parametrize("tags,state", [
    ({"ISO3166-2": "US-VA"}, "Virginia"),
    ({"is_in:state": "Tennessee"}, "Tennessee"),
    ({"is_in:state_code": "VA"}, "Virginia"),
])
def test_twin_towns_by_their_tags(tags, state):
    hit = A.place_clipped(BRISTOLS, dict(BRISTOL_VA, **tags), 6, BORDER, None)
    assert hit["admin1"] == state


@needs_osmium
def test_bristol_va_in_a_tennessee_extract(tmp_path, no_rg):
    """The TN extract has Sullivan County and Bristol TN whole, Bristol VA
    clipped: GeoNames' Bristol TN lies in Sullivan County (level 6, as
    Bristol VA), so only Bristol VA's own place stands for it."""
    pytest.importorskip("osmium")
    o = Osm()
    o.way(1, square(0, 0, 4, 1.9))
    o.rel(1, [("w", 1, "outer")], {"name": "Tennessee", "admin_level": "4",
                                   "ISO3166-2": "US-TN"})
    o.way(2, square(2, 0, 4, 1.85))
    o.rel(2, [("w", 2, "outer")], {"name": "Sullivan County", "admin_level": "6"})
    o.way(3, square(3.1, 1.6, 3.4, 1.8))
    o.rel(3, [("w", 3, "outer")], {"name": "Bristol", "admin_level": "8"})
    o.way(40, [(3.2, 1.9), (3.6, 1.95)], closed=False)
    o.rel(4, [("w", 40, "outer"), ("w", 41, "outer")], dict(BRISTOL_VA))
    feats = A.extract_admin_areas(o.write(tmp_path / "tn.osm"), geonames=BRISTOLS)
    va = [f for f in feats if f["osm"] == "r4"]
    assert len(va) == 1 and (va[0]["lon"], va[0]["lat"]) == (3.3, 2.1)
    assert va[0]["location"] == "Virginia"


def test_dedupe_keeps_the_lower_level():
    a = {"name": "Monaco", "admin_level": 8, "osm": "r2", "bbox": [0, 0, 1, 1]}
    b = {"name": "Monaco", "admin_level": 2, "osm": "r1", "bbox": [0, 0, 1, 1.005]}
    c = {"name": "Monaco", "admin_level": 10, "osm": "r3", "bbox": [0, 0, 0.5, 0.5]}
    assert [f["osm"] for f in A.dedupe([a, b, c])] == ["r1", "r3"]


def test_geonames_locate():
    near = (5.0, 1.0, 5.0, 1.5)
    assert GEONAMES.locate("Arlington County", near, 40)["name"] == "Arlington"
    assert GEONAMES.locate("Arlington County", near, 5) is None
    assert GEONAMES.locate("Arlington", near, 40)["name"] == "Arlington"
    # "County of X" or a different division does not stand in.
    assert GEONAMES.locate("Arlington Heights", near, 40) is None


@needs_osmium
def test_the_box_cut_keeps_whole_relations(tmp_path, no_rg, monkeypatch):
    """The cut keeps a relation with a member (a boundary node, its label or
    admin_centre) in the box plus the margin, and completes it; one with
    none there is dropped before Python reads it (documented: an area
    around the whole box with no member near it)."""
    pytest.importorskip("osmium")
    monkeypatch.setattr(A, "EXTRACT_MIN_BYTES", 0)
    o = Osm()
    o.way(1, square(0, 0, 10, 10))
    lab = o.node(5, 5, nid=100)
    o.rel(1, [("w", 1, "outer"), ("n", lab, "label")], {"name": "Big", "admin_level": "4"})
    o.way(2, square(0, 0, 10, 10.5))
    o.rel(2, [("w", 2, "outer")], {"name": "Unlabelled", "admin_level": "2"})
    o.way(3, square(20, 20, 21, 21))
    o.rel(3, [("w", 3, "outer")], {"name": "Far", "admin_level": "8"})
    o.way(4, square(6.2, 4, 8, 6))
    o.rel(4, [("w", 4, "outer")], {"name": "Near", "admin_level": "8"})
    stats = {}
    feats = A.extract_admin_areas(o.write(tmp_path / "x.osm"), (4, 4, 7.5, 6),
                                  geonames=False, stats=stats)
    assert sorted(f["name"] for f in feats) == ["Big", "Near"]
    assert [f["bbox"] for f in feats if f["name"] == "Big"] == [[0, 0, 10, 10]]
    assert stats["relations"] == 2
