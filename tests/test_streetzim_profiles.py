"""`streetzim --profile`: what each profile turns on, how explicit feature
flags override it, and what plan() then asks the builder for."""
from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

cli = pytest.importorskip("streetzim.cli")

REQ = ["--name", "osm_en_monaco", "--title", "Monaco", "--description", "Offline Monaco",
       "--area", "monaco"]


def parse(*flags: str) -> argparse.Namespace:
    return cli.parse_args(REQ + list(flags))


def terrain_by_profile() -> bool:
    """True once --terrain can also be turned off (the terrain branch), so
    the profile sets it; until then it keeps its own default (off)."""
    return "terrain" in cli.profile_features(cli.build_parser())


def test_full_is_the_default_profile_and_turns_every_feature_on():
    args = parse()
    assert args.profile == cli.DEFAULT_PROFILE == "full"
    assert args.wikidata is True and args.wikipedia is True and args.overture is True
    assert args.routing
    assert args.terrain is terrain_by_profile()
    assert (vars(parse("--profile", "full")).items() >= {
        "wikidata": True, "wikipedia": True, "overture": True}.items())


def test_basic_turns_off_everything_but_routing():
    args = parse("--profile", "basic")
    assert args.wikidata is False and args.wikipedia is False and args.overture is False
    assert not args.terrain
    assert args.routing


def test_satellite_is_in_no_profile():
    assert all("satellite" not in feats for feats in cli.PROFILES.values())
    assert all(not v for k, v in cli.PROFILES["basic"].items())
    assert all(cli.PROFILES["full"].values())


def test_profiles_name_the_same_features():
    names = [set(feats) for feats in cli.PROFILES.values()]
    assert all(n == names[0] for n in names)
    assert set(cli.PROFILES) == {"full", "basic"}


@pytest.mark.parametrize("flags, expect", [
    (["--profile", "basic", "--wikidata"], {"wikidata": True, "wikipedia": False}),
    (["--profile", "basic", "--wikidata=on"], {"wikidata": True}),
    (["--profile", "basic", "--wikidata", "on"], {"wikidata": True}),
    (["--profile", "basic", "--wikipedia"], {"wikipedia": True, "overture": False}),
    (["--profile", "basic", "--overture=on"], {"overture": True, "wikidata": False}),
    (["--no-wikidata"], {"wikidata": False, "wikipedia": True, "overture": True}),
    (["--wikidata=off"], {"wikidata": False, "overture": True}),
    (["--no-wikipedia"], {"wikipedia": False, "wikidata": True}),
    (["--no-overture", "--profile", "full"], {"overture": False, "wikipedia": True}),
    (["--overture=off", "--overture=off"], {"overture": False}),
    (["--no-routing", "--profile", "basic"], {"routing": False}),
])
def test_an_explicit_flag_wins_over_the_profile(flags, expect):
    args = parse(*flags)
    assert {k: getattr(args, k) for k in expect} == expect


@pytest.mark.parametrize("flags", [
    ["--wikidata", "--no-wikidata"], ["--wikidata=off", "--wikidata"],
    ["--profile=basic", "--overture", "--no-overture"], ["--wikipedia=on", "--wikipedia=off"],
])
def test_a_feature_switched_both_ways_is_refused(flags, capsys):
    with pytest.raises(SystemExit):
        parse(*flags)
    assert "contradicts" in capsys.readouterr().err


def test_bad_values_and_abbreviations_are_refused(capsys):
    for flags in (["--wikidata=yes"], ["--profile=everything"], ["--overt"],
                  ["--no-wiki"], ["--prof", "basic"]):
        with pytest.raises(SystemExit):
            parse(*flags)


def test_wikipedia_zim_needs_wikipedia(capsys):
    with pytest.raises(SystemExit):
        parse("--profile", "basic", "--wikipedia-zim-url", "https://example.org/w.zim")
    assert "needs --wikipedia" in capsys.readouterr().err
    assert parse("--profile", "basic", "--wikipedia",
                 "--wikipedia-zim-url", "https://example.org/w.zim").wikipedia


def _merged_parser(terrain_default: bool, kiwix: bool = False) -> argparse.ArgumentParser:
    """A parser shaped like build_parser() after the other branches merge:
    --terrain/--no-terrain (topic-terrain-openzim, whatever default it
    gives it) and --kiwix-poi-pages (topic-viewer-polish, store_true)."""
    p = argparse.ArgumentParser(allow_abbrev=False)
    p.add_argument("--wikidata", action="store_true", help="Wikidata")
    p.add_argument("--terrain", action=argparse.BooleanOptionalAction,
                   default=terrain_default, help="Hillshade. Default: on")
    if kiwix:
        p.add_argument("--kiwix-poi-pages", action="store_true",
                       help="Every POI in Kiwix search, +9%% on Luxembourg. Default: off")
    cli.add_profile_arguments(p)
    return p


@pytest.mark.parametrize("terrain_default", [True, False])
@pytest.mark.parametrize("flags, terrain", [
    ([], True), (["--profile", "basic"], False), (["--profile", "basic", "--terrain"], True),
    (["--no-terrain"], False), (["--profile", "full", "--terrain=off"], False),
])
def test_profile_sets_terrain_once_it_can_be_switched_off(terrain_default, flags, terrain):
    p = _merged_parser(terrain_default)
    args = cli.apply_profile(p.parse_args(flags), p)
    assert args.terrain is terrain
    helps = [a.help for a in p._actions if a.dest == "terrain" and a.nargs == "?"]
    assert helps == ["Hillshade. Default: on with --profile full, off with basic"]


@pytest.mark.parametrize("flags, pages", [
    ([], True), (["--profile", "basic"], False), (["--profile", "basic", "--kiwix-poi-pages"], True),
    (["--kiwix-poi-pages=off"], False), (["--no-kiwix-poi-pages"], False),
])
def test_profile_sets_kiwix_poi_pages_once_the_flag_exists(flags, pages):
    p = _merged_parser(True, kiwix=True)
    args = cli.apply_profile(p.parse_args(flags), p)
    assert args.kiwix_poi_pages is pages
    assert "POIs in Kiwix search" in next(a.help for a in p._actions if a.dest == "profile")


def test_a_feature_the_parser_lacks_is_left_alone():
    # Before topic-viewer-polish: no --kiwix-poi-pages, so nothing to set.
    args = parse()
    if "kiwix_poi_pages" in cli.profile_features(cli.build_parser()):
        assert args.kiwix_poi_pages is True
    else:
        assert not hasattr(args, "kiwix_poi_pages")


def test_a_store_true_terrain_keeps_its_default():
    # Before the terrain branch: --terrain alone cannot be undone, so no
    # profile turns it on behind the user's back.
    p = argparse.ArgumentParser()
    p.add_argument("--terrain", action="store_true", help="Hillshade")
    cli.add_profile_arguments(p)
    assert cli.apply_profile(p.parse_args([]), p).terrain is False
    assert cli.apply_profile(p.parse_args(["--terrain"]), p).terrain is True
    assert "profile" not in next(a.help for a in p._actions if a.dest == "terrain")


# ---------------------------------------------------------------- plan()


@pytest.fixture
def offline(monkeypatch):
    """fetch() and the Overture download without the network."""
    asked: dict[str, list] = {"fetch": [], "overture": []}

    def fake_fetch(url, dest):
        asked["fetch"].append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("x")
        return dest

    def fake_overture(bbox, release, dl):
        asked["overture"].append((bbox, release))
        return {t: dl / "overture" / f"{t}.parquet" for t in cli.OVERTURE_THEMES}
    monkeypatch.setattr(cli, "fetch", fake_fetch)
    monkeypatch.setattr(cli, "fetch_overture", fake_overture)
    return asked


def builder(argv):
    import create_osm_zim
    return create_osm_zim.build_parser().parse_args(argv)


def test_full_plan_asks_the_builder_for_every_feature(tmp_path, offline):
    argv, _ = cli.plan(parse(), tmp_path)
    ns = builder(argv)
    assert ns.wikidata and ns.routing
    assert ns.resolve_wikidata_titles and ns.bundle_wiki_articles
    assert ns.wiki_articles_source is None           # the API: text only
    assert Path(ns.wikidata_title_cache).is_relative_to(tmp_path)
    assert Path(ns.wikidata_title_cache).parent.is_dir()    # the builder does not create it
    assert Path(ns.wiki_articles_cache).is_relative_to(tmp_path)
    assert ns.overture_addresses.endswith("addresses.parquet")
    assert ns.overture_places.endswith("places.parquet")
    assert offline["overture"] == [("7.40,43.72,7.44,43.76", "latest")]
    assert ns.split_hot_search_chunks_mb == 10 and ns.no_llm_bundle
    assert not ns.satellite


def test_basic_plan_fetches_nothing_extra(tmp_path, offline):
    argv, info = cli.plan(parse("--profile", "basic"), tmp_path)
    ns = builder(argv)
    assert not (ns.wikidata or ns.bundle_wiki_articles or ns.resolve_wikidata_titles)
    assert ns.overture_addresses is None and ns.overture_places is None
    assert offline["fetch"] == [info["pbf_url"]] and offline["overture"] == []
    assert ns.routing and ns.split_find_chips
    assert ns.split_hot_search_chunks_mb == 10 and ns.no_llm_bundle


def test_wikipedia_zim_is_downloaded_and_read_with_images(tmp_path, offline):
    url = "https://download.kiwix.org/zim/wikipedia/wikipedia_en_top_maxi_2026-09.zim"
    argv, _ = cli.plan(parse("--wikipedia-zim-url", url, "--wikipedia-images", "lead",
                             "--overture-release", "2026-09-23.1"), tmp_path)
    ns = builder(argv)
    assert url in offline["fetch"]
    assert Path(ns.wiki_articles_source).is_relative_to(tmp_path / "wikipedia")
    assert ns.wiki_images == "lead" and ns.wiki_image_max_kb == 128
    assert offline["overture"][0][1] == "2026-09-23.1"


def test_fetch_overture_resolves_once_and_names_files_by_release(tmp_path, monkeypatch):
    calls = []

    def download(theme, bbox, release, out):
        calls.append((theme, release))
        Path(out).write_bytes(b"PAR1")
        return out
    fake = types.SimpleNamespace(
        resolve_release=lambda rel, themes: calls.append(("resolve", rel, tuple(themes)))
        or "2026-09-23.1",
        download_overture=download)
    monkeypatch.setitem(sys.modules, "download_overture_data", fake)
    got = cli.fetch_overture("7.4,43.72,7.44,43.76", "latest", tmp_path)
    assert calls[0] == ("resolve", "latest", ("addresses", "places"))
    assert calls[1:] == [("addresses", "2026-09-23.1"), ("places", "2026-09-23.1")]
    assert all("2026-09-23.1" in p.name and p.is_file() for p in got.values())
    assert not list(tmp_path.rglob("*.part"))
    calls.clear()
    assert cli.fetch_overture("7.4,43.72,7.44,43.76", "latest", tmp_path) == got
    assert calls == [("resolve", "latest", ("addresses", "places"))]     # reused

    def fail(*_a):
        raise SystemExit("Could not list Overture addresses files")
    monkeypatch.setattr(fake, "download_overture", fail)
    with pytest.raises(ValueError, match="Overture Maps: Could not list"):
        cli.fetch_overture("0,0,1,1", "latest", tmp_path)


def _fake_overture(monkeypatch, download):
    fake = types.SimpleNamespace(resolve_release=lambda rel, themes: "2026-09-23.1",
                                 download_overture=download)
    monkeypatch.setitem(sys.modules, "download_overture_data", fake)
    return fake


def test_an_interrupted_overture_download_is_never_reused(tmp_path, monkeypatch):
    # download_overture reuses whatever file sits at its output path, so it
    # must never write to the final name: a partial file would pass as the
    # whole extract next time (--dl is reused across runs).
    written = []

    def half(theme, bbox, release, out):
        written.append(out)
        Path(out).write_bytes(b"PAR1 half")
        raise OSError("connection reset")
    _fake_overture(monkeypatch, half)
    with pytest.raises(ValueError, match=r"Overture Maps: could not fetch.*connection reset"):
        cli.fetch_overture("7.4,43.72,7.44,43.76", "latest", tmp_path)
    assert written and all(w.endswith(".part") for w in written)
    assert not [p for p in (tmp_path / "overture").iterdir() if p.suffix == ".parquet"]

    def whole(theme, bbox, release, out):
        assert not Path(out).exists()          # the stale .part was cleared
        Path(out).write_bytes(b"PAR1 whole")
        return out
    _fake_overture(monkeypatch, whole)
    got = cli.fetch_overture("7.4,43.72,7.44,43.76", "latest", tmp_path)
    assert all(p.read_bytes() == b"PAR1 whole" for p in got.values())


def test_a_duckdb_error_is_a_clear_overture_failure(tmp_path, monkeypatch):
    duckdb = pytest.importorskip("duckdb")

    def broken(theme, bbox, release, out):
        raise duckdb.IOException("HTTP 403")
    _fake_overture(monkeypatch, broken)
    with pytest.raises(ValueError, match=r"Overture Maps: could not fetch.*HTTP 403.*--no-overture"):
        cli.fetch_overture("7.4,43.72,7.44,43.76", "latest", tmp_path)


def test_duckdb_uses_the_extension_directory_it_is_given(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from streetzim.overture import DUCKDB_EXT_ENV, duckdb_connect
    monkeypatch.setenv(DUCKDB_EXT_ENV, str(tmp_path / "it's-here"))
    got = duckdb_connect().execute(
        "SELECT current_setting('extension_directory')").fetchone()[0]
    assert got == str(tmp_path / "it's-here")


def test_source_summary_names_what_each_source_delivered():
    from streetzim import source_report
    source_report.reset()
    assert source_report.summary() == "sources: none besides OSM"
    source_report.note("Overture addresses", "4087 rows (4012 added)")
    source_report.note("Wikipedia articles", "16/53 (37 not fetched, 37 rate-limited)")
    assert source_report.summary() == ("sources: Overture addresses 4087 rows (4012 added); "
                                       "Wikipedia articles 16/53 (37 not fetched, "
                                       "37 rate-limited)")
    source_report.reset()
