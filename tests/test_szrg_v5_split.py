"""SZRG v5 split-layout tests.

v5 goal: move the ~33% of graph.bin that's polyline geometry into a
companion ZIM entry so we can defer loading until a route needs drawing.
Routing (A*) only reads the main graph.bin; geom decoding happens later
via a separate SZGM blob.

These tests build tiny v4 and v5 buffers in memory and verify that:
  1. v5 main parses without the companion and A* still works (geom-less).
  2. Attaching the SZGM companion yields geom_offsets/geom_blob equal to
     what a v4 writer would have emitted inline.
  3. Route identity: v4-inline vs v5-split of the same topology produce
     byte-identical route fingerprints.
"""

from __future__ import annotations

import struct

import pytest

from tests.szrg_reader import parse_szgm_bytes, parse_szrg_bytes
from tests.szrg_astar import find_route


def _pack_common(nodes_e7, edges, names=("",)):
    num_nodes = len(nodes_e7)
    num_edges = len(edges)
    num_names = len(names)

    nodes_blob = struct.pack(f"<{num_nodes * 2}i",
                             *[v for (lat, lon) in nodes_e7 for v in (lat, lon)])
    edges_sorted = sorted(edges, key=lambda e: e[0])
    adj_offsets = [0] * (num_nodes + 1)
    for e in edges_sorted:
        adj_offsets[e[0] + 1] += 1
    for i in range(1, num_nodes + 1):
        adj_offsets[i] += adj_offsets[i - 1]
    adj_blob = struct.pack(f"<{num_nodes + 1}I", *adj_offsets)

    edge_vals = []
    for (_from_idx, target, dist_dm, speed, geom_idx, name_idx) in edges_sorted:
        dist_speed = ((speed & 0xFF) << 24) | (dist_dm & 0xFFFFFF)
        edge_vals.extend([target, dist_speed, geom_idx, name_idx, 0])
    edges_blob = struct.pack(f"<{len(edge_vals)}I", *edge_vals)

    name_offsets = [0]
    name_bytes = b""
    for n in names:
        nb = n.encode("utf-8")
        name_bytes += nb
        name_offsets.append(len(name_bytes))
    name_offsets_blob = struct.pack(f"<{num_names + 1}I", *name_offsets)

    return (num_nodes, num_edges, num_names,
            nodes_blob, adj_blob, edges_blob,
            name_offsets_blob, name_bytes)


def _pack_v4_inline(nodes_e7, edges, names, num_geoms, geom_bytes_total,
                    geom_offsets_blob, geom_blob):
    (num_nodes, num_edges, num_names,
     nodes_blob, adj_blob, edges_blob,
     name_offsets_blob, name_bytes) = _pack_common(nodes_e7, edges, names)
    header = (b"SZRG" + struct.pack("<7I",
                                    4,
                                    num_nodes, num_edges,
                                    num_geoms, geom_bytes_total,
                                    num_names, len(name_bytes)))
    return (header + nodes_blob + adj_blob + edges_blob
            + geom_offsets_blob + geom_blob
            + name_offsets_blob + name_bytes)


def _pack_v5_split(nodes_e7, edges, names, num_geoms, geom_bytes_total,
                   geom_offsets_blob, geom_blob):
    (num_nodes, num_edges, num_names,
     nodes_blob, adj_blob, edges_blob,
     name_offsets_blob, name_bytes) = _pack_common(nodes_e7, edges, names)
    main_header = (b"SZRG" + struct.pack("<7I",
                                         5,
                                         num_nodes, num_edges,
                                         num_geoms, 0,  # geomBytes=0 → external
                                         num_names, len(name_bytes)))
    main_buf = (main_header + nodes_blob + adj_blob + edges_blob
                + name_offsets_blob + name_bytes)
    geoms_buf = (b"SZGM" + struct.pack("<3I", 1, num_geoms, geom_bytes_total)
                 + geom_offsets_blob + geom_blob)
    return main_buf, geoms_buf


def _tiny_graph(num_geoms=0, geom_bytes_total=0, geom_offsets_blob=None,
                geom_blob=b""):
    """Shared three-node chain: 0 -> 1 -> 2."""
    if geom_offsets_blob is None:
        geom_offsets_blob = struct.pack("<I", 0)  # closing offset only
    nodes_e7 = [(0, 0), (100000, 0), (200000, 0)]
    edges = [
        (0, 1, 10000, 30, 0xFFFFFFFF, 0),
        (1, 2, 10000, 30, 0xFFFFFFFF, 0),
    ]
    return dict(
        nodes_e7=nodes_e7, edges=edges, names=("",),
        num_geoms=num_geoms, geom_bytes_total=geom_bytes_total,
        geom_offsets_blob=geom_offsets_blob, geom_blob=geom_blob,
    )


def test_v5_header_declares_geom_blob_external():
    main_buf, geoms_buf = _pack_v5_split(**_tiny_graph())
    # Byte 4..8 = version, byte 20..24 = geomBytes
    version = struct.unpack_from("<I", main_buf, 4)[0]
    geom_bytes_in_header = struct.unpack_from("<I", main_buf, 20)[0]
    assert version == 5
    assert geom_bytes_in_header == 0, (
        "v5 main must advertise geomBytes=0 so v4-only parsers don't try to "
        "read past EOF"
    )
    assert geoms_buf[:4] == b"SZGM"


def test_v5_parse_main_only_has_no_geoms():
    main_buf, _ = _pack_v5_split(**_tiny_graph())
    g = parse_szrg_bytes(main_buf)
    assert g.version == 5
    assert g.has_geoms is False
    assert g.geom_blob == b""
    # Routing still works without geoms.
    r = find_route(g, 0, 2)
    assert r is not None
    assert r.node_sequence == [0, 1, 2]


def test_v5_parse_with_geoms_matches_v4_decode():
    """Build a v4 buffer and the equivalent v5 pair from the same topology.
    After v5.attach_geoms(), both must expose identical geom_offsets and
    geom_blob bytes."""
    # Two geoms totalling 16 bytes (two absolute int32 pairs).
    g0 = struct.pack("<ii", 1000, 2000)
    g1 = struct.pack("<ii", 3000, 4000)
    geom_blob = g0 + g1
    geom_offsets = struct.pack("<3I", 0, len(g0), len(g0) + len(g1))

    cfg = _tiny_graph(num_geoms=2, geom_bytes_total=len(geom_blob),
                      geom_offsets_blob=geom_offsets, geom_blob=geom_blob)

    buf4 = _pack_v4_inline(**cfg)
    g4 = parse_szrg_bytes(buf4)
    assert g4.geom_blob == geom_blob
    assert list(g4.geom_offsets) == [0, 8, 16]

    main_buf, geoms_buf = _pack_v5_split(**cfg)
    g5 = parse_szrg_bytes(main_buf)
    assert g5.has_geoms is False

    # Parse the companion directly to make sure its format is sane.
    g5_offsets, g5_blob, n = parse_szgm_bytes(geoms_buf)
    assert n == 2
    assert g5_blob == geom_blob
    assert list(g5_offsets) == [0, 8, 16]

    # Attach via the SZRG helper — should match v4 byte-for-byte.
    g5.attach_geoms(geoms_buf)
    assert g5.has_geoms
    assert g5.geom_blob == g4.geom_blob
    assert list(g5.geom_offsets) == list(g4.geom_offsets)


def test_v5_route_identity_with_v4_on_small_graph():
    cfg = _tiny_graph()
    buf4 = _pack_v4_inline(**cfg)
    main_buf, geoms_buf = _pack_v5_split(**cfg)

    g4 = parse_szrg_bytes(buf4)
    g5 = parse_szrg_bytes(main_buf)
    g5.attach_geoms(geoms_buf)

    r4 = find_route(g4, 0, 2)
    r5 = find_route(g5, 0, 2)
    assert r4 is not None and r5 is not None
    assert r4.fingerprint() == r5.fingerprint()


def test_v5_attach_rejects_mismatched_companion():
    main_buf, _ = _pack_v5_split(**_tiny_graph(num_geoms=2,
        geom_bytes_total=4,
        geom_offsets_blob=struct.pack("<3I", 0, 2, 4),
        geom_blob=b"\x00\x01\x02\x03"))
    # Companion built for 1 geom, not 2.
    bogus = (b"SZGM" + struct.pack("<3I", 1, 1, 4)
             + struct.pack("<2I", 0, 4)
             + b"\x00\x00\x00\x00")
    g = parse_szrg_bytes(main_buf)
    with pytest.raises(ValueError, match="SZGM numGeoms"):
        g.attach_geoms(bogus)


def test_v5_bad_szgm_magic_rejected():
    with pytest.raises(ValueError, match="SZGM"):
        parse_szgm_bytes(b"NOPE" + b"\x00" * 12)
