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
    """True once --terrain can be switched both ways (the terrain branch),
    so the profile sets it; until then it keeps its own default (off)."""
    return "terrain" in cli.switchable(cli.build_parser())


def test_full_is_the_default_profile_and_turns_every_feature_on():
    args = parse()
    assert args.profile == cli.DEFAULT_PROFILE == "full"
    assert args.wikidata and args.wikipedia and args.overture and args.routing
    assert args.terrain is terrain_by_profile()
    assert (vars(parse("--profile", "full")).items() >= {
        "wikidata": True, "wikipedia": True, "overture": True}.items())


def test_basic_turns_off_everything_but_routing():
    args = parse("--profile", "basic")
    assert not args.wikidata and not args.wikipedia and not args.overture
    assert not args.terrain
    assert args.routing


def test_satellite_is_in_no_profile():
    assert all("satellite" not in feats for feats in cli.PROFILES.values())


def test_profiles_name_the_same_features():
    names = [set(feats) for feats in cli.PROFILES.values()]
    assert all(n == names[0] for n in names)
    assert set(cli.PROFILES) == {"full", "basic"}


@pytest.mark.parametrize("flags, expect", [
    (["--profile", "basic", "--wikidata"], {"wikidata": True, "wikipedia": False}),
    (["--profile", "basic", "--wikipedia"], {"wikipedia": True, "overture": False}),
    (["--profile", "basic", "--overture"], {"overture": True, "wikidata": False}),
    (["--no-wikidata"], {"wikidata": False, "wikipedia": True, "overture": True}),
    (["--no-wikipedia"], {"wikipedia": False, "wikidata": True}),
    (["--no-overture", "--profile", "full"], {"overture": False, "wikipedia": True}),
    (["--no-routing", "--profile", "basic"], {"routing": False}),
])
def test_an_explicit_flag_wins_over_the_profile(flags, expect):
    args = parse(*flags)
    assert {k: getattr(args, k) for k in expect} == expect


def test_a_feature_switched_both_ways_is_refused(capsys):
    with pytest.raises(SystemExit):
        parse("--wikidata", "--no-wikidata")
    assert "--wikidata and --no-wikidata both given" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        parse("--profile=basic", "--overture", "--no-overture")


def test_wikipedia_zim_needs_wikipedia(capsys):
    with pytest.raises(SystemExit):
        parse("--profile", "basic", "--wikipedia-zim-url", "https://example.org/w.zim")
    assert "needs --wikipedia" in capsys.readouterr().err
    assert parse("--profile", "basic", "--wikipedia",
                 "--wikipedia-zim-url", "https://example.org/w.zim").wikipedia


def _merged_parser(terrain_default: bool) -> argparse.ArgumentParser:
    """A parser shaped like build_parser() once --terrain has a --no-terrain
    (whatever default that branch gives it)."""
    p = argparse.ArgumentParser()
    p.add_argument("--wikidata", action="store_true", help="Wikidata")
    p.add_argument("--terrain", action=argparse.BooleanOptionalAction,
                   default=terrain_default, help="Hillshade")
    cli.add_profile_arguments(p)
    return p


@pytest.mark.parametrize("terrain_default", [True, False])
@pytest.mark.parametrize("flags, terrain", [
    ([], True), (["--profile", "basic"], False), (["--profile", "basic", "--terrain"], True),
    (["--no-terrain"], False), (["--profile", "full", "--no-terrain"], False),
])
def test_profile_sets_terrain_once_it_can_be_switched_off(terrain_default, flags, terrain):
    p = _merged_parser(terrain_default)
    args = cli.apply_profile(p.parse_args(flags), flags, p)
    assert args.terrain is terrain
    assert "Default: on with --profile full, off with basic" in next(
        a.help for a in p._actions if a.dest == "terrain")


def test_a_store_true_feature_keeps_its_default():
    # Before the terrain branch: --terrain alone cannot be undone, so no
    # profile turns it on behind the user's back.
    p = argparse.ArgumentParser()
    p.add_argument("--terrain", action="store_true", help="Hillshade")
    cli.add_profile_arguments(p)
    assert cli.apply_profile(p.parse_args([]), [], p).terrain is False
    assert cli.apply_profile(p.parse_args(["--terrain"]), ["--terrain"], p).terrain is True
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
