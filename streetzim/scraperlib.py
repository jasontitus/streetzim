"""zimscraperlib, where it is installed: the openZIM rules and helpers the
`streetzim` command uses.

zimscraperlib 5.x requires Python 3.14, so it is installed there (the Docker
image, CI's 3.14 job) and not on 3.12, where the builder also runs.
`streetzim.zim_metadata` calls into this module when AVAILABLE and falls back
to its own copy of the same rules otherwise; tests/test_scraperlib_parity.py
checks, on 3.14, that the two agree.

What is used from zimscraperlib:
- metadata validation: the Name/Title/Description/... classes of
  zimscraperlib.zim.metadata (cleanup, grapheme limits, tag de-duplication);
- the illustration: format detection, SVG to PNG, conversion, the "cover"
  resize to 48x48, and the final 48x48 PNG check;
- downloads: stream_file, and its retrying session for the resumable
  MBTiles download (streetzim/download.py);
- the output folder check: validate_file_creatable.

The ZIM itself is still written by create_osm_zim (python-libzim directly,
not zimscraperlib's Creator): see docs/zimfarm.md.
"""
from __future__ import annotations

import io
import zlib
from pathlib import Path
from typing import Any

try:
    from zimscraperlib.download import (  # pyright: ignore[reportMissingImports]
        get_session, stream_file)
    from zimscraperlib.image.conversion import (  # pyright: ignore[reportMissingImports]
        convert_image, convert_svg2png)
    from zimscraperlib.image.probing import format_for  # pyright: ignore[reportMissingImports]
    from zimscraperlib.image.transformation import resize_image  # pyright: ignore[reportMissingImports]
    from zimscraperlib.zim import metadata as _md  # pyright: ignore[reportMissingImports]
    from zimscraperlib.zim.filesystem import (  # pyright: ignore[reportMissingImports]
        validate_file_creatable)
    AVAILABLE = True
except ImportError:
    AVAILABLE = False

_TEXT = {
    "Name": "NameMetadata",
    "Title": "TitleMetadata",
    "Description": "DescriptionMetadata",
    "LongDescription": "LongDescriptionMetadata",
    "Creator": "CreatorMetadata",
    "Publisher": "PublisherMetadata",
    "Scraper": "ScraperMetadata",
    "Flavour": "FlavourMetadata",
}


def text(key: str, value: str) -> str:
    """The cleaned value of text metadata `key`; ValueError if openZIM rejects it."""
    cls = getattr(_md, _TEXT[key])
    try:
        return cls(value).value
    except ValueError as e:
        limit = getattr(cls, "oz_max_length", 0)
        cleaned = _md.clean_str(value)
        if limit and _md.nb_grapheme_for(cleaned) > limit:
            # zimscraperlib's message gives the limit but not the length.
            raise ValueError(f"{key} is too long: {_md.nb_grapheme_for(cleaned)} "
                             f"characters, openZIM allows {limit}") from e
        raise ValueError(f"{key}: {e}") from e


def tags(values: list[str]) -> list[str]:
    """Cleaned, de-duplicated tags; ValueError on an empty tag."""
    try:
        return list(_md.TagsMetadata(values).value)
    except ValueError as e:
        raise ValueError(f"Tags: {e}") from e


def _is_svg(data: bytes) -> bool:
    """SVG or gzipped SVG (.svgz). libmagic, which zimscraperlib's format_for
    falls back on, calls an SVG without an <?xml> declaration that starts
    with whitespace, a comment or a BOM plain text, and misses .svgz."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:4] in (
            b"RIFF", b"GIF8"):
        return False
    head = data[:65536]
    if head[:2] == b"\x1f\x8b":
        try:   # a bounded peek: never inflate more than 64 KB here
            head = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(head, 65536)
        except zlib.error:
            return False
    return b"<svg" in head[:8192]


def illustration_png(data: bytes, size: int) -> bytes:
    """Any image zimscraperlib reads (SVG and .svgz included) -> a size x size
    PNG, cropped to fill, as maps2zim makes its illustration."""
    try:
        src = io.BytesIO(data)
        png = io.BytesIO()
        if _is_svg(data):
            # Render wide enough to crop from, keeping the aspect ratio
            # (cairosvg scales the height to the width); the "cover" resize
            # below then crops it to a square, as for any other image.
            convert_svg2png(src, png, size * 4)
        else:
            format_for(src, from_suffix=False)    # UnidentifiedImageError if not an image
            # RGBA first: resize_image would otherwise scale a palette
            # ("P") image in palette space and garble its colours.
            convert_image(src, png, fmt="PNG", colorspace="RGBA")
        png.seek(0)
        resize_image(png, width=size, height=size, method="cover")
        return bytes(_md.DefaultIllustrationMetadata(png.getvalue()).value)
    except Exception as e:
        raise ValueError(f"Illustration: not a usable image ({e})") from e


# Per read (not total) timeout. zimscraperlib's default is 10 s, and a
# mid-body stall is not retried, so a multi-GB extract would fail on one
# pause; urllib, used before (and still on 3.12), waited 60 s.
READ_TIMEOUT = 60


def _quick_session():
    """For small files that are checked before a build (the illustration):
    two retries, a few seconds apart, instead of zimscraperlib's default
    (five, 30 s backoff: about 8 minutes before a bad URL fails)."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    session = requests.Session()
    session.mount("http", HTTPAdapter(max_retries=Retry(
        total=2, backoff_factor=1, redirect=False,
        status_forcelist=[429, 500, 502, 503, 504])))
    return session


def download(url: str, *, user_agent: str, dest: Path | None = None) -> bytes | None:
    """Fetch `url` with zimscraperlib's stream_file: into `dest` when given
    (returns None; zimscraperlib's retrying session), else into memory
    (returns the bytes; the quick session, for small files)."""
    headers = {"User-Agent": user_agent}
    if dest is not None:
        stream_file(url, fpath=dest, headers=headers, block_size=1 << 20,
                    timeout=READ_TIMEOUT)
        return None
    buf = io.BytesIO()
    stream_file(url, byte_stream=buf, headers=headers, session=_quick_session(),
                timeout=30)
    return buf.getvalue()


def session() -> Any:
    """zimscraperlib's retrying requests session (5 retries, backoff), for
    downloads streamed by the caller (streetzim/download.py)."""
    return get_session(5)


def check_output(folder: Path, filename: str) -> None:
    """OSError unless `filename` can be created in `folder`.

    zimscraperlib creates the file and deletes it again, so never pass the
    name of a file that must survive (the finished ZIM under --overwrite)."""
    try:
        validate_file_creatable(folder, filename)
    except Exception as e:
        raise OSError(str(e)) from e
