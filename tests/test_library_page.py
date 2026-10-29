"""The library page (web/library/index.html) in headless Chromium over a
catalog made from the real cloud/region-places.tsv and m49-countries.tsv
(tests/library_page.mjs): the place filters, the area rule, search, the
address, the language hint, and on a phone the folded filters and the
direct download of a language version (or /build/ when the build worker
cannot serve it).

Needs the browser stack (NODE, CHROME_PATH, PLAYWRIGHT_CORE; BROWSER_LIBS on
the build host); skips without it."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NEED = ("NODE", "CHROME_PATH", "PLAYWRIGHT_CORE")
if not all(os.environ.get(k) and os.path.exists(os.environ[k]) for k in NEED):
    pytest.skip(f"the browser stack: set {', '.join(NEED)}", allow_module_level=True)


@pytest.fixture(scope="module")
def shown(tmp_path_factory: pytest.TempPathFactory) -> dict:
    spec = importlib.util.spec_from_file_location("site_generate_lp", ROOT / "web" / "generate.py")
    gen = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["generate.py"]
    try:
        spec.loader.exec_module(gen)
    finally:
        sys.argv = argv
    site = tmp_path_factory.mktemp("libsite")
    (site / "library").mkdir()
    shutil.copy(ROOT / "web" / "library" / "index.html", site / "library" / "index.html")
    library = {r["id"]: {"id": r["id"], "title": r["title"], "description": gen.plain_text(r["description"]),
                         "tier": r["tier"], "language": r.get("language", "en"), "file": f"osm-{r['id']}-2026-10-26.zim",
                         "size": 2_000_000_000 + n, "date": "2026-10-26",
                         "download": f"https://archive.org/download/streetzim-{r['id']}/osm-{r['id']}-2026-10-26.zim",
                         "details": f"https://archive.org/details/streetzim-{r['id']}", "torrent": None, "preview": None}
               for n, r in enumerate(gen.REGIONS)}
    versions = {rid: [{"lang": "de", "label": "Deutsch", "size": 1_500_000_000,
                       "webseed": f"https://merged.invalid/merged/{rid}/de/0b8fad05-13d1-5ed9-9cca-b7930cf38270/osm_de_{rid}.zim"}]
                for rid in ("greece", "iceland")}
    gen.write_library_catalog(library, versions, out_dir=str(site / "library"))
    r = subprocess.run([os.environ["NODE"], str(ROOT / "tests" / "library_page.mjs"), str(site)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_no_page_errors(shown):
    assert shown["pageErrors"] == []


def test_countries_are_under_their_un_regions(shown):
    ea = shown["eastern-asia"]
    assert {"China", "Japan", "Taiwan", "South Korea", "Mongolia"} <= set(ea["before"]["options"]["country"])
    assert {"China", "Himalayas"} <= set(ea["china"]["titles"]) and "West Asia" not in ea["china"]["titles"]
    assert "Iran" not in shown["western-asia"]["options"]["country"]
    assert {"Iran", "West Asia"} <= set(shown["iran"]["titles"])
    assert {"Cuba", "Jamaica", "Haiti", "Dominican Republic", "Puerto Rico"} <= set(shown["caribbean"]["options"]["country"])
    assert {"Palau", "Kiribati", "Marshall Islands", "Nauru"} <= set(shown["micronesia"]["options"]["country"])
    cz = shown["czechia"]
    assert "Czechia" in cz["options"]["country"] and {"Austria, Czechia & Slovakia", "Europe"} <= set(cz["titles"]), cz["titles"]


def test_an_area_shows_only_the_maps_that_hold_it(shown):
    assert shown["hawaii"]["titles"] == ["Hawaii"], shown["hawaii"]["titles"]
    fl = shown["florida"]["titles"]
    assert "Florida" in fl and "United States" in fl and "Hawaii" not in fl and "California" not in fl, fl
    assert any("East Coast" in t for t in fl), fl
    assert set(shown["taipei"]["titles"]) == {"Taipei", "Taiwan"}, shown["taipei"]["titles"]
    assert "Hawaii" in shown["hawaii"]["options"]["area"]


def test_search_finds_countries_in_continent_maps_and_ranks_titles_first(shown):
    assert set(shown["madagascar"]["titles"]) == {"Africa", "Africa (Light)"}, shown["madagascar"]["titles"]
    titles = shown["rank"]["titles"]
    assert titles and "Switzerland" in titles[0], titles
    assert titles.index("Europe") > max(i for i, t in enumerate(titles) if "Switzerland" in t), titles


def test_a_place_change_clears_the_narrower_filters(shown):
    v = shown["reset"]["values"]
    assert v["continent"] == "Europe" and v["region"] == "" and v["country"] == ""


def test_a_bad_address_does_not_break_the_page(shown):
    m = shown["malformed"]
    assert m["first"]["count"].endswith("maps") and m["after"]["values"]["continent"] == "Europe"


def test_all_languages_survives_the_address(shown):
    la = shown["lang-all"]
    assert la["first"]["values"]["lang"] == "" and la["first"]["values"]["country"] == "Japan", la["first"]["values"]
    assert "Japan" in la["first"]["titles"] and "lang=all" in la["after"]["hash"], la["after"]["hash"]


def test_a_language_says_how_many_maps_it_hides(shown):
    g = shown["german"]
    assert g["values"]["lang"] == "de" and g["langhint"] and "Show all languages" in g["langhint"], g["langhint"]


def test_a_desktop_builds_language_versions_on_build(shown):
    bars = {t: (text, href, aria) for t, text, href, aria in shown["desktop-de"]["bars"]}
    text, href, aria = bars["Greece"]
    assert text.startswith("Build & download - 1.4 GB") and href == "/build/?region=greece&lang=de&go=1", bars["Greece"]
    assert aria.startswith("Greece (Deutsch)")


def test_a_phone_folds_the_filters_and_downloads_directly(shown):
    ph = shown["phone"]
    assert ph["first"]["visibleSelects"] == ["f-lang"] and ph["first"]["scrollWidth"] <= ph["first"]["width"], ph["first"]
    assert {"f-continent", "f-region", "f-country"} <= set(ph["opened"]), ph["opened"]
    bars = {t: text for t, text, _h, _a in ph["first"]["bars"]}
    assert bars["Greece"].startswith("Download - 1.4 GB"), bars
    assert "/merged/greece/de/" in ph["greece"], ph            # the worker said yes: the browser downloads it
    assert ph["iceland"].endswith("/build/?region=iceland&lang=de&go=1"), ph          # 503: the builder page
    assert any(x.startswith("HEAD /merged/greece/") for x in shown["merged"]), shown["merged"]
