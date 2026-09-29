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
          "Publisher": 0, "Scraper": 0}

TEXTS = [
    "Monaco", "  padded\t\n", "", "   ", "\t\r\n", "a\x00b", "a\x07b\x1bc",
    "line\nbreak", "tab\there", "zero​width", "soft­hyphen", "bidi‮flip",
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
