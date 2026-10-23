"""Low-level ZIM (v6.x) reader used by the merge prototype.

Reads only the tables (header, MIME list, path/cluster pointer lists,
dirents, the v1 title listing). Never decompresses a cluster except to read
the cluster's own info byte / raw blob offset table, which is all the merge
needs.

Format summary (verified against libzim 9.7 src/fileheader.cpp,
src/dirent.cpp, src/cluster.cpp, src/writer/creator.cpp):

header (80 B, little-endian)
  0  u32 magic 0x044D495A          4  u16 major (6)   6 u16 minor (1..3)
  8  16B uuid                      24 u32 entryCount  28 u32 clusterCount
  32 u64 pathPtrPos                40 u64 titleIdxPos (-1 = no v0 list)
  48 u64 clusterPtrPos             56 u64 mimeListPos (80)
  64 u32 mainPage (-1 = none)      68 u32 layoutPage (-1)
  72 u64 checksumPos  (file size == checksumPos + 16, MD5 of [0,checksumPos))

dirent
  u16 mime (0xFFFF redirect, 0xFFFE linktarget, 0xFFFD deleted)
  u8  parameterLen  u8 namespace  u32 revision
  redirect: u32 targetEntryIndex          else: u32 cluster  u32 blob
  path\\0 title\\0 (title empty => same as path) parameter[parameterLen]
"""
from __future__ import annotations

import itertools
import struct
from dataclasses import dataclass
from collections.abc import Callable, Iterator
from typing import BinaryIO

HEADER = struct.Struct("<IHH16sIIQQQQIIQ")
U32 = struct.Struct("<I")
U64 = struct.Struct("<Q")
MAGIC = 0x044D495A
NO_U32 = 0xFFFFFFFF
NO_U64 = 0xFFFFFFFFFFFFFFFF
REDIRECT_MIME = 0xFFFF
LINKTARGET_MIME = 0xFFFE
DELETED_MIME = 0xFFFD


@dataclass
class Header:
    magic: int
    major: int
    minor: int
    uuid: bytes
    entry_count: int
    cluster_count: int
    path_ptr_pos: int
    title_ptr_pos: int
    cluster_ptr_pos: int
    mime_list_pos: int
    main_page: int
    layout_page: int
    checksum_pos: int

    def pack(self) -> bytes:
        return HEADER.pack(self.magic, self.major, self.minor, self.uuid,
                           self.entry_count, self.cluster_count,
                           self.path_ptr_pos, self.title_ptr_pos,
                           self.cluster_ptr_pos, self.mime_list_pos,
                           self.main_page, self.layout_page, self.checksum_pos)


@dataclass
class Dirent:
    index: int
    offset: int
    raw_len: int
    mime: int
    param_len: int
    ns: int
    revision: int
    cluster: int | None
    blob: int | None
    target: int | None
    path: bytes
    title_raw: bytes          # as stored (b"" means "same as path")
    parameter: bytes

    @property
    def is_redirect(self) -> bool:
        return self.mime == REDIRECT_MIME

    @property
    def title(self) -> bytes:
        return self.title_raw or self.path

    @property
    def key(self) -> tuple[int, bytes]:
        return (self.ns, self.path)


def parse_dirent(buf: bytes, pos: int, index: int, offset: int) -> Dirent:
    mime, plen, ns, rev = struct.unpack_from("<HBBI", buf, pos)
    p = pos + 8
    cluster = blob = target = None
    if mime == REDIRECT_MIME:
        (target,) = U32.unpack_from(buf, p)
        p += 4
    elif mime in (LINKTARGET_MIME, DELETED_MIME):
        pass
    else:
        cluster, blob = struct.unpack_from("<II", buf, p)
        p += 8
    e1 = buf.index(b"\0", p)
    path = bytes(buf[p:e1])
    e2 = buf.index(b"\0", e1 + 1)
    title = bytes(buf[e1 + 1:e2])
    param = bytes(buf[e2 + 1:e2 + 1 + plen])
    end = e2 + 1 + plen
    if len(param) != plen:
        raise ValueError("truncated dirent parameter")
    return Dirent(index, offset, end - pos, mime, plen, ns, rev, cluster, blob,
                  target, path, title, param)


class ZimReader:
    """Random/sequential access to the tables of one ZIM file."""

    def __init__(self, path: str):
        self.path = path
        self.f: BinaryIO = open(path, "rb", buffering=0)
        self.f.seek(0, 2)
        self.size = self.f.tell()
        h = HEADER.unpack(self.read_at(0, HEADER.size))
        self.header = Header(*h)
        if self.header.magic != MAGIC:
            raise ValueError(f"{path}: not a ZIM")
        if self.header.major != 6:
            raise ValueError(f"{path}: major version {self.header.major} unsupported")
        if self.header.checksum_pos + 16 != self.size:
            raise ValueError(f"{path}: checksumPos+16 != file size")
        self.mimes = self._read_mimes()
        self._dirent_cache: dict[int, Dirent] = {}

    def close(self):
        self.f.close()

    def read_at(self, offset: int, size: int) -> bytes:
        self.f.seek(offset)
        out = self.f.read(size)
        if len(out) != size:
            raise EOFError(f"{self.path}: short read at {offset}+{size}")
        return out

    def _read_mimes(self) -> list[bytes]:
        h = self.header
        limit = min(x for x in (h.path_ptr_pos, h.cluster_ptr_pos,
                                h.title_ptr_pos if h.title_ptr_pos != NO_U64 else NO_U64,
                                self.cluster_offset(0) if h.cluster_count else NO_U64,
                                self.path_ptr(0) if h.entry_count else NO_U64))
        data = self.read_at(h.mime_list_pos, min(limit - h.mime_list_pos, 1 << 20))
        out = []
        p = 0
        while True:
            e = data.index(b"\0", p)
            if e == p:
                break
            out.append(data[p:e])
            p = e + 1
        self.mime_list_end = h.mime_list_pos + p + 1
        return out

    # --- pointer lists -------------------------------------------------
    def path_ptr(self, i: int) -> int:
        return U64.unpack(self.read_at(self.header.path_ptr_pos + 8 * i, 8))[0]

    def cluster_offset(self, i: int) -> int:
        return U64.unpack(self.read_at(self.header.cluster_ptr_pos + 8 * i, 8))[0]

    def iter_u64_table(self, pos: int, count: int, chunk: int = 1 << 16) -> Iterator[int]:
        done = 0
        while done < count:
            n = min(chunk, count - done)
            data = self.read_at(pos + 8 * done, 8 * n)
            yield from (v for (v,) in U64.iter_unpack(data))
            done += n

    def cluster_offsets(self) -> list[int]:
        return list(self.iter_u64_table(self.header.cluster_ptr_pos, self.header.cluster_count))

    # --- dirents ---------------------------------------------------------
    def dirent_at(self, index: int) -> Dirent:
        off = self.path_ptr(index)
        size = 512
        while True:
            data = self.read_at(off, min(size, self.size - off))
            try:
                return parse_dirent(data, 0, index, off)
            except (ValueError, struct.error):
                if size >= self.size - off:
                    raise
                size *= 4

    def iter_dirents(self, window: int = 4 << 20) -> Iterator[Dirent]:
        """All dirents in path order, reading through a sliding window.

        Dirents are usually stored contiguously in index order (libzim and
        streetzim/pack.py both do); a dirent that is elsewhere is read on
        its own, so any layout works, just slower.
        """
        h = self.header
        buf = b""
        buf_start = 0
        for i, off in enumerate(self.iter_u64_table(h.path_ptr_pos, h.entry_count)):
            rel = off - buf_start
            if not (0 <= rel and rel + 4096 <= len(buf)):
                # refill (needs whole dirent: paths are < 64 KiB in practice)
                size = min(window, self.size - off)
                buf = self.read_at(off, size)
                buf_start = off
                rel = 0
            try:
                d = parse_dirent(buf, rel, i, off)
            except (ValueError, struct.error):
                d = self.dirent_at(i)
            yield d

    # --- clusters --------------------------------------------------------
    def cluster_info(self, i: int) -> int:
        return self.read_at(self.cluster_offset(i), 1)[0]

    def raw_blob(self, cluster: int, blob: int) -> tuple[int, int]:
        """(absolute offset, size) of a blob in an UNCOMPRESSED cluster."""
        off = self.cluster_offset(cluster)
        info = self.read_at(off, 1)[0]
        if info & 0x0F not in (0, 1):
            raise ValueError(f"cluster {cluster} is compressed (info={info:#x})")
        width = 8 if info & 0x10 else 4
        fmt = U64 if width == 8 else U32
        a, b = (fmt.unpack(x)[0] for x in
                (self.read_at(off + 1 + width * blob, width),
                 self.read_at(off + 1 + width * (blob + 1), width)))
        return off + 1 + a, b - a

    def blob_bytes(self, cluster: int, blob: int) -> bytes:
        """Content of one blob, decompressing the cluster stream if needed."""
        off = self.cluster_offset(cluster)
        info = self.read_at(off, 1)[0]
        if info & 0x0F in (0, 1):
            o, n = self.raw_blob(cluster, blob)
            return self.read_at(o, n)
        exact, offsets = self._cluster_stream(off, info)
        start, end = offsets[blob], offsets[blob + 1]
        exact(start - offsets[0])          # skip earlier blobs
        return exact(end - start)

    def cluster_blobs(self, cluster: int) -> list[bytes]:
        """Every blob of one cluster, in order, decompressing it once."""
        off = self.cluster_offset(cluster)
        info = self.read_at(off, 1)[0]
        width = 8 if info & 0x10 else 4
        fmt = U64 if width == 8 else U32
        if info & 0x0F in (0, 1):
            first = fmt.unpack(self.read_at(off + 1, width))[0]
            offsets = [v for (v,) in fmt.iter_unpack(self.read_at(off + 1, first))]
            return [self.read_at(off + 1 + a, b - a) for a, b in itertools.pairwise(offsets)]
        exact, offsets = self._cluster_stream(off, info)
        return [exact(b - a) for a, b in itertools.pairwise(offsets)]

    def _cluster_stream(self, off: int, info: int) -> tuple[Callable[[int], bytes], list[int]]:
        """A compressed cluster's reader, positioned after its offset table,
        and the table."""
        comp = info & 0x0F
        width = 8 if info & 0x10 else 4
        fmt = U64 if width == 8 else U32
        self.f.seek(off + 1)
        read: Callable[[int], bytes]
        if comp == 5:
            import zstandard
            reader = zstandard.ZstdDecompressor().stream_reader(self.f, read_across_frames=False)
            read = reader.read
        elif comp == 4:
            import lzma
            dec = lzma.LZMADecompressor()
            pending = bytearray()

            def read_xz(n: int) -> bytes:
                while len(pending) < n and not dec.eof:
                    chunk = self.f.read(1 << 16)
                    if not chunk:
                        break
                    pending.extend(dec.decompress(chunk))
                out = bytes(pending[:n])
                del pending[:n]
                return out
            read = read_xz
        else:
            raise ValueError(f"unsupported cluster compression {comp}")

        def exact(n: int) -> bytes:
            out = bytearray()
            while len(out) < n:
                part = read(n - len(out))
                if not part:
                    raise EOFError("truncated cluster")
                out += part
            return bytes(out)
        first = fmt.unpack(exact(width))[0]
        offsets = [first] + [v for (v,) in fmt.iter_unpack(exact(first - width))]
        return exact, offsets

    def lower_bound(self, ns: int, path: bytes) -> int:
        """The index of the first dirent whose (namespace, path) is not
        below the given one (entry_count when none)."""
        lo, hi = 0, self.header.entry_count
        while lo < hi:
            mid = (lo + hi) // 2
            d = self.dirent_at(mid)
            if d.key < (ns, path):
                lo = mid + 1
            else:
                hi = mid
        return lo

    def find(self, ns: int, path: bytes) -> int | None:
        lo = self.lower_bound(ns, path)
        if lo < self.header.entry_count and self.dirent_at(lo).key == (ns, path):
            return lo
        return None

    def v1_listing(self) -> tuple[int, int] | None:
        """(offset, count) of X/listing/titleOrdered/v1's raw blob.

        A v1 listing in a COMPRESSED cluster (zimru's writer does this) is
        ignored by libzim's reader; here it is decompressed once and served
        from memory (offset is then negative: see read_u32s)."""
        i = self.find(ord("X"), b"listing/titleOrdered/v1")
        if i is None:
            return None
        d = self.dirent_at(i)
        assert d.cluster is not None and d.blob is not None, "v1 listing is a redirect"
        info = self.cluster_info(d.cluster)
        if info & 0x0F in (0, 1):
            off, size = self.raw_blob(d.cluster, d.blob)
            return off, size // 4
        self._v1_mem = self.blob_bytes(d.cluster, d.blob)
        return -1, len(self._v1_mem) // 4

    def iter_u32_blob(self, pos: int, count: int, chunk: int = 1 << 16) -> Iterator[int]:
        if pos == -1:                       # in-memory (decompressed) listing
            yield from (v for (v,) in U32.iter_unpack(self._v1_mem[:4 * count]))
            return
        done = 0
        while done < count:
            n = min(chunk, count - done)
            data = self.read_at(pos + 4 * done, 4 * n)
            yield from (v for (v,) in U32.iter_unpack(data))
            done += n

    def u32_at(self, pos: int, k: int) -> int:
        if pos == -1:
            return U32.unpack_from(self._v1_mem, 4 * k)[0]
        return U32.unpack(self.read_at(pos + 4 * k, 4))[0]


def describe(path: str) -> dict:
    z = ZimReader(path)
    h = z.header
    offs = z.cluster_offsets()
    first_dirent = z.path_ptr(0) if h.entry_count else None
    ns_counts: dict[str, int] = {}
    redirects = 0
    for d in z.iter_dirents():
        ns_counts[chr(d.ns)] = ns_counts.get(chr(d.ns), 0) + 1
        redirects += d.is_redirect
    infos: dict[int, int] = {}
    for o in offs[:2000]:
        info = z.read_at(o, 1)[0]
        infos[info] = infos.get(info, 0) + 1
    v1 = z.v1_listing()
    out = {
        "file": path, "size": z.size, "version": f"{h.major}.{h.minor}",
        "uuid": h.uuid.hex(), "entries": h.entry_count, "clusters": h.cluster_count,
        "pathPtrPos": h.path_ptr_pos,
        "titleIdxPos": "none(-1)" if h.title_ptr_pos == NO_U64 else h.title_ptr_pos,
        "clusterPtrPos": h.cluster_ptr_pos, "mimeListPos": h.mime_list_pos,
        "mimeListEnd": z.mime_list_end, "mainPage": h.main_page,
        "layoutPage": h.layout_page, "checksumPos": h.checksum_pos,
        "mimes": [m.decode() for m in z.mimes],
        "first_cluster_offset": offs[0] if offs else None,
        "clusters_sorted_by_offset": offs == sorted(offs),
        "last_cluster_offset": max(offs) if offs else None,
        "first_dirent_offset": first_dirent,
        "ns_counts": ns_counts, "redirects": redirects,
        "cluster_info_bytes(first 2000)": {hex(k): v for k, v in infos.items()},
        "v1_listing": v1,
    }
    z.close()
    return out


if __name__ == "__main__":
    import json
    import sys
    for p in sys.argv[1:]:
        print(json.dumps(describe(p), indent=1))
