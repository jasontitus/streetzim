"""Language-ready builds (docs/language-ready.md): the region-language table
(cloud/region-languages.tsv), the language lists (streetzim/languages.py)
and what create_osm_zim does with --tile-languages / --region-languages:
map-config tileLanguages / regionLanguages, STREETZIM_TILE_LANGUAGES, and
nothing else for a build that passes neither."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import languages as L  # noqa: E402

TABLE = ROOT / "cloud" / "region-languages.tsv"


def _rows(path: Path) -> list[list[str]]:
    return [ln.split("\t") for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def test_every_registry_region_has_exactly_one_row():
    registry = [r[0] for r in _rows(ROOT / "cloud" / "regions.tsv")]
    ids = [r[0] for r in _rows(TABLE)]
    assert len(ids) == len(set(ids)), "an id is listed twice"
    assert sorted(set(registry) - set(ids)) == [], "registry regions without languages"
    assert sorted(set(ids) - set(registry)) == [], "rows for ids the registry lacks"


def test_every_row_is_a_clean_language_list():
    for row in _rows(TABLE):
        assert len(row) == 2, row
        rid, text = row
        # As the wrappers pass it on: already what region_languages makes of it.
        assert ",".join(L.region_languages(text)) == text, rid


@pytest.mark.parametrize("rid, first", [("greece", "el"), ("japan", "ja"), ("iran", "fa"),
                                        ("united-states", "en"), ("switzerland", "de"),
                                        ("benelux", "nl"), ("hispaniola", "ht"),
                                        # Led by the countries the site card names,
                                        # not by neighbours the box overlaps.
                                        ("central-asia", "uz"), ("southeast-asia", "id"),
                                        ("himalayas", "ne")])
def test_known_regions(rid, first):
    assert dict(_rows(TABLE))[rid].split(",")[0] == first


@pytest.mark.parametrize("rid", ["united-states", "california", "texas", "florida",
                                 "new-york-state", "nyc-metro", "greater-la"])
def test_spanish_where_millions_speak_it(rid):
    assert dict(_rows(TABLE))[rid].split(",")[:2] == ["en", "es"]


def test_the_matrix_list():
    assert len(L.MATRIX_TILE_LANGUAGES) == 21
    assert L.tile_languages(",".join(L.MATRIX_TILE_LANGUAGES)) == list(L.MATRIX_TILE_LANGUAGES)


def test_tile_languages():
    assert L.tile_languages(" fr, DE de\tar ") == ["ar", "de", "fr"]
    for bad in ("", " , ", "en", "de,en", "xx", "de,zz"):
        with pytest.raises(ValueError):
            L.tile_languages(bad)


def test_region_languages():
    assert L.region_languages("ht, es,fr,es") == ["ht", "es", "fr"]     # order kept
    assert L.region_languages("tpi") == ["tpi"]
    for bad in ("", "e", "engl", "e1", "zh-hant", ","):
        with pytest.raises(ValueError):
            L.region_languages(bad)


# ---- create_osm_zim ---------------------------------------------------------

@pytest.fixture
def coz(monkeypatch):
    import create_osm_zim
    monkeypatch.delenv("STREETZIM_TILE_LANGUAGES", raising=False)
    return create_osm_zim


def _args(coz, *argv):
    args = coz.build_parser().parse_args(list(argv))
    coz._apply_languages(args)
    return args


def _map_config(coz, args):
    _, mc = coz._build_map_config(
        args=args, bbox_str="28,29,34,32.5", name="T", overture_sources=None,
        routing_graph_path=None, satellite_dir=None, satellite_format="avif",
        satellite_max_zoom=None, satellite_tile_size=256, search_features=None,
        terrain_dir=None, terrain_max_zoom=None, total_steps=6, wiki_cross_refs=None,
        wikidata_data=None)
    return mc


def test_map_config_records_the_languages_and_nothing_else(coz):
    plain = _map_config(coz, _args(coz))
    assert "tileLanguages" not in plain and "regionLanguages" not in plain
    mc = _map_config(coz, _args(coz, "--tile-languages", "fr,de", "--region-languages", "el,en"))
    assert mc.pop("tileLanguages") == ["de", "fr"]
    assert mc.pop("regionLanguages") == ["el", "en"]
    assert mc == plain


def test_tile_languages_reach_the_tile_profile_and_the_glyphs(coz):
    from streetzim import glyph_fallback
    _args(coz, "--tile-languages", "th,ar")
    assert os.environ["STREETZIM_TILE_LANGUAGES"] == "ar,th"
    assert {"name:ar", "name:th"} <= glyph_fallback.label_keys()
    # A build in another language keeps its own, once.
    _args(coz, "--tile-languages", "th,ar", "--language", "fr")
    assert os.environ["STREETZIM_TILE_LANGUAGES"] == "ar,th,fr"
    _args(coz, "--tile-languages", "fr,ar", "--language", "fr")
    assert os.environ["STREETZIM_TILE_LANGUAGES"] == "ar,fr"


def test_without_the_flags_the_variable_is_as_before(coz, monkeypatch):
    monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", "de")     # from a shell: not inherited
    _args(coz)
    assert "STREETZIM_TILE_LANGUAGES" not in os.environ
    _args(coz, "--language", "fr", "--region-languages", "el")
    assert os.environ["STREETZIM_TILE_LANGUAGES"] == "fr"


@pytest.mark.parametrize("argv", [["--tile-languages", "en"], ["--tile-languages", ""],
                                  ["--tile-languages", "xx"], ["--region-languages", "e1"],
                                  ["--region-languages", ""]])
def test_main_rejects_bad_lists_before_building(coz, argv, capsys):
    with pytest.raises(SystemExit) as e:
        coz.main([*argv, "--bbox", "0,0,1,1"])
    assert e.value.code == 2
    assert "language" in capsys.readouterr().err


def test_main_applies_the_languages(coz, monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def stop(n):
        seen["env"] = os.environ.get("STREETZIM_TILE_LANGUAGES")
        raise Stop
    monkeypatch.setattr(coz._cpus, "set_build_cpus", stop)
    with pytest.raises(Stop):
        coz.main(["--tile-languages", "de,ar", "--region-languages", "el", "--bbox", "0,0,1,1"])
    assert seen["env"] == "ar,de"
