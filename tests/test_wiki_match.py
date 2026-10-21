"""A search feature's Wikipedia cross-ref (zim_writer.feature_wiki): the
lookup extract_wiki_tags_pbf builds keys on the OSM object's own `name`,
the search feature carries a Latin name and keeps that one as name_native,
unless the two are the same once folded ("Poznan" for Poznań). Matching
the search name alone placed 2,056 of Greece's 5,830 bundled articles in
the wiki geo-index, and neither Athens, Thessaloniki nor Patras; Poznań,
Kraków and Gdańsk had no `w`/`q` on their records."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import zim_writer as W  # noqa: E402
from streetzim.search_extract import search_record as tile_feature  # noqa: E402

# Athens' place node: name=Αθήνα, name:en=Athens, at 37.9755648, 23.7348324.
LAT, LON = 37.9755648, 23.7348324
CELL = (int(round(LAT * 1e4)), int(round(LON * 1e4)))
ATHENS_TAGS = {"wikipedia": "en:Athens", "wikidata": "Q1524"}


def from_tile(ftype, props):
    """A search feature as the extraction makes it from a tile feature's
    properties (search_extract: the name is name:latin, else name)."""
    return tile_feature(props.get("name:latin") or props.get("name", ""), ftype,
                        props, LAT, LON)


def athens():
    """Athens from a StreetZim tilemaker tile: name:latin, and OSM's own
    name as name_int."""
    return from_tile("place", {"class": "city", "name:latin": "Athens",
                               "name_int": "Αθήνα"})


def test_the_feature_keeps_its_own_name():
    assert athens()["name"] == "Athens" and athens()["name_native"] == "Αθήνα"


def test_found_by_its_own_script_name():
    refs = {("αθήνα",) + CELL: ATHENS_TAGS}
    assert W.feature_wiki(athens(), refs) is ATHENS_TAGS


def test_found_by_its_search_name():
    refs = {("lincoln memorial", 388930, -770502): {"wikidata": "Q162458"}}
    feat = {"name": "Lincoln Memorial", "type": "poi", "lat": 38.89302, "lon": -77.05018}
    assert W.feature_wiki(feat, refs) == {"wikidata": "Q162458"}


def test_another_name_at_the_same_point_is_another_object():
    # Syntagma Square's tags on the same 11 m cell as a station named
    # Syntagma: neither of the station's names is the square's.
    refs = {("πλατεία συντάγματος",) + CELL: {"wikidata": "Q842193"}}
    feat = from_tile("poi", {"class": "railway", "name:latin": "Syntagma",
                             "name_int": "Σύνταγμα"})
    assert W.feature_wiki(feat, refs) is None


def test_the_same_name_elsewhere_is_another_object():
    refs = {("αθήνα", CELL[0] + 1, CELL[1]): ATHENS_TAGS}
    assert W.feature_wiki(athens(), refs) is None


def test_a_translation_is_not_a_key():
    # The lookup holds OSM's `name` (or name:latin), never name:xx: a
    # build in German shows "Athen", which keys nothing.
    feat = dict(athens(), names={"de": "Athen"}, display_name="Athen")
    refs = {("athen",) + CELL: {"wikidata": "Q0"}}
    assert W.feature_wiki(feat, refs) is None
    assert W.wiki_match_names(feat) == ["athens", "αθήνα"]


def test_a_merged_street_matches_at_any_piece():
    tags = {"wikidata": "Q1"}
    feat = {"name": "Ermou", "name_native": "Ερμού", "type": "street",
            "lat": 37.9, "lon": 23.7, "_pts": [[37.9, 23.7], [37.976, 23.7315]]}
    assert W.feature_wiki(feat, {("ερμού", 379760, 237315): tags}) is tags


def test_the_search_name_wins_at_every_point():
    # What matched before keeps its entry: the search name is tried at every
    # point before the own-script name is tried at any.
    by_name, by_native = {"wikidata": "Q1"}, {"wikidata": "Q2"}
    feat = {"name": "Ermou", "name_native": "Ερμού", "type": "street",
            "lat": 37.9, "lon": 23.7, "_pts": [[37.9, 23.7], [37.976, 23.7315]]}
    refs = {("ερμού", 379000, 237000): by_native, ("ermou", 379760, 237315): by_name}
    assert W.feature_wiki(feat, refs) is by_name


def test_no_lookup():
    assert W.feature_wiki(athens(), None) is None
    assert W.feature_wiki(athens(), {}) is None


# Poznań's place node: name=Poznań; the search feature is "Poznan".
POZNAN = {"name": "Poznan", "type": "place", "subtype": "city",
          "lat": 52.4082671, "lon": 16.9335151}
POZNAN_CELL = (524083, 169335)


def test_found_by_its_folded_name():
    refs = {("poznań",) + POZNAN_CELL: {"wikidata": "Q268"}}
    assert W.feature_wiki(POZNAN, refs) is None
    assert W.feature_wiki(POZNAN, refs, W.wiki_folded_index(refs)) == {"wikidata": "Q268"}


def test_folded_names_of_two_objects_match_neither():
    refs = {("café central",) + CELL: {"wikidata": "Q1"},
            ("cafè central",) + CELL: {"wikidata": "Q2"}}
    feat = {"name": "Cafe Central", "type": "poi", "lat": LAT, "lon": LON}
    assert W.feature_wiki(feat, refs, W.wiki_folded_index(refs)) is None
    refs[("cafè central",) + CELL] = {"wikidata": "Q1"}      # the same tags twice
    assert W.feature_wiki(feat, refs, W.wiki_folded_index(refs)) == {"wikidata": "Q1"}


def test_one_name_folding_to_two_objects_matches_none():
    # The search name is ambiguous once folded; the other name alone is not.
    refs = {("café central",) + CELL: {"wikidata": "Q1"},
            ("cafè central",) + CELL: {"wikidata": "Q2"},
            ("καφέ σεντράλ",) + CELL: {"wikidata": "Q3"}}
    feat = {"name": "Cafe Central", "name_native": "Καφε Σεντραλ", "type": "poi",
            "lat": LAT, "lon": LON}
    assert W.feature_wiki(feat, refs, W.wiki_folded_index(refs)) is None


def test_a_merged_street_folding_to_two_objects_matches_neither():
    feat = {"name": "Poznanska", "type": "street", "lat": 52.0, "lon": 17.0,
            "_pts": [[52.0, 17.0], [52.01, 17.0]]}
    refs = {("poznańska", 520000, 170000): {"wikidata": "Q1"},
            ("poznańska", 520100, 170000): {"wikidata": "Q2"}}
    assert W.feature_wiki(feat, refs, W.wiki_folded_index(refs)) is None


def test_a_name_as_it_is_wins_over_a_folded_one():
    refs = {("cafe central",) + CELL: {"wikidata": "Q1"},
            ("café central",) + CELL: {"wikidata": "Q2"}}
    feat = {"name": "Cafe Central", "type": "poi", "lat": LAT, "lon": LON}
    assert W.feature_wiki(feat, refs, W.wiki_folded_index(refs)) == {"wikidata": "Q1"}


def test_the_first_name_of_an_osm_list():
    # native_name keeps the first of OSM's "A;B" names.
    feat = from_tile("poi", {"class": "castle", "name:latin": "Kastro Koskina",
                             "name_int": "Κάστρο Κοσκινά;Castle of Lepides"})
    assert feat["name_native"] == "Κάστρο Κοσκινά"
    refs = {("κάστρο κοσκινά;castle of lepides",) + CELL: {"wikidata": "Q16214282"}}
    assert W.feature_wiki(feat, refs, W.wiki_folded_index(refs)) == {"wikidata": "Q16214282"}


def test_the_folded_index_leaves_admin_areas_out():
    assert W.wiki_folded_index({("admin", "r47798"): {"wikidata": "Q803"}}) == {}
    assert W.wiki_folded_index(None) == {}


def test_records_and_geo_index(tmp_path):
    """Through search pass 1: the records get w/q and the bundled articles
    their place in the geo-index, by an own-script and by a folded name."""
    path = tmp_path / "search.jsonl"
    path.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in (athens(), POZNAN)),
                    encoding="utf-8")
    (tmp_path / "chunks").mkdir()
    b = W._search_bucket(search_features_path=str(path),
                         wikidata_data={"Q1524": {"d": "capital of Greece"}},
                         wiki_cross_refs={("αθήνα",) + CELL: dict(ATHENS_TAGS),
                                          ("poznań",) + POZNAN_CELL: {"wikipedia": "en:Poznań",
                                                                      "wikidata": "Q268"}},
                         loc_lookup=None, _bundled_set={"Athens", "Poznań"},
                         chunk_tmp=str(tmp_path / "chunks"))
    assert b.wiki_geo == {"Athens": [37.97556, 23.73483, "place", "Q1524", "capital of Greece"],
                          "Poznań": [52.40827, 16.93352, "place", "Q268", ""]}
    assert b.wiki_fields_added == 2

    def recs(prefix):
        return [(r["n"], r.get("nn"), r["w"], r["q"]) for r in map(
            json.loads, (tmp_path / "chunks" / f"{prefix}.jsonl").read_text(encoding="utf-8").splitlines())]
    assert recs("at") == [("Athens", "Αθήνα", "en:Athens", "Q1524")]
    assert recs("po") == [("Poznan", None, "en:Poznań", "Q268")]


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_the_lookup_from_osm_finds_the_tile_feature(tmp_path):
    """extract_wiki_tags_pbf's key and the tile feature's names agree."""
    from streetzim.addresses import extract_wiki_tags_pbf
    osm = tmp_path / "athens.osm"
    osm.write_text(
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<osm version='0.6' generator='test'>\n"
        f" <node id='1' version='1' lat='{LAT}' lon='{LON}'>\n"
        "  <tag k='place' v='city'/><tag k='name' v='Αθήνα'/>\n"
        "  <tag k='name:en' v='Athens'/><tag k='name:latin' v='Athína'/>\n"
        "  <tag k='wikipedia' v='el:Αθήνα'/><tag k='wikidata' v='Q1524'/>\n"
        " </node>\n</osm>\n", encoding="utf-8")
    refs = extract_wiki_tags_pbf(str(osm), precut=True)
    assert W.feature_wiki(athens(), refs) == {"wikipedia": "el:Αθήνα", "wikidata": "Q1524"}
