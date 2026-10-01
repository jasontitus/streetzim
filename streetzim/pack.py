"""Stream a StreetZim JSONL manifest into a ZIM, without the Rust packer.

The writer uses Python's sqlite3/lzma and python-zstandard. Directory entries,
redirect traversal and pointer tables stay on disk; compression jobs and open
cluster buckets are bounded. Archive layout follows ZIM 6.3. Reader/native
compatibility is checked by the unit tests with python-libzim's reader (and
openZIM zimcheck where it is installed), and in CI by a Monaco build with
``--zim-builder manifest --xapian none`` that cloud/validate_zim.py checks with
zimcheck required (``STREETZIM_REQUIRE_ZIMCHECK=1``).

The packer writes no Xapian databases of its own: without pre-built indexes
(``--xapian builder``) the archive has no ``X/title/xapian``, so Kiwix readers
offer no title suggestions for it.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import lzma
import os
import re
import resource
import signal
import sqlite3
import stat
import struct
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict, deque
from collections.abc import Generator, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import zstandard

HEADER = struct.Struct("<IHH16sIIQQQQIIQ")
ARTICLE = struct.Struct("<HBBIII")
REDIRECT = struct.Struct("<HBBII")
U32 = struct.Struct("<I")
U64 = struct.Struct("<Q")
MAX_U32 = (1 << 32) - 1
MIME_RESERVE = 64 << 10
CHUNK = 4 << 20
MAX_RECORD = 128 << 20
MAX_BUCKETS = 64
MAX_BLOBS = 4096
# libzim writes metadata with exactly this MIME string (upper-case UTF-8).
DEFAULT_METADATA_MIME = "text/plain;charset=UTF-8"
# The packer's own statistics (JSON) are written to this path when it is set,
# so the parent can read the child's peak RSS rather than a wait4() figure.
STATS_FILE_ENV = "STREETZIM_PACK_STATS_FILE"
LISTING_PATH = b"listing/titleOrdered/v1"
TOKEN = r"[A-Za-z0-9!#$%&'*+.^_`|~\-]+"
MIME_RE = re.compile(rf'{TOKEN}/{TOKEN}(?: *(?:; *{TOKEN} *= *(?:{TOKEN}|"(?:[^"\\]|\\.)*")))* *\Z')


class PackError(ValueError):
    """Invalid manifest/input or an unrepresentable archive."""


def _integer(value: Any, name: str, maximum: int, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PackError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _text(value: Any, name: str, *, controls: bool = False) -> str:
    if not isinstance(value, str):
        raise PackError(f"{name} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PackError(f"{name} contains invalid Unicode") from error
    if controls and any(unicodedata.category(char) == "Cc" for char in value):
        raise PackError(f"{name} contains a control character")
    return value


def _boolean(record: dict[str, Any], name: str, default: bool = False) -> bool:
    value = record.get(name, default)
    if type(value) is not bool:
        raise PackError(f"{name} must be a boolean")
    return value


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PackError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _bad_constant(value: str) -> None:
    raise PackError(f"invalid JSON number {value}")


def _manifest_chunks(source: BinaryIO) -> Iterator[bytes]:
    magic = source.read(4)
    source.seek(0)
    if magic != b"\x28\xb5\x2f\xfd":
        while chunk := source.read(64 << 10):
            yield chunk
        return
    decoder = None
    while data := source.read(1024):
        # A small input block bounds expansion while retaining explicit eof
        # checking. stream_reader silently accepts a missing frame trailer.
        while data:
            if decoder is None:
                decoder = zstandard.ZstdDecompressor().decompressobj()
            try:
                decoded = decoder.decompress(data)
            except zstandard.ZstdError as error:
                raise PackError(f"invalid zstd manifest: {error}") from error
            if decoded:
                yield decoded
            data = decoder.unused_data if decoder.eof else b""
            if decoder.eof:
                decoder = None
    if decoder is not None:
        raise PackError("truncated zstd manifest frame")


def iter_manifest(path: Path) -> Generator[tuple[int, dict[str, Any]], None, None]:
    """Read plain/concatenated-zstd JSONL, requiring complete frame trailers."""
    line = bytearray()
    number = 0
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        source = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with source:
        before = os.fstat(source.fileno())
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if not stat.S_ISREG(before.st_mode):
            raise PackError("manifest must be a regular file")
        for chunk in _manifest_chunks(source):
            start = 0
            while start < len(chunk):
                newline = chunk.find(b"\n", start)
                end = len(chunk) if newline < 0 else newline
                if len(line) + end - start > MAX_RECORD:
                    raise PackError(f"manifest line {number + 1}: exceeds {MAX_RECORD} bytes")
                line.extend(chunk[start:end])
                start = end + 1
                if newline >= 0:
                    number += 1
                    record = _parse_record(bytes(line), number)
                    line.clear()
                    if record is not None:
                        yield number, record
        if line:
            record = _parse_record(bytes(line), number + 1)
            if record is not None:
                yield number + 1, record
        after = os.fstat(source.fileno())
        current = path.stat()
        for info in (after, current):
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != identity:
                raise PackError("manifest changed while packing")


def _parse_record(line: bytes, number: int) -> dict[str, Any] | None:
    try:
        stripped = line.decode("utf-8").strip()
        if not stripped or stripped.startswith("#"):
            return None
        record = json.loads(stripped, object_pairs_hook=_json_object, parse_constant=_bad_constant)
        if not isinstance(record, dict):
            raise PackError("record must be a JSON object")
        return record
    except (ValueError, UnicodeError, RecursionError) as error:
        raise PackError(f"manifest line {number}: {str(error)[:200]}") from error


def _decode_base64(value: Any) -> bytes:
    encoded = _text(value, "body_b64")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise PackError("invalid body_b64") from error
    # validate=True does not reject nonzero unused padding bits.
    if encoded.endswith("=="):
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        if alphabet.index(encoded[-3]) & 15:
            raise PackError("noncanonical body_b64 padding bits")
    elif encoded.endswith("="):
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        if alphabet.index(encoded[-2]) & 3:
            raise PackError("noncanonical body_b64 padding bits")
    return data


@dataclass
class Body:
    size: int
    data: bytes | None = None
    path: Path | None = None
    identity: tuple[int, int, int, int] | None = None

    def chunks(self, stop: threading.Event) -> Iterator[bytes | memoryview]:
        if self.data is not None:
            view = memoryview(self.data)
            for start in range(0, len(view), CHUNK):
                if stop.is_set():
                    raise InterruptedError("packing cancelled")
                yield view[start:start + CHUNK]
            return
        assert self.path is not None
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            source = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with source:
            before = os.fstat(source.fileno())
            actual = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            if not stat.S_ISREG(before.st_mode) or actual != self.identity:
                raise PackError(f"file changed before packing: {self.path}")
            remaining = self.size
            while True:
                if stop.is_set():
                    raise InterruptedError("packing cancelled")
                data = source.read(min(CHUNK, remaining + 1))
                if not data:
                    break
                if len(data) > remaining:
                    raise PackError(f"file grew beyond expected {self.size} bytes: {self.path}")
                remaining -= len(data)
                yield data
            after = os.fstat(source.fileno())
            if remaining or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != self.identity:
                raise PackError(f"file shrank or changed while packing: {self.path}")


def _body(record: dict[str, Any], *, text_key: str | None = None) -> Body:
    keys = ([text_key] if text_key else []) + ["body_b64", "file"]
    present = [key for key in keys if record.get(key) is not None]
    if len(present) != 1:
        raise PackError(f"provide exactly one of {'/'.join(keys)}")
    key = present[0]
    if key == "file":
        path = Path(_text(record[key], "file")).absolute()
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise PackError(f"file body must be a regular file: {path}")
        body = Body(info.st_size, path=path, identity=(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    else:
        data = _decode_base64(record[key]) if key == "body_b64" else _text(record[key], key).encode("utf-8")
        body = Body(len(data), data=data)
    if _boolean(record, "streaming"):
        if key != "file":
            raise PackError("streaming requires only a file body")
        if record.get("size") is not None:
            expected = _integer(record["size"], "size", (1 << 64) - 1)
            if expected != body.size:
                raise PackError(f"declared size {expected} != on-disk size {body.size}")
    elif record.get("size") is not None:
        _integer(record["size"], "size", (1 << 64) - 1)
    return body


@dataclass
class Config:
    compression: str = "zstd"
    level: int | None = None
    strategy: str = "single"
    target: int = 2 << 20
    budget: int = 0
    main_path: str | None = None

    @classmethod
    def parse(cls, record: dict[str, Any]) -> Config:
        if _boolean(record, "_indexing_requested"):
            raise PackError("manifest packer cannot run libzim indexing; use --xapian=builder or --xapian=none")
        cfg = cls()
        for key, attr, choices in (("compression", "compression", {"none", "zstd", "xz"}),
                                   ("cluster_strategy", "strategy", {"single", "by_mime", "by_extension", "by_first_path_segment"})):
            if record.get(key) is not None:
                value = _text(record[key], key).lower()
                if value not in choices:
                    raise PackError(f"unknown {key}: {value!r}")
                setattr(cfg, attr, value)
        if record.get("compression_level") is not None:
            cfg.level = _integer(record["compression_level"], "compression_level", (1 << 31) - 1, minimum=-(1 << 31))
        if record.get("cluster_size_target") is not None:
            cfg.target = max(1, _integer(record["cluster_size_target"], "cluster_size_target", (1 << 64) - 1))
        if record.get("max_in_flight_bytes") is not None:
            cfg.budget = _integer(record["max_in_flight_bytes"], "max_in_flight_bytes", (1 << 64) - 1)
        if record.get("main_path") is not None:
            cfg.main_path = _text(record["main_path"], "main_path", controls=True)
        return cfg

    def compression_level(self) -> int:
        if self.compression == "xz":
            return min(9, max(0, self.level if self.level is not None else 3))
        value = self.level
        if value is None:
            try:
                value = _integer(int(os.environ.get("ZSTD_CLEVEL", "19")), "ZSTD_CLEVEL",
                                 (1 << 31) - 1, minimum=-(1 << 31))
            except ValueError:
                value = 19
        # libzstd accepts zero (its default) and negative fast levels.
        return min(22, value)


@dataclass
class Bucket:
    index: int
    compression: str
    bodies: list[Body] = field(default_factory=list)
    size: int = 0


def _offsets(sizes: list[int]) -> tuple[int, Iterator[bytes]]:
    extended = sum(sizes) + (len(sizes) + 1) * 4 > MAX_U32
    width = 8 if extended else 4
    def records():
        offset = (len(sizes) + 1) * width
        packer = U64 if extended else U32
        yield packer.pack(offset)
        for size in sizes:
            offset += size
            yield packer.pack(offset)
    return (16 if extended else 0), records()


def _encode(bucket: Bucket, output: BinaryIO, level: int, stop: threading.Event) -> None:
    extended, offsets = _offsets([body.size for body in bucket.bodies])
    code = {"none": 1, "xz": 4, "zstd": 5}[bucket.compression]
    output.write(bytes([code | extended]))
    if bucket.compression == "zstd":
        payload_size = sum(body.size for body in bucket.bodies) + (len(bucket.bodies) + 1) * (8 if extended else 4)
        window_log = max(10, min(27, (max(1, payload_size) - 1).bit_length()))
        parameters = zstandard.ZstdCompressionParameters.from_level(level, window_log=window_log, source_size=payload_size)
        encoder = zstandard.ZstdCompressor(compression_params=parameters).stream_writer(output, size=payload_size, closefd=False)
        try:
            for part in offsets:
                encoder.write(part)
            for body in bucket.bodies:
                for part in body.chunks(stop):
                    encoder.write(part)
        except BaseException:
            try:
                encoder.close()
            except BaseException:
                pass  # Preserve the input/I/O error if the size pledge also fails.
            raise
        else:
            encoder.close()
    elif bucket.compression == "xz":
        encoder = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=level)
        for part in offsets:
            output.write(encoder.compress(part))
        for body in bucket.bodies:
            for part in body.chunks(stop):
                output.write(encoder.compress(part))
        output.write(encoder.flush())
    else:
        for part in offsets:
            output.write(part)
        for body in bucket.bodies:
            for part in body.chunks(stop):
                output.write(part)


def _verify_structure(stream: BinaryIO, db: sqlite3.Connection) -> None:
    """Check serialized tables/dirents before publication, with disk-backed sort.

    This gate checks our layout without decompressing every compressed blob or
    allocating an archive-sized mapping. Native readers check payloads in tests.
    """
    def read_at(position: int, size: int) -> bytes:
        stream.seek(position)
        data = stream.read(size)
        if len(data) != size:
            raise PackError("incomplete serialized archive")
        return data

    header = HEADER.unpack(read_at(0, HEADER.size))
    magic, major, minor, _, count, clusters, urls, titles, pointers, mimes, main, layout, checksum = header
    stream.seek(0, os.SEEK_END)
    if (magic, major, minor, mimes, layout) != (0x044D495A, 6, 3, HEADER.size, MAX_U32) or stream.tell() != checksum + 16:
        raise PackError("invalid finalized header")
    directory = pointers + clusters * 8
    if not (HEADER.size + MIME_RESERVE <= urls and titles == urls + count * 8
            and pointers == titles + count * 4 and directory <= checksum):
        raise PackError("invalid pointer table layout")
    mime_data = read_at(mimes, MIME_RESERVE)
    mime_end = mime_data.find(b"\0\0")
    if mime_end < 0:
        raise PackError("unterminated MIME list")
    mime_count = len(mime_data[:mime_end].split(b"\0")) if mime_end else 0
    if mime_count >= 0xFFFD:
        raise PackError("reserved MIME index")
    for mime in mime_data[:mime_end].split(b"\0") if mime_end else []:
        value = mime.decode("ascii")
        if any(ord(char) < 32 or ord(char) == 127 for char in value) or not MIME_RE.fullmatch(value):
            raise PackError("invalid serialized MIME type")
    db.executescript("""
        CREATE TABLE verified_entries(idx INTEGER PRIMARY KEY,ns INTEGER,path BLOB,title BLOB,
            cluster INTEGER,blob INTEGER,target INTEGER,visited INTEGER DEFAULT 0);
        CREATE TABLE verified_clusters(idx INTEGER PRIMARY KEY,offset INTEGER UNIQUE);
    """)
    position, previous = directory, None
    for index in range(count):
        offset, = U64.unpack(read_at(urls + index * 8, 8))
        end = U64.unpack(read_at(urls + (index + 1) * 8, 8))[0] if index + 1 < count else checksum
        if (offset != position or not offset < end <= checksum
                or not REDIRECT.size + 2 <= end - offset <= MAX_RECORD + ARTICLE.size):
            raise PackError("invalid directory entry extent")
        data = read_at(offset, end - offset)
        mime, parameters, ns, revision = struct.unpack_from("<HBBI", data)
        redirect = mime == 0xFFFF
        fixed = REDIRECT.size if redirect else ARTICLE.size
        if parameters or revision or not 33 <= ns <= 126 or len(data) < fixed + 2:
            raise PackError("invalid directory entry")
        fields = data[fixed:].split(b"\0")
        if len(fields) != 3 or fields[-1] != b"":
            raise PackError("invalid directory strings")
        path, title = fields[:2]
        _text(path.decode("utf-8"), "serialized path", controls=True)
        _text(title.decode("utf-8"), "serialized title", controls=True)
        key = ns, path
        if previous is not None and key <= previous:
            raise PackError("directory paths are not strictly ordered")
        previous, position = key, end
        target = cluster = blob = None
        if redirect:
            target, = U32.unpack_from(data, 8)
            if target >= count:
                raise PackError("invalid redirect index")
        else:
            cluster, blob = struct.unpack_from("<II", data, 8)
            if mime >= mime_count or cluster >= clusters or blob >= MAX_BLOBS:
                raise PackError("invalid MIME or cluster index")
        db.execute("INSERT INTO verified_entries(idx,ns,path,title,cluster,blob,target) VALUES(?,?,?,?,?,?,?)",
                   (index, ns, path, title or path, cluster, blob, target))
    for expected, (index,) in enumerate(db.execute("SELECT idx FROM verified_entries ORDER BY ns,title,idx")):
        if U32.unpack(read_at(titles + expected * 4, 4))[0] != index:
            raise PackError("invalid title ordering/permutation")
    if db.execute("SELECT 1 FROM verified_entries r JOIN verified_entries t ON r.target=t.idx WHERE t.ns!=67 LIMIT 1").fetchone():
        raise PackError("redirect target is outside content namespace")
    db.execute("CREATE INDEX verified_visits ON verified_entries(visited)")
    for index, in db.execute("SELECT idx FROM verified_entries WHERE target IS NOT NULL ORDER BY idx"):
        current = index
        while True:
            target, visited = db.execute("SELECT target,visited FROM verified_entries WHERE idx=?", (current,)).fetchone()
            if target is None or visited == 2:
                break
            if visited == 1:
                raise PackError("serialized redirect cycle")
            db.execute("UPDATE verified_entries SET visited=1 WHERE idx=?", (current,))
            current = target
        db.execute("UPDATE verified_entries SET visited=2 WHERE visited=1")
    if main != MAX_U32 and not db.execute("SELECT 1 FROM verified_entries WHERE idx=? AND ns=87 AND path=? AND target IS NOT NULL", (main, b"mainPage")).fetchone():
        raise PackError("invalid main page index")
    for index in range(clusters):
        offset, = U64.unpack(read_at(pointers + index * 8, 8))
        if not HEADER.size + MIME_RESERVE <= offset < urls:
            raise PackError("invalid cluster pointer")
        db.execute("INSERT INTO verified_clusters VALUES(?,?)", (index, offset))
    db.execute("CREATE INDEX verified_blob_refs ON verified_entries(cluster,blob)")
    position = HEADER.size + MIME_RESERVE
    for index, offset in db.execute("SELECT idx,offset FROM verified_clusters ORDER BY offset"):
        if offset != position:
            raise PackError("noncontiguous cluster payload")
        next_row = db.execute("SELECT min(offset) FROM verified_clusters WHERE offset>?", (offset,)).fetchone()
        end = next_row[0] if next_row and next_row[0] is not None else urls
        info = read_at(offset, 1)[0]
        if info not in (1, 4, 5, 17, 20, 21):
            raise PackError("invalid cluster compression info")
        if info & 15 == 1:
            width = 8 if info & 16 else 4
            number = U64 if width == 8 else U32
            first, = number.unpack(read_at(offset + 1, width))
            if first < width or first % width or first // width > MAX_BLOBS + 1 or first > end - offset - 1:
                raise PackError("invalid raw cluster offset table")
            previous_blob = first
            for start in range(width, first, width):
                value, = number.unpack(read_at(offset + 1 + start, width))
                if not previous_blob <= value <= end - offset - 1:
                    raise PackError("invalid raw blob offset")
                previous_blob = value
            if previous_blob != end - offset - 1:
                raise PackError("raw cluster length mismatch")
            if db.execute("SELECT 1 FROM verified_entries WHERE cluster=? AND blob>=? LIMIT 1", (index, first // width - 1)).fetchone():
                raise PackError("invalid raw blob index")
        position = end
    if position != urls:
        raise PackError("cluster payload length mismatch")


class Writer:
    def __init__(self, stage: Path, output: BinaryIO, config: Config, threads: int):
        self.stage, self.output, self.config, self.threads = stage, output, config, threads
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="zim-compress")
        try:
            self.db = sqlite3.connect(stage / "directory.sqlite")
            self.db.execute("PRAGMA cache_size=-8192")
            self.db.execute("PRAGMA temp_store=FILE")
            self.db.execute("PRAGMA journal_mode=OFF")
            self.db.executescript("""
            CREATE TABLE entries(ns INTEGER,path BLOB,title BLOB,mime INTEGER,
                cluster INTEGER,blob INTEGER,target BLOB,idx INTEGER,eligible INTEGER DEFAULT 0,
                offset INTEGER,front INTEGER DEFAULT 0,PRIMARY KEY(ns,path)) WITHOUT ROWID;
            CREATE TABLE clusters(idx INTEGER PRIMARY KEY,offset INTEGER,size INTEGER);
            """)
            output.write(bytes(HEADER.size + MIME_RESERVE))
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.pool.shutdown(wait=True, cancel_futures=True)
            raise
        self.buckets: OrderedDict[tuple[str, str], Bucket] = OrderedDict()
        self.jobs: deque[tuple[int, int, Future[Path]]] = deque()
        self.queued_bytes = self.bucket_bytes = self.cluster_count = 0
        self.budget = config.budget or max(1, 3 * config.target * threads)
        self.bucket_budget = max(1, min(self.budget, 64 << 20))
        self.mimes: list[str] = []
        self.mime_ids: dict[str, int] = {}
        self.article_mimes: dict[str, None] = {}
        self.mime_bytes = 1
        self.counts = {"items": 0, "metadata": 0, "illustrations": 0, "redirects": 0,
                       "cluster_breaks": 0}
        self.metrics = {"peak_in_flight_bytes": 0, "peak_bucket_bytes": 0,
                        "peak_pending_body_bytes": 0, "peak_in_flight_clusters": 0,
                        "peak_open_buckets": 0, "raw_input_bytes": 0}

    def close(self):
        self.stop.set()
        for _, _, future in self.jobs:
            future.cancel()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.db.close()

    def pending_metrics(self):
        # Logical body sizes include file recipes; they exclude codec memory.
        self.metrics["peak_pending_body_bytes"] = max(
            self.metrics["peak_pending_body_bytes"], self.bucket_bytes + self.queued_bytes)
        self.metrics["peak_in_flight_clusters"] = max(
            self.metrics["peak_in_flight_clusters"], len(self.jobs))

    def intern(self, mime: str) -> int:
        _text(mime, "mime")
        if not mime.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in mime) or not MIME_RE.fullmatch(mime):
            raise PackError(f"invalid MIME type: {mime!r}")
        if mime in self.mime_ids:
            return self.mime_ids[mime]
        if len(self.mimes) >= 0xFFFD or self.mime_bytes + len(mime) + 1 > MIME_RESERVE:
            raise PackError("MIME list exceeds its serialized limit")
        index = len(self.mimes)
        self.mimes.append(mime)
        self.mime_ids[mime] = index
        self.mime_bytes += len(mime) + 1
        return index

    def insert(self, ns: int, path: str, title: str, mime: int | None, cluster: int | None = None,
               blob: int | None = None, target: str | None = None, front: bool = False):
        ns = _integer(ns, "namespace", 126, minimum=33)
        p = _text(path, "path", controls=True).encode("utf-8")
        t = _text(title, "title", controls=True).encode("utf-8") or p
        destination = None if target is None else _text(target, "redirect target", controls=True).encode("utf-8")
        try:
            self.db.execute("INSERT INTO entries(ns,path,title,mime,cluster,blob,target,front) VALUES(?,?,?,?,?,?,?,?)",
                            (ns, p, t, mime, cluster, blob, destination, int(front)))
        except sqlite3.IntegrityError as error:
            raise PackError(f"duplicate archive entry {chr(ns)}/{path}") from error

    def key(self, path: str, mime: str, compression: str) -> tuple[str, str]:
        if self.config.strategy == "by_mime":
            value = mime
        elif self.config.strategy == "by_extension":
            _, dot, extension = path.rpartition(".")
            value = extension if dot and "/" not in extension else ""
        elif self.config.strategy == "by_first_path_segment":
            value = path.split("/", 1)[0]
        else:
            value = ""
        return compression, value

    def add(self, ns: int, path: str, title: str, mime: str, body: Body, compress: bool | None = None,
            front: bool = False):
        mime_index = self.intern(mime)
        if ns == ord("C"):
            self.article_mimes.setdefault(mime.split(";", 1)[0].strip(), None)
        compression = "none" if ns == ord("X") or compress is False else self.config.compression
        key = self.key(path, mime, compression)
        if key in self.buckets and self.buckets[key].size + body.size > self.config.target:
            self.flush(key)
        # Free byte capacity from the fullest bucket. For the key-count cap,
        # retain useful accumulated buckets and evict the smallest/recent one.
        # Oldest-first ties churn forever on equal-size cyclic workloads.
        while self.buckets and (self.bucket_bytes + body.size > self.bucket_budget
                                or (key not in self.buckets and len(self.buckets) >= MAX_BUCKETS)):
            candidates = reversed(self.buckets)
            choose = max if self.bucket_bytes + body.size > self.bucket_budget else min
            victim = choose(candidates, key=lambda candidate: (
                self.buckets[candidate].size, len(self.buckets[candidate].bodies)))
            self.flush(victim)
        if key not in self.buckets:
            if self.cluster_count >= MAX_U32:
                raise PackError("too many clusters")
            bucket = Bucket(self.cluster_count, compression)
            self.cluster_count += 1
            self.db.execute("INSERT INTO clusters(idx) VALUES(?)", (bucket.index,))
            self.buckets[key] = bucket
        bucket = self.buckets[key]
        self.insert(ns, path, title, mime_index, bucket.index, len(bucket.bodies), front=front)
        bucket.bodies.append(body)
        bucket.size += body.size
        self.bucket_bytes += body.size
        self.metrics["raw_input_bytes"] += body.size
        self.metrics["peak_bucket_bytes"] = max(self.metrics["peak_bucket_bytes"], self.bucket_bytes)
        self.metrics["peak_open_buckets"] = max(self.metrics["peak_open_buckets"], len(self.buckets))
        self.pending_metrics()
        self.buckets.move_to_end(key)
        if bucket.size >= self.config.target or len(bucket.bodies) >= MAX_BLOBS or self.bucket_bytes >= self.bucket_budget:
            self.flush(key)

    def flush(self, key: tuple[str, str]):
        bucket = self.buckets.pop(key)
        self.bucket_bytes -= bucket.size
        # File-backed raw clusters need no intermediate whole-body copy.
        if bucket.compression == "none" and len(bucket.bodies) == 1 and bucket.bodies[0].path is not None:
            self.drain()
            start = self.output.tell()
            _encode(bucket, self.output, 0, self.stop)
            self.db.execute("UPDATE clusters SET offset=?,size=? WHERE idx=?", (start, self.output.tell() - start, bucket.index))
            return
        while self.jobs and (len(self.jobs) >= 2 * self.threads or self.queued_bytes + bucket.size > self.budget):
            self.drain_one()
        # An oversized cluster is admitted only when the queue is empty.
        destination = self.stage / f"cluster-{bucket.index:010d}.bin"
        def encode_file():
            with destination.open("wb") as stream:
                _encode(bucket, stream, self.config.compression_level(), self.stop)
            return destination
        future = self.pool.submit(encode_file)
        self.jobs.append((bucket.index, bucket.size, future))
        self.queued_bytes += bucket.size
        self.metrics["peak_in_flight_bytes"] = max(self.metrics["peak_in_flight_bytes"], self.queued_bytes)
        self.pending_metrics()

    def drain_one(self):
        index, size, future = self.jobs.popleft()
        try:
            source = future.result()
            start = self.output.tell()
            with source.open("rb") as stream:
                while chunk := stream.read(CHUNK):
                    self.output.write(chunk)
            self.db.execute("UPDATE clusters SET offset=?,size=? WHERE idx=?", (start, self.output.tell() - start, index))
            source.unlink()
        finally:
            self.queued_bytes -= size

    def drain(self):
        while self.jobs:
            self.drain_one()

    def record(self, record: dict[str, Any]):
        kind = record.get("kind")
        if kind == "item":
            path = _text(record.get("path"), "path", controls=True)
            title = _text(record.get("title", ""), "title", controls=True)
            mime = _text(record.get("mime"), "mime")
            ns = record.get("namespace")
            ns = ord("C") if ns is None else _integer(ns, "namespace", 126, minimum=33)
            # libzim's FRONT_ARTICLE hint: an explicit flag wins; without one,
            # libzim treats text/html items as front articles (getAmendedHints).
            front = _boolean(record, "front") if "front" in record else mime.startswith("text/html")
            compress = record.get("compress")
            if compress is not None and type(compress) is not bool:
                raise PackError("compress must be boolean or null")
            self.add(ns, path, title, mime, _body(record, text_key="content"), compress, front=front)
            self.counts["items"] += 1
        elif kind == "metadata":
            name = _text(record.get("name"), "name", controls=True)
            mime = record.get("mimetype")
            if mime is None:
                mime = DEFAULT_METADATA_MIME
            self.add(ord("M"), name, name, _text(mime, "mimetype"), _body(record, text_key="value"))
            self.counts["metadata"] += 1
        elif kind == "illustration":
            side = _integer(record.get("size"), "illustration size", MAX_U32)
            name = f"Illustration_{side}x{side}@1"
            self.add(ord("M"), name, name, "image/png", _body(record))
            self.counts["illustrations"] += 1
        elif kind == "redirect":
            # As in libzim, a redirect is listed only when flagged front.
            self.insert(ord("C"), _text(record.get("path"), "path", controls=True),
                        _text(record.get("title", ""), "title", controls=True), None,
                        target=record.get("target"), front=_boolean(record, "front"))
            if record.get("target") is None:
                raise PackError("redirect target must be a string")
            self.counts["redirects"] += 1
        elif kind == "cluster_break":
            target = record.get("cluster_size_target")
            if target is not None:
                target = max(1, _integer(target, "cluster_size_target", (1 << 64) - 1))
            for key in list(self.buckets):
                self.flush(key)
            if target is not None:
                self.config.target = target
            self.counts["cluster_breaks"] += 1
        else:
            raise PackError(f"unknown record kind: {kind!r}")

    def finalize(self) -> dict[str, Any]:
        if not self.db.execute("SELECT 1 FROM entries WHERE ns=77 AND path=?", (b"Counter",)).fetchone():
            counter: dict[str, int] = dict.fromkeys(self.article_mimes, 0)
            for mime, count in self.db.execute("SELECT mime,count(*) FROM entries WHERE ns=67 AND target IS NULL GROUP BY mime ORDER BY mime"):
                key = self.mimes[mime].split(";", 1)[0].strip()
                counter[key] = counter.get(key, 0) + count
            value = ";".join(f"{mime}={count}" for mime, count in counter.items()).encode()
            self.add(77, "Counter", "Counter", DEFAULT_METADATA_MIME, Body(len(value), data=value))
        if self.config.main_path is not None:
            self.insert(87, "mainPage", "mainPage", None, target=self.config.main_path)
        generate_listing = not self.db.execute("SELECT 1 FROM entries WHERE ns=88 AND path=?", (LISTING_PATH,)).fetchone()
        if generate_listing:
            self.insert(88, LISTING_PATH.decode(), LISTING_PATH.decode(), self.intern("application/octet-stream+zimlisting"))
        for key in list(self.buckets):
            self.flush(key)
        self.drain()
        count = 0
        for ns, path in self.db.execute("SELECT ns,path FROM entries ORDER BY ns,path"):
            if count >= MAX_U32:
                raise PackError("too many directory entries")
            self.db.execute("UPDATE entries SET idx=? WHERE ns=? AND path=?", (count, ns, path))
            count += 1
        self.db.execute("CREATE UNIQUE INDEX entry_index ON entries(idx)")
        self.db.execute("CREATE INDEX entry_eligibility ON entries(eligible)")
        # Resolve all redirects, including chains unrelated to the main page.
        for _index, target in self.db.execute("SELECT idx,target FROM entries WHERE target IS NOT NULL ORDER BY idx"):
            if not self.db.execute("SELECT 1 FROM entries WHERE ns=67 AND path=?", (target,)).fetchone():
                raise PackError(f"redirect has no target: {target[:200]!r}")
        # Detect redirect cycles: 2 = resolved to an item, 1 = on the current chain.
        self.db.execute("UPDATE entries SET eligible=2 WHERE target IS NULL")
        for index, in self.db.execute("SELECT idx FROM entries WHERE target IS NOT NULL ORDER BY idx"):
            current = index
            while True:
                eligible, target = self.db.execute("SELECT eligible,target FROM entries WHERE idx=?", (current,)).fetchone()
                if eligible == 2:
                    break
                if eligible == 1:
                    raise PackError("redirect cycle")
                self.db.execute("UPDATE entries SET eligible=1 WHERE idx=?", (current,))
                current, = self.db.execute("SELECT idx FROM entries WHERE ns=67 AND path=?", (target,)).fetchone()
            self.db.execute("UPDATE entries SET eligible=2 WHERE eligible=1")
        if generate_listing:
            original_index, = self.db.execute("SELECT idx FROM entries WHERE ns=88 AND path=?", (LISTING_PATH,)).fetchone()
            listing = self.stage / "title-listing.bin"
            with listing.open("wb") as stream:
                # Front articles only (libzim's FRONT_ARTICLE hint), so
                # application pages such as places.html stay unlisted.
                for index, in self.db.execute("SELECT idx FROM entries WHERE ns=67 AND front=1 ORDER BY title,idx"):
                    stream.write(U32.pack(index))
            self.db.execute("DELETE FROM entries WHERE ns=88 AND path=?", (LISTING_PATH,))
            self.add(88, LISTING_PATH.decode(), LISTING_PATH.decode(), "application/octet-stream+zimlisting", _body({"file": str(listing)}))
            self.db.execute("UPDATE entries SET idx=?,eligible=2 WHERE ns=88 AND path=?", (original_index, LISTING_PATH))
            for key in list(self.buckets):
                self.flush(key)
            self.drain()
        url_pos = self.output.tell()
        title_pos = url_pos + count * 8
        cluster_pos = title_pos + count * 4
        directory_pos = cluster_pos + self.cluster_count * 8
        position = directory_pos
        for index, path, title, target in self.db.execute("SELECT idx,path,title,target FROM entries ORDER BY idx"):
            self.db.execute("UPDATE entries SET offset=? WHERE idx=?", (position, index))
            position += (ARTICLE.size if target is None else REDIRECT.size) + len(path) + 1 + (0 if title == path else len(title)) + 1
        for offset, in self.db.execute("SELECT offset FROM entries ORDER BY idx"):
            self.output.write(U64.pack(offset))
        for index, in self.db.execute("SELECT idx FROM entries ORDER BY ns,title,idx"):
            self.output.write(U32.pack(index))
        for offset, in self.db.execute("SELECT offset FROM clusters ORDER BY idx"):
            if offset is None:
                raise PackError("unfinished cluster")
            self.output.write(U64.pack(offset))
        for ns, path, title, mime, cluster, blob, target in self.db.execute("SELECT ns,path,title,mime,cluster,blob,target FROM entries ORDER BY idx"):
            if target is None:
                self.output.write(ARTICLE.pack(mime, 0, ns, 0, cluster, blob))
            else:
                target_index, = self.db.execute("SELECT idx FROM entries WHERE ns=67 AND path=?", (target,)).fetchone()
                self.output.write(REDIRECT.pack(0xFFFF, 0, ns, 0, target_index))
            self.output.write(path + b"\0" + (b"" if title == path else title) + b"\0")
        checksum_pos = self.output.tell()
        if checksum_pos != position:
            raise PackError("directory layout size mismatch")
        main = MAX_U32
        if self.config.main_path is not None:
            main, = self.db.execute("SELECT idx FROM entries WHERE ns=87 AND path=?", (b"mainPage",)).fetchone()
        self.output.seek(0)
        self.output.write(HEADER.pack(0x044D495A, 6, 3, uuid.uuid4().bytes, count, self.cluster_count,
                                      url_pos, title_pos, cluster_pos, HEADER.size, main, MAX_U32, checksum_pos))
        self.output.write(b"\0".join(mime.encode("ascii") for mime in self.mimes) + b"\0\0")
        self.output.flush()
        self.output.seek(0)
        digest = hashlib.md5(usedforsecurity=False)
        while chunk := self.output.read(CHUNK):
            digest.update(chunk)
        self.output.write(digest.digest())
        self.output.flush()
        os.fsync(self.output.fileno())
        validation_started = time.monotonic()
        _verify_structure(self.output, self.db)
        return {**self.counts, **self.metrics, "entries": count, "clusters": self.cluster_count,
                "output_bytes": checksum_pos + 16, "threads": self.threads,
                "validation_s": time.monotonic() - validation_started,
                "max_in_flight_bytes": self.budget, "cluster_strategy": self.config.strategy}


def default_threads() -> int:
    value = os.environ.get("RAYON_NUM_THREADS")
    if value:
        try:
            return _integer(int(value), "RAYON_NUM_THREADS", MAX_U32, minimum=1)
        except ValueError as error:
            raise PackError("RAYON_NUM_THREADS must be a positive integer") from error
    from streetzim.cpus import build_cpus
    return min(20, build_cpus())


def pack(manifest: str | Path, output: str | Path, *, threads: int | None = None) -> dict[str, Any]:
    """Publish only a fully finalized archive; retain the caller's manifest."""
    started = time.monotonic()
    source, destination = Path(manifest), Path(output)
    if source.resolve() == destination.resolve():
        raise PackError("manifest and output must be different files")
    workers = default_threads() if threads is None else _integer(threads, "threads", MAX_U32, minimum=1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".streetzim-pack-", dir=destination.parent) as directory:
        stage = Path(directory)
        archive = stage / "archive.zim"
        records = iter_manifest(source)
        try:
            config = Config()
            first = next(records, None)
            configured = first is not None and first[1].get("kind") == "config"
            if configured and first is not None:
                config = Config.parse(first[1])
            with archive.open("w+b") as stream:
                writer = Writer(stage, stream, config, workers)
                try:
                    if first is not None and not configured:
                        writer.record(first[1])
                    for number, record in records:
                        if record.get("kind") == "config":
                            raise PackError(f"manifest line {number}: config must appear once before content")
                        try:
                            writer.record(record)
                        except (ValueError, OSError) as error:
                            raise PackError(f"manifest line {number}: {str(error)[:200]}") from error
                    stats = writer.finalize()
                finally:
                    writer.close()
        finally:
            records.close()
        os.replace(archive, destination)
    stats["wall_s"] = time.monotonic() - started
    stats["peak_rss_bytes"] = peak_rss_bytes()
    return stats


def peak_rss_bytes() -> int:
    """This process's peak RSS. Linux: VmHWM from /proc/self/status, which
    starts afresh at exec, so a packer run as a child reports its own peak
    and not the parent's (getrusage/wait4 ru_maxrss can include the RSS the
    child had before exec). Elsewhere, getrusage's ru_maxrss."""
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)


def _write_stats_file(stats: dict[str, Any]) -> None:
    """Report to the parent through STREETZIM_PACK_STATS_FILE, when set."""
    path = os.environ.get(STATS_FILE_ENV)
    if not path:
        return
    try:
        Path(path).write_text(json.dumps(stats, sort_keys=True))
    except OSError as error:
        print(f"streetzim-pack: could not write stats file: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pack a StreetZim JSONL manifest into a ZIM using Python.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--threads", type=int, help="compression workers (overrides RAYON_NUM_THREADS)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version="streetzim-pack (Python) 0.1.0")
    args = parser.parse_args(argv)
    previous = signal.getsignal(signal.SIGTERM)
    def cancelled(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, cancelled)
    try:
        stats = pack(args.manifest, args.output, threads=args.threads)
        # Run as a command, pack()'s peak_rss_bytes is the packer's own.
        _write_stats_file(stats)
        if args.verbose:
            print("streetzim-pack (Python): " + json.dumps(stats, sort_keys=True), file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("streetzim-pack: interrupted; archive was not published", file=sys.stderr)
        return 130
    except (ValueError, OSError, sqlite3.Error, lzma.LZMAError, zstandard.ZstdError) as error:
        print(f"streetzim-pack: {str(error)[:500]}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
