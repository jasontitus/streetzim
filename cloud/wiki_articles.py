"""Bundle full Wikipedia article pages into the streetzim (option B).

kiwix-serve can't deep-link from one ZIM into another, so an offline
streetzim that wants tappable/narratable Wikipedia articles must carry its
own copies. This fetches each linkable article (the `wikipedia` titles in
the cross-ref index — OSM `wikipedia=` tags plus any backfilled from
wikidata Q-IDs), trims it to a compact reader page, and stores it at
`wiki-article/<Title>`.

mcpzim resolves that path in `ZimService.articleByTitle`, and its narration
cleaner (`ArticleSections.stripHTML`) further de-noises for TTS, so the
pages narrate well through Kokoro. See docs/wikidata-title-resolution.md
and the mcpzim BundledArticleTests / ArticleSpeechCleanupTests.

Measured (California): the linkable set bundles to ~0.2-1% of the ZIM as
trimmed reader HTML. Off by default (`--bundle-wiki-articles`).

Sources: a local Wikipedia ZIM (offline, fast — pass `offline_zim`) or the
public Wikipedia API (cached to disk; cloud/wikimedia_http.py paces the
requests and honours Retry-After). Public data only; the User-Agent names
the project's public issue tracker (an operator may add an address with
STREETZIM_WIKI_CONTACT). Wikipedia text is CC BY-SA — every page keeps a
source link + license footer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from cloud.wikimedia_http import (
    Pacer,
    TransientError,
    api_error_code,
    env_number,
    get_json,
    polite_pacer,
    require_complete,
    stop_error,
)
from cloud.wikimedia_http import user_agent as _user_agent
from streetzim.paths import cache_root

PARSE_API = "https://en.wikipedia.org/w/api.php"
# The Wikipedia language of the bundle_wiki_articles call in progress
# (create_osm_zim --language): its API, article links and page language.
_LANG = "en"


def _wiki_host() -> str:
    from streetzim.languages import wiki_code
    return f"{wiki_code(_LANG)}.wikipedia.org"


def _parse_api() -> str:
    return f"https://{_wiki_host()}/w/api.php"
# Next to wikidata_cache.py's: $STREETZIM_CACHE_DIR, else the checkout, else
# a user cache dir when installed (streetzim/paths.py cache_root).
DEFAULT_CACHE_DIR = cache_root() / "wiki_articles_cache"

# Tags we keep (everything else is unwrapped to its text). Block + inline
# structure that reads/displays well; no media, tables, or interactivity.
_KEEP_TAGS = {"p", "h2", "h3", "h4", "ul", "ol", "li", "b", "i", "em",
              "strong", "blockquote", "dl", "dt", "dd", "br"}

# ---- images ------------------------------------------------------------
# Kiwix "maxi" ZIMs already carry downscaled WebP thumbnails, so bundling
# them is cheap: measured on California's 11,613 linkable articles,
# median 3 images/article, ~12 KB for the lead image and ~107 KB for all
# of them. Images are stored once each at wiki-image/<sha1>.<ext> (many
# articles share a location map or a seal) and referenced relatively
# from wiki-article/<Title> as ../wiki-image/<name>, which resolves the
# same way in kiwix-serve, the Kiwix apps and the PWA service worker.
IMAGE_MODES = ("none", "lead", "all")
# No SVG: it is the only script-capable format, and Kiwix pre-renders
# SVG figures to .svg.png -> webp anyway (0 SVG candidates in 500 real
# articles).
_EXT_FOR_MIME = {"image/webp": "webp", "image/png": "png", "image/jpeg": "jpg",
                 "image/gif": "gif"}
# UI chrome and pog markers that are never "the picture of the place".
_ICON_RE = re.compile(
    r"(OOjs|Symbol_|_Icon|Icon_|Wiki_letter|Ambox|Edit-|Crystal_Clear|"
    r"Question_book|Commons-logo|Wikisource|Wikiquote|Wikibooks|Wiktionary|"
    r"Wikivoyage|Red_pog|Green_pog|Blue_pog|Padlock|Speaker_Icon|Loudspeaker|"
    r"Nuvola|Emblem-|Disambig|Magnify-clip|Text_document)", re.I)
_MIN_IMAGE_BYTES = 1500          # below this it is an icon or a spacer
_IMG_RE = re.compile(r"<img\b[^>]*\bsrc=\"([^\"]+)\"[^>]*>", re.I)


def _img_width(tag: str) -> int:
    m = re.search(r'\bwidth="(\d+)"', tag)
    return int(m.group(1)) if m else 0


def _plain(text: str) -> str:
    """Caption text: strip tags, decode the entities Kiwix HTML already
    carries (or `&amp;` renders literally), collapse whitespace, then
    escape exactly once for the attribute/element we emit it into."""
    import html as _html
    t = re.sub(r"<[^>]+>", "", text)
    t = _html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


_DISAMBIG_RE = re.compile(
    r'class="[^"]*\b(dmbox|disambiguation)\b|'
    r'<p\b[^>]*>\s*(?:<b>)?[^<]{0,120}(?:</b>)?\s*(?:most commonly )?(?:may |can |could )?'
    r'(?:also )?refers? to\b|'
    r'\bmay refer to:|'
    # Other editions' disambiguation banners (fr, de, es, it, nl, pt, pl).
    r'Cette page d.homonymie|Begriffsklärungsseite|página de desambiguación|'
    r'pagina di disambiguazione|doorverwijspagina|página de desambiguação|'
    r'strona ujednoznaczniająca|id="disambigbox"', re.I)


def _is_disambiguation(raw_html: str) -> bool:
    """Disambiguation pages ("Roma or ROMA may refer to:", and the other
    editions' banners in _DISAMBIG_RE)."""
    head = raw_html[:20000]
    return bool(_DISAMBIG_RE.search(head))


def image_candidates(raw_html: str) -> list:
    """[(src, caption)] in preference order: the infobox image first, then
    figures/thumbs in document order (with their captions), then any other
    <img>. Icon-like srcs and tiny declared widths are skipped; srcs are
    de-duplicated. Resolution and size filtering happen later, against the
    source ZIM, so this is pure text work."""
    out, seen = [], set()

    def take(tag: str, caption: str = ""):
        m = re.search(r'\bsrc="([^"]+)"', tag)
        if not m:
            return
        src = m.group(1)
        if src in seen or _ICON_RE.search(src):
            return
        w = _img_width(tag)
        if 0 < w < 40:
            return
        if not caption:
            ma = re.search(r'\balt="([^"]*)"', tag)
            caption = _plain(ma.group(1)) if ma else ""
        seen.add(src)
        out.append((src, caption))

    mbox = re.search(r'<table\b[^>]*class="[^"]*infobox[^"]*"[^>]*>(.*?)</table>',
                     raw_html, re.S | re.I)
    if mbox:
        box = mbox.group(1)
        # Kiwix infobox pictures rarely carry alt text; the caption sits
        # in a sibling cell.
        mc = re.search(r'class="[^"]*infobox-caption[^"]*"[^>]*>(.*?)</(?:td|div|th)>',
                       box, re.S | re.I)
        cap = _plain(mc.group(1)) if mc else ""
        for tag in re.findall(r"<img\b[^>]*>", box, re.I):
            take(tag, cap)
            if out:
                break
    for m in re.finditer(r"<figure\b[^>]*>(.*?)</figure>", raw_html, re.S | re.I):
        block = m.group(1)
        tags = re.findall(r"<img\b[^>]*>", block, re.I)
        if not tags:
            continue
        mc = re.search(r"<figcaption\b[^>]*>(.*?)</figcaption>", block, re.S | re.I)
        take(tags[0], _plain(mc.group(1)) if mc else "")
    for m in re.finditer(r'<div\b[^>]*class="[^"]*\bthumb\b[^"]*"[^>]*>(.*?)</div>',
                         raw_html, re.S | re.I):
        block = m.group(1)
        tags = re.findall(r"<img\b[^>]*>", block, re.I)
        if not tags:
            continue
        mc = re.search(r'<div\b[^>]*class="[^"]*thumbcaption[^"]*"[^>]*>(.*?)</div>',
                       block, re.S | re.I)
        take(tags[0], _plain(mc.group(1)) if mc else "")
    for tag in re.findall(r"<img\b[^>]*>", raw_html, re.I):
        take(tag)
    return out


def _strip_lang(title: str) -> str:
    """`en:Foo Bar` -> `Foo Bar`; leaves un-prefixed titles alone."""
    ci = title.find(":")
    if 2 <= ci <= 3 and title[:ci].isalpha():
        return title[ci + 1:]
    return title


def _underscore(title: str) -> str:
    return _strip_lang(title).replace(" ", "_")


def _remove_spans_by_class(html: str, class_tokens: set[str]) -> str:
    """Balanced `<span class="…">…</span>` removal for nested spans
    (Wikipedia's per-character IPA tree, the ext-phonos ⓘ button, inline
    geo coords). Token match so "geo" doesn't eat "geography". Mirrors
    mcpzim's ArticleSections.removeSpansByClass."""
    return _remove_by_class(html, "span", lambda toks: bool(toks & class_tokens))


# Boilerplate containers, removed whole (nested <div>s included): matched
# as substrings of the class attribute, as before, plus French Wikipedia's
# banners (bandeau: disambiguation "homonymie", stub "ébauche", portal
# footer), its "Géolocalisation sur la carte" geobox and edit links.
BOILERPLATE_CLASSES = ("reflist", "navbox", "metadata", "mw-editsection",
                       "noprint", "hatnote", "thumb", "mw-empty-elt",
                       "bandeau-container", "bandeau-portail", "homonymie",
                       "geobox", "references-small", "infobox")


def _boilerplate(toks: set[str]) -> bool:
    attr = " ".join(toks)
    return any(c in attr for c in BOILERPLATE_CLASSES)


def _remove_by_class(html: str, tagname: str, pred) -> str:
    """Balanced removal of every `<tagname>` whose class tokens satisfy
    `pred`, with its content (nested same-name tags included)."""
    tag = re.compile(rf"<(/?){tagname}\b([^>]*)>", re.I)
    tags = list(tag.finditer(html))
    removals: list[tuple[int, int]] = []
    i = 0
    while i < len(tags):
        m = tags[i]
        if m.group(1):  # a </span>
            i += 1
            continue
        cls = re.search(r'class="([^"]*)"', m.group(2), re.I)
        toks = set(cls.group(1).lower().split()) if cls else set()
        if not pred(toks):
            i += 1
            continue
        depth, j = 1, i + 1
        while j < len(tags):
            depth += -1 if tags[j].group(1) else 1
            if depth == 0:
                break
            j += 1
        end = tags[j].end() if j < len(tags) else len(html)
        removals.append((m.start(), end))
        i = j + 1
    if not removals:
        return html
    out, prev = [], 0
    for a, b in removals:
        out.append(html[prev:a])
        out.append(" ")
        prev = b
    out.append(html[prev:])
    return "".join(out)


# The article opens by a full-page navigation from the map (the viewer
# stamps its camera into the URL hash first, so Back restores the view).
# A Home Screen web app on iOS has no browser chrome and Kiwix's own Back
# button is easy to miss, so every article carries its own way back: a
# sticky bar whose link goes to the map, and which uses history.back()
# when there is history so the stamped camera is restored. Sticky inside
# the body scroller, padded under an iOS status bar, 44 px tall to tap.
BACK_BAR_CSS = (
    ".sz-back{position:sticky;top:0;z-index:1;background:#fff;"
    "margin:-1em -1em .5em;padding:calc(.3em + env(safe-area-inset-top,0px)) 1em .3em;"
    "border-bottom:1px solid #eee}"
    ".sz-back a{display:inline-flex;align-items:center;min-height:44px;"
    "color:#2563eb;font-weight:600;text-decoration:none}"
)


def back_to_map_bar(title: str) -> str:
    """The "Back to map" bar for an article stored at wiki-article/<title>.

    A title with slashes is stored that many levels deeper (its images are
    linked ../../wiki-image/…), so the map link climbs the same depth.
    """
    up = "../" * (1 + title.count("/"))
    return (
        f'<nav class="sz-back"><a href="{up}index.html" '
        'onclick="if(history.length>1){history.back();return false}">'
        "&#8592; Back to map</a></nav>"
    )


def clean_article_html(html: str, title: str, source_url: str,
                       lead_html: str = "", gallery_html: str = "",
                       path_title: str | None = None) -> str:
    """Trim raw article HTML (Kiwix or Parsoid) to a compact, self-
    contained reader page: drop scripts/styles/tables/figures/nav/refs/
    edit-links and the IPA/coord clutter, unwrap links to text, whitelist
    structural tags, strip attributes, and add a CC BY-SA source footer.
    `path_title`: the title the page is stored under, when not `title` (a
    redirect's copy of its article), for the depth of the back link."""
    h = html
    # Narrow to the article body when a full document is given.
    mbody = re.search(r"<body\b[^>]*>(.*)</body>", h, re.S | re.I)
    if mbody:
        h = mbody.group(1)
    mparser = re.search(r'<div[^>]*class="[^"]*mw-parser-output[^"]*"[^>]*>(.*)',
                        h, re.S | re.I)
    if mparser:
        h = mparser.group(1)
    h = re.sub(r"<!--.*?-->", "", h, flags=re.S)
    # Whole-block drops (non-greedy; Kiwix/Parsoid output doesn't self-nest
    # these). The IPA/geo spans need the balanced remover above.
    h = _remove_spans_by_class(h, {"ipa", "rt-commentedtext", "ext-phonos",
                                    "geo", "coordinates"})
    # Tables nest (an infobox holds its location map's table): balanced.
    h = _remove_by_class(h, "table", lambda toks: True)
    for tag in ("script", "style", "figure", "nav", "aside",
                "sup", "ol", "math", "audio", "video"):
        h = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", " ", h, flags=re.S | re.I)
    # Reference/nav/edit/banner containers by class, balanced: a banner
    # nests <div>s, and cutting at its first </div> left its text behind.
    h = _remove_by_class(h, "div", _boilerplate)
    h = _remove_by_class(h, "span", _boilerplate)
    # Unwrap links → keep their text.
    h = re.sub(r"</?a\b[^>]*>", "", h, flags=re.I)
    # Some articles contain ESCAPED markup as literal text — an editor typed
    # `<a href="example.org">` into the wikitext, so Kiwix serves
    # `&lt;a href="example.org"&lt;/a&gt`. The unwrap above cannot see it, it
    # renders as garbage, and zimcheck's link scanner reads the bare
    # `href="..."` out of the text and reports a dangling internal link —
    # which fails the release gate (nyc-metro and new-york-state,
    # 2026-09-06, on Yeshivas_Chofetz_Chaim). Drop escaped tags that carry
    # an attribute; plain "a &lt; b" prose is untouched.
    h = re.sub(r"&lt;\s*/?\s*[a-zA-Z][^&]{0,300}?=[^&]{0,300}?(?:&gt;|(?=&lt;))", "", h)
    h = re.sub(r"&lt;\s*/\s*[a-zA-Z]+\s*&gt;?", "", h)
    # Whitelist tags, strip attributes; drop others (keep inner text).
    def keep(m: re.Match) -> str:
        closing, name = m.group(1), m.group(2).lower()
        return f"<{closing}{name}>" if name in _KEEP_TAGS else ""
    h = re.sub(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>", keep, h)
    # Collapse whitespace (HTML ignores it anyway) + drop empty tags +
    # tidy the parentheticals the IPA removal empties out.
    h = re.sub(r"\s+", " ", h)
    h = re.sub(r">\s+<", "><", h)
    for _ in range(3):
        h = re.sub(r"<(p|li|ul|ol|h2|h3|h4|b|i|em|strong)>\s*</\1>", "", h)
    h = h.replace("&#160;", " ").replace("&nbsp;", " ")  # nbsp → space (TTS)
    h = re.sub(r"\b[A-Za-z]+:\s*(?=[);])", "", h)  # dangling "Spanish:" before ) / ;
    h = re.sub(r"\(\s*[;,]\s*", "(", h)            # "( ; X" → "(X"
    h = re.sub(r"\s*[;,]\s*\)", ")", h)            # "X ; )" → "X)"
    h = re.sub(r"\(\s*[;,]?\s*\)", "", h)          # "( )" / "( ; )" leftovers
    h = re.sub(r"\s+([.,;:!?)])", r"\1", h)
    h = re.sub(r"\(\s+", "(", h)
    h = h.strip()

    safe_title = (title.replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;"))
    return (
        f"<!DOCTYPE html><html lang=\"{_LANG}\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<link rel=\"icon\" href=\"data:,\">"
        f"<title>{safe_title}</title>"
        "<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "max-width:42em;margin:1em auto;padding:0 1em;line-height:1.55;"
        "color:#222}h1{font-size:1.5em}h2{font-size:1.2em;margin-top:1.2em}"
        "footer{margin-top:2em;padding-top:1em;border-top:1px solid #ddd;"
        "font-size:.85em;color:#666}"
        "figure{margin:1em 0}img{max-width:100%;height:auto;border-radius:4px}"
        "figcaption{font-size:.85em;color:#555;margin-top:.3em}"
        ".gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:.8em}"
        + BACK_BAR_CSS +
        "</style></head><body>"
        + back_to_map_bar(title if path_title is None else path_title) +
        f"<h1>{safe_title}</h1>\n{lead_html}{h}\n{gallery_html}"
        f"<footer>From <a href=\"{source_url}\">Wikipedia</a> — text under "
        "<a href=\"https://creativecommons.org/licenses/by-sa/4.0/\">"
        "CC BY-SA 4.0</a>."
        + (" Images: their own licences on Wikimedia Commons." if (lead_html or gallery_html) else "")
        + "</footer></body></html>"
    )


# ---- fetching -------------------------------------------------------------

class _OfflineZim:
    """Lazy reader for a local Wikipedia ZIM (offline article source)."""
    def __init__(self, path: str):
        from libzim.reader import Archive  # lazy: only when offline source used
        self.a = Archive(Path(path))

    def image(self, src: str, max_bytes: int | None = None):
        """Bytes + mimetype for an <img src> as written in a Kiwix article
        ("./_assets_/<hash>/<name>", percent-encoded once more than the
        entry path is). None when the entry is absent or larger than
        `max_bytes` — checked on the dirent size BEFORE the blob is read,
        so an oversize image costs a lookup, not a decompressed cluster."""
        p = urllib.parse.unquote(src.lstrip("./"))
        for cand in (p, "I/" + p):
            try:
                it = self.a.get_entry_by_path(cand).get_item()
                if max_bytes is not None and it.size > max_bytes:
                    return None
                return bytes(it.content), it.mimetype
            except KeyError:
                continue
            except Exception:
                continue
        return None

    def html(self, title_us: str) -> str | None:
        ws = title_us.replace("_", " ")
        for p in (f"A/{title_us}", title_us, f"A/{ws}", ws):
            try:
                return bytes(self.a.get_entry_by_path(p).get_item().content).decode(
                    "utf-8", "replace")
            except KeyError:
                continue
            except Exception:
                continue
        return None

    def redirect_target(self, title_us: str) -> str | None:
        """The underscored title a redirect points to (a chain followed to
        its end); None when the title is an article or is not in the ZIM."""
        ws = title_us.replace("_", " ")
        for p in (f"A/{title_us}", title_us, f"A/{ws}", ws):
            try:
                e = self.a.get_entry_by_path(p)
            except Exception:
                continue
            if not e.is_redirect:
                return None
            try:
                for _ in range(5):
                    e = e.get_redirect_entry()
                    if not e.is_redirect:
                        break
            except Exception:
                return None
            path = e.path[2:] if e.path.startswith("A/") else e.path
            return path.replace(" ", "_")
        return None


# Cache layout (cache_dir/<sha1 of the title>.*):
#   .html, non-empty  the article HTML (a hit; never refetched)
#   .miss             a definitive "no article": JSON {title, reason, checked}
#   .html, empty      the old miss marker. Before the .miss files, a 429 or
#                     5xx that outlived the retries was written this way too,
#                     so an empty .html says nothing: it is re-checked once
#                     (at most STREETZIM_WIKI_RECHECK_MAX per build) and
#                     replaced by a .html or a .miss.
#   .redirect         where a title leads on English Wikipedia: JSON {title,
#                     to, page, checked}; `to` the article it redirects to
#                     (null: an article of its own, or no such page),
#                     `page` the page it opens (its own title, normalized,
#                     or the redirect's target; null: no such page)
# Only the API's own answers are cached. A rate limit, 5xx, timeout,
# connection error or unexpected body raises TransientError and leaves the
# cache alone.
_MISS_SUFFIX = ".miss"
# API error codes that are an answer about the page (anything else in an
# `error` body, e.g. ratelimited / maxlag / internal_api_error_*, is not).
_DEFINITIVE_API_ERRORS = frozenset({"missingtitle", "invalidtitle",
                                    "pagecannotexist", "nosuchpageid"})
# HTTP statuses that answer for the page itself whatever the headers say:
# only 414 (the title is too long to ask about). A 400/404/410 from api.php
# is a proxy or a wrong endpoint unless its MediaWiki-API-Error header names
# one of the codes above; 401/403 refuse the client. Those stop the run.
_DEFINITIVE_HTTP = frozenset({414})
# Stop requesting after this many titles in a row the API could not answer.
_GIVE_UP_AFTER = 25
RECHECK_ENV = "STREETZIM_WIKI_RECHECK_MAX"
_DEFAULT_RECHECK_MAX = 1000

# Pacing of the `action=parse` requests: cloud/wikimedia_http.polite_pacer
# (serial, 0.1 s from each response, at most 120 requests a minute, 5 s
# after an answer over 1 s; docs/zimfarm.md, "Wikimedia API etiquette").
# Until 2026-09 this paused a fixed 1 s after every answer: 1,308 of the
# 1,541 s the D.C. fetch took, in a 39 minute build with no 429.


_REDIRECT_SUFFIX = ".redirect"
# Titles per `action=query` redirect lookup (the API's limit for a client
# without the apihighlimits right).
QUERY_BATCH = 50
# ... and at most this many characters of titles, percent-encoded: a GET
# of 50 long Cyrillic or CJK titles (6-9 characters a letter encoded) runs
# past 12 KB, which a server may refuse with a 414 (every title of the
# batch unanswered, every build). 6,000 keeps the URL well under 8 KB.
QUERY_MAX_CHARS = 6000


def _query_batches(titles_us: list[str]) -> list[list[str]]:
    """`titles_us` split into action=query batches: at most QUERY_BATCH
    titles and QUERY_MAX_CHARS characters of encoded titles each (a
    longer title goes alone)."""
    out: list[list[str]] = []
    size = 0
    for t in titles_us:
        n = len(urllib.parse.quote(t.replace("_", " "), safe="")) + 3   # + an encoded "|"
        if not out or len(out[-1]) >= QUERY_BATCH or size + n > QUERY_MAX_CHARS:
            out.append([])
            size = 0
        out[-1].append(t)
        size += n
    return out


def _cache_paths(cache_dir: str, title_us: str) -> tuple[str, str]:
    key = hashlib.sha1(title_us.encode("utf-8")).hexdigest()
    base = os.path.join(cache_dir, key)
    return base + ".html", base + _MISS_SUFFIX


def _cache_state(title_us: str, cache_dir: str | None) -> tuple[str, str | None]:
    """What the cache knows, without the network: ("hit", html),
    ("miss", None), ("legacy", None) for an old empty marker, or
    ("none", None)."""
    if not cache_dir:
        return "none", None
    html_file, miss_file = _cache_paths(cache_dir, title_us)
    if os.path.exists(html_file) and os.path.getsize(html_file) > 0:
        with open(html_file, encoding="utf-8") as f:
            return "hit", f.read()
    if os.path.exists(miss_file):
        return "miss", None
    if os.path.exists(html_file):
        return "legacy", None
    return "none", None


def _write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.{os.getpid()}.part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _record_miss(html_file: str, miss_file: str, title_us: str, reason: str) -> None:
    _write_atomic(miss_file, json.dumps({
        "title": title_us, "reason": reason,
        "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
    if os.path.exists(html_file):   # the legacy empty marker it replaces
        os.remove(html_file)


def _parse_request(title_us: str, ua: str,
                   pacer: Pacer | None = None) -> tuple[dict | None, str | None]:
    """One `action=parse` request, redirects followed: (the `parse` object,
    None), or (None, the reason) for a definitive miss (no such page).
    Raises TransientError when the API did not answer about the page."""
    params = urllib.parse.urlencode({
        "action": "parse", "page": title_us.replace("_", " "),
        "prop": "text", "redirects": "1", "format": "json",
        # zhwiki mixes scripts; ask for Simplified (OSM name:zh mostly is).
        **({"variant": "zh-hans"} if _LANG == "zh" else {}),
        "disableeditsection": "1", "disablelimitreport": "1", "formatversion": "2",
    })
    try:
        data = get_json(f"{_parse_api()}?{params}", user_agent=ua, pacer=pacer)
    except urllib.error.HTTPError as e:
        code = api_error_code(e)
        if code in _DEFINITIVE_API_ERRORS:
            return None, code
        if e.code in _DEFINITIVE_HTTP:
            return None, f"http-{e.code}"
        raise stop_error(e) from e
    if not isinstance(data, dict):
        raise TransientError(f"unexpected {type(data).__name__} body")
    err = data.get("error")
    if err is not None:
        code = str((err or {}).get("code", "")) if isinstance(err, dict) else ""
        if code not in _DEFINITIVE_API_ERRORS:
            raise TransientError(f"API error {code or '?'}")
        return None, code
    parse = data.get("parse")
    if not isinstance(parse, dict):
        raise TransientError("body has neither parse nor error")
    return parse, None


def _parse_html(parse: dict) -> str | None:
    text: Any = parse.get("text")
    if isinstance(text, dict):  # formatversion=1 shape
        text = text.get("*")
    return text if isinstance(text, str) and text.strip() else None


def _fetch_network(title_us: str, cache_dir: str | None, ua: str,
                   pacer: Pacer | None = None) -> str | None:
    """Ask the API (no cache read) and cache its answer: the HTML, or a
    .miss for a definitive miss (returns None). Raises TransientError when
    the API did not answer about the page — nothing is cached then."""
    html_file = miss_file = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        html_file, miss_file = _cache_paths(cache_dir, title_us)
    parse, reason = _parse_request(title_us, ua, pacer)
    html = _parse_html(parse) if parse is not None else None
    if html_file and miss_file:
        if html:
            _write_atomic(html_file, html)
            # Where the title led (a redirect is followed): its canonical
            # page, which the redirect-only titles are matched against.
            assert parse is not None
            page = str(parse.get("title") or "").replace(" ", "_") or title_us
            _record_redirect(cache_dir, title_us,
                             page if parse.get("redirects") else None, page)
        else:
            _record_miss(html_file, miss_file, title_us, reason or "no-text")
    return html


def _redirect_path(cache_dir: str, title_us: str) -> str:
    return _cache_paths(cache_dir, title_us)[0][:-len(".html")] + _REDIRECT_SUFFIX


def _record_redirect(cache_dir: str | None, title_us: str, to: str | None,
                     page: str | None) -> None:
    if not cache_dir:
        return
    os.makedirs(cache_dir, exist_ok=True)
    _write_atomic(_redirect_path(cache_dir, title_us), json.dumps({
        "title": title_us, "to": to, "page": page,
        "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))


def _redirect_cached(title_us: str,
                     cache_dir: str | None) -> tuple[str | None, str | None] | None:
    """(to, page) from a .redirect answer in the cache (see the cache
    layout above), or None when the cache has no answer for the title."""
    if not cache_dir:
        return None
    try:
        with open(_redirect_path(cache_dir, title_us), encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or "page" not in rec:
        return None

    def title(v: Any) -> str | None:
        return v if isinstance(v, str) and v else None
    return title(rec.get("to")), title(rec.get("page"))


def _query_redirects(titles_us: list[str], ua: str, pacer: Pacer | None = None
                     ) -> dict[str, tuple[str | None, str | None]]:
    """Where each title leads, from one `action=query&redirects=1` request
    for up to QUERY_BATCH titles (no page text): {title_us: (to, page)},
    underscored, as in the .redirect cache. A title the answer does not
    account for is left out (unknown, not "no such page"). Raises
    TransientError when the API did not answer."""
    params = urllib.parse.urlencode({
        "action": "query", "titles": "|".join(t.replace("_", " ") for t in titles_us),
        "redirects": "1", "format": "json", "formatversion": "2",
    })
    try:
        data = get_json(f"{_parse_api()}?{params}", user_agent=ua, pacer=pacer)
    except urllib.error.HTTPError as e:
        raise stop_error(e) from e
    if not isinstance(data, dict):
        raise TransientError(f"unexpected {type(data).__name__} body")
    if data.get("error") is not None:
        err = data["error"]
        code = str(err.get("code", "")) if isinstance(err, dict) else ""
        raise TransientError(f"API error {code or '?'}")
    query = data.get("query")
    if not isinstance(query, dict):
        raise TransientError("body has neither query nor error")

    def hops(key: str) -> dict[str, str]:
        return {str(h.get("from")): str(h.get("to")) for h in query.get(key) or ()
                if isinstance(h, dict) and h.get("from") and h.get("to")}
    normalized, redirects = hops("normalized"), hops("redirects")
    pages = {str(p.get("title")): not (p.get("missing") or p.get("invalid"))
             for p in query.get("pages") or () if isinstance(p, dict) and p.get("title")}
    # A title with another wiki's prefix ("zh-yue:Foo", "simple:Foo": codes
    # _strip_lang leaves on) is listed under `interwiki`, not `pages`. It is
    # no page of English Wikipedia: the definitive "no such page", cached
    # like one (it used to go unanswered, so unfetched, every build).
    for iw in query.get("interwiki") or ():
        if isinstance(iw, dict) and iw.get("title"):
            pages[str(iw["title"])] = False
    out: dict[str, tuple[str | None, str | None]] = {}
    for t in titles_us:
        name = normalized.get(t.replace("_", " "), t.replace("_", " "))
        seen = {name}
        while name in redirects and redirects[name] not in seen:   # a chain, not a loop
            name = redirects[name]
            seen.add(name)
        if name not in pages:
            continue
        page = name.replace(" ", "_") if pages[name] else None
        out[t] = (page if len(seen) > 1 else None, page)
    return out


def _fetch_online(title_us: str, cache_dir: str | None, ua: str,
                  pacer: Pacer | None = None) -> str | None:
    """Article HTML from the cache or the API; None for a definitive miss
    (no such page, no text). An old empty marker is re-checked. Raises
    TransientError when the API could not answer (never cached)."""
    state, html = _cache_state(title_us, cache_dir)
    if state == "hit":
        return html
    if state == "miss":
        return None
    return _fetch_network(title_us, cache_dir, ua, pacer)


def bundle_wiki_articles(titles: Iterable[str],
                         add_item: Callable[[str, str, str, bytes], None],
                         *, lang: str = "en", **kw: Any) -> dict:
    """_bundle_wiki_articles from ``lang`` Wikipedia (default English): its
    API, its article links, pages marked with its language, and a cache of
    its own (<cache_dir>/lang/<lang>, as titles collide across languages).
    An offline source must be a Wikipedia ZIM in that language."""
    global _LANG
    if lang != "en":
        root = kw.get("cache_dir") or str(DEFAULT_CACHE_DIR)
        kw["cache_dir"] = os.path.join(root, "lang", lang)
        if os.path.isdir(root):
            from streetzim.cache_permissions import make_shared_dirs
            make_shared_dirs(kw["cache_dir"], root)
    prev, _LANG = _LANG, lang
    try:
        return _bundle_wiki_articles(titles, add_item, **kw)
    finally:
        _LANG = prev


def _bundle_wiki_articles(
    titles: Iterable[str],
    add_item: Callable[[str, str, str, bytes], None],
    *,
    cache_dir: str | None = None,
    user_agent: str | None = None,
    offline_zim: str | None = None,
    limit: int | None = None,
    sleep: float | None = None,
    max_per_min: float | None = None,
    log: Callable[[str], None] = print,
    images: str = "none",
    image_max_kb: int = 128,
    max_images_per_article: int = 12,
    source=None,
    redirect_only: Iterable[str] = (),
    add_redirect: Callable[[str, str, str], None] | None = None,
) -> dict:
    """Fetch + clean + store each distinct article at `wiki-article/<Title>`.

    `add_item(path, title, mimetype, content_bytes)` is the storage callback
    (wired to `creator.add_item(MapItem(...))` in the build; a plain dict
    collector in tests). Returns stats.

    Online, requests are serial and `sleep` is the gap from the end of
    each API response to the next request (cache hits cost none; default
    STREETZIM_WIKI_GAP, else 0.1 s), with at most `max_per_min` requests a
    minute (STREETZIM_WIKI_MAX_PER_MIN, else 120) and 5 s after an answer
    that took over 1 s (`polite_pacer`); a 429, maxlag or 5xx with
    Retry-After widens the gap and successes ease it back. A title the
    API could not answer (429, 5xx, network, refused) is counted in stats["unfetched"], never
    cached as a miss, and reported in a WARNING; with
    STREETZIM_REQUIRE_WIKI=1 it stops the build (SystemExit) instead of
    shipping fewer articles. Requests stop for the rest of the run after a
    401/403/404, 25 unanswered requests in a row, or a spent wait budget
    (STREETZIM_WIKI_WAIT_BUDGET); cached articles are still bundled.

    `redirect_only`: titles bundled only when English Wikipedia has them
    as a redirect to an article bundled here (from `titles`). These are
    non-English tags whose item has no English article (Wikidata says so):
    an English namesake that is a redirect is an editor's alias, such as
    "Aalten (dorp)" -> "Aalten", but a namesake article is another subject,
    and so is a redirect to one ("Pannenberg" -> "Wolfhart Pannenberg", a
    theologian; "VVAC" -> "Verde Valley Archaeology Center"). A redirect to
    an article of another place of this map (the municipality, the city)
    is kept. The target is matched by the page each bundled title opens, so
    a target bundled under an alias of its own (AEGON -> Aegon) counts.
    `add_redirect(path, title, target_path)` writes it as a ZIM redirect
    to that article (creator.add_redirection); without it, or when the two
    titles hold a different number of slashes (the PWA would serve the
    article at the redirect's depth, breaking its image links), a copy of
    the article is stored. Offline, the source ZIM's redirect entries tell;
    online, `action=query&redirects=1` for up to 50 titles a request
    (fewer when long: QUERY_MAX_CHARS), no text
    (each answer cached, .redirect). A source that cannot tell bundles
    none. A title also in `titles` is bundled as usual.
    """
    seen: set[str] = set()
    norm: list[str] = []
    for t in titles:
        if not t:
            continue
        u = _underscore(t)
        if u and u not in seen:
            seen.add(u)
            norm.append(u)
    if limit:
        norm = norm[:limit]
    redirect_titles: list[str] = []
    for t in redirect_only:
        u = _underscore(t) if t else ""
        # "de:" or "nl: " names no page at all: not asked about.
        if u.strip("_") and u not in seen:
            seen.add(u)
            redirect_titles.append(u)

    if images not in IMAGE_MODES:
        raise ValueError(f"images must be one of {IMAGE_MODES}, got {images!r}")
    src = source if source is not None else (_OfflineZim(offline_zim) if offline_zim else None)
    if images != "none" and (src is None or not hasattr(src, "image")):
        log("    bundle-wiki-articles: images need an offline source ZIM — "
            "bundling text only")
        images = "none"
    image_paths: set = set()       # wiki-image/<sha>.<ext> already stored
    images_stored = image_bytes = disambig = 0
    # Cache by default for the online path so a rebuild never re-crawls
    # Wikipedia (repo-relative, like wikidata_cache/). Offline (local ZIM)
    # needs no cache — reads are local.
    if src is None and cache_dir is None:
        cache_dir = str(DEFAULT_CACHE_DIR)
    log(f"    bundle-wiki-articles: {len(norm)} distinct titles"
        + (f" from {os.path.basename(offline_zim) if offline_zim else type(src).__name__}"
           if src else f" via Wikipedia API (cache: {cache_dir})"))

    bundled = failed = total_bytes = 0
    unfetched = rate_limited = 0   # the API could not answer; not cached
    streak = 0                     # consecutive network requests unanswered
    stopped = False                # no more requests this run (cache still read)
    rechecked = recheck_left = 0   # old empty markers re-checked / left for later
    recheck_max = int(env_number(RECHECK_ENV, _DEFAULT_RECHECK_MAX))
    pacer = polite_pacer(sleep, max_per_min)
    ua = user_agent or _user_agent("wiki")
    if src is None:
        states: dict[str, int] = {}
        for t in norm:
            st = _cache_state(t, cache_dir)[0]
            states[st] = states.get(st, 0) + 1
        legacy = states.get("legacy", 0)
        log(f"    bundle-wiki-articles: cache has {states.get('hit', 0)} articles, "
            f"{states.get('miss', 0)} known misses; {states.get('none', 0)} to fetch"
            + (f", {min(legacy, recheck_max)} of {legacy} old empty markers to re-check"
               f" ({RECHECK_ENV}={recheck_max})" if legacy else ""))
    stored_titles: set = set()   # title_us actually written — for the geo-index
    def store(title_us: str, article_us: str, raw: str) -> int:
        """Store the page for `title_us` (the text of `article_us`, the
        article itself or the one it redirects to); returns its size."""
        nonlocal images_stored, image_bytes
        disp = article_us.replace("_", " ")
        url = f"https://{_wiki_host()}/wiki/" + urllib.parse.quote(article_us)
        lead_html = gallery_html = ""
        # The page lives at wiki-article/<Title>; a title with N
        # slashes is N levels deeper, so the image link must climb
        # N+1 (108 of California's 11,613 titles are like
        # "Expo_Park/USC_station" — a fixed "../" left every one of
        # them with dangling links and a failed validate gate).
        up = "../" * (title_us.count("/") + 1)
        if images != "none" and src is not None:  # src is set when images are on
            figs = []
            for isrc, caption in image_candidates(raw):
                if len(figs) >= max_images_per_article:
                    break
                got = src.image(isrc, image_max_kb * 1024)
                if not got:
                    continue
                b, mt = got
                ext = _EXT_FOR_MIME.get((mt or "").split(";")[0].strip())
                if not ext or len(b) < _MIN_IMAGE_BYTES or len(b) > image_max_kb * 1024:
                    continue
                name = hashlib.sha1(b).hexdigest()[:20] + "." + ext
                ipath = "wiki-image/" + name
                if ipath not in image_paths:
                    add_item(ipath, "", mt.split(";")[0].strip(), b)
                    image_paths.add(ipath)
                    images_stored += 1
                    image_bytes += len(b)
                figs.append((name, caption))
                if images == "lead":
                    break
            if figs:
                name, caption = figs[0]
                cap = f"<figcaption>{caption}</figcaption>" if caption else ""
                lead_html = (f'<figure class="lead"><img src="{up}wiki-image/{name}" '
                             f'alt="{caption}" loading="lazy">{cap}</figure>\n')
                if len(figs) > 1:
                    cells = "".join(
                        f'<figure><img src="{up}wiki-image/{n}" alt="{c}" loading="lazy">'
                        + (f"<figcaption>{c}</figcaption>" if c else "") + "</figure>"
                        for n, c in figs[1:])
                    gallery_html = f'<section class="gallery">{cells}</section>\n'
        page = clean_article_html(raw, disp, url, lead_html, gallery_html,
                                  path_title=title_us).encode("utf-8")
        add_item(f"wiki-article/{title_us}", disp, "text/html", page)
        stored_titles.add(title_us)
        return len(page)

    for i, title_us in enumerate(norm, 1):
        raw: str | None = None
        if src:
            raw = src.html(title_us)
        else:
            state, raw = _cache_state(title_us, cache_dir)
            ask = state in ("none", "legacy")
            if state == "legacy" and rechecked >= recheck_max:
                ask = False          # stays an unverified miss until a later build
                recheck_left += 1
            elif ask and stopped:
                ask = False
                unfetched += 1
            if ask:
                rechecked += state == "legacy"
                try:
                    raw = _fetch_online(title_us, cache_dir, ua, pacer)
                    streak = 0
                except TransientError as e:
                    unfetched += 1
                    rate_limited += e.rate_limited
                    streak += 1
                    if e.stop or streak >= _GIVE_UP_AFTER:
                        # Refused, out of wait budget, or down: every
                        # further request would fail the same way. Cache
                        # hits still count; the rest wait for the next build.
                        stopped = True
                        log(f"    bundle-wiki-articles: not requesting the rest "
                            f"({e.reason}" + ("" if e.stop else
                                              f"; {streak} titles in a row unanswered")
                            + ")")
        if raw and _is_disambiguation(raw):
            # A non-English `wikipedia=` tag whose enwiki namesake is a
            # disambiguation page ("it:Roma" -> "Roma may refer to:") would
            # bundle the wrong page under the POI, lead image and all.
            raw = None
            disambig += 1
        if not raw:
            failed += 1
        else:
            total_bytes += store(title_us, title_us, raw)
            bundled += 1
        if i % 250 == 0:
            log(f"    ... {i}/{len(norm)} bundled={bundled} failed={failed} "
                f"{total_bytes // 1024} KB")
    # Titles bundled only as a redirect to an article stored above.
    redirects = redirects_skipped = 0

    def where(titles_us: list[str], counted: bool) -> dict[str, tuple[str | None, str | None]]:
        """(to, page) for each title the source or the API could answer
        (see _query_redirects). `counted`: an unanswered title is one of
        the run's unfetched articles."""
        nonlocal unfetched, rate_limited, streak, stopped
        if src is not None:
            lookup = getattr(src, "redirect_target", None)
            if lookup is None:    # a source without redirect entries cannot tell
                return {}
            out = {}
            for t in titles_us:
                to = lookup(t)
                out[t] = (to, to or t)
            return out
        out, todo = {}, []
        for t in titles_us:
            known = _redirect_cached(t, cache_dir)
            if known is not None:
                out[t] = known
            else:
                todo.append(t)
        for batch in _query_batches(todo):
            got: dict = {}
            if not stopped:
                try:
                    got = _query_redirects(batch, ua, pacer)
                    streak = 0
                except TransientError as e:
                    rate_limited += e.rate_limited * counted
                    streak += 1
                    if e.stop or streak >= _GIVE_UP_AFTER:
                        stopped = True
                        log(f"    bundle-wiki-articles: not requesting the rest ({e.reason})")
            for t, ans in got.items():
                _record_redirect(cache_dir, t, *ans)
                out[t] = ans
            if counted:
                unfetched += sum(1 for t in batch if t not in got)
        return out

    if redirect_titles:
        led = where(redirect_titles, counted=True)
        targets = {led[t][0] for t in redirect_titles if t in led and led[t][0]}
        # The page each bundled title opens: offline html() and the API
        # follow redirects, so a title may be stored under an alias of the
        # article a redirect-only title points to.
        page_of = {t: t for t in stored_titles}
        if targets - set(page_of):
            for t, (_to, page) in where(sorted(stored_titles), counted=False).items():
                if page:
                    page_of[t] = page
        stored_for: dict[str, str] = {}
        for t in sorted(stored_titles):
            stored_for.setdefault(page_of[t], t)
        for t in sorted(stored_titles):
            stored_for.setdefault(t, t)
        for title_us in redirect_titles:
            to = led.get(title_us, (None, None))[0]
            article = stored_for.get(to) if to else None
            if not article:
                redirects_skipped += 1
                continue
            if add_redirect is not None and title_us.count("/") == article.count("/"):
                add_redirect(f"wiki-article/{title_us}", title_us.replace("_", " "),
                             f"wiki-article/{article}")
            else:
                # A copy when the two sit at different depths ("AC-DC" ->
                # "AC/DC"): the PWA serves a redirect entry's content at the
                # redirect's own URL, where the article's relative
                # ../wiki-image/ links would climb the wrong number of levels.
                raw = src.html(article) if src is not None else _cache_state(article, cache_dir)[1]
                if not raw:
                    redirects_skipped += 1
                    continue
                total_bytes += store(title_us, article, raw)
            stored_titles.add(title_us)
            redirects += 1
        log(f"    bundle-wiki-articles: {redirects} of {len(redirect_titles)} titles with "
            f"no English article of their own bundled as redirects to an article "
            f"bundled here; {redirects_skipped} skipped")

    stats = {"requested": len(norm) + len(redirect_titles), "bundled": bundled,
             "failed": failed,
             "bytes": total_bytes, "stored_titles": stored_titles,
             "images": images_stored, "image_bytes": image_bytes,
             "disambiguation_skipped": disambig,
             "unfetched": unfetched, "rate_limited": rate_limited,
             "rechecked": rechecked, "recheck_left": recheck_left,
             "redirects": redirects, "redirects_skipped": redirects_skipped}
    log(f"    bundle-wiki-articles: stored {bundled} articles "
        f"({total_bytes / 1024:.0f} KB), {failed} unavailable"
        + (f" ({disambig} were enwiki disambiguation pages)" if disambig else "")
        + (f"; {images_stored} images ({image_bytes / 1024 / 1024:.0f} MB, mode={images})"
           if images != "none" else ""))
    if rechecked or recheck_left:
        log(f"    bundle-wiki-articles: re-checked {rechecked} old empty cache markers"
            + (f"; {recheck_left} left for later builds" if recheck_left else ""))
    if unfetched:
        msg = (f"WARNING: bundle-wiki-articles: {unfetched} of {len(norm) + len(redirect_titles)} articles "
               f"were NOT fetched ({rate_limited} rate-limited by Wikipedia, the "
               f"rest 5xx/network/refused). They are not cached as missing, so the next "
               f"build fetches them; this ZIM ships without them.")
        log("    " + msg)
        if require_complete():
            raise SystemExit(f"STREETZIM_REQUIRE_WIKI=1: {msg}")
    return stats
