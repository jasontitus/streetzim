"""Close to cars the road pieces a clip cuts off past the border (--clip-poly).

A clipped build keeps the roads within ``--clip-buffer-km`` of the region's
border (streetzim.clip), and the cut at the margin's edge leaves pieces of
road past the border that the region's network cannot reach: the far end
of a road that only leads back in, a stretch of the neighbour's network
whose link to a border crossing is gone. The routers snap a destination to
the nearest car road whose forward search reaches SNAP_MIN_REACH nodes; a
node with a way out and no way in passes that test, and the search then
runs through the whole graph before it gives up (Ahvaz to Basra in the
clipped Iran build: "No route found" after minutes in the viewer).

A node loses car access on its outgoing edges (class_access bit 9, which
walking and cycling ignore) when all of these hold:
  - it lies outside the border: nothing inside changes, so no route
    between two points inside does either (an exclave's roads included);
  - the main network (the largest strongly connected component of the
    drive graph) cannot reach it: a road you can drive to stays a
    destination, a one-way leaving the country included;
  - its own strongly connected component does not lie mostly (more than
    half) inside the border: an island's or an exclave's crossing into the
    margin stays open. (The main network, the largest component, is
    reached, so it stays open however small the region.)
The car snap then passes those nodes over. Nodes, edges and geometry stay.

One case is left to the viewer: a node past the border with no car edge
of its own (the end of a one-way) whose only way in was closed still
counts as a snap candidate, because the snappers treat a node with no car
edge as a one-way's end, and with nothing better within reach the snap
falls back to it. The viewer's route worker then finds it sealed
(destComponentClosed) and snaps again outside it (resnapPast).
cloud/route_cli.py has no such re-snap and reports no route there.
"""
from __future__ import annotations

import struct

from streetzim.routing.modes import NO_MOTOR_BIT, NO_MOTOR_ORD_MAX, NO_MOTOR_ORD_MIN

_HEADER = 32        # b"SZRG" + 7 u32 (docs/formats.md, SZRG v4)


def close_cut_off_roads(graph_path, border) -> tuple[int, int]:
    """Close to cars, in place, the outgoing edges of the cut-off nodes (see
    the module docstring) of the SZRG v4 graph at `graph_path`; `border` is
    a shapely geometry in lon/lat. Returns (nodes closed, edges closed)."""
    import numpy as np
    import shapely
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import breadth_first_order, connected_components

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
        del ordv
        # The drive graph as CSR straight from the file's adjacency: edges
        # are stored in from-node order, so node i's drive edges are the
        # drive ones in adj[i]:adj[i+1].
        before = np.zeros(e + 1, dtype=np.int64)
        np.cumsum(drive, out=before[1:])
        small = max(n, e) < 2 ** 31
        idx = np.int32 if small else np.int64
        indptr = before[adj].astype(idx)
        del before
        indices = np.asarray(edges[:, 0])[drive].astype(idx)
        graph = csr_matrix((np.ones(indices.size, dtype=bool), indices, indptr),
                           shape=(n, n))
        del indices
        _, label = connected_components(graph, directed=True, connection="strong")
        size = np.bincount(label)
        main = int(np.argmax(size))
        kept = np.bincount(label, weights=inside) * 2 > size    # mostly inside
        reached = np.zeros(n, dtype=bool)
        reached[breadth_first_order(graph, int(np.flatnonzero(label == main)[0]),
                                    directed=True, return_predecessors=False)] = True
        del graph
        cut = ~inside & ~reached & ~kept[label]
        del label, reached
        close = drive & np.repeat(cut, np.diff(adj))
        if close.any():
            edges[close, 4] = ca[close] | NO_MOTOR_BIT
            edges.flush()
        return int(np.count_nonzero(cut & (np.diff(indptr) > 0))), int(close.sum())
    finally:
        del edges
