#!/usr/bin/env python3
"""A live map with today's viewer, served by the build worker, with no new upload.

    fresh_viewer.py REGION ZIM --item ITEM --lang L --md5 MD5 [--etag ETAG]
                    --out DIR --webseed ORIGIN [--viewer-root REPO] [--rev REV]
                    [--work DIR] [--gates CMD] [--keep]

ZIM is the local copy of the file archive.org serves for the map (ITEM: its
archive.org item; checked: same size and MD5 as the item lists, and the
ETag the worker sends as If-Range). Its three viewer files sit in fixed-size
uncompressed slots (cloud/viewer_slots.py), so the map with the viewer of
VIEWER_ROOT (default: this checkout) is the same bytes but for those slots,
the UUID and the trailing MD5:

1. tools/langpack/swap_base_viewer.py makes that file in WORK: a reflink copy
   of ZIM, the slots written, a fresh UUID derived from the old one and the
   viewer's code (Kiwix tells books apart by UUID, so it is a new book;
   a rerun makes the same file), the MD5; every item but the viewer checked
   byte-identical; the file is named after the viewer code its About line
   shows (<base>.v<code>.zim), so a download says which viewer it has;
2. GATES (a command run with the file's path; default
   tools/langpack/fresh_viewer_gates.sh) checks it as Kiwix runs it;
3. into OUT (on the site: build/recipes/REGION-viewer/, a folder of its own:
   ops/langpack/publish_site.sh replaces build/recipes/REGION/ whole), for
   the build worker (build-proxy/merged-webseed.js, which serves it as
   /merged/REGION-viewer/L/<uuid>/<file>), the segment index L.ws.json
   (streetzim-webseed/1: the archive.org file's byte ranges, and blobs
   ws-<sha>.bin for the bytes that differ), a torrent L.torrent (its webseed
   the worker), and fresh.json, which web/generate.py reads to point the
   map's Download at the worker while the map's live file is still ZIM
   (archive.org's file when the worker cannot serve it).

The index's bytes are read once, from ZIM and the blobs, for the torrent's
pieces and two MD5s: the whole file's (fresh.json "file_md5") and that of all
but its last 16 bytes, which must equal those 16 bytes (the ZIM checksum the
composed file was given): so the worker serves exactly the file that passed
the gates. The composed file is deleted at the end (--keep keeps it).
docs/fresh-viewer.md has the why and the rollout.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid as uuidlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from cloud.viewer_slots import VIEWER_FILES, slot_needle, viewer_code  # noqa: E402
from tools.langpack.lang_torrents import (  # noqa: E402
    BLOB_RE, FILE_RE, ITEM_RE, LANG_RE, REGION_RE, auto_piece_size, blob_name, costs, make_torrent, webseed_url,
    write_atomic)

FORMAT = "streetzim-fresh-viewer/1"
INDEX_FORMAT = "streetzim-webseed/1"
HEADER_LEN = 80              # the ZIM header; the UUID is bytes 8-24
MD5_LEN = 16                 # libzim's checksum: the MD5 of everything before it
JOIN = 64                    # changed runs this close together are one blob
PY = sys.executable


class FreshError(Exception):
    pass


def viewer_rev(repo: str) -> str:
    """The viewer's revision: the last commit that changed resources/viewer,
    which must have no uncommitted change (the new UUID and the file's name
    come from the revision: other bytes under the same ones would be a
    different book Kiwix takes for the same)."""
    r = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%h", "--", "resources/viewer"],
                       capture_output=True, text=True)
    rev = r.stdout.strip()
    if r.returncode or not re.fullmatch(r"[0-9a-f]{7,40}", rev):
        raise FreshError(f"no viewer revision in {repo} ({r.stderr.strip() or 'git log printed nothing'})")
    d = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--", "resources/viewer"],
                       capture_output=True, text=True)
    if d.returncode or d.stdout.strip():
        raise FreshError(f"resources/viewer in {repo} has uncommitted changes: commit them first\n{d.stdout.strip()[:500]}")
    return rev


ETAG = re.compile(r'^"([0-9a-f]+)-([0-9a-f]+)"$')    # archive.org's: "<mtime hex>-<size hex>"


def check_etag(etag: str, size: int) -> str:
    """archive.org's ETag for the live file, as the worker sends it in
    If-Range: quotes and all, its size part this file's (a wrong one makes
    archive.org answer 200 to every range, and the worker calls the file
    replaced)."""
    m = ETAG.match(etag or "")
    if not m:
        raise FreshError(f"--etag {etag!r} is not archive.org's \"<mtime hex>-<size hex>\" (quote it in the shell)")
    if int(m.group(2), 16) != size:
        raise FreshError(f"--etag {etag} names a file of {int(m.group(2), 16)} bytes, not {size}")
    return etag


def fresh_name(zim_file: str, code: str) -> str:
    """osm-taipei-2026-10-29.zim + db752f01 (the viewer code About shows) ->
    osm-taipei-2026-10-29.vdb752f01.zim"""
    stem = zim_file[:-4] if zim_file.endswith(".zim") else zim_file
    return f"{stem}.v{code}.zim"


def md5_file(path: str) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb", buffering=0) as fh:
        while b := fh.read(16 << 20):
            h.update(b)
    return h.hexdigest()


def slot_windows(path: str) -> list[tuple[int, int]]:
    """(start, length) of the three viewer slots (the content and its
    padding), found by their markers; the same offsets in the base and the
    composed file, since the patch writes in place."""
    import mmap
    from cloud.patch_viewer_inplace import _find_slot
    out = []
    with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        for name in VIEWER_FILES:
            got = _find_slot(mm, name)
            if got is None:
                raise FreshError(f"{os.path.basename(path)} has no {name} slot ({slot_needle(name)!r}): "
                                 "built before viewer slots (2026-09-20); it needs a rebuild")
            start, slot_len = got[0], got[1]
            out.append((start, slot_len))
    return sorted(out)


def changed_runs(base: str, fresh: str, windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The byte runs in which `fresh` differs from `base`, looked for in the
    windows (the header, the slots, the checksum); runs closer than JOIN
    bytes are joined."""
    runs: list[list[int]] = []
    with open(base, "rb") as fa, open(fresh, "rb") as fb:
        for start, n in sorted(windows):
            fa.seek(start)
            fb.seek(start)
            a, b = fa.read(n), fb.read(n)
            if len(a) != n or len(b) != n:
                raise FreshError(f"window {start}+{n} runs past the end of the file")
            i = 0
            while i < n:
                if a[i] == b[i]:
                    i += 1
                    continue
                j = i
                while j < n and a[j] != b[j]:
                    j += 1
                lo, hi = start + i, start + j
                if runs and lo - runs[-1][1] < JOIN:
                    runs[-1][1] = max(runs[-1][1], hi)
                else:
                    runs.append([lo, hi])
                i = j
    return [(lo, hi - lo) for lo, hi in runs]


def build_index(region: str, lang: str, item: str, base_file: str, base_size: int, etag: str | None,
                fresh_path: str, fresh_file: str, uuid: str, runs: list[tuple[int, int]],
                webseed: str) -> tuple[dict, dict[str, bytes]]:
    """The segment index of the composed file: the base's bytes at their own
    offsets, and a blob for each changed run."""
    if not (REGION_RE.match(region) and LANG_RE.match(lang) and ITEM_RE.match(item)
            and FILE_RE.match(base_file) and FILE_RE.match(fresh_file)):
        raise FreshError(f"unusable name: {region}/{lang} {item}/{base_file} -> {fresh_file}")
    parts: list[list] = []
    blobs: dict[str, bytes] = {}
    pos = 0
    with open(fresh_path, "rb") as fh:
        for start, n in runs:
            if start > pos:
                parts.append([pos, start - pos, "s", "base", pos])
            fh.seek(start)
            data = fh.read(n)
            sha = hashlib.sha256(data).hexdigest()
            blobs[sha] = data
            parts.append([start, n, "x", sha, 0])
            pos = start + n
    if pos < base_size:
        parts.append([pos, base_size - pos, "s", "base", pos])
    src = {"item": item, "file": base_file, "size": base_size}
    if etag:
        src["etag"] = etag
    index = {
        "format": INDEX_FORMAT, "region": region, "lang": lang, "uuid": uuid,
        "file": fresh_file, "size": base_size, "md5": None, "file_md5": None,
        "sources": {"base": src},
        "blobs": {sha: {"file": blob_name(sha), "size": len(d)} for sha, d in blobs.items()},
        "parts": parts,
        "webseed": webseed_url(webseed, region, lang, uuid, fresh_file),
        "remote_min": 0,
    }
    index["subrequests"] = costs(index)
    return index, blobs


def read_index(index: dict, blobs: dict[str, bytes], base_path: str, plen: int) -> tuple[bytes, str, str, bytes]:
    """Every byte of the indexed file, in order, once: the torrent's piece
    hashes, the whole file's MD5, the MD5 of all but its last 16 bytes, and
    those 16 bytes."""
    size = index["size"]
    whole = hashlib.md5(usedforsecurity=False)
    body = hashlib.md5(usedforsecurity=False)
    pieces = bytearray()
    piece = hashlib.sha1(usedforsecurity=False)
    in_piece = 0
    tail = b""
    done = 0
    with open(base_path, "rb", buffering=0) as fh:
        for _start, n, kind, ref, off in index["parts"]:
            if kind == "x":
                chunks = [blobs[ref][off:off + n]]
            else:
                fh.seek(off)
                chunks = (fh.read(min(4 << 20, n - k)) for k in range(0, n, 4 << 20))
            for c in chunks:
                if not c:
                    raise FreshError(f"short read at {done}")
                whole.update(c)
                keep = max(0, min(len(c), size - MD5_LEN - done))
                body.update(c[:keep])
                if done + len(c) > size - MD5_LEN:
                    tail += c[keep:]
                i = 0
                while i < len(c):
                    take = min(plen - in_piece, len(c) - i)
                    piece.update(c[i:i + take])
                    in_piece += take
                    i += take
                    if in_piece == plen:
                        pieces += piece.digest()
                        piece = hashlib.sha1(usedforsecurity=False)
                        in_piece = 0
                done += len(c)
    if in_piece:
        pieces += piece.digest()
    if done != size:
        raise FreshError(f"read {done} bytes of {size}")
    return bytes(pieces), whole.hexdigest(), body.hexdigest(), tail


def compose(zim: str, work: str, repo: str, rev: str, name: str, full_copy: bool = False) -> tuple[str, dict]:
    """swap_base_viewer.py: the map with the viewer of `repo`, in `work` (a
    reflink copy of ZIM; full_copy: every byte copied where the filesystem
    cannot reflink)."""
    os.makedirs(work, exist_ok=True)
    out = os.path.join(work, name)
    if os.path.exists(out):
        os.unlink(out)
    r = subprocess.run([PY, os.path.join(ROOT, "tools", "langpack", "swap_base_viewer.py"), zim, out, repo, rev]
                       + (["--allow-full-copy"] if full_copy else []),
                       capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    tail = (r.stdout + r.stderr).strip()
    if r.returncode or "RESULT PASS" not in r.stdout:
        raise FreshError(f"swap_base_viewer.py failed (rc {r.returncode}): {tail[-1500:]}")
    m = re.search(r"^\{.*^\}", r.stdout, re.M | re.S)
    res = json.loads(m.group(0)) if m else {}
    return out, res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("region")
    ap.add_argument("zim", help="the local copy of the map's live archive.org file")
    ap.add_argument("--item", required=True, help="its archive.org item (streetzim-<region>)")
    ap.add_argument("--lang", required=True, help="the map's language (the index is <lang>.ws.json)")
    ap.add_argument("--out", required=True, help="where the index, blobs, torrent and fresh.json go")
    ap.add_argument("--webseed", required=True, help="the build worker's origin")
    ap.add_argument("--md5", required=True, help="the MD5 archive.org lists for the live file")
    ap.add_argument("--etag", required=True, help="archive.org's ETag for it (\"<mtime hex>-<size hex>\", quotes included)")
    ap.add_argument("--viewer-root", default=ROOT, help="the checkout whose resources/viewer goes in")
    ap.add_argument("--rev", help="the viewer's revision (default: the last commit to resources/viewer)")
    ap.add_argument("--work", default=None, help="where the composed file is made (default: beside ZIM)")
    ap.add_argument("--gates", default=os.path.join(ROOT, "tools", "langpack", "fresh_viewer_gates.sh"),
                    help="run with the composed file's path; '-' for none (tests)")
    ap.add_argument("--keep", action="store_true", help="keep the composed file")
    ap.add_argument("--allow-full-copy", action="store_true",
                    help="copy every byte of ZIM where WORK cannot reflink it (slow; the disk holds it twice)")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except FreshError as e:
        print(f"fresh_viewer: {a.region}: {e}", file=sys.stderr, flush=True)
        return 1


def run(a) -> int:
    zim, base_file = os.path.abspath(a.zim), os.path.basename(a.zim)
    base_size = os.path.getsize(zim)
    lang = a.lang
    site_region = f"{a.region}-viewer"          # the worker's folder for it: build/recipes/<region>-viewer/
    etag = check_etag(a.etag, base_size)
    print(f"fresh_viewer: {a.region}: checking {base_file} against archive.org's MD5", flush=True)
    if md5_file(zim) != a.md5.lower():
        raise FreshError(f"{zim} is not the file archive.org serves (MD5 {a.md5})")
    rev = a.rev or viewer_rev(a.viewer_root)
    if not re.fullmatch(r"[0-9a-f]{7,40}", rev):
        raise FreshError(f"viewer revision {rev!r} is not a commit hash")
    with open(os.path.join(a.viewer_root, "resources", "viewer", "index.html"), "rb") as fh:
        code = viewer_code(fh.read())
    if not code:
        raise FreshError(f"{a.viewer_root}/resources/viewer/index.html has no viewer code (SZ_VIEWER_BUILD): "
                         "build it with tools/build_viewer.py")
    name = fresh_name(base_file, code)
    work = a.work or os.path.join(os.path.dirname(zim), "fresh-viewer")
    print(f"fresh_viewer: {a.region}: composing {name} (viewer {code}, revision {rev})", flush=True)
    # the new UUID follows the code too: the same viewer bytes are the same book, whatever the commit
    path, swap = compose(zim, work, a.viewer_root, code, name, a.allow_full_copy)
    published = False
    try:
        from tools.langpack.recipe2 import zim_viewer_code
        if zim_viewer_code(path) != code:
            raise FreshError(f"{name} carries viewer {zim_viewer_code(path) or '(none)'}, not {code}")
        if a.gates != "-":
            print(f"fresh_viewer: {a.region}: gates: {a.gates} {path}", flush=True)
            g = subprocess.run([a.gates, path], text=True)
            if g.returncode:
                raise FreshError(f"the gates failed (rc {g.returncode}); nothing published")
        uuid = str(uuidlib.UUID(bytes=open(path, "rb").read(HEADER_LEN)[8:24]))
        if swap.get("new_uuid") and swap["new_uuid"] != uuid:
            raise FreshError(f"UUID {uuid} is not the one swap_base_viewer.py wrote ({swap['new_uuid']})")
        windows = [(0, HEADER_LEN)] + slot_windows(path) + [(base_size - MD5_LEN, MD5_LEN)]
        runs = changed_runs(zim, path, windows)
        index, blobs = build_index(site_region, lang, a.item, base_file, base_size, etag, path, name, uuid,
                                   runs, a.webseed)
        plen = auto_piece_size(base_size)
        pieces, file_md5, body_md5, tail = read_index(index, blobs, zim, plen)
        if body_md5 != tail.hex():
            raise FreshError(f"the indexed bytes do not carry their own checksum ({body_md5} != {tail.hex()}): "
                             "they are not the composed file")
        index["md5"], index["file_md5"] = tail.hex(), file_md5
        torrent, infohash = make_torrent(index, pieces, plen)
        index["piece_length"], index["infohash"], index["torrent"] = plen, infohash, f"{lang}.torrent"
        os.makedirs(a.out, exist_ok=True)
        for sha, data in blobs.items():
            write_atomic(os.path.join(a.out, blob_name(sha)), data)
        write_atomic(os.path.join(a.out, f"{lang}.torrent"), torrent)
        write_atomic(os.path.join(a.out, f"{lang}.ws.json"), json.dumps(index, indent=1).encode())
        fresh = {
            "format": FORMAT, "region": a.region, "site_region": site_region, "lang": lang, "viewer": rev,
            "viewer_code": code,
            "base": {"item": a.item, "file": base_file, "size": base_size, "md5": a.md5.lower(),
                     "etag": etag},
            "file": name, "size": base_size, "uuid": uuid, "md5": index["md5"], "file_md5": file_md5,
            "webseed": index["webseed"], "torrent": index["torrent"], "infohash": infohash,
            "parts": len(index["parts"]), "blob_bytes": sum(len(d) for d in blobs.values()),
            "subrequests": index["subrequests"], "gated": a.gates != "-",
            "made": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        write_atomic(os.path.join(a.out, "fresh.json"), json.dumps(fresh, indent=1).encode())
        published = True
        # an earlier viewer's blobs, torrent and index go: only this one is served
        for f in os.listdir(a.out):
            stale = (BLOB_RE.match(f) and f[3:-4] not in {s[:32] for s in blobs}) \
                or ((f.endswith(".ws.json") or f.endswith(".torrent")) and f not in (f"{lang}.ws.json", f"{lang}.torrent"))
            if stale:
                os.unlink(os.path.join(a.out, f))
        print(json.dumps(fresh), flush=True)
        print(f"fresh_viewer: {a.region}: {name}: {len(index['parts'])} parts, "
              f"{fresh['blob_bytes']} bytes of blobs, {index['subrequests']['whole']} subrequests for the whole file",
              flush=True)
        return 0
    finally:
        # the composed file and its lineage sidecar go (--keep keeps them);
        # the gates' logs stay when they failed
        if not a.keep:
            for p in (path, path + ".parent.json"):
                if os.path.exists(p):
                    os.unlink(p)
            if published:
                shutil.rmtree(path + ".gates", ignore_errors=True)
            if os.path.isdir(work) and not os.listdir(work):
                shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
