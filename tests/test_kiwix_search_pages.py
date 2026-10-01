"""Which search records get a Kiwix page (search/<slug>.html), and so an
entry in kiwix-serve's full-text search: places, parks, peaks, water,
airports and administrative areas (streetzim.zim_writer.KIWIX_PAGE_TYPES;
the areas are in tests/test_admin_search_pages.py), the same set on the
streaming and the in-memory path. POIs, streets and addresses do not, so
"Casino" in the Monaco ZIM finds the Fontaine du Casino (a lake) and not the
shops, stops and sights named Casino; the in-map search has those.
kiwix_poi_pages (--kiwix-poi-pages) adds the POIs, at ~440 B per POI (+16%
on Luxembourg, docs/zimfarm.md)."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FEATURES = [
    {"name": "Monte-Carlo Casino", "type": "poi", "subtype": "attraction"},
    {"name": "Casino", "type": "poi", "subtype": "grocery"},
    {"name": "Fontaine du Casino", "type": "water", "subtype": "lake"},
    {"name": "Monte-Carlo", "type": "place", "subtype": "suburb"},
    {"name": "Place du Casino", "type": "street", "subtype": "path"},
    {"name": "1 Place du Casino", "type": "addr"},
]


def test_page_types():
    from streetzim.zim_writer import KIWIX_PAGE_TYPES, kiwix_page_types
    assert KIWIX_PAGE_TYPES == {"place", "park", "peak", "water", "airport", "admin"}
    assert kiwix_page_types() == KIWIX_PAGE_TYPES
    assert kiwix_page_types(True) == KIWIX_PAGE_TYPES | {"poi"}


@pytest.mark.parametrize("poi_pages", [False, True])
@pytest.mark.parametrize("source", ["path", "in-memory"])
def test_kiwix_full_text_search_covers_the_page_types(tmp_path, source, poi_pages):
    pytest.importorskip("libzim.writer")
    mvt = pytest.importorskip("mapbox_vector_tile")
    from libzim.reader import Archive
    from libzim.search import Query, Searcher
    from streetzim.zim_writer import create_zim
    tile = gzip.compress(mvt.encode([{"name": "poi", "features": [
        {"geometry": "POINT(10 10)", "properties": {"name": "X"}}]}]))
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    feats = [dict(f, lat=43.73 + i * 1e-4, lon=7.42) for i, f in enumerate(FEATURES)]
    kw = {}
    if source == "path":
        path = tmp_path / "features.jsonl"
        path.write_text("".join(json.dumps(f) + "\n" for f in feats))
        kw["search_features_path"] = str(path)
    else:
        kw["search_features"] = feats
    work = tmp_path / "work"
    work.mkdir()
    create_zim(
        tmp_path / "t.zim", tiles={(14, 8529, 5974): tile}, tile_metadata={},
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "Monaco"}, name="OSM - Monaco",
        bbox=(7.40, 43.72, 7.44, 43.76), xapian_mode="libzim",
        xapian_workdir=str(work), kiwix_poi_pages=poi_pages, **kw)

    a = Archive(str(tmp_path / "t.zim"))
    pages = sorted(a._get_entry_by_id(i).title for i in range(a.all_entry_count)
                   if a._get_entry_by_id(i).path.startswith("search/"))
    pois = ["Casino", "Monte-Carlo Casino"] if poi_pages else []
    assert pages == sorted(["Fontaine du Casino", "Monte-Carlo"] + pois)
    assert a.has_fulltext_index
    search = Searcher(a).search(Query().set_query("casino"))
    hits = sorted(a.get_entry_by_path(p).title
                  for p in search.getResults(0, search.getEstimatedMatches()))
    assert hits == sorted(["Fontaine du Casino"] + pois)
    for phrase in ('"Food & Drink"', "maplibre", '"Data Sources"'):
        result = Searcher(a).search(Query().set_query(phrase))
        assert result.getEstimatedMatches() == 0, phrase
    # Title suggestions (the Kiwix search bar) need the pages to be front
    # articles; libzim leaves everything else out of the title index.
    assert a.has_title_index
    assert "Fontaine du Casino" in _suggest(a, "Fontaine")
    assert _suggest(a, "Place du") == []                  # streets: no page
    # repackage_zim (run on published ZIMs) keeps them front.
    from cloud.repackage_zim import repackage
    repackage(str(tmp_path / "t.zim"), str(tmp_path / "r.zim"))
    r = Archive(str(tmp_path / "r.zim"))
    assert "Fontaine du Casino" in _suggest(r, "Fontaine")
    # Front articles are exactly the main page and the search pages, before
    # and after repackage: not places.html or any other .html.
    for arc in (a, r):
        assert arc.get_entry_by_path("places.html").title   # present ...
        assert arc.article_count == len(pages) + 1          # ... but not front
        assert _suggest(arc, "Find places") == []


def _suggest(archive, text):
    from libzim.suggestion import SuggestionSearcher
    s = SuggestionSearcher(archive).suggest(text)
    return [archive.get_entry_by_path(p).title
            for p in s.getResults(0, s.getEstimatedMatches())]


@pytest.mark.parametrize("flag", [False, True])
def test_create_osm_zim_passes_the_flag_to_create_zim(tmp_path, monkeypatch, flag):
    """--kiwix-poi-pages must reach create_zim(kiwix_poi_pages=...): the
    argument hop create_osm_zim -> _write_zim -> create_zim."""
    import create_osm_zim as c
    seen = {}
    monkeypatch.setattr(c, "create_zim", lambda **kw: seen.update(kw))
    args = c.build_parser().parse_args(
        ["--bbox", "7.39,43.715,7.46,43.765"] + (["--kiwix-poi-pages"] if flag else []))
    c._write_zim(
        address_count=0, args=args, bbox_str="7.39,43.715,7.46,43.765", fonts={},
        map_config={}, maplibre_css="c", maplibre_js="j", mbtiles_path=None, name="M",
        output_path=str(tmp_path / "t.zim"), overture_sources=None, overture_themes=None,
        routing_graph_path=None, satellite_dir=None, satellite_format=None,
        satellite_max_zoom=0, search_features=[], terrain_dir=None, terrain_max_zoom=0,
        tile_metadata={}, tiles={}, tmpdir=str(tmp_path), total_tile_count=0,
        use_streaming=False, wiki_cross_refs=None, wikidata_data=None,
        zim_illustration=None, zim_metadata=None)
    assert seen["kiwix_poi_pages"] is flag
