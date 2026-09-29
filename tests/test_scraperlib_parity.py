"""The copy of openZIM's metadata rules in streetzim/zim_metadata.py (used on
Python 3.12, where zimscraperlib 5.x cannot be installed) must decide exactly
as zimscraperlib does (used on 3.14). Runs where zimscraperlib is installed."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("regex")
from streetzim import scraperlib  # noqa: E402
from streetzim import zim_metadata as zm  # noqa: E402

pytestmark = pytest.mark.skipif(not scraperlib.AVAILABLE,
                                reason="zimscraperlib not installed (Python < 3.14)")

LIMITS = {"Name": 0, "Title": zm.TITLE_MAX, "Description": zm.DESCRIPTION_MAX,
          "LongDescription": zm.LONG_DESCRIPTION_MAX, "Creator": 0,
          "Publisher": 0, "Scraper": 0, "Flavour": 0}

TEXTS = [
    "Monaco", "  padded\t\n", "", "   ", "\t\r\n", "a\x00b", "a\x07b\x1bc",
    "line\nbreak", "tab\there", "zero\u200bwidth", "soft\xadhyphen", "bidi\u202eflip",
    "é" * 30, "é" * 31, "🇫🇷" * 30, "🇫🇷" * 31,
    "👩‍👩‍👧‍👦" * 30, "👩‍👩‍👧‍👦" * 31, "x" * 30, "x" * 31, "x" * 80, "x" * 81,
    "y" * 4000, "y" * 4001, " " + "x" * 30 + " ", "﻿bom", "private",
    "osm_en_monaco", "Überall ☕", "日本語の地図",
]


def _outcome(fn, *args):
    try:
        return ("ok", fn(*args))
    except ValueError:
        return ("rejected", None)


@pytest.mark.parametrize("key", sorted(LIMITS))
@pytest.mark.parametrize("value", TEXTS)
def test_text_rules_match(key, value):
    ours = _outcome(zm._text, key, value, LIMITS[key])
    theirs = _outcome(scraperlib.text, key, value)
    assert ours == theirs


@pytest.mark.parametrize("value", [
    "maps", "maps;osm", "a;b;a", "a;;b", ";a", "a;", " a ; b ", "a;\tb",
    "_ftindex:yes;_ftindex:yes", "_category:maps;_pictures:no", "x\x00y;z", " ; ",
])
def test_tag_rules_match(value):
    ours = _outcome(zm.parse_tags, value)
    theirs = _outcome(scraperlib.tags, value.split(";"))
    assert ours == theirs


def _mirror_illustration(data):
    """zim_metadata's own illustration path, as it runs without zimscraperlib."""
    from unittest import mock
    with mock.patch.object(zm, "_scraperlib", return_value=False):
        return zm.illustration_png(data)


@pytest.mark.parametrize("size,mode,fmt", [
    ((64, 64), "RGB", "PNG"), ((200, 100), "RGB", "JPEG"), ((100, 200), "RGBA", "PNG"),
    ((10, 10), "RGB", "PNG"), ((300, 300), "P", "PNG"), ((120, 80), "RGB", "WEBP"),
])
def test_illustration_matches(size, mode, fmt):
    from PIL import Image, ImageChops, ImageStat
    w, h = size
    img = Image.new("RGB", size)
    img.putdata([(x * 255 // w, y * 255 // h, 128) for y in range(h) for x in range(w)])
    buf = io.BytesIO()
    img.convert(mode).save(buf, fmt)
    a = Image.open(io.BytesIO(_mirror_illustration(buf.getvalue())))
    b = Image.open(io.BytesIO(zm.illustration_png(buf.getvalue())))
    assert (a.format, a.size) == (b.format, b.size) == ("PNG", (48, 48))
    # Both crop to fill; resampling filters differ, the picture must not.
    diff = ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB"))).mean
    assert max(diff) < 12, diff


def _random_text(rng, n):
    pools = [range(0x20, 0x7f), range(0x00, 0x20), range(0x80, 0x250), range(0x2000, 0x2070),
             range(0x300, 0x370), range(0x1f300, 0x1f6ff), range(0xe000, 0xe010),
             range(0xfe00, 0xfe10), [0x200d, 0xfeff, 0xa0, 0x3000, 0x85]]
    return "".join(chr(rng.choice(rng.choice(pools))) for _ in range(n))


def test_fuzz_text_and_tags_match():
    import random
    rng = random.Random(1729)
    for _ in range(1500):
        v = _random_text(rng, rng.choice([0, 1, 2, 5, 29, 30, 31, 79, 81, 200]))
        for key in ("Title", "Description", "Name"):
            assert _outcome(zm._text, key, v, LIMITS[key]) == \
                _outcome(scraperlib.text, key, v), (key, v)
        tags = ";".join(_random_text(rng, rng.choice([0, 1, 3, 8]))
                        for _ in range(rng.randint(1, 4)))
        assert _outcome(zm.parse_tags, tags) == \
            _outcome(scraperlib.tags, tags.split(";")), tags


# ------------------------------------------------ zimscraperlib path only


RECT = (b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="32">'
        b'<rect width="64" height="32" fill="#22aa77"/></svg>')


@pytest.mark.parametrize("data", [
    RECT,
    b"\n\n  " + RECT,                                  # no <?xml>, leading whitespace
    b"<!-- icon -->\n" + RECT,                          # leading comment
    b"\xef\xbb\xbf" + RECT,                            # byte-order mark
    __import__("gzip").compress(RECT),                 # .svgz
])
def test_svg_variants_crop_to_fill(data):
    from PIL import Image
    img = Image.open(io.BytesIO(zm.illustration_png(data))).convert("RGBA")
    assert img.size == (48, 48)
    # Cropped to fill: no transparent letterbox bands at the edges.
    for xy in [(24, 0), (24, 47), (0, 24), (47, 24), (24, 24)]:
        assert img.getpixel(xy)[3] == 255, xy


def test_download_timeouts_and_retries(monkeypatch):
    calls = []
    monkeypatch.setattr(scraperlib, "stream_file",
                        lambda url, **kw: calls.append(kw) or (0, {}))
    scraperlib.download("https://example.org/x.pbf", user_agent="t", dest=Path("x"))
    scraperlib.download("https://example.org/icon.png", user_agent="t")
    big, small = calls
    assert big["timeout"] >= 60 and "session" not in big
    retry = small["session"].get_adapter("https://example.org").max_retries
    assert small["timeout"] <= 30 and retry.total <= 2 and retry.backoff_factor <= 1
