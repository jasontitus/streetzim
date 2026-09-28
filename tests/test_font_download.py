"""Font glyph download: transient errors are retried, 404s are skipped,
and a range that still fails stops the build (it used to ship silently as
missing glyphs; seen as 1-9 of 768 ranges in CI-sized builds)."""
from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import create_osm_zim as coz  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, behaviour):
    calls = {}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls[url] = calls.get(url, 0) + 1
        return behaviour(url, calls[url])

    monkeypatch.setattr(coz.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(coz.time, "sleep", lambda s: None)
    return calls


def _http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


def test_transient_errors_are_retried(monkeypatch):
    def behaviour(url, n):
        if url.endswith("/256-511.pbf") and n < 3:
            raise ConnectionResetError("reset")
        if url.endswith("/512-767.pbf") and n < 2:
            raise _http(503)
        if url.endswith("/768-1023.pbf"):
            raise _http(404)
        return _Resp(b"glyphs")
    calls = _install(monkeypatch, behaviour)
    fonts = coz.generate_sdf_font_glyphs()
    assert fonts[("OpenSansRegular", "256-511")] == b"glyphs"
    assert fonts[("OpenSansBold", "512-767")] == b"glyphs"
    assert ("OpenSansRegular", "768-1023") not in fonts          # 404 = no glyphs
    assert max(n for u, n in calls.items() if u.endswith("/768-1023.pbf")) == 1


def test_persistent_failure_stops_the_build(monkeypatch):
    def behaviour(url, n):
        if url.endswith("/8192-8447.pbf"):
            raise _http(500)
        return _Resp(b"g")
    _install(monkeypatch, behaviour)
    with pytest.raises(SystemExit):
        coz.generate_sdf_font_glyphs()


def test_escape_hatch(monkeypatch):
    def behaviour(url, n):
        if url.endswith("/8192-8447.pbf"):
            raise _http(500)
        return _Resp(b"g")
    _install(monkeypatch, behaviour)
    monkeypatch.setenv("STREETZIM_ALLOW_FONT_ERRORS", "1")
    fonts = coz.generate_sdf_font_glyphs()
    assert ("OpenSansRegular", "8192-8447") not in fonts
