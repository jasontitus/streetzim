"""Languages a StreetZim can be built in (create_osm_zim --language).

A build in language ``xx`` labels the map with OSM's ``name:xx`` (falling
back to the place's own name), names search results the same way, bundles
``xx`` Wikipedia articles and ``xx`` Wikidata descriptions, and records the
language in the ZIM metadata. The viewer's own buttons and messages are
English for now.

Codes: ISO 639-1 (what OSM's ``name:xx`` and Wikipedia's ``xx.wikipedia.org``
use) -> ISO 639-3 (the ZIM ``Language`` metadata, openZIM's rule).
"""
from __future__ import annotations

ISO639_3 = {
    "en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita",
    "pt": "por", "nl": "nld", "ru": "rus", "uk": "ukr", "pl": "pol",
    "cs": "ces", "sk": "slk", "sv": "swe", "no": "nor", "nb": "nob",
    "da": "dan", "fi": "fin", "is": "isl", "et": "est", "lv": "lav",
    "lt": "lit", "hu": "hun", "ro": "ron", "bg": "bul", "el": "ell",
    "tr": "tur", "hr": "hrv", "sr": "srp", "sl": "slv", "ca": "cat",
    "eu": "eus", "gl": "glg", "ga": "gle", "cy": "cym",
    "zh": "zho", "ja": "jpn", "ko": "kor", "vi": "vie", "th": "tha",
    "id": "ind", "ms": "msa", "tl": "tgl", "hi": "hin", "bn": "ben",
    "ur": "urd", "ta": "tam", "te": "tel", "mr": "mar", "ne": "nep",
    "ar": "ara", "fa": "fas", "he": "heb", "sw": "swa", "am": "amh",
}

# Right-to-left scripts: the viewer's layout is left-to-right; labels and
# search results still render correctly (MapLibre's RTL plugin).
RTL = frozenset({"ar", "fa", "he", "ur"})


# Wikipedia editions whose code is not the language's ISO 639-1 code.
WIKI_CODE = {"nb": "no"}


def wiki_code(lang: str) -> str:
    """The Wikipedia edition of ``lang``: xx.wikipedia.org, site xxwiki."""
    return WIKI_CODE.get(lang, lang)


def iso639_3(lang: str) -> str:
    try:
        return ISO639_3[lang]
    except KeyError:
        raise ValueError(
            f"unsupported language {lang!r}: one of {', '.join(sorted(ISO639_3))}") from None


def check(lang: str | None) -> str:
    """The language code a build uses ("en" when none is given)."""
    lang = (lang or "en").strip().lower()
    iso639_3(lang)
    return lang
