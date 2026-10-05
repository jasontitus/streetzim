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
