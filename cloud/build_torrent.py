#!/usr/bin/env python3
"""Generate a single-file BitTorrent .torrent referencing a ZIM hosted on
archive.org via HTTP webseed (BEP 19).

archive.org's auto-generated `_archive.torrent` files include the full
file list — every active ZIM plus all `history/files/*.zim.~N~` backups
that ever lived on the item. Default-on torrent clients then download
3-5x more bytes than the user actually needs. We sidestep the whole
problem by generating our own torrent that references just one file
(the current ZIM), with archive.org's HTTP URL as a webseed and a mix
of archive.org's own trackers + DHT for peer discovery.

The hash computation streams the bytes from archive.org once — no
intermediate file on disk — so generating torrents for all regions
takes O(transfer_time) without consuming GB of local storage.

Usage:
  python3 cloud/build_torrent.py <zim_url> <output.torrent>

Example:
  python3 cloud/build_torrent.py \\
      https://archive.org/download/streetzim-africa/osm-africa-2026-04-29.zim \\
      web/torrents/africa.torrent
"""
import argparse
import hashlib
import os
import sys
import time
import urllib.request
import urllib.parse


# Trackers we list in the torrent. archive.org's two trackers accept
# arbitrary infohashes (they coordinate by hash, not by item ownership).
# Three popular open trackers are included so DHT-disabled clients still
# find peers if any volunteer seeds spring up.
TRACKERS = [
    "http://bt1.archive.org:6969/announce",
    "http://bt2.archive.org:6969/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://9.rarbg.to:2710/announce",
]


def auto_piece_size(file_size_bytes: int) -> int:
    """Pick a piece size that yields ~1000-4000 pieces.

    Powers of 2 only — torrent spec requires it. For very large files
    (≥ 16 GB) we cap at 8 MiB so any one piece-failure on a flaky
    connection doesn't force re-downloading more than 8 MiB.
    """
    target_pieces = 1500
    raw = max(file_size_bytes // target_pieces, 16 * 1024)
    # round up to next power of 2
    size = 1
    while size < raw:
        size <<= 1
    # clamp
    return min(max(size, 256 * 1024), 8 * 1024 * 1024)


def stream_piece_hashes(url: str, piece_size: int):
    """Stream `url` and return (concatenated_sha1_hashes, total_size).

    The bytes are NOT stored anywhere — each piece is hashed and
    discarded. Memory use stays at one piece (≤ 8 MiB) regardless of
    file size, so this scales to 20+ GB ZIMs without trouble.
    """
    pieces = bytearray()
    total = 0
    buf = bytearray()
    started = time.time()
    last_log = started

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "streetzim-torrent-gen/1.0"},
    )
    # archive.org occasionally redirects to a specific ia*.us.archive.org
    # mirror; urllib follows redirects automatically.
    with urllib.request.urlopen(req, timeout=60) as resp:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            buf.extend(chunk)
            total += len(chunk)
            while len(buf) >= piece_size:
                pieces.extend(hashlib.sha1(bytes(buf[:piece_size])).digest())
                del buf[:piece_size]
            now = time.time()
            if now - last_log >= 5.0:
                mb = total / (1024 * 1024)
                rate = mb / max(now - started, 0.001)
                print(f"    streamed {mb:.0f} MB ({rate:.1f} MB/s)", flush=True)
                last_log = now
        if buf:
            pieces.extend(hashlib.sha1(bytes(buf)).digest())
    elapsed = time.time() - started
    rate = (total / (1024 * 1024)) / max(elapsed, 0.001)
    print(f"    done: {total/1e9:.2f} GB in {elapsed:.0f}s ({rate:.1f} MB/s)")
    return bytes(pieces), total


def bencode(value) -> bytes:
    """Minimal bencode encoder. Handles bytes/str/int/list/dict only."""
    if isinstance(value, int) and not isinstance(value, bool):
        return f"i{value}e".encode("ascii")
    if isinstance(value, str):
        b = value.encode("utf-8")
        return f"{len(b)}:".encode("ascii") + b
    if isinstance(value, (bytes, bytearray)):
        return f"{len(value)}:".encode("ascii") + bytes(value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(x) for x in value) + b"e"
    if isinstance(value, dict):
        # bencode requires keys sorted by raw byte value. Encode keys as
        # bytes once so the comparison is consistent regardless of input
        # being str or bytes.
        items = []
        for k, v in value.items():
            kb = k.encode("utf-8") if isinstance(k, str) else bytes(k)
            items.append((kb, v))
        items.sort(key=lambda kv: kv[0])
        out = b"d"
        for kb, v in items:
            out += bencode(kb) + bencode(v)
        return out + b"e"
    raise TypeError(f"cannot bencode {type(value).__name__}")


def build_torrent(zim_url: str, out_path: str, comment: str = "") -> None:
    """Write a single-file .torrent for `zim_url` to `out_path`."""
    # File name is the basename of the URL path, URL-decoded.
    parsed = urllib.parse.urlparse(zim_url)
    file_name = urllib.parse.unquote(os.path.basename(parsed.path))
    if not file_name:
        raise ValueError(f"could not derive filename from {zim_url}")

    # Probe size first so we can pick a good piece size before streaming.
    head = urllib.request.Request(
        zim_url,
        method="HEAD",
        headers={"User-Agent": "streetzim-torrent-gen/1.0"},
    )
    with urllib.request.urlopen(head, timeout=30) as resp:
        size = int(resp.headers["Content-Length"])
    piece_size = auto_piece_size(size)
    n_pieces = (size + piece_size - 1) // piece_size
    print(f"  {file_name}: {size/1e9:.2f} GB, piece={piece_size//1024} KiB, "
          f"{n_pieces} pieces")

    pieces, streamed_size = stream_piece_hashes(zim_url, piece_size)
    if streamed_size != size:
        raise RuntimeError(
            f"size mismatch: HEAD said {size}, streamed {streamed_size}"
        )

    info = {
        "name": file_name,
        "piece length": piece_size,
        "pieces": pieces,
        "length": size,
    }
    torrent = {
        "announce": TRACKERS[0],
        "announce-list": [[t] for t in TRACKERS],
        "comment": comment or f"streetzim — {file_name}",
        "created by": "streetzim/build_torrent.py",
        "creation date": int(time.time()),
        "info": info,
        # BEP 19 webseed: the canonical archive.org URL serves the
        # bytes directly, so even a torrent with zero peers downloads
        # at full HTTP speed.
        "url-list": [zim_url],
    }
    encoded = bencode(torrent)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(encoded)
    print(f"  wrote {out_path} ({len(encoded)} bytes)")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("zim_url", help="HTTP URL of the ZIM file (archive.org)")
    p.add_argument("out", help="Output path for .torrent")
    p.add_argument("--comment", default="", help="Torrent comment field")
    args = p.parse_args()
    try:
        build_torrent(args.zim_url, args.out, comment=args.comment)
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
