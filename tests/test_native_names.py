"""Native-script names (docs/search-records.md, `nn`): extracted from the
tiles, indexed under their own prefixes, carried into the search records
and the Kiwix page titles."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cloud import search_shards as S  # noqa: E402
from streetzim import search_extract as E  # noqa: E402
from streetzim import zim_writer as W  # noqa: E402


@pytest.mark.parametrize("props, want", [
    ({"name:latin": "Peking University", "name_int": "北京大学"}, "北京大学"),   # tilemaker
    ({"name:latin": "Peking University", "name": "北京大学"}, "北京大学"),       # OpenFreeMap
    ({"name:latin": "Beijing", "name": "Beijing"}, None),
    ({"name:latin": "Zurich", "name": "Zürich"}, None),          # same once folded
    ({"name:latin": "Seoul", "name_int": "서"}, None),             # one character
    ({"name:latin": "Seoul"}, None),
])
def test_native_name(props, want):
    assert E.native_name(props, props["name:latin"]) == want
    rec = E.search_record(props["name:latin"], "poi", props, 1.0, 2.0)
    assert rec.get("name_native") == want


def test_record_names_and_prefixes():
    rec = {"n": "Peking University", "nn": "北京大学", "t": "poi"}
    assert S.record_names(rec) == ["Peking University", "北京大学"]
    assert S.record_names({"n": "X", "nn": "", "alt": ["Y"]}) == ["X", "Y"]
    assert S.record_paths("u5317", rec, 4) == {("u4eac", "u5927", "u5b66", "_e")}
    assert S.record_paths("pe", rec, 4) != {(S.TERMINAL,)}


def test_kiwix_title_and_body():
    feat = {"name": "Peking University", "name_native": "北京大学", "type": "poi",
            "subtype": "university", "lat": 39.99, "lon": 116.3}
    assert W.kiwix_page_title(feat) == "Peking University · 北京大学"
    admin = {"name": "Beijing", "name_native": "北京市", "type": "admin",
             "subtype": "city", "lat": 39.9, "lon": 116.4}
    assert W.kiwix_page_title(admin) == "Beijing (city) · 北京市"


def test_search_cache_schema(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text("")
    assert E.schema_of(p) == 1
    E.mark_schema(p)
    assert E.schema_of(p) == E.SEARCH_SCHEMA == 2


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
    tile = gzip.compress(mvt.encode([{"name": "poi", "features": [
        {"geometry": "POINT(10 10)", "properties": {"name": "X"}}]}]))
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    feats = [{"name": "Peking University", "name_native": "北京大学", "type": "poi",
              "subtype": "university", "lat": 39.99, "lon": 116.30},
             {"name": "Tsinghua University", "type": "poi", "subtype": "university",
              "lat": 40.0, "lon": 116.32},
             {"name": "Haidian", "name_native": "海淀区", "type": "place",
              "subtype": "suburb", "lat": 39.96, "lon": 116.29}]
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
        tmp_path / "t.zim", tiles={(14, 13675, 6219): tile}, tile_metadata={},
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "Beijing"}, name="OSM - Beijing",
        bbox=(116.2, 39.9, 116.4, 40.1), xapian_mode="libzim",
        xapian_workdir=str(work), **kw)
    a = Archive(str(tmp_path / "t.zim"))

    def chunk(k):
        return json.loads(bytes(a.get_entry_by_path(f"search-data/{k}.json").get_item().content))
    pku = [r for r in chunk("pe") if r["n"] == "Peking University"]
    assert pku and pku[0]["nn"] == "北京大学"
    assert all("nn" not in r for r in chunk("ts"))
    if source == "path":
        # (the in-memory path keys whole names by their raw first two
        # characters, so it has no u<hex> chunks)
        native = chunk("u5317")
        assert [r["n"] for r in native] == ["Peking University"]
        assert [r["n"] for r in chunk("u6d77")] == ["Haidian"]
    # Kiwix pages (places, not POIs) are titled with both names.
    assert "Haidian · 海淀区" in _suggest(a, "Haidian")
    assert "Haidian · 海淀区" in _suggest(a, "海淀区")


@pytest.mark.parametrize("props, want", [
    # OpenFreeMap: name is the native name, name_int the Latin one.
    ({"name:latin": "Tokyo Tower", "name": "東京タワー", "name_int": "Tokyo Tower"}, "東京タワー"),
    ({"name:latin": "Higashi-Kyushu Expwy", "name_int": "東九州自動車道;延岡道路"}, "東九州自動車道"),
    ({"name:latin": "Hotel Itami", "name_int": "HOTEL ITAMI（ホテル伊丹）"}, None),
    ({"name:latin": "Unazuki Onsen (宇奈月)", "name_int": "宇奈月"}, None),
])
def test_native_name_rules(props, want):
    assert E.native_name(props, props["name:latin"]) == want


def test_record_names_include_the_latin_name():
    assert S.record_names({"n": "Tour Eiffel", "nl": "Eiffel Tower"}) == ["Tour Eiffel", "Eiffel Tower"]
    assert S.record_paths("ei", {"n": "Tour Eiffel", "nl": "Eiffel Tower", "t": "poi"}, 4)


def test_a_language_build_refuses_a_cache_without_the_language(tmp_path, monkeypatch):
    p = tmp_path / "c.jsonl"
    p.write_text("")
    E.mark_schema(p, languages=["de"])
    assert E.schema_languages(p) == ["de"]
    monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", "fr")
    E.mark_schema(p)
    assert E.schema_languages(p) == ["fr"]
    q = tmp_path / "d.jsonl"
    E.copy_schema(p, q)
    assert E.schema_languages(q) == ["fr"]


def test_shared_cache_dirs(tmp_path):
    import os
    from streetzim.cache_permissions import make_shared_dirs
    old = os.umask(0o022)
    try:
        os.chmod(tmp_path, 0o2775)
        make_shared_dirs(tmp_path / "lang" / "fr", tmp_path)
        make_shared_dirs(tmp_path / "lang" / "fr", tmp_path)          # again: fine
        for d in (tmp_path / "lang", tmp_path / "lang" / "fr"):
            assert d.stat().st_mode & 0o7777 == 0o2775
    finally:
        os.umask(old)


def test_shared_cache_dirs_fresh_root(tmp_path):
    """The first non-English build into a fresh --dl: the cache root
    (<dl>/cache/wikidata_cache) does not exist yet."""
    from streetzim.cache_permissions import make_shared_dirs
    root = tmp_path / "cache" / "wikidata_cache"
    make_shared_dirs(root / "lang" / "de", root)
    assert (root / "lang" / "de").is_dir()
