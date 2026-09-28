"""What StreetZim relies on from stock libzim (python-libzim), so that the
whole pipeline keeps working without the optional Rust packer (zimru):

* ``Hint.COMPRESS: False`` stores an entry's bytes verbatim in an
  uncompressed cluster. ``cloud/patch_viewer_inplace.py`` finds the viewer
  slots by scanning the raw file, and large routing blobs are kept raw so
  readers never have to inflate a >500 MB cluster (docs/formats.md).
* The archive ends with a 16-byte MD5 of everything before it, which the
  in-place patcher recomputes.
* The viewer slot padding round-trips through libzim and back out of the
  reader byte-for-byte.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

libzim = pytest.importorskip("libzim")
from libzim.reader import Archive  # noqa: E402
from libzim.writer import Creator, Hint, Item, StringProvider  # noqa: E402

from cloud.viewer_slots import pad_to_slot, slot_needle  # noqa: E402


class _Item(Item):
    def __init__(self, path, mime, data, compress):
        super().__init__()
        self._p, self._m, self._d, self._c = path, mime, data, compress

    def get_path(self): return self._p
    def get_title(self): return self._p
    def get_mimetype(self): return self._m
    def get_contentprovider(self): return StringProvider(self._d)
    def get_hints(self): return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: self._c}


def _build(tmp_path, items):
    path = tmp_path / "c.zim"
    with Creator(str(path)).config_indexing(False, "eng") as c:
        c.set_mainpath(items[0][0])
        for p, m, d, comp in items:
            c.add_item(_Item(p, m, d, comp))
    return path


def test_uncompressed_hint_stores_bytes_verbatim(tmp_path):
    # Highly compressible, so a compressed cluster could never contain it raw.
    raw = b"SZRG" + b"\x00\x01\x02\x03" * 50_000
    squeezed = b"compressible " * 20_000
    path = _build(tmp_path, [
        ("index.html", "text/html", b"<html></html>", True),
        ("routing-data/graph.bin", "application/octet-stream", raw, False),
        ("search-data/a.json", "application/json", squeezed, True),
    ])
    blob = path.read_bytes()
    assert raw in blob, "COMPRESS=False entry was compressed"
    assert squeezed not in blob, "COMPRESS=True entry was stored raw"
    assert bytes(Archive(str(path)).get_entry_by_path(
        "routing-data/graph.bin").get_item().content) == raw


def test_trailing_md5_is_what_the_inplace_patcher_expects(tmp_path):
    path = _build(tmp_path, [("index.html", "text/html", b"<html></html>", True)])
    blob = path.read_bytes()
    assert hashlib.md5(blob[:-16]).digest() == blob[-16:]
    assert Archive(str(path)).check()


def test_viewer_slot_roundtrips_through_libzim(tmp_path):
    html = (ROOT / "resources/viewer/index.html").read_bytes()
    slotted = pad_to_slot("index.html", html)
    path = _build(tmp_path, [("index.html", "text/html", slotted, False)])
    assert slot_needle("index.html") in path.read_bytes()
    got = bytes(Archive(str(path)).get_entry_by_path("index.html").get_item().content)
    assert got == slotted and got.startswith(html)
