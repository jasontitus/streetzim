"""web/generate.py's archive.org metadata fetch: an error answer is a failed
fetch (the site is not written), never an item without a ZIM; the item's last
good listing, up to a week old, stands in for one."""
import importlib.util
import io
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_metadata_fetch", ROOT / "web" / "generate.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


@pytest.fixture(autouse=True)
def _listings(tmp_path, monkeypatch):
    """Each test's own folder of kept listings (none at the start)."""
    monkeypatch.setenv("STREETZIM_IA_LISTINGS", str(tmp_path / "listings"))
    gen.STALE_LISTINGS.clear()
    return tmp_path / "listings"


def _answer(monkeypatch, *bodies):
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        return io.BytesIO(json.dumps(bodies[min(len(calls), len(bodies)) - 1]).encode())

    monkeypatch.setattr(gen.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gen.time, "sleep", lambda s: None)
    gen._ITEM_DETAILS.clear()
    return calls


def test_an_error_answer_is_a_failed_fetch(monkeypatch):
    calls = _answer(monkeypatch, {"error": "item metadata may be invalid"})
    assert gen.fetch_item_details("streetzim-africa", retries=3) is None
    assert len(calls) == 3                         # tried again, as for a timeout


def test_no_file_list_is_a_failed_fetch(monkeypatch):
    # {} for an identifier archive.org does not know; anything not a listing
    for body in ({}, [], None, {"metadata": {"title": "x"}}, {"files": None}):
        _answer(monkeypatch, body)
        assert gen.fetch_item_details("streetzim-x", retries=2) is None, body


def test_an_error_then_the_listing(monkeypatch):
    listing = {"files": [{"name": "osm-africa-2026-10-29.zim", "size": "1"}], "metadata": {}}
    _answer(monkeypatch, {"error": "item metadata may be invalid"}, listing)
    assert gen.fetch_item_details("streetzim-africa", retries=3) == listing


def test_a_listing_is_returned_as_it_is(monkeypatch):
    # a new item mid-upload lists no ZIM yet: "upcoming" is its caller's call
    for body in ({"files": [], "metadata": {"title": "new"}}, {"files": [{"name": "a.zim"}], "error": "x"}):
        _answer(monkeypatch, body)
        assert gen.fetch_item_details("streetzim-x", retries=1) == body


def test_a_listing_is_fetched_once_per_build(monkeypatch):
    listing = {"files": [{"name": "osm-x-2026-10-31.zim", "size": "1"}]}
    calls = _answer(monkeypatch, listing, {"error": "item metadata may be invalid"})
    assert gen.fetch_item_details("streetzim-x") == listing
    assert gen.fetch_item_details("streetzim-x") == listing     # the next language's page: the same listing
    assert len(calls) == 1


def test_the_last_good_listing_stands_in(monkeypatch, _listings):
    listing = {"files": [{"name": "osm-africa-2026-10-29.zim", "size": "1"}], "metadata": {}}
    _answer(monkeypatch, listing)
    assert gen.fetch_item_details("streetzim-africa") == listing        # kept
    assert (_listings / "streetzim-africa.json").exists()
    calls = _answer(monkeypatch, {"error": "item metadata may be invalid"})   # the next build
    assert gen.fetch_item_details("streetzim-africa", retries=2) == listing
    assert len(calls) == 2 and "streetzim-africa" in gen.STALE_LISTINGS
    assert gen.fetch_item_details("streetzim-kyiv", retries=1) is None        # none kept for it
    assert "streetzim-kyiv" not in gen.STALE_LISTINGS


def test_a_kept_listing_over_a_week_old_does_not(monkeypatch, _listings):
    listing = {"files": [{"name": "osm-africa-2026-10-20.zim", "size": "1"}]}
    _listings.mkdir()
    for age, ok in ((6.9, True), (7.1, False), (-1, False)):          # days; a future date is no good either
        (_listings / "streetzim-africa.json").write_text(json.dumps({"fetched": time.time() - age * 86400, "listing": listing}))
        _answer(monkeypatch, {"error": "item metadata may be invalid"})
        assert (gen.fetch_item_details("streetzim-africa", retries=1) == listing) is ok, age


def test_a_damaged_kept_listing_does_not(monkeypatch, _listings):
    _listings.mkdir()
    for kept in ("not json", json.dumps({"listing": {"files": []}}), json.dumps({"fetched": time.time(), "listing": {"x": 1}}),
                 json.dumps({"fetched": "soon", "listing": {"files": []}}), json.dumps([1])):
        (_listings / "streetzim-africa.json").write_text(kept)
        _answer(monkeypatch, {"error": "item metadata may be invalid"})
        assert gen.fetch_item_details("streetzim-africa", retries=1) is None, kept
