"""Raw ZIM file format: header, dirents, clusters, and a writer that can copy
clusters byte-for-byte.

Why this exists: python-libzim reads ZIMs at the *entry* level and writes them
only through a Creator that re-compresses everything. Deriving a variant
(drop satellite, cap tiles at z13) without a re-pack needs the level below:
which dirents point into which clusters, where each cluster's bytes are, and
the ability to emit a new dirent table over clusters that were copied
verbatim. The format is small (openzim.org/wiki/ZIM_file_format) and this
module implements exactly the parts streetzim needs.

Layout written by :class:`ZimWriter`::

    header(80) | mime list | clusters... | url ptr list | title ptr list |
    cluster ptr list | md5(everything before)

Readers do not care about section order; libzim follows the header offsets.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

import numpy as np

MAGIC = 72173914
HEADER_LEN = 80
HEADER_FMT = "<IHH16sIIQQQQIIQ"

COMP_NONE = 1
COMP_ZSTD = 5
COMP_XZ = 4
EXTENDED_FLAG = 0x10

MIME_REDIRECT = 0xFFFF
MIME_LINKTARGET = 0xFFFE
MIME_DELETED = 0xFFFD

NO_MAIN_PAGE = 0xFFFFFFFF


# ---------------------------------------------------------------- header --

@dataclass
class Header:
    major: int
    minor: int
    uuid: bytes
    entry_count: int
    cluster_count: int
    url_ptr_pos: int
    title_ptr_pos: int
    cluster_ptr_pos: int
    mime_list_pos: int
    main_page: int
    layout_page: int
    checksum_pos: int

    @classmethod
    def parse(cls, buf: bytes) -> "Header":
        (magic, major, minor, uuid, entry_count, cluster_count, url_ptr_pos,
         title_ptr_pos, cluster_ptr_pos, mime_list_pos, main_page, layout_page,
         checksum_pos) = struct.unpack_from(HEADER_FMT, buf, 0)
        if magic != MAGIC:
            raise ValueError(f"not a ZIM file (magic {magic:#x})")
        return cls(major, minor, uuid, entry_count, cluster_count, url_ptr_pos,
                   title_ptr_pos, cluster_ptr_pos, mime_list_pos, main_page,
                   layout_page, checksum_pos)

    def pack(self) -> bytes:
        return struct.pack(HEADER_FMT, MAGIC, self.major, self.minor, self.uuid,
                           self.entry_count, self.cluster_count, self.url_ptr_pos,
                           self.title_ptr_pos, self.cluster_ptr_pos,
                           self.mime_list_pos, self.main_page, self.layout_page,
                           self.checksum_pos)


# ---------------------------------------------------------------- dirent --

class _Short(Exception):
    """Window too small to hold the whole dirent."""

@dataclass
class Dirent:
    """One directory entry. ``mime`` is an index into the MIME list, or
    MIME_REDIRECT. Redirects carry ``redirect``; items carry ``cluster`` and
    ``blob``. ``url`` and ``title`` are raw bytes (UTF-8)."""
    mime: int
    namespace: str
    revision: int
    url: bytes
    title: bytes
    parameter: bytes = b""
    redirect: int = -1
    cluster: int = -1
    blob: int = -1

    @property
    def is_redirect(self) -> bool:
        return self.mime == MIME_REDIRECT

    @property
    def path(self) -> str:
        return self.url.decode("utf-8", "replace")

    @property
    def effective_title(self) -> bytes:
        return self.title if self.title else self.url

    @classmethod
    def parse(cls, src, off: int) -> "Dirent":
        """Parse from a bytes-like or a Source (anything sliceable). Reads a
        small window and grows it until the two NUL terminators are found."""
        n = 512
        while True:
            buf = src[off:off + n]
            if not isinstance(buf, (bytes, bytearray)):
                buf = bytes(buf)
            try:
                return cls._parse_bytes(buf, 0)
            except _Short:
                if off + n >= getattr(src, "size", len(src)) and n > len(buf):
                    raise ValueError(f"truncated dirent at {off}")
                n *= 4

    @classmethod
    def _parse_bytes(cls, buf: bytes, off: int) -> "Dirent":
        if len(buf) < off + 16:
            raise _Short()
        mime, plen, ns = struct.unpack_from("<HBc", buf, off)
        revision, = struct.unpack_from("<I", buf, off + 4)
        p = off + 8
        redirect = cluster = blob = -1
        if mime == MIME_REDIRECT:
            redirect, = struct.unpack_from("<I", buf, p)
            p += 4
        elif mime in (MIME_LINKTARGET, MIME_DELETED):
            pass
        else:
            cluster, blob = struct.unpack_from("<II", buf, p)
            p += 8
        e = buf.find(b"\0", p)
        if e < 0:
            raise _Short()
        url = bytes(buf[p:e])
        p = e + 1
        e = buf.find(b"\0", p)
        if e < 0:
            raise _Short()
        title = bytes(buf[p:e])
        p = e + 1
        if len(buf) < p + plen:
            raise _Short()
        param = bytes(buf[p:p + plen])
        return cls(mime, ns.decode("latin-1"), revision, url, title, param,
                   redirect, cluster, blob)

    def pack(self) -> bytes:
        head = struct.pack("<HBcI", self.mime, len(self.parameter),
                           self.namespace.encode("latin-1"), self.revision)
        if self.is_redirect:
            body = struct.pack("<I", self.redirect)
        elif self.mime in (MIME_LINKTARGET, MIME_DELETED):
            body = b""
        else:
            body = struct.pack("<II", self.cluster, self.blob)
        return head + body + self.url + b"\0" + self.title + b"\0" + self.parameter


def sort_key_url(d: Dirent) -> tuple:
    return (d.namespace, d.url)


def sort_key_title(d: Dirent) -> tuple:
    return (d.namespace, d.effective_title)


# ---------------------------------------------------------------- reader --

def _decompress(comp: int, data) -> bytes:
    if comp == COMP_NONE:
        return bytes(data)
    if comp == COMP_ZSTD:
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj().decompress(bytes(data))
    if comp == COMP_XZ:
        import lzma
        return lzma.decompress(bytes(data))
    raise ValueError(f"unsupported cluster compression {comp}")


def parse_cluster_body(body: bytes) -> tuple[list[int], bool]:
    """Return (offsets, extended) for a decompressed cluster body (without
    the leading info byte). offsets has n+1 entries."""
    raise NotImplementedError  # replaced below (needs the info byte)


@dataclass
class ClusterInfo:
    index: int
    offset: int          # file offset of the info byte
    size: int            # bytes in the file, including the info byte
    compression: int
    extended: bool

    @property
    def compressed(self) -> bool:
        return self.compression != COMP_NONE


class MmapSource:
    """Local file, read through mmap. Slicing returns bytes."""

    def __init__(self, path: str):
        import mmap
        self.path = path
        self.size = os.path.getsize(path)
        self._fh = open(path, "rb")
        self.mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)

    def __getitem__(self, k):
        return self.mm[k]

    def find(self, sub: bytes, start: int) -> int:
        return self.mm.find(sub, start)

    def view(self, start: int, end: int) -> memoryview:
        return memoryview(self.mm)[start:end]

    def close(self):
        self.mm.close()
        self._fh.close()


class HttpSource:
    """A ZIM behind an HTTP server that honours Range (archive.org does).
    Reads go through a block cache; :meth:`prefetch` pulls one contiguous
    region (the dirent table) in a single request. Enough to inventory a
    23 GB continent by downloading a few hundred MB of tables."""

    BLOCK = 1 << 20

    def __init__(self, url: str, verbose: bool = False):
        import urllib.request
        self.url = url
        self.path = url
        self._verbose = verbose
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as resp:
            self.size = int(resp.headers["Content-Length"])
            self.url = resp.geturl()   # follow the redirect once, then hit the node directly
        self._blocks: dict[int, bytes] = {}
        self._regions: list[tuple[int, bytes]] = []
        self._region_starts: list[int] = []
        self.bytes_fetched = 0
        self.requests = 0

    def _fetch(self, start: int, end: int) -> bytes:
        import urllib.request
        end = min(end, self.size)
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={start}-{end - 1}"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                break
            except Exception as ex:  # noqa: BLE001
                if attempt == 3:
                    raise
                import time
                time.sleep(2 ** attempt)
        self.requests += 1
        self.bytes_fetched += len(data)
        if self._verbose:
            print(f"    range {start}-{end - 1} ({len(data)/1e6:.1f} MB)", flush=True)
        return data

    def prefetch(self, start: int, end: int):
        self._add_region(start, self._fetch(start, end))

    def _add_region(self, start: int, data: bytes):
        import bisect
        i = bisect.bisect_left(self._region_starts, start)
        self._region_starts.insert(i, start)
        self._regions.insert(i, (start, data))

    def prefetch_many(self, ranges: list[tuple[int, int]], per_request: int = 64):
        """Fetch many small [start, end) pieces with multipart range requests
        (archive.org honours them). Used for the one-byte cluster info headers
        and the offset tables of raw clusters."""
        import re
        import urllib.request
        ranges = sorted(set((int(a), int(b)) for a, b in ranges if b > a))
        for i in range(0, len(ranges), per_request):
            batch = ranges[i:i + per_request]
            if len(batch) == 1:
                a, b = batch[0]
                self._add_region(a, self._fetch(a, b))
                continue
            spec = ",".join(f"{a}-{b - 1}" for a, b in batch)
            req = urllib.request.Request(self.url, headers={"Range": f"bytes={spec}"})
            # archive.org occasionally accepts a multipart request and then
            # never sends the body; a short socket timeout plus a retry on a
            # fresh connection is what gets past it.
            for attempt in range(5):
                try:
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        ctype = resp.headers.get("Content-Type", "")
                        body = resp.read()
                    break
                except Exception as ex:  # noqa: BLE001
                    if self._verbose:
                        print(f"    multipart retry {attempt + 1}: {type(ex).__name__}", flush=True)
                    if attempt == 4:
                        raise
            self.requests += 1
            self.bytes_fetched += len(body)
            m = re.search(r'boundary=("?)([^";]+)\1', ctype)
            if resp.status == 206 and m:
                bnd = ("--" + m.group(2)).encode()
                for part in body.split(bnd)[1:]:
                    if part.startswith(b"--"):
                        break
                    head, _, data = part.partition(b"\r\n\r\n")
                    cr = re.search(rb"Content-Range:\s*bytes\s+(\d+)-(\d+)", head, re.I)
                    if not cr:
                        continue
                    a, b = int(cr.group(1)), int(cr.group(2))
                    self._add_region(a, data[:b - a + 1])
            elif resp.status == 206:
                a, b = batch[0]
                self._add_region(a, body)
                self.prefetch_many(batch[1:], per_request)
            else:
                # server ignored Range and sent the whole file: keep what we need
                for a, b in batch:
                    self._add_region(a, body[a:b])
            if self._verbose:
                print(f"    multipart {len(batch)} ranges ({len(body)/1e3:.0f} KB)", flush=True)

    def _read(self, start: int, end: int) -> bytes:
        import bisect
        i = bisect.bisect_right(self._region_starts, start) - 1
        if i >= 0:
            rs, data = self._regions[i]
            if end <= rs + len(data):
                return data[start - rs:end - rs]
        out = bytearray()
        b0, b1 = start // self.BLOCK, (end - 1) // self.BLOCK
        for b in range(b0, b1 + 1):
            blk = self._blocks.get(b)
            if blk is None:
                blk = self._blocks[b] = self._fetch(b * self.BLOCK, (b + 1) * self.BLOCK)
            lo = max(start, b * self.BLOCK) - b * self.BLOCK
            hi = min(end, (b + 1) * self.BLOCK) - b * self.BLOCK
            out += blk[lo:hi]
        return bytes(out)

    def __getitem__(self, k):
        if isinstance(k, slice):
            start = k.start or 0
            stop = self.size if k.stop is None else k.stop
            return self._read(start, stop)
        return self._read(k, k + 1)[0]

    def find(self, sub: bytes, start: int) -> int:
        # only used for NUL-terminated strings: scan in 4 KiB steps
        pos = start
        while pos < self.size:
            chunk = self._read(pos, min(pos + 4096, self.size))
            i = chunk.find(sub)
            if i >= 0:
                return pos + i
            pos += 4096 - len(sub) + 1
        return -1

    def view(self, start: int, end: int):
        return memoryview(self._read(start, end))

    def close(self):
        pass


class ZimReader:
    """Random access to the raw structure of a ZIM file (local path or
    http(s) URL)."""

    def __init__(self, path: str, *, verbose: bool = False):
        self.path = path
        self.remote = path.startswith(("http://", "https://"))
        self.src = HttpSource(path, verbose) if self.remote else MmapSource(path)
        self.mm = self.src
        self.size = self.src.size
        self.header = Header.parse(self.src[:HEADER_LEN])
        h = self.header
        self.mimes = self._read_mimes(h.mime_list_pos)
        self.url_ptrs = np.frombuffer(
            self.src[h.url_ptr_pos:h.url_ptr_pos + 8 * h.entry_count], dtype="<u8")
        self.cluster_ptrs = np.frombuffer(
            self.src[h.cluster_ptr_pos:h.cluster_ptr_pos + 8 * h.cluster_count], dtype="<u8")
        if self.remote and h.entry_count:
            # dirents are contiguous; pull them in one request
            lo = int(self.url_ptrs.min())
            hi = int(self.url_ptrs.max()) + 4096
            self.src.prefetch(lo, min(hi, self.size))
        self.title_ptrs = None
        # libzim >= 8 may write an absent title index (all ones) and rely on
        # X/listing/titleOrdered/* instead; zimru writes a real one.
        self.has_title_ptrs = 0 < h.title_ptr_pos < self.size and h.title_ptr_pos != h.url_ptr_pos
        if self.has_title_ptrs:
            self.title_ptrs = np.frombuffer(
                self.src[h.title_ptr_pos:h.title_ptr_pos + 4 * h.entry_count], dtype="<u4")
        self._cluster_ends = self._compute_cluster_ends()
        self._dirent_cache: dict[int, Dirent] = {}
        self._blob_cache: tuple[int, list[int], bytes] | None = None

    def close(self):
        self.src.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _read_mimes(self, pos: int) -> list[str]:
        mimes = []
        p = pos
        while True:
            e = self.mm.find(b"\0", p)
            if e == p:
                break
            mimes.append(self.mm[p:e].decode("utf-8"))
            p = e + 1
        return mimes

    def _compute_cluster_ends(self) -> np.ndarray:
        h = self.header
        boundaries = set(int(x) for x in self.cluster_ptrs)
        boundaries.update([h.url_ptr_pos, h.cluster_ptr_pos, h.mime_list_pos,
                           h.checksum_pos, self.size])
        if self.has_title_ptrs:
            boundaries.add(h.title_ptr_pos)
        arr = np.array(sorted(boundaries), dtype=np.int64)
        idx = np.searchsorted(arr, self.cluster_ptrs.astype(np.int64), side="right")
        return arr[idx]

    # -- dirents --
    def dirent(self, i: int) -> Dirent:
        d = self._dirent_cache.get(i)
        if d is None:
            d = Dirent.parse(self.mm, int(self.url_ptrs[i]))
            self._dirent_cache[i] = d
        return d

    def dirents(self) -> Iterator[tuple[int, Dirent]]:
        mm = self.mm
        for i, off in enumerate(self.url_ptrs.tolist()):
            yield i, Dirent.parse(mm, off)

    def find(self, namespace: str, url: bytes) -> int:
        """Binary search the URL-sorted pointer list. Returns index or -1."""
        lo, hi = 0, self.header.entry_count
        key = (namespace, url)
        while lo < hi:
            mid = (lo + hi) // 2
            d = self.dirent(mid)
            k = (d.namespace, d.url)
            if k < key:
                lo = mid + 1
            elif k > key:
                hi = mid
            else:
                return mid
        return -1

    def mime_of(self, d: Dirent) -> str:
        return self.mimes[d.mime] if d.mime < len(self.mimes) else ""

    # -- clusters --
    def preload_cluster_infos(self):
        """Remote only: fetch every cluster's info byte in a few requests."""
        if self.remote:
            self.src.prefetch_many([(int(o), int(o) + 1) for o in self.cluster_ptrs])

    def preload_raw_tables(self, clusters: list[int]):
        """Remote only: fetch the offset tables of the given raw clusters."""
        if not self.remote or not clusters:
            return
        heads = []
        for c in clusters:
            ci = self.cluster_info(c)
            heads.append((ci.offset + 1, ci.offset + 1 + (8 if ci.extended else 4)))
        self.src.prefetch_many(heads)
        tables = []
        for c in clusters:
            ci = self.cluster_info(c)
            w = 8 if ci.extended else 4
            first = struct.unpack("<Q" if ci.extended else "<I", self.src[ci.offset + 1:ci.offset + 1 + w])[0]
            tables.append((ci.offset + 1, ci.offset + 1 + first))
        self.src.prefetch_many(tables, per_request=64)

    def cluster_info(self, c: int) -> ClusterInfo:
        off = int(self.cluster_ptrs[c])
        info = self.mm[off]
        return ClusterInfo(c, off, int(self._cluster_ends[c]) - off,
                           info & 0x0F, bool(info & EXTENDED_FLAG))

    def cluster_raw(self, c: int) -> memoryview:
        ci = self.cluster_info(c)
        return self.src.view(ci.offset, ci.offset + ci.size)

    def raw_cluster_blob_sizes(self, c: int) -> list[int] | None:
        """Blob sizes of an UNCOMPRESSED cluster from its offset table alone
        (a few KB read, no payload). None for compressed clusters."""
        ci = self.cluster_info(c)
        if ci.compressed:
            return None
        w = 8 if ci.extended else 4
        first = struct.unpack("<Q" if ci.extended else "<I", self.src[ci.offset + 1:ci.offset + 1 + w])[0]
        n = first // w
        table = self.src[ci.offset + 1:ci.offset + 1 + first]
        offs = struct.unpack(f"<{n}{'Q' if ci.extended else 'I'}", table)
        return [offs[i + 1] - offs[i] for i in range(n - 1)]

    def cluster_offsets(self, c: int) -> tuple[list[int], bytes]:
        """Decompress cluster ``c``. Returns (offsets[n+1], body) where body
        is the decompressed payload (offset table + blobs)."""
        if self._blob_cache and self._blob_cache[0] == c:
            return self._blob_cache[1], self._blob_cache[2]
        ci = self.cluster_info(c)
        body = _decompress(ci.compression,
                           self.mm[ci.offset + 1:ci.offset + ci.size])
        offs = _offsets(body, ci.extended)
        self._blob_cache = (c, offs, body)
        return offs, body

    def blob_count(self, c: int) -> int:
        return len(self.cluster_offsets(c)[0]) - 1

    def blob_sizes(self, c: int) -> list[int]:
        offs, _ = self.cluster_offsets(c)
        return [offs[i + 1] - offs[i] for i in range(len(offs) - 1)]

    def blob(self, c: int, b: int) -> bytes:
        offs, body = self.cluster_offsets(c)
        return body[offs[b]:offs[b + 1]]

    def content(self, d: Dirent) -> bytes:
        return self.blob(d.cluster, d.blob)

    def get(self, path: str, namespace: str = "C") -> bytes | None:
        i = self.find(namespace, path.encode("utf-8"))
        if i < 0:
            return None
        d = self.dirent(i)
        while d.is_redirect:
            d = self.dirent(d.redirect)
        return self.content(d)


def _offsets(body: bytes, extended: bool) -> list[int]:
    w = 8 if extended else 4
    first, = struct.unpack_from("<Q" if extended else "<I", body, 0)
    n = first // w
    return list(struct.unpack_from(f"<{n}{'Q' if extended else 'I'}", body, 0))


# ---------------------------------------------------------------- writer --

def zstd_window_log(n: int) -> int:
    """zimru pins windowLog to ceil(log2(payload)) clamped to [10, 27] so the
    browser-side fzstd decoder never allocates a 128 MiB window for a small
    cluster (docs/zim-packaging-gotchas.md #6). Match it."""
    return max(10, min(27, math.ceil(math.log2(max(n, 2)))))


def encode_cluster(blobs: list[bytes], compress: bool, level: int = 22) -> bytes:
    """Build a cluster (info byte + offset table + blobs)."""
    total = sum(len(b) for b in blobs)
    n = len(blobs)
    extended = total + (n + 1) * 4 >= 2 ** 32
    w = 8 if extended else 4
    offs = [(n + 1) * w]
    for b in blobs:
        offs.append(offs[-1] + len(b))
    body = struct.pack(f"<{n + 1}{'Q' if extended else 'I'}", *offs) + b"".join(blobs)
    info = (EXTENDED_FLAG if extended else 0)
    if not compress:
        return bytes([info | COMP_NONE]) + body
    import zstandard
    params = zstandard.ZstdCompressionParameters.from_level(
        level, window_log=zstd_window_log(len(body)), write_content_size=True)
    comp = zstandard.ZstdCompressor(compression_params=params).compress(body)
    return bytes([info | COMP_ZSTD]) + comp


class ZimWriter:
    """Emit a ZIM from a list of dirents and a sequence of cluster byte
    strings. Clusters are streamed to disk as they are produced; the dirent
    tables are written at the end.

    Typical use::

        w = ZimWriter(out_path, mimes, uuid=..., major=6, minor=1)
        w.begin()
        for raw in clusters: w.add_cluster(raw)     # bytes incl. info byte
        w.finish(dirents, main_page=idx)
    """

    def __init__(self, path: str, mimes: list[str], *, uuid: bytes,
                 major: int = 6, minor: int = 1):
        self.path = path
        self.mimes = list(mimes)
        self.uuid = uuid
        self.major = major
        self.minor = minor
        self._fh = None
        self._cluster_offsets: list[int] = []
        self._pos = 0

    def begin(self):
        self._fh = open(self.path, "wb")
        self._fh.write(b"\0" * HEADER_LEN)
        self._pos = HEADER_LEN
        self.mime_list_pos = self._pos
        blob = b"".join(m.encode("utf-8") + b"\0" for m in self.mimes) + b"\0"
        self._write(blob)

    def _write(self, b: bytes):
        self._fh.write(b)
        self._pos += len(b)

    def add_cluster(self, raw) -> int:
        """Append one cluster (info byte + payload). Returns its index."""
        self._cluster_offsets.append(self._pos)
        self._write(bytes(raw) if not isinstance(raw, (bytes, bytearray)) else raw)
        return len(self._cluster_offsets) - 1

    def copy_cluster_from(self, src: ZimReader, c: int) -> int:
        """Copy cluster ``c`` of ``src`` byte-for-byte via the reader's mmap."""
        ci = src.cluster_info(c)
        self._cluster_offsets.append(self._pos)
        mv = src.src.view(ci.offset, ci.offset + ci.size)
        # write in 64 MiB pieces so a multi-GB raw routing cluster does not
        # materialise as one bytes object
        step = 64 << 20
        for s in range(0, len(mv), step):
            self._fh.write(mv[s:s + step])
        self._pos += ci.size
        return len(self._cluster_offsets) - 1

    def finish(self, dirents: list[Dirent], *, main_page: int = NO_MAIN_PAGE):
        """``dirents`` must already be sorted by (namespace, url); redirect
        and cluster indexes must already be final."""
        n = len(dirents)
        # dirents
        dirent_pos = []
        for d in dirents:
            dirent_pos.append(self._pos)
            self._write(d.pack())
        # url pointer list
        url_ptr_pos = self._pos
        self._write(np.asarray(dirent_pos, dtype="<u8").tobytes())
        # title pointer list (all entries, by namespace + title)
        title_ptr_pos = self._pos
        order = sorted(range(n), key=lambda i: sort_key_title(dirents[i]))
        self._write(np.asarray(order, dtype="<u4").tobytes())
        # cluster pointer list
        cluster_ptr_pos = self._pos
        self._write(np.asarray(self._cluster_offsets, dtype="<u8").tobytes())
        checksum_pos = self._pos
        hdr = Header(self.major, self.minor, self.uuid, n, len(self._cluster_offsets),
                     url_ptr_pos, title_ptr_pos, cluster_ptr_pos, self.mime_list_pos,
                     main_page, NO_MAIN_PAGE, checksum_pos)
        self._fh.seek(0)
        self._fh.write(hdr.pack())
        self._fh.flush()
        self._fh.close()
        write_checksum(self.path)
        return hdr


def write_checksum(path: str):
    """Append (or rewrite) the 16-byte MD5 trailer over everything before
    ``checksum_pos``."""
    with open(path, "r+b") as fh:
        hdr = Header.parse(fh.read(HEADER_LEN))
        fh.seek(0)
        h = hashlib.md5()
        remaining = hdr.checksum_pos
        while remaining:
            chunk = fh.read(min(remaining, 64 << 20))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
        fh.seek(hdr.checksum_pos)
        fh.write(h.digest())
        fh.truncate(hdr.checksum_pos + 16)


def verify_checksum(path: str) -> bool:
    with open(path, "rb") as fh:
        hdr = Header.parse(fh.read(HEADER_LEN))
        fh.seek(0)
        h = hashlib.md5()
        remaining = hdr.checksum_pos
        while remaining:
            chunk = fh.read(min(remaining, 64 << 20))
            if not chunk:
                return False
            h.update(chunk)
            remaining -= len(chunk)
        return fh.read(16) == h.digest()


# ------------------------------------------------------------ title lists --

TITLE_LISTING_V0 = b"listing/titleOrdered/v0"
TITLE_LISTING_V1 = b"listing/titleOrdered/v1"


def title_listing(dirents: list[Dirent], front: Callable[[int, Dirent], bool] | None) -> bytes:
    """libzim's ``X/listing/titleOrdered/v0`` (all entries) / ``v1`` (front
    articles only): u32 LE entry indexes sorted by (namespace, title)."""
    idx = [i for i, d in enumerate(dirents) if front is None or front(i, d)]
    idx.sort(key=lambda i: sort_key_title(dirents[i]))
    return np.asarray(idx, dtype="<u4").tobytes()
