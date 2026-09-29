"""openZIM metadata rules for the flags --title, --description, --tags, ...

Where zimscraperlib is installed (Python 3.14: the Docker image, CI's 3.14
job) the checks are zimscraperlib's own, through streetzim.scraperlib. On
3.12, where the builder also runs and zimscraperlib 5.x cannot be installed,
this module applies its copy of the same rules (zimscraperlib 5.4,
zimscraperlib/zim/metadata.py). tests/test_scraperlib_parity.py runs both on
the same inputs under 3.14 and fails if they disagree.

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
USER_AGENT = "streetzim (https://github.com/jasontitus/streetzim)"


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
    """Semicolon-delimited tags, cleaned and de-duplicated (order kept).
    Exactly zimscraperlib's rule: only a tag that is empty after cleanup is
    refused. build_overrides also refuses whitespace-only tags."""
    tags = [clean_str(t) for t in value.split(";")]
    if not all(tags):
        raise ValueError(f"Tags: empty tag in {value!r}")
    return list(dict.fromkeys(tags))


def _scraperlib() -> bool:
    from streetzim import scraperlib
    return scraperlib.AVAILABLE


def _check(label: str, value: str, max_len: int = 0) -> str:
    if _scraperlib():
        from streetzim import scraperlib
        return scraperlib.text(label, value)
    return _text(label, value, max_len)


def _check_tags(value: str) -> list[str]:
    # Stricter than zimscraperlib, on both paths: clean_str strips only
    # ASCII whitespace, so "maps; " with a no-break space would pass as a
    # blank tag.
    if any(not t.strip() for t in value.split(";")):
        raise ValueError(f"Tags: empty tag in {value!r}")
    if _scraperlib():
        from streetzim import scraperlib
        return scraperlib.tags(value.split(";"))
    return parse_tags(value)


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
        md["Name"] = _check("Name", name)
    if title is not None:
        md["Title"] = _check("Title", title, TITLE_MAX)
    if description is not None:
        md["Description"] = _check("Description", description, DESCRIPTION_MAX)
    if long_description is not None:
        md["LongDescription"] = _check("LongDescription", long_description,
                                      LONG_DESCRIPTION_MAX)
    if creator is not None:
        md["Creator"] = _check("Creator", creator)
    if publisher is not None:
        md["Publisher"] = _check("Publisher", publisher)
    if tags is not None:
        md["Tags"] = _check_tags(tags)
    if scraper is not None:
        md["Scraper"] = _check("Scraper", scraper)
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
    """An image -> a 48x48 PNG, cropped to fill ("cover", as maps2zim does).
    With zimscraperlib, anything it reads, SVG included; without it, what
    Pillow reads (no SVG)."""
    if _scraperlib():
        from streetzim import scraperlib
        return scraperlib.illustration_png(data, ILLUSTRATION_SIZE)
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
    if re.match(r"^https?://", src, re.I) and _scraperlib():
        from streetzim import scraperlib
        data = scraperlib.download(src, user_agent=USER_AGENT) or b""
    elif re.match(r"^https?://", src, re.I):
        import urllib.request
        req = urllib.request.Request(src, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    else:
        path = src[len("file://"):] if src.startswith("file://") else src
        with open(path, "rb") as f:
            data = f.read()
    return illustration_png(data)
