"""Unit tests for SZRG parser + A*.

Focus: catch subtle bit-layout bugs before they compound into a route
divergence. Most tests hand-pack a small SZRG buffer so the failures
point directly at the offending column/bitshift.
"""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from tests.szrg_reader import parse_szrg_bytes
from tests.szrg_astar import find_route, haversine_m


def _pack_v4_graph(nodes_e7: list[tuple[int, int]],
                   edges: list[tuple[int, int, int, int, int, int]],
                   # edges = [(from_idx, target, dist_m*10, speed_kph, geom_idx, name_idx), ...]
                   # class_access fixed to 0 for simplicity
                   names: list[str] = ("",)) -> bytes:
    """Hand-pack a v4 SZRG buffer. Returns bytes."""
    num_nodes = len(nodes_e7)
    num_edges = len(edges)
    num_geoms = 0
    num_names = len(names)

    # Nodes: Int32Array [lat_e7, lon_e7, ...]
    nodes_blob = struct.pack(f"<{num_nodes * 2}i",
                             *[v for (lat, lon) in nodes_e7 for v in (lat, lon)])

    # Sort edges by from_idx (routing reader expects this)
    edges_sorted = sorted(edges, key=lambda e: e[0])

    # Build adj_offsets[num_nodes+1]
    adj_offsets = [0] * (num_nodes + 1)
    for e in edges_sorted:
        adj_offsets[e[0] + 1] += 1
    for i in range(1, num_nodes + 1):
        adj_offsets[i] += adj_offsets[i - 1]
    adj_blob = struct.pack(f"<{num_nodes + 1}I", *adj_offsets)

    # Edges: v4 stride 5: target, dist_speed, geom_idx, name_idx, class_access
    edge_vals = []
    for (_from_idx, target, dist_dm, speed, geom_idx, name_idx) in edges_sorted:
        dist_speed = ((speed & 0xFF) << 24) | (dist_dm & 0xFFFFFF)
        edge_vals.extend([target, dist_speed, geom_idx, name_idx, 0])
    edges_blob = struct.pack(f"<{len(edge_vals)}I", *edge_vals)

    # No geoms — just the closing offset
    geom_offsets_blob = struct.pack("<I", 0)
    geom_blob = b""

    # Names
    name_offsets = [0]
    name_bytes = b""
    for name in names:
        nb = name.encode("utf-8")
        name_bytes += nb
        name_offsets.append(len(name_bytes))
    name_offsets_blob = struct.pack(f"<{num_names + 1}I", *name_offsets)

    header = (b"SZRG" + struct.pack("<7I",
                                    4,              # version
                                    num_nodes,
                                    num_edges,
                                    num_geoms,
                                    0,              # geom_bytes
                                    num_names,
                                    len(name_bytes)))
    return (header + nodes_blob + adj_blob + edges_blob
            + geom_offsets_blob + geom_blob
            + name_offsets_blob + name_bytes)


def test_parse_magic_version_v4():
    buf = _pack_v4_graph(
        nodes_e7=[(375000000, -1220000000), (375010000, -1220000000)],
        edges=[(0, 1, 1000, 50, 0xFFFFFFFF, 0)],
    )
    g = parse_szrg_bytes(buf)
    assert g.version == 4
    assert g.num_nodes == 2
    assert g.num_edges == 1


def test_parse_rejects_bad_magic():
    buf = b"ZZZZ" + b"\x00" * 28
    with pytest.raises(ValueError):
        parse_szrg_bytes(buf)


def test_parse_rejects_unknown_version():
    bad = b"SZRG" + struct.pack("<7I", 99, 0, 0, 0, 0, 1, 0) + struct.pack("<I", 0)
    with pytest.raises(ValueError):
        parse_szrg_bytes(bad)


def test_edge_dist_speed_roundtrip():
    # 50 kph, 1234.5 m = 12345 decimeters
    buf = _pack_v4_graph(
        nodes_e7=[(0, 0), (1000, 1000)],
        edges=[(0, 1, 12345, 50, 0xFFFFFFFF, 0)],
    )
    g = parse_szrg_bytes(buf)
    assert g.edge_target(0) == 1
    assert g.edge_speed(0) == 50
    assert abs(g.edge_dist_m(0) - 1234.5) < 1e-9


def test_edge_class_access_v4():
    """v4 adds a 5th u32 per edge. Verify it's read correctly."""
    nodes = [(0, 0), (1000, 0)]
    edges_in = [(0, 1, 100, 30, 0xFFFFFFFF, 0)]
    buf = bytearray(_pack_v4_graph(nodes, edges_in))
    # overwrite the class_access u32 in the single edge: offset = header(32) + nodes(2*2*4=16) + adj(3*4=12) + edge_stride*4 for first 4 cols
    class_access = 0x100 | 0x80 | 2  # roundabout + oneway + motorway_link ordinal
    edge_base = 32 + 16 + 12 + 4 * 4  # skip 4 u32s for target/dist_speed/geom_idx/name_idx
    struct.pack_into("<I", buf, edge_base, class_access)
    g = parse_szrg_bytes(bytes(buf))
    assert g.edge_class_access(0) == class_access


def test_find_route_simple_chain():
    # 3 nodes in a line, 0 -> 1 -> 2, both 1 km / 30 kph
    buf = _pack_v4_graph(
        nodes_e7=[(0, 0), (100000, 0), (200000, 0)],
        edges=[
            (0, 1, 10000, 30, 0xFFFFFFFF, 0),
            (1, 2, 10000, 30, 0xFFFFFFFF, 0),
        ],
    )
    g = parse_szrg_bytes(buf)
    r = find_route(g, 0, 2)
    assert r is not None
    assert r.node_sequence == [0, 1, 2]
    assert abs(r.total_dist_m - 2000.0) < 1e-6


def test_find_route_prefers_fast_path():
    """A* heuristic = time, so it should prefer the faster of two routes
    even when slower route has a shorter hop count."""
    # 0 -> 1 -> 3:  short slow (10km @ 30 km/h = 1200s)
    # 0 -> 2 -> 3:  longer fast (15km @ 100 km/h = 540s)
    nodes = [(0, 0), (100000, 1000), (100000, -1000), (200000, 0)]
    edges_in = [
        (0, 1, 100000, 30, 0xFFFFFFFF, 0),
        (1, 3, 100000, 30, 0xFFFFFFFF, 0),
        (0, 2, 150000, 100, 0xFFFFFFFF, 0),
        (2, 3, 150000, 100, 0xFFFFFFFF, 0),
    ]
    buf = _pack_v4_graph(nodes, edges_in)
    g = parse_szrg_bytes(buf)
    r = find_route(g, 0, 3)
    assert r is not None
    assert r.node_sequence == [0, 2, 3]


def test_find_route_unreachable_returns_none():
    buf = _pack_v4_graph(
        nodes_e7=[(0, 0), (100000, 0)],
        edges=[],
    )
    g = parse_szrg_bytes(buf)
    assert find_route(g, 0, 1) is None


def test_haversine_sanity():
    # ~1° latitude ≈ 111 km
    d = haversine_m(0, 0, 1, 0)
    assert 110_000 < d < 112_000


def test_fingerprint_is_json_safe():
    """Fingerprint must round-trip through json — required by diff_corpora."""
    import json
    buf = _pack_v4_graph(
        nodes_e7=[(0, 0), (100000, 0)],
        edges=[(0, 1, 1000, 30, 0xFFFFFFFF, 0)],
    )
    g = parse_szrg_bytes(buf)
    r = find_route(g, 0, 1)
    assert r is not None
    fp = r.fingerprint()
    s = json.dumps(fp, separators=(",", ":"))
    back = json.loads(s)
    assert back == json.loads(s)


def test_fingerprint_geom_sequence_preserved():
    """Geom indices for every edge must surface in the fingerprint so a
    corpus diff notices if the geom_idx column shifted. -1 sentinel for
    no-geom edges."""
    buf = _pack_v4_graph(
        nodes_e7=[(0, 0), (100000, 0), (200000, 0)],
        edges=[
            (0, 1, 1000, 30, 0xFFFFFFFF, 0),  # no geom
            (1, 2, 1000, 30, 42, 0),          # geom idx 42
        ],
    )
    # We packed num_geoms=0 in _pack_v4_graph, but the A* geom sequence
    # carries whatever index is on the edge — it's the writer's job to
    # keep these synced. Confirm the fingerprint faithfully reports them.
    g = parse_szrg_bytes(buf)
    r = find_route(g, 0, 2)
    assert r is not None
    assert r.geom_sequence == [-1, 42]
    assert r.fingerprint()["g"] == [-1, 42]


def test_fingerprint_road_coalesces_same_name():
    """Two consecutive edges with same name_idx should coalesce into one
    road entry, with distances summed."""
    buf = _pack_v4_graph(
        nodes_e7=[(0, 0), (100000, 0), (200000, 0)],
        edges=[
            (0, 1, 10000, 30, 0xFFFFFFFF, 5),  # 1km on name 5
            (1, 2, 20000, 30, 0xFFFFFFFF, 5),  # 2km on name 5
        ],
        names=["", "", "", "", "", "Main St"],
    )
    g = parse_szrg_bytes(buf)
    r = find_route(g, 0, 2)
    assert r is not None
    assert len(r.road_sequence) == 1
    name_idx, flags, dist_m = r.road_sequence[0]
    assert name_idx == 5
    assert flags == 0
    assert abs(dist_m - 3000.0) < 1e-6


def test_fingerprint_road_splits_on_name_change():
    """Different name_idx → separate road entries."""
    buf = _pack_v4_graph(
        nodes_e7=[(0, 0), (100000, 0), (200000, 0)],
        edges=[
            (0, 1, 10000, 30, 0xFFFFFFFF, 5),
            (1, 2, 20000, 30, 0xFFFFFFFF, 6),
        ],
        names=["", "", "", "", "", "Main St", "Oak Ave"],
    )
    g = parse_szrg_bytes(buf)
    r = find_route(g, 0, 2)
    assert r is not None
    assert [r[0] for r in r.road_sequence] == [5, 6]
