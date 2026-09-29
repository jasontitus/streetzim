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
- downloads: stream_file, with its retrying session;
- the output folder check: validate_file_creatable.

The ZIM itself is still written by create_osm_zim (python-libzim directly,
not zimscraperlib's Creator): see docs/zimfarm.md.
"""
from __future__ import annotations

import io
from pathlib import Path

try:
    from zimscraperlib.download import stream_file  # pyright: ignore[reportMissingImports]
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


def illustration_png(data: bytes, size: int) -> bytes:
    """Any image zimscraperlib reads (SVG included) -> a size x size PNG,
    cropped to fill, as maps2zim makes its illustration."""
    try:
        src = io.BytesIO(data)
        fmt = format_for(src, from_suffix=False)
        png = io.BytesIO()
        if fmt == "SVG":
            convert_svg2png(src, png, size, size)
        else:
            # RGBA first: resize_image would otherwise scale a palette
            # ("P") image in palette space and garble its colours.
            convert_image(src, png, fmt="PNG", colorspace="RGBA")
        png.seek(0)
        resize_image(png, width=size, height=size, method="cover")
        return bytes(_md.DefaultIllustrationMetadata(png.getvalue()).value)
    except Exception as e:
        raise ValueError(f"Illustration: not a usable image ({e})") from e


def download(url: str, *, user_agent: str, dest: Path | None = None) -> bytes | None:
    """Fetch `url` with zimscraperlib's retrying session: into `dest` when
    given (returns None), else into memory (returns the bytes)."""
    headers = {"User-Agent": user_agent}
    if dest is not None:
        stream_file(url, fpath=dest, headers=headers, block_size=1 << 20)
        return None
    buf = io.BytesIO()
    stream_file(url, byte_stream=buf, headers=headers)
    return buf.getvalue()


def check_output(folder: Path, filename: str) -> None:
    """OSError unless `filename` can be created in `folder`.

    zimscraperlib creates the file and deletes it again, so never pass the
    name of a file that must survive (the finished ZIM under --overwrite)."""
    try:
        validate_file_creatable(folder, filename)
    except Exception as e:
        raise OSError(str(e)) from e
