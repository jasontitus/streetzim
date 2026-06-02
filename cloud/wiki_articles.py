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


def clean_article_html(html: str, title: str, source_url: str) -> str:
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
        "font-size:.85em;color:#666}</style></head><body>"
        f"<h1>{safe_title}</h1>\n{h}\n"
        f"<footer>From <a href=\"{source_url}\">Wikipedia</a> — text under "
        "<a href=\"https://creativecommons.org/licenses/by-sa/4.0/\">"
        "CC BY-SA 4.0</a>.</footer></body></html>"
    )


# ---- fetching -------------------------------------------------------------

class _OfflineZim:
    """Lazy reader for a local Wikipedia ZIM (offline article source)."""
    def __init__(self, path: str):
        from libzim.reader import Archive  # lazy: only when offline source used
        self.a = Archive(path)

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
            if e.code in (429, 503) and attempt < 3:
                time.sleep(2 ** attempt); continue
            cacheable_miss = True   # 4xx (e.g. 404) = a real miss, cache it
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

    src = _OfflineZim(offline_zim) if offline_zim else None
    # Cache by default for the online path so a rebuild never re-crawls
    # Wikipedia (repo-relative, like wikidata_cache/). Offline (local ZIM)
    # needs no cache — reads are local.
    if src is None and cache_dir is None:
        cache_dir = str(DEFAULT_CACHE_DIR)
    log(f"    bundle-wiki-articles: {len(norm)} distinct titles"
        + (f" from {os.path.basename(offline_zim)}" if src
           else f" via Wikipedia API (cache: {cache_dir})"))

    bundled = failed = total_bytes = 0
    for i, title_us in enumerate(norm, 1):
        raw = src.html(title_us) if src else _fetch_online(title_us, cache_dir, user_agent)
        if not raw:
            failed += 1
        else:
            disp = title_us.replace("_", " ")
            url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title_us)
            page = clean_article_html(raw, disp, url).encode("utf-8")
            add_item(f"wiki-article/{title_us}", disp, "text/html", page)
            bundled += 1
            total_bytes += len(page)
        if not src and sleep:
            time.sleep(sleep)
        if i % 250 == 0:
            log(f"    ... {i}/{len(norm)} bundled={bundled} failed={failed} "
                f"{total_bytes // 1024} KB")
    stats = {"requested": len(norm), "bundled": bundled, "failed": failed,
             "bytes": total_bytes}
    log(f"    bundle-wiki-articles: stored {bundled} articles "
        f"({total_bytes / 1024:.0f} KB), {failed} unavailable")
    return stats
