"""In-memory v4 → v5 converter used by end-to-end correctness testing.

Converts a real v4 SZRG buffer (pulled from an existing ZIM) into the
v5 split pair (main buf, SZGM geoms buf) WITHOUT going through the full
ZIM-builder. That lets the route-identity suite run in seconds instead
of the 5–20 min a real rebuild costs.

Byte-identical to what ``extract_routing_graph(split_graph=True)`` in
``create_osm_zim.py`` emits — if this converter ever drifts, the end-
to-end test will catch it.
"""

from __future__ import annotations

import struct


def v4_to_v5_bufs(v4_buf: bytes) -> tuple[bytes, bytes]:
    """Given a v4 SZRG buffer, return (main_v5_buf, szgm_buf)."""
    if v4_buf[:4] != b"SZRG":
        raise ValueError("Not a SZRG buffer")
    version, num_nodes, num_edges, num_geoms, geom_bytes_total, num_names, names_bytes = \
        struct.unpack_from("<7I", v4_buf, 4)
    if version != 4:
        raise ValueError(f"expected v4, got v{version}")

    edge_stride = 5
    off = 32
    nodes_len = num_nodes * 2 * 4
    adj_len = (num_nodes + 1) * 4
    edges_len = num_edges * edge_stride * 4
    geom_offsets_len = (num_geoms + 1) * 4
    name_offsets_len = (num_names + 1) * 4

    nodes = v4_buf[off:off + nodes_len]; off += nodes_len
    adj = v4_buf[off:off + adj_len]; off += adj_len
    edges = v4_buf[off:off + edges_len]; off += edges_len
    geom_offsets = v4_buf[off:off + geom_offsets_len]; off += geom_offsets_len
    geom_blob = v4_buf[off:off + geom_bytes_total]; off += geom_bytes_total
    name_offsets = v4_buf[off:off + name_offsets_len]; off += name_offsets_len
    names_blob = v4_buf[off:off + names_bytes]

    main_header = b"SZRG" + struct.pack("<7I",
                                        5,
                                        num_nodes, num_edges,
                                        num_geoms, 0,
                                        num_names, names_bytes)
    main_buf = main_header + nodes + adj + edges + name_offsets + names_blob

    szgm_buf = (b"SZGM" + struct.pack("<3I", 1, num_geoms, geom_bytes_total)
                + geom_offsets + geom_blob)

    return main_buf, szgm_buf
