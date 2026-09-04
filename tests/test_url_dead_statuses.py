"""STREETZIM_URL_DEAD_STATUSES narrows which liveness-cache entries drop
or scrub a business record (both the build-side and cloud helper)."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cloud"))

import create_osm_zim  # noqa: E402
import url_cache_filter  # noqa: E402

CACHE = {
    "https://gone.example/": {"alive": False, "status": 404},
    "https://nodns.example/": {"alive": False, "status": "dns"},
    "https://blocked.example/": {"alive": False, "status": 403},
    "https://slow.example/": {"alive": False, "status": "timeout"},
    "https://fine.example/": {"alive": True, "status": 200},
}


@pytest.fixture(params=[create_osm_zim._is_url_dead, url_cache_filter.is_url_dead],
                ids=["create_osm_zim", "url_cache_filter"])
def is_dead(request):
    return request.param


def test_default_any_alive_false_is_dead(monkeypatch, is_dead):
    monkeypatch.delenv("STREETZIM_URL_DEAD_STATUSES", raising=False)
    assert is_dead("https://gone.example/", CACHE)
    assert is_dead("https://blocked.example/", CACHE)
    assert is_dead("https://slow.example/", CACHE)
    assert not is_dead("https://fine.example/", CACHE)
    assert not is_dead("https://unknown.example/", CACHE)


def test_narrowed_statuses(monkeypatch, is_dead):
    monkeypatch.setenv("STREETZIM_URL_DEAD_STATUSES", "404,410, DNS")
    assert is_dead("https://gone.example/", CACHE)
    assert is_dead("https://nodns.example/", CACHE)
    assert not is_dead("https://blocked.example/", CACHE)   # 403 = bot block
    assert not is_dead("https://slow.example/", CACHE)      # timeout
    assert not is_dead("https://fine.example/", CACHE)


def test_decide_record_action_respects_narrowing(monkeypatch):
    monkeypatch.setenv("STREETZIM_URL_DEAD_STATUSES", "404")
    rec = {"ws": "https://blocked.example/"}
    assert url_cache_filter.decide_record_action(rec, CACHE, policy="drop-record") == "keep"
    rec = {"ws": "https://gone.example/"}
    assert url_cache_filter.decide_record_action(rec, CACHE, policy="drop-record") == "drop"
