#!/usr/bin/env python3
"""N-pack merge for the language-matrix prototype (prefix layout only).

    merge_zim.py BASE.zim PACK1.zim [PACK2.zim ...] -o OUT.zim --lang L
                 [--region R] [--title T] [--description D] [--plan-only]

* BASE is the region's published default ZIM. Its bytes [first cluster, end of
  dirents) stay VERBATIM at their original offsets (the visitor's local file
  is read in the browser), and so do its path / cluster pointer tables and v1
  listing, which the recipe references as base ranges.
* Each PACK keeps all its C paths under one prefix `~w/<xx>/` (disjoint,
  sorting after every base path). Packs never override or delete; their M/,
  W/ and X/ entries are dropped (metadata is generated; no pack Xapian).
* Generated (the only non-verbatim bytes): header + MIME list, one
  uncompressed MERGE CLUSTER holding the v1 title listing, M/Counter,
  map-config.json (base's + wikiPacks + language) and the metadata (Name,
  Title, Language, Flavour[, Description]), the pack dirents (cluster and
  MIME renumbered), the pointer entries for them, and the MD5.

* An optional SEARCH PACK (index_pack.py: libzim's own indexes of the base's
  pages and the packs' articles) gives the merged file its X/title/xapian and
  X/fulltext/xapian, which replace the base's (those bytes stay, unused): Kiwix
  searches through them. Its clusters are copied verbatim like a pack's; only
  those two entries of it are carried over.

Layout:
  head | base [cs, d1) verbatim | pack 1..N clusters verbatim | search pack
  clusters verbatim | merge cluster | path ptrs | cluster ptrs | new dirents | md5

The emitter yields bytes or Ref(src, off, len) pieces; merge_zim writes them,
recipe2.py turns the same stream into a streetzim-recipe/2 file, so the two
cannot disagree.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import itertools
import json
import os
import re
import struct
import sys
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from collections.abc import Iterator

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tools.langpack.zimfmt import (  # noqa: E402
    HEADER, NO_U32, NO_U64, REDIRECT_MIME, U32, U64, Dirent, Header, ZimReader)

MERGER_VERSION = "streetzim-merge/2.2"      # 2.2: search packs
MERGE_UUID_NS = uuidlib.UUID("6f1d3c52-2b9e-4f57-9a43-0d6f5e1c7a11")
C, M, W, X = (ord(c) for c in "CMWX")
KEY_V1 = (X, b"listing/titleOrdered/v1")
KEY_V0 = (X, b"listing/titleOrdered/v0")
KEY_COUNTER = (M, b"Counter")
KEY_MAIN = (W, b"mainPage")
KEY_MAPCFG = (C, b"map-config.json")
LISTING_MIME = b"application/octet-stream+zimlisting"
COUNTER_MIME = b"text/plain"
META_MIME = b"text/plain;charset=UTF-8"
CHUNK = 8 << 20
PACK_PREFIX_RE = re.compile(rb"^~w/([a-z]{2,3})/")
ISO3 = {"ar": "ara", "de": "deu", "el": "ell", "en": "eng", "es": "spa", "fa": "fas", "fr": "fra",
        "he": "heb", "hi": "hin", "id": "ind", "it": "ita", "ja": "jpn", "ko": "kor", "nl": "nld",
        "pl": "pol", "pt": "por", "ru": "rus", "th": "tha", "tr": "tur", "uk": "ukr", "vi": "vie",
        "zh": "zho"}


class MergeError(ValueError):
    pass


@dataclass(frozen=True)
class Ref:
    """A verbatim byte range of an input: src 'base' or ('pack', i)."""
    src: str | tuple[str, int]
    off: int
    n: int


def utf16_units(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def peak_rss_mb() -> float:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    return 0.0


def gallop_lower_bound(n, key_at, key, start=0) -> int:
    if start >= n or key_at(start) >= key:
        return start
    lo, step, hi = start, 1, start + 1
    while hi < n and key_at(hi) < key:
        lo, step = hi, step * 2
        hi = lo + step
    hi = min(hi, n)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if key_at(mid) < key:
            lo = mid
        else:
            hi = mid
    return hi


def strip_mime(m: bytes) -> bytes:
    return m.split(b";", 1)[0].strip()


def parse_counter(v: bytes) -> dict[bytes, int]:
    out: dict[bytes, int] = {}
    for part in (v or b"").split(b";"):
        if b"=" in part:
            k, n = part.rsplit(b"=", 1)
            out[strip_mime(k)] = out.get(strip_mime(k), 0) + int(n)
    return out


def cluster_region(z: ZimReader) -> tuple[int, int]:
    h = z.header
    offs = z.cluster_offsets()
    lo, hi = min(offs), max(offs)
    cands = [h.path_ptr_pos, h.cluster_ptr_pos, h.checksum_pos, z.path_ptr(0)]
    if h.title_ptr_pos != NO_U64:
        cands.append(h.title_ptr_pos)
    if any(lo <= c < hi for c in cands):
        raise MergeError(f"{z.path}: a table lies between clusters")
    return lo, min(c for c in cands if c > hi)


def blob_of(z: ZimReader, key) -> bytes | None:
    i = z.find(*key)
    if i is None:
        return None
    d = z.dirent_at(i)
    while d.is_redirect:
        assert d.target is not None
        d = z.dirent_at(d.target)
    assert d.cluster is not None and d.blob is not None, f"{key}: not an item"
    return z.blob_bytes(d.cluster, d.blob)


@dataclass
class Pack:
    path: str
    z: ZimReader
    lang: str
    prefix: bytes
    uuid: bytes
    first_cluster: int = 0                  # merged number of its cluster 0
    mime_map: list[int] = field(default_factory=list)
    region: tuple[int, int] = (0, 0)
    pos: int = 0                            # output offset of its cluster region
    articles: int = 0
    base_uuid: str = ""                     # pack.json base_uuid (hex, no dashes)


@dataclass
class NewEntry:
    key: tuple[int, bytes]
    title_raw: bytes
    param: bytes
    is_redirect: bool
    mime: int = 0
    cluster: int = 0
    blob: int = 0
    front: bool = False
    pack: int | None = None          # index into packs (pack entries)
    pack_index: int | None = None    # entry index inside that pack
    target_ref: int | None = None    # pack-internal target index
    payload: bytes | None = None     # generated content (merge cluster)
    ins: int = 0
    overrides: int | None = None
    target: int = 0

    @property
    def title(self) -> bytes:
        return self.title_raw or self.key[1]

    def size(self) -> int:
        return (12 if self.is_redirect else 16) + len(self.key[1]) + 2 + len(self.title_raw) + len(self.param)

    def encode(self) -> bytes:
        tail = self.key[1] + b"\0" + self.title_raw + b"\0" + self.param
        if self.is_redirect:
            return struct.pack("<HBBII", REDIRECT_MIME, len(self.param), self.key[0], 0, self.target) + tail
        return struct.pack("<HBBIII", self.mime, len(self.param), self.key[0], 0, self.cluster, self.blob) + tail


@dataclass
class Plan:
    base: ZimReader
    packs: list[Pack]
    lang: str
    header: Header = field(default_factory=lambda: Header(*HEADER.unpack(bytes(HEADER.size))))
    mimes: list[bytes] = field(default_factory=list)
    entries: list[NewEntry] = field(default_factory=list)
    A: list[int] = field(default_factory=list)
    R: list[int] = field(default_factory=list)
    override_of: dict[int, int] = field(default_factory=dict)
    pack_front: list[tuple[int, int]] = field(default_factory=list)
    base_v1: tuple[int, int] = (0, 0)
    blobs: list[bytes] = field(default_factory=list)   # merge cluster payloads, blob order
    listing_entries: int = 0
    layout: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    index: Pack | None = None                           # the search pack, if any

    def new_idx_base(self, i: int) -> int:
        return i - bisect.bisect_left(self.R, i) + bisect.bisect_right(self.A, i)

    def new_idx_entry(self, j: int) -> int:
        a = self.A[j]
        return a - bisect.bisect_left(self.R, a) + j


_MD5_CACHE: dict[tuple, str] = {}


def _md5_path(path: str) -> str:
    st = os.stat(path)
    key = (os.path.realpath(path), st.st_size, st.st_mtime_ns, st.st_ino)
    if key not in _MD5_CACHE:
        h = hashlib.md5(usedforsecurity=False)
        with open(path, "rb") as fh:
            while b := fh.read(16 << 20):
                h.update(b)
        _MD5_CACHE[key] = h.hexdigest()
    return _MD5_CACHE[key]


def base_lineage(base_path: str, base_uuid: bytes, check_md5: bool = True) -> set[str]:
    """The base's UUID plus the parents recorded by swap_base_viewer.py in
    <base>.parent.json, so a pack built against the parent fits the swapped
    base (same entries, new viewer). The sidecar counts only when it names
    THIS base: its child_uuid must equal the base's header UUID (and, with
    check_md5, its child_md5 the file's MD5). A sidecar copied beside another
    file, or left from an older build, is ignored (review r2 #2)."""
    out = {base_uuid.hex()}
    side = os.path.realpath(base_path) + ".parent.json"
    if not os.path.exists(side):
        return out
    try:
        d = json.load(open(side))
    except ValueError:
        return out
    if (d.get("child_uuid") or "").replace("-", "").lower() != base_uuid.hex():
        return out
    if check_md5 and d.get("child_md5") != _md5_path(base_path):
        return out
    for u in [d.get("parent_uuid")] + list(d.get("parents", [])):
        if isinstance(u, str) and re.fullmatch(r"[0-9a-f]{32}", u.replace("-", "").lower()):
            out.add(u.replace("-", "").lower())
    return out


def derive_uuid(base_uuid: bytes, pack_uuids: list[bytes], lang: str, meta: dict, mapcfg: bytes) -> bytes:
    """The merged UUID: the input UUIDs, the map language, the merger version
    AND a hash of the generated content (map-config.json and the metadata
    values), so two merges that differ only in a title or a map-config field
    get different UUIDs (review S3)."""
    h = hashlib.sha256(mapcfg)
    for k in sorted(meta):
        h.update(b"\0" + k + b"=" + meta[k])
    src = ":".join([base_uuid.hex()] + sorted(u.hex() for u in pack_uuids) + [lang, MERGER_VERSION, h.hexdigest()])
    return uuidlib.uuid5(MERGE_UUID_NS, src).bytes


SEARCH = "search"                       # the search pack's key among a recipe's packs
INDEX_KEYS = ((X, b"fulltext/xapian"), (X, b"title/xapian"))


def open_index(path: str) -> Pack:
    """A search pack (index_pack.py): its two Xapian indexes, and the base it
    was built for (its sidecar, <path>.json)."""
    z = ZimReader(path)
    for k in INDEX_KEYS:
        i = z.find(*k)
        if i is None or z.dirent_at(i).is_redirect:
            raise MergeError(f"{path}: no {k[1].decode()} (not a search pack)")
    p = Pack(path, z, SEARCH, b"", z.header.uuid)
    try:
        p.base_uuid = str(json.load(open(path + ".json")).get("base_uuid", "")).replace("-", "").lower()
    except (OSError, ValueError):
        p.base_uuid = ""
    return p


def open_pack(path: str) -> Pack:
    z = ZimReader(path)
    prefixes = set()
    for d in z.iter_dirents():
        if d.ns == C:
            m = PACK_PREFIX_RE.match(d.path)
            if not m:
                raise MergeError(f"{path}: C path outside ~w/<xx>/: {d.path[:80]!r}")
            prefixes.add(m.group(0))
    if len(prefixes) != 1:
        raise MergeError(f"{path}: expected one ~w/<xx>/ prefix, found {sorted(prefixes)}")
    prefix = prefixes.pop()
    lang = prefix[3:-1].decode()
    pj = blob_of(z, (C, prefix + b"pack.json"))
    if pj is not None and json.loads(pj).get("lang") not in (None, lang):
        raise MergeError(f"{path}: pack.json lang != {lang}")
    p = Pack(path, z, lang, prefix, z.header.uuid)
    p.base_uuid = (json.loads(pj).get("base_uuid") or "").replace("-", "").lower() if pj is not None else ""
    v1 = z.v1_listing()
    p.articles = v1[1] if v1 else 0
    if pj is not None:
        p.articles = json.loads(pj).get("articles", p.articles)
    return p


def build_plan(base_path: str, pack_paths: list[str], lang: str, *, region: str | None = None,
               title: str | None = None, description: str | None = None,
               language_meta: str = "list", region_langs: list[str] | None = None,
               allow_foreign_packs: bool = False, index_path: str | None = None) -> Plan:
    t0 = time.perf_counter()
    base = ZimReader(base_path)
    bh = base.header
    if bh.minor < 1:
        raise MergeError("base uses the old namespace scheme")
    if b'"wikiPacks"' in (blob_of(base, KEY_MAPCFG) or b""):
        raise MergeError("the base is already a merged file (its map-config.json has wikiPacks); "
                         "merge from the region's published base")
    packs = sorted((open_pack(p) for p in pack_paths), key=lambda p: p.prefix)
    lineage = base_lineage(base_path, bh.uuid, check_md5=True)  # review r3: child_md5 too
    for p in packs:
        if not allow_foreign_packs and p.base_uuid not in lineage:
            raise MergeError(f"{os.path.basename(p.path)}: pack.json base_uuid {p.base_uuid or '(none)'} is not this "
                             f"base ({bh.uuid.hex()}) or a recorded parent; rebuild the pack against this base")
    for a, b in itertools.pairwise(packs):
        if b.prefix.startswith(a.prefix) or a.prefix == b.prefix:
            raise MergeError(f"pack prefixes not disjoint: {a.prefix} {b.prefix}")
    index = open_index(index_path) if index_path else None
    if index is not None and not allow_foreign_packs and index.base_uuid not in lineage:
        raise MergeError(f"{os.path.basename(index.path)}: built for base {index.base_uuid or '(unknown)'}, "
                         f"not this one ({bh.uuid.hex()})")
    Nb = bh.entry_count
    cache: dict[int, Dirent] = {}

    def bd(i: int) -> Dirent:
        d = cache.get(i)
        if d is None:
            if len(cache) > 200_000:
                cache.clear()
            d = cache[i] = base.dirent_at(i)
        return d

    key_at = lambda i: bd(i).key  # noqa: E731
    nC = gallop_lower_bound(Nb, key_at, (C + 1, b""))
    last_c = bd(nC - 1).path if nC else b""
    if packs and not last_c < packs[0].prefix:
        raise MergeError(f"base C path {last_c[:60]!r} sorts at/after pack prefix {packs[0].prefix!r}")
    base_v1 = base.v1_listing()
    if base_v1 is None or base_v1[0] < 0:
        raise MergeError("base needs a raw (uncompressed) X/listing/titleOrdered/v1")

    # ---- MIME list: base list first (base dirents keep their index)
    mimes = list(base.mimes)
    midx = {m: i for i, m in enumerate(mimes)}

    def intern(m: bytes) -> int:
        if m not in midx:
            midx[m] = len(mimes)
            mimes.append(m)
        return midx[m]

    # ---- pack entries --------------------------------------------------------
    Bc = bh.cluster_count
    nxt = Bc
    entries: list[NewEntry] = []
    pack_counts: dict[bytes, int] = {}
    for k, p in enumerate(packs):
        p.first_cluster = nxt
        nxt += p.z.header.cluster_count
        p.mime_map = [intern(m) for m in p.z.mimes]
        p.region = cluster_region(p.z)
        fronts = set()
        v1 = p.z.v1_listing()
        if v1:
            fronts = set(p.z.iter_u32_blob(*v1))
        seen = set()
        for d in p.z.iter_dirents():
            if d.ns != C:
                continue                       # M/ W/ X/ of packs are dropped
            e = NewEntry(d.key, d.title_raw, d.parameter, d.is_redirect, front=d.index in fronts,
                         pack=k, pack_index=d.index)
            if d.is_redirect:
                e.target_ref = d.target
            elif d.mime >= 0xFFFD:
                raise MergeError(f"{p.path}: unsupported dirent type for {d.path!r}")
            else:
                assert d.cluster is not None and d.blob is not None
                e.mime = p.mime_map[d.mime]
                e.cluster = d.cluster + p.first_cluster
                e.blob = d.blob
                if (d.cluster, d.blob) not in seen:      # libzim counts aliases once
                    seen.add((d.cluster, d.blob))
                    km = strip_mime(p.z.mimes[d.mime])
                    pack_counts[km] = pack_counts.get(km, 0) + 1
            entries.append(e)
    if index is not None:
        # the search pack's two indexes replace the base's; nothing else of it is carried
        index.first_cluster = nxt
        nxt += index.z.header.cluster_count
        index.mime_map = [intern(m) for m in index.z.mimes]
        index.region = cluster_region(index.z)
        for k in INDEX_KEYS:
            ki = index.z.find(*k)
            assert ki is not None                 # open_index checked
            d = index.z.dirent_at(ki)
            assert d.cluster is not None and d.blob is not None
            e = NewEntry(d.key, d.title_raw, d.parameter, False, index.mime_map[d.mime])
            e.cluster, e.blob = d.cluster + index.first_cluster, d.blob
            entries.append(e)
    Cm = nxt                                       # merge cluster number

    # ---- generated content -------------------------------------------------------
    raw_cfg = blob_of(base, KEY_MAPCFG)
    if raw_cfg is None:
        raise MergeError("the base has no map-config.json")
    base_cfg = json.loads(raw_cfg)
    # the base's own Wikipedia language (prototype bases omit "language": English)
    base_wiki = (base_cfg.get("language") or "en") if base_cfg.get("hasWikiArticles") else None
    if "wikiPacks" in base_cfg:
        raise MergeError("the base is already a merged file (its map-config.json has wikiPacks); "
                         "merge from the region's published base")
    cfg = dict(base_cfg)
    cfg["baseLanguage"] = base_cfg.get("language") or "en"
    # the visitor's order: the map language first, then the region's own
    # languages, then any other pack (prefix order); the layout stays in prefix order
    rank = {x: i for i, x in enumerate([lang] + list(region_langs or []))}
    visitor = sorted(packs, key=lambda p: (rank.get(p.lang, len(rank)), p.prefix))
    cfg["wikiPacks"] = [{"lang": p.lang, "prefix": p.prefix.decode(), "articles": p.articles} for p in visitor]
    cfg["language"] = lang
    mapcfg = json.dumps(cfg, ensure_ascii=False, indent=2).encode()
    base_meta = {k: v for k, v in ((k, blob_of(base, (M, k))) for k in
                                   (b"Name", b"Title", b"Language", b"Flavour", b"Description")) if v is not None}
    if region is None:
        m = re.match(rb"^osm_[a-z]{2,3}_(.+)$", base_meta.get(b"Name", b""))
        region = m.group(1).decode() if m else "region"
    title = title or base_meta.get(b"Title", b"").decode() or region or "region"
    if utf16_units(title) > 30:
        raise MergeError(f"Title {title!r} is {utf16_units(title)} UTF-16 units (max 30)")
    langs = [lang] + [p.lang for p in visitor if p.lang != lang]
    if language_meta == "map":      # kiwix-serve refuses /search on a multi-language book
        langs = [lang]
    try:
        language = ",".join(ISO3[x] for x in langs)
    except KeyError as e:
        raise MergeError(f"no ISO 639-3 code for {e}") from None
    wiki_set = ([base_wiki] if base_wiki else []) + [p.lang for p in packs if p.lang != base_wiki]
    meta = {b"Name": f"osm_{lang}_{region}".encode(), b"Title": title.encode(),
            b"Language": language.encode(),
            b"Flavour": ("maxi_wiki-" + "-".join(wiki_set)).encode() if wiki_set else b"maxi"}
    base_desc = base_meta.get(b"Description", b"").decode()
    if description is None and len(base_desc) > 80:
        # the base's own Description breaks zimcheck's limit: generate one
        description = f"{title}: offline map, Wikipedia {'/'.join(wiki_set) or '-'}"[:80]
    if description is not None:
        if len(description) > 80:
            raise MergeError("Description longer than 80 characters")
        meta[b"Description"] = description.encode()
    final_meta = dict(meta)
    if b"Description" not in final_meta and base_desc:
        final_meta[b"Description"] = base_desc.encode()

    def base_mime_of(key, default: bytes) -> int:
        i = base.find(*key)
        if i is not None:
            d = base.dirent_at(i)
            if not d.is_redirect:
                return d.mime
        return intern(default)

    gen = [NewEntry(KEY_V1, b"", b"", False, intern(LISTING_MIME)),
           NewEntry(KEY_COUNTER, b"", b"", False, intern(COUNTER_MIME)),
           NewEntry(KEY_MAPCFG, b"", b"", False, base_mime_of(KEY_MAPCFG, b"application/json"),
                    payload=mapcfg)]
    for k, v in sorted(meta.items()):
        gen.append(NewEntry((M, k), b"", b"", False, base_mime_of((M, k), META_MIME), payload=v))
    for g in gen:
        g.cluster = Cm
    entries.extend(gen)
    entries.sort(key=lambda e: e.key)
    for a, b in itertools.pairwise(entries):
        if a.key == b.key:
            raise MergeError(f"duplicate key {a.key}")
    t_scan = time.perf_counter()

    # ---- locate in base order --------------------------------------------------
    pos, R, override_of = 0, set(), {}
    for j, e in enumerate(entries):
        pos = gallop_lower_bound(Nb, key_at, e.key, pos)
        e.ins = pos
        if pos < Nb and key_at(pos) == e.key:
            if e.pack is not None:
                raise MergeError(f"pack entry {e.key} would override the base")
            e.overrides = pos
            override_of[pos] = j
            R.add(pos)
    v0 = base.find(*KEY_V0)
    if v0 is not None:
        raise MergeError("base carries a v0 listing item; unsupported")
    Rs = sorted(R)
    A = [e.ins for e in entries]
    plan = Plan(base, packs, lang, mimes=mimes, entries=entries, A=A, R=Rs, override_of=override_of,
                base_v1=base_v1, meta={k.decode(): v.decode() for k, v in meta.items()}, index=index)
    c_stable = all(a >= nC for j, a in enumerate(A) if entries[j].overrides is None) and \
        all(r in override_of for r in Rs)
    if not c_stable:
        raise MergeError("base C indices would shift; packs must sort after the base")

    # ---- pack redirects (within their own pack) -------------------------------
    by_pack: dict[tuple[int | None, int | None], int] = {
        (e.pack, e.pack_index): j for j, e in enumerate(entries) if e.pack is not None}
    for e in entries:
        if e.is_redirect:
            j = by_pack.get((e.pack, e.target_ref))
            if j is None:
                raise MergeError(f"pack redirect {e.key} leaves its pack")
            e.target = plan.new_idx_entry(j)

    # ---- v1 title listing: base fronts + every pack's fronts, by title ----------
    voff, Fb = base_v1

    def v1_key(k):
        d = bd(base.u32_at(voff, k))
        return (d.ns, d.title)

    pf = sorted(((e.key[0], e.title), e.pack, e.pack_index, j) for j, e in enumerate(entries) if e.front)
    pack_front, p = [], 0
    for tkey, _pk, _pi, j in pf:
        p = gallop_lower_bound(Fb, v1_key, tkey, p)
        pack_front.append((p, j))
    plan.pack_front = pack_front
    removed_front_pos = []
    for r in Rs:
        d = bd(r)
        k = gallop_lower_bound(Fb, v1_key, (d.ns, d.title))
        while k < Fb and v1_key(k) == (d.ns, d.title):
            if base.u32_at(voff, k) == r:
                removed_front_pos.append(k)
                break
            k += 1
    plan.layout["removed_front_pos"] = sorted(removed_front_pos)
    F = Fb - len(removed_front_pos) + len(pack_front)
    plan.listing_entries = F
    t_v1 = time.perf_counter()

    # ---- M/Counter ----------------------------------------------------------------
    counts = parse_counter(blob_of(base, KEY_COUNTER) or b"")
    for r in Rs:
        d = bd(r)
        if d.ns == C and not d.is_redirect:
            km = strip_mime(base.mimes[d.mime])
            counts[km] = counts.get(km, 0) - 1
    for e in entries:
        if e.pack is None and e.key[0] == C and not e.is_redirect:
            km = strip_mime(mimes[e.mime])
            counts[km] = counts.get(km, 0) + 1
    for km, n in pack_counts.items():
        counts[km] = counts.get(km, 0) + n
    counter = b";".join(b"%s=%d" % (k, v) for k, v in sorted(counts.items()) if v > 0)

    # ---- merge cluster blobs: v1, Counter, map-config, metadata (key order) ----------
    blobs = [b"\0" * (4 * F), counter]
    for e in entries:
        if e.key == KEY_V1:
            e.blob = 0
        elif e.key == KEY_COUNTER:
            e.blob = 1
        elif e.payload is not None:
            e.blob = len(blobs)
            blobs.append(e.payload)
    plan.blobs = blobs

    # ---- layout ----------------------------------------------------------------------
    bcr = cluster_region(base)
    last = base.dirent_at(Nb - 1)
    d0, d1 = base.path_ptr(0), last.offset + last.raw_len
    if d0 < bcr[1]:
        raise MergeError("base dirents must follow its clusters")
    cs = bcr[0]
    mime_blob = b"".join(m + b"\0" for m in mimes) + b"\0"
    if HEADER.size + len(mime_blob) > cs:
        raise MergeError("merged MIME list does not fit before the base's first cluster")
    pos = d1
    for p in packs + ([index] if index is not None else []):
        p.pos = pos
        pos += p.region[1] - p.region[0]
    mc = pos
    payload = sum(len(b) for b in blobs)
    nb = len(blobs)
    ext = (nb + 1) * 4 + payload > 0xFFFFFFFF
    w = 8 if ext else 4
    mc_size = 1 + (nb + 1) * w + payload
    N = Nb - len(Rs) + len(entries)
    Cn = Cm + 1
    path_ptr_pos = mc + mc_size
    cluster_ptr_pos = path_ptr_pos + 8 * N
    dirent_pos = cluster_ptr_pos + 8 * Cn
    checksum_pos = dirent_pos + sum(e.size() for e in entries)
    main = NO_U32
    bm = base.find(*KEY_MAIN)
    if bm is not None:
        main = plan.new_idx_base(bm)
    new_uuid = derive_uuid(bh.uuid, [p.uuid for p in packs] + ([index.uuid] if index is not None else []),
                           lang, final_meta, mapcfg)
    plan.header = Header(0x044D495A, 6, bh.minor, new_uuid, N, Cn, path_ptr_pos, NO_U64,
                         cluster_ptr_pos, HEADER.size, main, NO_U32, checksum_pos)
    plan.layout.update({
        "nC": nC, "cluster_start": cs, "base_dirents_end": d1, "mime_blob": mime_blob,
        "merge_cluster": mc, "merge_cluster_size": mc_size, "merge_cluster_extended": ext,
        "dirent_pos": dirent_pos, "total": checksum_pos + 16, "base_wiki": base_wiki,
        "generated_tail": checksum_pos + 16 - mc, "mapcfg": mapcfg,
    })
    plan.timings = {"scan_s": t_scan - t0, "v1_s": t_v1 - t_scan, "plan_s": time.perf_counter() - t0}
    return plan


# ------------------------------------------------------------------------------
# emission: a stream of bytes / Ref pieces
# ------------------------------------------------------------------------------
def pieces(plan: Plan) -> Iterator[bytes | Ref]:
    base, h, L = plan.base, plan.header, plan.layout
    bh = base.header
    cs = L["cluster_start"]
    yield h.pack() + L["mime_blob"] + bytes(cs - HEADER.size - len(L["mime_blob"]))
    yield Ref("base", cs, L["base_dirents_end"] - cs)
    for k, p in enumerate(plan.packs):
        yield Ref(("pack", k), p.region[0], p.region[1] - p.region[0])
    if plan.index is not None:
        yield Ref(("index", 0), plan.index.region[0], plan.index.region[1] - plan.index.region[0])
    # merge cluster
    blobs = plan.blobs
    w, fmt = (8, U64) if L["merge_cluster_extended"] else (4, U32)
    head = bytearray([0x11 if L["merge_cluster_extended"] else 0x01])
    off = (len(blobs) + 1) * w
    head += fmt.pack(off)
    for b in blobs:
        off += len(b)
        head += fmt.pack(off)
    yield bytes(head)
    # v1: base v1 runs, pack fronts spliced in, removed fronts skipped
    voff, Fb = plan.base_v1
    events = sorted([(k, 0, 0) for k in L["removed_front_pos"]] +
                    [(pp, -1, s) for s, (pp, _j) in enumerate(plan.pack_front)])
    k0, emitted = 0, 0
    ins = bytearray()
    for k, kind, s in events:
        if k > k0:
            if ins:
                yield bytes(ins)
                ins.clear()
            yield Ref("base", voff + 4 * k0, 4 * (k - k0))
            emitted += k - k0
        if kind == -1:
            ins += U32.pack(plan.new_idx_entry(plan.pack_front[s][1]))
            emitted += 1
            k0 = k
        else:
            k0 = k + 1
    if ins:
        yield bytes(ins)
    if Fb > k0:
        yield Ref("base", voff + 4 * k0, 4 * (Fb - k0))
        emitted += Fb - k0
    if emitted != plan.listing_entries:
        raise MergeError(f"v1 count {emitted} != {plan.listing_entries}")
    for b in blobs[1:]:
        yield b
    # path pointers: base pointer runs, new entries spliced in
    entries = plan.entries
    off = L["dirent_pos"]
    i0 = 0
    events = sorted([(e.ins, -1, j) for j, e in enumerate(entries)] + [(r, 0, 0) for r in plan.R])
    ins = bytearray()
    for i, kind, j in events:
        if i > i0:
            if ins:
                yield bytes(ins)
                ins.clear()
            yield Ref("base", bh.path_ptr_pos + 8 * i0, 8 * (i - i0))
        if kind == -1:
            ins += U64.pack(off)
            off += entries[j].size()
            i0 = i
        else:
            i0 = i + 1
    if ins:
        yield bytes(ins)
    if bh.entry_count > i0:
        yield Ref("base", bh.path_ptr_pos + 8 * i0, 8 * (bh.entry_count - i0))
    # cluster pointers
    yield Ref("base", bh.cluster_ptr_pos, 8 * bh.cluster_count)
    cp = bytearray()
    for p in plan.packs + ([plan.index] if plan.index is not None else []):
        delta = p.pos - p.region[0]
        for o in p.z.iter_u64_table(p.z.header.cluster_ptr_pos, p.z.header.cluster_count):
            cp += U64.pack(o + delta)
    cp += U64.pack(L["merge_cluster"])
    yield bytes(cp)
    # new dirents
    yield b"".join(e.encode() for e in entries)


def ref_path(plan: Plan, r: Ref) -> str:
    """The input file a Ref reads: the base, a pack, or the search pack."""
    if isinstance(r.src, str):
        return plan.base.path
    if r.src[0] == "index":
        assert plan.index is not None
        return plan.index.path
    return plan.packs[r.src[1]].path


def read_ref(plan: Plan, r: Ref, chunk: int = CHUNK) -> Iterator[bytes]:
    path = ref_path(plan, r)
    fd = os.open(path, os.O_RDONLY)
    try:
        pos, end = r.off, r.off + r.n
        while pos < end:
            b = os.pread(fd, min(chunk, end - pos), pos)
            if not b:
                raise EOFError(path)
            yield b
            pos += len(b)
    finally:
        os.close(fd)


def emit(plan: Plan, write) -> bytes:
    md5 = hashlib.md5(usedforsecurity=False)
    n = 0
    for pc in pieces(plan):
        for b in (read_ref(plan, pc) if isinstance(pc, Ref) else (pc,)):
            md5.update(b)
            write(b)
            n += len(b)
    if n != plan.header.checksum_pos:
        raise MergeError(f"emitted {n} != checksumPos {plan.header.checksum_pos}")
    d = md5.digest()
    write(d)
    return d


def merge(base, packs, out, lang, **kw) -> dict:
    t0 = time.perf_counter()
    plan = build_plan(base, packs, lang, **kw)
    t1 = time.perf_counter()
    tmp = out + ".part"
    with open(tmp, "wb", buffering=0) as fh:
        def w(b):
            mv = memoryview(b)
            while mv:
                mv = mv[fh.write(mv):]
        digest = emit(plan, w)
    if os.path.getsize(tmp) != plan.layout["total"]:
        raise MergeError("size mismatch")
    os.replace(tmp, out)
    t2 = time.perf_counter()
    return {"output": out, "bytes": plan.layout["total"], "md5": digest.hex(),
            "uuid": str(uuidlib.UUID(bytes=plan.header.uuid)), "lang": lang,
            "packs": [p.lang for p in plan.packs], "search_pack": bool(plan.index),
            "entries": plan.header.entry_count,
            "clusters": plan.header.cluster_count, "listing_entries": plan.listing_entries,
            "pack_fronts": len(plan.pack_front), "metadata": plan.meta,
            "generated_tail": plan.layout["generated_tail"], "plan_s": round(t1 - t0, 4),
            "emit_s": round(t2 - t1, 3), "MBps": round(plan.layout["total"] / 1e6 / max(t2 - t1, 1e-9), 1),
            "peak_rss_MB": round(peak_rss_mb(), 1)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base")
    ap.add_argument("packs", nargs="*")
    ap.add_argument("-o", "--out")
    ap.add_argument("--lang", required=True, help="map language L (2-letter)")
    ap.add_argument("--region")
    ap.add_argument("--title")
    ap.add_argument("--description")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--region-langs", default="", help="the region's own languages, comma list (wikiPacks order)")
    ap.add_argument("--allow-foreign-packs", action="store_true",
                    help="accept packs whose pack.json base_uuid is not this base or a recorded parent")
    ap.add_argument("--language-meta", choices=("list", "map"), default="list",
                    help="M/Language: list = L + pack languages (spec); map = L only")
    ap.add_argument("--index", help="a search pack (index_pack.py) whose indexes replace the base's")
    a = ap.parse_args(argv)
    kw = {"region": a.region, "title": a.title, "description": a.description, "language_meta": a.language_meta,
          "region_langs": [x for x in a.region_langs.split(",") if x], "allow_foreign_packs": a.allow_foreign_packs,
          "index_path": a.index}
    if a.plan_only:
        t = time.perf_counter()
        p = build_plan(a.base, a.packs, a.lang, **kw)
        print(json.dumps({"content_length": p.layout["total"], "plan_s": round(time.perf_counter() - t, 4),
                          "generated_tail": p.layout["generated_tail"], "metadata": p.meta,
                          "packs": [x.lang for x in p.packs], "peak_rss_MB": round(peak_rss_mb(), 1)}, indent=1))
        return 0
    if not a.out:
        ap.error("-o OUT required")
    print(json.dumps(merge(a.base, a.packs, a.out, a.lang, **kw), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
