"""Wikipedia cross-refs (and the names lookup) in the cells around a search
record's own (zim_writer._neighbour_link). A tile's point of an OSM node
is within a metre of it, but the lookup's cells are 11 m: Zurich's record
(47.37445075) is in cell 473745 and its node (47.3744489) in 473744, so the
city had no w/q; São Paulo's likewise. Only an OSM object of the record's
own sort (addresses.osm_kind) is taken there, and only when every one found
links the same article."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import zim_writer as W  # noqa: E402
from streetzim.addresses import OsmRef, osm_kind  # noqa: E402

ZURICH = {"name": "Zurich", "type": "place", "subtype": "city",
          "lat": 47.37445075382187, "lon": 8.541038632392883}
ZCELL = (473744, 85410)                 # the node, 47.3744489 8.5410422
ZTAGS = {"wikipedia": "en:Zürich", "wikidata": "Q72"}
SAO_PAULO = {"name": "São Paulo", "type": "place", "subtype": "city",
             "lat": -23.550646354424163, "lon": -46.63338482379913}
SPCELL = (-235507, -466334)             # the node, -23.5506507 -46.6333824


def place(tags=ZTAGS):
    return OsmRef("place", tags)


def match(feat, refs):
    return W.feature_wiki(feat, refs, W.wiki_folded_index(refs))


def test_the_record_and_the_node_are_in_cells_side_by_side():
    assert (int(round(ZURICH["lat"] * 1e4)), int(round(ZURICH["lon"] * 1e4))) == (473745, 85410)
    assert (int(round(47.3744489 * 1e4)), int(round(8.5410422 * 1e4))) == ZCELL


def test_a_folded_name_in_the_next_cell():
    refs = {("zürich",) + ZCELL: place()}
    assert match(ZURICH, refs) == ZTAGS


def test_a_name_as_it_is_in_the_next_cell():
    refs = {("são paulo",) + SPCELL: place({"wikidata": "Q174"})}
    assert W.feature_wiki(SAO_PAULO, refs) == {"wikidata": "Q174"}


def test_a_diagonal_cell():
    refs = {("zürich", 473744, 85411): place()}
    assert match(ZURICH, refs) == ZTAGS


def test_two_cells_away_is_too_far():
    refs = {("zürich", 473743, 85410): place()}
    assert match(ZURICH, refs) is None


def test_an_object_of_another_sort_is_not_taken():
    # The review's cases: a peak <- an information board, a quarter <- a
    # river, a neighbourhood <- a clock.
    peak = {"name": "Monte Pravello", "type": "peak", "lat": LAT, "lon": LON}
    assert match(peak, {("monte pravello",) + NEXT: OsmRef("other", {"wikidata": "Q1"})}) is None
    assert match(peak, {("monte pravello",) + NEXT: OsmRef("peak", {"wikidata": "Q1"})}) == {"wikidata": "Q1"}
    quarter = {"name": "Vrbanja", "type": "place", "lat": LAT, "lon": LON}
    assert match(quarter, {("vrbanja",) + NEXT: OsmRef("water", {"wikidata": "Q2"})}) is None
    poi = {"name": "Roloi", "type": "poi", "lat": LAT, "lon": LON}
    assert match(poi, {("roloi",) + NEXT: OsmRef("place", {"wikidata": "Q3"})}) is None
    assert match(poi, {("roloi",) + NEXT: OsmRef("other", {"wikidata": "Q3"})}) == {"wikidata": "Q3"}
    street = {"name": "Via Roma", "type": "street", "lat": LAT, "lon": LON}
    assert match(street, {("via roma",) + NEXT: OsmRef("street", {"wikidata": "Q4"})}) == {"wikidata": "Q4"}
    water = {"name": "Limmat", "type": "water", "lat": LAT, "lon": LON}
    assert match(water, {("limmat",) + NEXT: OsmRef("water", {"wikidata": "Q5"})}) == {"wikidata": "Q5"}


# A record well inside its cell, and the cell next to it.
LAT, LON = 46.00001, 8.00001
NEXT = (460001, 80000)


def test_of_its_sort_beside_another_sort():
    refs = {("roloi",) + NEXT: OsmRef("place", {"wikidata": "Q9"}),
            ("roloi", 460001, 80001): OsmRef("other", {"wikidata": "Q3"})}
    poi = {"name": "Roloi", "type": "poi", "lat": LAT, "lon": LON}
    assert match(poi, refs) == {"wikidata": "Q3"}


def test_an_entry_that_does_not_know_its_sort_is_not_taken():
    # Lookups made by hand, or by an extractor without osm_kind.
    refs = {("zürich",) + ZCELL: dict(ZTAGS)}
    assert match(ZURICH, refs) is None


def test_neighbours_that_link_two_articles_match_none():
    refs = {("zürich",) + ZCELL: place(),
            ("zürich", 473746, 85410): place({"wikidata": "Q11943"})}
    assert match(ZURICH, refs) is None
    refs[("zürich", 473746, 85410)] = place({"wikidata": "Q72", "wikipedia": "en:Zurich"})
    assert match(ZURICH, refs) is refs[("zürich",) + ZCELL]


def test_an_ambiguous_folded_key_around_matches_none():
    refs = {("zürich",) + ZCELL: place(),
            ("zúrich",) + ZCELL: place({"wikidata": "Q99"})}
    assert match(ZURICH, refs) is None
    # Not even beside an unambiguous one in another cell around.
    refs[("zürich", 473746, 85410)] = place()
    assert match(ZURICH, refs) is None


def test_the_own_cell_wins():
    own = place({"wikidata": "Q1"})
    refs = {("zurich", 473745, 85410): own, ("zurich",) + ZCELL: place({"wikidata": "Q2"})}
    assert match(ZURICH, refs) is own
    # Folded in its own cell, as it is next door: still its own cell.
    refs = {("zürich", 473745, 85410): own, ("zurich",) + ZCELL: place({"wikidata": "Q2"})}
    assert match(ZURICH, refs) is own


def test_an_address_never_looks_around():
    addr = {"name": "Zurich", "type": "addr", "lat": ZURICH["lat"], "lon": ZURICH["lon"]}
    assert match(addr, {("zürich",) + ZCELL: OsmRef("other", ZTAGS)}) is None


def test_around_a_merged_streets_pieces():
    feat = {"name": "Limmatquai", "type": "street", "lat": 47.37, "lon": 8.54,
            "_pts": [[47.37, 8.54], [47.37445075, 8.54]]}
    refs = {("limmatquai", 473744, 85400): OsmRef("street", {"wikidata": "Q1"})}
    assert match(feat, refs) == {"wikidata": "Q1"}
    refs = {("limmatquai", 473744, 85401): OsmRef("street", {"wikidata": "Q1"})}   # diagonal
    assert match(feat, refs) == {"wikidata": "Q1"}


def test_the_record_gets_w_q_and_a_geo_row(tmp_path):
    path = tmp_path / "search.jsonl"
    path.write_text(json.dumps(ZURICH) + "\n", encoding="utf-8")
    (tmp_path / "chunks").mkdir()
    b = W._search_bucket(search_features_path=str(path), wikidata_data=None,
                         wiki_cross_refs={("zürich",) + ZCELL: place(dict(ZTAGS))},
                         loc_lookup=None, _bundled_set={"Zürich"},
                         chunk_tmp=str(tmp_path / "chunks"))
    assert b.wiki_fields_added == 1
    assert b.wiki_geo == {"Zürich": [47.37445, 8.54104, "place", "Q72", ""]}
    [rec] = [json.loads(x) for x in (tmp_path / "chunks" / "zu.jsonl").read_text().splitlines()]
    assert (rec["w"], rec["q"]) == ("en:Zürich", "Q72")


def test_the_names_lookup_looks_around_too(tmp_path):
    village = {"name": "Glion", "type": "place", "subtype": "village",
               "lat": 46.77386, "lon": 9.2051}          # cell 467739
    names = {("glion", 467738, 92051): OsmRef("place", names={"rm": "Glion", "de": "Ilanz"})}
    path = tmp_path / "search.jsonl"
    path.write_text(json.dumps(village) + "\n", encoding="utf-8")
    (tmp_path / "chunks").mkdir()
    W._search_bucket(search_features_path=str(path), wikidata_data=None,
                     wiki_cross_refs={}, loc_lookup=None, _bundled_set=set(),
                     chunk_tmp=str(tmp_path / "chunks"), ln_languages=["de", "rm"],
                     ln_name_refs=names)
    [rec] = [json.loads(x) for x in (tmp_path / "chunks" / "gl.jsonl").read_text().splitlines()]
    assert rec.get("ln", {}).get("de") == "Ilanz"


@pytest.mark.parametrize("tags,gtype,kind", [
    ({"place": "city"}, "Point", "place"),
    ({"place": "sea"}, "Point", "water"),
    ({"natural": "water", "name": "x"}, "Polygon", "water"),
    ({"waterway": "river"}, "LineString", "water"),
    ({"water": "lake", "place": "locality"}, "Polygon", "water"),
    ({"landuse": "reservoir"}, "Polygon", "water"),
    ({"natural": "peak"}, "Point", "peak"),
    ({"natural": "saddle", "place": "locality"}, "Point", "peak"),
    ({"highway": "primary"}, "LineString", "street"),
    ({"railway": "rail"}, "LineString", "street"),
    ({"aerialway": "cable_car"}, "LineString", "street"),
    ({"highway": "bus_stop"}, "Point", "other"),
    ({"railway": "station"}, "Point", "other"),
    ({"amenity": "clock"}, "Point", "other"),
    ({"tourism": "information"}, "Point", "other"),
    ({"building": "church"}, "LineString", "other"),
])
def test_osm_kind(tags, gtype, kind):
    assert osm_kind(tags, gtype) == kind


def test_an_osm_ref_is_a_plain_dict_to_everyone_else():
    import pickle
    r = OsmRef("place", ZTAGS)
    assert r == ZTAGS and len(r) == 2 and r.kind == "place"
    assert pickle.loads(pickle.dumps(r)).kind == "place"


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_the_extractors_know_each_objects_sort(tmp_path):
    from streetzim.addresses import extract_name_tags_pbf, extract_wiki_tags_pbf
    osm = tmp_path / "t.osm"
    osm.write_text(
        "<?xml version='1.0' encoding='UTF-8'?>\n<osm version='0.6' generator='t'>\n"
        " <node id='1' version='1' lat='47.3744489' lon='8.5410422'><tag k='place' v='city'/>"
        "<tag k='name' v='Zürich'/><tag k='name:rm' v='Turitg'/><tag k='wikidata' v='Q72'/></node>\n"
        " <node id='2' version='1' lat='47.3700' lon='8.5400'><tag k='tourism' v='information'/>"
        "<tag k='name' v='Zürich'/><tag k='name:rm' v='Turitg'/><tag k='wikidata' v='Q1'/></node>\n"
        " <node id='3' version='1' lat='47.36' lon='8.53'/><node id='4' version='1' lat='47.37' lon='8.54'/>\n"
        " <way id='10' version='1'><nd ref='3'/><nd ref='4'/><tag k='waterway' v='river'/>"
        "<tag k='name' v='Limmat'/><tag k='name:rm' v='Limmat'/><tag k='wikidata' v='Q2'/></way>\n"
        "</osm>\n", encoding="utf-8")
    wiki = extract_wiki_tags_pbf(str(osm), precut=True)
    assert {k[0] + "@" + str(k[1]): v.kind for k, v in wiki.items()} == {
        "zürich@473744": "place", "zürich@473700": "other", "limmat@473700": "water"}
    names = extract_name_tags_pbf(str(osm), ["rm"], precut=True)
    assert {k[0] + "@" + str(k[1]): v.kind for k, v in names.items()} == {
        "zürich@473744": "place", "zürich@473700": "other", "limmat@473700": "water"}
