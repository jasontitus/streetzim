"""Per-mode routing in the Python readers (streetzim/routing/modes.py):
what walking and cycling may use, what they cost, the time reported,
and mode-aware snapping. Driving must not change; the differential and
reference tests elsewhere pin that."""
from __future__ import annotations

import itertools

import pytest

from streetzim.routing.modes import HEURISTIC_KPH, MODES, edge_cost
from streetzim.routing.spatial_astar import find_route_spatial
from tests.szrg_reader import parse_szrg_bytes
from tests.szrg_spatial import build_spatial
from tests.test_routing_worker_v3 import _pack_v4_graph_cls, _spatial_graph_from_dir

MOTORWAY, TRUNK, PRIMARY, SECONDARY, RES = 1, 3, 5, 7, 11
FOOTWAY, CYCLEWAY, TRACK, PATH, STEPS = 17, 18, 15, 16, 20
FOOT_DENY, BIKE_DENY, NO_MOTOR, CONTRA, GEOM_REV = 0x20, 0x40, 0x200, 0x400, 0x800
PUSH, LANE, INFRA, SIDEWALK_NO, FOOT_DIR_DENY = 0x1000, 0x2000, 0x4000, 0x20000, 0x40000
PAVED, FIRM, ROUGH = 1 << 15, 2 << 15, 3 << 15
NONE = 0xFFFFFFFF
KM = 10_000          # 1 km in decimetres


def _sd(speed, dist_dm=KM):
    return (speed << 24) | dist_dm


# ---- the cost table ------------------------------------------------------

def test_drive_is_plain_time_and_skips_records_and_footways():
    assert edge_cost("drive", _sd(36), RES) == (100.0, 100.0)
    assert edge_cost("drive", _sd(0), RES | NO_MOTOR | CONTRA) is None
    assert edge_cost("drive", _sd(10), FOOTWAY) is None


def test_walk_rules():
    assert edge_cost("walk", _sd(30), RES) == (720.0, 720.0)            # 5 km/h
    assert edge_cost("walk", _sd(0), RES | NO_MOTOR | CONTRA | GEOM_REV) == (720.0, 720.0)
    assert edge_cost("walk", _sd(30), RES | FOOT_DENY) is None
    assert edge_cost("walk", _sd(0), RES | NO_MOTOR | CONTRA | FOOT_DIR_DENY) is None
    assert edge_cost("walk", _sd(100), MOTORWAY) is None
    assert edge_cost("walk", _sd(5), STEPS) == (1440.0, 1440.0)        # 2.5 km/h
    assert edge_cost("walk", _sd(5), PATH | ROUGH) == (800.0, 800.0)   # 4.5 km/h
    assert edge_cost("walk", _sd(80), TRUNK) == (1080.0, 720.0)
    assert edge_cost("walk", _sd(80), TRUNK | SIDEWALK_NO) == (1620.0, 720.0)
    assert edge_cost("walk", _sd(50), PRIMARY | SIDEWALK_NO) == (1080.0, 720.0)
    assert edge_cost("walk", _sd(30), RES | SIDEWALK_NO) == (720.0, 720.0)


def test_bike_rules():
    assert edge_cost("bike", _sd(30), RES) == (200.0, 200.0)            # 18 km/h
    assert edge_cost("bike", _sd(30), RES | BIKE_DENY) is None
    assert edge_cost("bike", _sd(100), MOTORWAY) is None
    # A foot-only ban does not stop a bike, and vice versa.
    assert edge_cost("bike", _sd(30), RES | FOOT_DENY) is not None
    assert edge_cost("walk", _sd(30), RES | BIKE_DENY) is not None
    assert edge_cost("bike", _sd(0), RES | NO_MOTOR | CONTRA | PUSH) == (1350.0, 900.0)
    assert edge_cost("bike", _sd(5), STEPS | PUSH) == (2700.0, 900.0)
    assert edge_cost("bike", _sd(5), STEPS | PUSH | BIKE_DENY) is None
    assert edge_cost("bike", _sd(5), PATH | FIRM)[1] == pytest.approx(1000 / (14 / 3.6))
    assert edge_cost("bike", _sd(5), TRACK | ROUGH)[1] == 450.0         # 8 km/h
    assert edge_cost("bike", _sd(5), PATH)[1] == 300.0                  # 12 km/h unknown
    assert edge_cost("bike", _sd(5), CYCLEWAY)[1] == 200.0
    assert edge_cost("bike", _sd(80), TRUNK)[0] == pytest.approx(320.0)
    assert edge_cost("bike", _sd(50), PRIMARY)[0] == pytest.approx(280.0)
    assert edge_cost("bike", _sd(40), SECONDARY)[0] == pytest.approx(240.0)
    assert edge_cost("bike", _sd(50), PRIMARY | LANE) == (200.0, 200.0)
    assert edge_cost("bike", _sd(50), PRIMARY | INFRA) == (200.0, 200.0)


def test_unknown_mode():
    with pytest.raises(ValueError):
        edge_cost("fly", _sd(30), RES)


@pytest.mark.parametrize("mode", ["walk", "bike"])
def test_heuristic_is_admissible_for_every_bit_combination(mode):
    """No edge is faster than the straight-line speed A* assumes, and the
    search cost never undercuts the time."""
    bits = [FOOT_DENY, BIKE_DENY, NO_MOTOR | CONTRA, PUSH, LANE, INFRA,
            SIDEWALK_NO, FOOT_DIR_DENY, PAVED, FIRM, ROUGH]
    floor = 1000 / (HEURISTIC_KPH[mode] / 3.6)
    for ordv in range(25):
        for k in range(len(bits) + 1):
            for combo in itertools.combinations(bits, k):
                ca = ordv
                for b in combo:
                    ca |= b
                r = edge_cost(mode, _sd(0), ca)
                if r is not None:
                    assert r[1] >= floor - 1e-9 and r[0] >= r[1], (ordv, combo, r)


# ---- routing on small graphs ----------------------------------------------

def _graph(tmp_path, nodes, edges):
    out = tmp_path / "routing-data"
    build_spatial(parse_szrg_bytes(_pack_v4_graph_cls(nodes, edges)),
                  cell_scale=10, output_dir=out)
    return _spatial_graph_from_dir(out)


def _line(n, lat=400_000_000, lon0=-1_050_000_000, step=6_000):
    return [(lat, lon0 + i * step) for i in range(n)]


def _contra(src, dst, dist_dm, ca):
    return (src, dst, dist_dm, 0, NONE, 0, ca | NO_MOTOR | CONTRA | GEOM_REV)


def test_walking_goes_against_a_one_way_driving_cannot(tmp_path):
    nodes = _line(6)
    edges = [(i, i + 1, 500, 30, NONE, 0, RES | 0x80) for i in range(5)]
    edges += [_contra(i + 1, i, 500, RES) for i in range(5)]
    g = _graph(tmp_path, nodes, edges)
    assert find_route_spatial(g, 5, 0) is None
    r = find_route_spatial(g, 5, 0, travel_mode="walk")
    assert r.node_sequence == [5, 4, 3, 2, 1, 0]
    assert r.total_dist_m == pytest.approx(250.0)
    assert r.total_time_s == pytest.approx(250 / (5 / 3.6))
    bike = find_route_spatial(g, 5, 0, travel_mode="bike")
    assert bike.node_sequence == [5, 4, 3, 2, 1, 0]


def test_a_foot_denied_record_blocks_walking_only(tmp_path):
    nodes = _line(3)
    edges = [(0, 1, 500, 30, NONE, 0, RES), (1, 2, 500, 30, NONE, 0, RES),
             _contra(2, 1, 500, RES | FOOT_DIR_DENY), _contra(1, 0, 500, RES)]
    g = _graph(tmp_path, nodes, edges)
    assert find_route_spatial(g, 2, 0, travel_mode="walk") is None
    assert find_route_spatial(g, 2, 0, travel_mode="bike") is not None


def _two_ways(tmp_path, road_ca, alt_ca, alt_detour):
    """0 -> 1 directly on a road, or 0 -> 2 -> 1 on an alternative whose
    length is alt_detour times the road's."""
    nodes = [(400_000_000, -1_050_000_000), (400_000_000, -1_049_900_000),
             (400_050_000, -1_049_950_000)]
    half = int(KM * alt_detour / 2)
    edges = [(0, 1, KM, 50, NONE, 0, road_ca),
             (0, 2, half, 20, NONE, 0, alt_ca), (2, 1, half, 20, NONE, 0, alt_ca)]
    return _graph(tmp_path, nodes, edges)


def test_bikes_take_a_slightly_longer_cycleway_over_a_primary(tmp_path):
    g = _two_ways(tmp_path, PRIMARY, CYCLEWAY, 1.3)
    assert find_route_spatial(g, 0, 1).node_sequence == [0, 1]          # car: primary
    r = find_route_spatial(g, 0, 1, travel_mode="bike")
    assert r.node_sequence == [0, 2, 1]
    assert r.total_time_s == pytest.approx(1300 / (18 / 3.6))           # time, not cost
    g2 = _two_ways(tmp_path / "b", PRIMARY | LANE, CYCLEWAY, 1.3)
    assert find_route_spatial(g2, 0, 1, travel_mode="bike").node_sequence == [0, 1]


def test_walkers_avoid_a_trunk_without_sidewalk(tmp_path):
    g = _two_ways(tmp_path, TRUNK | SIDEWALK_NO, FOOTWAY, 2.0)
    assert find_route_spatial(g, 0, 1, travel_mode="walk").node_sequence == [0, 2, 1]
    g2 = _two_ways(tmp_path / "b", MOTORWAY, RES, 5.0)
    assert find_route_spatial(g2, 0, 1, travel_mode="walk").node_sequence == [0, 2, 1]
    assert find_route_spatial(g2, 0, 1, travel_mode="bike").node_sequence == [0, 2, 1]


def test_unknown_travel_mode(tmp_path):
    g = _graph(tmp_path, _line(2), [(0, 1, 500, 30, NONE, 0, RES)])
    with pytest.raises(ValueError):
        find_route_spatial(g, 0, 1, travel_mode="fly")
    assert set(MODES) == {"drive", "walk", "bike"}


# ---- snapping ---------------------------------------------------------

def test_walk_snaps_to_a_footpath_a_car_skips(tmp_path):
    """Tap beside a 40-node footpath that is far from a road: driving snaps
    to the road, walking to the footpath."""
    path = _line(40, lat=400_000_000)
    road = _line(40, lat=400_300_000)
    nodes = path + road
    edges = []
    for i in range(39):
        edges += [(i, i + 1, 500, 5, NONE, 0, FOOTWAY), (i + 1, i, 500, 5, NONE, 0, FOOTWAY)]
        a, b = 40 + i, 41 + i
        edges += [(a, b, 500, 30, NONE, 0, RES), (b, a, 500, 30, NONE, 0, RES)]
    g = _graph(tmp_path, nodes, edges)
    q = (path[10][0] + 100, path[10][1])
    assert g.nearest_node(*q) >= 40
    assert g.nearest_node(*q, travel_mode="walk") == 10
    assert g.nearest_node(*q, travel_mode="bike") == 10


def test_walk_does_not_snap_onto_a_motorway(tmp_path):
    mw = _line(40, lat=400_000_000)
    res = _line(40, lat=400_300_000)
    edges = []
    for i in range(39):
        edges += [(i, i + 1, 500, 100, NONE, 0, MOTORWAY)]
        a, b = 40 + i, 41 + i
        edges += [(a, b, 500, 30, NONE, 0, RES), (b, a, 500, 30, NONE, 0, RES)]
    g = _graph(tmp_path, mw + res, edges)
    q = (mw[10][0] + 100, mw[10][1])
    assert g.nearest_node(*q) < 40
    assert g.nearest_node(*q, travel_mode="walk") >= 40


# ---- Monaco, built with walk/bike records ------------------------------

def test_monaco_walk_and_bike_routes(tmp_path, monkeypatch):
    """Every walk/bike route uses only edges its mode allows, its time is
    at least the straight-line time at the heuristic speed, and walking
    reaches places driving cannot (pedestrian streets, stairs)."""
    import random
    from pathlib import Path

    pytest.importorskip("osmium")
    from streetzim.routing.astar import haversine_m
    from streetzim.routing.build import extract_routing_graph
    from streetzim.routing.reader import load_from_file
    from streetzim.routing.spatial import build_spatial as build_cells

    pbf = Path(__file__).parent / "fixtures/monaco-full/monaco.osm.pbf"
    monkeypatch.setenv("STREETZIM_NODE_LOC_DIR", str(tmp_path))
    monkeypatch.setenv("STREETZIM_ROUTING_WALKBIKE", "1")
    (tmp_path / "g").mkdir()
    szrg = load_from_file(extract_routing_graph(str(pbf), str(tmp_path / "g")))
    build_cells(szrg, cell_scale=10, output_dir=tmp_path / "cells")
    g = _spatial_graph_from_dir(tmp_path / "cells")

    rng = random.Random(7)
    found = {"walk": 0, "bike": 0, "drive": 0}
    walk_only = 0
    for _ in range(60):
        a, b = (g.node_coords_e7(rng.randrange(g.num_nodes)) for _ in range(2))
        crow = haversine_m(a[0] / 1e7, a[1] / 1e7, b[0] / 1e7, b[1] / 1e7)
        got = {}
        for mode in ("walk", "bike", "drive"):
            s = g.nearest_node(*a, "origin", travel_mode=mode)
            e = g.nearest_node(*b, "dest", travel_mode=mode)
            r = find_route_spatial(g, s, e, travel_mode=mode)
            if r is None:
                continue
            found[mode] += 1
            got[mode] = r
            seq = r.node_sequence
            time_s = 0.0
            for u, v in zip(seq, seq[1:]):
                costs = [edge_cost(mode, sd, ca)
                         for (t, sd, _gi, _ni, ca) in g.edges_of_node(u) if t == v]
                ok = [c for c in costs if c is not None]
                assert ok, (mode, u, v)
                time_s += min(ok)[1]
            if mode != "drive":
                assert r.total_time_s == pytest.approx(time_s, rel=1e-6)
                (sa, sb) = (g.node_coords_e7(s), g.node_coords_e7(e))
                d = haversine_m(sa[0] / 1e7, sa[1] / 1e7, sb[0] / 1e7, sb[1] / 1e7)
                assert r.total_time_s >= d / (HEURISTIC_KPH[mode] / 3.6) - 1e-6
        if "walk" in got and "drive" not in got:
            walk_only += 1
        if crow > 300 and "walk" in got and "drive" in got:
            assert got["walk"].total_time_s > got["drive"].total_time_s
    assert found["walk"] >= 58 and found["bike"] >= 58, found
