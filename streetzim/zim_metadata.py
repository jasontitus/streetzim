"""openZIM metadata rules for the flags --title, --description, --tags, ...

These mirror zimscraperlib 5.4 (zimscraperlib/zim/metadata.py), which
maps2zim uses. We don't depend on zimscraperlib itself: every 5.x release
requires Python 3.14, while this builder runs on Ubuntu 24.04's 3.12 with
rasterio and osmium. When the image moves to 3.14 this module can become a
thin wrapper around zimscraperlib.

Validation runs when the flags are parsed, so a bad title fails in a second,
not after a multi-hour build. Nothing here is imported on the default build
path, so it adds no dependency unless the flags are used.
"""
from __future__ import annotations

import io
import re

# zimscraperlib.constants
TITLE_MAX = 30
DESCRIPTION_MAX = 80
LONG_DESCRIPTION_MAX = 4000
ILLUSTRATION_SIZE = 48


def nb_graphemes(value: str) -> int:
    """Visually perceived characters, as zimscraperlib counts them (regex \\X)."""
    import regex  # listed in requirements.txt; only needed for these flags
    return len(regex.findall(r"\X", value))


def clean_str(value: str) -> str:
    """Drop control characters except newline/tab/CR, strip the ends."""
    import regex
    return regex.sub(r"(?![\n\t\r])\p{C}", "", value).strip(" \r\n\t")


def _text(label: str, value: str, max_len: int = 0) -> str:
    value = clean_str(value)
    if not value.strip():
        raise ValueError(f"{label}: empty value not allowed")
    if max_len and nb_graphemes(value) > max_len:
        raise ValueError(f"{label} is too long: {nb_graphemes(value)} characters, "
                         f"openZIM allows {max_len}")
    return value


def parse_tags(value: str) -> list[str]:
    """Semicolon-delimited tags, cleaned and de-duplicated (order kept)."""
    tags = [clean_str(t) for t in value.split(";")]
    if not all(t.strip() for t in tags):
        raise ValueError(f"Tags: empty tag in {value!r}")
    return list(dict.fromkeys(tags))


def build_overrides(*, name: str | None = None, title: str | None = None,
                    description: str | None = None,
                    long_description: str | None = None,
                    creator: str | None = None, publisher: str | None = None,
                    tags: str | None = None,
                    scraper: str | None = None) -> dict[str, str | list[str]]:
    """Validated metadata values keyed by ZIM metadata name. Only the values
    actually given are returned; the builder keeps its defaults for the rest."""
    md: dict[str, str | list[str]] = {}
    if name is not None:
        md["Name"] = _text("Name", name)
    if title is not None:
        md["Title"] = _text("Title", title, TITLE_MAX)
    if description is not None:
        md["Description"] = _text("Description", description, DESCRIPTION_MAX)
    if long_description is not None:
        md["LongDescription"] = _text("LongDescription", long_description,
                                      LONG_DESCRIPTION_MAX)
    if creator is not None:
        md["Creator"] = _text("Creator", creator)
    if publisher is not None:
        md["Publisher"] = _text("Publisher", publisher)
    if tags is not None:
        md["Tags"] = parse_tags(tags)
    if scraper is not None:
        md["Scraper"] = _text("Scraper", scraper)
    return md


def merge_tags(builder_tags: str, extra: list[str] | None) -> str:
    """The builder's tags followed by the user's, without duplicates. A user
    tag of the form `_key:value` replaces the builder's `_key:` tag, as
    maps2zim merges them (e.g. `_pictures:no`)."""
    user = list(extra or [])
    user_keys = {t.split(":", 1)[0] for t in user if t.startswith("_") and ":" in t}
    tags = [t for t in builder_tags.split(";")
            if t and not (t.startswith("_") and t.split(":", 1)[0] in user_keys)]
    return ";".join(dict.fromkeys(tags + [t for t in user if t]))


def illustration_png(data: bytes) -> bytes:
    """Any image Pillow can read -> a 48x48 PNG, cropped to fill ("cover",
    as maps2zim does). SVG is not supported (it would need cairosvg)."""
    from PIL import Image, ImageOps
    if data.lstrip()[:5] in (b"<?xml", b"<svg ") or b"<svg" in data[:512]:
        raise ValueError("Illustration: SVG is not supported; give a PNG, JPEG or WebP")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ValueError(f"Illustration: not a readable image ({e})") from e
    img = ImageOps.fit(img.convert("RGBA"), (ILLUSTRATION_SIZE, ILLUSTRATION_SIZE),
                       Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def load_illustration(src: str, *, timeout: int = 30) -> bytes:
    """Read an illustration from a local path, file:// or http(s) URL."""
    if re.match(r"^https?://", src, re.I):
        import urllib.request
        req = urllib.request.Request(src, headers={
            "User-Agent": "streetzim (https://github.com/jasontitus/streetzim)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    else:
        path = src[len("file://"):] if src.startswith("file://") else src
        with open(path, "rb") as f:
            data = f.read()
    return illustration_png(data)
