"""Dirent.parse on a Source (MmapSource), not only on bytes: a dirent
longer than the first 512-byte window, and one cut off by the end of the
file. A Source has .size but no len(); parse used to call len() anyway."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud import zimfmt  # noqa: E402


def _dirent(url: bytes, title: bytes) -> bytes:
    return (struct.pack("<HBcIII", 0, 0, b"C", 0, 7, 3)
            + url + b"\0" + title + b"\0")


def _source(tmp_path, data: bytes):
    path = tmp_path / "d.bin"
    path.write_bytes(data)
    return zimfmt.MmapSource(str(path))


def test_long_dirent_from_a_source(tmp_path):
    url = b"x" * 900
    src = _source(tmp_path, b"\0" * 5 + _dirent(url, b"t") + b"\0" * 3)
    try:
        d = zimfmt.Dirent.parse(src, 5)
    finally:
        src.close()
    assert (d.url, d.title, d.cluster, d.blob) == (url, b"t", 7, 3)


def test_truncated_dirent_from_a_source(tmp_path):
    src = _source(tmp_path, _dirent(b"y" * 900, b"t")[:700])
    try:
        with pytest.raises(ValueError, match="truncated dirent at 0"):
            zimfmt.Dirent.parse(src, 0)
    finally:
        src.close()
