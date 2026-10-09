"""Close the road pieces a clip cuts off to cars (--clip-poly).

A clipped build keeps the roads within ``--clip-buffer-km`` of the region's
border (streetzim.clip), and the cut at the margin's edge leaves pieces of
road the region's network cannot reach: the far end of a road that only
leads back in, a stretch of the neighbour's network whose link to a border
crossing is gone. The routers snap a destination to the nearest car road
whose forward search reaches SNAP_MIN_REACH nodes; a node with a way out
and no way in passes that test, and the search then runs through the whole
graph before it gives up (Ahvaz to Basra in the clipped Iran build: "No
route found" after ten minutes in the viewer).

The unit is a strongly connected component of the drive graph: a network
in which every node can reach every other. A component keeps car access
when it is the main network (the largest) or when most of its nodes lie
inside the border: islands, exclaves, the one-way stubs and parking loops
inside the region. Every other component (what the cut left of the
neighbour's roads, including the bits of them that the border outline
takes in) loses car access on its outgoing edges: class_access bit 9,
which walking and cycling ignore. The car snap then passes those nodes
over. Nodes, edges and geometry stay. A route between two nodes of a kept
component uses only nodes of that component, so every such route (the main
network's, an island's) is exactly what it was.

In the clipped Iran build, the components touching the inside were Iran's
islands (Qeshm, Kish, Kharg: all inside) and pieces of Iraq, Azerbaijan and
Turkey with 4-23% of their nodes inside Geofabrik's outline.
"""
from __future__ import annotations

import struct

from streetzim.routing.modes import NO_MOTOR_BIT, NO_MOTOR_ORD_MAX, NO_MOTOR_ORD_MIN

_HEADER = 32        # b"SZRG" + 7 u32 (docs/formats.md, SZRG v4)


def close_cut_off_roads(graph_path, border) -> tuple[int, int]:
    """Close to cars, in place, the outgoing edges of every node of the SZRG
    v4 graph at `graph_path` whose drive component is neither the largest
    nor mostly inside `border` (a shapely geometry, lon/lat). Returns
    (nodes closed, edges closed)."""
    import numpy as np
    import shapely
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    with open(graph_path, "rb") as f:
        head = f.read(_HEADER)
    if len(head) < _HEADER or head[:4] != b"SZRG":
        raise ValueError(f"{graph_path}: not an SZRG routing graph")
    version, n, e = struct.unpack_from("<3I", head, 4)
    if version != 4:
        raise ValueError(f"{graph_path}: SZRG v{version}; only v4 is written here")
    if not n or not e:
        return 0, 0
    nodes = np.fromfile(graph_path, dtype="<i4", count=2 * n, offset=_HEADER)
    adj = np.fromfile(graph_path, dtype="<u4", count=n + 1,
                      offset=_HEADER + 8 * n).astype(np.int64)
    shapely.prepare(border)
    inside = shapely.contains_xy(border, nodes[1::2] / 1e7, nodes[0::2] / 1e7)
    del nodes
    if inside.all():
        return 0, 0
    edges = np.memmap(graph_path, dtype="<u4", mode="r+",
                      offset=_HEADER + 8 * n + 4 * (n + 1), shape=(e, 5))
    try:
        ca = np.asarray(edges[:, 4])
        ordv = ca & 0x1F
        # The routers' car rule (modes.edge_cost): speed > 0 and not no-motor.
        drive = (((np.asarray(edges[:, 1]) >> 24) != 0)
                 & ((ca & NO_MOTOR_BIT) == 0)
                 & ~((ordv >= NO_MOTOR_ORD_MIN) & (ordv <= NO_MOTOR_ORD_MAX)))
        src = np.repeat(np.arange(n, dtype=np.int64), np.diff(adj))
        dst = np.asarray(edges[:, 0]).astype(np.int64)
        graph = csr_matrix((np.ones(int(drive.sum()), dtype=bool),
                            (src[drive], dst[drive])), shape=(n, n))
        _, label = connected_components(graph, directed=True, connection="strong")
        del graph, dst
        size = np.bincount(label)
        kept = np.bincount(label, weights=inside) * 2 > size    # mostly inside
        kept[np.argmax(size)] = True        # the main network, however small the region
        close = drive & ~kept[label][src]
        if close.any():
            edges[close, 4] = ca[close] | NO_MOTOR_BIT
            edges.flush()
        return int(np.unique(src[close]).size), int(close.sum())
    finally:
        del edges
