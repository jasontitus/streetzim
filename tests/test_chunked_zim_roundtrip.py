"""ZIM-layer round-trip for chunked routing graph entries.

Writes a real libzim ZIM whose ``routing-data`` directory contains the
chunked layout (manifest + graph-chunk-*.bin), then reads it back with
``load_from_zim`` — which transparently reassembles. Confirms the SZRG
parser sees identical bytes to the original graph.

Skipped if libzim isn't installed (same as other ZIM-touching tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def synthetic_v4_graph() -> bytes:
    """Build a tiny but valid v4 SZRG buffer — enough to parse and route."""
    num_nodes = 3
    num_edges = 2
    num_geoms = 0
    geom_bytes = 0
    num_names = 1
    names_bytes = 0

    header = b"SZRG" + struct.pack("<7I",
                                    4,
                                    num_nodes, num_edges,
                                    num_geoms, geom_bytes,
                                    num_names, names_bytes)
    nodes = struct.pack(f"<{num_nodes * 2}i",
                        0, 0,
                        100000, 0,
                        200000, 0)
    adj = struct.pack("<4I", 0, 1, 2, 2)
    # 2 edges, stride 5: (target, dist_speed, geom_idx, name_idx, class_access)
    e1 = struct.pack("<5I", 1, (30 << 24) | 10000, 0xFFFFFFFF, 0, 0)
    e2 = struct.pack("<5I", 2, (30 << 24) | 10000, 0xFFFFFFFF, 0, 0)
    geom_offsets = struct.pack("<I", 0)  # one sentinel entry, num_geoms+1 = 1
    name_offsets = struct.pack("<2I", 0, 0)
    return header + nodes + adj + e1 + e2 + geom_offsets + name_offsets


def test_chunked_zim_roundtrip(synthetic_v4_graph, tmp_path):
    try:
        from libzim.reader import Archive
        from libzim.writer import Creator, Item, StringProvider, Hint
    except ImportError:
        pytest.skip("libzim not installed")

    from create_osm_zim import chunk_graph_file
    from tests.szrg_reader import load_from_zim

    # Write the graph to a tempfile and chunk it.
    graph_bin = tmp_path / "routing-graph.bin"
    graph_bin.write_bytes(synthetic_v4_graph)
    chunks, manifest = chunk_graph_file(
        str(graph_bin), 32, out_prefix="graph-chunk")
    assert len(chunks) >= 2, "need at least 2 chunks to exercise reassembly"

    zim_path = tmp_path / "chunked.zim"

    class MapItem(Item):
        def __init__(self, path, mime, data):
            super().__init__()
            self._path = path
            self._mime = mime
            self._data = data
        def get_path(self): return self._path
        def get_title(self): return self._path
        def get_mimetype(self): return self._mime
        def get_contentprovider(self): return StringProvider(self._data)
        def get_hints(self): return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: True}

    with Creator(str(zim_path)) as c:
        c.add_metadata("Title", "chunked test")
        c.add_metadata("Description", "chunked test")
        c.add_metadata("Language", "en")
        c.add_metadata("Creator", "test")
        c.add_metadata("Publisher", "test")
        c.add_metadata("Date", "2026-04-22")
        c.add_metadata("Name", "test")
        # index.html — libzim refuses to create a ZIM without a mainPath.
        c.add_item(MapItem("index.html", "text/html", b"<html></html>"))
        c.set_mainpath("index.html")
        c.add_item(MapItem(
            "routing-data/graph-chunk-manifest.json",
            "application/json",
            json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
        ))
        for i, cp in enumerate(chunks):
            c.add_item(MapItem(
                f"routing-data/graph-chunk-{i:04d}.bin",
                "application/octet-stream",
                Path(cp).read_bytes(),
            ))

    # Now ask load_from_zim to resolve + parse — it should transparently
    # reassemble the chunks and hand us back a graph byte-identical to
    # the original.
    g = load_from_zim(zim_path)
    assert g.version == 4
    assert g.num_nodes == 3
    assert g.num_edges == 2


def test_torn_chunk_rejected(tmp_path):
    """If a chunk's size doesn't match the manifest, loading must fail
    loudly rather than silently producing a broken graph."""
    try:
        from libzim.reader import Archive
        from libzim.writer import Creator, Item, StringProvider, Hint
    except ImportError:
        pytest.skip("libzim not installed")

    from create_osm_zim import chunk_graph_file
    from tests.szrg_reader import load_from_zim

    # Build a valid graph, chunk it, but then mutate the manifest to lie.
    num_nodes = 2
    num_edges = 0
    header = b"SZRG" + struct.pack("<7I", 4, num_nodes, num_edges, 0, 0, 1, 0)
    nodes = struct.pack("<4i", 0, 0, 100000, 0)
    adj = struct.pack("<3I", 0, 0, 0)
    geom_offsets = struct.pack("<I", 0)
    name_offsets = struct.pack("<2I", 0, 0)
    graph_bytes = header + nodes + adj + geom_offsets + name_offsets
    graph_bin = tmp_path / "routing-graph.bin"
    graph_bin.write_bytes(graph_bytes)
    chunks, manifest = chunk_graph_file(
        str(graph_bin), 32, out_prefix="graph-chunk")

    # Pretend the first chunk is longer than it actually is.
    manifest["chunks"][0]["bytes"] += 1

    zim_path = tmp_path / "torn.zim"

    class MapItem(Item):
        def __init__(self, path, mime, data):
            super().__init__()
            self._path = path
            self._mime = mime
            self._data = data
        def get_path(self): return self._path
        def get_title(self): return self._path
        def get_mimetype(self): return self._mime
        def get_contentprovider(self): return StringProvider(self._data)
        def get_hints(self): return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: True}

    with Creator(str(zim_path)) as c:
        for k in ("Title", "Description", "Language", "Creator",
                   "Publisher", "Date", "Name"):
            c.add_metadata(k, "x")
        c.add_item(MapItem("index.html", "text/html", b"<html></html>"))
        c.set_mainpath("index.html")
        c.add_item(MapItem(
            "routing-data/graph-chunk-manifest.json",
            "application/json",
            json.dumps(manifest).encode("utf-8"),
        ))
        for i, cp in enumerate(chunks):
            c.add_item(MapItem(
                f"routing-data/graph-chunk-{i:04d}.bin",
                "application/octet-stream",
                Path(cp).read_bytes(),
            ))

    with pytest.raises(Exception, match="(size mismatch|sha256|total bytes)"):
        load_from_zim(zim_path)
