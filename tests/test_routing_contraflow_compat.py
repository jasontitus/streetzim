"""Car routing must not change when a graph carries walk/bike records for
travel against a one-way: a reverse edge with speed byte 0 and the
no-motor bit 9 (plus bit 10). Every car A* already skips them; these
cover the three places that looked at ANY out-edge before (adversarial
review of the walk/bike design): the destination snap (a one-way's end
must stay a sink), the two-pass highway seed (a node whose only highway
edge is such a record cannot start leg B), and route_cli's helpers."""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from tests.szrg_reader import parse_szrg_bytes
from tests.szrg_spatial import build_spatial
from tests.test_routing_worker_v3 import (
    _pack_v4_graph_cls,
    _run_worker,
    _spatial_graph_from_dir,
)

RES, PRIMARY, TRUNK = 11, 5, 3
NO_MOTOR, CAR_DIR_DENY = 0x200, 0x400
NONE = 0xFFFFFFFF


def _contra(src, dst, dist_dm, ordinal):
    """The walk/bike record for src -> dst against a one-way."""
    return (src, dst, dist_dm, 0, NONE, 0, ordinal | NO_MOTOR | CAR_DIR_DENY)


def _line(n, lat=400_000_000, lon0=-1_050_000_000, step=6_000):
    return [(lat, lon0 + i * step) for i in range(n)]


def _build(tmp_path, nodes, edges, name="routing-data"):
    out = tmp_path / name
    build_spatial(parse_szrg_bytes(_pack_v4_graph_cls(nodes, edges)),
                  cell_scale=10, output_dir=out)
    return out


def _node_js():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")


@pytest.mark.parametrize("contraflow", [False, True])
def test_a_one_way_end_stays_the_car_destination(tmp_path, contraflow):
    """A one-way street 0 -> 1 -> ... -> 5 ends in a sink (node 5). The
    destination snap keeps sinks; walk/bike records 5 -> 4 -> ... must not
    make node 5 look like a footpath vertex and move the destination."""
    _node_js()
    nodes = _line(6)
    edges = [(i, i + 1, 500, 30, NONE, 0, RES) for i in range(5)]
    if contraflow:
        edges = [_contra(i + 1, i, 500, RES) for i in range(5)] + edges
    rd = _build(tmp_path, nodes, edges)
    out = _run_worker(tmp_path, [[nodes[0][0] / 1e7, nodes[0][1] / 1e7,
                                  nodes[5][0] / 1e7 + 0.00005, nodes[5][1] / 1e7]],
                      [{"modeA": "origin", "modeB": "dest"}])[0]
    assert out["end"] == 5 and out["ok"] and out["time"] is not None
    assert abs(out["time"] - 5 * 50.0 / (30 / 3.6)) < 1e-3
    sg = _spatial_graph_from_dir(rd)
    assert sg.nearest_node(nodes[5][0] + 500, nodes[5][1], mode="dest") == 5


def test_a_car_never_drives_a_walk_record(tmp_path):
    """Against the one-way there is only the walk/bike record: no car route."""
    _node_js()
    nodes = _line(6)
    edges = ([_contra(i + 1, i, 500, RES) for i in range(5)]
             + [(i, i + 1, 500, 30, NONE, 0, RES) for i in range(5)])
    _build(tmp_path, nodes, edges)
    out = _run_worker(tmp_path, [[nodes[5][0] / 1e7, nodes[5][1] / 1e7,
                                  nodes[0][0] / 1e7, nodes[0][1] / 1e7]],
                      [{"modeA": "origin", "modeB": "origin"}])[0]
    assert out["ok"] and out["time"] is None


@pytest.mark.parametrize("contraflow", [False, True])
def test_the_two_pass_seed_skips_a_highway_node_a_car_cannot_leave(tmp_path, contraflow):
    """S -res- X -res- H -primary- P -primary- E. X also ends a one-way trunk
    link Z -> X; its walk record X -> Z is a 'highway' edge a car cannot
    take. The two-pass seed must pass X and take H, or leg B dies at X."""
    _node_js()
    nodes = _line(5) + [(400_010_000, -1_050_000_000 + 6_000)]   # Z, north of X
    S, X, H, P, E, Z = range(6)
    two = lambda a, b, sp, o: [(a, b, 500, sp, NONE, 0, o), (b, a, 500, sp, NONE, 0, o)]
    edges = two(S, X, 30, RES) + two(X, H, 30, RES) + two(H, P, 60, PRIMARY) + two(P, E, 60, PRIMARY)
    edges.append((Z, X, 1100, 80, NONE, 0, TRUNK))
    if contraflow:
        edges.insert(0, _contra(X, Z, 1100, TRUNK))
    _build(tmp_path, nodes, edges)
    out = _run_worker(tmp_path, [[nodes[S][0] / 1e7, nodes[S][1] / 1e7,
                                  nodes[E][0] / 1e7, nodes[E][1] / 1e7]],
                      [{"options": {"route": "two-pass"}, "modeA": "origin", "modeB": "dest"}])[0]
    assert out["ok"] and out["time"] is not None, out


class _G:
    """Just what route_cli's helpers read: nodes_scaled + edges_of_node."""

    def __init__(self, nodes, edges):
        self.nodes_scaled = np.array([v for n in nodes for v in n], dtype=np.int32)
        self._out = {}
        for (s, t, dist, speed, gi, ni, ca) in edges:
            self._out.setdefault(s, []).append((t, (speed << 24) | dist, gi, ni, ca))

    def edges_of_node(self, n):
        return self._out.get(n, [])


def test_route_cli_measures_the_car_edge_not_a_parallel_walk_record():
    from cloud.route_cli import measure_path
    g = _G(_line(2), [_contra(0, 1, 9_000, RES), (0, 1, 500, 30, NONE, 0, RES)])
    dist, time = measure_path(g, [0, 1])
    assert dist == 50.0 and abs(time - 50.0 / (30 / 3.6)) < 1e-9


def test_route_cli_highway_seed_needs_a_drivable_highway_edge():
    from cloud.route_cli import nearest_node_filtered
    nodes = _line(3)
    g = _G(nodes, [_contra(0, 1, 500, TRUNK), (2, 1, 500, 60, NONE, 0, PRIMARY)])
    node, _ = nearest_node_filtered(g, nodes[0][0] / 1e7, nodes[0][1] / 1e7, highway_only=True)
    assert node == 2
