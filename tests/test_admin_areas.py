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


@pytest.mark.parametrize("cli", [True, False])
def test_extract(extract, no_rg, monkeypatch, cli):
    if cli:
        if not A.shutil.which("osmium"):
            pytest.skip("osmium CLI not installed")
    else:
        monkeypatch.setattr(A.shutil, "which", lambda name: None)
    pytest.importorskip("osmium")
    stats = {}
    feats = {f["name"]: f for f in A.extract_admin_areas(extract, geonames=GEONAMES,
                                                         stats=stats)}
    assert sorted(feats) == ["Arlington County", "Clip Town", "North State",
                             "Testland", "Upper County", "Ward 1", "Way Town"]
    assert stats["polygon"] == 6 and stats["label"] == 1 and stats["geonames"] == 1

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

    assert feats["Way Town"]["osm"] == "w9"
    assert feats["Way Town"]["bbox"] == [3.1, 0.1, 3.3, 0.3]


def test_bbox_keeps_areas_whose_point_is_inside(extract, no_rg):
    pytest.importorskip("osmium")
    names = {f["name"] for f in A.extract_admin_areas(
        extract, (-1, -1, 4.5, 5), geonames=GEONAMES)}
    assert "Arlington County" not in names       # its point is at lon 5.1
    assert "Testland" in names and "Ward 1" in names


def test_clipped_areas_need_geonames_or_a_node(extract, no_rg):
    pytest.importorskip("osmium")
    names = {f["name"] for f in A.extract_admin_areas(extract, geonames=False)}
    assert "Arlington County" not in names and "Clip Town" in names


def test_append_admin_areas(extract, no_rg, tmp_path, monkeypatch):
    pytest.importorskip("osmium")
    monkeypatch.setattr(A.GeoNamesPlaces, "load", classmethod(lambda cls: GEONAMES))
    out = tmp_path / "search.jsonl"
    out.write_text('{"name": "Cafe", "type": "poi", "lat": 1, "lon": 1}\n')
    assert A.append_admin_areas(extract, str(out)) == 7
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines[0]["name"] == "Cafe" and len(lines) == 8
    assert all(f["type"] == "admin" for f in lines[1:])


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
