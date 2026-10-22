#!/usr/bin/env python3
"""Language torrents: a torrent for every language version a region's /build/
page offers, webseeded by the build worker (build-proxy/).

    lang_torrents.py OUT_DIR --base BASE.zim --packs-dir DIR --webseed URL
                     [--base-torrent BASE.torrent] [--ia-cache DIR] [--full] [--langs el,de]
    lang_torrents.py OUT_DIR --strip      remove every torrent file and menu field
    lang_torrents.py X --only-torrent-failures CHECK_OUT   (ship_packs.sh: did site_check fail on torrents only?)

OUT_DIR is make_site.py's output for one region (recipes/<region>/: <L>.json,
region.json, index.json). For every menu entry it writes, next to the recipe:

  <L>.torrent   a single-file torrent of the merged ZIM, byte for byte the
                file the builder page assembles (so a visitor who built it
                can seed it). Piece length as ops/cloud/build_torrent.py
                chooses for that size; trackers LANG_TRACKERS (and DHT); the
                BEP 19 webseed (url-list) is
                URL/merged/<region>/<L>/<recipe uuid>/<file_name>.zim;
                its creation date is the newest archive.org mtime of its
                sources (none without --ia-cache), so a rerun on the same
                published files writes the same bytes
  <L>.ws.json   the worker's segment index (streetzim-webseed/1, a few KB):
                the merged file as parts, each either a byte range of one
                archive.org file (the base, a pack, the search pack; with its
                archive.org ETag when --ia-cache knows the file's mtime, so the
                worker's If-Range refuses a file replaced since) or a range of
                one of the blobs below
  ws-<h>.bin    the bytes the worker cannot range-read from archive.org: the
                recipe's inline segments, the MD5, and base or pack segments
                shorter than the remote threshold, coalesced; named by
                content, so identical runs (the viewer slots) are stored once
                per region

and adds "torrent" and "infohash" to the entry in region.json and index.json.
Blobs no index of OUT_DIR names any more are removed.

Subrequests (the free Workers plan allows 50 per request): a request costs
the worker 1 (the index, cold) + 1 per blob + 1 per archive.org part (2 for
the first part of each file: archive.org's redirect). Every 16 MiB window
(libtorrent's largest web seed request) must stay <= SUBREQ_LIMIT (40).
Short segments start at a 256 KiB remote threshold, local runs shorter than
4 KiB share one pool blob per entry, and an entry over the limit is rebuilt
with a higher threshold (1, 4, 16, 64 MiB: more bytes in blobs, fewer
parts); it fails if none fits, or its blobs would pass BLOB_BUDGET (64 MiB),
or the region's blobs, all entries together, REGION_BLOB_BUDGET (256 MiB).
The worker cuts a bigger range to what fits and answers a whole-file GET
that does not fit with 416 (ranges only).

Every byte is read through recipe2.ChunkReader (each 16 MiB chunk checked
against the recipe's SHA-256), and the merged stream's MD5 must equal the
recipe's file_md5 -- always.

--base-torrent: the base's own torrent (ops/cloud/build_torrent.py, same
piece length). The base's bytes keep their offsets in the merged file, so a
piece lying wholly inside an unchanged base range has the base torrent's
hash. Used only when the base torrent names this base (file name, length)
with the same piece length and EVERY one of its pieces matches the local
base (hashed once per run, for all entries); it saves the SHA-1 of those
pieces, not the read or the MD5. --full ignores it.

Exit: 0 every entry has its torrent; 3 some entries failed (they have none,
the others do: the folder is consistent); 1 the run itself failed
(ship_packs.sh then runs --strip).

Nothing is served or uploaded: the files are site data, sealed and published
with the recipes (ship_packs.sh, publish_site.sh).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ops.cloud.build_torrent import auto_piece_size, bencode  # noqa: E402
from tools.langpack.merge_zim import MergeError  # noqa: E402
from tools.langpack.recipe2 import FORMAT as RECIPE_FORMAT, ChunkReader  # noqa: E402

INDEX_FORMAT = "streetzim-webseed/1"
REMOTE_MIN = 256 << 10       # shorter base/pack segments are served from blobs (the ladder's first step)
BLOB_MAX = 4 << 20           # a blob is fetched whole by the worker and kept in memory
BLOB_RE = re.compile(r"^ws-[0-9a-f]{32}\.bin$")
ETAG_RE = re.compile(r'^"([0-9a-f]+)-[0-9a-f]+"$')    # archive.org: "<mtime hex>-<size hex>"
ITEM_RE = re.compile(r"^streetzim-[a-z0-9-]+$")
FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.zim$")
REGION_RE = re.compile(r"^[a-z0-9-]+$")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]+)?$")
# Trackers (2026-10-21): open UDP trackers that answered a BEP 15 connect
# (tracker.openbittorrent.com did not), opentrackr first as `announce`, and
# archive.org's two, which answered an announce for a hash that is not one of
# their items. DHT and PEX also work (the torrents are not private).
LANG_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "http://bt1.archive.org:6969/announce",
    "http://bt2.archive.org:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://exodus.desync.com:6969/announce",
]
SUBREQ_LIMIT = int(os.environ.get("LANGPACK_WEBSEED_SUBREQ_LIMIT") or 40)
BLOB_BUDGET = int(os.environ.get("LANGPACK_WEBSEED_BLOB_BUDGET") or 64 << 20)   # blob bytes per entry
REGION_BLOB_BUDGET = int(os.environ.get("LANGPACK_WEBSEED_REGION_BLOB_BUDGET") or 256 << 20)   # per region (all entries)
REMOTE_LADDER = (256 << 10, 1 << 20, 4 << 20, 16 << 20, 64 << 20)
WINDOW = 16 << 20            # a libtorrent web seed request (urlseed_max_request_bytes)
TINY_MAX = 4096              # local runs shorter than this share the entry's pool blob


class TorrentError(Exception):
    pass


def blob_name(sha: str) -> str:
    return f"ws-{sha[:32]}.bin"


def item_of(url: str, label: str) -> tuple[str, str]:
    """(archive.org item, file) from a recipe/region URL: "{proxy}/<item>/<file>"
    or "https://archive.org/download/<item>/<file>"."""
    path = None
    if url.startswith("{proxy}/"):
        path = url[len("{proxy}/"):]
    else:
        u = urllib.parse.urlparse(url)
        if u.netloc == "archive.org" and u.path.startswith("/download/"):
            path = u.path[len("/download/"):]
    parts = (path or "").split("/")
    if len(parts) != 2 or not ITEM_RE.match(parts[0]) or not FILE_RE.match(parts[1]):
        raise TorrentError(f"{label}: {url!r} is not <proxy or archive.org>/<streetzim item>/<file>.zim")
    return parts[0], parts[1]


def webseed_url(webseed: str, region: str, lang: str, uuid: str, file: str) -> str:
    return f"{webseed.rstrip('/')}/merged/{region}/{lang}/{uuid}/{file}"


def build_index(recipe: dict, region: dict, base_path: str, pack_paths: dict[str, str],
                lang: str, webseed: str, remote_min: int = REMOTE_MIN,
                etags: dict[tuple[str, str], str] | None = None) -> tuple[dict, dict[str, bytes], dict]:
    """The worker's segment index for one recipe, the blobs it names, and the
    open ChunkReaders (for hashing). Base/pack segments of at least remote_min
    bytes are archive.org parts; the rest goes into blobs."""
    if recipe.get("format") != RECIPE_FORMAT:
        raise TorrentError("not a streetzim-recipe/2")
    b = recipe["base"]
    if os.path.basename(os.path.realpath(base_path)) != b["file"] or os.path.getsize(base_path) != b["size"]:
        raise TorrentError(f"{base_path} is not the recipe's base {b['file']} ({b['size']} bytes)")
    rb = region["base"]
    base_item, base_file = item_of(rb.get("url") or rb["download"], "region base")
    if base_file != b["file"]:
        raise TorrentError(f"region base {base_file} != recipe base {b['file']}")
    readers = {"base": ChunkReader(base_path, b["size"], b["chunk"], b["sha256"], "base", b.get("mask"))}
    sources = {"base": {"item": base_item, "file": base_file, "size": b["size"]}}
    etags = etags or {}
    for key, p in recipe["packs"].items():
        item, file = item_of(p["url"], f"pack {key}")
        if file != p["file"]:
            raise TorrentError(f"pack {key}: URL file {file} != {p['file']}")
        if key not in pack_paths:
            raise TorrentError(f"pack {key}: {p['file']} is not in the packs folder")
        readers[key] = ChunkReader(pack_paths[key], p["size"], p["chunk"], p["sha256"], f"pack {key}")
        sources[key] = {"item": item, "file": file, "size": p["size"]}
    for src in sources.values():
        tag = etags.get((src["item"], src["file"]))
        if tag:
            src["etag"] = tag
    masks = b.get("mask") or []

    parts: list[list] = []
    blobs: dict[str, bytes] = {}
    pool = bytearray()             # the entry's tiny runs, one blob (offset-addressed)
    pool_parts: list[list] = []
    buf = bytearray()
    buf_start = 0
    pos = 0

    def flush(upto: int | None = None):
        nonlocal buf, buf_start
        while buf and (upto is None or len(buf) >= upto):
            take = bytes(buf[:BLOB_MAX])
            sha = hashlib.sha256(take).hexdigest()
            blobs[sha] = take
            parts.append([buf_start, len(take), "x", sha, 0])
            buf_start += len(take)
            del buf[:len(take)]

    def close_pool():
        if pool:
            sha = hashlib.sha256(bytes(pool)).hexdigest()
            blobs[sha] = bytes(pool)
            for part in pool_parts:
                part[3] = sha
            pool.clear()
            pool_parts.clear()

    def end_run():
        """A local run ended: a short one joins the pool, a longer one is blobs."""
        nonlocal buf_start
        if buf and len(buf) < TINY_MAX:
            if len(pool) + len(buf) > BLOB_MAX:
                close_pool()
            part = [buf_start, len(buf), "x", None, len(pool)]
            pool.extend(buf)
            parts.append(part)
            pool_parts.append(part)
            buf_start += len(buf)
            buf.clear()
        flush()

    for s in recipe["segments"]:
        src = s["src"]
        if src == "md5":
            data, n = bytes.fromhex(recipe["md5"]), 16
        else:
            n = s["len"]
            data = None
            if src == "inline":
                data = base64.b64decode(s["data"])
                if len(data) != n or hashlib.sha256(data).hexdigest() != s["sha256"]:
                    raise TorrentError(f"inline segment at {pos} fails its SHA-256")
            elif src in ("base", "pack"):
                key = "base" if src == "base" else s["pack"]
                if key not in readers:
                    raise TorrentError(f"segment at {pos} reads unknown source {key!r}")
                if src == "base":
                    for off, ln in masks:
                        if s["off"] < off + ln and off < s["off"] + n:
                            raise TorrentError(f"base segment at {s['off']} reads the masked range at {off}")
                if n >= remote_min:
                    end_run()
                    parts.append([pos, n, "s", key, s["off"]])
                    buf_start = pos + n
                    pos += n
                    continue
                data = b"".join(readers[key].read(s["off"], n))
            else:
                raise TorrentError(f"unknown segment {src!r}")
        if not buf:
            buf_start = pos
        buf += data
        pos += n
        flush(BLOB_MAX)
    end_run()
    close_pool()
    if pos != recipe["size"]:
        raise TorrentError(f"segments add up to {pos}, recipe size {recipe['size']}")
    file = f"{recipe.get('file_name') or recipe['name']}.zim"
    if not FILE_RE.match(file) or not LANG_RE.match(lang) or not REGION_RE.match(region["id"]):
        raise TorrentError(f"unusable name: {region['id']}/{lang}/{file}")
    index = {
        "format": INDEX_FORMAT, "region": region["id"], "lang": lang, "uuid": recipe["uuid"],
        "file": file, "size": recipe["size"], "md5": recipe["md5"], "file_md5": recipe.get("file_md5"),
        "sources": sources,
        "blobs": {sha: {"file": blob_name(sha), "size": len(data)} for sha, data in blobs.items()},
        "parts": parts,
        "webseed": webseed_url(webseed, region["id"], lang, recipe["uuid"], file),
        "remote_min": remote_min,
    }
    index["subrequests"] = costs(index)
    return index, blobs, readers


def subrequests(index: dict, start: int, end: int) -> int:
    """The worker's subrequests for bytes [start, end] in a cold isolate: the
    index, each blob once, each archive.org part (2 for a file's first part:
    archive.org's redirect to its storage node)."""
    n, blobs, files = 1, set(), set()
    for pstart, plen, kind, ref, _off in index["parts"]:
        if pstart + plen <= start or pstart > end:
            continue
        if kind == "x":
            if ref not in blobs:
                blobs.add(ref)
                n += 1
        else:
            n += 1 if ref in files else 2
            files.add(ref)
    return n


def costs(index: dict) -> dict:
    """Worst-case subrequests: the worst 16 MiB window (which bounds what a
    torrent client asks: libtorrent's web seed requests are at most 16 MiB)
    and the whole file. A window's parts change only where it starts at a
    part's first or last byte, so those starts (and multiples of 16 MiB) find
    the worst."""
    size = index["size"]
    starts = sorted({p[0] for p in index["parts"]} | {p[0] + p[1] - 1 for p in index["parts"]}
                    | set(range(0, size, WINDOW)))
    return {"whole": subrequests(index, 0, size - 1),
            "window16m": max(subrequests(index, s, min(size, s + WINDOW) - 1) for s in starts)}


def blob_bytes(blobs: dict[str, bytes]) -> int:
    return sum(len(b) for b in blobs.values())


def plan_index(recipe: dict, region: dict, base_path: str, pack_paths: dict[str, str], lang: str,
               webseed: str, etags: dict | None = None) -> tuple[dict, dict[str, bytes], dict]:
    """build_index with the lowest remote threshold at which every 16 MiB
    window fits SUBREQ_LIMIT; TorrentError if none does, or if the blobs
    would pass BLOB_BUDGET (a higher threshold only adds blob bytes)."""
    for rm in REMOTE_LADDER:
        index, blobs, readers = build_index(recipe, region, base_path, pack_paths, lang, webseed, rm, etags)
        if blob_bytes(blobs) > BLOB_BUDGET:
            raise TorrentError(f"{blob_bytes(blobs) / 1e6:.0f} MB of blobs at a {rm >> 10} KiB threshold "
                               f"(budget {BLOB_BUDGET / 1e6:.0f} MB)")
        if index["subrequests"]["window16m"] <= SUBREQ_LIMIT:
            return index, blobs, readers
    raise TorrentError(f"a 16 MiB request needs {index['subrequests']['window16m']} subrequests even with "
                       f"{REMOTE_LADDER[-1] >> 20} MiB parts (limit {SUBREQ_LIMIT})")


def load_etags(ia_dir: str | None) -> dict[tuple[str, str], str]:
    """archive.org ETags ("<mtime hex>-<size hex>") from item metadata JSON in
    ia_dir (ia-<item>.json, as site_check.py caches it): (item, file) -> etag."""
    out: dict[tuple[str, str], str] = {}
    if not ia_dir or not os.path.isdir(ia_dir):
        return out
    for f in os.listdir(ia_dir):
        if not (f.startswith("ia-") and f.endswith(".json")):
            continue
        try:
            doc = json.load(open(os.path.join(ia_dir, f), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        item = (doc.get("metadata") or {}).get("identifier") or f[len("ia-"):-len(".json")]
        for x in doc.get("files") or doc.get("result") or []:
            try:
                out[(item, x["name"])] = '"%x-%x"' % (int(x["mtime"]), int(x["size"]))
            except (KeyError, ValueError, TypeError):
                continue
    return out


class Stream:
    """Random access to the merged bytes an index describes."""

    def __init__(self, index: dict, blobs: dict[str, bytes], readers: dict):
        self.parts, self.blobs, self.readers = index["parts"], blobs, readers
        self.starts = [p[0] for p in self.parts]

    def read(self, start: int, n: int):
        import bisect
        i = max(0, bisect.bisect_right(self.starts, start) - 1)
        end = start + n
        while start < end:
            p = self.parts[i]
            pstart, plen, kind, ref, off = p
            lo, hi = start - pstart, min(end, pstart + plen) - pstart
            if hi <= lo:
                raise TorrentError(f"no part covers byte {start}")
            if kind == "x":
                yield self.blobs[ref][off + lo:off + hi]
            else:
                yield from self.readers[ref].read(off + lo, hi - lo)
            start = pstart + hi
            i += 1


def bdecode(data: bytes) -> Any:
    """Minimal bdecode (the inverse of build_torrent.bencode)."""
    def dec(i: int) -> tuple[Any, int]:
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c == b"l":
            out, i = [], i + 1
            while data[i:i + 1] != b"e":
                v, i = dec(i)
                out.append(v)
            return out, i + 1
        if c == b"d":
            out, i = {}, i + 1
            while data[i:i + 1] != b"e":
                k, i = dec(i)
                v, i = dec(i)
                out[k] = v
            return out, i + 1
        j = data.index(b":", i)
        n = int(data[i:j])
        return data[j + 1:j + 1 + n], j + 1 + n
    v, end = dec(0)
    if end != len(data):
        raise TorrentError("trailing bytes after the torrent")
    return v


def reusable_pieces(index: dict, plen: int) -> set[int]:
    """Pieces wholly inside a part that copies the base to its own offsets."""
    out: set[int] = set()
    size = index["size"]
    for start, n, kind, ref, off in index["parts"]:
        if kind != "s" or ref != "base" or off != start:
            continue
        first = -(-start // plen)                      # first piece starting at or after the part
        k = first
        while k * plen < start + n:
            if min((k + 1) * plen, size) <= start + n:
                out.add(k)
            k += 1
    return out


def piece_hashes(index: dict, stream: Stream, plen: int, base_pieces: bytes | None = None,
                 reuse: set[int] | None = None) -> tuple[bytes, str, dict]:
    """SHA-1 of every piece (a reused piece takes the base torrent's) and the
    MD5 of the whole stream: every byte is read (and chunk-checked) either way."""
    size = index["size"]
    n_pieces = -(-size // plen)
    reuse = reuse or set()
    md5 = hashlib.md5(usedforsecurity=False)
    out = bytearray()
    hashed = 0
    for k in range(n_pieces):
        start = k * plen
        n = min(plen, size - start)
        h = None if k in reuse else hashlib.sha1(usedforsecurity=False)
        got = 0
        for chunk in stream.read(start, n):
            md5.update(chunk)
            if h is not None:
                h.update(chunk)
            got += len(chunk)
        if got != n:
            raise TorrentError(f"piece {k}: read {got} of {n} bytes")
        if h is None:
            assert base_pieces is not None
            out += base_pieces[20 * k:20 * k + 20]
        else:
            hashed += n
            out += h.digest()
    return bytes(out), md5.hexdigest(), {"pieces": n_pieces, "reused": len(reuse), "hashed_bytes": hashed}


def load_base_torrent(path: str, base_file: str, base_size: int, plen: int) -> tuple[bytes | None, str]:
    """The base torrent's piece hashes when it can be reused, else (None, why)."""
    try:
        t = bdecode(open(path, "rb").read())
        info = t[b"info"]
        name, length, pl, pieces = info[b"name"].decode(), info[b"length"], info[b"piece length"], info[b"pieces"]
    except (OSError, KeyError, ValueError, TorrentError, AttributeError) as e:
        return None, f"unreadable base torrent ({e})"
    if name != base_file or length != base_size:
        return None, f"base torrent is for {name} ({length} bytes), not {base_file} ({base_size})"
    if pl != plen:
        return None, f"base torrent piece length {pl} != {plen}"
    if len(pieces) != 20 * -(-base_size // plen):
        return None, "base torrent has the wrong number of pieces"
    return pieces, ""


_VERIFIED: dict[tuple, tuple[bytes | None, str]] = {}


def verified_base_pieces(path: str, base_path: str, base_file: str, base_size: int,
                         plen: int) -> tuple[bytes | None, str]:
    """load_base_torrent, then every piece checked against the local base
    (once per run: the result is kept for the other entries)."""
    st = os.stat(base_path)
    key = (os.path.realpath(path), os.path.realpath(base_path), st.st_size, st.st_mtime_ns, plen)
    if key not in _VERIFIED:
        pieces, why = load_base_torrent(path, base_file, base_size, plen)
        if pieces is not None:
            with open(base_path, "rb", buffering=0) as fh:
                k = 0
                while True:
                    b = fh.read(plen)
                    if not b:
                        break
                    if hashlib.sha1(b, usedforsecurity=False).digest() != pieces[20 * k:20 * k + 20]:
                        pieces, why = None, f"base torrent piece {k} does not match the local base"
                        break
                    k += 1
            if pieces is not None:
                why = f"base torrent verified against the local base ({k} pieces)"
        _VERIFIED[key] = (pieces, why)
    return _VERIFIED[key]


def creation_date(index: dict) -> int | None:
    """The newest archive.org mtime among the index's sources (from their
    ETags): the same published files give the same torrent bytes on a rerun
    (a re-ship rewrites nothing unchanged). None when no source has one."""
    ts = [int(m.group(1), 16) for x in index["sources"].values()
          if (m := ETAG_RE.match(x.get("etag") or ""))]
    return max(ts) if ts else None


def make_torrent(index: dict, pieces: bytes, plen: int) -> tuple[bytes, str]:
    info = {"name": index["file"], "piece length": plen, "pieces": pieces, "length": index["size"]}
    torrent = {
        "announce": LANG_TRACKERS[0],
        "announce-list": [[t] for t in LANG_TRACKERS],
        "comment": f"streetzim — {index['file']} ({index['region']}, {index['lang']})",
        "created by": "streetzim/lang_torrents.py",
        "info": info,
        # BEP 19: the build worker serves this file virtually from the base
        # and packs on archive.org, so it downloads with no peer online.
        "url-list": [index["webseed"]],
    }
    when = creation_date(index)
    if when is not None:
        torrent["creation date"] = when
    return bencode(torrent), hashlib.sha1(bencode(info)).hexdigest()


def write_atomic(path: str, data: bytes):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def torrent_for(recipe_path: str, region: dict, lang: str, base_path: str, pack_paths: dict[str, str],
                webseed: str, base_torrent: str | None = None, full: bool = False,
                out_dir: str | None = None, etags: dict | None = None,
                region_blobs: dict[str, int] | None = None) -> dict:
    recipe = json.load(open(recipe_path, encoding="utf-8"))
    if not recipe.get("file_md5"):
        raise TorrentError(f"{lang}: the recipe has no file_md5")
    t0 = time.perf_counter()
    index, blobs, readers = plan_index(recipe, region, base_path, pack_paths, lang, webseed, etags)
    plen = auto_piece_size(index["size"])
    stream = Stream(index, blobs, readers)
    reuse: set[int] = set()
    base_pieces = None
    why = "--full" if full else "no base torrent"
    if base_torrent and not full:
        base_pieces, why = verified_base_pieces(base_torrent, base_path, recipe["base"]["file"],
                                                recipe["base"]["size"], plen)
        if base_pieces is not None:
            reuse = reusable_pieces(index, plen)
    pieces, md5, st = piece_hashes(index, stream, plen, base_pieces, reuse)
    if md5 != recipe["file_md5"]:
        raise TorrentError(f"{lang}: the merged stream's MD5 {md5} != recipe file_md5 {recipe['file_md5']}")
    data, infohash = make_torrent(index, pieces, plen)
    index["piece_length"] = plen
    index["infohash"] = infohash
    index["torrent"] = f"{lang}.torrent"
    out_dir = out_dir or os.path.dirname(recipe_path)
    if region_blobs is not None:            # the region's blobs, all entries together
        new = {sha: len(b) for sha, b in blobs.items() if sha not in region_blobs}
        total = sum(region_blobs.values()) + sum(new.values())
        if total > REGION_BLOB_BUDGET:
            raise TorrentError(f"{lang}: the region's blobs would reach {total / 1e6:.0f} MB "
                               f"(budget {REGION_BLOB_BUDGET / 1e6:.0f} MB)")
        region_blobs.update(new)
    for sha, blob in blobs.items():
        p = os.path.join(out_dir, blob_name(sha))
        if not (os.path.exists(p) and os.path.getsize(p) == len(blob)
                and hashlib.sha256(open(p, "rb").read()).hexdigest() == sha):
            write_atomic(p, blob)
    write_atomic(os.path.join(out_dir, f"{lang}.ws.json"),
                 json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode())
    write_atomic(os.path.join(out_dir, f"{lang}.torrent"), data)
    return {"lang": lang, "infohash": infohash, "size": index["size"], "piece_length": plen,
            "parts": len(index["parts"]), "blobs": len(blobs), "blob_bytes": sum(len(x) for x in blobs.values()),
            "remote_min": index["remote_min"], "subrequests": index["subrequests"],
            "etags": sum(1 for x in index["sources"].values() if "etag" in x),
            "md5_checked": True, "reuse": why, **st, "s": round(time.perf_counter() - t0, 2)}


def verify_entry(out_dir: str, region: dict, entry: dict, recipe: dict, base_path: str,
                 pack_paths: dict[str, str], expected_origin: str | None = None,
                 etags: dict | None = None) -> list[str]:
    """What is wrong with a menu entry's torrent files (site_check.py), [] if
    nothing; every message starts "TORRENT " (ship_packs.sh strips torrents
    when they are all that failed). The index is rebuilt from the recipe (with
    its remote threshold) and must be the stored one and fit SUBREQ_LIMIT;
    the webseed origin must be expected_origin (LANGPACK_WEBSEED) when given;
    every blob must be on disk with its SHA-256; the torrent must describe the
    recipe's file with that webseed and the entry's infohash; and EVERY piece
    must hash as the torrent says (the bytes read through the recipe's chunk
    check), and the stream's MD5 be the recipe's file_md5."""
    L = entry["lang"]
    fails: list[str] = []

    def bad(msg: str):
        fails.append(f"TORRENT {L}: {msg}")
    try:
        ix = json.load(open(os.path.join(out_dir, f"{L}.ws.json"), encoding="utf-8"))
        origin = ix["webseed"].split("/merged/", 1)[0]
        if expected_origin is not None and origin != expected_origin.rstrip("/"):
            bad(f"webseed origin {origin} != LANGPACK_WEBSEED {expected_origin}")
        if etags is None:
            etags = {(x["item"], x["file"]): x["etag"] for x in ix.get("sources", {}).values() if "etag" in x}
        want, blobs, readers = build_index(recipe, region, base_path, pack_paths, L, origin,
                                           int(ix.get("remote_min") or REMOTE_MIN), etags)
        for k in ("format", "region", "lang", "uuid", "file", "size", "md5", "file_md5", "sources", "blobs",
                  "parts", "webseed", "remote_min", "subrequests"):
            if ix.get(k) != want.get(k):
                bad(f"{L}.ws.json {k} != the index rebuilt from the recipe")
        if want["subrequests"]["window16m"] > SUBREQ_LIMIT:
            bad(f"a 16 MiB request may need {want['subrequests']['window16m']} subrequests (limit {SUBREQ_LIMIT})")
        if blob_bytes(blobs) > BLOB_BUDGET:
            bad(f"{blob_bytes(blobs)} blob bytes (budget {BLOB_BUDGET})")
        for sha in blobs:
            p = os.path.join(out_dir, blob_name(sha))
            if not os.path.exists(p) or hashlib.sha256(open(p, "rb").read()).hexdigest() != sha:
                bad(f"blob {blob_name(sha)} missing or not its SHA-256")
        t = bdecode(open(os.path.join(out_dir, f"{L}.torrent"), "rb").read())
        info = t[b"info"]
        plen = auto_piece_size(recipe["size"])
        if (info[b"name"].decode(), info[b"length"], info[b"piece length"]) != (want["file"], recipe["size"], plen):
            bad("torrent name/length/piece length != the recipe's file")
        if len(info[b"pieces"]) != 20 * -(-recipe["size"] // plen):
            bad("torrent piece count")
        if [u.decode() for u in t.get(b"url-list", [])] != [want["webseed"]]:
            bad(f"torrent webseed != {want['webseed']}")
        ih = hashlib.sha1(bencode(info)).hexdigest()
        if ih != entry.get("infohash") or ih != ix.get("infohash"):
            bad(f"infohash {ih} != the entry's/index's")
        if entry.get("torrent", "").rsplit("/", 1)[-1] != f"{L}.torrent":
            bad(f"entry torrent path {entry.get('torrent')}")
        if not fails:
            pieces, md5, _ = piece_hashes(want, Stream(want, blobs, readers), plen)
            if pieces != info[b"pieces"]:
                k = next(i for i in range(len(pieces) // 20) if pieces[20 * i:20 * i + 20] != info[b"pieces"][20 * i:20 * i + 20])
                bad(f"torrent piece {k} does not hash as the merged bytes do")
            if md5 != recipe.get("file_md5"):
                bad(f"the merged stream's MD5 {md5} != recipe file_md5")
    except (OSError, KeyError, ValueError, TypeError, TorrentError, MergeError, StopIteration) as e:
        bad(f"torrent files unreadable or inconsistent ({e})")
    return fails


def region_blob_bytes(out_dir: str) -> int:
    """Bytes of every blob in a region's folder (site_check.py: REGION_BLOB_BUDGET)."""
    return sum(os.path.getsize(os.path.join(out_dir, f)) for f in os.listdir(out_dir) if BLOB_RE.match(f))


def stray_files(out_dir: str, region: dict) -> list[str]:
    """Torrent files no menu entry with a torrent names (site_check.py)."""
    langs = {m["lang"] for m in region["menu"] if m.get("torrent")}
    keep = set()
    for L in langs:
        try:
            keep |= {b["file"] for b in json.load(open(os.path.join(out_dir, f"{L}.ws.json")))["blobs"].values()}
        except (OSError, ValueError, KeyError):
            pass
    return [f for f in sorted(os.listdir(out_dir))
            if (BLOB_RE.match(f) and f not in keep)
            or (f.endswith(".ws.json") and f[:-len(".ws.json")] not in langs)
            or (f.endswith(".torrent") and f[:-len(".torrent")] not in langs)]


def annotate(out_dir: str, results: dict[str, dict]):
    """Add torrent/infohash to region.json's and index.json's menu entries."""
    for name in ("region.json", "index.json"):
        p = os.path.join(out_dir, name)
        if not os.path.exists(p):
            continue
        doc = json.load(open(p, encoding="utf-8"))
        regions = doc["regions"] if "regions" in doc else [doc]
        for r in regions:
            prefix = None
            for m in r["menu"]:
                res = results.get(m["lang"])
                if res is None:
                    m.pop("torrent", None)
                    m.pop("infohash", None)
                    continue
                prefix = m["recipe"].rsplit("/", 1)[0] + "/" if "/" in m["recipe"] else ""
                m["torrent"] = f"{prefix}{m['lang']}.torrent"
                m["infohash"] = res["infohash"]
        write_atomic(p, json.dumps(doc, ensure_ascii=False, indent=1).encode())


def prune(out_dir: str) -> list[str]:
    """Remove blobs, indexes and torrents no menu entry uses."""
    region = json.load(open(os.path.join(out_dir, "region.json"), encoding="utf-8"))
    removed = stray_files(out_dir, region)
    for f in removed:
        os.remove(os.path.join(out_dir, f))
    return removed


def only_torrent_failures(check_output: str) -> bool:
    """True when site_check.py failed only on torrent files (every FAIL line
    starts "FAIL TORRENT "): ship_packs.sh then strips them and checks again."""
    fails = [ln for ln in check_output.splitlines() if ln.startswith("FAIL")]
    return bool(fails) and all(ln.startswith("FAIL TORRENT ") for ln in fails)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir")
    ap.add_argument("--base")
    ap.add_argument("--packs-dir")
    ap.add_argument("--webseed", help="the build worker's origin, e.g. https://streetzim-build-proxy.<acct>.workers.dev")
    ap.add_argument("--strip", action="store_true", help="remove every torrent file and menu field")
    ap.add_argument("--base-torrent", help="the base's torrent (reuse its piece hashes)")
    ap.add_argument("--full", action="store_true", help="hash every byte and check the MD5 (ignore --base-torrent)")
    ap.add_argument("--langs", help="only these menu languages (comma-separated)")
    ap.add_argument("--ia-cache", help="archive.org item metadata (ia-<item>.json): the sources' ETags")
    ap.add_argument("--only-torrent-failures", metavar="CHECK_OUT",
                    help="exit 0 if site_check.py's output CHECK_OUT failed only on torrent files, else 1")
    a = ap.parse_args(argv)
    if a.only_torrent_failures:
        return 0 if only_torrent_failures(open(a.only_torrent_failures, encoding="utf-8").read()) else 1
    if a.strip:
        annotate(a.out_dir, {})
        print(json.dumps({"stripped": prune(a.out_dir)}), flush=True)
        return 0
    if not (a.base and a.packs_dir and a.webseed):
        ap.error("--base, --packs-dir and --webseed are required (or --strip)")
    u = urllib.parse.urlparse(a.webseed)
    if u.scheme not in ("https", "http") or not u.netloc or u.path not in ("", "/"):
        print(f"lang_torrents: --webseed must be an origin (https://host), not {a.webseed!r}", file=sys.stderr)
        return 1
    region = json.load(open(os.path.join(a.out_dir, "region.json"), encoding="utf-8"))
    packs = {}
    for f in os.listdir(a.packs_dir):
        if f.endswith(".zim"):
            packs[f] = os.path.join(a.packs_dir, f)
    only = set(a.langs.split(",")) if a.langs else None
    etags = load_etags(a.ia_cache)
    results: dict[str, dict] = {}
    region_blobs: dict[str, int] = {}
    for m in region["menu"]:                # the entries this run keeps count against the region budget
        if only is not None and m["lang"] not in only and m.get("torrent"):
            try:
                ix = json.load(open(os.path.join(a.out_dir, f"{m['lang']}.ws.json"), encoding="utf-8"))
                region_blobs.update({sha: b["size"] for sha, b in ix["blobs"].items()})
            except (OSError, ValueError, KeyError):
                pass
    rc = 0
    for m in region["menu"]:
        L = m["lang"]
        if only is not None and L not in only:
            continue
        rpath = os.path.join(a.out_dir, os.path.basename(m["recipe"]))
        try:
            recipe_packs = json.load(open(rpath, encoding="utf-8"))["packs"]
            pack_paths = {k: packs[p["file"]] for k, p in recipe_packs.items() if p["file"] in packs}
            res = torrent_for(rpath, region, L, a.base, pack_paths, a.webseed, a.base_torrent, a.full,
                              etags=etags, region_blobs=region_blobs)
        except (TorrentError, MergeError, OSError, KeyError, ValueError) as e:
            print(json.dumps({"lang": L, "FAILED": str(e)}), flush=True)
            rc = 3
            continue
        results[L] = res
        print(json.dumps(res), flush=True)
    if only is not None:          # keep the entries not redone
        for m in region["menu"]:
            if m["lang"] not in results and m.get("torrent") and m["lang"] not in only:
                results[m["lang"]] = {"infohash": m["infohash"]}
    annotate(a.out_dir, results)
    removed = prune(a.out_dir)
    print(json.dumps({"torrents": sorted(results), "removed": removed}), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
