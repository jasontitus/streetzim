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
    ({"highway": "bus_stop"}, "Point", "stop"),
    ({"highway": "crossing"}, "Point", "other"),
    ({"railway": "level_crossing"}, "Point", "other"),
    ({"public_transport": "stop_position", "highway": "primary"}, "Point", "stop"),
    ({"public_transport": "platform", "railway": "platform"}, "LineString", "stop"),
    ({"amenity": "bus_station"}, "LineString", "stop"),
    ({"railway": "station"}, "Point", "station"),
    ({"railway": "station", "building": "train_station"}, "LineString", "station"),
    ({"railway": "tram_stop"}, "Point", "station"),
    ({"railway": "tram_stop", "public_transport": "stop_position"}, "Point", "station"),
    ({"railway": "stop", "public_transport": "platform"}, "Point", "station"),
    ({"railway": "halt"}, "Point", "station"),
    ({"building": "train_station"}, "LineString", "other"),
    ({"amenity": "clock"}, "Point", "other"),
    ({"tourism": "information"}, "Point", "information"),
    ({"tourism": "information", "place": "locality"}, "Point", "place"),
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
        "zürich@473744": "place", "zürich@473700": "information", "limmat@473700": "water"}
    names = extract_name_tags_pbf(str(osm), ["rm"], precut=True)
    assert {k[0] + "@" + str(k[1]): v.kind for k, v in names.items()} == {
        "zürich@473744": "place", "zürich@473700": "information", "limmat@473700": "water"}


# --- review of cbea330 ------------------------------------------------------

def test_a_bus_stop_takes_nothing_and_a_station_only_a_station():
    # The bus stop "Patras" beside Patras railway station; a stop of its
    # name tagged with its operator's item; the station record itself.
    bus = {"name": "Patras", "type": "poi", "subtype": "bus", "lat": LAT, "lon": LON}
    station = OsmRef("station", {"wikidata": "Q1"})
    stop = OsmRef("stop", {"wikipedia": "pl:Komunikacja Miejska w Głogowie", "wikidata": "Q11742123"})
    for kind in ("station", "stop", "other", "place", "information"):   # "other": "Kurtheater"
        assert match(bus, {("patras",) + NEXT: OsmRef(kind, {"wikidata": "Q3"})}) is None
    assert match(bus, {("patras",) + NEXT: stop}) is None
    # In its own cell, as before the cells around were looked at.
    assert match(bus, {("patras", 460000, 80000): stop}) is stop
    rail = dict(bus, subtype="railway")
    assert match(rail, {("patras",) + NEXT: station}) is station
    assert match(rail, {("patras",) + NEXT: stop}) is None
    assert match(rail, {("patras",) + NEXT: OsmRef("other", {"wikidata": "Q3"})}) is None


def test_an_information_board_takes_nothing_and_gives_nothing():
    board = {"name": "Wettingen", "type": "poi", "subtype": "information", "lat": LAT, "lon": LON}
    for kind in ("station", "other", "information", "stop", "place"):
        assert match(board, {("wettingen",) + NEXT: OsmRef(kind, {"wikidata": "Q1"})}) is None
    theatre = {"name": "Kurtheater", "type": "poi", "subtype": "theatre", "lat": LAT, "lon": LON}
    for kind in ("information", "stop", "station"):
        assert match(theatre, {("kurtheater",) + NEXT: OsmRef(kind, {"wikidata": "Q1"})}) is None
    assert match(theatre, {("kurtheater",) + NEXT: OsmRef("other", {"wikidata": "Q1"})}) == {"wikidata": "Q1"}


def test_in_its_own_cell_the_sort_does_not_matter():
    # As before the cells around were looked at: a same-cell match keeps.
    bus = {"name": "Patras", "type": "poi", "subtype": "bus", "lat": LAT, "lon": LON}
    station = OsmRef("station", {"wikidata": "Q1"})
    assert match(bus, {("patras", 460000, 80000): station}) is station


def test_the_cells_beyond_must_agree():
    # A ruin of the castle's name two cells away tagged with the village;
    # the castle itself next door.
    castle = {"name": "Castle of Monolithos", "type": "poi", "subtype": "castle", "lat": LAT, "lon": LON}
    near = OsmRef("other", {"wikipedia": "en:Monolithos,_Greece", "wikidata": "Q1"})
    far = OsmRef("other", {"wikidata": "Q56450446"})
    assert match(castle, {("castle of monolithos",) + NEXT: near,
                          ("castle of monolithos", 460002, 80000): far}) is None
    # The same article beyond, or another sort beyond: taken.
    assert match(castle, {("castle of monolithos",) + NEXT: near,
                          ("castle of monolithos", 460002, 80001): OsmRef("other", dict(near))}) is near
    assert match(castle, {("castle of monolithos",) + NEXT: near,
                          ("castle of monolithos", 460000, 79998): OsmRef("place", {"wikidata": "Q9"})}) is near
    # Beyond alone: none; three cells away: not looked at.
    assert match(castle, {("castle of monolithos", 460002, 80000): far}) is None
    assert match(castle, {("castle of monolithos",) + NEXT: near,
                          ("castle of monolithos", 460003, 80000): far}) is near


def test_an_ambiguous_folded_key_beyond_matches_none():
    poi = {"name": "Kaplica grobowa", "type": "poi", "lat": LAT, "lon": LON}
    refs = {("kaplica grobowa",) + NEXT: OsmRef("other", {"wikidata": "Q1"}),
            ("kaplica grobowa", 459998, 80000): OsmRef("other", {"wikidata": "Q1"}),
            ("kapliča grobowa", 459998, 80000): OsmRef("other", {"wikidata": "Q2"})}
    assert match(poi, refs) is None


def test_a_disagreeing_own_cell_is_not_rescued_from_next_door():
    clean = place({"wikidata": "Q72"})
    # Its own-script name in its own cells: two articles.
    feat = {"name": "Zurich", "name_native": "Züri", "type": "place", "lat": LAT, "lon": LON,
            "_pts": [[LAT, LON], [46.1, 8.1]]}
    refs = {("züri", 460000, 80000): place({"wikidata": "Q1"}),
            ("züri", 461000, 81000): place({"wikidata": "Q2"}),
            ("züri",) + NEXT: clean}
    assert match(feat, refs) is None
    # Folded in its own cell: ambiguous.
    feat = {"name": "Zurich", "type": "place", "lat": LAT, "lon": LON}
    refs = {("zürich", 460000, 80000): place({"wikidata": "Q1"}),
            ("zúrich", 460000, 80000): place({"wikidata": "Q2"}),
            ("zurich",) + NEXT: clean}
    assert match(feat, refs) is None


def test_a_poi_with_a_plain_entry_is_not_taken():
    poi = {"name": "Roloi", "type": "poi", "lat": LAT, "lon": LON}
    assert match(poi, {("roloi",) + NEXT: {"wikidata": "Q3"}}) is None


def test_without_a_folded_index_every_name_looks_around():
    feat = {"name": "Athens", "name_native": "Αθήνα", "type": "place", "lat": LAT, "lon": LON}
    refs = {("αθήνα",) + NEXT: place({"wikidata": "Q1524"})}
    assert W.feature_wiki(feat, refs) == {"wikidata": "Q1524"}


def test_the_match_says_whether_it_came_from_next_door():
    own = place({"wikidata": "Q1"})
    feat = {"name": "Zurich", "type": "place", "lat": LAT, "lon": LON}
    assert W.feature_wiki_match(feat, {("zurich", 460000, 80000): own}) == (own, False)
    assert W.feature_wiki_match(feat, {("zurich",) + NEXT: own}) == (own, True)
    assert W.feature_wiki_match(feat, {}) == (None, False)
    assert W.feature_wiki_match(feat, {("x", 1, 2): own}) == (None, False)


def _geo(tmp_path, feats, refs, bundled):
    tmp_path.mkdir(exist_ok=True)
    path = tmp_path / "search.jsonl"
    path.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in feats), encoding="utf-8")
    (tmp_path / "chunks").mkdir()
    return W._search_bucket(search_features_path=str(path), wikidata_data=None,
                            wiki_cross_refs=refs, loc_lookup=None, _bundled_set=bundled,
                            chunk_tmp=str(tmp_path / "chunks")).wiki_geo


def test_a_city_places_its_article_over_an_earlier_namesake(tmp_path):
    # São Paulo: a village in Pará tagged with the city's article comes
    # first in the file; the city record places the row. A village of
    # the name does not take it from a town either way round.
    tags = {"wikipedia": "en:São_Paulo", "wikidata": "Q174"}
    village = {"name": "São Paulo", "type": "place", "subtype": "village", "lat": -1.20432, "lon": -47.1584}
    city = {"name": "São Paulo", "type": "place", "subtype": "city", "lat": -23.5506464, "lon": -46.6333848}
    refs = {("são paulo", -12043, -471584): place(dict(tags)), ("são paulo",) + SPCELL: place(dict(tags))}
    assert _geo(tmp_path, [village, city], refs, {"São_Paulo"})["São_Paulo"][:3] == [-23.55065, -46.63338, "place"]


def test_an_own_cell_match_places_over_an_earlier_one_from_next_door(tmp_path):
    tags = {"wikipedia": "en:Fira", "wikidata": "Q1"}
    near = {"name": "Fira", "type": "poi", "subtype": "ferry_terminal", "lat": LAT, "lon": LON}
    own = {"name": "Fira", "type": "poi", "subtype": "attraction", "lat": 46.1, "lon": 8.1}
    refs = {("fira",) + NEXT: OsmRef("other", dict(tags)), ("fira", 461000, 81000): OsmRef("other", dict(tags))}
    assert _geo(tmp_path, [near, own], refs, {"Fira"})["Fira"][:2] == [46.1, 8.1]


def test_equal_records_keep_the_first(tmp_path):
    tags = {"wikipedia": "en:Fira", "wikidata": "Q1"}
    a = {"name": "Fira", "type": "poi", "lat": LAT, "lon": LON}
    b = {"name": "Fira", "type": "poi", "lat": 46.1, "lon": 8.1}
    refs = {("fira", 460000, 80000): OsmRef("other", dict(tags)), ("fira", 461000, 81000): OsmRef("other", dict(tags))}
    assert _geo(tmp_path, [a, b], refs, {"Fira"})["Fira"][:2] == [LAT, LON]
    # A street, even from its own cell, gives way to a record from next door.
    street = {"name": "Fira", "type": "street", "lat": 46.1, "lon": 8.1}
    refs[("fira",) + NEXT] = refs.pop(("fira", 460000, 80000))
    assert _geo(tmp_path / "s", [street, a], refs, {"Fira"})["Fira"][:3] == [LAT, LON, "poi"]
