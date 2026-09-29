"""Font glyph download: transient errors are retried, ranges the lock file
records as absent are skipped, a range that still fails stops the build (it
used to ship silently as missing glyphs; seen as 1-9 of 768 ranges in
CI-sized builds), and every range is checked against its pinned SHA-256
(tests/test_viewer_assets.py covers the pinning itself)."""
from __future__ import annotations

import hashlib
import io
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import create_osm_zim as coz  # noqa: E402
from streetzim import viewer_assets  # noqa: E402

STACKS = {"OpenSansRegular": "Open Sans Regular", "OpenSansBold": "Open Sans Bold"}
RANGES = [f"{s}-{s + 255}" for s in range(0, 65536, 256)]
ABSENT = "768-1023"


def content(url: str) -> bytes:
    """What the fake CDN serves for a URL (distinct per range and font)."""
    return b"glyphs " + url.encode()


def url_of(stack: str, r: str) -> str:
    return f"https://fonts.example/{STACKS[stack].replace(' ', '%20')}/{r}.pbf"


def make_lock() -> dict:
    ranges = {s: {r: (None if r == ABSENT else hashlib.sha256(content(url_of(s, r))).hexdigest())
                  for r in RANGES} for s in STACKS}
    return {"fonts": {"base_url": "https://fonts.example", "fontstacks": STACKS, "ranges": ranges}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(viewer_assets, "BAKED", tmp_path / "baked")


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
        return _Resp(content(url))
    calls = _install(monkeypatch, behaviour)
    fonts = coz.generate_sdf_font_glyphs(make_lock())
    assert fonts[("OpenSansRegular", "256-511")] == content(url_of("OpenSansRegular", "256-511"))
    assert fonts[("OpenSansBold", "512-767")] == content(url_of("OpenSansBold", "512-767"))
    assert ("OpenSansRegular", ABSENT) not in fonts           # absent: not even requested
    assert not any(u.endswith(f"/{ABSENT}.pbf") for u in calls)
    assert len(fonts) == len(STACKS) * (len(RANGES) - 1)


def test_persistent_failure_stops_the_build(monkeypatch):
    def behaviour(url, n):
        if url.endswith("/8192-8447.pbf"):
            raise _http(500)
        return _Resp(content(url))
    calls = _install(monkeypatch, behaviour)
    with pytest.raises(SystemExit, match="failed to download"):
        coz.generate_sdf_font_glyphs(make_lock())
    assert calls[url_of("OpenSansRegular", "8192-8447")] == 5


def test_pinned_range_gone_from_the_cdn_is_a_failure(monkeypatch):
    def behaviour(url, n):
        if url.endswith("/8192-8447.pbf"):
            raise _http(404)
        return _Resp(content(url))
    _install(monkeypatch, behaviour)
    with pytest.raises(SystemExit, match="failed to download"):
        coz.generate_sdf_font_glyphs(make_lock())


def test_escape_hatch(monkeypatch):
    def behaviour(url, n):
        if url.endswith("/8192-8447.pbf"):
            raise _http(500)
        return _Resp(content(url))
    _install(monkeypatch, behaviour)
    monkeypatch.setenv("STREETZIM_ALLOW_FONT_ERRORS", "1")
    fonts = coz.generate_sdf_font_glyphs(make_lock())
    assert ("OpenSansRegular", "8192-8447") not in fonts


def test_changed_content_stops_the_build_even_with_the_escape_hatch(monkeypatch):
    def behaviour(url, n):
        if url.endswith("/0-255.pbf"):
            return _Resp(b"tampered")
        return _Resp(content(url))
    _install(monkeypatch, behaviour)
    monkeypatch.setenv("STREETZIM_ALLOW_FONT_ERRORS", "1")
    with pytest.raises(SystemExit, match="do not match their pinned sha256"):
        coz.generate_sdf_font_glyphs(make_lock())


def test_second_build_is_served_from_the_cache(monkeypatch):
    _install(monkeypatch, lambda url, n: _Resp(content(url)))
    first = coz.generate_sdf_font_glyphs(make_lock())

    def offline(url, n):
        raise OSError("network is unreachable")
    _install(monkeypatch, offline)
    assert coz.generate_sdf_font_glyphs(make_lock()) == first
