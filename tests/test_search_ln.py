"""Search in the visitor's language: a language-ready map's search records
carry `ln`, {lang: name}, OSM's name:<lang> of the place in the map's tile
and region languages, and are indexed and matched under those names, so a
German visitor typing "Genf" finds Geneva (docs/search-records.md). From
the OSM object the record's wiki cross-ref matched (extract_wiki_tags_pbf
with `languages`), an admin area's relation, or the feature's own tile
names. A map that is not language-ready writes no `ln`."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cloud.search_shards import prefixes_for, record_names  # noqa: E402
from streetzim import languages as L  # noqa: E402
from streetzim import zim_writer as W  # noqa: E402

LANGS = ["de", "fr", "it", "ru", "zh"]
# Geneva's place node (name=Genève) and its cross-ref entry with names.
GLAT, GLON = 46.2017590, 6.1465985
GCELL = (int(round(GLAT * 1e4)), int(round(GLON * 1e4)))
GENEVA_NAMES = {"de": "Genf", "fr": "Genève", "it": "Ginevra", "ru": "Женева"}
GENEVA_REF = {"wikipedia": "en:Geneva", "wikidata": "Q71", "names": GENEVA_NAMES}


def geneva():
    return {"name": "Geneva", "name_native": "Genève", "type": "place",
            "subtype": "city", "lat": GLAT, "lon": GLON}


# --- streetzim.languages -------------------------------------------------

def test_first_name_of_an_osm_list():
    assert L.first_name(" 洛杉矶 ; 洛杉磯") == "洛杉矶"
    assert L.first_name(";;Basel") == "Basel"
    assert L.first_name("") == "" and L.first_name(None) == "" and L.first_name(5) == ""


def test_osm_names_as_the_tile_profile_writes_them():
    tags = {"name:de": "Genf", "name:fr": "Genève;Genf", "name:zh": "日內瓦",
            "name:zh-Hans": "日内瓦", "name:it": "  ", "name:en": "Geneva"}
    assert L.osm_names(tags, ["de", "fr", "it", "zh", "en"]) == {
        "de": "Genf", "fr": "Genève", "zh": "日内瓦", "en": "Geneva"}
    assert L.osm_names({"name:zh": "日內瓦"}, ["zh"]) == {"zh": "日內瓦"}
    assert L.osm_names(tags, []) == {}


def test_search_languages():
    assert L.search_languages(["de", "fr", "it"], ["de", "fr", "it", "rm"]) == ["de", "fr", "it", "rm"]
    assert L.search_languages(["ar", "de"], ["el", "en", "de"]) == ["ar", "de", "el"]
    # Not language-ready: none, whatever the region speaks.
    assert L.search_languages([], ["el"]) == [] and L.search_languages(None, None) == []
    # A long row (a continent's) adds none, as for the tiles.
    europe = ["ru", "de", "fr", "en", "it", "es", "pl", "uk", "ro", "nl", "hu"]
    assert len(europe) > L.REGION_TILE_ROW_MAX
    assert L.search_languages(["de", "fr"], europe) == ["de", "fr"]
    assert L.search_languages(["de"], europe[:L.REGION_TILE_ROW_MAX]) == [
        "de", "ru", "fr", "it", "es", "pl"]


def test_the_languages_switch(monkeypatch):
    monkeypatch.setenv("STREETZIM_SEARCH_LN_LANGUAGES", "ru, zh,de")
    assert L.search_languages(["de", "fr", "ru", "zh"], ["rm"]) == ["de", "ru", "zh"]
    monkeypatch.setenv("STREETZIM_SEARCH_LN_LANGUAGES", "")
    assert L.search_languages(["de", "fr"], None) == []
    monkeypatch.delenv("STREETZIM_SEARCH_LN_LANGUAGES")
    assert L.search_languages(["de", "fr"], None) == ["de", "fr"]


def test_the_build_asks_for_the_tile_and_region_languages(monkeypatch):
    import create_osm_zim as C

    from argparse import Namespace
    args = Namespace(tile_languages=["de", "fr"], region_languages=["fr", "rm"])
    monkeypatch.delenv("STREETZIM_SEARCH_LN", raising=False)
    assert C._search_languages(args) == ["de", "fr", "rm"]
    assert C._ln_kwargs(args) == {"languages": ["de", "fr", "rm"]}
    monkeypatch.setenv("STREETZIM_SEARCH_LN", "0")
    assert C._search_languages(args) == [] and C._ln_kwargs(args) == {}
    monkeypatch.delenv("STREETZIM_SEARCH_LN")
    assert C._search_languages(Namespace()) == [] and C._ln_kwargs(Namespace()) == {}


def test_the_writer_reads_the_languages_from_map_config(monkeypatch):
    monkeypatch.delenv("STREETZIM_SEARCH_LN", raising=False)
    cfg = {"tileLanguages": ["de", "it"], "regionLanguages": ["de", "fr", "it"]}
    assert W.search_ln_languages(cfg) == ["de", "it", "fr"]
    assert W.search_ln_languages({"regionLanguages": ["de"]}) == []
    assert W.search_ln_languages(None) == []
    monkeypatch.setenv("STREETZIM_SEARCH_LN", "0")
    assert W.search_ln_languages(cfg) == []


# --- the record -----------------------------------------------------------

GENEVA_LN = dict(GENEVA_NAMES, fr="")    # Genève is the record's nn


def test_names_from_the_matched_object():
    assert W.localized_names(geneva(), GENEVA_REF, LANGS) == GENEVA_LN


def test_the_order_is_the_languages_order():
    assert list(W.localized_names(geneva(), GENEVA_REF, ["ru", "it", "de", "fr"])) == ["ru", "it", "de", "fr"]


def test_the_features_own_names_win():
    feat = dict(geneva(), names={"de": "Genf (Stadt)", "zh": "日内瓦"})
    assert W.localized_names(feat, GENEVA_REF, LANGS) == {
        "de": "Genf (Stadt)", "fr": "", "it": "Ginevra", "ru": "Женева", "zh": "日内瓦"}


def test_a_name_the_record_shows_is_left_out_but_its_native_name_is_kept():
    feat = {"name": "Zurich", "type": "place", "lat": 47.37, "lon": 8.54}
    ref = {"wikidata": "Q72", "names": {"de": "Zürich", "fr": "Zurich", "it": "Zurigo"}}
    # fr equals n exactly: dropped; de differs only by an accent: kept.
    assert W.localized_names(feat, ref, LANGS) == {"de": "Zürich", "it": "Zurigo"}
    # Genève equals nn: "" (the viewer shows nn), so a French visitor still
    # sees it, and the record does not repeat it.
    assert W.localized_names(geneva(), GENEVA_REF, ["fr"]) == {"fr": ""}


def test_only_for_the_types_a_visitor_looks_up_by_name():
    for t in ("place", "admin", "airport", "peak", "park", "water", "poi"):
        assert W.localized_names(dict(geneva(), type=t), GENEVA_REF, ["de"]) == {"de": "Genf"}, t
    for t in ("street", "addr", "building", "area", ""):
        assert W.localized_names(dict(geneva(), type=t), GENEVA_REF, ["de"]) == {}, t
    assert W.localized_names(dict(geneva(), type="street"), GENEVA_REF, ["de"],
                             frozenset({"street"})) == {"de": "Genf"}


def test_the_types_switch(monkeypatch):
    monkeypatch.setenv("STREETZIM_SEARCH_LN_TYPES", "place, admin")
    assert W._ln_types() == frozenset({"place", "admin"})
    monkeypatch.delenv("STREETZIM_SEARCH_LN_TYPES")
    assert W._ln_types() == W.LN_TYPES


def test_odd_names_are_left_out():
    ref = {"names": {"de": " Genf ", "fr": "", "it": "x" * 101, "ru": 7}}
    assert W.localized_names(geneva(), ref, LANGS) == {"de": "Genf"}


def test_nothing_without_languages_or_names():
    assert W.localized_names(geneva(), GENEVA_REF, []) == {}
    assert W.localized_names(geneva(), None, LANGS) == {}
    assert W.localized_names(geneva(), {"wikidata": "Q71"}, LANGS) == {}


def test_search_record_carries_ln_only_when_asked():
    assert "ln" not in W.search_record(geneva(), GENEVA_REF)
    rec = W.search_record(geneva(), GENEVA_REF, ["de", "it"])
    assert rec["ln"] == {"de": "Genf", "it": "Ginevra"} and rec["q"] == "Q71"
    # A record with no name in the languages has no key at all.
    assert "ln" not in W.search_record(geneva(), {"wikidata": "Q71"}, ["de"])


def test_record_names_include_ln_once():
    rec = {"n": "Geneva", "nn": "Genève",
           "ln": {"de": "Genf", "fr": "Genève", "it": "Ginevra", "rm": "Genf"}}
    assert record_names(rec) == ["Geneva", "Genève", "Genf", "Ginevra"]
    assert record_names({"n": "X", "ln": "Genf"}) == ["X"]
    assert record_names({"n": "X", "ln": {"de": 3, "fr": ""}}) == ["X"]


# --- through search pass 1 -------------------------------------------------

def _bucket(tmp_path, feats, refs, langs):
    path = tmp_path / "search.jsonl"
    path.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in feats),
                    encoding="utf-8")
    (tmp_path / "chunks").mkdir()
    return W._search_bucket(search_features_path=str(path), wikidata_data=None,
                            wiki_cross_refs=refs, loc_lookup=None, _bundled_set=set(),
                            chunk_tmp=str(tmp_path / "chunks"), ln_languages=langs)


def _chunk(tmp_path, prefix):
    p = tmp_path / "chunks" / f"{prefix}.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_indexed_under_its_names_in_other_languages(tmp_path):
    milan = {"name": "Milan", "name_native": "Milano", "type": "place", "subtype": "city",
             "lat": 45.4642, "lon": 9.19}
    mref = {"wikidata": "Q490", "names": {"de": "Mailand", "fr": "Milan", "ru": "Милан"}}
    refs = {("genève",) + GCELL: GENEVA_REF,
            ("milano", int(round(45.4642 * 1e4)), int(round(9.19 * 1e4))): mref}
    _bucket(tmp_path, [geneva(), milan], refs, LANGS)
    # "Mailand" puts Milan under "ma", "Ginevra" Geneva under "gi", "Женева"
    # under its first letter's key.
    assert [r["n"] for r in _chunk(tmp_path, "ma")] == ["Milan"]
    assert [r["n"] for r in _chunk(tmp_path, "gi")] == ["Geneva"]
    assert [r["n"] for r in _chunk(tmp_path, next(iter(prefixes_for("Женева"))))] == ["Geneva"]
    [g] = _chunk(tmp_path, "gi")
    assert g["ln"] == GENEVA_LN
    [m] = _chunk(tmp_path, "ma")
    assert m["ln"] == {"de": "Mailand", "ru": "Милан"}


def test_a_map_that_is_not_language_ready_is_as_before(tmp_path):
    refs = {("genève",) + GCELL: GENEVA_REF}
    _bucket(tmp_path, [geneva()], refs, None)
    assert not (tmp_path / "chunks" / "gi.jsonl").exists()
    [g] = _chunk(tmp_path, "ge")
    assert "ln" not in g and g["q"] == "Q71"


def test_an_admin_area_names_from_its_relation(tmp_path):
    area = {"name": "Geneva", "type": "admin", "subtype": "canton", "lat": 46.2, "lon": 6.15,
            "admin_level": 4, "osm": "r1702419", "names": {"de": "Genf", "it": "Ginevra"}}
    _bucket(tmp_path, [area], {}, LANGS)
    [a] = _chunk(tmp_path, "gi")
    assert a["t"] == "admin" and a["ln"] == {"de": "Genf", "it": "Ginevra"}


def test_streets_get_no_ln(tmp_path):
    street = {"name": "Rue du Rhône", "type": "street", "lat": GLAT, "lon": GLON,
              "names": {"de": "Rhonestrasse"}}
    _bucket(tmp_path, [street], {}, LANGS)
    assert not (tmp_path / "chunks" / "rh.jsonl").exists() or all(
        "ln" not in r for r in _chunk(tmp_path, "rh"))
    assert all("ln" not in r for r in _chunk(tmp_path, "ru"))


# --- the PBF side -----------------------------------------------------------

def _osm(tmp_path, body):
    p = tmp_path / "t.osm"
    p.write_text("<?xml version='1.0' encoding='UTF-8'?>\n<osm version='0.6' generator='t'>\n"
                 + body + "</osm>\n", encoding="utf-8")
    return str(p)


GENEVA_NODE = (
    f" <node id='1' version='1' lat='{GLAT}' lon='{GLON}'>\n"
    "  <tag k='place' v='city'/><tag k='name' v='Genève'/><tag k='name:de' v='Genf'/>\n"
    "  <tag k='name:it' v='Ginevra'/><tag k='name:ru' v='Женева'/>\n"
    "  <tag k='name:zh' v='日內瓦;日内瓦'/><tag k='name:zh-Hans' v='日内瓦'/>\n"
    "  <tag k='wikipedia' v='fr:Genève'/><tag k='wikidata' v='Q71'/>\n"
    " </node>\n")


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_the_lookup_keeps_the_names_asked_for(tmp_path):
    from streetzim.addresses import extract_wiki_tags_pbf
    path = _osm(tmp_path, GENEVA_NODE)
    plain = extract_wiki_tags_pbf(path, precut=True)
    assert plain == {("genève",) + GCELL: {"wikipedia": "fr:Genève", "wikidata": "Q71"}}
    named = extract_wiki_tags_pbf(path, precut=True, languages=["de", "fr", "zh", "ru"])
    assert named == {("genève",) + GCELL: {"wikipedia": "fr:Genève", "wikidata": "Q71",
                                           "names": {"de": "Genf", "zh": "日内瓦", "ru": "Женева"}}}
    # The tile feature (search name Geneva, own name Genève) finds them.
    wiki = W.feature_wiki(geneva(), named)
    assert W.localized_names(geneva(), wiki, ["de", "it"]) == {"de": "Genf"}


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_names_never_change_which_object_wins(tmp_path):
    """A node with only a wikidata tag but names, then a way of the same
    name and cell with both wiki tags: the way wins as it always did, and
    takes the node's names, which it has none of."""
    from streetzim.addresses import extract_wiki_tags_pbf
    body = (f" <node id='1' version='1' lat='{GLAT}' lon='{GLON}'>"
            "<tag k='name' v='Genève'/><tag k='name:de' v='Genf'/><tag k='wikidata' v='Q71'/></node>\n"
            f" <node id='2' version='1' lat='{GLAT - 0.00001}' lon='{GLON - 0.00001}'/>\n"
            f" <node id='3' version='1' lat='{GLAT + 0.00001}' lon='{GLON + 0.00001}'/>\n"
            " <way id='10' version='1'><nd ref='2'/><nd ref='1'/><nd ref='3'/>"
            "<tag k='name' v='Genève'/><tag k='wikipedia' v='fr:Genève'/>"
            "<tag k='wikidata' v='Q71'/></way>\n")
    path = _osm(tmp_path, body)
    plain = extract_wiki_tags_pbf(path, precut=True)
    named = extract_wiki_tags_pbf(path, precut=True, languages=["de"])
    [k] = plain
    assert plain[k] == {"wikipedia": "fr:Genève", "wikidata": "Q71"}
    assert named[k] == {"wikipedia": "fr:Genève", "wikidata": "Q71", "names": {"de": "Genf"}}


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_an_admin_relation_carries_its_names(tmp_path):
    pytest.importorskip("osmium")
    from streetzim import admin_areas as A
    sys.path.insert(0, str(ROOT / "tests"))
    from test_admin_areas import Osm, square
    o = Osm()
    o.way(1, square(6.0, 46.1, 6.3, 46.4))
    o.rel(1, [("w", 1, "outer")], {"name": "Genève", "admin_level": "4",
                                   "name:de": "Genf", "name:it": "Ginevra",
                                   "ISO3166-2": "CH-GE"})
    path = o.write(tmp_path / "ge.osm")
    [plain] = A.extract_admin_areas(path, geonames=False)
    assert "names" not in plain
    [named] = A.extract_admin_areas(path, geonames=False, languages=["de", "it", "ru"])
    assert named["lnames"] == {"de": "Genf", "it": "Ginevra"} and "names" not in named
    rec = W.search_record(named, None, ["de", "it"])
    assert rec["ln"] == {"de": "Genf", "it": "Ginevra"}


# --- what the survivors of the first mutation run showed --------------------

@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_a_losing_object_gives_its_names_to_the_winner(tmp_path):
    """A node with both wiki tags first (osmium exports nodes before ways),
    then a way of the same name and cell with one tag but names: the node
    stays, and gains the way's names."""
    from streetzim.addresses import extract_wiki_tags_pbf
    body = (f" <node id='2' version='1' lat='{GLAT - 0.00001}' lon='{GLON - 0.00001}'/>\n"
            f" <node id='3' version='1' lat='{GLAT}' lon='{GLON}'/>\n"
            f" <node id='4' version='1' lat='{GLAT + 0.00001}' lon='{GLON + 0.00001}'/>\n"
            f" <node id='5' version='1' lat='{GLAT}' lon='{GLON}'>"
            "<tag k='name' v='Genève'/><tag k='wikipedia' v='fr:Genève'/>"
            "<tag k='wikidata' v='Q71'/></node>\n"
            " <way id='10' version='1'><nd ref='2'/><nd ref='3'/><nd ref='4'/>"
            "<tag k='name' v='Genève'/><tag k='name:it' v='Ginevra'/>"
            "<tag k='wikidata' v='Q71'/></way>\n")
    path = _osm(tmp_path, body)
    named = extract_wiki_tags_pbf(path, precut=True, languages=["it"])
    [v] = named.values()
    assert v == {"wikipedia": "fr:Genève", "wikidata": "Q71", "names": {"it": "Ginevra"}}


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_appended_admin_areas_carry_their_names(tmp_path):
    pytest.importorskip("osmium")
    from streetzim import admin_areas as A
    sys.path.insert(0, str(ROOT / "tests"))
    from test_admin_areas import Osm, square
    o = Osm()
    o.way(1, square(6.0, 46.1, 6.3, 46.4))
    o.rel(1, [("w", 1, "outer")], {"name": "Genève", "admin_level": "4", "name:de": "Genf"})
    pbf = o.write(tmp_path / "ge.osm")
    jsonl = tmp_path / "search.jsonl"
    assert A.append_admin_areas(pbf, str(jsonl), languages=["de"]) == 1
    [area] = [json.loads(x) for x in jsonl.read_text(encoding="utf-8").splitlines()]
    assert area["lnames"] == {"de": "Genf"}


class _Item:
    def __init__(self, path, title, mime, content, is_front=False):
        self.path, self.content = path, content


class _Creator:
    def __init__(self):
        self.items = {}

    def add_item(self, item):
        self.items[item.path] = item.content

    def add_redirection(self, *a):
        pass


def test_the_in_memory_path_names_places_too(monkeypatch):
    monkeypatch.delenv("STREETZIM_SEARCH_LN", raising=False)
    c = _Creator()
    W._add_search_in_memory(c, _Item, search_features=[dict(geneva(), names={"it": "Ginevra"})],
                            loc_lookup=None, ln_languages=["it"])
    [g] = json.loads(c.items["search-data/gi.json"])
    assert g["ln"] == {"it": "Ginevra"}


def test_the_build_takes_the_languages_from_map_config(tmp_path, monkeypatch):
    """_add_search hands search pass 1 the map's search languages."""
    monkeypatch.delenv("STREETZIM_SEARCH_LN", raising=False)
    path = tmp_path / "search.jsonl"
    path.write_text(json.dumps(geneva(), ensure_ascii=False) + "\n", encoding="utf-8")
    got = {}
    real = W._search_bucket

    def spy(**kw):
        got["ln"] = kw.get("ln_languages")
        raise RuntimeError("stop")
    monkeypatch.setattr(W, "_search_bucket", spy)
    for cfg, want in (({"tileLanguages": ["de"], "regionLanguages": ["de", "rm"]}, ["de", "rm"]),
                      ({}, [])):
        with pytest.raises(RuntimeError):
            W._add_search(_Creator(), _Item, mbtiles_path=None, search_features_path=str(path),
                          search_features=None, wikidata_data=None, wiki_cross_refs={},
                          _bundled_set=set(), split_hot_search_chunks_mb=0, split_find_chips=False,
                          no_llm_bundle=True, map_config=cfg, name="T", bbox=None,
                          routing_graph_path=None, address_count=0, overture_sources=None,
                          overture_themes=None, xapian_mode="libzim", chunk_tmp=str(tmp_path))
        assert got["ln"] == want
    assert real is not None


# --- the names lookup: every OSM object with names in the languages --------

VILLAGE = {"name": "Ilanz", "type": "place", "subtype": "town", "lat": 46.7739, "lon": 9.2051}
VCELL = (int(round(46.7739 * 1e4)), int(round(9.2051 * 1e4)))


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_the_names_lookup_reads_every_object_with_names(tmp_path):
    from streetzim.addresses import extract_name_tags_pbf
    body = (
        # a town with no wiki tag
        " <node id='1' version='1' lat='46.7739' lon='9.2051'><tag k='place' v='town'/>"
        "<tag k='name' v='Ilanz/Glion'/><tag k='name:rm' v='Glion'/><tag k='name:de' v='Ilanz'/></node>\n"
        " <node id='2' version='1' lat='46.70' lon='9.20'/><node id='3' version='1' lat='46.71' lon='9.21'/>\n"
        " <node id='4' version='1' lat='46.80' lon='9.30'/><node id='5' version='1' lat='46.80' lon='9.32'/>\n"
        " <node id='6' version='1' lat='46.82' lon='9.32'/><node id='7' version='1' lat='46.82' lon='9.30'/>\n"
        # names only in a language not asked for
        " <node id='8' version='1' lat='46.75' lon='9.25'><tag k='name' v='X'/><tag k='name:sv' v='Y'/></node>\n"
        # only name:zh-Hans: zh
        " <node id='9' version='1' lat='46.76' lon='9.26'><tag k='name' v='Z'/><tag k='name:zh-Hans' v='兹'/></node>\n"
        # a road: left out
        " <way id='10' version='1'><nd ref='2'/><nd ref='3'/><tag k='highway' v='primary'/>"
        "<tag k='name' v='Via Principala'/><tag k='name:de' v='Hauptstrasse'/></way>\n"
        # a lake: its outer ring's mean
        " <way id='11' version='1'><nd ref='4'/><nd ref='5'/><nd ref='6'/><nd ref='7'/><nd ref='4'/>"
        "<tag k='natural' v='water'/><tag k='name' v='Lag Grond'/><tag k='name:de' v='Grosser See'/>"
        "<tag k='name:zh' v='大湖;大湖泊'/><tag k='name:zh-Hans' v='大湖'/></way>\n")
    path = _osm(tmp_path, body)
    refs = extract_name_tags_pbf(path, ["de", "rm", "zh"], precut=True)
    assert refs[("ilanz/glion",) + VCELL] == {"names": {"de": "Ilanz", "rm": "Glion"}}
    assert not any(k[0] == "via principala" for k in refs)
    # osmium exports a closed way as a line and as an area: both keys.
    lake = {k[1:]: v for k, v in refs.items() if k[0] == "lag grond"}
    assert lake[(int(round(46.808 * 1e4)), int(round(9.308 * 1e4)))] == {
        "names": {"de": "Grosser See", "zh": "大湖"}}
    assert all(v == {"names": {"de": "Grosser See", "zh": "大湖"}} for v in lake.values())
    assert not any(k[0] == "x" for k in refs)
    assert [v for k, v in refs.items() if k[0] == "z"] == [{"names": {"zh": "兹"}}]
    assert extract_name_tags_pbf(path, [], precut=True) == {}


def test_a_village_gets_names_from_the_lookup(tmp_path):
    refs = {("ilanz/glion",) + VCELL: {"names": {"de": "Ilanz", "rm": "Glion"}}}
    feat = dict(VILLAGE, name_native="Ilanz/Glion")
    b = _search_bucket_with(tmp_path, [feat], {}, ["de", "rm"], refs)
    [r] = _chunk(tmp_path, "gl")
    assert r["ln"] == {"rm": "Glion"} and b.total_features == 1   # de "Ilanz" is n


def test_the_wiki_objects_names_come_before_the_lookups(tmp_path):
    feat = dict(geneva())
    wiki = {("genève",) + GCELL: {"wikidata": "Q71", "names": {"de": "Genf"}}}
    names = {("genève",) + GCELL: {"names": {"de": "Genf (Ort)", "it": "Ginevra"}}}
    _search_bucket_with(tmp_path, [feat], wiki, ["de", "it"], names)
    [g] = _chunk(tmp_path, "gi")
    assert g["ln"] == {"de": "Genf", "it": "Ginevra"}


def test_the_lookup_is_ignored_without_languages_and_for_streets(tmp_path):
    refs = {("ilanz/glion",) + VCELL: {"names": {"rm": "Glion"}},
            ("via principala",) + VCELL: {"names": {"de": "Hauptstrasse"}}}
    street = dict(VILLAGE, name="Via Principala", type="street")
    _search_bucket_with(tmp_path, [dict(VILLAGE, name_native="Ilanz/Glion"), street], {}, None, refs)
    assert all("ln" not in r for p in ("il", "vi", "pr") for r in _chunk(tmp_path, p))


def test_two_objects_folding_to_one_key_with_other_names_give_none():
    refs = {("zürich", 473769, 85417): {"names": {"rm": "Turitg"}},
            ("zurich", 473769, 85417): {"names": {"rm": "Zürich"}}}
    folded = W.wiki_folded_index(refs, W.names_link)
    feat = {"name": "Zuerich", "type": "place", "lat": 47.3769, "lon": 8.5417}
    assert folded[("zurich", 473769, 85417)] is None
    assert W.feature_wiki(dict(feat, name="Zurích"), refs, folded,
                          link=W.names_link) is None
    # Without the names link the two would agree (no wiki tags): a match.
    assert W.feature_wiki(dict(feat, name="Zurích"), refs, W.wiki_folded_index(refs)) is not None
    same = {("zürich", 473769, 85417): {"names": {"rm": "Turitg"}},
            ("zurich", 473769, 85417): {"names": {"rm": "Turitg"}}}
    assert W.wiki_folded_index(same, W.names_link)[("zurich", 473769, 85417)] == {"names": {"rm": "Turitg"}}


def _search_bucket_with(tmp_path, feats, wiki, langs, names):
    path = tmp_path / "search.jsonl"
    path.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in feats),
                    encoding="utf-8")
    (tmp_path / "chunks").mkdir()
    return W._search_bucket(search_features_path=str(path), wikidata_data=None,
                            wiki_cross_refs=wiki, loc_lookup=None, _bundled_set=set(),
                            chunk_tmp=str(tmp_path / "chunks"), ln_languages=langs,
                            ln_name_refs=names)


def test_add_search_hands_the_lookup_on(tmp_path, monkeypatch):
    path = tmp_path / "search.jsonl"
    path.write_text(json.dumps(geneva(), ensure_ascii=False) + "\n", encoding="utf-8")
    got = {}

    def spy(**kw):
        got.update(kw)
        raise RuntimeError("stop")
    monkeypatch.setattr(W, "_search_bucket", spy)
    refs = {("x", 1, 2): {"names": {"de": "y"}}}
    with pytest.raises(RuntimeError):
        W._add_search(_Creator(), _Item, mbtiles_path=None, search_features_path=str(path),
                      search_features=None, wikidata_data=None, wiki_cross_refs={},
                      _bundled_set=set(), split_hot_search_chunks_mb=0, split_find_chips=False,
                      no_llm_bundle=True, map_config={"tileLanguages": ["de"]}, name="T", bbox=None,
                      routing_graph_path=None, address_count=0, overture_sources=None,
                      overture_themes=None, xapian_mode="libzim", chunk_tmp=str(tmp_path),
                      ln_name_refs=refs)
    assert got["ln_name_refs"] is refs


def test_a_zim_names_a_village_in_romansh(tmp_path, monkeypatch):
    """create_zim end to end: a language-ready map-config (rm among the tile
    languages) and the names lookup give the published record its ln."""
    reader = pytest.importorskip("libzim.reader")
    pytest.importorskip("libzim.writer")
    monkeypatch.delenv("STREETZIM_SEARCH_LN", raising=False)
    monkeypatch.delenv("STREETZIM_SEARCH_LN_LANGUAGES", raising=False)
    source = tmp_path / "features.jsonl"
    source.write_text(json.dumps(dict(VILLAGE, name_native="Ilanz/Glion")) + "\n", encoding="utf-8")
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    target = tmp_path / "ilanz.zim"
    W.create_zim(
        str(target), tiles={}, tile_metadata={}, fonts={},
        maplibre_js_path=str(tmp_path / "ml.js"), maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "Ilanz", "tileLanguages": ["de", "rm"], "regionLanguages": ["de", "rm"]},
        name="OSM - Ilanz", bbox=(9.1, 46.7, 9.3, 46.8), search_features_path=str(source),
        ln_name_refs={("ilanz/glion",) + VCELL: {"names": {"de": "Ilanz", "rm": "Glion"}}},
        xapian_mode="none", zim_workers=1)
    archive = reader.Archive(str(target))
    manifest = json.loads(bytes(archive.get_entry_by_path("search-data/manifest.json").get_item().content))
    assert "gl" in manifest["chunks"]
    [rec] = json.loads(bytes(archive.get_entry_by_path("search-data/gl.json").get_item().content))
    assert rec["n"] == "Ilanz" and rec["ln"] == {"rm": "Glion"}


def test_write_zim_hands_create_zim_the_lookup(tmp_path, monkeypatch):
    import create_osm_zim as c
    seen = {}
    monkeypatch.setattr(c, "create_zim", lambda **kw: seen.update(kw))
    args = c.build_parser().parse_args(["--bbox", "9.1,46.7,9.3,46.8"])
    args._ln_name_refs = {("x", 1, 2): {"names": {"rm": "y"}}}
    c._write_zim(
        address_count=0, args=args, bbox_str="9.1,46.7,9.3,46.8", fonts={},
        map_config={}, maplibre_css="c", maplibre_js="j", mbtiles_path=None, name="M",
        output_path=str(tmp_path / "t.zim"), overture_sources=None, overture_themes=None,
        routing_graph_path=None, satellite_dir=None, satellite_format=None,
        satellite_max_zoom=0, search_features=[], terrain_dir=None, terrain_max_zoom=0,
        tile_metadata={}, tiles={}, tmpdir=str(tmp_path), total_tile_count=0,
        use_streaming=False, wiki_cross_refs=None, wikidata_data=None,
        zim_illustration=None, zim_metadata=None)
    assert seen["ln_name_refs"] is args._ln_name_refs
    del args._ln_name_refs
    c._write_zim(
        address_count=0, args=args, bbox_str="9.1,46.7,9.3,46.8", fonts={},
        map_config={}, maplibre_css="c", maplibre_js="j", mbtiles_path=None, name="M",
        output_path=str(tmp_path / "t.zim"), overture_sources=None, overture_themes=None,
        routing_graph_path=None, satellite_dir=None, satellite_format=None,
        satellite_max_zoom=0, search_features=[], terrain_dir=None, terrain_max_zoom=0,
        tile_metadata={}, tiles={}, tmpdir=str(tmp_path), total_tile_count=0,
        use_streaming=False, wiki_cross_refs=None, wikidata_data=None,
        zim_illustration=None, zim_metadata=None)
    assert seen["ln_name_refs"] is None


def test_a_folding_collision_in_the_lookup_gives_the_record_none(tmp_path):
    refs = {("zürich", 473769, 85417): {"names": {"rm": "Turitg"}},
            ("zurich", 473769, 85417): {"names": {"rm": "Zuric"}}}
    feat = {"name": "Zurich", "type": "place", "lat": 47.3769, "lon": 8.5417}
    _search_bucket_with(tmp_path, [feat], {}, ["rm"], refs)
    [r] = _chunk(tmp_path, "zu")
    assert r["ln"] == {"rm": "Zuric"}        # the exact name first: "zurich"
    feat2 = {"name": "Zurích", "type": "place", "lat": 47.3769, "lon": 8.5417}
    (tmp_path / "chunks").rename(tmp_path / "chunks-1")
    _search_bucket_with(tmp_path, [feat2], {}, ["rm"], refs)
    assert all("ln" not in r for r in _chunk(tmp_path, "zu"))


def test_point_of_each_geometry():
    from streetzim.addresses import _point_of
    assert _point_of({"type": "Point", "coordinates": [9.2, 46.7]}) == (46.7, 9.2)
    ring = [[9.0, 46.0], [9.2, 46.0], [9.2, 46.2], [9.0, 46.0]]
    lat, lon = _point_of({"type": "Polygon", "coordinates": [ring]})
    assert (round(lat, 3), round(lon, 3)) == (46.05, 9.1)
    big = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
    assert _point_of({"type": "MultiPolygon", "coordinates": [[ring], [big]]}) == (1.6, 1.6)
    assert _point_of({"type": "LineString", "coordinates": [[1, 2], [3, 4], [5, 6]]}) == (4, 3)
    assert _point_of({"type": "GeometryCollection"}) is None


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_one_key_goes_to_the_first_object_a_node_before_a_building(tmp_path):
    from streetzim.addresses import extract_name_tags_pbf
    body = (" <node id='1' version='1' lat='46.7739' lon='9.2051'><tag k='name' v='Glion'/>"
            "<tag k='name:de' v='Ilanz'/></node>\n"
            " <node id='2' version='1' lat='46.7739' lon='9.2051'><tag k='name' v='Glion'/>"
            "<tag k='name:de' v='Ilanz'/><tag k='name:it' v='Ilanz'/><tag k='name:ru' v='Иланц'/></node>\n"
            " <node id='3' version='1' lat='46.77389' lon='9.20509'/>\n"
            " <node id='4' version='1' lat='46.77391' lon='9.20509'/>\n"
            " <node id='5' version='1' lat='46.77391' lon='9.20511'/>\n"
            " <way id='10' version='1'><nd ref='3'/><nd ref='4'/><nd ref='5'/><nd ref='3'/>"
            "<tag k='building' v='yes'/><tag k='name' v='Glion'/><tag k='name:fr' v='Bâtiment'/></way>\n")
    refs = extract_name_tags_pbf(_osm(tmp_path, body), ["de", "fr", "it", "ru"], precut=True)
    assert refs[("glion",) + VCELL] == {"names": {"de": "Ilanz"}}


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_a_bus_stop_node_keeps_its_names(tmp_path):
    """Roads are left out (ways tagged highway), a stop is a node."""
    from streetzim.addresses import extract_name_tags_pbf
    body = (" <node id='1' version='1' lat='46.7739' lon='9.2051'><tag k='highway' v='bus_stop'/>"
            "<tag k='name' v='Glion, staziun'/><tag k='name:de' v='Ilanz, Bahnhof'/></node>\n")
    refs = extract_name_tags_pbf(_osm(tmp_path, body), ["de"], precut=True)
    assert refs == {("glion, staziun",) + VCELL: {"names": {"de": "Ilanz, Bahnhof"}}}


def test_a_record_whose_names_fold_to_two_objects_gets_none(tmp_path):
    """Its name and own-script name fold to two keys whose objects carry
    different names: no ln (as two articles give no w/q)."""
    refs = {("zurich", 473769, 85417): {"names": {"rm": "Turitg"}},
            ("turigg", 473769, 85417): {"names": {"rm": "Turig"}}}
    feat = {"name": "Zurích", "name_native": "Turígg", "type": "place", "lat": 47.3769, "lon": 8.5417}
    _search_bucket_with(tmp_path, [feat], {}, ["rm"], refs)
    assert all("ln" not in r for p in ("zu", "tu") for r in _chunk(tmp_path, p))


def test_the_wrapper_takes_another_search_cache():
    """STREETZIM_SEARCH_CACHE replaces build-region-fast.sh's cache path
    (a cache extracted from the region's multilingual tiles)."""
    import subprocess
    text = (ROOT / "ops" / "build-region-fast.sh").read_text(encoding="utf-8")
    [line] = [ln for ln in text.splitlines() if ln.startswith("SEARCH=")]
    run = lambda env: subprocess.run(["bash", "-c", f'SRC_ID=switzerland; {line}; printf %s "$SEARCH"'],
                                     env=env, capture_output=True, text=True).stdout
    assert run({"PATH": "/usr/bin:/bin"}) == "/storage/streetzim/world-data/regions/switzerland.search.jsonl"
    assert run({"PATH": "/usr/bin:/bin", "STREETZIM_SEARCH_CACHE": "/x/ml.search.jsonl"}) == "/x/ml.search.jsonl"


# --- the review of 3ac3688..72274ae -----------------------------------------

def test_an_admin_areas_names_do_not_rename_it_in_another_language():
    """Admin names are `lnames`, which localize_feature (a build in another
    language) leaves alone: Moscow Oblast keeps its name and gets no
    English "native" name; its record still carries ln."""
    from streetzim.search_extract import localize_feature
    area = {"name": "Moscow Oblast", "type": "admin", "subtype": "oblast", "lat": 55.5, "lon": 37.5,
            "admin_level": 4, "osm": "r51490", "lnames": {"ru": "Московская область", "de": "Oblast Moskau"}}
    out = localize_feature(dict(area), "ru")
    assert out.get("display_name", "Moscow Oblast") == "Moscow Oblast"
    assert out.get("display_native") in (None, "Moscow Oblast")
    assert W.search_record(area, None, ["de", "ru"])["ln"] == {"de": "Oblast Moskau", "ru": "Московская область"}


def test_in_another_languages_build_ln_leaves_out_the_name_shown():
    """A de build shows Geneva as "Genf" (display_name): ln de, equal to
    what the record shows, is left out; the raw name does not count."""
    feat = dict(geneva(), display_name="Genf", display_native="Genève", display_latin="Geneva")
    assert W.localized_names(feat, GENEVA_REF, ["de", "it"]) == {"it": "Ginevra"}


def test_the_types_switch_reaches_the_streaming_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_SEARCH_LN_TYPES", "place")
    poi = {"name": "Musée d'ethnographie", "type": "poi", "subtype": "museum", "lat": GLAT, "lon": GLON,
           "names": {"de": "Ethnographisches Museum"}}
    _bucket(tmp_path, [geneva() | {"names": {"de": "Genf"}}, poi], {}, ["de"])
    assert [r.get("ln") for r in _chunk(tmp_path, "ge")] == [{"de": "Genf"}]
    assert all("ln" not in r for r in _chunk(tmp_path, "mu"))


def test_an_empty_types_switch_means_none(monkeypatch):
    monkeypatch.setenv("STREETZIM_SEARCH_LN_TYPES", "")
    assert W._ln_types() == frozenset()
    assert W.localized_names(geneva(), GENEVA_REF, ["de"], W._ln_types()) == {}


def test_a_name_of_exactly_the_limit_is_kept():
    ok = "x" * W.LN_MAX_CHARS
    assert W.localized_names(geneva(), {"names": {"de": ok, "it": ok + "y"}}, ["de", "it"]) == {"de": ok}


def test_an_admin_record_takes_no_names_from_the_lookup(tmp_path):
    area = {"name": "Geneva", "type": "admin", "subtype": "canton", "lat": GLAT, "lon": GLON,
            "admin_level": 4, "osm": "r1702419", "lnames": {"de": "Genf"}}
    lookup = {("geneva",) + GCELL: {"names": {"de": "Genf (Ort)", "it": "Ginevra"}}}
    _search_bucket_with(tmp_path, [area], {}, ["de", "it"], lookup)
    [a] = _chunk(tmp_path, "ge")
    assert a["ln"] == {"de": "Genf"}


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_wiki_objects_tied_on_tags_keep_the_first(tmp_path):
    from streetzim.addresses import extract_wiki_tags_pbf
    body = (f" <node id='1' version='1' lat='{GLAT}' lon='{GLON}'><tag k='name' v='Genève'/>"
            "<tag k='wikidata' v='Q71'/></node>\n"
            f" <node id='2' version='1' lat='{GLAT}' lon='{GLON}'><tag k='name' v='Genève'/>"
            "<tag k='wikidata' v='Q11917'/><tag k='name:de' v='Kanton Genf'/></node>\n")
    refs = extract_wiki_tags_pbf(_osm(tmp_path, body), precut=True, languages=["de"])
    [v] = refs.values()
    # The first stays, and the other item's names are not its.
    assert v == {"wikidata": "Q71"}


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_the_winner_keeps_its_own_names_and_never_another_items(tmp_path):
    from streetzim.addresses import extract_wiki_tags_pbf
    body = (f" <node id='1' version='1' lat='{GLAT}' lon='{GLON}'><tag k='name' v='Genève'/>"
            "<tag k='wikidata' v='Q71'/><tag k='name:de' v='Genf (Knoten)'/></node>\n"
            f" <node id='2' version='1' lat='{GLAT - 0.00001}' lon='{GLON - 0.00001}'/>\n"
            f" <node id='3' version='1' lat='{GLAT + 0.00001}' lon='{GLON + 0.00001}'/>\n"
            f" <node id='4' version='1' lat='{GLAT}' lon='{GLON}'><tag k='name' v='Genève'/>"
            "<tag k='wikidata' v='Q999'/><tag k='name:it' v='Altro'/></node>\n"
            " <way id='10' version='1'><nd ref='2'/><nd ref='1'/><nd ref='3'/>"
            "<tag k='name' v='Genève'/><tag k='wikipedia' v='fr:Genève'/><tag k='wikidata' v='Q71'/>"
            "<tag k='name:de' v='Genf'/></way>\n")
    refs = extract_wiki_tags_pbf(_osm(tmp_path, body), precut=True, languages=["de", "it"])
    [v] = refs.values()
    # The way (both tags) wins with its own names, not the node's.
    assert v == {"wikipedia": "fr:Genève", "wikidata": "Q71", "names": {"de": "Genf"}}


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_names_of_another_item_are_never_adopted(tmp_path):
    from streetzim.addresses import extract_wiki_tags_pbf
    body = (f" <node id='1' version='1' lat='{GLAT}' lon='{GLON}'><tag k='name' v='Genève'/>"
            "<tag k='wikidata' v='Q11917'/><tag k='name:de' v='Kanton Genf'/></node>\n"
            f" <node id='2' version='1' lat='{GLAT - 0.00001}' lon='{GLON - 0.00001}'/>\n"
            f" <node id='3' version='1' lat='{GLAT + 0.00001}' lon='{GLON + 0.00001}'/>\n"
            " <way id='10' version='1'><nd ref='2'/><nd ref='1'/><nd ref='3'/>"
            "<tag k='name' v='Genève'/><tag k='wikipedia' v='fr:Genève'/><tag k='wikidata' v='Q71'/></way>\n")
    refs = extract_wiki_tags_pbf(_osm(tmp_path, body), precut=True, languages=["de"])
    [v] = refs.values()
    assert v == {"wikipedia": "fr:Genève", "wikidata": "Q71"}
    # ... while a loser of the same item gives them (L1 only narrows).
    body2 = body.replace("Q11917", "Q71")
    (tmp_path / "t.osm").unlink()
    [v2] = extract_wiki_tags_pbf(_osm(tmp_path, body2), precut=True, languages=["de"]).values()
    assert v2["names"] == {"de": "Kanton Genf"}


@pytest.mark.skipif(not shutil.which("osmium"), reason="osmium CLI not on PATH")
def test_boundary_relations_stay_out_of_the_names_lookup(tmp_path):
    """Roads and boundary relations are dropped before the export (their
    geometries were most of it); admin areas carry their own names."""
    pytest.importorskip("osmium")
    sys.path.insert(0, str(ROOT / "tests"))
    from test_admin_areas import Osm, square
    from streetzim.addresses import extract_name_tags_pbf
    o = Osm()
    o.way(1, square(9.0, 46.6, 9.4, 46.9))
    o.rel(1, [("w", 1, "outer")], {"name": "Surselva", "admin_level": "6", "name:de": "Surselva (Bezirk)"})
    o.node(9.2051, 46.7739, {"name": "Glion", "place": "town", "name:de": "Ilanz"}, nid=50)
    refs = extract_name_tags_pbf(o.write(tmp_path / "b.osm"), ["de"], precut=True)
    assert [k[0] for k in refs] == ["glion"]
