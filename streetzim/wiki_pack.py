"""Wikipedia packs: one language's Wikipedia about one region's places.

The language matrix (docs/language-matrix-prototype.md, "Wikipedia packs")
splices packs onto a region's published ZIM (the base). A pack is a small
ZIM whose paths all sit under ``~w/<xx>/``:

    ~w/<xx>/wiki-article/<Title>     a cleaned article, front article
    ~w/<xx>/wiki-image/<hash>.webp   its pictures, named by content as the base names them
    ~w/<xx>/wiki-geo-index.json      {title: [lat, lon, type, qid, desc]}, as the base's
    ~w/<xx>/wikidata-text.json       {qid: [label, description]} in xx
    ~w/<xx>/pack.json                lang, region, base_uuid, articles, images, source, built

**Which articles.** The base's Q-IDs (its ``wikidata/`` chunks and its
search records' ``q``) that have an article in xx Wikipedia. A Kiwix
Wikipedia ZIM does not say which item an article is about, so Q-ID ->
title comes from xx Wikipedia's SQL dumps: ``page_props`` (wikibase_item ->
page id) and ``page`` (page id -> title, namespace 0, not a redirect),
downloaded once into the cache and reduced to a TSV per language. The
article text and its pictures come from the local Kiwix Wikipedia ZIM: a
title the ZIM holds as a redirect is followed; a title it lacks (renamed
between the ZIM and the dump) is counted and listed. Several Q-IDs that
lead to one article give it one entry.

**Where each article sits on the map.** The base's search record of the
Q-ID (its point and type) when it has one; otherwise the article's own
coordinates (``<meta name="geo.position">``, which mwoffliner writes) inside
the base's bounds, with a type guessed from the Q-ID's Wikidata entry. An
article with neither is left out: the viewer reaches articles only through
the geo-index. ``desc`` is the article's opening sentence (at most 160
characters): the pack's language, where the base's descriptions are the
base's.

**Pages.** Cleaned as the base cleans its own (cloud/wiki_articles.py
clean_article_html), with the back-to-map bar three levels up
(``../../../index.html``), pictures linked ``../wiki-image/<hash>``, links to
another article of the pack kept (relative: ``./Other_Title``) and every
other link unwrapped to text.

**wikidata-text.json.** Labels and descriptions in xx of every Q-ID of the
region that has them on Wikidata, article or not, so a popup can name a
place in xx where xx Wikipedia has nothing. From ``wbgetentities``
(labels|descriptions, 50 ids a request, at most one request a second,
maxlag=5, Retry-After honoured), all the matrix's languages in one request,
cached per language as JSON lines; a run that is throttled out writes what
it has and says so.
"""
from __future__ import annotations

import argparse
import array
import datetime
import gzip
import html as _html
import json
import os
import posixpath
import re
import resource
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cloud.wiki_articles import (
    _is_disambiguation,
    article_figures,
    article_url,
    clean_article_html,
)
from streetzim.languages import iso639_3, wiki_code, zim_matches

PREFIX = "~w/{lang}/"
TITLE_MAX_UTF16 = 30        # docs/language-matrix.md: Title <= 30 UTF-16 units
DESCRIPTION_MAX = 80
DESC_MAX = 160              # the geo-index's desc, as the base cuts its own
IMAGE_MODES = ("none", "lead", "all")
# The matrix's map languages (docs/language-matrix-prototype.md, "Bases")
# plus English: wikidata-text is fetched for all of them in one request.
TEXT_LANGS = ("ar", "de", "el", "en", "es", "fa", "fr", "he", "hi", "id", "it", "ja",
              "ko", "nl", "pl", "pt", "ru", "th", "tr", "uk", "vi", "zh")
# Wikidata label languages read for a pack language, first found wins
# (without languagefallback, which would hand back English).
TEXT_VARIANTS = {"zh": ("zh", "zh-hans", "zh-cn", "zh-sg", "zh-my",
                        "zh-hant", "zh-tw", "zh-hk", "zh-mo"),
                 "pt": ("pt", "pt-br")}
DUMPS_URL = "https://dumps.wikimedia.org/{site}/latest/{site}-latest-{table}.sql.gz"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_QID_RE = re.compile(r"Q[1-9][0-9]{0,9}")
# Which search record places a Q-ID several records carry: the settlement
# before its admin area, a peak before a POI on it, a street last.
TYPE_RANK = {"place": 0, "admin": 1, "peak": 2, "water": 3, "park": 4, "airport": 5,
             "poi": 6, "street": 7}


def log_stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def site_of(lang: str) -> str:
    """The Wikimedia database name of xx Wikipedia (``dewiki``)."""
    return wiki_code(lang).replace("-", "_") + "wiki"


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


# ---- the base -----------------------------------------------------------

@dataclass
class Base:
    """What a pack needs from the region's published ZIM."""
    path: str
    uuid: str
    name: str                                   # map-config "name" ("Greece")
    bounds: tuple[float, float, float, float] | None   # W, S, E, N
    illustration: bytes | None
    places: dict[str, tuple[float, float, str]] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)   # Q-ID -> guessed type
    placed_from: str = ""

    @property
    def qids(self) -> list[str]:
        return sorted(set(self.places) | set(self.kinds), key=lambda q: int(q[1:]))

    def inside(self, lat: float, lon: float) -> bool:
        if self.bounds is None:
            return True
        w, s, e, n = self.bounds
        return s <= lat <= n and (w <= lon <= e if w <= e else (lon >= w or lon <= e))


def guess_type(wd: dict[str, Any]) -> str:
    """A search-record type for a Q-ID the base has no record of, from its
    Wikidata chunk entry (``i`` the instance-of label, ``p`` population)."""
    i = str(wd.get("i") or "").lower()
    if any(w in i for w in ("mountain", "peak", "hill", "volcano", "summit")):
        return "peak"
    if any(w in i for w in ("lake", "river", "bay", "sea", "strait", "reservoir",
                            "lagoon", "gulf", "stream", "spring", "waterfall", "cove")):
        return "water"
    if any(w in i for w in ("park", "protected area", "nature reserve", "wilderness",
                            "national forest", "wildlife refuge")):
        return "park"
    if "airport" in i:
        return "airport"
    if wd.get("p") or any(w in i for w in (
            "settlement", "village", "town", "city", "municipal", "community",
            "neighborhood", "neighbourhood", "island", "census-designated", "suburb",
            "hamlet", "quarter", "district", "county", "region", "mahalle")):
        return "place"
    return "poi"


def _content(archive: Any, path: str) -> bytes | None:
    try:
        return bytes(archive.get_entry_by_path(path).get_item().content)
    except KeyError:
        return None


def path_range(archive: Any, prefix: str) -> range:
    """The ids of the content entries whose path starts with `prefix`.

    Content entries are numbered in path order, so two binary searches find
    them: a few dozen directory reads, where walking every entry of a big
    base (California: 825 k) reads its whole directory at random."""
    def first(key: str) -> int:
        lo, hi = 0, archive.entry_count
        while lo < hi:
            mid = (lo + hi) // 2
            if archive._get_entry_by_id(mid).path < key:   # no public iterator
                lo = mid + 1
            else:
                hi = mid
        return lo
    return range(first(prefix), first(prefix + "\U0010ffff"))


# The base's category files that hold records of these types (water-g000.json
# when the base cut a big one into geographic shards); not the POI chips.
_CATEGORY_RE = re.compile(
    r"category-index/(place|admin|peak|water|park|airport)(?:-g[0-9a-f]+)?\.json")


def read_base(path: str | os.PathLike[str], cache_dir: Path | None = None,
              log: Callable[[str], None] = log_stderr, *, full_scan: bool = False) -> Base:
    """The base's identity, bounds, illustration and Q-IDs, with the place
    of every Q-ID a record of the base has.

    Places come from the base's category files (places, admin areas,
    peaks, water, parks, airports: tens of MB) and its own wiki-geo-index (every Q-ID
    with an article in the base's language, POIs and streets included).
    `full_scan` reads every search record instead: all of them, at the
    cost of the whole search index (California: 7.8 GB, read at idle I/O
    priority on a busy disk, hours). Either is cached by the base's UUID;
    a cached full scan serves both."""
    from libzim.reader import Archive
    a = Archive(Path(path))
    uuid = str(a.uuid)
    cfg = json.loads(_content(a, "map-config.json") or b"{}")
    b = cfg.get("bounds")
    bounds = (float(b[0]), float(b[1]), float(b[2]), float(b[3])) if b and len(b) == 4 else None
    try:
        illustration = bytes(a.get_metadata("Illustration_48x48@1"))
    except (KeyError, RuntimeError):
        illustration = None
    base = Base(str(path), uuid, str(cfg.get("name") or Path(path).stem), bounds, illustration)
    full = cache_dir / "bases" / f"{uuid}.json.gz" if cache_dir else None
    light = cache_dir / "bases" / f"{uuid}.light2.json.gz" if cache_dir else None
    for cached, how in ((full, "search records"), (None if full_scan else light,
                                                    "category files + geo-index")):
        if cached is not None and cached.exists():
            with gzip.open(cached, "rt", encoding="utf-8") as f:
                got = json.load(f)
            base.places = {q: (v[0], v[1], v[2]) for q, v in got["places"].items()}
            base.kinds = got["kinds"]
            base.placed_from = how
            log(f"  base: {len(base.places)} placed + {len(base.kinds)} other Q-IDs "
                f"(from its {how}; cached, {cached.name})")
            return base
    t0 = time.time()
    rank: dict[str, int] = {}
    files = records = 0

    def take(r: Any) -> None:
        nonlocal records
        records += 1
        q = r.get("q") if isinstance(r, dict) else None
        if not q or not _QID_RE.fullmatch(q):
            return
        t = str(r.get("t") or "")
        rk = TYPE_RANK.get(t, len(TYPE_RANK))
        if q not in rank or rk < rank[q]:
            try:
                base.places[q] = (round(float(r["a"]), 5), round(float(r["o"]), 5), t)
            except (KeyError, TypeError, ValueError):
                return
            rank[q] = rk

    if full_scan:
        ids = [*path_range(a, "search-data/"), *path_range(a, "wikidata/")]
    else:
        ids = [*path_range(a, "category-index/"), *path_range(a, "wikidata/")]
    for i in ids:
        e = a._get_entry_by_id(i)
        p = e.path
        if p.startswith("wikidata/") and p != "wikidata/manifest.json":
            data = json.loads(bytes(e.get_item().content))
            for q, wd in data.items():
                if _QID_RE.fullmatch(q) and isinstance(wd, dict):
                    base.kinds.setdefault(q, guess_type(wd))
        elif (p.startswith("search-data/") and p.endswith(".json")) or _CATEGORY_RE.fullmatch(p):
            data = json.loads(bytes(e.get_item().content))
            if not isinstance(data, list):      # search-data/manifest.json
                continue
            files += 1
            for r in data:
                take(r)
    if not full_scan:
        # The base's own geo-index: [lat, lon, type, qid, desc] rows (older
        # bases: no Q-ID), for the Q-IDs the category files do not place.
        geo = json.loads(_content(a, "wiki-geo-index.json") or b"{}")
        for row in geo.values() if isinstance(geo, dict) else ():
            if isinstance(row, list) and len(row) >= 4 and isinstance(row[3], str):
                take({"q": row[3], "a": row[0], "o": row[1], "t": row[2]})
    for q in base.places:
        base.kinds.pop(q, None)
    base.placed_from = "search records" if full_scan else "category files + geo-index"
    log(f"  base: {files} {'search chunks' if full_scan else 'category files'} and its "
        f"geo-index, {records:,} records: {len(base.places)} placed Q-IDs + "
        f"{len(base.kinds)} more in wikidata/ ({time.time() - t0:.0f}s)")
    cached = full if full_scan else light
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_name(cached.name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"base": os.path.basename(str(path)), "places": base.places,
                       "kinds": base.kinds}, f)
        os.replace(tmp, cached)
    return base


# ---- Q-ID -> title, from xx Wikipedia's dumps ------------------------------

def _download(url: str, dest: Path, log: Callable[[str], None]) -> None:
    """`url` to `dest` through dest.part (resumed when present)."""
    from cloud.wikimedia_http import user_agent
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": user_agent("wiki-pack")}
    if have:
        headers["Range"] = f"bytes={have}-"
    t0 = time.time()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        mode = "ab" if have and resp.status == 206 else "wb"
        with open(part, mode) as out:
            shutil.copyfileobj(resp, out, 1 << 20)
    os.replace(part, dest)
    log(f"  downloaded {dest.name}: {dest.stat().st_size / 1e6:.0f} MB "
        f"in {time.time() - t0:.0f}s")


def fetch_dump(lang: str, table: str, cache_dir: Path, *, offline: bool = False,
               log: Callable[[str], None] = log_stderr) -> Path:
    """xx Wikipedia's latest ``table`` SQL dump, downloaded once."""
    site = site_of(lang)
    dest = cache_dir / "dumps" / f"{site}-latest-{table}.sql.gz"
    if not dest.exists():
        if offline:
            raise FileNotFoundError(f"{dest} (offline: download it first)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        _download(DUMPS_URL.format(site=site, table=table), dest, log)
    return dest


def _gunzip_lines(path: Path) -> Iterator[bytes]:
    """The lines of a .gz, through pigz when the host has it (3-4x faster)."""
    pigz = shutil.which("pigz")
    if pigz:
        proc = subprocess.Popen([pigz, "-dc", str(path)], stdout=subprocess.PIPE,
                                bufsize=1 << 20)
        assert proc.stdout is not None
        try:
            yield from proc.stdout
        finally:
            proc.stdout.close()
            if proc.wait() not in (0, -13):   # -13: we stopped reading (SIGPIPE)
                raise RuntimeError(f"pigz -dc {path} failed")
    else:
        with gzip.open(path, "rb") as f:
            yield from f


# A row starts a line (one row a line, as the dumps are written since 2025),
# follows "VALUES " or another row's "),". Strings escape ' as \'.
_ROW_START = rb"(?:^|\),|VALUES )\("
_PROPS_ROW = re.compile(_ROW_START + rb"(\d+),'wikibase_item','Q([1-9]\d{0,9})'")
_PAGE_ROW = re.compile(_ROW_START + rb"(\d+),0,'((?:[^'\\]|\\.)*)',([01]),")
_SQL_ESCAPES = {b"0": b"\0", b"n": b"\n", b"r": b"\r", b"t": b"\t", b"Z": b"\x1a"}


def _sql_unescape(s: bytes) -> bytes:
    return re.sub(rb"\\(.)", lambda m: _SQL_ESCAPES.get(m.group(1), m.group(1)), s)


def build_title_map(lang: str, cache_dir: Path, *, offline: bool = False,
                    log: Callable[[str], None] = log_stderr) -> Path:
    """``<cache>/titles/<site>-qid-title.tsv``: "Q<n>\\t<Title>" for every
    article (namespace 0, not a redirect) of xx Wikipedia that has a
    Wikidata item. Rebuilt when the dumps it came from change."""
    props = fetch_dump(lang, "page_props", cache_dir, offline=offline, log=log)
    page = fetch_dump(lang, "page", cache_dir, offline=offline, log=log)
    out = cache_dir / "titles" / f"{site_of(lang)}-qid-title.tsv"
    meta_path = out.with_suffix(".json")
    stamp = {"page_props": [props.stat().st_size, int(props.stat().st_mtime)],
             "page": [page.stat().st_size, int(page.stat().st_mtime)]}
    if out.exists() and meta_path.exists():
        try:
            if json.loads(meta_path.read_text()).get("dumps") == stamp:
                return out
        except ValueError:
            pass
    t0 = time.time()
    pids, qnums = array.array("q"), array.array("q")
    for line in _gunzip_lines(props):
        if b"wikibase_item" not in line:
            continue
        for m in _PROPS_ROW.finditer(line):
            pids.append(int(m.group(1)))
            qnums.append(int(m.group(2)))
    import numpy as np
    pid_arr = np.frombuffer(pids, dtype=np.int64)
    q_arr = np.frombuffer(qnums, dtype=np.int64)
    order = np.argsort(pid_arr, kind="stable")
    pid_arr, q_arr = pid_arr[order], q_arr[order]
    del pids, qnums, order
    t1 = time.time()
    rows = articles = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    pending_pid: list[int] = []
    pending_title: list[bytes] = []

    def flush(f: Any) -> int:
        if not pending_pid:
            return 0
        want = np.asarray(pending_pid, dtype=np.int64)
        at = np.searchsorted(pid_arr, want)
        at[at >= len(pid_arr)] = 0
        hit = pid_arr[at] == want
        n = 0
        for k in np.nonzero(hit)[0]:
            f.write(b"Q%d\t%s\n" % (int(q_arr[at[k]]), pending_title[k]))
            n += 1
        pending_pid.clear()
        pending_title.clear()
        return n

    with open(tmp, "wb") as f:
        for line in _gunzip_lines(page):
            for m in _PAGE_ROW.finditer(line):
                rows += 1
                if m.group(3) == b"1":
                    continue
                title = _sql_unescape(m.group(2))
                if b"\t" in title or b"\n" in title:
                    continue
                pending_pid.append(int(m.group(1)))
                pending_title.append(title)
            if len(pending_pid) >= 200_000:
                articles += flush(f)
        articles += flush(f)
    os.replace(tmp, out)
    meta_path.write_text(json.dumps({"dumps": stamp, "items": int(len(pid_arr)),
                                     "articles": articles}))
    log(f"  {out.name}: {articles:,} articles with an item ({len(pid_arr):,} wikibase_item "
        f"props in {t1 - t0:.0f}s, {rows:,} namespace-0 pages in {time.time() - t1:.0f}s)")
    return out


def load_titles(lang: str, qids: Iterable[str], cache_dir: Path, *, offline: bool = False,
                log: Callable[[str], None] = log_stderr) -> dict[str, str]:
    """{Q-ID: underscored title} of the `qids` xx Wikipedia has an article on."""
    want = set(qids)
    tsv = build_title_map(lang, cache_dir, offline=offline, log=log)
    out: dict[str, str] = {}
    with open(tsv, encoding="utf-8", errors="replace") as f:
        for line in f:
            q, _, title = line.rstrip("\n").partition("\t")
            if q in want and q not in out:
                out[q] = title
    return out


def articles_now(lang: str, titles: Iterable[str], cache_dir: Path, *, offline: bool = False,
                 log: Callable[[str], None] = log_stderr) -> set[str]:
    """Which of `titles` are articles of xx Wikipedia now (with an item)."""
    want = set(titles)
    if not want:
        return set()
    tsv = build_title_map(lang, cache_dir, offline=offline, log=log)
    out: set[str] = set()
    with open(tsv, encoding="utf-8", errors="replace") as f:
        for line in f:
            t = line.rstrip("\n").partition("\t")[2]
            if t in want:
                out.add(t)
    return out


_REDIRECT_ROW = re.compile(_ROW_START + rb"(\d+),0,'((?:[^'\\]|\\.)*)'")
_ANY_PAGE_ROW = re.compile(_ROW_START + rb"(\d+),(-?\d+),'((?:[^'\\]|\\.)*)',([01]),")


def renamed_titles(lang: str, targets: Iterable[str], cache_dir: Path, *, offline: bool = False,
                   log: Callable[[str], None] = log_stderr) -> dict[str, list[str]]:
    """{title: [the titles that redirect to it now]} for `targets`, from the
    ``redirect`` and ``page`` dumps: a source ZIM older than the dumps may
    hold an article under its old name (``Omonia-Platz``, now ``Omonia``).
    Answers are cached per language, so each title is looked up once."""
    targets = sorted(set(targets))
    if not targets:
        return {}
    page = fetch_dump(lang, "page", cache_dir, offline=offline, log=log)
    redirect = fetch_dump(lang, "redirect", cache_dir, offline=offline, log=log)
    path = cache_dir / "titles" / f"{site_of(lang)}-renamed.json"
    stamp = [redirect.stat().st_size, int(redirect.stat().st_mtime),
             page.stat().st_size, int(page.stat().st_mtime)]
    known: dict[str, list[str]] = {}
    try:
        got = json.loads(path.read_text())
        if got.get("dumps") == stamp:
            known = got["titles"]
    except (OSError, ValueError, KeyError):
        pass
    todo = {t.encode("utf-8") for t in targets if t not in known}
    if todo:
        t0 = time.time()
        by_pid: dict[int, bytes] = {}
        for line in _gunzip_lines(redirect):
            for m in _REDIRECT_ROW.finditer(line):
                to = _sql_unescape(m.group(2))
                if to in todo:
                    by_pid[int(m.group(1))] = to
        found: dict[str, list[str]] = {t.decode("utf-8"): [] for t in todo}
        if by_pid:
            for line in _gunzip_lines(page):
                for m in _ANY_PAGE_ROW.finditer(line):
                    pid = int(m.group(1))
                    if pid in by_pid and m.group(2) == b"0" and m.group(4) == b"1":
                        found[by_pid[pid].decode("utf-8")].append(
                            _sql_unescape(m.group(3)).decode("utf-8", "replace"))
        known.update({k: sorted(v) for k, v in found.items()})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"dumps": stamp, "titles": known}, ensure_ascii=False))
        os.replace(tmp, path)
        log(f"  {len(todo)} titles the source lacks: {len(by_pid)} redirects to them "
            f"in the dumps ({time.time() - t0:.0f}s)")
    return {t: known.get(t, []) for t in targets}


# ---- wikidata-text: labels and descriptions in xx --------------------------

def _text_cache(cache_dir: Path, lang: str) -> Path:
    return cache_dir / "wikidata-text" / f"{lang}.jsonl"


def _load_text_cache(path: Path) -> dict[str, tuple[str, str]]:
    """{Q-ID: (label, description)}; ("", "") is an answer with neither. A
    torn last line (a run stopped mid-write) is skipped."""
    out: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                q, label, desc = json.loads(line)
            except ValueError:
                continue
            out[q] = (label, desc)
    return out


def _pick(texts: dict[str, Any], lang: str) -> str:
    for code in TEXT_VARIANTS.get(lang, (lang,)):
        v = texts.get(code)
        if isinstance(v, dict) and v.get("value"):
            return str(v["value"])
    return ""


Fetcher = Callable[[list[str], list[str]], dict[str, Any]]


def api_fetcher(log: Callable[[str], None] = log_stderr) -> Fetcher:
    """wbgetentities for <=50 ids and the given label languages, at most a
    request a second, maxlag=5, Retry-After honoured (cloud/wikimedia_http).
    Returns the ``entities`` object. Raises TransientError when the API
    did not answer about the ids (throttled, failing, refusing the client)
    and ValueError for an error about them."""
    from cloud.wikimedia_http import TransientError, get_json, polite_pacer, stop_error, user_agent
    pacer = polite_pacer(gap=0.2, max_per_min=60)
    ua = user_agent("wiki-pack")

    def fetch(ids: list[str], codes: list[str]) -> dict[str, Any]:
        params = urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(ids),
            "props": "labels|descriptions", "languages": "|".join(codes),
            "format": "json", "maxlag": "5"})
        try:
            data = get_json(f"{WIKIDATA_API}?{params}", user_agent=ua, pacer=pacer,
                            timeout=60, throttle_retries=1000, log=log)
        except urllib.error.HTTPError as e:
            raise stop_error(e) from e      # refused (403, 404, ...): stop asking
        if not isinstance(data, dict):
            raise TransientError("wbgetentities: not a JSON object")
        err = data.get("error")
        if err is not None:
            code = str(err.get("code", "")) if isinstance(err, dict) else ""
            if code in ("maxlag", "ratelimited", "readonly") or code.startswith("internal_api_error"):
                raise TransientError(f"wbgetentities: {code}")
            raise ValueError(f"wbgetentities: {code or '?'}")   # about the ids
        ents = data.get("entities")
        if not isinstance(ents, dict):
            raise TransientError("wbgetentities: no entities")
        return ents
    return fetch


def wikidata_text(qids: Iterable[str], lang: str, cache_dir: Path, *,
                  fetch: Fetcher | None = None, langs: Iterable[str] = TEXT_LANGS,
                  log: Callable[[str], None] = log_stderr) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """({Q-ID: [label, description]} in `lang` for the `qids` that have
    either, stats). Q-IDs the cache of `lang` lacks are asked for in every
    language of `langs` at once, and every language's cache is appended to
    (JSON lines, so a stopped run keeps what it got). `fetch` None: the
    cache only."""
    from cloud.wikimedia_http import TransientError
    qids = sorted(set(qids), key=lambda q: int(q[1:]))
    langs = list(dict.fromkeys([lang, *langs]))
    caches = {lg: _load_text_cache(_text_cache(cache_dir, lg)) for lg in langs}
    missing = [q for q in qids if q not in caches[lang]]
    stats: dict[str, Any] = {"qids": len(qids), "asked": len(missing), "requests": 0,
                             "complete": True, "seconds": 0.0, "refused": []}
    codes = sorted({c for lg in langs for c in TEXT_VARIANTS.get(lg, (lg,))})
    t0 = time.time()
    if missing and fetch is None:
        stats["complete"] = False
    elif missing and fetch is not None:
        log(f"  wikidata-text: asking Wikidata for {len(missing)} Q-IDs "
            f"({(len(missing) + 49) // 50} requests, {len(langs)} languages)")
        _text_cache(cache_dir, lang).parent.mkdir(parents=True, exist_ok=True)
        files = {lg: open(_text_cache(cache_dir, lg), "a", encoding="utf-8") for lg in langs}
        try:
            todo = [missing[i:i + 50] for i in range(0, len(missing), 50)]
            while todo:
                batch = todo.pop(0)
                try:
                    ents = fetch(batch, codes)
                    stats["requests"] += 1
                except TransientError as e:
                    log(f"  wikidata-text: stopped after {stats['requests']} requests "
                        f"({e.reason}); {sum(len(b) for b in todo) + len(batch)} Q-IDs "
                        "not fetched")
                    stats["complete"] = False
                    break
                except urllib.error.HTTPError as e:
                    log(f"  wikidata-text: stopped: HTTP {e.code}")
                    stats["complete"] = False
                    break
                except ValueError as e:
                    stats["requests"] += 1
                    if len(batch) > 1:      # a refused batch: halve to find the bad id
                        todo[:0] = [batch[:len(batch) // 2], batch[len(batch) // 2:]]
                        continue
                    # Not cached: the refusal may have had nothing to do with it.
                    log(f"  wikidata-text: {batch[0]} refused ({e})")
                    stats["refused"].append(batch[0])
                    continue
                got: dict[str, dict[str, Any]] = {}
                for key, ent in ents.items():
                    if not isinstance(ent, dict):
                        continue
                    frm = (ent.get("redirects") or {}).get("from")
                    got[frm if frm in batch else key] = ent
                for q in batch:
                    ent = got.get(q, {})
                    for lg in langs:
                        row = (_pick(ent.get("labels") or {}, lg),
                               _pick(ent.get("descriptions") or {}, lg))
                        caches[lg][q] = row
                        files[lg].write(json.dumps([q, *row], ensure_ascii=False) + "\n")
                for f in files.values():
                    f.flush()
                if stats["requests"] % 100 == 0:
                    log(f"    ... {stats['requests']} requests, {time.time() - t0:.0f}s")
        finally:
            for f in files.values():
                f.close()
    stats["seconds"] = round(time.time() - t0, 1)
    out: dict[str, list[str]] = {}
    for q in qids:
        label, desc = caches[lang].get(q, ("", ""))
        if label or desc:
            out[q] = [label, desc]
    stats["labels"] = sum(1 for v in out.values() if v[0])
    stats["descriptions"] = sum(1 for v in out.values() if v[1])
    stats["missing"] = sum(1 for q in qids if q not in caches[lang])
    return out, stats


# ---- articles ---------------------------------------------------------------

_GEO_META_RE = re.compile(
    r'<meta\s+name="geo\.position"\s+content="\s*(-?\d+(?:\.\d+)?)\s*[;,]\s*(-?\d+(?:\.\d+)?)\s*"',
    re.I)
_EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//|^(?:mailto|geo|tel|data|javascript|"
                          r"irc|ircs|news|urn|sms|magnet|ftp|https?):", re.I)
# Kept unescaped in a link, as mwoffliner writes them ("P%C3%A9riph%C3%A9rie_(Gr%C3%A8ce)").
_URL_SAFE = "/()'!*,;:@&=+$-._~"


def geo_position(raw: str) -> tuple[float, float] | None:
    """The article's coordinates, from mwoffliner's geo.position meta."""
    m = _GEO_META_RE.search(raw[:20000])
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
        return round(lat, 5), round(lon, 5)
    return None


# A sentence ends at "!", "?" or a full stop after a word of 4+ letters
# (not "bzw.", "St.", "v. Chr."), before a space and a capital, or at a CJK
# full stop.
_SENTENCE_END_RE = re.compile(r"(?:(?<=[^\W\d_]{4})\.|[!?])(?=\s+[\"'«„“(]?[^\W\d_](?<![a-z]))"
                              r"|[。！？।]")


def excerpt(page_html: str, limit: int = DESC_MAX) -> str:
    """The opening sentence of a cleaned page (its first paragraph after the
    heading), plain text, at most `limit` characters."""
    body = page_html.split("</h1>", 1)[-1]
    m = re.search(r"<p>(.*?)</p>", body, re.S)
    if not m:
        return ""
    text = " ".join(_html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).split())
    for s in _SENTENCE_END_RE.finditer(text):
        if s.end() >= (10 if s.group() in "。！？।" else 40):
            if s.end() <= limit:
                return text[:s.end()]
            break
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.6 else cut).rstrip(" ,;:") + "…"


class SourceZim:
    """The local Kiwix Wikipedia ZIM: entries by title, redirects followed."""

    def __init__(self, path: str | os.PathLike[str]):
        from libzim.reader import Archive
        self.path = str(path)
        self.archive = Archive(Path(path))
        self.prefix = "" if self.archive.has_new_namespace_scheme else "A/"
        self._final: dict[str, tuple[str, str] | None] = {}

    def warm(self, log: Callable[[str], None] = log_stderr) -> None:
        """Read the ZIM's directory (its dirents and pointer lists, together
        at the end of the file) once, in order, so the page cache holds it.

        Every title, link and picture is looked up by a binary search over
        the dirents: hundreds of thousands of random 4 KiB reads on a
        Wikipedia ZIM (de: 9.7 M entries, 0.8 GB of directory). On a busy
        hard disk those reads, not the work, set the pace; one sequential
        read is far kinder to it."""
        import struct
        t0 = time.time()
        with open(self.path, "rb") as f:
            head = f.read(80)
            if len(head) < 80 or head[:4] != b"ZIM\x04":
                return
            entries, clusters, path_ptr, _title_ptr, cluster_ptr = struct.unpack_from("<IIQQQ", head, 24)
            f.seek(path_ptr)
            ptrs = f.read(entries * 8)
            if len(ptrs) < entries * 8 or not entries:
                return
            import numpy as np
            start = int(np.frombuffer(ptrs, dtype="<u8").min())
            end = max(path_ptr + entries * 8, cluster_ptr + clusters * 8)
            if not 0 < start < end or end - start > 8 << 30:
                return
            f.seek(start)
            left = end - start
            while left > 0:
                got = f.read(min(left, 8 << 20))
                if not got:
                    break
                left -= len(got)
        log(f"  read the source's directory ({(end - start) / 1e6:.0f} MB) in "
            f"{time.time() - t0:.0f}s")

    @property
    def language(self) -> str:
        try:
            return bytes(self.archive.get_metadata("Language")).decode()
        except (KeyError, RuntimeError):
            return ""

    def final(self, path: str) -> tuple[str, str] | None:
        """(path, title) of the article an entry path leads to, redirects
        followed; None when the ZIM has no such entry. Cached."""
        if path in self._final:
            return self._final[path]
        out: tuple[str, str] | None = None
        try:
            e = self.archive.get_entry_by_path(path)
            for _ in range(6):
                if not e.is_redirect:
                    break
                e = e.get_redirect_entry()
            if not e.is_redirect:
                out = (e.path, e.title)
        except (KeyError, RuntimeError):
            out = None
        if len(self._final) < 2_000_000:
            self._final[path] = out
        return out

    def article(self, title_us: str) -> tuple[str, str] | None:
        """(path, title) of the article a title (spaces or underscores) opens."""
        got = self.final(self.prefix + title_us)
        if got is None and "_" in title_us:
            got = self.final(self.prefix + title_us.replace("_", " "))
        return got

    def is_article(self, title_us: str) -> tuple[str, str] | None:
        """(path, title) when the ZIM holds `title_us` as an article (not a
        redirect)."""
        for p in (self.prefix + title_us, self.prefix + title_us.replace("_", " ")):
            try:
                e = self.archive.get_entry_by_path(p)
            except (KeyError, RuntimeError):
                continue
            return None if e.is_redirect else (e.path, e.title)
        return None

    def html(self, path: str) -> str:
        return bytes(self.archive.get_entry_by_path(path).get_item().content).decode(
            "utf-8", "replace")

    def image(self, src: str, max_bytes: int | None = None) -> tuple[bytes, str] | None:
        """Bytes and mimetype of an <img src> (as cloud.wiki_articles reads it)."""
        p = urllib.parse.unquote(src.lstrip("./"))
        for cand in (p, "I/" + p):
            try:
                it = self.archive.get_entry_by_path(cand).get_item()
            except (KeyError, RuntimeError):
                continue
            if max_bytes is not None and it.size > max_bytes:
                return None
            return bytes(it.content), it.mimetype
        return None


def stored_title(source_path: str, prefix: str) -> str:
    """The pack's name for an article: its source path without "A/"."""
    return source_path[len(prefix):] if prefix and source_path.startswith(prefix) else source_path


def relative_link(from_title: str, to_title: str) -> str:
    """The href from wiki-article/<from_title> to wiki-article/<to_title>:
    "./To" (or "../To" from a title with a slash), percent-encoded."""
    # Up to wiki-article/ and down again: posixpath.relpath would make a
    # link to an ancestor title ("Foo/Bar" -> "Foo") "./.", a folder.
    rel = ("../" * from_title.count("/") or "./") + to_title
    return urllib.parse.quote(rel, safe=_URL_SAFE)


def link_rewriter(src: SourceZim, source_path: str, title_us: str, pack: dict[str, str],
                  seen: dict[str, str] | None = None) -> Callable[[str], str | None]:
    """link_href for clean_article_html: a link to an article of this pack
    (`pack`: source path -> stored title), redirects followed, becomes a
    relative link to it; any other link None (unwrapped to text). `seen`
    collects each URL written -> the stored title it leads to."""
    here = posixpath.dirname(source_path)

    def href(raw: str) -> str | None:
        if not raw or raw.startswith("#") or raw.startswith("/") or _EXTERNAL_RE.match(raw):
            return None
        p = raw.split("#", 1)[0].split("?", 1)[0]
        if not p:
            return None
        p = urllib.parse.unquote(p)
        target = posixpath.normpath(posixpath.join(here, p))
        if target.startswith("../") or target == "..":
            return None
        got = src.final(target)
        to = pack.get(got[0]) if got else None
        if not to:
            return None
        url = relative_link(title_us, to)
        if seen is not None:
            seen[url] = to
        return url
    return href


@dataclass
class Candidate:
    qid: str
    source_path: str        # the article's path in the source ZIM
    title: str              # its display title
    title_us: str           # its name in the pack (wiki-article/<title_us>)
    others: list[str] = field(default_factory=list)   # more Q-IDs that lead here


@dataclass
class PackResult:
    path: str
    stats: dict[str, Any]


def _rank(base: Base, q: str) -> tuple[int, int, int]:
    """Order in which Q-IDs claim an article: placed by the base first."""
    if q in base.places:
        return (0, TYPE_RANK.get(base.places[q][2], len(TYPE_RANK)), int(q[1:]))
    return (1, TYPE_RANK.get(base.kinds.get(q, ""), len(TYPE_RANK)), int(q[1:]))


def plan(base: Base, titles: dict[str, str], src: SourceZim, stats: dict[str, Any],
         renamed: Callable[[list[str]], dict[str, list[str]]] | None = None,
         current: Callable[[set[str]], set[str]] | None = None) -> list[Candidate]:
    """One candidate per article the Q-IDs lead to, in title order.

    A title the source lacks is looked for under the names that redirect to
    it now (`renamed`), as an article. A title the source holds only as a
    redirect is followed unless the article it leads to is another article
    now (`current`: which titles are): the source predates the article, and
    the page it redirected to is about something else (a station split out
    of its line's article)."""
    by_path: dict[str, Candidate] = {}
    found: dict[str, tuple[str, str]] = {}
    missing: list[str] = []
    for q, t in titles.items():
        got = src.article(t)
        if got is None:
            missing.append(q)
        else:
            found[q] = got
    via = {q: stored_title(got[0], src.prefix) for q, got in found.items()
           if stored_title(got[0], src.prefix).replace(" ", "_") != titles[q].replace(" ", "_")}
    elsewhere: list[list[str]] = []
    if via and current is not None:
        now = current(set(via.values()))
        for q, to in sorted(via.items(), key=lambda x: int(x[0][1:])):
            if to in now:
                del found[q]
                elsewhere.append([q, titles[q], to])
    olds = renamed(sorted({titles[q] for q in missing})) if (renamed and missing) else {}
    not_in_zim: list[list[str]] = []
    n_renamed = 0
    for q in missing:
        for old in olds.get(titles[q], []):
            got = src.is_article(old)
            if got is not None:
                found[q] = got
                n_renamed += 1
                break
        else:
            not_in_zim.append([q, titles[q]])
    for q in sorted(found, key=lambda q: _rank(base, q)):
        path, title = found[q]
        if path in by_path:
            by_path[path].others.append(q)
            continue
        by_path[path] = Candidate(q, path, title, stored_title(path, src.prefix))
    stats["found_under_old_title"] = n_renamed
    stats["through_redirect"] = len(via) - len(elsewhere)
    stats["redirect_to_other_article"] = len(elsewhere)
    stats["redirect_to_other_article_list"] = elsewhere
    stats["not_in_zim"] = len(not_in_zim)
    stats["not_in_zim_list"] = sorted(not_in_zim, key=lambda r: int(r[0][1:]))
    stats["shared_article"] = sum(len(c.others) for c in by_path.values())
    return sorted(by_path.values(), key=lambda c: c.title_us)


def peak_rss_mb() -> int:
    self_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return max(self_kb, child_kb) // 1024


def fit(template: str, name: str, limit: int, measure: Callable[[str], int]) -> str:
    """template.format(name=...), the name shortened with "…" to fit."""
    text = template.format(name=name)
    while measure(text) > limit and len(name) > 1:
        name = name[:-2].rstrip() + "…"
        text = template.format(name=name)
    if measure(text) > limit:
        raise ValueError(f"{text!r} is over {limit}")
    return text


class _Entry:
    """An item for cloud.manifest_writer.ManifestCreator (its MapItem duck type)."""

    def __init__(self, path: str, title: str, mimetype: str, data: bytes,
                 front: bool = False, compress: bool = True):
        self._path, self._title, self._mimetype, self._data = path, title, mimetype, data
        self._is_front, self._compress = front, compress
        self._namespace = self._file_path = None


# ---- pages, one candidate at a time (in worker processes) --------------------

@dataclass
class _Job:
    """What a worker needs; set before the pool forks."""
    source: str
    lang: str
    images: str
    image_max_kb: int
    images_per_article: int
    base: Base
    pack_paths: dict[str, str]      # every candidate: source path -> title_us


@dataclass
class _Page:
    qid: str
    title_us: str
    title: str
    html: str
    desc: str
    where: tuple[float, float, str]
    by_article: bool                      # placed by the article's coordinates
    links: dict[str, str]                 # href -> title_us it links to
    images: list[tuple[str, str, bytes]]  # (name, mimetype, bytes) not sent before


_WORKER: dict[str, Any] = {}


def _init_worker(job: _Job) -> None:
    _WORKER.clear()
    _WORKER.update(job=job, src=SourceZim(job.source), sent=set())


def _page(c: Candidate) -> tuple[str, _Page | None]:
    """("page", the cleaned page) or (why it is left out, None)."""
    job: _Job = _WORKER["job"]
    src: SourceZim = _WORKER["src"]
    sent: set[str] = _WORKER["sent"]
    base = job.base
    raw = src.html(c.source_path)
    if _is_disambiguation(raw):
        return "disambiguation", None
    qid, where, by_article = c.qid, None, False
    for q in [c.qid, *c.others]:
        if q in base.places:
            qid, where = q, base.places[q]
            break
    if where is None:
        pos = geo_position(raw)
        if pos is None:
            return "unplaced", None
        if not base.inside(*pos):
            return "outside", None
        where, by_article = (pos[0], pos[1], base.kinds.get(c.qid, "poi")), True
    new_images: list[tuple[str, str, bytes]] = []

    def store_image(name: str, mimetype: str, b: bytes) -> None:
        if name not in sent:
            sent.add(name)
            new_images.append((name, mimetype, b))

    lead_html = gallery_html = ""
    if job.images != "none":
        lead_html, gallery_html = article_figures(
            raw, src, up="../" * (c.title_us.count("/") + 1), images=job.images,
            image_max_kb=job.image_max_kb, max_images=job.images_per_article,
            store_image=store_image)
    links: dict[str, str] = {}
    link_href = link_rewriter(src, c.source_path, c.title_us, job.pack_paths, links)
    page = clean_article_html(
        raw, c.title, article_url(c.title_us, job.lang), lead_html, gallery_html,
        path_title=c.title_us, lang=job.lang, prefix_depth=2, link_href=link_href)
    return "page", _Page(qid, c.title_us, c.title, page, excerpt(page), where, by_article,
                         links, new_images)


_KEPT_LINK_RE = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.S)


def unlink(page: str, hrefs: set[str]) -> str:
    """The page with its kept links to `hrefs` (HTML-escaped as written)
    unwrapped to their text."""
    if not hrefs:
        return page
    return _KEPT_LINK_RE.sub(lambda m: m.group(2) if _html.unescape(m.group(1)) in hrefs
                             else m.group(0), page)


def build_pack(base_path: str | os.PathLike[str], *, lang: str, source: str | os.PathLike[str],
               region: str, output: str | os.PathLike[str], cache_dir: Path,
               images: str = "all", image_max_kb: int = 128, images_per_article: int = 12,
               text: bool = True, fetch: Fetcher | None = None, offline: bool = False,
               limit: int | None = None, jobs: int = 8, workers: int = 4,
               date: str | None = None, warm: bool = True, full_base_scan: bool = False,
               log: Callable[[str], None] = log_stderr) -> PackResult:
    """Build the xx Wikipedia pack of `region` onto the base at `base_path`.
    `jobs` processes read and clean the articles (the source ZIM is read at
    random: on a busy disk the reads, not the CPU, set the pace; `warm`
    first reads its directory in one go, SourceZim.warm); `workers`
    compression threads. `fetch`: the Wikidata API (api_fetcher()
    when None and not `offline`)."""
    import multiprocessing

    from cloud.manifest_writer import ManifestCreator
    if images not in IMAGE_MODES:
        raise ValueError(f"images must be one of {IMAGE_MODES}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", region):
        raise ValueError(f"region id {region!r}: lower-case letters, digits, - and _")
    t0 = time.time()
    lang = lang.strip().lower()
    iso3 = iso639_3(lang)
    prefix = PREFIX.format(lang=lang)
    built = date or datetime.date.today().isoformat()
    src = SourceZim(source)
    if src.language and not zim_matches(lang, src.language):
        raise ValueError(f"{source} is a {src.language} Wikipedia, not {lang} ({iso3})")
    log(f"wiki pack {region} {lang}: base {os.path.basename(str(base_path))}, "
        f"source {os.path.basename(str(source))}")
    if warm:
        src.warm(log)
    base = read_base(base_path, cache_dir, log, full_scan=full_base_scan)
    stats: dict[str, Any] = {"base_placed_from": base.placed_from, "region": region, "lang": lang, "base": os.path.basename(str(base_path)),
                             "source": os.path.basename(str(source)), "base_qids": len(base.qids),
                             "base_placed": len(base.places), "images_mode": images}
    t_titles = time.time()
    titles = load_titles(lang, base.qids, cache_dir, offline=offline, log=log)
    stats["sitelinks"] = len(titles)
    stats["no_sitelink"] = len(base.qids) - len(titles)
    log(f"  {len(titles)} of {len(base.qids)} Q-IDs have a {lang} article (dump)")
    cands = plan(base, titles, src, stats,
                 renamed=lambda ts: renamed_titles(lang, ts, cache_dir, offline=offline, log=log),
                 current=lambda ts: articles_now(lang, ts, cache_dir, offline=offline, log=log))
    stats["seconds_titles"] = round(time.time() - t_titles, 1)
    if limit:
        cands = cands[:limit]
    log(f"  {len(cands)} articles in the source ZIM ({stats['found_under_old_title']} under "
        f"their old title, {stats['through_redirect']} through a redirect); "
        f"{stats['not_in_zim']} titles it lacks, {stats['redirect_to_other_article']} it "
        f"redirects to another article; {stats['shared_article']} Q-IDs share an article")

    title = fit("{name} Wikipedia (" + lang + ")", base.name, TITLE_MAX_UTF16, utf16_len)
    desc = fit("Wikipedia (" + lang + ") articles about places in {name}, for StreetZim",
               base.name, DESCRIPTION_MAX, len)
    text_blob: bytes | None = None
    text_stats: dict[str, Any] = {}
    if text:
        fetcher = fetch if fetch is not None else (None if offline else api_fetcher(log))
        texts, text_stats = wikidata_text(base.qids, lang, cache_dir, fetch=fetcher, log=log)
        text_blob = json.dumps(texts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        text_stats["bytes"] = len(text_blob)
        log(f"  wikidata-text: {len(texts)} of {len(base.qids)} Q-IDs have {lang} text "
            f"({text_stats['labels']} labels, {text_stats['descriptions']} descriptions)"
            + ("" if text_stats["complete"] else
               f"; INCOMPLETE: {text_stats['missing']} Q-IDs never answered"))
    stats["wikidata_text"] = text_stats
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = output.with_name(output.name + ".tmp")
    spool_path = output.with_name(output.name + ".images.tmp")
    for stale in (tmp_out, spool_path):
        if stale.exists():
            stale.unlink()
    # Read, place and clean every candidate, `jobs` at a time. Links may
    # point at any candidate: those to one left out are unwrapped once all
    # are known, so pages wait in memory and pictures in a spool file.
    job = _Job(str(source), lang, images, image_max_kb, images_per_article, base,
               {c.source_path: c.title_us for c in cands})
    t_pages = time.time()
    pages: list[_Page] = []
    spooled: list[tuple[str, str, int, int]] = []     # name, mimetype, offset, size
    image_names: set[str] = set()
    left_out = {"disambiguation": 0, "unplaced": 0, "outside": 0}
    image_bytes = 0
    pool = None
    if jobs > 1 and len(cands) > 1:
        pool = multiprocessing.get_context("fork").Pool(jobs, _init_worker, (job,))
        results: Iterable[tuple[str, _Page | None]] = pool.imap(_page, cands, chunksize=2)
    else:
        _init_worker(job)
        results = map(_page, cands)
    try:
        with open(spool_path, "wb") as spool:
            for k, (why, pg) in enumerate(results, 1):
                if pg is None:
                    left_out[why] += 1
                else:
                    for name, mt, b in pg.images:
                        if name not in image_names:
                            image_names.add(name)
                            spooled.append((name, mt, image_bytes, len(b)))
                            spool.write(b)
                            image_bytes += len(b)
                    pg.images = []
                    pages.append(pg)
                if k % 500 == 0:
                    log(f"    ... {k}/{len(cands)} read, {len(pages)} pages, {len(spooled)} "
                        f"images ({image_bytes / 1e6:.0f} MB), {time.time() - t_pages:.0f}s")
    except BaseException:
        if pool is not None:
            pool.terminate()        # not the queued candidates first
        raise
    finally:
        if pool is not None:
            pool.close()
            pool.join()
    stats.update(disambiguation=left_out["disambiguation"], unplaced=left_out["unplaced"],
                 outside_bounds=left_out["outside"],
                 placed_by_article=sum(1 for p in pages if p.by_article),
                 seconds_pages=round(time.time() - t_pages, 1))
    log(f"  {len(pages)} pages: {left_out['disambiguation']} disambiguation, "
        f"{left_out['unplaced']} without a place, {left_out['outside']} outside the map; "
        f"{stats['placed_by_article']} placed by the article's coordinates "
        f"({stats['seconds_pages']:.0f}s)")

    # The ZIM: the repository's own packer (as the base's --zim-builder
    # manifest), which writes the v1 title listing of the front articles and
    # no Xapian index (libzim's Creator always adds X/title/xapian).
    t_write = time.time()
    kept = {p.title_us for p in pages}
    geo: dict[str, list[Any]] = {}
    article_bytes = links_kept = links_dropped = 0
    creator = ManifestCreator(str(tmp_out))
    creator.config_nbworkers(max(1, workers))
    if pages:
        creator.set_mainpath(prefix + "wiki-article/" + pages[0].title_us)
    with creator:
        with open(spool_path, "rb") as spool:
            for name, mt, off, size in spooled:
                spool.seek(off)
                creator.add_item(_Entry(prefix + "wiki-image/" + name, "", mt, spool.read(size),
                                        compress=False))
        spool_path.unlink()
        for pg in pages:
            gone = {h for h, t in pg.links.items() if t not in kept}
            links_dropped += len(gone)
            links_kept += len(pg.links) - len(gone)
            data = unlink(pg.html, gone).encode("utf-8")
            pg.html = ""
            article_bytes += len(data)
            creator.add_item(_Entry(prefix + "wiki-article/" + pg.title_us, pg.title,
                                    "text/html", data, front=True))
            lat, lon, typ = pg.where
            geo[pg.title_us] = [lat, lon, typ, pg.qid, pg.desc]
        if text_blob is not None:
            creator.add_item(_Entry(prefix + "wikidata-text.json", "", "application/json",
                                    text_blob))
        geo_blob = json.dumps(geo, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        creator.add_item(_Entry(prefix + "wiki-geo-index.json", "", "application/json", geo_blob))
        meta = {"lang": lang, "region": region, "base_uuid": base.uuid, "articles": len(pages),
                "images": len(spooled), "source": os.path.basename(str(source)), "built": built}
        creator.add_item(_Entry(prefix + "pack.json", "", "application/json",
                                   json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8")))
        creator.add_metadata("Name", f"osm_{region}_wiki-{lang}")
        creator.add_metadata("Title", title)
        creator.add_metadata("Description", desc)
        creator.add_metadata("Language", iso3)
        creator.add_metadata("Date", built)
        creator.add_metadata("Creator", "Wikipedia")
        creator.add_metadata("Publisher", "StreetZim")
        creator.add_metadata("Source", f"{wiki_code(lang)}.wikipedia.org")
        creator.add_metadata("Scraper", "streetzim wiki_pack")
        creator.add_metadata("License", "Text: CC BY-SA 4.0 (Wikipedia); images: their own "
                                        "licences on Wikimedia Commons; Wikidata: CC0")
        creator.add_metadata("Tags", "streetzim;wikipedia;_ftindex:no;_videos:no;"
                                     + ("_pictures:yes" if len(spooled) else "_pictures:no"))
        if base.illustration:
            creator.add_illustration(48, base.illustration)
        log(f"  writing {output.name} ...")
    os.replace(tmp_out, output)
    stats.update(articles=len(pages), images=len(spooled), image_bytes=image_bytes,
                 article_bytes=article_bytes, links_kept=links_kept, links_dropped=links_dropped,
                 geo_index_bytes=len(geo_blob), zim_bytes=output.stat().st_size, title=title,
                 description=desc, seconds_write=round(time.time() - t_write, 1),
                 seconds=round(time.time() - t0, 1), peak_rss_mb=peak_rss_mb())
    log(f"  {output.name}: {len(pages)} articles, {len(spooled)} images, "
        f"{stats['zim_bytes'] / 1e6:.1f} MB in {stats['seconds']:.0f}s "
        f"(peak RSS {stats['peak_rss_mb']} MB)")
    return PackResult(str(output), stats)


# ---- the pack gate ------------------------------------------------------------

_BAR_LINK_RE = re.compile(r"^(?:\./)?(?:\.\./)+index\.html$")   # zimru writes "./../"


def find_zimcheck() -> str | None:
    """$ZIMCHECK_BIN, else zimcheck on PATH."""
    explicit = os.environ.get("ZIMCHECK_BIN")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    return shutil.which("zimcheck")


def check_pack(path: str | os.PathLike[str], lang: str,
               zimcheck: str | None = None) -> dict[str, Any]:
    """The pack's gate (docs/language-matrix-prototype.md, "Gates"): the
    checksum (Archive.check), every content path under ~w/<xx>/, the
    metadata limits, pack.json and the geo-index against the articles, no
    Xapian, and zimcheck -A when given. A pack's back-to-map links reach
    the base's index.html, which only the merged file has, so those are the
    one finding zimcheck may report; anything else fails."""
    from libzim.reader import Archive
    a = Archive(Path(path))
    prefix = PREFIX.format(lang=lang)
    problems: list[str] = []
    checksum = bool(a.check())
    if not checksum:
        problems.append("checksum (Archive.check) failed")
    content = [a._get_entry_by_id(i).path for i in range(a.entry_count)]
    stray = [p for p in content if not p.startswith(prefix)]
    if stray:
        problems.append(f"{len(stray)} paths outside {prefix}: {stray[:3]}")
    if any(p.rsplit("/", 1)[-1] == "map-config.json" for p in content):
        problems.append("carries a map-config.json (the merger writes the merged file's)")
    others = {a._get_entry_by_id(i).path for i in range(a.entry_count, a.all_entry_count)}
    if {"fulltext/xapian", "title/xapian"} & others:
        problems.append("has a Xapian index")
    if "listing/titleOrdered/v1" not in others:
        problems.append("no v1 title listing")

    def meta(k: str) -> str:
        try:
            return bytes(a.get_metadata(k)).decode("utf-8")
        except (KeyError, RuntimeError):
            return ""
    title, desc = meta("Title"), meta("Description")
    if not title or utf16_len(title) > TITLE_MAX_UTF16:
        problems.append(f"Title {title!r}: {utf16_len(title)} UTF-16 units")
    if not desc or len(desc) > DESCRIPTION_MAX:
        problems.append(f"Description {desc!r}: {len(desc)} characters")
    if meta("Language") != iso639_3(lang):
        problems.append(f"Language {meta('Language')!r}, not {iso639_3(lang)}")
    if not re.fullmatch(r"osm_[a-z0-9_-]+_wiki-" + re.escape(lang), meta("Name")):
        problems.append(f"Name {meta('Name')!r}")
    for k in ("Date", "Creator", "Publisher"):
        if not meta(k):
            problems.append(f"no {k}")
    try:
        png = bytes(a.get_metadata("Illustration_48x48@1"))
        if png[:8] != b"\x89PNG\r\n\x1a\n" or png[16:24] != (48).to_bytes(4, "big") * 2:
            problems.append("Illustration_48x48@1 is not a 48x48 PNG")
    except (KeyError, RuntimeError):
        problems.append("no Illustration_48x48@1")
    articles = {p[len(prefix + "wiki-article/"):] for p in content
                if p.startswith(prefix + "wiki-article/")}
    pack = json.loads(_content(a, prefix + "pack.json") or b"{}")
    geo = json.loads(_content(a, prefix + "wiki-geo-index.json") or b"null")
    if set(pack) != {"lang", "region", "base_uuid", "articles", "images", "source", "built"}:
        problems.append(f"pack.json keys {sorted(pack)}")
    elif pack["articles"] != len(articles) or pack["lang"] != lang:
        problems.append(f"pack.json says {pack['articles']} {pack['lang']} articles; "
                        f"the pack holds {len(articles)}")
    if not isinstance(geo, dict) or set(geo) != articles:
        problems.append("the geo-index does not list exactly the articles")
    elif any(not (isinstance(v, list) and len(v) == 5 and _QID_RE.fullmatch(str(v[3])))
             for v in geo.values()):
        problems.append("a geo-index row is not [lat, lon, type, qid, desc]")
    if a.article_count != len(articles):
        problems.append(f"{a.article_count} front articles, {len(articles)} pages")
    out: dict[str, Any] = {"articles": len(articles), "checksum": checksum}
    if zimcheck:
        res = subprocess.run([zimcheck, "-A", "-J", str(path)], capture_output=True, text=True)
        try:
            report = json.loads(res.stdout)
        except ValueError:
            report = None
        if not isinstance(report, dict):
            problems.append(f"zimcheck gave no JSON (exit {res.returncode})")
            out["zimcheck"] = "error"
        else:
            bar = other = 0
            findings: dict[str, int] = {}     # check -> findings not excused
            for row in report.get("logs") or []:
                if (row.get("check") == "url_internal" and row.get("links")
                        and row.get("normalized_link") == "index.html"
                        and all(_BAR_LINK_RE.match(x) for x in row["links"])):
                    bar += 1
                elif row.get("level") in ("ERROR", "WARNING"):
                    other += 1
                    findings[str(row.get("check"))] = findings.get(str(row.get("check")), 0) + 1
                    if other <= 3:
                        problems.append(f"zimcheck: {row.get('check')}: "
                                        f"{str(row.get('message', ''))[:200]}")
            out["zimcheck_findings"] = findings
            out["zimcheck"] = ("pass" if report.get("status") else
                               f"pass but {bar} back-to-map links to the base's index.html"
                               if not other else f"FAIL ({other} findings)")
    out["ok"] = not problems
    out["problems"] = problems
    return out


# ---- command line -----------------------------------------------------------

def pick_source(source: str, lang: str) -> str:
    """A Wikipedia ZIM, or the newest wikipedia_<xx>_all_maxi_*.zim in a folder
    (else the newest wikipedia_<xx>_*.zim; files still downloading end .part)."""
    if os.path.isdir(source):
        found = (sorted(Path(source).glob(f"wikipedia_{wiki_code(lang)}_all_maxi_*.zim"))
                 or sorted(Path(source).glob(f"wikipedia_{wiki_code(lang)}_*.zim")))
        if not found:
            raise FileNotFoundError(f"no wikipedia_{wiki_code(lang)}_*.zim in {source}")
        return str(max(found, key=lambda p: (p.name.rsplit("_", 1)[-1], p.name)))
    return source


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="build_wiki_pack.py",
        description="Build a Wikipedia pack (~w/<xx>/) for a StreetZim base "
                    "(docs/language-matrix-prototype.md).")
    p.add_argument("base", help="the region's published StreetZim ZIM")
    p.add_argument("--lang", required=True, help="Wikipedia language (ISO 639-1: de, fr, th, ...)")
    p.add_argument("--source", required=True,
                   help="the local Kiwix Wikipedia ZIM in that language, or a folder "
                        "holding wikipedia_<xx>_*.zim (the newest is used)")
    p.add_argument("--region", required=True, help="region id, as in the pack's Name (greece)")
    p.add_argument("-o", "--output", help="the pack to write (default: "
                   "osm_<region>_wiki-<xx>_<date>.zim in the current folder)")
    p.add_argument("--cache", default=None,
                   help="dumps, title maps, base scans and Wikidata text "
                        "(default: <cache root>/wiki_pack_cache)")
    p.add_argument("--images", choices=IMAGE_MODES, default="all",
                   help="pictures per article: none, the lead one, or all (default, "
                        "as the base builds)")
    p.add_argument("--image-max-kb", type=int, default=128)
    p.add_argument("--images-per-article", type=int, default=12)
    p.add_argument("--no-wikidata-text", action="store_true",
                   help="leave out wikidata-text.json")
    p.add_argument("--full-base-scan", action="store_true",
                   help="place Q-IDs by every search record of the base, not only its "
                        "category files and geo-index (slow on a big base)")
    p.add_argument("--offline", action="store_true",
                   help="no network: dumps and Wikidata text from the cache only")
    p.add_argument("--limit", type=int, default=None, help="at most N articles (testing)")
    p.add_argument("--jobs", type=int, default=8,
                   help="processes reading and cleaning articles (default 8: on a busy "
                        "disk the source ZIM's random reads set the pace)")
    p.add_argument("--workers", type=int, default=4, help="libzim compression threads")
    p.add_argument("--stats", help="write the build's statistics here as JSON")
    p.add_argument("--zimcheck", default=None, metavar="BIN",
                   help="zimcheck to gate the pack with (default: $ZIMCHECK_BIN, else "
                        "zimcheck on PATH, else the gate runs without it)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from streetzim.paths import cache_root
    lang = args.lang.strip().lower()
    try:
        iso639_3(lang)
    except ValueError as e:
        print(f"build_wiki_pack: {e}", file=sys.stderr)
        return 2
    cache = Path(args.cache) if args.cache else cache_root() / "wiki_pack_cache"
    source = pick_source(args.source, lang)
    output = args.output or f"osm_{args.region}_wiki-{lang}_{datetime.date.today().isoformat()}.zim"
    res = build_pack(args.base, lang=lang, source=source, region=args.region, output=output,
                     cache_dir=cache, images=args.images, image_max_kb=args.image_max_kb,
                     images_per_article=args.images_per_article,
                     text=not args.no_wikidata_text, offline=args.offline, limit=args.limit,
                     jobs=args.jobs, workers=args.workers,
                     full_base_scan=args.full_base_scan)
    res.stats["check"] = check_pack(res.path, lang, args.zimcheck or find_zimcheck())
    if args.stats:
        Path(args.stats).write_text(json.dumps(res.stats, ensure_ascii=False, indent=1))
    summary = {k: v for k, v in res.stats.items() if not k.endswith("_list")}
    print(json.dumps(summary, ensure_ascii=False))
    if not res.stats["check"]["ok"]:
        print(f"build_wiki_pack: the pack fails its gate: {res.stats['check']['problems']}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
