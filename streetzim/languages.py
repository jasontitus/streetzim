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

import os
import re

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
    # Switzerland's fourth national language (its tiles carry name:rm;
    # a Romansh language-pack ZIM needs the metadata code).
    "rm": "roh",
}

# Other regions' own languages that a short-row map's tiles and search
# records carry (default_tile_languages): codes --tile-languages accepts and
# ZIM metadata can name (iso639_3), but no --language build is made in them
# (check, the CLI's --language choices: ISO639_3 only).
REGION_ISO639_3 = {
    "sq": "sqi", "mk": "mkd", "bs": "bos", "cnr": "cnr", "lb": "ltz",
    "fy": "fry", "sc": "srd", "az": "aze", "ku": "kur", "glk": "glk",
    "mzn": "mzn", "mn": "mon", "za": "zha", "tt": "tat", "ce": "che",
    "ht": "hat", "jam": "jam", "pap": "pap", "qu": "que", "gn": "grn",
    "ay": "aym", "haw": "haw", "esu": "esu", "zgh": "zgh", "kab": "kab",
    "ty": "tah", "rar": "rar",
}

# Right-to-left scripts: the viewer's layout is left-to-right; labels and
# search results still render correctly (MapLibre's RTL plugin). Gilaki
# and Mazandarani (Iran's tiles) are written in Arabic script too.
RTL = frozenset({"ar", "fa", "he", "ur", "glk", "mzn"})


# Wikipedia editions whose code is not the language's ISO 639-1 code.
WIKI_CODE = {"nb": "no"}


def wiki_code(lang: str) -> str:
    """The Wikipedia edition of ``lang``: xx.wikipedia.org, site xxwiki."""
    return WIKI_CODE.get(lang, lang)


# ISO 639-3 codes a Kiwix Wikipedia ZIM in the language may declare.
ZIM_LANGUAGE_ALIASES = {"nb": {"nob", "nor"}, "no": {"nob", "nor"}, "ms": {"msa", "zsm"},
                        "fa": {"fas", "pes"}, "zh": {"zho", "chi"}}


def zim_matches(lang: str, zim_language: str) -> bool:
    """Whether a ZIM's Language metadata ("fra", "eng,fra") includes
    ``lang``."""
    want = ZIM_LANGUAGE_ALIASES.get(lang, set()) | {iso639_3(lang)}
    return bool(want & {x.strip() for x in zim_language.split(",") if x.strip()})


def iso639_3(lang: str) -> str:
    """The ISO 639-3 code of a build or region language (ISO639_3,
    REGION_ISO639_3)."""
    code = ISO639_3.get(lang) or REGION_ISO639_3.get(lang)
    if code is None:
        raise ValueError(
            f"unsupported language {lang!r}: one of "
            f"{', '.join(sorted(set(ISO639_3) | set(REGION_ISO639_3)))}")
    return code


def check(lang: str | None) -> str:
    """The language code a build uses ("en" when none is given): one of
    ISO639_3."""
    lang = (lang or "en").strip().lower()
    if lang not in ISO639_3:
        raise ValueError(
            f"unsupported language {lang!r}: one of {', '.join(sorted(ISO639_3))}")
    return lang


# The name:xx languages a multilingual ("language-ready") map carries in its
# tiles besides English, which the tiles always carry as name:latin
# (docs/language-ready.md; chosen in docs/language-packs.md: no dialects,
# transliterations or mass-tagged imports).
MATRIX_TILE_LANGUAGES = ("ar", "de", "el", "es", "fa", "fr", "he", "hi", "id", "it", "ja",
                         "ko", "nl", "pl", "pt", "ru", "th", "tr", "uk", "vi", "zh")


# A map whose row in cloud/region-languages.tsv lists at most this many
# languages (a country, or a few: Switzerland's de,fr,it,rm, the Baltics,
# the Balkans' seven) carries all of them in its tiles besides the matrix
# (default_tile_languages), so its own minority and national languages
# label the map and name its search records (Romansh in Graubünden). A
# longer row (Europe's 38, Africa's 31, the Indian subcontinent's 22) adds
# none: dozens of languages per continent map, each mostly unlabelled.
REGION_TILE_ROW_MAX = 7


def default_tile_languages(region_langs) -> list[str]:
    """The name:xx languages a language-ready map's tiles carry by default:
    the matrix, plus the region's own languages when its row is short
    (REGION_TILE_ROW_MAX), sorted (tile_languages). A code the matrix
    lacks must be in ISO639_3 or REGION_ISO639_3
    (tests/test_region_languages.py checks the table)."""
    region = [x for x in (region_langs or []) if x != "en"]
    extra = ([x for x in region if x not in MATRIX_TILE_LANGUAGES]
             if len(list(region_langs or [])) <= REGION_TILE_ROW_MAX else [])
    return tile_languages(",".join(list(MATRIX_TILE_LANGUAGES) + extra))


def first_name(value: object) -> str:
    """The first non-empty name of an OSM ";" list, trimmed: the tile
    profile's FirstName ("洛杉矶;洛杉磯" -> "洛杉矶")."""
    if not isinstance(value, str):
        return ""
    for part in value.split(";"):
        part = part.strip()
        if part:
            return part
    return ""


def osm_names(tags: dict, languages) -> dict[str, str]:
    """{lang: name} of OSM's name:<lang> tags for ``languages``, chosen as
    the tile profile writes name:<lang> (resources/tilemaker/
    process-openmaptiles.lua LangName): the first of a ";" list, and for zh
    name:zh-Hans (Simplified) when OSM has it. Languages without one are
    left out."""
    out: dict[str, str] = {}
    for lang in languages:
        v = first_name(tags.get("name:zh-Hans")) if lang == "zh" else ""
        v = v or first_name(tags.get(f"name:{lang}"))
        if v:
            out[lang] = v
    return out


def search_languages(tile_langs, region_langs) -> list[str]:
    """The languages a language-ready map's search records name places in
    (``ln``, docs/search-records.md): its tile languages, then its region's
    own languages that are not among them when its row is short
    (REGION_TILE_ROW_MAX, the tiles' rule: Europe's 38 add none). Empty
    without tile languages: a build that is not language-ready writes no
    ``ln``. English never: the record's ``n`` is already the English /
    Latin name."""
    if not tile_langs:
        return []
    region = list(region_langs or [])
    if len(region) > REGION_TILE_ROW_MAX:
        region = []
    out: list[str] = []
    for lang in list(tile_langs) + region:
        if lang != "en" and lang not in out:
            out.append(lang)
    # A cap for a map whose search data is near its budget (Europe's
    # manifest): STREETZIM_SEARCH_LN_LANGUAGES keeps only these, in this
    # order. Read here, so the extraction and the writer agree.
    only = os.environ.get("STREETZIM_SEARCH_LN_LANGUAGES")
    if only is not None:
        keep = set(_codes(only))
        out = [x for x in out if x in keep]
    return out


def _codes(text: str) -> list[str]:
    return [c for c in re.split(r"[\s,]+", text.strip().lower()) if c]


def tile_languages(text: str) -> list[str]:
    """The name:xx languages ``text`` ("de,fr", "fr de") asks tiles to carry,
    as STREETZIM_TILE_LANGUAGES and map-config ``tileLanguages`` hold them:
    codes a build can be made in, or a region language (REGION_ISO639_3),
    sorted, once each. English is refused (the
    tiles always carry it as name:latin), and so is an empty list."""
    langs = sorted(set(_codes(text)))
    if not langs:
        raise ValueError("no tile languages given")
    if "en" in langs:
        raise ValueError("en is not a tile language: the tiles always carry English "
                         "(name:latin)")
    for lang in langs:
        iso639_3(lang)
    return langs


_REGION_CODE = re.compile(r"[a-z]{2,3}")


def region_languages(text: str) -> list[str]:
    """A region's own languages (cloud/region-languages.tsv; map-config
    ``regionLanguages``): ISO 639 codes, largest first, once each. Any
    two- or three-letter code: a region's language need not be one a build
    can be made in (ht, ka)."""
    langs: list[str] = []
    for code in _codes(text):
        if not _REGION_CODE.fullmatch(code):
            raise ValueError(f"region language {code!r} is not an ISO 639 code "
                             "(two or three letters)")
        if code not in langs:
            langs.append(code)
    if not langs:
        raise ValueError("no region languages given")
    return langs
