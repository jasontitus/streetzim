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
    assert W.kiwix_alt_titles(dict(ALEXANDRIA, alt=["City of Alexandria", "Alex"])) == [
        "City of Alexandria", "Alex"]


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
    if source == "path":
        # The relation's own tags, and the other names' prefixes.
        assert alex[0]["q"] == "Q88" and alex[0]["w"] == "en:Alexandria, Virginia"
        assert any(r["n"] == "District of Columbia" for r in chunk("th"))
        assert any(r["n"] == "District of Columbia" for r in chunk("d_"))

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
