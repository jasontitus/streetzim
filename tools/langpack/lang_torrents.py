#!/usr/bin/env python3
"""Language torrents: a torrent for every language version a region's /build/
page offers, webseeded by the build worker (build-proxy/).

    lang_torrents.py OUT_DIR --base BASE.zim --packs-dir DIR --webseed URL
                     [--base-torrent BASE.torrent] [--full] [--langs el,de]
    lang_torrents.py OUT_DIR --strip      remove every torrent file and menu field

Exit: 0 every entry has its torrent; 3 some entries failed (they have none,
the others do: the folder is consistent); 1 the run itself failed
(ship_packs.sh then runs --strip).

OUT_DIR is make_site.py's output for one region (recipes/<region>/: <L>.json,
region.json, index.json). For every menu entry it writes, next to the recipe:

  <L>.torrent   a single-file torrent of the merged ZIM, byte for byte the
                file the builder page assembles (so a visitor who built it
                can seed it). Piece length and trackers as
                ops/cloud/build_torrent.py chooses for that size; the BEP 19
                webseed (url-list) is
                URL/merged/<region>/<L>/<recipe uuid>/<file_name>.zim
  <L>.ws.json   the worker's segment index (streetzim-webseed/1, a few KB):
                the merged file as parts, each either a byte range of one
                archive.org file (the base, a pack, the search pack) or a
                range of one of the blobs below
  ws-<h>.bin    the bytes the worker cannot range-read from archive.org: the
                recipe's inline segments, the MD5, and runs of short base or
                pack segments, coalesced so that one webseed request touches
                a handful of parts (the free Workers plan allows 50
                subrequests per request); named by content, so identical
                runs (the viewer slots) are stored once per region

and adds "torrent" and "infohash" to the entry in region.json and index.json.
Blobs no index of OUT_DIR names any more are removed.

Every byte is read through recipe2.ChunkReader (each 16 MiB chunk checked
against the recipe's SHA-256), and, without a base torrent, the merged
stream's MD5 must equal the recipe's file_md5.

--base-torrent: the base's own torrent (ops/cloud/build_torrent.py, same
piece length). The base's bytes keep their offsets in the merged file, so a
piece lying wholly inside an unchanged base range has the base torrent's
hash. Used only when the base torrent names this base (file name, length)
with the same piece length, and only after a sample of the reused hashes
matches the local base; then the MD5 pass is skipped (every byte that is
hashed is still chunk-checked). --full ignores it.

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
from ops.cloud.build_torrent import TRACKERS, auto_piece_size, bencode  # noqa: E402
from tools.langpack.merge_zim import MergeError  # noqa: E402
from tools.langpack.recipe2 import FORMAT as RECIPE_FORMAT, ChunkReader  # noqa: E402

INDEX_FORMAT = "streetzim-webseed/1"
REMOTE_MIN = 256 << 10       # shorter base/pack segments are served from blobs
BLOB_MAX = 4 << 20           # a blob is fetched whole by the worker and kept in memory
BLOB_RE = re.compile(r"^ws-[0-9a-f]{32}\.bin$")
ITEM_RE = re.compile(r"^streetzim-[a-z0-9-]+$")
FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.zim$")
REGION_RE = re.compile(r"^[a-z0-9-]+$")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]+)?$")
SAMPLES = 8                  # reused base hashes checked against the local base


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
                lang: str, webseed: str) -> tuple[dict, dict[str, bytes], dict]:
    """The worker's segment index for one recipe, the blobs it names, and the
    open ChunkReaders (for hashing)."""
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
    for key, p in recipe["packs"].items():
        item, file = item_of(p["url"], f"pack {key}")
        if file != p["file"]:
            raise TorrentError(f"pack {key}: URL file {file} != {p['file']}")
        if key not in pack_paths:
            raise TorrentError(f"pack {key}: {p['file']} is not in the packs folder")
        readers[key] = ChunkReader(pack_paths[key], p["size"], p["chunk"], p["sha256"], f"pack {key}")
        sources[key] = {"item": item, "file": file, "size": p["size"]}
    masks = b.get("mask") or []

    parts: list[list] = []
    blobs: dict[str, bytes] = {}
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
                if n >= REMOTE_MIN:
                    flush()
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
    flush()
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
    }
    return index, blobs, readers


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
                 reuse: set[int] | None = None) -> tuple[bytes, str | None, dict]:
    """SHA-1 of every piece (and the stream's MD5 when nothing is reused)."""
    size = index["size"]
    n_pieces = -(-size // plen)
    reuse = reuse or set()
    md5 = None if reuse else hashlib.md5(usedforsecurity=False)
    out = bytearray()
    hashed = 0
    for k in range(n_pieces):
        if k in reuse:
            assert base_pieces is not None
            out += base_pieces[20 * k:20 * k + 20]
            continue
        start = k * plen
        n = min(plen, size - start)
        h = hashlib.sha1(usedforsecurity=False)
        got = 0
        for chunk in stream.read(start, n):
            h.update(chunk)
            if md5 is not None:
                md5.update(chunk)
            got += len(chunk)
        if got != n:
            raise TorrentError(f"piece {k}: read {got} of {n} bytes")
        hashed += n
        out += h.digest()
    return bytes(out), (md5.hexdigest() if md5 is not None else None), {
        "pieces": n_pieces, "reused": len(reuse), "hashed_bytes": hashed}


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


def make_torrent(index: dict, pieces: bytes, plen: int) -> tuple[bytes, str]:
    info = {"name": index["file"], "piece length": plen, "pieces": pieces, "length": index["size"]}
    torrent = {
        "announce": TRACKERS[0],
        "announce-list": [[t] for t in TRACKERS],
        "comment": f"streetzim — {index['file']} ({index['region']}, {index['lang']})",
        "created by": "streetzim/lang_torrents.py",
        "creation date": int(time.time()),
        "info": info,
        # BEP 19: the build worker serves this file virtually from the base
        # and packs on archive.org, so it downloads with no peer online.
        "url-list": [index["webseed"]],
    }
    return bencode(torrent), hashlib.sha1(bencode(info)).hexdigest()


def write_atomic(path: str, data: bytes):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def torrent_for(recipe_path: str, region: dict, lang: str, base_path: str, pack_paths: dict[str, str],
                webseed: str, base_torrent: str | None = None, full: bool = False,
                out_dir: str | None = None) -> dict:
    recipe = json.load(open(recipe_path, encoding="utf-8"))
    t0 = time.perf_counter()
    index, blobs, readers = build_index(recipe, region, base_path, pack_paths, lang, webseed)
    plen = auto_piece_size(index["size"])
    stream = Stream(index, blobs, readers)
    reuse: set[int] = set()
    base_pieces = None
    why = "--full" if full else "no base torrent"
    if base_torrent and not full:
        base_pieces, why = load_base_torrent(base_torrent, recipe["base"]["file"], recipe["base"]["size"], plen)
        if base_pieces is not None:
            reuse = reusable_pieces(index, plen)
            # the reused hashes must be this base's: check a spread of them
            picks = sorted(reuse)
            sample = sorted({picks[int(i * (len(picks) - 1) / max(1, SAMPLES - 1))] for i in range(SAMPLES)}) if picks else []
            for k in sample:
                start = k * plen
                h = hashlib.sha1(b"".join(stream.read(start, min(plen, index["size"] - start))),
                                 usedforsecurity=False).digest()
                if h != base_pieces[20 * k:20 * k + 20]:
                    reuse, base_pieces = set(), None
                    why = f"base torrent piece {k} does not match the local base"
                    break
            else:
                why = f"reused {len(reuse)} base pieces (sample of {len(sample)} checked)"
    pieces, md5, st = piece_hashes(index, stream, plen, base_pieces, reuse)
    if md5 is not None and recipe.get("file_md5") and md5 != recipe["file_md5"]:
        raise TorrentError(f"{lang}: the merged stream's MD5 {md5} != recipe file_md5 {recipe['file_md5']}")
    data, infohash = make_torrent(index, pieces, plen)
    index["piece_length"] = plen
    index["infohash"] = infohash
    index["torrent"] = f"{lang}.torrent"
    out_dir = out_dir or os.path.dirname(recipe_path)
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
            "md5_checked": md5 is not None, "reuse": why, **st, "s": round(time.perf_counter() - t0, 2)}


def verify_entry(out_dir: str, region: dict, entry: dict, recipe: dict, base_path: str,
                 pack_paths: dict[str, str], sample_pieces: int = 4) -> list[str]:
    """What is wrong with a menu entry's torrent files (site_check.py), [] if
    nothing: the index is rebuilt from the recipe and must be the stored one,
    every blob must be on disk with its SHA-256, the torrent must describe the
    recipe's file with the index's webseed and the entry's infohash, and a
    sample of pieces (first, last, spread) must hash as the torrent says."""
    L = entry["lang"]
    fails: list[str] = []
    try:
        ix = json.load(open(os.path.join(out_dir, f"{L}.ws.json"), encoding="utf-8"))
        origin = ix["webseed"].split("/merged/", 1)[0]
        want, blobs, readers = build_index(recipe, region, base_path, pack_paths, L, origin)
        for k in ("format", "region", "lang", "uuid", "file", "size", "md5", "file_md5", "sources", "blobs",
                  "parts", "webseed"):
            if ix.get(k) != want.get(k):
                fails.append(f"{L}: {L}.ws.json {k} != the index rebuilt from the recipe")
        for sha in blobs:
            p = os.path.join(out_dir, blob_name(sha))
            if not os.path.exists(p) or hashlib.sha256(open(p, "rb").read()).hexdigest() != sha:
                fails.append(f"{L}: blob {blob_name(sha)} missing or not its SHA-256")
        t = bdecode(open(os.path.join(out_dir, f"{L}.torrent"), "rb").read())
        info = t[b"info"]
        plen = auto_piece_size(recipe["size"])
        if (info[b"name"].decode(), info[b"length"], info[b"piece length"]) != (want["file"], recipe["size"], plen):
            fails.append(f"{L}: torrent name/length/piece length != the recipe's file")
        if len(info[b"pieces"]) != 20 * -(-recipe["size"] // plen):
            fails.append(f"{L}: torrent piece count")
        if [u.decode() for u in t.get(b"url-list", [])] != [want["webseed"]]:
            fails.append(f"{L}: torrent webseed != {want['webseed']}")
        ih = hashlib.sha1(bencode(info)).hexdigest()
        if ih != entry.get("infohash") or ih != ix.get("infohash"):
            fails.append(f"{L}: infohash {ih} != the entry's/index's")
        if entry.get("torrent", "").rsplit("/", 1)[-1] != f"{L}.torrent":
            fails.append(f"{L}: entry torrent path {entry.get('torrent')}")
        if not fails:
            stream = Stream(want, blobs, readers)
            n = len(info[b"pieces"]) // 20
            for k in sorted({0, n - 1, *[(n - 1) * i // max(1, sample_pieces - 1) for i in range(sample_pieces)]}):
                start = k * plen
                h = hashlib.sha1(b"".join(stream.read(start, min(plen, recipe["size"] - start))),
                                 usedforsecurity=False).digest()
                if h != info[b"pieces"][20 * k:20 * k + 20]:
                    fails.append(f"{L}: torrent piece {k} does not hash as the merged bytes do")
                    break
    except (OSError, KeyError, ValueError, TypeError, TorrentError, MergeError) as e:
        fails.append(f"{L}: torrent files unreadable or inconsistent ({e})")
    return fails


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
    a = ap.parse_args(argv)
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
    results: dict[str, dict] = {}
    rc = 0
    for m in region["menu"]:
        L = m["lang"]
        if only is not None and L not in only:
            continue
        rpath = os.path.join(a.out_dir, os.path.basename(m["recipe"]))
        try:
            recipe_packs = json.load(open(rpath, encoding="utf-8"))["packs"]
            pack_paths = {k: packs[p["file"]] for k, p in recipe_packs.items() if p["file"] in packs}
            res = torrent_for(rpath, region, L, a.base, pack_paths, a.webseed, a.base_torrent, a.full)
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
