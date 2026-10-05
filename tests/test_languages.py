"""Builds in another language (create_osm_zim --language): names, the
written ZIM's records / Kiwix titles / metadata, the CLI."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import languages as L  # noqa: E402
from streetzim import search_extract as E  # noqa: E402
from streetzim import zim_writer as W  # noqa: E402


def test_codes():
    assert L.check(None) == "en" and L.check(" FR ") == "fr"
    assert L.iso639_3("fr") == "fra" and L.iso639_3("ja") == "jpn"
    with pytest.raises(ValueError):
        L.check("xx")


@pytest.mark.parametrize("feat, disp, native, latin", [
    # Tokyo in a French build: name:fr, the native name, the English one.
    ({"name": "Tokyo Tower", "name_native": "東京タワー",
      "names": {"fr": "Tour de Tokyo"}}, "Tour de Tokyo", "東京タワー", "Tokyo Tower"),
    # No name:fr: the place's own name, the English one beside it.
    ({"name": "Peking University", "name_native": "北京大学"}, "北京大学", None,
     "Peking University"),
    # name:fr is the English name: no separate Latin name.
    ({"name": "Paris", "names": {"fr": "Paris"}}, "Paris", None, None),
    # A French name for a place whose own name is French (no name_native).
    ({"name": "Genève", "names": {"fr": "Genève"}}, "Genève", None, None),
    ({"name": "Munich", "name_native": "München", "names": {"fr": "Munich"}},
     "Munich", "München", None),
])
def test_localize_feature(feat, disp, native, latin):
    f = E.localize_feature(dict(feat), "fr")
    assert (f["display_name"], f.get("display_native"), f.get("display_latin")) == (
        disp, native, latin)
    assert f["name"] == feat["name"]            # dedup / wiki keys unchanged
    assert W.shown_names(f) == (disp, native, latin)


def test_english_builds_are_untouched(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text(json.dumps({"name": "X", "names": {"fr": "Y"}}) + "\n")
    before = p.read_bytes()
    assert E.localize_features_file(p, "en") == 0 and p.read_bytes() == before
    assert E.localize_features_file(p, "fr") == 1
    assert json.loads(p.read_text())["display_name"] == "Y"


def test_extraction_keeps_only_the_build_language(monkeypatch):
    props = {"name:latin": "Tokyo", "name_int": "東京", "name:fr": "Tokyo",
             "name:de": "Tokio"}
    assert "names" not in E.search_record("Tokyo", "place", props, 1, 2)
    monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", "de")
    assert E.search_record("Tokyo", "place", props, 1, 2)["names"] == {"de": "Tokio"}


def test_cli_language(monkeypatch):
    from streetzim import cli
    base = ["--title", "Lyon", "--description", "Carte", "--include-poly", "x"]
    args = cli.parse_args(["--name", "osm_fr_lyon", "--language", "fr", *base])
    assert args.language == "fr"
    with pytest.raises(SystemExit):
        cli.parse_args(["--name", "osm_fr_lyon", *base])          # en vs _fr_
    with pytest.raises(SystemExit):
        cli.parse_args(["--name", "osm_xx_lyon", "--language", "xx", *base])


def _suggest(archive, text):
    from libzim.suggestion import SuggestionSearcher
    s = SuggestionSearcher(archive).suggest(text)
    return [archive.get_entry_by_path(p).title
            for p in s.getResults(0, s.getEstimatedMatches())]


def test_written_zim_in_french(tmp_path):
    pytest.importorskip("libzim.writer")
    mvt = pytest.importorskip("mapbox_vector_tile")
    from libzim.reader import Archive
    tile = gzip.compress(mvt.encode([{"name": "poi", "features": [
        {"geometry": "POINT(10 10)", "properties": {"name": "X"}}]}]))
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    feats = [E.localize_feature(f, "fr") for f in [
        {"name": "Tokyo", "name_native": "東京", "names": {"fr": "Tokyo"},
         "type": "place", "subtype": "city", "lat": 35.68, "lon": 139.76},
        {"name": "Imperial Palace", "name_native": "皇居", "names": {"fr": "Palais impérial"},
         "type": "park", "subtype": "park", "lat": 35.685, "lon": 139.75}]]
    path = tmp_path / "features.jsonl"
    path.write_text("".join(json.dumps(f) + "\n" for f in feats))
    work = tmp_path / "work"
    work.mkdir()
    W.create_zim(
        tmp_path / "t.zim", tiles={(14, 14552, 6451): tile}, tile_metadata={},
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "Tokyo", "language": "fr"}, name="OSM - Tokyo",
        bbox=(139.7, 35.6, 139.8, 35.7), xapian_mode="libzim",
        xapian_workdir=str(work), search_features_path=str(path))
    a = Archive(str(tmp_path / "t.zim"))
    assert bytes(a.get_metadata("Language")).decode() == "fra"
    cfg = json.loads(bytes(a.get_entry_by_path("map-config.json").get_item().content))
    assert cfg["language"] == "fr"

    def chunk(k):
        return json.loads(bytes(a.get_entry_by_path(f"search-data/{k}.json").get_item().content))
    palace = [r for r in chunk("pa") if r["n"] == "Palais impérial"]
    assert palace and palace[0]["nn"] == "皇居" and palace[0]["nl"] == "Imperial Palace"
    assert any(r["n"] == "Palais impérial" for r in chunk("im"))      # by its English name
    assert any(r["n"] == "Palais impérial" for r in chunk("u7687"))   # and its own
    assert "Palais impérial · 皇居" in _suggest(a, "Palais")


# ---- Wikipedia and Wikidata in the build's language ----------------------

def test_lang_titles():
    from cloud.wikidata_titles import is_lang_title
    assert is_lang_title("fr:Tour Eiffel", "fr") and is_lang_title("FR:X", "fr")
    assert not is_lang_title("en:Eiffel Tower", "fr")
    assert not is_lang_title("Eiffel Tower", "fr")          # no prefix: English
    assert is_lang_title("Eiffel Tower", "en") and is_lang_title("en:X", "en")


def test_cross_refs_resolve_to_the_build_wikipedia(monkeypatch):
    from cloud import wikidata_titles as T
    asked = {}

    def resolve(qids, **kw):
        asked["site"] = kw["site"]
        kw["misses"].add("Q2")
        return {"Q1": "Tour Eiffel"}

    monkeypatch.setattr(T, "resolve_qids", resolve)
    refs = {"a": {"wikipedia": "en:Eiffel Tower", "wikidata": "Q1"},
            "b": {"wikipedia": "fr:Louvre", "wikidata": "Q3"},
            "c": {"wikipedia": "en:Nowhere", "wikidata": "Q2"}}
    T.augment_wiki_cross_refs(refs, lang="fr", log=lambda *_: None)
    assert asked["site"] == "frwiki"
    assert refs["a"]["wikipedia"] == "fr:Tour_Eiffel" and refs["a"]["wikipedia_osm"] == "en:Eiffel Tower"
    assert refs["b"] == {"wikipedia": "fr:Louvre", "wikidata": "Q3"}      # already French
    assert refs["c"].get("wikipedia_no_en")                                # no French article


def test_articles_come_from_the_build_wikipedia(monkeypatch, tmp_path):
    from cloud import wiki_articles as A
    urls = []

    def get_json(url, **kw):
        urls.append(url)
        return {"parse": {"title": "Tour Eiffel", "text": "<p>La tour Eiffel est une tour.</p>"}}

    monkeypatch.setattr(A, "get_json", get_json)
    stored = {}
    stats = A.bundle_wiki_articles(
        ["fr:Tour_Eiffel"], lambda path, title, mt, data: stored.__setitem__(path, data),
        cache_dir=str(tmp_path), lang="fr", sleep=0)
    assert stats["bundled"] == 1 and urls and all(u.startswith("https://fr.wikipedia.org/") for u in urls)
    page = next(v for k, v in stored.items() if k.startswith("wiki-article/")).decode()
    assert '<html lang="fr">' in page and "https://fr.wikipedia.org/wiki/Tour_Eiffel" in page
    assert (tmp_path / "lang" / "fr").is_dir() and A._LANG == "en"


def test_wikidata_in_the_build_language(monkeypatch, tmp_path):
    import wikidata_cache as wc
    assert wc.lang_cache_dir(tmp_path, "en") == tmp_path
    assert wc.lang_cache_dir(tmp_path, "fr") == tmp_path / "lang" / "fr"
    queries = []
    monkeypatch.setattr(wc, "_run_sparql", lambda q, **kw: queries.append(q) or [])
    wc.fetch_wikidata_batch(["Q90"], lang="fr")
    assert 'wikibase:language "fr,en,de,es"' in queries[0]
    assert "<https://fr.wikipedia.org/>" in queries[0]
    monkeypatch.setattr(wc, "extract_qids_from_pbf", lambda pbf, cache_dir=None: (
        queries.append(("scan", cache_dir)) or {"Q90": {}}))
    monkeypatch.setattr(wc, "fetch_wikidata_batch",
                        lambda qids, **kw: {"Q90": {"label": "Paris", "description": "capitale"}})
    monkeypatch.setattr(wc, "fetch_wikipedia_extracts", lambda e, **kw: 0)
    path = wc.build_cache(pbf_path="x.pbf", cache_dir=tmp_path, lang="fr")
    assert path == tmp_path / "lang" / "fr"
    assert ("scan", tmp_path) in queries                     # Q-ID scans stay shared
    assert wc.load_cache_for_zim(path)["Q90"]["d"] == "capitale"
    assert not wc.load_cache(tmp_path)                       # English cache untouched


def test_language_guards(tmp_path):
    from streetzim.languages import wiki_code, zim_matches
    assert wiki_code("nb") == "no" and wiki_code("fr") == "fr"
    assert zim_matches("fr", "fra") and zim_matches("fr", "eng,fra")
    assert zim_matches("nb", "nor") and zim_matches("nb", "nob")
    assert not zim_matches("fr", "eng")
    p = tmp_path / "c.jsonl"
    p.write_text("")
    assert E.cache_language_error(p, "en") is None
    assert "has none" in E.cache_language_error(p, "fr")          # no marker at all
    E.mark_schema(p, languages=["fr"])
    assert E.cache_language_error(p, "fr") is None
    assert E.cache_language_error(p, "de")


def test_contradictory_no_and_only_drop_that_mode():
    from streetzim.routing import restrictions as tr
    c = tr.Collector()
    seq = {1: ([(10, 100), (11, 101)], 0), 2: ([(11, 101), (12, 102)], 0)}
    c.raw = [({"car": "no", "bike": None}, [1], 101, [], [2]),
             ({"car": "only", "bike": None}, [1], 101, [], [2])]
    recs, dropped = tr.resolve(c, seq)
    assert recs == [] and dropped["contradictory no_* and only_* on one path"] == 1
