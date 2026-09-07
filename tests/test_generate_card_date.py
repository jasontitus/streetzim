"""Each live download card shows the ZIM's build date.

Without it the only way to tell a refreshed ZIM from the one already on
disk was to download it and look inside.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen", ROOT / "web" / "generate.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["gen"] = gen
spec.loader.exec_module(gen)

REGION = {"id": "california", "title": "California",
          "zim_file": "osm-california-2026-09-06.zim",
          "description": "All of California.", "tier": "local"}


def test_date_comes_from_the_filename():
    assert gen.zim_build_date("osm-california-2026-09-06.zim") == ("2026-09-06", "6 Sep 2026")


def test_suffixed_rebuild_still_parses():
    # a same-day re-roll is osm-<id>-<date>c.zim
    assert gen.zim_build_date("osm-russia-2026-05-12c.zim") == ("2026-05-12", "12 May 2026")


def test_undated_legacy_file_falls_back_to_mtime():
    iso, human = gen.zim_build_date("osm-europe.zim", {"mtime": "1780392880"})
    assert iso == "2026-06-02" and human == "2 Jun 2026"


def test_unparseable_yields_no_date_rather_than_a_wrong_one():
    assert gen.zim_build_date("osm-x.zim") is None
    assert gen.zim_build_date("osm-bad-2026-13-45.zim") is None      # month 13
    assert gen.zim_build_date("osm-e.zim", {"mtime": "not-a-number"}) is None


def test_card_renders_a_machine_readable_date():
    html = gen.render_live_card(REGION, "3.9 GB",
                                build_date=("2026-09-06", "6 Sep 2026"))
    assert 'class="map-card-date"' in html
    assert '<time datetime="2026-09-06">6 Sep 2026</time>' in html
    assert "Updated" in html


def test_card_without_a_date_omits_the_line_entirely():
    html = gen.render_live_card(REGION, "3.9 GB", build_date=None)
    assert "map-card-date" not in html
