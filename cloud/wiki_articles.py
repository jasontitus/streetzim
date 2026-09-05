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
public Wikipedia API (cached to disk). Public data only; the User-Agent is
the project's public repo URL, never personal contact info. Wikipedia text
is CC BY-SA — every page keeps a source link + license footer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional

USER_AGENT = "streetzim-wiki/1.0 (+https://github.com/jasontitus/streetzim)"
PARSE_API = "https://en.wikipedia.org/w/api.php"
# Repo-relative cache dir, matching wikidata_cache.py's convention
# (SCRIPT_DIR / "wikidata_cache"). create_osm_zim.py lives one level up.
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "wiki_articles_cache"

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
_EXT_FOR_MIME = {"image/webp": "webp", "image/png": "png", "image/jpeg": "jpg",
                 "image/gif": "gif", "image/svg+xml": "svg"}
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
    """Caption text: strip tags, collapse whitespace, HTML-escape."""
    t = re.sub(r"<[^>]+>", "", text)
    t = re.sub(r"\s+", " ", t).strip()
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


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
        mc = re.search(r'class="[^"]*infobox-caption[^"]*"[^>]*>(.*?)</', box, re.S | re.I)
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
    tag = re.compile(r"<(/?)span\b([^>]*)>", re.I)
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
        if not (toks & class_tokens):
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


def clean_article_html(html: str, title: str, source_url: str,
                       lead_html: str = "", gallery_html: str = "") -> str:
    """Trim raw article HTML (Kiwix or Parsoid) to a compact, self-
    contained reader page: drop scripts/styles/tables/figures/nav/refs/
    edit-links and the IPA/coord clutter, unwrap links to text, whitelist
    structural tags, strip attributes, and add a CC BY-SA source footer."""
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
    for tag in ("script", "style", "table", "figure", "nav", "aside",
                "sup", "ol", "math", "audio", "video"):
        h = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", " ", h, flags=re.S | re.I)
    # Reference/nav/edit containers by class/role.
    for cls in ("reflist", "navbox", "metadata", "mw-editsection",
                "noprint", "hatnote", "thumb", "mw-empty-elt"):
        h = re.sub(rf'<div\b[^>]*class="[^"]*{cls}[^"]*"[^>]*>.*?</div>',
                  " ", h, flags=re.S | re.I)
        h = re.sub(rf'<span\b[^>]*class="[^"]*{cls}[^"]*"[^>]*>.*?</span>',
                  " ", h, flags=re.S | re.I)
    # Unwrap links → keep their text.
    h = re.sub(r"</?a\b[^>]*>", "", h, flags=re.I)
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
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{safe_title}</title>"
        "<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "max-width:42em;margin:1em auto;padding:0 1em;line-height:1.55;"
        "color:#222}h1{font-size:1.5em}h2{font-size:1.2em;margin-top:1.2em}"
        "footer{margin-top:2em;padding-top:1em;border-top:1px solid #ddd;"
        "font-size:.85em;color:#666}"
        "figure{margin:1em 0}img{max-width:100%;height:auto;border-radius:4px}"
        "figcaption{font-size:.85em;color:#555;margin-top:.3em}"
        ".gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:.8em}"
        "</style></head><body>"
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
        self.a = Archive(path)

    def image(self, src: str):
        """Bytes + mimetype for an <img src> as written in a Kiwix article
        ("./_assets_/<hash>/<name>", percent-encoded once more than the
        entry path is). None when the entry is absent."""
        p = urllib.parse.unquote(src.lstrip("./"))
        for cand in (p, "I/" + p):
            try:
                it = self.a.get_entry_by_path(cand).get_item()
                return bytes(it.content), it.mimetype
            except KeyError:
                continue
            except Exception:
                continue
        return None

    def html(self, title_us: str) -> Optional[str]:
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


def _fetch_online(title_us: str, cache_dir: Optional[str], ua: str) -> Optional[str]:
    cache_file = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        key = hashlib.sha1(title_us.encode("utf-8")).hexdigest()
        cache_file = os.path.join(cache_dir, key + ".html")
        if os.path.exists(cache_file):
            with open(cache_file, encoding="utf-8") as f:
                return f.read() or None
    params = urllib.parse.urlencode({
        "action": "parse", "page": title_us.replace("_", " "),
        "prop": "text", "redirects": "1", "format": "json",
        "disableeditsection": "1", "disablelimitreport": "1", "formatversion": "2",
    })
    req = urllib.request.Request(f"{PARSE_API}?{params}", headers={"User-Agent": ua})
    html = None
    cacheable_miss = False  # API answered with no article (vs transient net error)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            html = (data.get("parse") or {}).get("text")
            if isinstance(html, dict):  # formatversion=1 shape
                html = html.get("*")
            cacheable_miss = not html
            break
        except urllib.error.HTTPError as e:
            if (e.code in (429, 500, 502, 503, 504)) and attempt < 3:
                time.sleep(2 ** attempt); continue
            cacheable_miss = 400 <= e.code < 500
            break
        except Exception:
            if attempt < 3:
                time.sleep(2 ** attempt); continue
            break                   # network/timeout — do NOT poison the cache
    # Persist the result so rebuilds never re-crawl: real HTML, or an empty
    # file as a known-miss marker (read back as "" -> None, no refetch).
    # Skip only transient network failures so they retry next build.
    if cache_file is not None and (html or cacheable_miss):
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write(html or "")
    return html


def bundle_wiki_articles(
    titles: Iterable[str],
    add_item: Callable[[str, str, str, bytes], None],
    *,
    cache_dir: Optional[str] = None,
    user_agent: str = USER_AGENT,
    offline_zim: Optional[str] = None,
    limit: Optional[int] = None,
    sleep: float = 0.1,
    log: Callable[[str], None] = print,
    images: str = "none",
    image_max_kb: int = 128,
    source=None,
) -> dict:
    """Fetch + clean + store each distinct article at `wiki-article/<Title>`.

    `add_item(path, title, mimetype, content_bytes)` is the storage callback
    (wired to `creator.add_item(MapItem(...))` in the build; a plain dict
    collector in tests). Returns stats.
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

    if images not in IMAGE_MODES:
        raise ValueError(f"images must be one of {IMAGE_MODES}, got {images!r}")
    src = source if source is not None else (_OfflineZim(offline_zim) if offline_zim else None)
    if images != "none" and (src is None or not hasattr(src, "image")):
        log("    bundle-wiki-articles: images need an offline source ZIM — "
            "bundling text only")
        images = "none"
    image_paths: set = set()       # wiki-image/<sha>.<ext> already stored
    images_stored = image_bytes = 0
    # Cache by default for the online path so a rebuild never re-crawls
    # Wikipedia (repo-relative, like wikidata_cache/). Offline (local ZIM)
    # needs no cache — reads are local.
    if src is None and cache_dir is None:
        cache_dir = str(DEFAULT_CACHE_DIR)
    log(f"    bundle-wiki-articles: {len(norm)} distinct titles"
        + (f" from {os.path.basename(offline_zim) if offline_zim else type(src).__name__}"
           if src else f" via Wikipedia API (cache: {cache_dir})"))

    bundled = failed = total_bytes = 0
    stored_titles: set = set()   # title_us actually written — for the geo-index
    for i, title_us in enumerate(norm, 1):
        raw = src.html(title_us) if src else _fetch_online(title_us, cache_dir, user_agent)
        if not raw:
            failed += 1
        else:
            disp = title_us.replace("_", " ")
            url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title_us)
            lead_html = gallery_html = ""
            if images != "none":
                figs = []
                for isrc, caption in image_candidates(raw):
                    got = src.image(isrc)
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
                    lead_html = (f'<figure class="lead"><img src="../wiki-image/{name}" '
                                 f'alt="{caption}" loading="lazy">{cap}</figure>\n')
                    if len(figs) > 1:
                        cells = "".join(
                            f'<figure><img src="../wiki-image/{n}" alt="{c}" loading="lazy">'
                            + (f"<figcaption>{c}</figcaption>" if c else "") + "</figure>"
                            for n, c in figs[1:])
                        gallery_html = f'<section class="gallery">{cells}</section>\n'
            page = clean_article_html(raw, disp, url, lead_html, gallery_html).encode("utf-8")
            add_item(f"wiki-article/{title_us}", disp, "text/html", page)
            stored_titles.add(title_us)
            bundled += 1
            total_bytes += len(page)
        if not src and sleep:
            time.sleep(sleep)
        if i % 250 == 0:
            log(f"    ... {i}/{len(norm)} bundled={bundled} failed={failed} "
                f"{total_bytes // 1024} KB")
    stats = {"requested": len(norm), "bundled": bundled, "failed": failed,
             "bytes": total_bytes, "stored_titles": stored_titles,
             "images": images_stored, "image_bytes": image_bytes}
    log(f"    bundle-wiki-articles: stored {bundled} articles "
        f"({total_bytes / 1024:.0f} KB), {failed} unavailable"
        + (f"; {images_stored} images ({image_bytes / 1024 / 1024:.0f} MB, mode={images})"
           if images != "none" else ""))
    return stats
