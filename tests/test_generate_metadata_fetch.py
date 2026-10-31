"""web/generate.py's archive.org metadata fetch: an error answer is a failed
fetch (the site is not written), never an item without a ZIM."""
import importlib.util
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_metadata_fetch", ROOT / "web" / "generate.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


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
