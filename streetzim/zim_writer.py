"""ZIM packaging: create_zim and its helpers (search detail pages, hot
search-chunk splitting, the xapianbuilder full-text path). Moved verbatim
from create_osm_zim.py, which re-exports these names."""
import gzip
import html as html_mod
import json
import os
import shutil
import tempfile
import time
import urllib.parse
from functools import wraps
from pathlib import Path
from typing import NamedTuple

from cloud.viewer_slots import pad_to_slot as _pad_to_slot
from streetzim.search_extract import build_location_index
from streetzim import area as _area
from streetzim.cpus import compression_cpus
# The builder's flushing, phase-timing print (see streetzim/common.py).
from streetzim.common import (
    PHASE_TIMER,
    _HTTP_OK_RE,
    print,
    VIEWER_DIR,
    REPO_ROOT,
    _SEARCH_COORD_DP,
    peak_rss_bytes,
)
from streetzim.routing.build import (
    chunk_graph_file,
)
from streetzim.tiles import (
    estimate_tile_total,
    iter_tiles_from_mbtiles,
)
from streetzim.tile_alias import TileAliaser, max_alias_bytes
from streetzim import viewer_assets


# Search records that get a Kiwix page (search/<slug>.html), and with it an
# entry in Kiwix's full-text search (libzim indexes the page) and in its
# title suggestions (the pages are front articles; libzim's title index
# holds only those, so without it Kiwix suggested nothing). Streets and addresses never
# do; the in-map search has them. POIs do only with --kiwix-poi-pages
# (kiwix_poi_pages=True): without them kiwix-serve's search for "Casino" in
# Monaco finds the Fontaine du Casino (a lake) and none of the shops, stops
# and sights named Casino. Measured on 2026-09-29: about 440 B per named POI
# (page, dirent, full-text and title index), Monaco +28% (1.8k pages),
# Luxembourg +16% (56.7 -> 66.0 MB, 21k pages), so ~ +19% for Switzerland
# and +12% for the Netherlands; build time within noise. docs/zimfarm.md.
# Administrative areas (`admin`, streetzim/admin_areas.py) always get one:
# they are what maps2zim's Kiwix search is made of.
KIWIX_PAGE_TYPES = frozenset({"place", "airport", "park", "peak", "water", "admin"})

# The viewer zoom a search page's "View on map" opens at, by record type
# (an admin area with a box is fitted to it instead).
PAGE_ZOOM = {"place": 14, "airport": 14, "peak": 15, "park": 15,
             "water": 14, "poi": 17, "street": 16}
# An admin area without a box (one the extract clips), by admin_level.
ADMIN_ZOOM = {2: 5, 3: 6, 4: 7, 5: 8, 6: 10, 7: 11, 8: 12, 9: 13, 10: 14}


def kiwix_page_types(poi_pages: bool = False) -> frozenset[str]:
    """The record types that get a Kiwix page in this build."""
    return KIWIX_PAGE_TYPES | {"poi"} if poi_pages else KIWIX_PAGE_TYPES


def admin_wiki(feat, wiki_cross_refs):
    """The wiki cross-ref entry of an admin area: its relation's own
    `wikipedia`/`wikidata` tags as the build resolved them (put in the
    lookup by admin_areas.add_admin_wiki_refs, so a non-English tag may
    have become its item's English article), else the tags on the record
    itself. None when it has neither."""
    if wiki_cross_refs and feat.get("osm"):
        from streetzim.admin_areas import admin_wiki_key
        entry = wiki_cross_refs.get(admin_wiki_key(feat["osm"]))
        if entry:
            return entry
    return {k: feat[k] for k in ("wikipedia", "wikidata") if feat.get(k)} or None


def admin_record_fields(feat):
    """The search-record keys an administrative area adds
    (docs/search-records.md): al, bb, alt, osm. Empty for other types."""
    if feat.get("type") != "admin":
        return {}
    out = {}
    if feat.get("admin_level") is not None:
        out["al"] = int(feat["admin_level"])
    bb = feat.get("bbox")
    if bb and len(bb) == 4:
        out["bb"] = [round(float(v), _SEARCH_COORD_DP) for v in bb]
    if feat.get("alt"):
        out["alt"] = list(feat["alt"])
    if feat.get("osm"):
        out["osm"] = feat["osm"]
    return out


def _names_type(name, label):
    """Whether `name` says `label` as a word: "Arlington County" says
    county, "Georgetown" does not say town nor "Statesboro" state."""
    import re
    return bool(label) and re.search(
        r"(?<!\w)" + re.escape(label.casefold()) + r"(?!\w)", name.casefold()) is not None


def _title_text(text):
    """A ZIM title without control characters (Unicode Cc: a newline or a
    tab in an OSM name, "Tunda\\nBhuj"), which both packers refuse: each
    becomes a space, and the spaces around it one."""
    import unicodedata
    if not any(unicodedata.category(c) == "Cc" for c in text):
        return text
    import re
    out = "".join(" " if unicodedata.category(c) == "Cc" else c for c in text)
    return re.sub(r" {2,}", " ", out).strip()


def kiwix_page_title(feat):
    """A search page's title (what Kiwix suggests). An admin area's name
    gets its type when the name does not say it: "Alexandria (city)",
    but "Arlington County"."""
    name = _title_text(feat["name"])
    if feat.get("type") == "admin":
        label = (feat.get("subtype") or "").strip()
        if label and not _names_type(name, label):
            return f"{name} ({label})"
    return name


# Types whose English official names read "<Type> of <Name>" ("City of
# Alexandria", "Town of Capitol Heights", "Canton of Geneva").
FORMAL_OF_LABELS = frozenset({"city", "town", "village", "borough",
                              "municipality", "commune", "canton",
                              "province", "state"})
MAX_ALT_TITLES = 6


def kiwix_alt_titles(feat):
    """Other titles an admin area's page is suggested under (redirects to
    it, front articles): its other names, and "<Type> of <Name>" for the
    types in FORMAL_OF_LABELS when the name does not already say the
    type. Kiwix's title search wants every word typed, so "City of
    Alexandria" never found a page titled "Alexandria (city)"."""
    if feat.get("type") != "admin":
        return []
    name = _title_text(feat["name"])
    label = (feat.get("subtype") or "").strip()
    cands = []
    if label in FORMAL_OF_LABELS and not _names_type(name, label):
        cands.append(f"{label[:1].upper()}{label[1:]} of {name}")
    cands += [_title_text(a) for a in feat.get("alt") or []]
    seen = {name.casefold(), kiwix_page_title(feat).casefold()}
    out = []
    for c in cands:
        if c and c.casefold() not in seen:
            seen.add(c.casefold())
            out.append(c)
    return out[:MAX_ALT_TITLES]


def _add_redirect(creator, path, title, target, front=False):
    """A ZIM redirect, on either writer. Both libzim's Creator and
    cloud/manifest_writer.py's take the FRONT_ARTICLE hint; the fallback
    serves creators without a hints argument."""
    try:
        from libzim.writer import Hint
        creator.add_redirection(path, title, target, {Hint.FRONT_ARTICLE: front})
    except (ImportError, TypeError):
        creator.add_redirection(path, title, target)


def kiwix_alt_redirects(page_path, feat):
    """(path, title) of each redirect add_alt_titles writes to `page_path`."""
    stem = page_path[:-len(".html")]
    return [(f"{stem}~{k}.html", t) for k, t in enumerate(kiwix_alt_titles(feat))]


def add_alt_titles(creator, page_path, feat):
    """The redirects for kiwix_alt_titles(feat) to `page_path`, as front
    articles so Kiwix suggests them. Returns how many were added."""
    alts = kiwix_alt_redirects(page_path, feat)
    for path, title in alts:
        _add_redirect(creator, path, title, page_path, front=True)
    return len(alts)


def kiwix_page_hash(feat):
    """The viewer fragment a search page's "View on map" opens. An admin
    area with a box: centred on the box at the zoom that fits it, with
    `bounds=` (which a viewer that knows it fits exactly) and a pin on
    the area's point."""
    lat, lon = feat["lat"], feat["lon"]
    if feat.get("type") == "admin":
        label_q = urllib.parse.quote(feat["name"], safe="")
        bb = feat.get("bbox")
        if bb and len(bb) == 4:
            from streetzim.admin_areas import fit_zoom
            w, s_, e, n = (float(v) for v in bb)
            return (f"map={fit_zoom(bb)}/{(s_ + n) / 2:.5f}/{(w + e) / 2:.5f}"
                    f"&bounds={w:.5f},{s_:.5f},{e:.5f},{n:.5f}"
                    f"&pin={lat},{lon}&label={label_q}")
        z = ADMIN_ZOOM.get(int(feat.get("admin_level") or 8), 12)
        return f"map={z}/{lat}/{lon}&pin={lat},{lon}&label={label_q}"
    return f"map={PAGE_ZOOM.get(feat['type'], 15)}/{lat}/{lon}"


# The credit on an admin area's page whose region or point came from
# GeoNames (streetzim/admin_areas.py); the viewer's About panel says the same.
GEONAMES_CREDIT = ("Region names beside search results, and the location of "
                   "administrative areas cut off by the OSM extract: GeoNames "
                   "(geonames.org), CC BY 4.0")


def search_page(feat, i):
    """(path, title, html) of the Kiwix page for search feature `feat`,
    the i-th page written."""
    slug = feat["name"].lower()
    slug = "".join(c if c.isalnum() or c in "-_ " else "" for c in slug)
    slug = slug.strip().replace(" ", "-")[:80]
    slug = f"{slug}-{i}"
    # Prefer Overture's normalized category for display
    # when present (falls back to OMT subtype / OSM type).
    kind_raw = feat.get("cat") or feat.get("subtype") or feat["type"]
    label = kind_raw.replace("_", " ").title()
    also = None
    credit = None
    if feat.get("type") == "admin":
        label = label[:1].upper() + kind_raw.replace("_", " ")[1:]
        if feat.get("location"):
            label += f" in {feat['location']}"
        also = feat.get("alt") or None
        if feat.get("geonames"):
            credit = GEONAMES_CREDIT
    enrich = {k: feat[k] for k in ("ws", "p", "soc", "brand", "wd")
              if feat.get(k)}
    page_html = search_detail_html(
        feat["name"], label, feat["lat"], feat["lon"], kiwix_page_hash(feat),
        enrich=enrich, also_known_as=also, title=kiwix_page_title(feat),
        record_type=feat.get("type"), credit=credit)
    return f"search/{slug}.html", kiwix_page_title(feat), page_html


def search_detail_html(name, kind_label, lat, lon, map_hash, enrich=None,
                       also_known_as=None, title=None, record_type=None,
                       credit=None):
    """HTML for a search-result detail page (`search/<slug>.html`).

    CTAs: "Directions to here" + "View on map" (no auto-redirect any
    more). The viewer parses `index.html#dest=lat,lon&label=…` on load
    and pops the routing panel open — see `applyHash` in
    `resources/viewer/src/index/120-map-init-and-style.js`.

    `enrich` is an optional dict sourced from Overture's places theme:
        {"ws": website, "p": phone, "soc": [social urls],
         "brand": brand name, "wd": wikidata Q-ID, "cat": category}
    Rendered as a compact contact block below the kind label when
    any field is present. Empty / missing fields are skipped so the
    page stays readable for plain OSM-only POIs.

    Note: the enrichment key for *website* is `ws`, not `w`. `w` is
    reserved for the Wikipedia tag ("en:Article_Title") that OSM POIs
    carry in the same record — colliding the two corrupts downstream
    consumers (mcpzim reads `rec["w"]` as a wiki title; feeding it a
    URL breaks article lookup).
    """
    safe_name = html_mod.escape(name)
    safe_title = html_mod.escape(title or name)
    safe_kind = html_mod.escape(kind_label)
    also_html = ""
    if also_known_as:
        also_html = ('<p class="also">Also: '
                     + html_mod.escape(", ".join(also_known_as)) + "</p>")
    label_q = urllib.parse.quote(name, safe="")
    dest_hash = f"dest={lat},{lon}&label={label_q}"

    enrich = enrich or {}
    contact_html = ""
    contact_parts = []
    if enrich.get("brand"):
        contact_parts.append(
            f'<p class="brand">{html_mod.escape(enrich["brand"])}</p>')
    links = []
    _http_ok = _HTTP_OK_RE
    # Only http(s) URLs may become hrefs — index data is not trusted
    # (a javascript: value would run on tap). Same rule as places.html.
    if enrich.get("ws") and _http_ok.match(str(enrich["ws"]).strip()):
        w = str(enrich["ws"]).strip()
        w_show = html_mod.escape(w)
        w_attr = html_mod.escape(w, quote=True)
        links.append(
            f'<a href="{w_attr}" target="_blank" rel="noopener noreferrer">'
            f'🌐 {w_show}</a>')
    if enrich.get("p"):
        p = enrich["p"]
        p_attr = html_mod.escape(p.replace(" ", ""), quote=True)
        links.append(
            f'<a href="tel:{p_attr}">📞 {html_mod.escape(p)}</a>')
    for s in (enrich.get("soc") or [])[:3]:
        if not isinstance(s, str) or not _http_ok.match(s.strip()):
            continue
        s = s.strip()
        s_attr = html_mod.escape(s, quote=True)
        host = s.lower()
        if "facebook" in host:   g = "Facebook"
        elif "instagram" in host: g = "Instagram"
        elif "twitter" in host or "x.com" in host: g = "X / Twitter"
        elif "tiktok" in host:   g = "TikTok"
        else:                    g = "Social"
        links.append(
            f'<a href="{s_attr}" target="_blank" rel="noopener noreferrer">'
            f'{g}</a>')
    if enrich.get("wd"):
        wd = html_mod.escape(enrich["wd"], quote=True)
        links.append(
            f'<a href="https://www.wikidata.org/wiki/{wd}" '
            'target="_blank" rel="noopener noreferrer">Wikidata</a>')
    if links:
        contact_parts.append(
            '<ul class="contact">' +
            "".join(f'<li>{l}</li>' for l in links) +
            '</ul>')
    if contact_parts:
        contact_html = "".join(contact_parts)

    return (
        '<!DOCTYPE html><html><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{safe_title}</title>'
        '<style>'
        'body{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;'
        'margin:0;padding:24px;max-width:640px;color:#1a1a1a;'
        'background:#fafafa;line-height:1.45}'
        'h1{margin:0 0 4px;font-size:1.6rem}'
        'p.kind{margin:0 0 14px;color:#666;font-size:0.95rem}'
        'p.brand{margin:0 0 10px;color:#666;font-style:italic;font-size:0.95rem}'
        'p.also{margin:0 0 14px;color:#666;font-size:0.95rem}'
        'p.credit{margin:8px 0 0;color:#888;font-size:0.8rem}'
        'ul.contact{list-style:none;padding:0;margin:0 0 18px;'
        'display:flex;flex-direction:column;gap:6px;font-size:0.95rem}'
        'ul.contact a{color:#0a7cff;text-decoration:none;word-break:break-all}'
        'ul.contact a:hover{text-decoration:underline}'
        'p.coords{margin:18px 0 0;color:#888;font-size:0.85rem;'
        'font-family:ui-monospace,Menlo,monospace}'
        '.cta{display:flex;flex-direction:column;gap:10px;margin-top:14px}'
        '.cta a{display:block;padding:12px 16px;border-radius:10px;'
        'text-decoration:none;font-weight:600;text-align:center;'
        'border:1px solid #d0d0d0;color:#1a1a1a;background:#fff}'
        '.cta a.primary{background:#0a7cff;color:#fff;border-color:#0a7cff}'
        '.cta a:active{transform:scale(0.99)}'
        '@media(prefers-color-scheme:dark){'
        'body{background:#111;color:#eee}p.kind,p.brand{color:#aaa}p.coords{color:#888}'
        '.cta a{background:#1c1c1c;border-color:#333;color:#eee}'
        '.cta a.primary{background:#0a7cff;color:#fff;border-color:#0a7cff}}'
        '</style>'
        '</head>'
        # The search record's type, for tools that read the page.
        + (f'<body data-type="{html_mod.escape(record_type, quote=True)}">'
           if record_type else '<body>') +
        f'<h1>{safe_name}</h1>'
        f'<p class="kind">{safe_kind}</p>'
        f'{also_html}'
        f'{contact_html}'
        # Search detail pages live at `search/<slug>.html` inside the
        # ZIM. A bare `index.html#...` resolves to `search/index.html`
        # (which doesn't exist) — zimcheck flagged hundreds of these
        # as broken internal URLs and Kiwix's library validator
        # treats the whole ZIM as Fail. Use `../index.html` so the
        # link reaches the viewer at the ZIM root regardless of
        # how the host (Kiwix Desktop, kiwix-serve, our PWA's SW)
        # serves the path.
        '<div class="cta">'
        f'<a class="primary" href="../index.html#{dest_hash}">'
        'Directions to here</a>'
        f'<a href="../index.html#{map_hash}">View on map</a>'
        '</div>'
        f'<p class="coords">{lat:.5f}, {lon:.5f}</p>'
        + (f'<p class="credit">{html_mod.escape(credit)}</p>' if credit else '') +
        '</body></html>'
    )


# FNV-1a sub-bucket hash; one copy in cloud/search_shards.py. MUST match the
# viewer's subBucketFor and mcpzim's Geocoder.subBucketFor.
from cloud.search_shards import sub_bucket_for_name as _sub_bucket_for_name  # noqa: E402


def _split_big_search_chunk(prefix: str, records: list, n_buckets: int = 16
                            ) -> list[tuple[str, bytes]]:
    """Fan out `records` into up to `n_buckets` sub-chunks based on
    FNV-1a hash of record['n']. Returns [(sub_prefix, json_bytes), …]
    — same on-disk format the repackage writer + JS/Swift readers
    expect. Empty buckets are omitted (not emitted)."""
    import json as _json
    buckets: list[list] = [[] for _ in range(n_buckets)]
    for rec in records:
        name = rec.get("n", "") or ""
        buckets[_sub_bucket_for_name(name, n_buckets)].append(rec)
    hex_width = len(format(n_buckets - 1, "x"))
    out = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        sub_prefix = f"{prefix}-{format(i, f'0{hex_width}x')}"
        sub_bytes = _json.dumps(bucket, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
        out.append((sub_prefix, sub_bytes))
    return out


def _resolve_xapianbuilder_binary(override: str | None = None) -> str:
    """Locate the xapianbuilder binary. Resolution order:
    1. ``override`` argument (typically the --xapianbuilder-bin flag).
    2. ``$XAPIANBUILDER_BIN`` env var.
    3. ``../xapianbuilder/target/release/xapianbuilder``.
    4. ``../xapianbuilder/target/debug/xapianbuilder``.

    Raises FileNotFoundError if none found.
    """
    env_path = os.environ.get("XAPIANBUILDER_BIN")
    explicit = override or env_path
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise FileNotFoundError(f"xapianbuilder is not an executable file: {explicit!r}")
        return str(candidate.resolve())
    repo_root = REPO_ROOT
    candidates = [repo_root.parent / "xapianbuilder" / "target" / build / "xapianbuilder"
                  for build in ("release", "debug")]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c.resolve())
    raise FileNotFoundError(
        "xapianbuilder binary not found. Build it with "
        "`cd ../xapianbuilder && cargo build --release` or pass "
        "--xapianbuilder-bin=PATH (or set $XAPIANBUILDER_BIN). "
        f"Tried: {candidates}"
    )


def xapianbuilder_doc(feat, path, *, language="eng", target_path=""):
    """The xapianbuilder input record (``{"path", "title", "mimetype",
    "body", "language", "target_path"}``) of search feature `feat` at
    `path`. Indexable body: the fields libzim's HTML-stub auto-indexer
    would have seen (name + location + type + subtype + category + brand,
    and an admin area's other names and formal titles, kiwix_alt_titles),
    with a ``geo.position`` meta tag so xapianbuilder's MyHtmlParser fills
    value slot 2 with the lat/lon (on parity with libzim's path). Title:
    kiwix_page_title. ``target_path``
    (title index only): the page a redirect title points at."""
    import html as _html
    body_parts = [feat.get("name") or ""]
    for k in ("location", "type", "subtype", "cat", "brand"):
        v = feat.get(k)
        if v:
            body_parts.append(str(v))
    body_parts += [str(a) for a in feat.get("alt") or ()]
    # An admin area's formal title ("Town of Moneghetti"): Kiwix's full text
    # wants every typed word, "of" too.
    body_parts += [t for t in kiwix_alt_titles(feat) if t not in body_parts]
    body_text = " ".join(body_parts)
    lat = feat.get("lat", 0)
    lon = feat.get("lon", 0)
    body_html = (
        f'<html><head><meta name="geo.position" '
        f'content="{lat};{lon}"></head><body>{_html.escape(body_text)}'
        f'</body></html>'
    )
    return {
        "path": path,
        "title": kiwix_page_title(feat),
        "mimetype": "text/html",
        "body": body_html,
        "language": language,
        "target_path": target_path,
    }


class XapianCorpus:
    """The xapianbuilder inputs of a --xapian=builder build: what libzim's
    indexer would take in a --xapian=libzim build of the same features.

    ``page(feat, path)``: the Kiwix page at `path` (search_page) as a
    document of both indexes (xapianbuilder_doc), and each of its redirects
    (kiwix_alt_redirects; front articles, so libzim puts their titles in the
    title index) as a title document at the redirect, ``target_path`` the
    page. ``fulltext(path, title, html)``: another text/html entry libzim
    would index (the bundled Wikipedia articles: not front articles, so in
    the full text only).

    Before 2026-10-02 the build fed xapianbuilder documents named
    ``s/<n>`` and wrote nothing there (f38cfb4, 2026-05-08): every
    Kiwix suggestion and full-text hit of a builder ZIM was a dead link
    (ops/cloud/swap_viewer_rust.py --rebuild-xapian mends published ones).
    """

    def __init__(self, workdir, language="eng"):
        os.makedirs(workdir, exist_ok=True)
        self.language = language
        self.paths = {"fulltext": os.path.join(workdir, "_xapian-fulltext.jsonl"),
                      "title": os.path.join(workdir, "_xapian-title.jsonl")}
        self._ft = open(self.paths["fulltext"], "w", encoding="utf-8")
        self._ti = open(self.paths["title"], "w", encoding="utf-8")
        self.pages = self.redirects = self.extra = 0

    def _put(self, f, rec):
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def page(self, feat, path):
        doc = xapianbuilder_doc(feat, path, language=self.language)
        self._put(self._ft, doc)
        self._put(self._ti, doc)
        self.pages += 1
        for rpath, title in kiwix_alt_redirects(path, feat):
            self._put(self._ti, {"path": rpath, "title": title, "mimetype": "text/html",
                                 "body": "", "language": self.language,
                                 "target_path": path})
            self.redirects += 1

    def fulltext(self, path, title, content):
        if isinstance(content, (bytes, bytearray)):
            content = bytes(content).decode("utf-8", "replace")
        self._put(self._ft, {"path": path, "title": title or path,
                             "mimetype": "text/html", "body": content,
                             "language": self.language, "target_path": ""})
        self.extra += 1

    def close(self):
        """Close both inputs; returns ``{"fulltext": path, "title": path}``."""
        self._ft.close()
        self._ti.close()
        return dict(self.paths)

    def remove(self):
        self.close()
        for p in self.paths.values():
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


_XAPIAN_TERMINATE_TIMEOUT = 5.0


def _glass_stamp(glass_path: str) -> str:
    return glass_path + ".input-sha256"


def _glass_key(input_path: str, language: str) -> str:
    """The hash of a xapianbuilder input file and its language."""
    import hashlib
    h = hashlib.sha256(f"{language}\0".encode())
    with open(input_path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _glass_reusable(glass_path: str, key: str) -> bool:
    """Whether `glass_path` exists, is nonempty and was built from the input
    whose _glass_key is `key`."""
    try:
        if os.path.getsize(glass_path) <= 0:
            return False
        with open(_glass_stamp(glass_path)) as f:
            return f.read().strip() == key
    except OSError:
        return False


def _build_xapian_via_xapianbuilder(workdir: str,
                                    *,
                                    inputs: dict[str, str],
                                    language: str = "eng",
                                    binary_override: str | None = None,
                                    jobs: int = 0,
                                    ) -> tuple[str, str]:
    """Run xapianbuilder over its inputs, per mode (``{"fulltext": path,
    "title": path}``, xapianbuilder JSONL: XapianCorpus writes them for a
    build, ops/cloud/swap_viewer_rust.py --rebuild-xapian for a retrofit;
    the title input may carry redirect titles, ``target_path``, that the
    full-text input must not) and return ``(fulltext_glass_path,
    title_glass_path)`` in `workdir`.

    Idempotent: a mode's glass file already in `workdir` (resuming a
    --keep-temp build that crashed at the pack step) is reused only when
    the stamp beside it (``<glass>.input-sha256``) is the hash of this
    run's input and language. A builder corpus changes between runs (the
    numbering of same-name pages follows the feature order), so an index
    built from another run's corpus would point at the wrong pages.

    The fulltext and title runs are independent processes, run in
    parallel; Python's memory stays constant.
    """
    import time
    import subprocess
    binary = _resolve_xapianbuilder_binary(binary_override)
    os.makedirs(workdir, exist_ok=True)
    ft_glass = os.path.join(workdir, "X-fulltext-xapian.glass")
    ti_glass = os.path.join(workdir, "X-title-xapian.glass")

    # Recovery: skip a builder run whose output exists and was built from
    # this very input (_glass_stamp).
    keys = {mode: _glass_key(inputs[mode], language) for mode in ("fulltext", "title")}
    have_ft = _glass_reusable(ft_glass, keys["fulltext"])
    have_ti = _glass_reusable(ti_glass, keys["title"])
    if have_ft and have_ti:
        print(f"      reusing existing Xapian glass DBs ({os.path.getsize(ft_glass)/1e6:.1f} MB ft, {os.path.getsize(ti_glass)/1e6:.1f} MB title)", flush=True)
        return ft_glass, ti_glass

    procs = []
    proc_starts: dict[str, float] = {}
    completed = False
    try:
        for mode, out_path, missing in (
            ("fulltext", ft_glass, not have_ft),
            ("title",    ti_glass, not have_ti),
        ):
            if not missing:
                continue
            # Output file must NOT exist (xapianbuilder refuses to
            # overwrite). Remove any prior partial.
            for stale in (out_path, _glass_stamp(out_path)):
                try: os.unlink(stale)
                except FileNotFoundError: pass
            cmd = [binary, mode,
                   "--input", inputs[mode],
                   "--output", out_path,
                   "--language", language,
                   "--jobs", str(jobs),
                   "--quiet"]
            print(f"      launching xapianbuilder {mode} → {os.path.basename(out_path)}", flush=True)
            proc_starts[mode] = time.time()
            procs.append((mode, subprocess.Popen(cmd)))

        failures = []
        proc_durs: dict[str, float] = {}
        pair_t0 = time.time()
        for mode, p in procs:
            rc = p.wait()
            proc_durs[mode] = time.time() - proc_starts[mode]
            if rc != 0:
                failures.append((mode, rc))
        pair_wall = time.time() - pair_t0
        if failures:
            details = ", ".join(f"{m}: rc={rc}" for m, rc in failures)
            raise RuntimeError(f"xapianbuilder failed ({details})")
        for mode, _ in procs:
            out_path = ft_glass if mode == "fulltext" else ti_glass
            with open(_glass_stamp(out_path), "w") as f:
                f.write(keys[mode])
        completed = True
    finally:
        # A failed second launch or an interrupted wait must not leave an
        # indexer writing into scratch files that its caller is removing.
        # Send TERM to both before waiting, then reap each and escalate any
        # child that does not stop within the grace period.
        running = [p for _, p in procs if p.poll() is None]
        for process in running:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        for process in running:
            try:
                process.wait(timeout=_XAPIAN_TERMINATE_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait()
        if not completed:
            # Resume accepts a nonempty glass file, so failed jobs must not
            # leave partial databases behind. Reused databases were never
            # launched and must survive a failed attempt at the other index.
            for mode, _ in procs:
                out_path = ft_glass if mode == "fulltext" else ti_glass
                try:
                    os.unlink(out_path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    print(f"      warning: could not remove failed {mode} index {out_path}: {exc}", flush=True)

    ft_size = os.path.getsize(ft_glass)
    ti_size = os.path.getsize(ti_glass)
    print(f"      xapianbuilder done — fulltext {ft_size/1e6:.0f} MB ({proc_durs.get('fulltext',0):.0f}s), title {ti_size/1e6:.0f} MB ({proc_durs.get('title',0):.0f}s); parallel wall-clock {pair_wall:.0f}s", flush=True)
    if "fulltext" in proc_durs:
        PHASE_TIMER.record_subphase(
            "xapian: build fulltext", proc_durs["fulltext"],
            note=f"{ft_size/1e6:.0f} MB glass DB")
    if "title" in proc_durs:
        PHASE_TIMER.record_subphase(
            "xapian: build title", proc_durs["title"],
            note=f"{ti_size/1e6:.0f} MB glass DB")
    PHASE_TIMER.record_subphase(
        "xapian: parallel wall-clock", pair_wall,
        note="max(ft, title) — both ran concurrently")
    PHASE_TIMER.record_metric(
        "xapian: fulltext glass size", f"{ft_size/1e6:.0f}", "MB")
    PHASE_TIMER.record_metric(
        "xapian: title glass size", f"{ti_size/1e6:.0f}", "MB")
    return ft_glass, ti_glass


def _create_zim(
    output_path,
    tiles,
    tile_metadata,
    fonts,
    maplibre_js_path,
    maplibre_css_path,
    viewer_html_path,
    map_config,
    name,
    mbtiles_path=None,
    tile_count=None,
    description="Offline OpenStreetMap",
    cluster_size=2048 * 1024,
    search_features=None,
    search_features_path=None,
    satellite_dir=None,
    satellite_max_zoom=None,
    satellite_format="webp",
    terrain_dir=None,
    terrain_max_zoom=None,
    zim_workers=None,
    bbox=None,
    wikidata_data=None,
    routing_graph_path=None,
    routing_graph_chunk_mb=0,
    wiki_cross_refs=None,
    address_count=0,
    overture_sources=None,
    overture_themes=None,
    overture_release=None,
    split_hot_search_chunks_mb=0,
    split_find_chips=False,
    zim_builder="python",
    max_zoom=None,
    xapian_mode="libzim",
    xapianbuilder_bin=None,
    xapian_workdir=None,
    no_llm_bundle=False,
    spatial_chunk_scale=0,
    bundle_wiki_articles=False,
    wiki_articles_cache=None,
    wiki_articles_source=None,
    wiki_images="none",
    wiki_image_max_kb=128,
    wiki_images_per_article=12,
    metadata=None,
    illustration=None,
    kiwix_poi_pages=False,
):
    """Create a ZIM file containing the map viewer and all tiles.

    ``xapian_mode``:
      ``"libzim"`` — emit search/<slug>.html stubs and let libzim's
        auto-indexer build the Xapian DBs at finalize. Default.
      ``"builder"`` — the same search pages and redirects, indexed by the
        external ``xapianbuilder`` instead (XapianCorpus: the pages, their
        redirect titles, the bundled Wikipedia articles) into glass DBs
        added at namespace 'X' with compress=False. Requires
        ``zim_builder='manifest'`` (or ``'rust'``) because the libzim
        Creator does not accept items in the X namespace via its public
        API.
      ``"none"`` — skip Xapian entirely: no X/fulltext/xapian and no
        X/title/xapian, so Kiwix has no full-text search and no Xapian
        title suggestions (libzim falls back to title-prefix matches over
        the front articles in the title listing). The in-ZIM places.html
        (which reads the JSON search-data chunks) is the only full search UI.

    ``metadata`` / ``illustration``: openZIM metadata overrides and a 48x48
    PNG, from the --title/--description/... flags (see _add_metadata).

    ``kiwix_poi_pages``: give named POIs a Kiwix page too (KIWIX_PAGE_TYPES).
    """
    from libzim.writer import Creator as LibzimCreator, Item, StringProvider, FileProvider, IndexData
    from libzim.writer import Hint
    if zim_workers is not None and zim_workers < 1:
        raise ValueError("compression workers must be positive")
    if zim_builder not in {"python", "manifest", "rust"}:
        raise ValueError(f"unknown ZIM builder: {zim_builder!r}")
    # 'rust' writes the same manifest as 'manifest' but packs it with a built
    # Rust streetzim-pack (ManifestCreator fails when there is none).
    pack_backend = zim_builder
    if zim_builder == "rust":
        zim_builder = "manifest"
    if zim_builder == "manifest":
        from cloud.manifest_writer import ManifestCreator
        # The Python manifest adapter explicitly configures this level. libzim's
        # effective default is not exposed through python-libzim's API.
        zstd_level = int(os.environ.get("ZSTD_CLEVEL", "22"))
        # Capture once for the closure so the lambda captures the
        # resolved value, not the name.
        _level = zstd_level
        Creator = lambda p: ManifestCreator(  # noqa: E731 — small adapter
            p, verbose=True, compression_level=_level, builder=pack_backend
        )
        print(f"  ZIM compression: zstd level {zstd_level} "
              f"({'Rust' if pack_backend == 'rust' else 'Python'} manifest path)", flush=True)
        # Surface the compression level as a build metric so
        # before/after comparisons can attribute size deltas correctly.
        PHASE_TIMER.record_metric(
            "zim-pack: zstd level", str(zstd_level), "")
    else:
        Creator = LibzimCreator
        # ZSTD_CLEVEL configures the manifest adapter; libzim uses its own
        # fixed compression default and does not read that environment flag.
        compression_settings = "libzim default"
        print(f"  ZIM compression: {compression_settings}", flush=True)
        PHASE_TIMER.record_metric(
            "zim-pack: compression settings", compression_settings, "")

    if xapian_mode == "builder" and zim_builder != "manifest":
        # libzim's public Creator API doesn't accept items at the X
        # namespace — that's reserved for libzim's own auto-indexer.
        # Pre-built Xapian DBs can only be injected via the manifest writer.
        raise ValueError(
            "--xapian=builder requires --zim-builder=manifest; "
            "libzim's Creator can't place items in the X namespace"
        )
    if zim_builder == "manifest" and xapian_mode == "libzim":
        raise ValueError("--zim-builder=manifest requires --xapian=builder or --xapian=none; "
                         "the manifest writer cannot run libzim's search indexer")

    print(f"  Creating ZIM file: {output_path}")
    print(f"    Name: {name}")
    print(f"    Tiles: {tile_count if tiles is None else len(tiles)}")
    print(f"    Fonts: {len(fonts)}")

    class MapItem(Item):
        """A single item (file) in the ZIM archive.

        ``namespace`` is captured for the manifest emit path
        (``cloud.manifest_writer.ManifestCreator``) which can place items
        into reserved namespaces such as ``'X'`` (Xapian indexes,
        compressed=False by Kiwix convention). The python-libzim path
        ignores it — libzim's public API doesn't accept per-item
        namespace, so any integrator using the libzim Creator must keep
        items in the default 'C' namespace and let libzim's
        ``config_indexing`` produce X-namespace entries itself.
        """
        def __init__(self, path, title, mimetype, content,
                     is_front=False, compress=True, namespace=None):
            super().__init__()
            self._path = path
            self._title = title
            self._mimetype = mimetype
            self._is_front = is_front
            self._compress = compress
            self._namespace = namespace
            # Keep application chrome out of Kiwix's full-text results. None
            # selects libzim's normal HTML indexer for content pages; an
            # empty IndexData explicitly excludes the two application pages.
            self.get_indexdata = (IndexData
                                  if path in {"index.html", "places.html"} else None)
            # Normalize content to bytes
            if isinstance(content, (str, Path)) and os.path.isfile(str(content)):
                self._file_path = str(content)
                self._data = None
            else:
                self._file_path = None
                self._data = content if isinstance(content, bytes) else str(content).encode("utf-8")

        def get_path(self):
            return self._path

        def get_title(self):
            return self._title

        def get_mimetype(self):
            return self._mimetype

        def get_contentprovider(self):
            if self._file_path:
                return FileProvider(self._file_path)
            return StringProvider(self._data)

        def get_hints(self):
            return {Hint.FRONT_ARTICLE: self._is_front, Hint.COMPRESS: self._compress}

    # Create ZIM file
    # config_indexing and set_mainpath must be called BEFORE __enter__
    creator = Creator(str(output_path))
    # libzim's auto-indexer ingests every text/html item we add and
    # writes X/fulltext/xapian + X/title/xapian at finalize. Skip it for
    # the builder/none modes; in 'builder' mode we'll inject pre-built
    # glass DBs directly, in 'none' mode we ship without Xapian and
    # rely on the in-ZIM places.html (JSON search-data) for search.
    creator.config_indexing(xapian_mode == "libzim", "en")
    creator.config_clustersize(cluster_size)
    # Cap Python's default worker count; callers can reduce it for hosts
    # where compression contexts and the queue consume too much memory.
    num_workers = zim_workers or min(compression_cpus(), 20)
    shown_workers = (os.environ.get("RAYON_NUM_THREADS", "automatic")
                     if zim_builder == "manifest" and zim_workers is None else num_workers)
    print(f"    ZIM compression workers: {shown_workers} (tiles: {tile_count if tiles is None else len(tiles)})", flush=True)
    if zim_builder != "manifest" or zim_workers is not None:
        creator.config_nbworkers(num_workers)
    creator.set_mainpath("index.html")
    has_wikidata = bool(wikidata_data)      # as map-config's hasWikidata
    if search_features and not isinstance(search_features, str):
        # An in-memory feature list: its admin areas' tags join the wiki
        # lookup here, so their articles are bundled like the rest (for a
        # search JSONL, create_osm_zim adds them before resolving titles).
        from streetzim.admin_areas import add_admin_wiki_refs
        refs = dict(wiki_cross_refs or {})
        if add_admin_wiki_refs(refs, search_features):
            wiki_cross_refs = refs
    # The search chunk files (GBs for a country; with --xapian builder, also
    # the Xapian databases, which libzim reads only as the creator closes).
    # Entered before the creator, so it is removed after the creator has
    # closed, after a failure as well: it used to be left behind in the
    # system temp dir whenever a build failed or used --xapian builder.
    with tempfile.TemporaryDirectory(prefix="streetzim_chunks_", dir=xapian_workdir,
                                     ignore_cleanup_errors=True) as chunk_tmp, creator:
        # --xapian=builder: xapianbuilder's inputs, what libzim's indexer
        # would take in a --xapian=libzim build (the Kiwix pages, their
        # redirect titles, the bundled Wikipedia articles).
        corpus = (XapianCorpus(os.path.join(chunk_tmp, "xapian-corpus"))
                  if xapian_mode == "builder" else None)
        _add_viewer(creator, MapItem, maplibre_js_path=maplibre_js_path,
                    maplibre_css_path=maplibre_css_path,
                    viewer_html_path=viewer_html_path, map_config=map_config,
                    name=name)
        _add_vector_tiles(creator, MapItem, output_path=output_path, tiles=tiles,
                          mbtiles_path=mbtiles_path, tile_count=tile_count,
                          bbox=bbox, zim_builder=zim_builder, max_zoom=max_zoom)
        _add_raster_layers(creator, MapItem, satellite_dir=satellite_dir,
                           satellite_max_zoom=satellite_max_zoom,
                           satellite_format=satellite_format,
                           terrain_dir=terrain_dir,
                           terrain_max_zoom=terrain_max_zoom,
                           terrain_min_zoom=int((map_config or {}).get("terrainMinZoom") or 0),
                           bbox=bbox)
        _add_font_glyphs(creator, MapItem, fonts=fonts)
        wikidata_data = _add_wikidata(creator, MapItem, tiles=tiles,
                                      mbtiles_path=mbtiles_path, bbox=bbox,
                                      wikidata_data=wikidata_data,
                                      max_zoom=max_zoom)
        _bundled_set = _add_wiki_articles(
            creator, MapItem, wiki_cross_refs=wiki_cross_refs,
            bundle_wiki_articles=bundle_wiki_articles,
            wiki_articles_cache=wiki_articles_cache,
            wiki_articles_source=wiki_articles_source,
            wiki_images=wiki_images, wiki_image_max_kb=wiki_image_max_kb,
            wiki_images_per_article=wiki_images_per_article,
            fulltext_doc=corpus.fulltext if corpus is not None else None)
        # map-config.json and the License metadata come after the articles
        # (libzim does not care about order), so both credit Wikipedia only
        # when at least one article was stored. wiki_cross_refs alone are
        # OSM's wikipedia=/wikidata= tags (ODbL), not Wikipedia content.
        has_articles = bool(_bundled_set)
        _add_map_config(creator, MapItem, map_config=map_config,
                        has_wiki_articles=has_articles,
                        about=_about_fields(name=name, description=description,
                                            metadata=metadata))
        _add_metadata(creator, name=name, description=description,
                      overture_sources=overture_sources, xapian_mode=xapian_mode,
                      metadata=metadata, illustration=illustration,
                      has_satellite=bool(satellite_dir and os.path.isdir(satellite_dir)),
                      satellite_source=map_config.get("satelliteSource"),
                      has_terrain=bool(terrain_dir and os.path.isdir(terrain_dir)),
                      has_wiki=has_wikidata or has_articles,
                      tile_credit=(_tile_credit(tile_metadata)
                                   if map_config.get("tileSource") else None))
        _add_routing_graph(creator, MapItem,
                           routing_graph_path=routing_graph_path,
                           routing_graph_chunk_mb=routing_graph_chunk_mb,
                           spatial_chunk_scale=spatial_chunk_scale)
        _add_search(creator, MapItem, mbtiles_path=mbtiles_path,
                    search_features_path=search_features_path,
                    search_features=search_features,
                    wikidata_data=wikidata_data,
                    wiki_cross_refs=wiki_cross_refs, _bundled_set=_bundled_set,
                    split_hot_search_chunks_mb=split_hot_search_chunks_mb,
                    split_find_chips=split_find_chips,
                    no_llm_bundle=no_llm_bundle, map_config=map_config,
                    name=name, bbox=bbox,
                    routing_graph_path=routing_graph_path,
                    address_count=address_count,
                    overture_sources=overture_sources,
                    overture_themes=overture_themes,
                    overture_release=overture_release, xapian_mode=xapian_mode,
                    chunk_tmp=chunk_tmp, corpus=corpus,
                    page_types=kiwix_page_types(kiwix_poi_pages))
        if corpus is not None:
            _add_builder_xapian(creator, MapItem, corpus,
                                workdir=xapian_workdir or chunk_tmp,
                                xapianbuilder_bin=xapianbuilder_bin)
        print("    Finalizing ZIM (ZSTD compression + Xapian indexing)...", flush=True)
        finalize_start = time.time()

    finalize_elapsed = time.time() - finalize_start
    print(f"    Finalized in {finalize_elapsed:.0f}s", flush=True)
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"    ZIM file created: {size_mb:.1f} MB")


@wraps(_create_zim)
def create_zim(output_path, *args, **kwargs):
    """Publish a completed archive atomically, preserving any previous build.

    Both writers may leave files behind on failure (libzim even finalizes
    when the body raises). Keep all writer output in a private folder on
    the destination filesystem until finalization has succeeded. A reader
    sees the previous archive, or the complete replacement, at output_path.
    """
    target = Path(output_path).absolute()
    work = Path(tempfile.mkdtemp(prefix=f".{target.name}.building-", dir=target.parent))
    try:
        staged = work / target.name
        result = _create_zim(str(staged), *args, **kwargs)
        os.replace(staged, target)
        return result
    finally:
        # ManifestCreator removes a failed attempt's stage (manifest, staged
        # bodies, partial archive) unless STREETZIM_KEEP_PACK_STAGE=1; a stage
        # still here was kept on request, so keep the folder its error names.
        # External inputs (e.g. Xapian scratch files) may already be gone.
        if any(p.is_dir() for p in work.glob("*.pack-stage-*")):
            print(f"    Manifest build diagnostics kept for inspection at: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# create_zim's phases, in the order it runs them. Each was moved verbatim
# from the body of create_zim; only the indentation changed. They share the
# open creator, the MapItem class, and the few values passed between them
# explicitly (wikidata_data, the bundled wiki titles, SearchBuckets).
# ---------------------------------------------------------------------------


class SearchBuckets(NamedTuple):
    """What search pass 1 (_search_bucket) leaves on disk for the later passes."""
    chunk_tmp: str
    chunk_counts: dict
    xapian_path: str
    total_features: int
    xapian_count: int
    type_counts: dict
    wiki_fields_added: int
    wiki_geo: dict
    cat_chunk_counts: dict
    cat_dir: str
    cat_shards: dict
    CATEGORY_SHARD_MIN_BYTES: int


def _add_search(creator, MapItem, *, mbtiles_path, search_features_path,
                search_features, wikidata_data, wiki_cross_refs, _bundled_set,
                split_hot_search_chunks_mb, split_find_chips, no_llm_bundle,
                map_config, name, bbox, routing_graph_path, address_count,
                overture_sources, overture_themes, xapian_mode, chunk_tmp,
                overture_release=None, page_types=KIWIX_PAGE_TYPES, corpus=None):
    """Search data: JSON chunks, category index, chips, streetzim-meta.json,
    overture-sources.json and the Kiwix pages (with --xapian=builder, their
    xapianbuilder documents go to `corpus`). The chunk files go to
    `chunk_tmp`, which create_zim removes after the creator has closed."""

    # Build location index for search feature enrichment
    loc_lookup = None
    if mbtiles_path:
        print("    Building location index for search results...")
        loc_lookup = build_location_index(mbtiles_path)

    # Add search features — stream from disk if path provided, else use in-memory list
    if search_features_path and os.path.isfile(search_features_path) and os.path.getsize(search_features_path) > 0:
        b = _search_bucket(search_features_path=search_features_path,
                           wikidata_data=wikidata_data,
                           wiki_cross_refs=wiki_cross_refs,
                           loc_lookup=loc_lookup, _bundled_set=_bundled_set,
                           chunk_tmp=chunk_tmp, page_types=page_types)
        _search_emit_chunks(creator, MapItem,
                            split_hot_search_chunks_mb=split_hot_search_chunks_mb,
                            chunk_tmp=b.chunk_tmp, chunk_counts=b.chunk_counts,
                            total_features=b.total_features)
        _search_category_index(creator, MapItem,
                               split_find_chips=split_find_chips,
                               no_llm_bundle=no_llm_bundle, wiki_geo=b.wiki_geo,
                               cat_chunk_counts=b.cat_chunk_counts,
                               cat_dir=b.cat_dir, cat_shards=b.cat_shards,
                               CATEGORY_SHARD_MIN_BYTES=b.CATEGORY_SHARD_MIN_BYTES)
        _add_meta_json(creator, MapItem, map_config=map_config, name=name,
                       bbox=bbox, wikidata_data=wikidata_data,
                       routing_graph_path=routing_graph_path,
                       address_count=address_count,
                       total_features=b.total_features,
                       type_counts=b.type_counts,
                       wiki_fields_added=b.wiki_fields_added)
        _add_overture_credits(creator, MapItem, overture_sources=overture_sources,
                              overture_themes=overture_themes,
                              overture_release=overture_release)
        _search_xapian_pages(creator, MapItem, xapian_mode=xapian_mode,
                             xapian_path=b.xapian_path,
                             total_features=b.total_features,
                             xapian_count=b.xapian_count, corpus=corpus)

    elif search_features:
        _add_search_in_memory(creator, MapItem, search_features=search_features,
                              loc_lookup=loc_lookup, page_types=page_types,
                              wiki_cross_refs=wiki_cross_refs, corpus=corpus)


def _tile_credit(tile_metadata):
    from streetzim import mbtiles
    return mbtiles.license_text(tile_metadata or {})


def _add_metadata(creator, *, name, description, overture_sources, xapian_mode,
                  metadata=None, illustration=None, has_satellite=True,
                  has_terrain=True, has_wiki=True, satellite_source=None,
                  tile_credit=None):
    """ZIM metadata (Name, Title, Tags, License, ...) and the 48x48 illustration.

    ``metadata`` holds openZIM-flag overrides validated by
    streetzim.zim_metadata.build_overrides (Name, Title, Description,
    LongDescription, Creator, Publisher, Tags, Scraper); anything not in it
    keeps the builder's default. ``illustration`` is a 48x48 PNG.
    ``has_*`` say which optional layers the ZIM contains, so License names
    only the licences that apply (a ZIM without the satellite layer must not
    claim CC BY-NC-SA). ``satellite_source`` names the mosaic
    (streetzim.satellite_sources; default the builder's, 2021): with a
    non-commercial one, License opens with a "Non-commercial use only" notice.
    """
    from streetzim import satellite_sources
    md = metadata or {}
    # Add metadata — Name and Illustration are required by Kiwix to register the ZIM
    import re as _re_name
    # `name` usually already reads "OSM - <Region>", which yields the
    # ugly "osm_osm_-_region". Every ZIM shipped since 2026-04 carries
    # exactly that form, and Kiwix keys book identity / update
    # detection on Name (repackage_zim.py copies the source Name for
    # rerolls) — so keep it. Set STREETZIM_CLEAN_ZIM_NAME=1 to emit
    # "osm_region" and start a new lineage deliberately.
    zim_name = name.strip()
    if os.environ.get("STREETZIM_CLEAN_ZIM_NAME") == "1":
        zim_name = _re_name.sub(r"^osm\s*-\s*", "", zim_name, flags=_re_name.I)
    zim_name = zim_name.lower().replace(" ", "_").replace(",", "").replace(".", "")
    creator.add_metadata("Title", md.get("Title", name))
    creator.add_metadata("Description", md.get("Description", description))
    if md.get("LongDescription"):
        creator.add_metadata("LongDescription", md["LongDescription"])
    creator.add_metadata("Language", "eng")
    creator.add_metadata("Publisher", md.get("Publisher", "create_osm_zim"))
    creator.add_metadata("Creator", md.get("Creator", "OpenStreetMap contributors"))
    import time as _time
    creator.add_metadata("Date", _time.strftime("%Y-%m-%d"))
    # Only advertise a full-text index when one is actually built;
    # `_ftindex:yes` under --xapian=none showed Kiwix a search box
    # that returned nothing.
    _tags = "maps;osm;offline;_pictures:yes"
    if xapian_mode in ("libzim", "builder"):
        _tags += ";_ftindex:yes"
    if md.get("Tags"):
        # The user's tags come after ours: _ftindex must stay accurate.
        from streetzim.zim_metadata import merge_tags
        _tags = merge_tags(_tags, md["Tags"])
    creator.add_metadata("Tags", _tags)
    creator.add_metadata("Name", md.get("Name", f"osm_{zim_name}"))
    creator.add_metadata("Flavour", md.get("Flavour", "maxi"))
    creator.add_metadata("Scraper", md.get("Scraper", "streetzim/1.0"))
    license_parts = [
        "Map data: ODbL (OpenStreetMap)",
        "Tile schema: CC-BY 4.0 (OpenMapTiles)",
    ]
    if tile_credit:
        # Ready-made tiles (streetzim --mbtiles/--mbtiles-url): whose they are.
        license_parts.append(tile_credit)
    if has_satellite:
        sat = satellite_sources.get(satellite_source or satellite_sources.BUILDER_DEFAULT)
        if sat.noncommercial:
            license_parts.insert(0, f"Non-commercial use only: the satellite imagery "
                                    f"is {sat.license}")
        license_parts.append(sat.license_metadata)
    if has_terrain:
        license_parts.append(
            # GLO-90 too: low zooms, and cells GLO-30 leaves out. The same
            # attribution covers both.
            "Elevation: Copernicus DEM GLO-30/GLO-90 © DLR/Airbus, provided "
            "under COPERNICUS by EU and ESA")
    if has_wiki:
        # Wikipedia text has been CC BY-SA 4.0 since June 2023.
        license_parts.append("Place info: CC0 (Wikidata) / CC BY-SA 4.0 (Wikipedia)")
    if overture_sources:
        # Overture's addresses theme ships mixed per-source licenses
        # (CC0/CC-BY-4.0/OGL-UK/etc.). We point to the dataset credits
        # embedded in the ZIM as overture-sources.json rather than
        # enumerating every upstream feed inline.
        license_parts.append(
            "Address enrichment: Overture Maps Foundation "
            "(overturemaps.org) — dataset credits in overture-sources.json"
        )
    license_parts.append("Tool code: MIT")
    creator.add_metadata("License", "; ".join(license_parts))

    # Add 48x48 illustration (required by Kiwix to show in library)
    if illustration:
        creator.add_illustration(48, illustration)
        return
    # Generate a simple map icon as PNG
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (48, 48), (37, 99, 235, 255))
        draw = ImageDraw.Draw(img)
        # Simple globe/map icon
        draw.ellipse([8, 8, 40, 40], outline=(255, 255, 255, 200), width=2)
        draw.line([24, 8, 24, 40], fill=(255, 255, 255, 120), width=1)
        draw.line([8, 24, 40, 24], fill=(255, 255, 255, 120), width=1)
        draw.arc([4, 8, 44, 40], 0, 360, fill=(255, 255, 255, 80), width=1)
        import io
        buf = io.BytesIO()
        img.save(buf, "PNG")
        creator.add_illustration(48, buf.getvalue())
    except ImportError:
        pass  # PIL not available, skip illustration


# MapLibre's RTL text plugin (Arabic/Hebrew shaping and bidi), vendored from
# @mapbox/mapbox-gl-rtl-text (the version MapLibre's setRTLTextPlugin
# documentation pins) and pinned in resources/viewer-assets.lock.json like
# MapLibre itself (streetzim/viewer_assets.py). Written into every ZIM and
# named in map-config.json as `rtlTextPlugin`; the viewer (137-rtl-text.js)
# loads it only once a tile carries RTL text. A missing or altered copy
# stops the build (viewer_assets.IntegrityError), as for MapLibre.
RTL_TEXT_PLUGIN_ENTRY = viewer_assets.RTL_TEXT_PLUGIN_ENTRY


def _rtl_text_plugin_bytes():
    """The ZIM entry (viewer_assets.rtl_text_plugin_entry): the verified
    plugin behind a comment carrying its licence."""
    return viewer_assets.rtl_text_plugin_entry()


def _add_viewer(creator, MapItem, *, maplibre_js_path, maplibre_css_path, viewer_html_path, map_config, name):
    """The viewer: index.html, routing-worker.js and places.html in their
    fixed uncompressed slots, and MapLibre. (map-config.json is written by
    _add_map_config, after the Wikipedia articles.)"""
    # Add the viewer HTML (main page).
    #
    # The three viewer files go into fixed-size UNCOMPRESSED slots
    # (cloud/viewer_slots.py) so a later viewer change can be patched in
    # place by cloud/patch_viewer_inplace.py in ~10 s instead of a full
    # re-pack -- measured 2026-09-21 at 3 s on a 3.4 GB region and 10 s on
    # 7.4 GB, against 8-22 min for the equivalent swap_viewer_rust pass.
    # Uncompressed is the mechanism, not an oversight: bytes inside a
    # compressed cluster do not map to file offsets, so there would be
    # nothing to overwrite. Cost is ~1.4 MB per ZIM.
    #
    # Until now only swap_viewer_rust.py wrote slots, so every freshly
    # built region was born without them and owed one full re-pack at the
    # next viewer change.
    print("    Adding viewer HTML (slotted)...")
    creator.add_item(MapItem(
        "index.html", name, "text/html",
        _pad_to_slot("index.html",
                     open(str(viewer_html_path), "rb").read()),
        is_front=True, compress=False,
    ))
    routing_worker_path = VIEWER_DIR / "routing-worker.js"
    if routing_worker_path.exists():
        print("    Adding routing-worker.js (slotted)...")
        creator.add_item(MapItem(
            "routing-worker.js", "Routing Worker", "application/javascript",
            _pad_to_slot("routing-worker.js",
                         routing_worker_path.read_bytes()),
            is_front=False, compress=False,
        ))

    # Find-places mini-app (`places.html`). LLM-free: searches the
    # in-ZIM `search-data/` + `category-index/` files client-side
    # and links each result through the viewer's `dest=` hash so
    # the user lands in the routing panel with the destination
    # pre-filled. Same single file works in Kiwix and the Firebase
    # PWA shell — see HOW_TO_BUILD-style notes in the file itself.
    places_path = VIEWER_DIR / "places.html"
    if places_path.exists():
        print("    Adding places.html (find-places mini-app, slotted)...")
        creator.add_item(MapItem(
            "places.html", "Find places", "text/html",
            _pad_to_slot("places.html", places_path.read_bytes()),
            is_front=False, compress=False,
        ))

    # Add MapLibre GL JS
    print("    Adding MapLibre GL JS...")
    creator.add_item(MapItem(
        "maplibre-gl.js", "MapLibre GL JS", "application/javascript",
        maplibre_js_path,
    ))
    creator.add_item(MapItem(
        "maplibre-gl.css", "MapLibre GL CSS", "text/css",
        maplibre_css_path,
    ))
    creator.add_item(MapItem(
        RTL_TEXT_PLUGIN_ENTRY, "MapLibre RTL text plugin", "application/javascript",
        _rtl_text_plugin_bytes(),
    ))



def _about_fields(*, name, description, metadata=None):
    """What the viewer's About panel shows (140-view-home-about.js): the
    same Title / Description _add_metadata writes, since a page in the ZIM
    cannot read M/ metadata portably, and the release that built it. The
    build month is map-config's existing buildDate."""
    from streetzim.__about__ import __version__
    md = metadata or {}
    return {
        "title": md.get("Title", name),
        "description": md.get("Description", description),
        "generator": f"streetzim {__version__}",
    }


def _add_map_config(creator, MapItem, *, map_config, has_wiki_articles, about=None):
    """map-config.json, with hasWikiArticles only when articles were stored
    (the viewer's credits list Wikipedia on it) and the About fields."""
    map_config = dict(map_config)
    for k, v in (about or {}).items():
        if v:
            map_config.setdefault(k, v)
    # _add_viewer wrote the file (or stopped the build).
    map_config["rtlTextPlugin"] = RTL_TEXT_PLUGIN_ENTRY
    if has_wiki_articles:
        map_config["hasWikiArticles"] = True
    else:
        map_config.pop("hasWikiArticles", None)
    config_json = json.dumps(map_config, indent=2)
    creator.add_item(MapItem(
        "map-config.json", "Map Config", "application/json",
        config_json.encode("utf-8"),
    ))


def _add_vector_tiles(creator, MapItem, *, output_path, tiles, mbtiles_path, tile_count, bbox, zim_builder, max_zoom):
    """Vector tiles, streamed from the MBTiles (or the in-memory dict), with
    libzim backpressure and a stall watchdog."""
    # Watchdog thread: monitors progress and dumps all thread stacks on stall
    import threading, sys, traceback
    _watchdog_tile_count = [0]  # mutable container for thread access
    _watchdog_stop = threading.Event()

    def _watchdog():
        last_count = 0
        stall_seconds = 0
        while not _watchdog_stop.is_set():
            _watchdog_stop.wait(10)  # check every 10 seconds
            current = _watchdog_tile_count[0]
            if current == last_count and current > 0:
                stall_seconds += 10
                if stall_seconds >= 30:
                    # Stall detected — dump everything
                    print(f"\n\n=== WATCHDOG: No progress for {stall_seconds}s (stuck at tile {current}) ===", flush=True)
                    try:
                        tmp_path = str(output_path) + ".tmp"
                        if os.path.exists(tmp_path):
                            print(f"    File size: {os.path.getsize(tmp_path) / 1e9:.2f} GB", flush=True)
                        else:
                            print(f"    File size: {os.path.getsize(str(output_path)) / 1e9:.2f} GB", flush=True)
                    except OSError:
                        print("    File not yet created", flush=True)
                    mem_gb = peak_rss_bytes() / (1024**3)
                    print(f"    RSS: {mem_gb:.1f} GB", flush=True)
                    print(f"    Threads: {threading.active_count()}", flush=True)
                    # Dump all thread stacks
                    frames = sys._current_frames()
                    for tid, frame in frames.items():
                        tname = "unknown"
                        for t in threading.enumerate():
                            if t.ident == tid:
                                tname = t.name
                                break
                        print(f"\n--- Thread {tid} ({tname}) ---", flush=True)
                        traceback.print_stack(frame)
                        sys.stdout.flush()
                    print("=== END WATCHDOG DUMP ===\n", flush=True)
                    stall_seconds = 0  # reset so we dump again if still stuck
            else:
                stall_seconds = 0
            last_count = current

    watchdog_thread = threading.Thread(target=_watchdog, name="streetzim-tile-watchdog", daemon=True)
    watchdog_thread.start()

    tile_source = None
    try:
        # Add vector tiles — decompress in parallel for speed
        import time
        import itertools
        from concurrent.futures import ThreadPoolExecutor

        bad_gzip_tiles = []

        def decompress_tile(item):
            z, x, y, data = item
            if data[:2] == b"\x1f\x8b":  # gzip magic bytes
                try:
                    data = gzip.decompress(data)
                except Exception as exc:
                    # A corrupt tile used to be stored still-gzipped as
                    # application/x-protobuf; MapLibre silently dropped it.
                    bad_gzip_tiles.append((z, x, y, str(exc)))
                    data = b""
            return z, x, y, data

        # Stream tiles from mbtiles or use in-memory dict
        if mbtiles_path:
            # NOT tile_count: that is COUNT(*) over the whole shared world
            # MBTiles (345 M tiles), which made the progress ETA useless.
            total_tiles = estimate_tile_total(
                mbtiles_path, bbox=bbox, max_zoom=max_zoom) or (tile_count or 0)
            tile_source = iter_tiles_from_mbtiles(mbtiles_path, bbox=bbox, max_zoom=max_zoom)
        else:
            total_tiles = len(tiles)
            tile_source = iter([(z, x, y, data) for (z, x, y), data in sorted(tiles.items())])

        print(f"    Adding {total_tiles} vector tiles...", flush=True)
        tiles_added = 0
        # Tilemaker emits a 0-byte PBF for every tile coord that has no
        # features in its bbox (deep ocean / desert / pure-empty). Adding
        # those wastes a libzim entry per tile (~50 B each) and floods
        # zimcheck's "Empty article" report (3k–191k per region as of
        # 2026-04-25). MapLibre treats 404 and "0-byte tile" the same —
        # nothing to render — so we drop them at write time. Real-content
        # near-empty tiles (e.g. 55-byte ocean-only with a water/ocean
        # layer) ARE kept; they paint the right ocean color when MapLibre
        # styles them.
        tiles_skipped_empty = 0
        tile_start = time.time()
        batch_size = 1000
        # Adaptive backpressure, ONLY for the libzim builder. With libzim,
        # add_item() feeds its C++ queue directly, and per-item / per-batch
        # sleeps let the compression workers drain — guarding the spin-lock
        # death spiral in libzim's queue.h. With zim_builder="manifest" the creator
        # is ManifestCreator, which appends a line to a file: there is no queue
        # to drain, so a slow batch means slow disk, and sleeping only made
        # central-asia's tile phase slower ("rust" arrives here as "manifest"
        # too). build-region-fast.sh uses a manifest writer, but
        # --zim-builder defaults to "python" (libzim), which every build
        # without that flag takes (streetzim, Zimfarm, older ops wrappers),
        # so the guard must stay for them.
        _libzim_backpressure = (zim_builder != "manifest")
        backpressure_sleep = 0.0
        # Identical tiles (open sea, tiles inside one landcover polygon) are
        # stored once; tile_source yields in (z, x, y) order, so the first-seen
        # target, and the ZIM, are the same on every build.
        aliaser = TileAliaser(creator)
        with ThreadPoolExecutor(max_workers=compression_cpus()) as pool:
            while True:
                batch = list(itertools.islice(tile_source, batch_size))
                if not batch:
                    break
                results = list(pool.map(decompress_tile, batch))
                if bad_gzip_tiles:
                    # Abort now (the SystemExit below reports it) instead of
                    # spending hours adding the remaining tiles first.
                    break

                add_start = time.time()
                for z, x, y, tile_data in results:
                    # See note above: 0-byte tiles are MVT placeholders for
                    # bbox cells with no features. Drop them — MapLibre
                    # rendering is unaffected, ZIM entries dedup, zimcheck
                    # "Empty article" count goes to 0.
                    if not tile_data:
                        tiles_skipped_empty += 1
                        continue
                    item_start = time.time() if _libzim_backpressure else 0.0
                    tile_path = f"tiles/{z}/{x}/{y}.pbf"
                    alias_of = aliaser.target_for(tile_path, tile_data)
                    if alias_of is not None:
                        # Same bytes as an earlier tile: a second dirent on its
                        # blob (see streetzim/tile_alias.py).
                        aliaser.add_alias(tile_path, f"Tile {z}/{x}/{y}",
                                          alias_of, len(tile_data))
                    else:
                        creator.add_item(MapItem(
                            tile_path, f"Tile {z}/{x}/{y}",
                            "application/x-protobuf",
                            tile_data,
                        ))
                    tiles_added += 1
                    _watchdog_tile_count[0] = tiles_added
                    if _libzim_backpressure:
                        # A single add_item() over 100 ms means libzim's queue
                        # is full — sleep so the workers can drain.
                        item_elapsed = time.time() - item_start
                        if item_elapsed > 0.1:
                            time.sleep(min(item_elapsed * 2, 2.0))
                add_time = time.time() - add_start

                if _libzim_backpressure:
                    batch_rate = batch_size / add_time if add_time > 0 else float("inf")
                    # No total_tiles clause: it used to be the world
                    # COUNT(*), i.e. always over any threshold, so gating on a
                    # now-region-sized total would quietly disarm this for
                    # small regions on the libzim writer.
                    if batch_rate < 5000:
                        backpressure_sleep = min(backpressure_sleep + 0.05, 1.0)
                        time.sleep(backpressure_sleep)
                    elif batch_rate > 15000:
                        backpressure_sleep = max(backpressure_sleep - 0.01, 0.0)


                if tiles_added % 2000 == 0:
                    elapsed = time.time() - tile_start
                    rate = tiles_added / elapsed if elapsed > 0 else 0
                    remaining = (total_tiles - tiles_added) / rate if rate > 0 else 0
                    mem_gb = peak_rss_bytes() / (1024**3)
                    print(f"\r    Added {tiles_added}/{total_tiles} tiles "
                          f"({rate:.0f}/s, ~{remaining/60:.0f}m left, {mem_gb:.1f}GB RSS)...",
                          end="", flush=True)

        elapsed = time.time() - tile_start
        rate_str = f"{tiles_added/elapsed:.0f}/s" if elapsed > 0 else "instant"
        if bad_gzip_tiles:
            _bad = ", ".join(f"{z}/{x}/{y}" for z, x, y, _ in bad_gzip_tiles[:5])
            raise SystemExit(
                f"{len(bad_gzip_tiles)} vector tile(s) failed gzip decompression "
                f"({_bad}{'…' if len(bad_gzip_tiles) > 5 else ''}) — corrupt MBTiles; "
                f"re-run tilemaker before packaging")
        skip_str = (f" (skipped {tiles_skipped_empty} empty)"
                    if tiles_skipped_empty else "")
        print(f"\r    Added {tiles_added} tiles in {elapsed:.0f}s ({rate_str}){skip_str}; "
              f"{aliaser.summary()}                ", flush=True)
        PHASE_TIMER.record_subphase(
            "zim-pack: vector tiles", elapsed,
            note=f"{tiles_added:,} tiles ({rate_str})"
                 + (f", skipped {tiles_skipped_empty} empty" if tiles_skipped_empty else "")
                 + (f", {aliaser.aliases:,} aliased" if aliaser.aliases else ""))
    finally:
        _watchdog_stop.set()
        watchdog_thread.join()
        close_source = getattr(tile_source, "close", None)
        if close_source is not None:
            close_source()


def _add_raster_layers(creator, MapItem, *, satellite_dir, satellite_max_zoom, satellite_format, terrain_dir, terrain_max_zoom, bbox, terrain_min_zoom=0):
    """Satellite and terrain raster tiles from their on-disk caches."""
    # Build bbox tile filter if bbox is provided (shared cache may have tiles from other areas)
    def _tile_in_bbox(z, x, y, bbox_coords):
        """Check if tile (z,x,y) overlaps with bbox. Uses mercantile for accuracy."""
        import mercantile
        tile_bounds = mercantile.bounds(mercantile.Tile(x, y, z))
        # Each side of the antimeridian separately (streetzim/area.py).
        return any(not (tile_bounds.east < minlon or tile_bounds.west > maxlon or
                        tile_bounds.north < minlat or tile_bounds.south > maxlat)
                   for minlon, minlat, maxlon, maxlat in _area.split(bbox_coords))

    def _add_raster_tiles(source_dir, zim_prefix, max_zoom, label, ext="webp",
                          mimetype="image/webp", min_zoom=0):
        """Walk a tile cache dir and add tiles to ZIM, filtering by bbox."""
        _t0 = time.time()
        count = 0
        skipped = 0
        empty = 0
        unreadable = 0
        suffix = f".{ext}"
        strip_len = len(suffix)
        aliaser = TileAliaser(creator)
        for z in range(min_zoom, max_zoom + 1):
            z_dir = os.path.join(source_dir, str(z))
            if not os.path.isdir(z_dir):
                continue
            for x_name in sorted(os.listdir(z_dir)):
                x_dir = os.path.join(z_dir, x_name)
                if not os.path.isdir(x_dir):
                    continue
                try:
                    x = int(x_name)
                except ValueError:
                    continue
                # Sorted: alias targets (first-seen) must not depend on
                # the filesystem's directory order.
                for fname in sorted(os.listdir(x_dir)):
                    if not fname.endswith(suffix):
                        continue
                    try:
                        y = int(fname[:-strip_len])
                    except ValueError:
                        continue
                    if bbox and not _tile_in_bbox(z, x, y, bbox):
                        skipped += 1
                        continue
                    fpath = os.path.join(x_dir, fname)
                    # A zero-byte cache file is a download that wrote
                    # nothing. Adding it produces a ZIM entry with no
                    # content, which zimcheck reports as "Empty article"
                    # and the validator gate fails: australia-nz's
                    # 2026-09-14 build did exactly that on two satellite
                    # tiles cached empty on 2026-04-13. The vector-tile
                    # loop already drops empty tiles; do the same here.
                    try:
                        fsize = os.path.getsize(fpath)
                        if fsize == 0:
                            empty += 1
                            continue
                    except OSError:
                        # Just listed from the directory, so a stat failure
                        # is an I/O or permission problem — count it.
                        unreadable += 1
                        continue
                    zim_path = f"{zim_prefix}/{z}/{x_name}/{fname}"
                    title = f"{label} {z}/{x_name}/{fname}"
                    count += 1
                    # Raster tiles sit in uncompressed clusters, so a repeat
                    # (sea, flat terrain) costs its full size unless aliased.
                    alias_of = None
                    if aliaser.enabled and fsize <= max_alias_bytes(zim_path):
                        with open(fpath, "rb") as fh:
                            alias_of = aliaser.target_for(zim_path, fh.read())
                    if alias_of is not None:
                        aliaser.add_alias(zim_path, title, alias_of, fsize)
                    else:
                        creator.add_item(MapItem(
                            zim_path, title,
                            mimetype,
                            fpath,
                            compress=False,
                        ))
                    if count % 2000 == 0:
                        print(f"\r    Added {count} {label.lower()} tiles...", end="", flush=True)
        elapsed = time.time() - _t0
        rate = (count / elapsed) if elapsed > 0 else 0
        print(f"\r    Added {count} {label.lower()} tiles in {elapsed:.0f}s ({rate:.0f}/s); "
              f"{aliaser.summary()}" +
              (f" (skipped {skipped} outside bbox)" if skipped else "") +
              (f" (dropped {empty} zero-byte cache files)" if empty else "") +
              (f" (skipped {unreadable} unreadable files)" if unreadable else ""),
              flush=True)
        PHASE_TIMER.record_subphase(
            f"zim-pack: {label.lower()} tiles", elapsed,
            note=f"{count:,} tiles ({rate:.0f}/s)"
                 + (f", skipped {skipped} outside bbox" if skipped else "")
                 # Dropped files are invisible to the validator's coverage
                 # check (it can only count entries that exist), so record
                 # them where the build summary keeps them.
                 + (f", dropped {empty} zero-byte cache files" if empty else "")
                 + (f", {unreadable} unreadable" if unreadable else "")
                 + (f", {aliaser.aliases:,} aliased" if aliaser.aliases else ""))
        return count

    # Add satellite tiles if provided
    if satellite_dir and os.path.isdir(satellite_dir):
        sat_ext = satellite_format  # "webp" or "avif"
        sat_mime = "image/avif" if sat_ext == "avif" else "image/webp"
        max_sz = satellite_max_zoom if satellite_max_zoom is not None else 99
        _add_raster_tiles(satellite_dir, "satellite", max_sz, "Satellite",
                          ext=sat_ext, mimetype=sat_mime)

    # Add terrain tiles if provided
    if terrain_dir and os.path.isdir(terrain_dir):
        max_tz = terrain_max_zoom if terrain_max_zoom is not None else 99
        # From the zoom the viewer starts at (map-config terrainMinZoom): a
        # shared cache can hold lower tiles made for other areas.
        _add_raster_tiles(terrain_dir, "terrain", max_tz, "Terrain",
                          min_zoom=terrain_min_zoom)


def _add_font_glyphs(creator, MapItem, *, fonts):
    """SDF font glyph ranges."""
    # Add font glyphs
    with PHASE_TIMER.subphase("zim-pack: font glyphs") as _sp:
        print(f"    Adding {len(fonts)} font glyph ranges...")
        for (font_name, range_key), data in fonts.items():
            # font_name has no spaces (e.g. "OpenSansRegular") to avoid
            # URL-encoding issues across Kiwix implementations
            if range_key.endswith(".txt"):  # a font licence (fonts/NotoSans/OFL.txt)
                creator.add_item(MapItem(
                    f"fonts/{font_name}/{range_key}", f"{font_name} font licence",
                    "text/plain", data,
                ))
                continue
            path = f"fonts/{font_name}/{range_key}.pbf"
            creator.add_item(MapItem(
                path, f"Font {font_name} {range_key}",
                "application/x-protobuf",
                data,
            ))
        _sp.set_note(f"{len(fonts)} entries")


def _add_wikidata(creator, MapItem, *, tiles, mbtiles_path, bbox, wikidata_data, max_zoom):
    """Wikidata place info, filtered to the Q-IDs present in this build's tiles.
    Returns the (possibly filtered) wikidata_data, which the search phase uses."""
    # Add Wikidata info — filter to Q-IDs present in the bbox tiles
    # Skip filtering for world bbox (all Q-IDs are relevant)
    if wikidata_data:
        _wd_t0 = time.time()
        is_world_bbox = bbox and abs(bbox[0] - (-180)) < 1 and abs(bbox[2] - 180) < 1 and abs(bbox[1] - (-85)) < 2 and abs(bbox[3] - 85) < 2
        # Scan whichever tile source this build has. mbtiles_path is only
        # passed in STREAMING mode (see create_zim's call site), so gating
        # on it alone silently skipped the filter for every region with
        # its own small extract -- hawaii and iceland rebuilt on
        # 2026-09-25 shipped the full 640 MB global set while benelux,
        # which streams from the world file, shipped 9.5 MB. The in-memory
        # `tiles` dict is keyed (z, x, y) and already covers the bbox.
        # Scan the DEEPEST zoom this build actually has, not a hardcoded
        # 14. A light variant caps tiles at z13 (--max-zoom 13), so a
        # z14-only scan found no tiles, hence no Q-IDs, and the region
        # shipped with ZERO Wikidata while map-config still advertised
        # hasWikidata -- every POI panel would come up empty. Caught on
        # switzerland-light 2026-09-26: "Filtered Wikidata: 0 entries in
        # bbox (from 3342069 total)".
        _scan_z = min(14, max_zoom) if max_zoom else 14
        _tile_src = None
        if bbox and not is_world_bbox:
            if mbtiles_path:
                _tile_src = iter_tiles_from_mbtiles(mbtiles_path, zoom_level=_scan_z, bbox=bbox)
            elif tiles:
                _tile_src = ((z, x, y, d) for (z, x, y), d in tiles.items() if z == _scan_z)
        if _tile_src is not None:
            print("    Scanning tiles for Wikidata Q-IDs in bbox...")
            import mapbox_vector_tile as _mvt
            bbox_qids = set()
            for _z, _x, _y, data in _tile_src:
                tile_data = data
                if data[:2] == b"\x1f\x8b":
                    try:
                        tile_data = gzip.decompress(data)
                    except Exception:
                        continue
                try:
                    decoded = _mvt.decode(tile_data, y_coord_down=True)
                except Exception:
                    continue
                for layer in decoded.values():
                    for feat in layer.get("features", []):
                        qid = (feat.get("properties") or {}).get("wikidata", "")
                        if qid and qid.startswith("Q"):
                            bbox_qids.add(qid)
            filtered = {qid: data for qid, data in wikidata_data.items() if qid in bbox_qids}
            print(f"    Filtered Wikidata: {len(filtered)} entries in bbox "
                  f"(from {len(wikidata_data)} total, scanned z{_scan_z})")
            if not filtered and wikidata_data:
                # Zero is never right for a populated region: it means the
                # scan looked at a zoom this build does not contain, or the
                # tiles carry no wikidata tags at all. Keeping the full set
                # is wasteful; shipping none breaks every POI panel while
                # map-config still says hasWikidata. Fail loudly instead.
                raise SystemExit(
                    f"FATAL: Wikidata bbox filter matched 0 of {len(wikidata_data)} "
                    f"entries scanning z{_scan_z}. Shipping this would leave every "
                    f"place panel empty. Check that the build has z{_scan_z} tiles.")
            wikidata_data = filtered

        if _tile_src is None and len(wikidata_data) > 100_000:
            print(f"    WARNING: Wikidata NOT filtered to the region "
                  f"({len(wikidata_data)} entries, ~600 MB). bbox="
                  f"{bool(bbox)} mbtiles={bool(mbtiles_path)} tiles="
                  f"{bool(tiles)} world_bbox={bool(is_world_bbox)}", flush=True)
        print(f"    Adding Wikidata info for {len(wikidata_data)} features...")
        from collections import defaultdict as _dd
        wd_chunks = _dd(dict)
        for qid, data in wikidata_data.items():
            # Bucket by first 2 chars of Q-ID number for chunked loading
            num = qid[1:]  # strip 'Q'
            prefix = num[:2] if len(num) >= 2 else num.ljust(2, "0")
            wd_chunks[prefix][qid] = data

        # Write manifest
        wd_manifest = {
            "total": len(wikidata_data),
            "chunks": {k: len(v) for k, v in sorted(wd_chunks.items())},
        }
        creator.add_item(MapItem(
            "wikidata/manifest.json", "Wikidata Manifest", "application/json",
            json.dumps(wd_manifest, separators=(",", ":")).encode("utf-8"),
        ))

        # Write each chunk
        for prefix, chunk_entries in sorted(wd_chunks.items()):
            chunk_json = json.dumps(chunk_entries, separators=(",", ":"),
                                    ensure_ascii=False)
            creator.add_item(MapItem(
                f"wikidata/{prefix}.json",
                f"Wikidata chunk {prefix}",
                "application/json",
                chunk_json.encode("utf-8"),
            ))

        total_bytes = sum(
            len(json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode())
            for v in wd_chunks.values()
        )
        print(f"    Added {len(wd_chunks)} Wikidata chunks ({total_bytes / 1024:.0f} KB)")
        PHASE_TIMER.record_subphase(
            "zim-pack: wikidata", time.time() - _wd_t0,
            note=f"{len(wikidata_data)} entries → {len(wd_chunks)} chunks, {total_bytes / 1024:.0f} KB")
    return wikidata_data


def _add_wiki_articles(creator, MapItem, *, wiki_cross_refs, bundle_wiki_articles, wiki_articles_cache, wiki_articles_source, wiki_images, wiki_image_max_kb, wiki_images_per_article, fulltext_doc=None):
    """Bundled Wikipedia article pages. Returns the set of titles actually
    stored (None when not bundling), which gates the wiki geo-index.
    ``fulltext_doc(path, title, html)`` (--xapian=builder: XapianCorpus.
    fulltext) gets each article page, which libzim's indexer would take."""
    def _add(path, title, mt, content):
        creator.add_item(MapItem(path, title, mt, content))
        if fulltext_doc is not None and mt.startswith("text/html"):
            fulltext_doc(path, title, content)
    # Bundle full Wikipedia article pages (option B) so offline clients
    # can open + narrate them — kiwix can't deep-link across ZIMs. Titles
    # come from the cross-ref index (`w` OSM tags + any backfilled from
    # wikidata via --resolve-wikidata-titles). Stored at
    # wiki-article/<Title>; mcpzim's articleByTitle reads them there and
    # its narration cleaner de-noises for TTS. Cached so rebuilds don't
    # re-crawl. Source: a local Wikipedia ZIM (offline) or the API.
    _bundled_set = None  # title_us actually stored — gates the geo-index
    _wa_stats = None
    if bundle_wiki_articles and wiki_cross_refs:
        # A non-English tag whose item has no English article (Wikidata
        # says so) is bundled only when its English namesake is a redirect
        # to an article bundled here (an editor's alias: "Aalten (dorp)" ->
        # "Aalten"); a namesake article is a different page.
        _wa_titles = {e["wikipedia"] for e in wiki_cross_refs.values()
                      if e.get("wikipedia") and not e.get("wikipedia_no_en")}
        _wa_redirect_only = {e["wikipedia"] for e in wiki_cross_refs.values()
                             if e.get("wikipedia") and e.get("wikipedia_no_en")}
        # The same title in another non-English tag of an object without a
        # Q-ID ("li:Limmel", "NL:Limmel", "nl:Sint_Pieter" for "nl:Sint
        # Pieter") names the same article: it follows the flag, else its
        # namesake would be bundled after all and shown for the flagged
        # object too (the geo-index and the viewer go by the title, as
        # _underscore makes it). An English tag, OSM's or resolved, names
        # that article itself and stays.
        from cloud.wiki_articles import _underscore
        from cloud.wikidata_titles import is_english_title
        _flagged = {_underscore(t) for t in _wa_redirect_only}
        _wa_titles = {t for t in _wa_titles
                      if is_english_title(t) or _underscore(t) not in _flagged}
        if _wa_titles:
            from cloud.wiki_articles import bundle_wiki_articles as _bundle_wa
            _wa_t0 = time.time()
            _wa_stats = _bundle_wa(
                _wa_titles,
                _add,
                cache_dir=wiki_articles_cache,
                offline_zim=wiki_articles_source,
                images=wiki_images,
                image_max_kb=wiki_image_max_kb,
                max_images_per_article=wiki_images_per_article,
                redirect_only=_wa_redirect_only,
                add_redirect=lambda path, title, target: _add_redirect(
                    creator, path, title, target),
            )
            _bundled_set = _wa_stats.get("stored_titles") or set()
            PHASE_TIMER.record_subphase(
                "zim-pack: wiki-articles", time.time() - _wa_t0,
                note=f"{_wa_stats['bundled']} articles, "
                     f"{_wa_stats['bytes'] // 1024} KB, "
                     f"{_wa_stats['failed']} missing "
                     f"({_wa_stats.get('unfetched', 0)} unfetched), "
                     f"{_wa_stats.get('images', 0)} images "
                     f"{_wa_stats.get('image_bytes', 0) // 1048576} MB")
    if _wa_stats:
        from streetzim.source_report import note
        note("Wikipedia articles",
             f"{_wa_stats.get('bundled', 0)}/{_wa_stats.get('requested', '?')} "
             f"({_wa_stats.get('unfetched', 0)} not fetched, "
             f"{_wa_stats.get('rate_limited', 0)} rate-limited)")
    return _bundled_set


def _spatial_cell_files(routing_graph_path, cell_scale, output_dir):
    """build_spatial into output_dir: ({cell_id: path}, meta). The index is
    written there too (graph-cells-index.bin), so its bytes are dropped."""
    from streetzim.routing.reader import load_from_file
    from streetzim.routing.spatial import build_spatial
    _, cells, meta = build_spatial(load_from_file(routing_graph_path),
                                   cell_scale=cell_scale, output_dir=output_dir)
    return cells, meta


def _add_routing_graph(creator, MapItem, *, routing_graph_path, routing_graph_chunk_mb, spatial_chunk_scale):
    """The routing graph: SZRG v4 graph.bin (optionally chunked), or the
    spatial SZCI v3 + SZRC v2 cells with --spatial-chunk-scale."""
    # Add routing graph data.
    # Large regions produce multi-hundred-MB / multi-GB graph.bin
    # files (Japan = 1.8 GB, Europe/US ≥ 3 GB). libzim's default
    # ZSTD clustering puts the whole file in one giant compressed
    # cluster, which our in-browser PWA's pure-JS `fzstd` port
    # cannot decompress in a single shot — it throws "invalid zstd
    # data" around ~500 MB. Set COMPRESS=0 so the file lands in
    # its own uncompressed cluster; the cluster header becomes
    # type-1 (raw) and `zim-reader.js` bypasses fzstd entirely.
    # SZRG is a tight binary format already (~10–15% ZSTD gain),
    # so the ZIM grows by only that much in return for PWA-
    # parseable routing.
    if routing_graph_path and os.path.isfile(routing_graph_path):
        _rt_t0 = time.time()
        _rt_size_b = os.path.getsize(routing_graph_path)
        size_mb = _rt_size_b / (1024 * 1024)
        if spatial_chunk_scale and spatial_chunk_scale > 0:
            # In-build spatial chunking — replaces the post-process
            # `cloud/repackage_zim.py --spatial-chunk-scale N` step.
            # Reuses streetzim/routing/spatial.build_spatial which streams
            # from the routing graph file into a spill dir, so peak
            # RSS stays bounded. Output: graph-cells-index.bin (the
            # SZCI index) + graph-cell-NNNNN.bin per cell. Items are added
            # via FileProvider so libzim/zimru streams from disk.
            #
            # SZCI v3 stores coordinates inside cell payloads so
            # mobile readers never load a global node table. Other Kiwix readers
            # use the X/fulltext/xapian + JSON search-data paths
            # we ship, so neither is on the routing hot path here.
            import sys as _sys
            _repo_root = REPO_ROOT
            if str(_repo_root) not in _sys.path:
                _sys.path.insert(0, str(_repo_root))
            from streetzim.isolate import run_in_child
            _spatial_outdir = Path(routing_graph_path).parent / "spatial"
            _spatial_outdir.mkdir(parents=True, exist_ok=True)
            print(f"    Spatial-chunking routing graph "
                  f"(scale={spatial_chunk_scale}, "
                  f"src={size_mb:.1f} MB → {_spatial_outdir})...",
                  flush=True)
            # In a child process: it loads the whole graph (China's is
            # 4.9 GB) on top of everything this process already holds
            # for the ZIM, which took China in 16 GB to 15.6 GB.
            _cells_bytes, _spatial_meta = run_in_child(
                _spatial_cell_files, routing_graph_path, spatial_chunk_scale,
                _spatial_outdir)
            # Index — eager-load by readers, must stay raw when ≥ 200 MB
            # (Kiwix Desktop / iOS WebView decompression watchdog
            # times out on big compressed clusters; see project
            # memory `cells-index-raw-threshold`).
            idx_path = _spatial_outdir / "graph-cells-index.bin"
            idx_size = os.path.getsize(idx_path)
            idx_compress = idx_size < 200 * 1024 * 1024
            creator.add_item(MapItem(
                "routing-data/graph-cells-index.bin",
                "Routing Cells Index",
                "application/octet-stream",
                str(idx_path),
                compress=idx_compress,
            ))
            # Legacy SZCI v2 builds may still report sharded node
            # tables. SZCI v3 reports none.
            node_shard_paths = _spatial_meta.get("node_shard_paths") or []
            for shard_path in node_shard_paths:
                creator.add_item(MapItem(
                    f"routing-data/{os.path.basename(shard_path)}",
                    f"Routing Nodes Shard {os.path.basename(shard_path)}",
                    "application/octet-stream",
                    str(shard_path),
                    compress=True,
                ))
            # Per-cell SZRC files. build_spatial(output_dir=...) has
            # already written each cell to disk; _cells_bytes maps
            # cell_id → path string in that mode. Cap compression at
            # 200 MB to dodge the same fzstd ceiling.
            _cell_count = 0
            for cid in sorted(_cells_bytes.keys()):
                cp = _cells_bytes[cid]  # str path written by build_spatial
                cp_size = os.path.getsize(cp)
                creator.add_item(MapItem(
                    f"routing-data/graph-cell-{cid:05d}.bin",
                    f"Routing Graph Cell {cid}",
                    "application/octet-stream",
                    cp,
                    compress=cp_size < 200 * 1024 * 1024,
                ))
                _cell_count += 1
            print(f"    Wrote spatial routing layout: "
                  f"{_cell_count} cells, {len(node_shard_paths)} node shards, "
                  f"index {idx_size/1e6:.1f} MB", flush=True)
            PHASE_TIMER.record_subphase(
                "zim-pack: routing graph (spatial)", time.time() - _rt_t0,
                note=f"{_cell_count} cells, {len(node_shard_paths)} node shards, "
                     f"index {idx_size/1e6:.1f} MB")
        elif routing_graph_chunk_mb and routing_graph_chunk_mb > 0:
            # Byte-range chunk the primary graph file into N entries
            # so libzim puts each in its own cluster. fzstd's ~500 MB
            # ceiling is the actual blocker for Japan-size ZIMs; this
            # side-steps it without touching the SZRG format.
            # Always emit the monolithic graph.bin — Kiwix iOS
            # (and Desktop, and mcpzim) read it natively via
            # libzim's cluster decompression, independent of the
            # PWA fzstd path. Skipping this broke iOS on Iran
            # 2026-04-24. The chunks below are for PWA fzstd
            # only; both coexist cheaply.
            compress_graph = size_mb < 200
            creator.add_item(MapItem(
                "routing-data/graph.bin",
                "Routing Graph",
                "application/octet-stream",
                routing_graph_path,
                compress=compress_graph,
            ))
            print(f"    Adding routing graph ({size_mb:.1f} MB, "
                  f"{'compressed' if compress_graph else 'raw'}) "
                  f"+ {routing_graph_chunk_mb} MB chunks for PWA...")
            # NOTE: out_prefix is what the manifest records. The
            # reader joins it with the manifest's *directory* inside
            # the ZIM, so keep this in lock-step with the ZIM entry
            # names below ("graph-chunk-NNNN.bin" under routing-data/).
            chunk_paths, manifest = chunk_graph_file(
                routing_graph_path,
                routing_graph_chunk_mb * 1024 * 1024,
                out_prefix="graph-chunk",
            )
            # Manifest first — the reader checks it to learn chunk order.
            creator.add_item(MapItem(
                "routing-data/graph-chunk-manifest.json",
                "Routing Graph Manifest",
                "application/json",
                json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
                compress=True,
            ))
            for i, cp in enumerate(chunk_paths):
                cp_mb = os.path.getsize(cp) / (1024 * 1024)
                compress_chunk = cp_mb < 200
                creator.add_item(MapItem(
                    f"routing-data/graph-chunk-{i:04d}.bin",
                    f"Routing Graph Chunk {i}",
                    "application/octet-stream",
                    cp,
                    compress=compress_chunk,
                ))
            print(f"    Wrote monolithic graph.bin + "
                  f"{len(chunk_paths)} chunks + manifest")
        else:
            # Cap where compression helps more than it hurts: below
            # ~200 MB fzstd handles it fine in one shot, so keep it
            # compressed. Above, skip compression for PWA compat.
            compress_graph = size_mb < 200
            compress_note = "compressed" if compress_graph else "raw (PWA-compat)"
            print(f"    Adding routing graph ({size_mb:.1f} MB, {compress_note})...")
            creator.add_item(MapItem(
                "routing-data/graph.bin",
                "Routing Graph",
                "application/octet-stream",
                routing_graph_path,
                compress=compress_graph,
            ))
        PHASE_TIMER.record_subphase(
            "zim-pack: routing graph", time.time() - _rt_t0,
            note=f"{_rt_size_b/1e6:.0f} MB graph.bin"
                 + (f" + {routing_graph_chunk_mb} MB chunks" if routing_graph_chunk_mb else ""))


def search_record(feat, wiki=None):
    """The search record (docs/search-records.md) of search feature `feat`,
    as search pass 1 writes it; `wiki`: its wiki cross-ref entry (for an
    admin area, admin_wiki()), or None.

    Canonical record shape consumed by mcpzim:
      n, t (type), s (subtype), a (lat), o (lon), l (location)
    Optional additions (safe to forward through their parser):
      w  = wikipedia tag value(s)  (OSM format, e.g. "en:Lincoln_Memorial")
      q  = wikidata Q-ID
      Overture-places enrichment (set by merge_overture_places; empty on
      non-POI rows): ws = website, p = phone, soc = socials, brand = brand
      primary name, wd = brand Wikidata Q-ID, cat = normalized category,
      source = "overture" for Pass-2 adds.
      al, bb, alt, osm = an administrative area's (admin_record_fields).
    Coordinates rounded to 5 dp (~1.1 m). They were emitted at full float64
    repr -- 56.92662663189116, nanometre precision for a bus stop -- and that
    is entropy zstd cannot remove. Measured on 207,848 real records: 1.72 MB
    -> 1.54 MB compressed, ~10% off the search payload, which is ~10% of a
    ZIM. Display and routing read the same field, so 1 m is the floor:
    SEARCH_COORD_DP=7 restores ~1 cm if that ever bites. (Shared with
    ops/cloud/swap_viewer_rust.py --add-admin-areas.)"""
    rec = {"n": feat["name"], "t": feat.get("type", ""), "s": feat.get("subtype", ""),
           "a": round(feat["lat"], _SEARCH_COORD_DP),
           "o": round(feat["lon"], _SEARCH_COORD_DP),
           "l": feat.get("location", "")}
    for ov_key in ("ws", "p", "soc", "brand", "wd", "cat", "source"):
        v = feat.get(ov_key)
        if v:
            rec[ov_key] = v
    rec.update(admin_record_fields(feat))
    if wiki:
        if wiki.get("wikipedia"):
            rec["w"] = wiki["wikipedia"]
            # Provenance: "wd" = title backfilled from a wikidata Q-ID (see
            # --resolve-wikidata-titles); absent = the OSM wikipedia= tag itself.
            if wiki.get("wikipedia_src"):
                rec["wsrc"] = wiki["wikipedia_src"]
        if wiki.get("wikidata"):
            rec["q"] = wiki["wikidata"]
    return rec


def _search_bucket(*, search_features_path, wikidata_data, wiki_cross_refs, loc_lookup, _bundled_set, chunk_tmp, page_types=KIWIX_PAGE_TYPES):
    """Search pass 1: stream the search JSONL into per-prefix and per-category
    chunk files in `chunk_tmp`, plus the Xapian candidates file."""
    xapian_types = page_types

    # Pass 1: stream JSONL -> per-prefix chunk files + xapian file
    chunk_counts = {}
    chunk_fds = {}  # prefix -> open file handle
    xapian_path = os.path.join(chunk_tmp, "_xapian.jsonl")
    total_features = 0
    xapian_count = 0

    # Keys per name: the whole name's first two characters plus each word's
    # (so "cathedral" finds "Washington National Cathedral"), accent-folded
    # and split by the current word rule (marks continue a word). One
    # implementation, shared with the planner and the retrofit, mirrored by
    # the viewer's SEARCH_SHARDS.words / keyFor; the manifest records the
    # rule as "word_rule" (docs/search-prefix-locality.md#word-rule).
    from cloud.search_shards import prefixes_for as _prefixes_for

    # Open-file budget for the per-prefix chunk writers (see the
    # LRU eviction at the write site).
    try:
        import resource as _resource
        _soft, _hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
        _want = min(_hard, 65536) if _hard != _resource.RLIM_INFINITY else 65536
        if _soft < _want:
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (_want, _hard))
            _soft = _want
        _chunk_fd_budget = max(64, _soft - 256)
    except Exception:
        _chunk_fd_budget = 512

    # Per-type counts for streetzim-meta.json, plus a parallel set of
    # chunk files keyed by OSM top-level `type` (category-index).
    # Category-index is a cheap O(1)-per-query alternative to
    # near_places scanning every search-data chunk (mcpzim does that
    # linearly today; see STREETZIM_CONSUMPTION.md).
    type_counts = {}
    wiki_fields_added = 0
    wiki_geo = {}  # title_us -> [lat, lon, type]: geo-index for the
                   # viewer's nearby-Wikipedia list + markers (any zoom)
    cat_chunk_fds = {}
    cat_chunk_counts = {}
    # places.html fetches category-index/place.json whole on load to
    # name the city nearest the viewport. Past this size it is cut
    # into geographic shards (cloud/chip_shards.py) and the viewer
    # reads the one around the viewport: china ships 109 MB today
    # and fails its browser gate on it, europe 480 MB, median 7.7 MB.
    CATEGORY_SHARD_MIN_BYTES = 8 * 1024 * 1024
    cat_shards: dict = {}
    cat_dir = os.path.join(chunk_tmp, "categories")
    os.makedirs(cat_dir, exist_ok=True)
    def _cat_slug(t):
        s = "".join(c if c.isascii() and (c.isalnum() or c == "_") else "_" for c in t.lower())
        return s[:40] or "_"

    _bucket_t0 = time.time()
    print("    Streaming search features from disk...", flush=True)
    with open(xapian_path, "w") as xf:
        with open(search_features_path) as sf:
            for line in sf:
                feat = json.loads(line)
                total_features += 1
                t = feat.get("type", "")
                type_counts[t] = type_counts.get(t, 0) + 1

                # Enrich with location (state, country) if missing
                # (An admin area's `location` is its region, set when it
                # was extracted, or none for a country.)
                if loc_lookup and not feat.get("location") and t != "admin":
                    feat["location"] = loc_lookup(feat["lat"], feat["lon"])

                # Enrich with wiki cross-refs if this POI has matching
                # (name, coord) in the OSM-tag lookup built from the PBF.
                wiki = None
                if t == "admin":
                    # An admin area carries its relation's own tags; a
                    # name+point match could be another object (the place
                    # node at the area's admin_centre).
                    wiki = admin_wiki(feat, wiki_cross_refs)
                    if wiki:
                        wiki_fields_added += 1
                elif wiki_cross_refs:
                    # A merged street keeps every piece's point in
                    # _pts (merge_streets_in_file); the OSM tag may
                    # match any of them.
                    for plat, plon in ([(feat["lat"], feat["lon"])]
                                       + [tuple(p) for p in feat.get("_pts", ())]):
                        wiki = wiki_cross_refs.get((
                            feat["name"].lower(),
                            int(round(plat * 1e4)),
                            int(round(plon * 1e4)),
                        ))
                        if wiki:
                            break
                    if wiki:
                        wiki_fields_added += 1

                # The record shape and its rounding: search_record.
                rec = search_record(feat, wiki)
                if wiki:
                    if wiki.get("wikipedia"):
                        # Geo-index: underscored title -> [lat, lon, type].
                        # Matches the bundled wiki-article/<Title> path so
                        # the viewer can list + pin nearby Wikipedia at any
                        # zoom (no tile scan, no side-loaded bridge).
                        _wt = wiki["wikipedia"]
                        _ci = _wt.find(":")
                        _gt = (_wt[_ci + 1:] if 2 <= _ci <= 3
                               and _wt[:_ci].isalpha() else _wt).replace(" ", "_")
                        # Only index titles whose article was actually
                        # bundled (enwiki had a page) — else the viewer
                        # would list places that 404 on "Read full article".
                        if (_gt and _gt not in wiki_geo
                                and _bundled_set is not None
                                and _gt in _bundled_set):
                            # [lat, lon, type, qid, desc]. The short
                            # description is baked in so the viewer's
                            # nearby-Wikipedia list needs NO
                            # wikidata/<prefix>.json chunk. Those chunks
                            # are keyed by Q-ID prefix and are
                            # region-global (the 10–13 prefixes are
                            # 20–45 MB each); a wide "explore" used to
                            # prefetch several at once and OOM mobile.
                            _gq = wiki.get("wikidata")
                            _gd = ""
                            if _gq and wikidata_data:
                                _gwd = wikidata_data.get(_gq)
                                if _gwd:
                                    _gd = (_gwd.get("d") or "")[:160]
                            wiki_geo[_gt] = [round(feat["lat"], 5),
                                             round(feat["lon"], 5), t,
                                             _gq, _gd]
                entry = json.dumps(rec, separators=(",", ":")) + "\n"

                # Write abbreviated entry to per-prefix chunk file(s).
                # Index under each word's prefix — duplicates entries
                # across 1–4 chunks (avg ~2×) but enables substring
                # hits like "cathedral" → "Washington National Cathedral".
                # An admin area is found by its other names too.
                _keys = _prefixes_for(feat["name"])
                for _alt in rec.get("alt", ()):
                    _keys |= _prefixes_for(_alt)
                for prefix in sorted(_keys):
                    if prefix not in chunk_fds:
                        if len(chunk_fds) >= _chunk_fd_budget:
                            # `u<hex>` buckets give one file per
                            # leading codepoint (thousands on CJK
                            # regions); keep the open-fd count
                            # under the soft limit by closing the
                            # least recently used handle (append
                            # reopens it later).
                            lru_prefix = next(iter(chunk_fds))
                            chunk_fds.pop(lru_prefix).close()
                        chunk_fds[prefix] = open(
                            os.path.join(chunk_tmp, f"{prefix}.jsonl"), "a",
                            encoding="utf-8")
                        chunk_counts.setdefault(prefix, 0)
                    else:
                        # Refresh recency (dict order = LRU order).
                        chunk_fds[prefix] = chunk_fds.pop(prefix)
                    chunk_fds[prefix].write(entry)
                    chunk_counts[prefix] += 1

                # Also write to the category-index (one file per type).
                # Same record shape so downstream consumers stay trivial.
                if t:
                    cat_slug = _cat_slug(t)
                    if cat_slug not in cat_chunk_fds:
                        cat_chunk_fds[cat_slug] = open(
                            os.path.join(cat_dir, f"{cat_slug}.jsonl"), "w")
                        cat_chunk_counts[cat_slug] = 0
                    cat_chunk_fds[cat_slug].write(entry)
                    cat_chunk_counts[cat_slug] += 1

                # Collect xapian-eligible features separately
                if feat["type"] in xapian_types:
                    xf.write(line)
                    xapian_count += 1

                if total_features % 500_000 == 0:
                    print(f"\r    Bucketed {total_features} features into {len(chunk_counts)} chunks...", end="", flush=True)
    for fd in cat_chunk_fds.values():
        fd.close()
    del cat_chunk_fds
    if wiki_fields_added:
        print(f"    Enriched {wiki_fields_added} entries with wiki cross-refs")

    # Close all chunk file handles
    for fd in chunk_fds.values():
        fd.close()
    del chunk_fds

    print(f"\r    Bucketed {total_features} features into {len(chunk_counts)} chunks, {xapian_count} xapian entries", flush=True)
    PHASE_TIMER.record_subphase(
        "zim-pack: search bucketing (pass 1)", time.time() - _bucket_t0,
        note=f"{total_features:,} features → {len(chunk_counts)} chunks, {xapian_count:,} xapian entries")
    return SearchBuckets(chunk_tmp=chunk_tmp, chunk_counts=chunk_counts, xapian_path=xapian_path, total_features=total_features, xapian_count=xapian_count, type_counts=type_counts, wiki_fields_added=wiki_fields_added, wiki_geo=wiki_geo, cat_chunk_counts=cat_chunk_counts, cat_dir=cat_dir, cat_shards=cat_shards, CATEGORY_SHARD_MIN_BYTES=CATEGORY_SHARD_MIN_BYTES)


def _search_emit_chunks(creator, MapItem, *, split_hot_search_chunks_mb, chunk_tmp, chunk_counts, total_features, manifest_extra=None):
    """Search pass 2: emit search-data/<prefix>.json chunks (splitting hot
    prefixes) and the search manifest. ``manifest_extra`` adds keys to the
    manifest (a retrofit carries the source's other keys through); it
    cannot override what this pass computed."""
    _emit_t0 = time.time()

    # Pass 2: read each chunk file, serialize, and emit. When
    # `split_hot_search_chunks_mb` > 0, fan out any chunk whose
    # JSON exceeds that threshold into 16 FNV-1a sub-buckets
    # (`{prefix}-{0..f}.json`). The manifest then records the
    # fan-out in ``sub_chunks`` so clients (viewer, Swift) know
    # which queries to spread across sub-files.
    #
    # Accumulate manifest mutations during the emission loop
    # (instead of writing the manifest up-front) so split vs
    # passthrough decisions are reflected in the final manifest.
    hot_split_bytes = (split_hot_search_chunks_mb * 1024 * 1024
                       if split_hot_search_chunks_mb > 0 else None)
    hot_split_N = 16
    manifest_chunks: dict[str, int] = {}
    manifest_sub_chunks: dict[str, list[str]] = {}
    split_total = 0

    chunks_added = 0
    manifest_char_split: dict[str, list[str]] = {}
    # A prefix whose plan groups small siblings under a range token
    # (cloud/search_shards.py GROUP_BYTES) is listed here instead, so a
    # viewer that predates ranges reads it whole rather than wrongly.
    manifest_char_ranges: dict[str, list[str]] = {}

    def _emit_whole_chunk(prefix, chunk_path):
        """Small prefix: one file, exactly as before."""
        entries = []
        with open(chunk_path, encoding="utf-8") as cf:
            for cline in cf:
                entries.append(json.loads(cline))
        creator.add_item(MapItem(
            f"search-data/{prefix}.json",
            f"Search chunk {prefix}",
            "application/json",
            json.dumps(entries, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8"),
        ))
        manifest_chunks[prefix] = len(entries)

    for prefix in sorted(chunk_counts):
        chunk_path = os.path.join(chunk_tmp, f"{prefix}.jsonl")
        if not hot_split_bytes:
            _emit_whole_chunk(prefix, chunk_path)
            os.unlink(chunk_path)
            chunks_added += 1
            continue

        # Pass 1: size the prefix WITHOUT holding it. Reading a whole
        # hot prefix into a list is what used to stall a continent
        # build — `av` on united-states is 2.93 GB of JSON.
        from cloud.search_shards import (
            Aggregator, SHARD_TARGET_BYTES, char_split_paths,
            leaf_for, plan_by_tier, split_key, tier_for)
        from cloud.search_shards import split_records_recursive as _split_records_recursive
        agg = Aggregator(prefix)
        total_chunk_bytes = 0
        with open(chunk_path, encoding="utf-8") as cf:
            for cline in cf:
                size = len(cline.encode("utf-8"))
                total_chunk_bytes += size
                agg.add(json.loads(cline), size)
        if total_chunk_bytes <= hot_split_bytes:
            _emit_whole_chunk(prefix, chunk_path)
            os.unlink(chunk_path)
            chunks_added += 1
            continue

        # Hot prefix: character paths + tiers, so a reader fetches the
        # leaf matching what was typed instead of every leaf under the
        # prefix (docs/search-prefix-locality.md).
        target = min(hot_split_bytes, SHARD_TARGET_BYTES)
        planned = agg.leaves(target_bytes=target)
        planned_paths = plan_by_tier(planned)

        # Pass 2: stream records into one temp file per leaf, LRU over
        # open descriptors so a 1000-leaf prefix cannot exhaust them.
        leaf_dir = os.path.join(chunk_tmp, f"{prefix}.leaves")
        os.makedirs(leaf_dir, exist_ok=True)
        leaf_fds: dict[str, object] = {}
        leaf_seen: set[str] = set()
        LEAF_FD_CAP = 256

        # The per-prefix state is bound as defaults: this function is
        # redefined on each prefix and must never see another prefix's.
        def _leaf_fd(name, leaf_fds=leaf_fds, leaf_seen=leaf_seen,
                     leaf_dir=leaf_dir, cap=LEAF_FD_CAP):
            fd = leaf_fds.get(name)
            if fd is not None:
                # Refresh recency. dict order is insertion order, so
                # without this the cache evicts FIFO: 'ca' on
                # south-america plans ~2856 leaves against 256 slots
                # and records arrive in ingest order, so nearly every
                # one of 19.6 M writes would reopen a file.
                leaf_fds[name] = leaf_fds.pop(name)
                return fd
            if len(leaf_fds) >= cap:
                leaf_fds.pop(next(iter(leaf_fds))).close()
            leaf_seen.add(name)
            fd = open(os.path.join(leaf_dir, name + ".jsonl"), "a",
                      encoding="utf-8")
            leaf_fds[name] = fd
            return fd

        orphans = 0
        first_orphan = ""
        with open(chunk_path, encoding="utf-8") as cf:
            for cline in cf:
                rec = json.loads(cline)
                paths = planned_paths.get(tier_for(rec), ())
                names = list(leaf_for(prefix, rec, paths))
                if not names:
                    orphans += 1
                    if not first_orphan:
                        first_orphan = (rec.get("n") or "")[:60]
                    continue
                for lname in names:
                    _leaf_fd(lname).write(cline if cline.endswith("\n")
                                          else cline + "\n")
        for fd in leaf_fds.values():
            fd.close()
        leaf_fds.clear()
        if orphans:
            # A record that reaches no leaf is a place the user can
            # never find again, and nothing downstream would notice.
            raise RuntimeError(
                f"search-data {prefix}: {orphans} record(s) matched no "
                f"leaf (first: {first_orphan!r}) — the planner and the "
                f"writer disagree about paths")
        if not leaf_seen:
            # Never ship sub_chunks[prefix] = [] — old clients fall
            # back to a name scan and would find nothing.
            _emit_whole_chunk(prefix, chunk_path)
            os.unlink(chunk_path)
            os.rmdir(leaf_dir)
            chunks_added += 1
            continue
        os.unlink(chunk_path)

        # Emit each leaf; a leaf that characters could not divide
        # ("Carrera 7" a million times) still gets the hash split, so
        # no chunk ships over the validator's size bar.
        sub_prefix_list = []
        for lname in sorted(leaf_seen):
            lpath = os.path.join(leaf_dir, lname + ".jsonl")
            lrecs = []
            with open(lpath, encoding="utf-8") as lf:
                for lineno, lline in enumerate(lf):
                    if not lline.strip():
                        continue
                    try:
                        lrecs.append(json.loads(lline))
                    except Exception as exc:
                        # A leaf that does not round-trip means a
                        # record was written incomplete. Observed
                        # twice in the retrofit on east-coast-us
                        # prefix 44 and not reproducible afterwards,
                        # so the cause is still open — until it is,
                        # fail the build rather than ship search data
                        # that has silently lost a place.
                        raise RuntimeError(
                            f"search-data {prefix}: leaf {lname} line "
                            f"{lineno} did not round-trip ({exc}); "
                            f"{len(lline)} bytes: {lline[:80]!r}") from exc
            os.unlink(lpath)
            lbytes = json.dumps(lrecs, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
            if len(lbytes) > hot_split_bytes:
                for sub_prefix, sub_bytes, leaf_count in \
                        _split_records_recursive(
                            lrecs, lname, hot_split_bytes,
                            n_buckets=hot_split_N, max_depth=5):
                    creator.add_item(MapItem(
                        f"search-data/{sub_prefix}.json",
                        f"Search chunk {sub_prefix}",
                        "application/json", sub_bytes))
                    manifest_chunks[sub_prefix] = leaf_count
                    sub_prefix_list.append(sub_prefix)
                    split_total += 1
            else:
                creator.add_item(MapItem(
                    f"search-data/{lname}.json",
                    f"Search chunk {lname}",
                    "application/json", lbytes))
                manifest_chunks[lname] = len(lrecs)
                sub_prefix_list.append(lname)
                split_total += 1
            del lrecs
        os.rmdir(leaf_dir)
        # Old clients (iOS, in-ZIM apps) resolve sub_chunks and fetch
        # every leaf: as slow as before, never wrong. New clients use
        # char_split to pick one.
        manifest_sub_chunks[prefix] = sub_prefix_list
        (manifest_char_ranges if split_key(planned) == "char_ranges"
         else manifest_char_split)[prefix] = char_split_paths(planned)
        chunks_added += 1
        if chunks_added % 100 == 0:
            print(f"\r    Added {chunks_added}/{len(chunk_counts)} search chunks...", end="", flush=True)

    # Emit the manifest AFTER the emission loop so it reflects
    # every split decision.
    from cloud.search_shards import WORD_RULE
    # "word_rule": how names were split into words for keys and paths.
    # Readers that find it absent use rule 1, so every older ZIM keeps
    # working with a newer viewer.
    manifest_dict: dict = dict(manifest_extra or {})
    manifest_dict.update({"total": total_features,
                          "word_rule": WORD_RULE,
                          "chunks": manifest_chunks})
    manifest_dict.pop("sub_chunks", None)
    manifest_dict.pop("char_split", None)
    manifest_dict.pop("char_ranges", None)
    if manifest_sub_chunks:
        manifest_dict["sub_chunks"] = manifest_sub_chunks
    if manifest_char_split:
        manifest_dict["char_split"] = manifest_char_split
    if manifest_char_ranges:
        manifest_dict["char_ranges"] = manifest_char_ranges
    creator.add_item(MapItem(
        "search-data/manifest.json", "Search Manifest",
        "application/json",
        json.dumps(manifest_dict, separators=(",", ":")).encode("utf-8"),
    ))

    if hot_split_bytes:
        print(f"\r    Added {chunks_added} chunks; "
              f"{len(manifest_sub_chunks)} hot prefix(es) split → "
              f"{split_total} sub-chunks "
              f"({total_features} features)          ",
              flush=True)
    else:
        print(f"\r    Added {chunks_added} search chunks "
              f"({total_features} features)          ",
              flush=True)
    PHASE_TIMER.record_subphase(
        "zim-pack: search-data emit (pass 2)", time.time() - _emit_t0,
        note=f"{chunks_added} chunks"
             + (f", {len(manifest_sub_chunks)} hot prefixes split → {split_total} sub-chunks" if hot_split_bytes else ""))


def _search_category_index(creator, MapItem, *, split_find_chips, no_llm_bundle, wiki_geo, cat_chunk_counts, cat_dir, cat_shards, CATEGORY_SHARD_MIN_BYTES):
    """Search pass 2b: category-index files, Find chips and the wiki geo-index."""
    _cat_t0 = time.time()

    # Pass 2b: category-index files (optional, mirrors search-data
    # chunks but keyed by OSM top-level `type`). Lets consumers answer
    # "all museums in this region" with one file read instead of a
    # linear scan. Same canonical record shape as search-data chunks.
    if cat_chunk_counts:
        cat_total_records = 0
        # poi and park JSONL files kept on disk for the Find chips,
        # which are split from them a chip at a time (not held as dicts:
        # China's poi records took over 8 GB that way).
        chip_sources: dict[str, str] = {}
        # The LLM bundle (addr/poi/street.json) is the heaviest
        # part of category-index — hundreds of MB to multi-GB on
        # continent regions. With `no_llm_bundle=True` (what
        # build-region-fast.sh passes) we skip writing them;
        # cloud/repackage_zim.py drops them from older ZIMs by
        # default. Chip emission still
        # gets `chip_sources` populated below so chip-*.json
        # files are derivable. The category manifest also drops
        # the entries we skipped, so validators don't complain
        # about declared-but-missing categories.
        _llm_bundle = {"addr", "poi", "street"}
        _llm_skipped = []
        for cat_slug in sorted(cat_chunk_counts):
            cat_path = os.path.join(cat_dir, f"{cat_slug}.jsonl")
            if no_llm_bundle and cat_slug in _llm_bundle:
                # Skipped categories: only poi/park are needed
                # (for chip emission). addr/street are the 100M+
                # line files on continents — loading them into a
                # list of dicts just to drop them was a
                # tens-of-GB allocation for nothing. Count lines
                # streaming and move on.
                with open(cat_path, "rb") as cf:
                    cat_total_records += sum(1 for _ in cf)
                if split_find_chips and cat_slug in ("poi", "park"):
                    chip_sources[cat_slug] = cat_path
                else:
                    os.unlink(cat_path)
                _llm_skipped.append(cat_slug)
                continue
            entries = []
            with open(cat_path, encoding="utf-8") as cf:
                for cline in cf:
                    entries.append(json.loads(cline))
            if split_find_chips and cat_slug in ("poi", "park"):
                chip_sources[cat_slug] = cat_path
            else:
                os.unlink(cat_path)
            # ensure_ascii=False: \uXXXX escapes roughly doubled
            # CJK category/chip files (search-data already uses it).
            chunk_json = json.dumps(entries, separators=(",", ":"),
                                    ensure_ascii=False)
            # places.html fetches place.json whole, on load, only to
            # name the city nearest the viewport — 109 MB on china
            # (whose browser gate fails on it), 480 MB on europe.
            # Shard it the way Find chips are sharded so the viewer
            # reads the shard around the viewport instead.
            # Shard ANY oversized category, not just `place`.
            # europe ships category-index/park.json at 53 MB and
            # china's place.json at 109 MB; the 48 MB cap in
            # validate_zim is an error-severity gate, so park alone
            # blocked europe's upload entirely (live europe was still
            # 2026-05-06 as a result). Restricting this to "place" was
            # arbitrary -- the shard format and the viewer's reader are
            # both keyed on the manifest, not on the slug.
            if (len(chunk_json.encode("utf-8")) > CATEGORY_SHARD_MIN_BYTES):
                from cloud.chip_shards import plan_chip
                cplan = plan_chip(entries)
                n_cat_files = 0
                for cpath, ctitle, cblob in cplan.files(
                        cat_slug, cat_slug, name_prefix="",
                        title_kind="Category index"):
                    creator.add_item(MapItem(cpath, ctitle,
                                             "application/json", cblob))
                    n_cat_files += 1
                # plan_chip decides for itself whether to split. If it
                # declines, files() emits the single whole file, so a
                # category_shards entry would declare a sharded layout
                # with no shard files behind it (caught in the
                # swap_viewer_rust path on 2026-09-21).
                if cplan.sharded:
                    cat_shards[cat_slug] = cplan.manifest_entry(cat_slug)
                print(f"    category-index/{cat_slug}: {len(entries):,} records "
                      f"({len(chunk_json)/1048576:.1f} MB) → {n_cat_files} shard(s)",
                      flush=True)
                del cplan
            else:
                creator.add_item(MapItem(
                    f"category-index/{cat_slug}.json",
                    f"Category index {cat_slug}",
                    "application/json",
                    chunk_json.encode("utf-8"),
                ))
            cat_total_records += len(entries)
            del entries, chunk_json
        if _llm_skipped:
            print(f"    --no-llm-bundle: skipped category-index/{{{','.join(_llm_skipped)}}}.json", flush=True)
        # Validator's `places_categories` check picks the first
        # listed category and tries to read it; with the LLM
        # bundle dropped we'd point at addr.json which we
        # didn't write. Strip the dropped slugs from the
        # categories manifest so the check finds a real entry.
        cat_manifest = {k: cat_chunk_counts[k]
                        for k in sorted(cat_chunk_counts)
                        if not (no_llm_bundle and k in _llm_bundle)}
        manifest_payload = {"total": cat_total_records,
                            "categories": cat_manifest}
        if cat_shards:
            # Which categories were cut into geographic shards, in the
            # same shape as a chip entry, so the viewer can fetch the
            # shard around the viewport instead of the whole file.
            manifest_payload["category_shards"] = cat_shards
        if split_find_chips and chip_sources:
            from cloud.chip_rules import CHIP_RULES, read_jsonl, split_jsonl_by_chip
            from cloud.chip_shards import plan_chip
            chip_paths = split_jsonl_by_chip(chip_sources, cat_dir)
            for src in chip_sources.values():
                os.unlink(src)
            chips_manifest: dict = {}
            # Chips over 2 MiB are cut into geographic shards
            # (cloud/chip_shards.py) so a phone fetches only the
            # shards around the viewport. The old name-hash
            # buckets had no locality: every Find tap loaded the
            # whole chip (east-coast-us Shops: 147 MB of JSON).
            n_chip_files = 0
            for chip in CHIP_RULES:
                plan = plan_chip(read_jsonl(chip_paths[chip.id]))
                os.unlink(chip_paths[chip.id])
                for path, title, blob in plan.files(chip.id, chip.label):
                    creator.add_item(MapItem(path, title, "application/json", blob))
                    n_chip_files += 1
                chips_manifest[chip.id] = plan.manifest_entry(chip.label)
                del plan
            manifest_payload["chips"] = chips_manifest
            print(f"    Added {len(chips_manifest)} chips in {n_chip_files} files "
                  f"({sum(c['count'] for c in chips_manifest.values())} records)")
        creator.add_item(MapItem(
            "category-index/manifest.json",
            "Category Index Manifest",
            "application/json",
            json.dumps(manifest_payload, separators=(",", ":")).encode("utf-8"),
        ))
        print(f"    Added category-index: "
              f"{len(cat_chunk_counts)} categories, {cat_total_records} records")
        # Wiki geo-index: {title: [lat, lon, type]} for every placed
        # bundled-article, so the viewer renders the nearby-Wikipedia
        # list + map markers at any zoom without scanning vector tiles
        # or a side-loaded bridge. Tiny (~0.005% of the ZIM).
        if wiki_geo:
            creator.add_item(MapItem(
                "wiki-geo-index.json",
                "Wikipedia Geo Index",
                "application/json",
                json.dumps(wiki_geo, separators=(",", ":")).encode("utf-8"),
            ))
            print(f"    Added wiki-geo-index: {len(wiki_geo)} placed "
                  f"articles", flush=True)
        PHASE_TIMER.record_subphase(
            "zim-pack: chips + category-index", time.time() - _cat_t0,
            note=f"{len(cat_chunk_counts)} categories, {cat_total_records:,} records"
                 + (f", {len(chips_manifest) if 'chips_manifest' in dir() else 0} chip files" if cat_chunk_counts else ""))


def _add_meta_json(creator, MapItem, *, map_config, name, bbox, wikidata_data, routing_graph_path, address_count, total_features, type_counts, wiki_fields_added):
    """streetzim-meta.json: the ZIM-level summary for offline agents."""
    # streetzim-meta.json — ZIM-level summary for offline LLM agents.
    # Shape matches the mcpzim consumption contract (see
    # docs/STREETZIM_CONSUMPTION.md) so they can expose a `zim_info`
    # tool without inferring capabilities from filenames.
    routing_stats = {}
    if routing_graph_path and os.path.isfile(routing_graph_path):
        try:
            import struct as _struct
            with open(routing_graph_path, "rb") as _rf:
                _magic = _rf.read(4)
                _hdr = _struct.unpack("<7I", _rf.read(28))
                if _magic == b"SZRG":
                    routing_stats = {
                        "version": int(_hdr[0]),
                        "nodes": int(_hdr[1]),
                        "edges": int(_hdr[2]),
                        "geoms": int(_hdr[3]),
                    }
        except Exception:
            pass

    meta = {
        "name": map_config.get("name", name),
        "buildDate": time.strftime("%Y-%m-%d"),
        "hasRouting": bool(routing_graph_path),
        "hasSatellite": bool(map_config.get("hasSatellite")),
        "hasTerrain": bool(map_config.get("hasTerrain")),
        "hasWikidata": bool(map_config.get("hasWikidata")),
        "hasOvertureAddresses": bool(map_config.get("hasOvertureAddresses")),
        "hasAddresses": address_count > 0,
        "counts": {
            "total": total_features,
            "addresses": int(address_count),
            "byType": type_counts,
            "wikiCrossRefs": int(wiki_fields_added),
            "wikidataEntries": int(len(wikidata_data) if wikidata_data else 0),
        },
    }
    if bbox:
        meta["bbox"] = list(bbox)  # [minLon, minLat, maxLon, maxLat]
    if routing_stats:
        meta["routingGraph"] = routing_stats
    meta["wikipediaLang"] = "en"  # we emit OSM-raw `<lang>:<Title>`; en is the dominant edition we reference
    creator.add_item(MapItem(
        "streetzim-meta.json", "StreetZim Meta", "application/json",
        json.dumps(meta, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8"),
    ))
    print(f"    Added streetzim-meta.json (name={meta['name']}, "
          f"types={len(type_counts)}, addresses={address_count})")


def _add_overture_credits(creator, MapItem, *, overture_sources, overture_themes,
                          overture_release=None):
    """overture-sources.json (always written; empty without Overture data).

    `overture_release` is {theme: release} for the parquets merged (see
    streetzim.overture.overture_release). `release` is written when they
    agree, `releases` always; neither when no release is known."""
    # Overture dataset credits. Written when --overture-addresses
    # was used so the viewer's Sources panel (and the ZIM-level
    # License metadata) can point readers at the actual upstream
    # feeds the address enrichment came from — OpenAddresses
    # contributors, national/regional registers, etc.
    if overture_sources:
        themes = overture_themes or ["addresses"]
        themes_phrase = (
            "Address data is derived from the Overture addresses theme"
            if themes == ["addresses"] else
            "Place info (POIs, websites, phones, socials, brand,"
            " categories) is derived from the Overture places theme"
            if themes == ["places"] else
            "Address + place info (POIs, websites, phones, socials,"
            " brand, categories) are derived from the Overture"
            " addresses + places themes"
        )
        # Filter out the salvage sentinel — when the build ran
        # with --skip-address-extract, overture_sources holds the
        # marker ``__salvage_inherited__`` instead of real dataset
        # names (the prior merge's dataset list isn't retained in
        # the search cache). We still want to emit the JSON so the
        # static link in index.html resolves and zimcheck doesn't
        # flag it; the ``_note`` field below makes the situation
        # explicit, and canonicalCredits points users at the
        # authoritative upstream list.
        real_datasets = [d for d in overture_sources
                         if not d.startswith("__")]
        is_salvage_stub = (not real_datasets
                           and any(d.startswith("__") for d in overture_sources))
        attribution_tail = (
            "see canonicalCredits URL for the upstream dataset list."
            if is_salvage_stub
            else "credits for each underlying dataset follow."
        )
        releases = {t: r for t, r in (overture_release or {}).items() if r}
        overture_doc = {}
        if releases and len(set(releases.values())) == 1:
            overture_doc["release"] = next(iter(releases.values()))
        if releases:
            overture_doc["releases"] = releases
        overture_doc.update({
            "themes": themes,
            "attribution": (
                "© OpenStreetMap contributors and Overture Maps "
                "Foundation (overturemaps.org). "
                f"{themes_phrase}; "
                f"{attribution_tail}"
            ),
            "datasets": real_datasets,
            "canonicalCredits": "https://docs.overturemaps.org/attribution/",
        })
        if is_salvage_stub:
            overture_doc["_note"] = (
                "Salvage rebuild — upstream dataset list not "
                "retained from prior search cache. The data is "
                "present in this ZIM's search index, but the "
                "per-feed list lives in the original Overture "
                "parquet metadata which the salvage cache didn't "
                "preserve.")
        creator.add_item(MapItem(
            "overture-sources.json", "Overture Dataset Credits",
            "application/json",
            json.dumps(overture_doc, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8"),
        ))
        if is_salvage_stub:
            print("    Added overture-sources.json "
                  "(stub — salvage rebuild, upstream dataset list "
                  "not retained)")
        else:
            print(f"    Added overture-sources.json "
                  f"({len(real_datasets)} upstream datasets, "
                  f"release {overture_doc.get('release') or releases or 'unknown'})")
    else:
        # index.html links overture-sources.json statically, so a
        # build without Overture data must still ship the file —
        # otherwise zimcheck's link checker fails the validator on
        # every plain --pbf build.
        creator.add_item(MapItem(
            "overture-sources.json", "Overture Dataset Credits",
            "application/json",
            json.dumps({
                "themes": [],
                "datasets": [],
                "attribution": "This build contains no Overture Maps data.",
                "canonicalCredits": "https://docs.overturemaps.org/attribution/",
            }, separators=(",", ":")).encode("utf-8"),
        ))
        print("    Added overture-sources.json (empty — no Overture themes in this build)")


def _add_kiwix_pages(creator, MapItem, feats, *, count, total, corpus=None):
    """Kiwix's own search: the i-th feature of `feats` (the records of a page
    type, in feature order) gets its page search/<slug>-<i>.html (search_page;
    a front article: Kiwix suggests it) and its redirects (add_alt_titles).
    libzim indexes the pages (--xapian=libzim); `corpus` (XapianCorpus,
    --xapian=builder) gets each page's xapianbuilder documents instead.
    Returns how many pages were written."""
    print(f"    Adding {count} Kiwix search pages (of {total} total)...", flush=True)
    t0 = time.time()
    i = 0
    for i, feat in enumerate(feats, 1):
        path, title, page_html = search_page(feat, i - 1)
        creator.add_item(MapItem(path, title, "text/html", page_html.encode("utf-8"),
                                 is_front=True))   # in the title index: Kiwix suggestions
        add_alt_titles(creator, path, feat)
        if corpus is not None:
            corpus.page(feat, path)
        if i % 2000 == 0:
            rate = i / max(time.time() - t0, 1e-9)
            remaining = (count - i) / rate if rate > 0 else 0
            print(f"\r    Added {i}/{count} search pages ({rate:.0f}/s, ~{remaining/60:.0f}m left)...",
                  end="", flush=True)
    print(f"\r    Added {i} search pages in {time.time() - t0:.0f}s                ", flush=True)
    return i


def _add_builder_xapian(creator, MapItem, corpus, *, workdir, xapianbuilder_bin):
    """--xapian=builder: build both Xapian databases from `corpus` with the
    external xapianbuilder and add them at X/fulltext/xapian and
    X/title/xapian, uncompressed (Kiwix convention: libzim's reader maps
    them in place). Saves the ~2-6h libzim spends indexing on
    continent-scale ZIMs."""
    inputs = corpus.close()
    print(f"    Building Xapian indexes via xapianbuilder ({corpus.pages} pages, "
          f"{corpus.redirects} redirect titles, {corpus.extra} other documents)...",
          flush=True)
    PHASE_TIMER.record_metric("xapian: input records",
                              f"{corpus.pages + corpus.redirects + corpus.extra:,}", "")
    ft_glass, ti_glass = _build_xapian_via_xapianbuilder(
        workdir, inputs=inputs, language=corpus.language,
        binary_override=xapianbuilder_bin)
    corpus.remove()
    creator.add_item(MapItem("fulltext/xapian", "", "application/octet-stream+xapian",
                             ft_glass, is_front=False, compress=False, namespace="X"))
    creator.add_item(MapItem("title/xapian", "", "application/octet-stream+xapian",
                             ti_glass, is_front=False, compress=False, namespace="X"))


def _search_xapian_pages(creator, MapItem, *, xapian_mode, xapian_path, total_features,
                         xapian_count, corpus=None):
    """Search pass 3: Kiwix's own search, per --xapian mode. The pages are
    the same in the libzim and builder modes (_add_kiwix_pages); libzim
    indexes them as it finalizes, xapianbuilder (`corpus`, _create_zim calls
    _add_builder_xapian) after the search passes."""
    if xapian_mode in ("libzim", "builder"):
        def feats():
            with open(xapian_path) as xf:
                for line in xf:
                    yield json.loads(line)
        _add_kiwix_pages(creator, MapItem, feats(), count=xapian_count,
                         total=total_features,
                         corpus=corpus if xapian_mode == "builder" else None)
        os.unlink(xapian_path)
    elif xapian_mode == "none":
        # No Xapian. Drop the JSONL — nothing reads it.
        try: os.unlink(xapian_path)
        except OSError: pass
        print(f"    --xapian=none — skipped {xapian_count} Xapian pages "
              "(no fulltext/title indexes; users search via places.html)",
              flush=True)


def _add_search_in_memory(creator, MapItem, *, search_features, loc_lookup,
                          page_types=KIWIX_PAGE_TYPES, wiki_cross_refs=None,
                          corpus=None):
    """Search for an in-memory feature list (small builds and tests). Its
    Kiwix pages are written in every --xapian mode; `corpus`
    (--xapian=builder) gets their xapianbuilder documents."""
    print(f"    Adding {len(search_features)} search entries...")

    # Enrich with location if available
    # (An admin area's `location` is its region, or none for a country:
    # as in _search_bucket.)
    if loc_lookup:
        for f in search_features:
            if not f.get("location") and f.get("type") != "admin":
                f["location"] = loc_lookup(f["lat"], f["lon"])

    def _key(name):
        prefix = name.lower()[:2].replace(" ", "_")
        prefix = "".join(c if c.isalnum() or c == "_" else "_" for c in prefix)
        return (prefix or "__")[:2].ljust(2, "_")

    # Build chunked search index for scalable on-demand loading.
    from collections import defaultdict
    chunks = defaultdict(list)
    for f in search_features:
        rec = {"n": f["name"], "t": f["type"], "s": f.get("subtype", ""),
               "a": f["lat"], "o": f["lon"], "l": f.get("location", ""),
               **admin_record_fields(f)}
        if f.get("type") == "admin":
            # The relation's own tags, as resolved (see _search_bucket).
            wiki = admin_wiki(f, wiki_cross_refs) or {}
            if wiki.get("wikipedia"):
                rec["w"] = wiki["wikipedia"]
                if wiki.get("wikipedia_src"):
                    rec["wsrc"] = wiki["wikipedia_src"]
            if wiki.get("wikidata"):
                rec["q"] = wiki["wikidata"]
        # Under its other names too, as _search_bucket does.
        for prefix in sorted({_key(n) for n in [f["name"], *rec.get("alt", ())]}):
            chunks[prefix].append(rec)

    from cloud.search_shards import WORD_RULE
    manifest = {k: len(v) for k, v in sorted(chunks.items())}
    total_features = len(search_features)   # a record under 2 keys counts once
    creator.add_item(MapItem(
        "search-data/manifest.json", "Search Manifest", "application/json",
        # word_rule as every writer records it. This legacy path keys by
        # the whole name only, so no word split is involved either way.
        json.dumps({"total": total_features, "word_rule": WORD_RULE,
                    "chunks": manifest},
                   separators=(",", ":")).encode("utf-8"),
    ))

    for prefix, entries in sorted(chunks.items()):
        chunk_json = json.dumps(entries, separators=(",", ":"))
        creator.add_item(MapItem(
            f"search-data/{prefix}.json",
            f"Search chunk {prefix}",
            "application/json",
            chunk_json.encode("utf-8"),
        ))

    print(f"    Added {len(chunks)} search chunks ({total_features} features)")

    xapian_features = [f for f in search_features if f["type"] in page_types]
    _add_kiwix_pages(creator, MapItem, xapian_features, count=len(xapian_features),
                     total=len(search_features), corpus=corpus)
