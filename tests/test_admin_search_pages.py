"""Administrative areas in a written ZIM (docs/search-records.md, `admin`):
their search-data records, their Kiwix pages (title suggestions, full-text,
"View on map" fitted to the area), on the streaming and in-memory paths."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import zim_writer as W  # noqa: E402

ALEXANDRIA = {"name": "Alexandria", "type": "admin", "subtype": "city",
              "lat": 38.80484, "lon": -77.04692, "location": "Virginia",
              "admin_level": 6, "osm": "r206637", "wikidata": "Q88",
              "wikipedia": "en:Alexandria, Virginia"}
DC = {"name": "District of Columbia", "type": "admin", "subtype": "district",
      "lat": 38.8951, "lon": -77.03638, "location": "United States",
      "admin_level": 4, "osm": "r162069", "alt": ["D.C.", "The District"],
      "bbox": [-77.11979, 38.79163, -76.90937, 38.99597], "wikidata": "Q3551781"}
SHOP = {"name": "Alexandria", "type": "poi", "subtype": "shop",
        "lat": 38.9, "lon": -77.0}


def test_record_fields():
    assert W.admin_record_fields(SHOP) == {}
    assert W.admin_record_fields(ALEXANDRIA) == {"al": 6, "osm": "r206637"}
    assert W.admin_record_fields(DC) == {
        "al": 4, "bb": DC["bbox"], "alt": ["D.C.", "The District"], "osm": "r162069"}


def test_page_title_names_the_type_once():
    assert W.kiwix_page_title(ALEXANDRIA) == "Alexandria (city)"
    assert W.kiwix_page_title(DC) == "District of Columbia"
    assert W.kiwix_page_title(dict(ALEXANDRIA, name="Arlington County",
                                   subtype="county")) == "Arlington County"
    assert W.kiwix_page_title(SHOP) == "Alexandria"


def test_alt_titles():
    assert W.kiwix_alt_titles(SHOP) == []
    assert W.kiwix_alt_titles(ALEXANDRIA) == ["City of Alexandria"]
    assert W.kiwix_alt_titles(DC) == ["D.C.", "The District"]      # names the type
    assert W.kiwix_alt_titles(dict(ALEXANDRIA, subtype="quarter")) == []
    assert W.kiwix_alt_titles(dict(ALEXANDRIA, alt=["Alex", "City of Alexandria"])) == [
        "City of Alexandria", "Alex"]
    # "<Type> of <Name>" first, so the cut to 6 never drops it.
    many = dict(ALEXANDRIA, alt=[f"A{i}" for i in range(9)])
    assert W.kiwix_alt_titles(many)[0] == "City of Alexandria"
    assert len(W.kiwix_alt_titles(many)) == 6


@pytest.mark.parametrize("name,label,title", [
    ("Georgetown", "town", "Georgetown (town)"),
    ("Statesboro", "state", "Statesboro (state)"),
    ("Arlington County", "county", "Arlington County"),
    ("Town of Colmar Manor", "town", "Town of Colmar Manor"),
])
def test_type_in_the_name_is_a_whole_word(name, label, title):
    feat = dict(ALEXANDRIA, name=name, subtype=label)
    assert W.kiwix_page_title(feat) == title
    formal = f"{label.title()} of {name}"
    assert (formal in W.kiwix_alt_titles(feat)) == (title != name)


def test_geonames_credit_on_the_page():
    _, _, html = W.search_page(dict(ALEXANDRIA, geonames=True), 0)
    assert '<p class="credit">' in html and "GeoNames (geonames.org), CC BY 4.0" in html
    _, _, html = W.search_page(DC, 0)
    assert 'class="credit"' not in html.split("</style>")[1]


def test_page_hash():
    h = W.kiwix_page_hash(DC)
    assert h.startswith("map=10.8/38.89380/-77.01458&bounds=-77.11979,38.79163,"
                        "-76.90937,38.99597&pin=38.8951,-77.03638&label=District%20of%20Columbia")
    # No box (clipped by the extract): the point, at a zoom for the level.
    assert W.kiwix_page_hash(ALEXANDRIA) == (
        "map=10/38.80484/-77.04692&pin=38.80484,-77.04692&label=Alexandria")
    assert W.kiwix_page_hash(SHOP) == "map=17/38.9/-77.0"


def test_page_html():
    path, title, html = W.search_page(DC, 3)
    assert path == "search/district-of-columbia-3.html" and title == "District of Columbia"
    assert "<title>District of Columbia</title>" in html
    assert '<p class="kind">District in United States</p>' in html
    assert '<body data-type="admin">' in html
    assert '<p class="also">Also: D.C., The District</p>' in html
    assert "&bounds=-77.11979" in html
    _, title, html = W.search_page(ALEXANDRIA, 0)
    assert title == "Alexandria (city)" and "<title>Alexandria (city)</title>" in html
    assert "<h1>Alexandria</h1>" in html and "City in Virginia" in html
    # Other types are as before.
    _, title, html = W.search_page(dict(SHOP, type="park", subtype="park"), 1)
    assert title == "Alexandria" and '<p class="kind">Park</p>' in html
    assert "map=15/38.9/-77.0" in html and "also" not in html.split("</style>")[1]


def _suggest(archive, text):
    from libzim.suggestion import SuggestionSearcher
    s = SuggestionSearcher(archive).suggest(text)
    return [archive.get_entry_by_path(p).title
            for p in s.getResults(0, s.getEstimatedMatches())]


@pytest.mark.parametrize("source", ["path", "in-memory"])
def test_written_zim(tmp_path, source):
    pytest.importorskip("libzim.writer")
    mvt = pytest.importorskip("mapbox_vector_tile")
    from libzim.reader import Archive
    from libzim.search import Query, Searcher
    tile = gzip.compress(mvt.encode([{"name": "poi", "features": [
        {"geometry": "POINT(10 10)", "properties": {"name": "X"}}]}]))
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    feats = [ALEXANDRIA, DC, SHOP]
    kw = {}
    if source == "path":
        path = tmp_path / "features.jsonl"
        path.write_text("".join(json.dumps(f) + "\n" for f in feats))
        kw["search_features_path"] = str(path)
    else:
        kw["search_features"] = [dict(f) for f in feats]
    work = tmp_path / "work"
    work.mkdir()
    W.create_zim(
        tmp_path / "t.zim", tiles={(14, 4580, 6264): tile}, tile_metadata={},
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "DC"}, name="OSM - DC",
        bbox=(-77.12, 38.79, -76.91, 39.0), xapian_mode="libzim",
        xapian_workdir=str(work), **kw)
    a = Archive(str(tmp_path / "t.zim"))

    def chunk(k):
        return json.loads(bytes(a.get_entry_by_path(f"search-data/{k}.json").get_item().content))
    alex = [r for r in chunk("al") if r["t"] == "admin"]
    assert alex and alex[0]["s"] == "city" and alex[0]["al"] == 6
    dc = [r for r in chunk("di") if r["t"] == "admin"][0]
    assert dc["bb"] == DC["bbox"] and dc["alt"] == ["D.C.", "The District"]
    assert dc["l"] == "United States"
    # The relation's own tags, and the other names' prefixes, on both paths.
    assert alex[0]["q"] == "Q88" and alex[0]["w"] == "en:Alexandria, Virginia"
    assert any(r["n"] == "District of Columbia" for r in chunk("th"))
    assert any(r["n"] == "District of Columbia" for r in chunk("d_"))
    meta = json.loads(bytes(a.get_entry_by_path("search-data/manifest.json").get_item().content))
    assert meta["total"] == 3

    # Kiwix: title suggestions and full text.
    assert _suggest(a, "City of Alexandria") == ["City of Alexandria"]     # the redirect
    assert "Alexandria (city)" in _suggest(a, "Alexandria")
    assert "District of Columbia" in _suggest(a, "District of Columbia")
    assert "D.C." in _suggest(a, "D.C.")
    target = a.get_entry_by_title("City of Alexandria")
    assert target.is_redirect and target.get_redirect_entry().title == "Alexandria (city)"
    # repackage_zim (run on published ZIMs) keeps the redirects suggested.
    from cloud.repackage_zim import repackage
    repackage(str(tmp_path / "t.zim"), str(tmp_path / "r.zim"))
    r = Archive(str(tmp_path / "r.zim"))
    assert _suggest(r, "City of Alexandria") == ["City of Alexandria"]
    assert "Alexandria (city)" in _suggest(r, "Alexandria")
    search = Searcher(a).search(Query().set_query("District"))
    hits = [a.get_entry_by_path(p).title
            for p in search.getResults(0, search.getEstimatedMatches())]
    assert "District of Columbia" in hits


def test_in_memory_path_keeps_an_empty_region():
    """A country's region is intentionally empty; the location lookup
    fills other records only (as _search_bucket does)."""
    items, redirects = {}, []

    class Creator:
        def add_item(self, item):
            items[item.path] = item

        def add_redirection(self, *a):
            redirects.append(a)

    class Item:
        def __init__(self, path, title, mime, content, is_front=False):
            self.path, self.content = path, content

    feats = [dict(DC, location="", admin_level=2, subtype="country"), dict(SHOP)]
    W._add_search_in_memory(Creator(), Item, search_features=feats,
                            loc_lookup=lambda lat, lon: "Somewhere")
    recs = json.loads(items["search-data/di.json"].content)
    assert recs[0]["l"] == ""
    assert json.loads(items["search-data/al.json"].content)[0]["l"] == "Somewhere"
    assert redirects and redirects[0][1] == "D.C."


# ---- Wikipedia articles of admin areas ------------------------------------

UTRECHT = {"name": "Utrecht", "type": "admin", "subtype": "municipality",
           "lat": 52.0907, "lon": 5.1214, "location": "Utrecht", "admin_level": 8,
           "osm": "r47798", "wikidata": "Q803", "wikipedia": "nl:Utrecht (stad)"}
LIMMEL = {"name": "Limmel", "type": "admin", "subtype": "neighbourhood",
          "lat": 50.8666, "lon": 5.7087, "location": "Limburg", "admin_level": 10,
          "osm": "r2", "wikidata": "Q2", "wikipedia": "nl:Limmel"}
ONLY_Q = {"name": "Delft", "type": "admin", "subtype": "municipality",
          "lat": 52.0116, "lon": 4.3571, "location": "South Holland", "admin_level": 8,
          "osm": "r3", "wikidata": "Q690"}


def _wikidata(mapping):
    """urlopen stand-in answering wbgetentities from {qid: title | None}."""
    import io
    import urllib.parse
    from contextlib import contextmanager

    @contextmanager
    def answer(req, timeout=None):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
        ents = {}
        for i in q["ids"][0].split("|"):
            t = mapping.get(i)
            ents[i] = {"id": i, "sitelinks": {"enwiki": {"title": t}} if t else {}}
        yield io.BytesIO(json.dumps({"entities": ents}).encode())
    return answer


def _fake_bundler(seen):
    from cloud.wiki_articles import _underscore

    def bundle(titles, add_item, **kw):
        seen["titles"] = set(titles)
        stored = set()
        for t in seen["titles"]:
            add_item(f"wiki-article/{_underscore(t)}", t, "text/html", b"<p>x</p>")
            stored.add(_underscore(t))
        return {"bundled": len(stored), "bytes": 0, "failed": 0, "stored_titles": stored}
    return bundle


def test_admin_tags_are_resolved_and_their_articles_bundled(tmp_path, monkeypatch):
    """An admin area's non-English tag becomes its item's English article
    (create_osm_zim._finish_wiki_cross_refs, before titles are resolved),
    the record carries that title, and the article is bundled for it, not
    because a place node happens to carry the same tag."""
    pytest.importorskip("libzim.writer")
    mvt = pytest.importorskip("mapbox_vector_tile")
    from types import SimpleNamespace

    import create_osm_zim as c
    from cloud import wiki_articles as wa
    from cloud import wikidata_titles as wt
    from libzim.reader import Archive
    path = tmp_path / "features.jsonl"
    path.write_text("".join(json.dumps(f) + "\n" for f in (UTRECHT, LIMMEL, ONLY_Q, SHOP)))
    monkeypatch.setattr(wt.urllib.request, "urlopen",
                        _wikidata({"Q803": "Utrecht", "Q2": None, "Q690": "Delft"}))
    args = SimpleNamespace(resolve_wikidata_titles=True, wikidata_title_cache=None,
                           wikidata_title_map=None)
    refs = c._finish_wiki_cross_refs(args, None, str(path))
    assert refs[("admin", "r47798")]["wikipedia"] == "en:Utrecht"
    assert refs[("admin", "r2")]["wikipedia_no_en"] is True
    assert refs[("admin", "r3")]["wikipedia"] == "en:Delft"

    seen = {}
    monkeypatch.setattr(wa, "bundle_wiki_articles", _fake_bundler(seen))
    tile = gzip.compress(mvt.encode([{"name": "poi", "features": [
        {"geometry": "POINT(10 10)", "properties": {"name": "X"}}]}]))
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    work = tmp_path / "work"
    work.mkdir()
    W.create_zim(
        tmp_path / "t.zim", tiles={(14, 8424, 5399): tile}, tile_metadata={},
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "NL"}, name="OSM - NL", bbox=(3.3, 50.7, 7.3, 53.6),
        xapian_mode="none", xapian_workdir=str(work), search_features_path=str(path),
        wiki_cross_refs=refs, bundle_wiki_articles=True)
    # Bundled deliberately: the resolved titles, not the flagged Dutch one.
    assert seen["titles"] == {"en:Utrecht", "en:Delft"}
    a = Archive(str(tmp_path / "t.zim"))

    def rec(key, name):
        body = bytes(a.get_entry_by_path(f"search-data/{key}.json").get_item().content)
        return [r for r in json.loads(body) if r["n"] == name and r["t"] == "admin"][0]
    assert (rec("ut", "Utrecht")["w"], rec("ut", "Utrecht")["wsrc"]) == ("en:Utrecht", "wd")
    assert rec("ut", "Utrecht")["q"] == "Q803"
    assert rec("li", "Limmel")["w"] == "nl:Limmel" and "wsrc" not in rec("li", "Limmel")
    assert rec("de", "Delft")["w"] == "en:Delft"
    geo = json.loads(bytes(a.get_entry_by_path("wiki-geo-index.json").get_item().content))
    assert set(geo) == {"Utrecht", "Delft"}


def test_admin_articles_are_bundled_on_the_in_memory_path(tmp_path, monkeypatch):
    """A feature list's admin areas join the wiki lookup inside create_zim
    (no resolution there: create_osm_zim resolves a search JSONL's)."""
    pytest.importorskip("libzim.writer")
    mvt = pytest.importorskip("mapbox_vector_tile")
    from cloud import wiki_articles as wa
    from libzim.reader import Archive
    seen = {}
    monkeypatch.setattr(wa, "bundle_wiki_articles", _fake_bundler(seen))
    tile = gzip.compress(mvt.encode([{"name": "poi", "features": [
        {"geometry": "POINT(10 10)", "properties": {"name": "X"}}]}]))
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    work = tmp_path / "work"
    work.mkdir()
    W.create_zim(
        tmp_path / "t.zim", tiles={(14, 4580, 6264): tile}, tile_metadata={},
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "DC"}, name="OSM - DC", bbox=(-77.12, 38.79, -76.91, 39.0),
        xapian_mode="none", xapian_workdir=str(work),
        search_features=[dict(ALEXANDRIA), dict(DC), dict(SHOP)],
        wiki_cross_refs={("x", 1, 2): {"wikipedia": "en:Other"},
                         # resolved from DC's Q-ID by the caller
                         ("admin", "r162069"): {"wikidata": "Q3551781", "wikipedia_src": "wd",
                                                "wikipedia": "en:Washington,_D.C."}},
        bundle_wiki_articles=True)
    assert seen["titles"] == {"en:Alexandria, Virginia", "en:Other", "en:Washington,_D.C."}
    a = Archive(str(tmp_path / "t.zim"))
    assert a.has_entry_by_path("wiki-article/Alexandria,_Virginia")
    dc = [r for r in json.loads(bytes(a.get_entry_by_path("search-data/di.json")
                                      .get_item().content)) if r["t"] == "admin"][0]
    assert (dc["w"], dc["wsrc"], dc["q"]) == ("en:Washington,_D.C.", "wd", "Q3551781")
