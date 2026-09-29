"""Which search records get a Kiwix page (search/<slug>.html), and so an
entry in kiwix-serve's full-text search: places, parks, peaks, water and
airports (streetzim.zim_writer.KIWIX_PAGE_TYPES), the same set on the
streaming and the in-memory path. POIs, streets and addresses do not, so
"Casino" in the Monaco ZIM finds the Fontaine du Casino (a lake) and not the
shops, stops and sights named Casino; the in-map search has those. Adding
"poi" costs ~245 B per POI (+9.4% on Luxembourg), see KIWIX_PAGE_TYPES."""
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
    from streetzim.zim_writer import KIWIX_PAGE_TYPES
    assert KIWIX_PAGE_TYPES == {"place", "park", "peak", "water", "airport"}


@pytest.mark.parametrize("source", ["path", "in-memory"])
def test_kiwix_full_text_search_covers_the_page_types(tmp_path, source):
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
        xapian_workdir=str(work), **kw)

    a = Archive(str(tmp_path / "t.zim"))
    pages = sorted(a._get_entry_by_id(i).title for i in range(a.all_entry_count)
                   if a._get_entry_by_id(i).path.startswith("search/"))
    assert pages == ["Fontaine du Casino", "Monte-Carlo"]
    assert a.has_fulltext_index
    search = Searcher(a).search(Query().set_query("casino"))
    hits = sorted(a.get_entry_by_path(p).title
                  for p in search.getResults(0, search.getEstimatedMatches()))
    assert hits == ["Fontaine du Casino"]
