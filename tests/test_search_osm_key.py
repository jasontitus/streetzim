"""``osm_key`` (streetzim/search_extract.py, search_record) is internal to
the search-feature JSONL: extraction writes it for tilemaker POIs whose
class was a raw OSM key, the Overture places merge reads it (the end-to-end
test with the streaming extraction is in test_overture.py), and the ZIM
never carries it."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Tile 14/8529/5973 covers Monaco; MVT coordinates are 0..4096 in tile space.
TILE = (14, 8529, 5973)
POIS = [
    # tilemaker: no OpenMapTiles class for amenity=restaurant.
    {"name": "Da Mario", "class": "amenity", "subclass": "restaurant"},
    # OpenFreeMap / Planetiler: the class is already specific.
    {"name": "Chez Paul", "class": "restaurant", "subclass": "restaurant"},
]


def _tile_bytes():
    mvt = pytest.importorskip("mapbox_vector_tile")
    return gzip.compress(mvt.encode([{"name": "poi", "features": [
        {"geometry": f"POINT({1000 + 1000 * i} 2000)", "properties": p}
        for i, p in enumerate(POIS)]}]))


def test_in_memory_extraction_writes_osm_key():
    """extract_searchable_features(tiles=...) runs _process_tile_for_search."""
    from streetzim.search_extract import _process_tile_for_search
    z, x, y = TILE
    recs = {r["name"]: r for r in _process_tile_for_search(
        (z, x, y, _tile_bytes(), {"poi": "poi"}))}
    assert recs["Da Mario"]["subtype"] == "restaurant"
    assert recs["Da Mario"]["osm_key"] == "amenity"
    assert "osm_key" not in recs["Chez Paul"]


def _zim_json_entries(zim: Path):
    from libzim.reader import Archive
    a = Archive(str(zim))
    for i in range(a.all_entry_count):
        e = a._get_entry_by_id(i)
        if e.is_redirect or not e.path.startswith(("search-data/", "category-index/")):
            continue
        body = bytes(e.get_item().content)
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        yield e.path, body


@pytest.mark.parametrize("source", ["path", "in-memory"])
def test_osm_key_never_reaches_the_zim(tmp_path, source):
    pytest.importorskip("libzim.writer")
    from streetzim.zim_writer import create_zim
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    feats = [{"name": f"Resto {i}", "type": "poi", "subtype": "restaurant",
              "osm_key": "amenity", "lat": 43.73 + i * 1e-4, "lon": 7.42}
             for i in range(20)]
    kw = {}
    if source == "path":
        path = tmp_path / "features.jsonl"
        path.write_text("".join(json.dumps(f) + "\n" for f in feats))
        kw["search_features_path"] = str(path)
    else:
        kw["search_features"] = feats
    work = tmp_path / "work"
    work.mkdir()
    z, x, y = TILE
    create_zim(
        tmp_path / "t.zim", tiles={(z, x, y): _tile_bytes()}, tile_metadata={},
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "Monaco"}, name="OSM - Monaco",
        bbox=(7.40, 43.72, 7.44, 43.76), split_find_chips=True,
        xapian_mode="none", xapian_workdir=str(work), **kw)
    entries = dict(_zim_json_entries(tmp_path / "t.zim"))
    # The records are there (so the check below looks at real output) ...
    assert any(b"Resto 7" in body for p, body in entries.items()
               if p.startswith("search-data/")), sorted(entries)
    if source == "path":
        assert b"Resto 7" in entries["category-index/chip-food.json"]
    # ... and none carries the internal key.
    assert [p for p, body in entries.items() if b"osm_key" in body] == []
