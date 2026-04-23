"""Tests for byte-range chunking of the routing graph.

The writer splits routing-graph.bin into fixed-size entries
(``routing-data/graph-chunk-0000.bin`` etc) plus a manifest. The reader
reassembles them in the manifest order and verifies sha256 before
parsing. Torn uploads must fail loud rather than silently produce a
garbage graph.

The end-to-end check: convert a real v4 graph into chunks in memory,
parse back, and prove the reassembled bytes (and therefore every route)
are identical to the un-chunked source.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from create_osm_zim import chunk_graph_file  # noqa: E402


def _write_bytes(tmp: Path, name: str, data: bytes) -> str:
    p = tmp / name
    p.write_bytes(data)
    return str(p)


def test_chunker_splits_evenly(tmp_path):
    payload = b"".join(bytes([i & 0xFF]) * 100 for i in range(10))  # 1000 B
    src = _write_bytes(tmp_path, "graph.bin", payload)
    chunks, manifest = chunk_graph_file(src, 300, out_prefix="chunk")
    # 1000 / 300 → 3 x 300 + 1 x 100
    assert len(chunks) == 4
    assert [os.path.getsize(c) for c in chunks] == [300, 300, 300, 100]
    assert manifest["total_bytes"] == 1000
    assert manifest["schema"] == 1
    assert len(manifest["chunks"]) == 4
    assert manifest["chunks"][0]["path"] == "chunk-0000.bin"


def test_chunker_sha_is_source_hash(tmp_path):
    payload = os.urandom(5000)
    src = _write_bytes(tmp_path, "graph.bin", payload)
    expected_sha = hashlib.sha256(payload).hexdigest()
    _, manifest = chunk_graph_file(src, 1024, out_prefix="chunk")
    assert manifest["sha256"] == expected_sha


def test_chunker_concat_matches_source(tmp_path):
    payload = os.urandom(12345)
    src = _write_bytes(tmp_path, "graph.bin", payload)
    chunks, manifest = chunk_graph_file(src, 1024, out_prefix="chunk")
    concat = b"".join(Path(c).read_bytes() for c in chunks)
    assert concat == payload
    assert hashlib.sha256(concat).hexdigest() == manifest["sha256"]


def test_chunker_rejects_nonpositive_chunk_size(tmp_path):
    src = _write_bytes(tmp_path, "graph.bin", b"abc")
    with pytest.raises(ValueError, match="must be positive"):
        chunk_graph_file(src, 0, out_prefix="chunk")


# ---- End-to-end: v4 graph → chunks → reassemble → parse ----

def test_v4_chunk_roundtrip_preserves_bytes(tmp_path):
    """Simulate the full chunk → reassemble cycle on a real v4 graph."""
    zim_path = ROOT / "osm-washington-dc-2026-04-22.zim"
    if not zim_path.is_file():
        pytest.skip(f"missing {zim_path.name} for end-to-end chunk test")

    from libzim.reader import Archive
    arc = Archive(str(zim_path))
    original = bytes(arc.get_entry_by_path("routing-data/graph.bin").get_item().content)

    src = _write_bytes(tmp_path, "routing-graph.bin", original)
    chunks, manifest = chunk_graph_file(
        src, 10 * 1024 * 1024, out_prefix="routing-graph-chunk"
    )
    # Reassemble via manifest order (mirrors what _fetch_chunked_blob does).
    concat = b"".join(Path(c).read_bytes() for c in chunks)
    assert concat == original
    assert hashlib.sha256(concat).hexdigest() == manifest["sha256"]

    # The reassembled bytes should still parse cleanly — catches bugs
    # where chunking left an extra byte at EOF or truncated the last piece.
    from tests.szrg_reader import parse_szrg_bytes
    g = parse_szrg_bytes(concat)
    assert g.num_nodes > 0 and g.num_edges > 0
