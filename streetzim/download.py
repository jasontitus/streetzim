"""Large downloads for `streetzim` (the MBTiles of --mbtiles-url): resumed
when interrupted, reused while unchanged upstream, checked against
OpenFreeMap's published checksums, one task at a time per file.

- The upstream version is its ETag, Last-Modified and Content-Length (the
  "stamp"), from a HEAD request, or from a one-byte GET when HEAD is refused.
- An interrupted download (<file>.part, with the stamp it was started from)
  is resumed only when the server takes ranges, the file has a validator
  (ETag or Last-Modified) and it is unchanged upstream. The request carries
  If-Range, and the answer must be a 206 starting at the offset; a 200 is
  the whole file again, written from the start. A complete .part is
  renamed without downloading.
- A finished download is reused while its stamp is unchanged. A file
  already in place without a stamp (a pre-seeded download folder) is used
  when it has the upstream size and, for OpenFreeMap, the published SHA-256.
- OpenFreeMap's files (https://*.openfreemap.com/areas/<area>/<version>/
  tiles.mbtiles) are checked against that version's SHA256SUMS, and once a
  new version is in place the other versions of the same area are removed.
- dest.lock (fcntl) keeps two tasks from downloading the same file at once.

Stdlib only; zimscraperlib's retrying session is used where installed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

STAMP_KEYS = ("ETag", "Last-Modified", "Content-Length")
BLOCK = 1 << 20
OFM_PATH = re.compile(r"^/areas/([^/]+)/([^/]+)/tiles\.mbtiles$")


def name_of_url(url: str) -> str:
    """The file name a URL is downloaded to."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[-1])


def head(url: str, user_agent: str) -> dict[str, str] | None:
    """The response headers for `url`: a HEAD request, or when the server
    refuses HEAD, a GET of its first byte (Content-Length then comes from
    Content-Range). None when it cannot be reached."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return dict(r.headers.items())
    except urllib.error.HTTPError:
        pass                                   # HEAD refused (403, 405, ...)
    except OSError:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            h = dict(r.headers.items())
            m = re.match(r"bytes \d+-\d+/(\d+)$", h.get("Content-Range", ""))
            if r.status == 206 and m:
                h["Content-Length"] = m.group(1)
                h.setdefault("Accept-Ranges", "bytes")
            elif r.status != 200:
                return None
            return h
    except OSError:
        return None


def stamp_of(h: dict[str, str] | None) -> dict[str, str] | None:
    """The upstream version in the headers `h`."""
    if h is None:
        return None
    return {k: h.get(k, "") for k in STAMP_KEYS}


class Progress:
    """A file being downloaded into: writes through, logs progress every 5%
    (every GB when the size is unknown) and hands the first bytes of a new
    file to `check_head`, which may stop the download by raising."""

    def __init__(self, f: Any, done: int, total: int,
                 check_head: Callable[[bytes], None] | None = None) -> None:
        self.f, self.done, self.total = f, done, total
        self.check_head = check_head if done == 0 else None
        self.head = b""
        self.step = max(total // 20, 1) if total else 1 << 30
        self.next = (done // self.step + 1) * self.step

    def write(self, data: bytes) -> int:
        if self.check_head is not None:
            self.head += data[:64 - len(self.head)]
            if len(self.head) >= 64:
                self.check_head(self.head)
                self.check_head = None
        self.f.write(data)
        self.done += len(data)
        if self.done >= self.next:
            self.next = (self.done // self.step + 1) * self.step
            pct = f" ({self.done * 100 // self.total}%)" if self.total else ""
            print(f"    {self.done / 1e6:,.0f} MB{pct}", flush=True)
        return len(data)


@contextlib.contextmanager
def _stream(url: str, headers: dict[str, str]
            ) -> Generator[tuple[int, dict[str, str], Iterator[bytes]], None, None]:
    """(status, headers, body blocks) of a GET: zimscraperlib's retrying
    session where it is installed, else urllib."""
    from streetzim import scraperlib
    if scraperlib.AVAILABLE:
        resp = scraperlib.session().get(url, stream=True, headers=headers,
                                        timeout=scraperlib.READ_TIMEOUT)
        try:
            resp.raise_for_status()
            yield (int(resp.status_code), dict(resp.headers.items()),
                   resp.iter_content(BLOCK))
        finally:
            resp.close()
        return
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        yield int(r.status), dict(r.headers.items()), iter(lambda: r.read(BLOCK), b"")


def _download(url: str, part: Path, offset: int, total: int, validator: str,
              user_agent: str, check_head: Callable[[bytes], None] | None) -> bool:
    """Write `url` into `part` from byte `offset` (a Range request with
    If-Range when offset > 0). False when a resume was answered with a
    range that does not start at the offset (nothing written)."""
    headers = {"User-Agent": user_agent}
    if offset:
        headers.update({"Range": f"bytes={offset}-", "If-Range": validator})
    with _stream(url, headers) as (status, h, blocks):
        if offset and status == 200:
            print("    the server sent the whole file: starting over", flush=True)
            offset = 0
        elif offset and not (status == 206
                             and h.get("Content-Range", "").startswith(f"bytes {offset}-")):
            print(f"    unexpected answer to a resume ({status} "
                  f"{h.get('Content-Range', '')!r}): starting over", flush=True)
            return False
        with open(part, "r+b" if part.exists() else "wb") as f:
            f.truncate(offset)
            f.seek(offset)
            sink = Progress(f, offset, total, check_head)
            for block in blocks:
                sink.write(block)
    return True


def ofm_sums_url(url: str) -> str | None:
    """SHA256SUMS of an OpenFreeMap tiles.mbtiles URL; None for other URLs."""
    u = urlsplit(url)
    host = u.hostname or ""
    if u.scheme != "https" or not (host == "openfreemap.com" or host.endswith(".openfreemap.com")):
        return None
    if not OFM_PATH.match(u.path):
        return None
    return f"https://{u.netloc}{u.path.rsplit('/', 1)[0]}/SHA256SUMS"


def expected_sha256(url: str, user_agent: str) -> str | None:
    """The published SHA-256 of `url`, when there is one (OpenFreeMap)."""
    sums = ofm_sums_url(url)
    if sums is None:
        return None
    req = urllib.request.Request(sums, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read(1 << 16).decode("utf-8", "replace")
    except OSError as e:
        print(f"    could not read {sums} ({e}); not checked", flush=True)
        return None
    name = url.rsplit("/", 1)[-1]
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and len(parts[0]) == 64:
            return parts[0].lower()
    return None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    size = path.stat().st_size
    done, step = 0, max(size // 10, BLOCK)
    nxt = step
    with open(path, "rb") as f:
        while block := f.read(8 * BLOCK):
            h.update(block)
            done += len(block)
            if size > 1 << 30 and done >= nxt:
                nxt += step
                print(f"    checksum {done * 100 // size}%", flush=True)
    return h.hexdigest()


def _evict_other_versions(url: str, dest: Path) -> None:
    """After a new OpenFreeMap version of an area is in place, remove the
    other versions of that area from the download folder (each is as big as
    the new one: 103 GB for the planet). One being used by another task
    (its lock held) is left."""
    u = urlsplit(url)
    m = OFM_PATH.match(u.path)
    if not m or ofm_sums_url(url) is None:
        return
    prefix = name_of_url(f"{u.netloc}/areas/{m.group(1)}/")
    suffix = "_tiles.mbtiles"
    for other in sorted(dest.parent.glob(prefix + "*" + suffix)):
        if other.name == dest.name or not other.name.endswith(suffix):
            continue
        with _lock(other, block=False) as got:
            if not got:
                print(f"  Keeping {other.name}: in use by another task", flush=True)
                continue
            size = other.stat().st_size if other.exists() else 0
            for extra in (other, other.with_name(other.name + ".source.json"),
                          other.with_name(other.name + ".part"),
                          other.with_name(other.name + ".part.source.json")):
                extra.unlink(missing_ok=True)
            print(f"  Removed older download {other.name} ({size / 1e9:,.1f} GB)",
                  flush=True)
        other.with_name(other.name + ".lock").unlink(missing_ok=True)


@contextlib.contextmanager
def _lock(dest: Path, *, block: bool = True) -> Generator[bool, None, None]:
    """An exclusive lock on dest.lock; yields whether it was taken."""
    import fcntl
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest.with_name(dest.name + ".lock"), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if not block:
                yield False
                return
            print(f"  Waiting for another task downloading {dest.name}", flush=True)
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def fetch_resumable(url: str, dest: Path, *, user_agent: str,
                    check_head: Callable[[bytes], None] | None = None) -> Path:
    """Download `url` to `dest` as the module docstring describes, and
    return it."""
    with _lock(dest):
        return _fetch_locked(url, dest, user_agent, check_head)


def _fetch_locked(url: str, dest: Path, user_agent: str,
                  check_head: Callable[[bytes], None] | None) -> Path:
    meta = dest.with_name(dest.name + ".source.json")
    part = dest.with_name(dest.name + ".part")
    part_meta = part.with_name(part.name + ".source.json")
    h = head(url, user_agent)
    stamp = stamp_of(h)
    if dest.exists() and dest.stat().st_size > 0:
        if stamp is None:
            print(f"  Reusing {dest} (could not check {url} for updates)")
            return dest
        if _read_json(meta) == stamp:
            print(f"  Reusing {dest} (unchanged upstream)")
            return dest
        if not meta.exists() and stamp["Content-Length"] == str(dest.stat().st_size):
            want = expected_sha256(url, user_agent)
            if want is None or sha256_of(dest) == want:
                print(f"  Reusing {dest} (same size as upstream"
                      + (", same SHA-256)" if want else ")"))
                meta.write_text(json.dumps(stamp))
                return dest
            print(f"  {dest} is not the upstream file (SHA-256 differs): downloading")
    if h is None or stamp is None:
        raise OSError(f"cannot reach {url}")
    total = int(stamp["Content-Length"] or 0)
    validator = stamp["ETag"] or stamp["Last-Modified"]
    offset = part.stat().st_size if part.exists() else 0
    resumable = (h.get("Accept-Ranges", "").lower() == "bytes" and bool(validator)
                 and _read_json(part_meta) == stamp and 0 < offset <= total)
    if not resumable:
        offset = 0
        part.unlink(missing_ok=True)
    part_meta.write_text(json.dumps(stamp))
    for attempt in (1, 2):
        if offset and offset == total:
            print(f"  {part.name} is complete: not downloading it again")
            if check_head is not None:
                with open(part, "rb") as f:
                    check_head(f.read(64))
            break
        if offset:
            print(f"  Resuming {url} at {offset / 1e6:,.0f} of {total / 1e6:,.0f} MB")
            if check_head is not None:
                with open(part, "rb") as f:
                    check_head(f.read(64))
        else:
            print(f"  Downloading {url}" + (f" ({total / 1e6:,.0f} MB)" if total else ""))
        try:
            wrote = _download(url, part, offset, total, validator, user_agent, check_head)
        except ValueError:                      # not the file wanted: don't resume it
            part.unlink(missing_ok=True)
            part_meta.unlink(missing_ok=True)
            raise
        size = part.stat().st_size if part.exists() else 0
        if wrote and (not total or size == total):
            break
        if attempt == 2 or not offset:
            raise OSError(f"{url}: got {size:,} bytes, expected {total:,}")
        offset = 0                              # a resume that went wrong: start over
    want = expected_sha256(url, user_agent)
    if want is not None:
        got = sha256_of(part)
        if got != want:
            part.unlink(missing_ok=True)
            part_meta.unlink(missing_ok=True)
            raise OSError(f"{url}: SHA-256 {got} is not the published {want}")
        print("    SHA-256 matches the published checksum", flush=True)
    os.replace(part, dest)
    meta.write_text(json.dumps(stamp))
    part_meta.unlink(missing_ok=True)
    _evict_other_versions(url, dest)
    return dest
