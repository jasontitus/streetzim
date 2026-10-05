"""Walk/bike additions to the routing graph (STREETZIM_ROUTING_WALKBIKE,
default on): resolved foot/bike access (bits 5, 6), push / lane / cycle
infrastructure / surface / sidewalk bits (12-17), walking forbidden in an
edge's direction (bit 18), and records for travel against a one-way
(speed 0, bits 9 + 10, the stored geometry reversed: bit 11).

The car graph must not change: with those records removed and the new
bits masked, the graph is the one STREETZIM_ROUTING_WALKBIKE=0 builds."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

osmium = pytest.importorskip("osmium")

from streetzim.routing.build import extract_routing_graph  # noqa: E402
from streetzim.routing.reader import load_from_file  # noqa: E402

MONACO = Path(__file__).parent / "fixtures/monaco-full/monaco.osm.pbf"
FOOT_DENY, BIKE_DENY, ONEWAY, ROUNDABOUT, NO_MOTOR = 0x20, 0x40, 0x80, 0x100, 0x200
CONTRA, GEOM_REV, PUSH, LANE, INFRA = 0x400, 0x800, 0x1000, 0x2000, 0x4000
SIDEWALK_NO, FOOT_DIR_DENY = 0x20000, 0x40000
SEPARATE, PRIVATE = 0x80000, 0x100000


def _build(tmp_path, pbf, name, walkbike, monkeypatch):
    out = tmp_path / name
    out.mkdir()
    monkeypatch.setenv("STREETZIM_NODE_LOC_DIR", str(tmp_path))
    monkeypatch.setenv("STREETZIM_ROUTING_WALKBIKE", "1" if walkbike else "0")
    return load_from_file(extract_routing_graph(str(pbf), str(out)))


def _out_edges(g):
    e = g.edges.reshape(-1, g.edge_stride)
    adj = g.adj_offsets
    return [[tuple(int(x) for x in e[i]) for i in range(adj[n], adj[n + 1])]
            for n in range(g.num_nodes)]


def _car_view(edges):
    """Drop walk/bike records; keep the bits a car router reads (class
    ordinal, one-way, roundabout, no-motor); ordinals added for walk/bike
    (21-24, all no-motor) read as the 0 they were."""
    out = []
    for (t, sd, gi, ni, ca) in edges:
        if ca & CONTRA:
            continue
        ordv = ca & 0x1F
        out.append((t, sd, gi, ni, (0 if ordv >= 21 else ordv) | (ca & 0x380)))
    return out


def test_the_car_graph_is_unchanged_on_monaco(tmp_path, monkeypatch):
    old = _build(tmp_path, MONACO, "old", False, monkeypatch)
    new = _build(tmp_path, MONACO, "new", True, monkeypatch)
    assert np.array_equal(old.nodes_scaled, new.nodes_scaled)
    assert bytes(old.geom_blob) == bytes(new.geom_blob)
    assert np.array_equal(old.geom_offsets, new.geom_offsets)
    assert old.names_blob == new.names_blob
    assert [_car_view(n) for n in _out_edges(old)] == [_car_view(n) for n in _out_edges(new)]
    contra = [e for n in _out_edges(new) for e in n if e[4] & CONTRA]
    assert len(contra) > 100                                  # Monaco has many one-ways
    assert all(e[1] >> 24 == 0 and e[4] & NO_MOTOR and e[4] & GEOM_REV
               and not e[4] & (ONEWAY | ROUNDABOUT) for e in contra)


# ---- one tagged two-node way per case, each well apart -------------------

CASES = {
    "residential_oneway": {"highway": "residential", "oneway": "yes"},
    "residential_reverse": {"highway": "residential", "oneway": "-1"},
    "footway_oneway": {"highway": "footway", "oneway": "yes"},
    "footway_oneway_foot_no": {"highway": "footway", "oneway": "yes", "oneway:foot": "no"},
    "motorway": {"highway": "motorway"},
    "bike_contraflow": {"highway": "residential", "oneway": "yes", "oneway:bicycle": "no"},
    "foot_no_bike_contra": {"highway": "residential", "oneway": "yes", "foot": "no",
                            "cycleway": "opposite_lane"},
    "steps": {"highway": "steps"},
    "steps_bike_no": {"highway": "steps", "bicycle": "no"},
    "steps_ramp": {"highway": "steps", "ramp:bicycle": "yes"},
    "footway": {"highway": "footway"},
    "footway_bike_yes": {"highway": "footway", "bicycle": "yes"},
    "motorroad": {"highway": "trunk", "motorroad": "yes"},
    "oneway_foot_yes": {"highway": "residential", "oneway:foot": "yes"},
    "lane_paved_nosidewalk": {"highway": "primary", "cycleway:right": "lane",
                              "surface": "asphalt", "sidewalk": "no"},
    "cycleway_gravel": {"highway": "cycleway", "surface": "gravel"},
    # Access.
    "private": {"highway": "residential", "access": "private"},
    "path_vehicle_no": {"highway": "path", "vehicle": "no"},
    "footway_access_no": {"highway": "footway", "access": "no"},
    "use_sidepath": {"highway": "primary", "bicycle": "use_sidepath"},
    "dismount": {"highway": "residential", "bicycle": "dismount"},
    "pedestrian": {"highway": "pedestrian"},
    "bridleway": {"highway": "bridleway"},
    "motorway_foot_yes": {"highway": "motorway", "oneway": "yes", "foot": "yes"},
    # One-ways that do or do not bind walkers / bikes.
    "pedestrian_oneway": {"highway": "pedestrian", "oneway": "yes"},
    "path_bike_designated_oneway": {"highway": "path", "oneway": "yes",
                                    "bicycle": "designated", "foot": "designated"},
    "mtb_oneway": {"highway": "path", "oneway": "yes", "mtb:scale": "2"},
    "side_lane_contraflow": {"highway": "residential", "oneway": "yes",
                             "cycleway:left": "lane", "cycleway:left:oneway": "-1"},
    "side_lane_with_traffic": {"highway": "residential", "oneway": "-1",
                               "cycleway:left": "lane", "cycleway:left:oneway": "-1"},
    "both_opposite_track": {"highway": "residential", "oneway": "yes",
                            "cycleway:both": "opposite_track"},
    "with_flow_lane_only": {"highway": "residential", "oneway": "yes",
                            "cycleway:right": "lane"},
    "oneway_bicycle_yes": {"highway": "residential", "oneway:bicycle": "yes"},
    "cycleway_oneway_gravel": {"highway": "cycleway", "oneway": "yes",
                               "oneway:bicycle": "no", "surface": "gravel",
                               "sidewalk": "no"},
    "roundabout": {"highway": "residential", "junction": "roundabout",
                   "name": "Place du Rond"},
    # Surface and sidewalks.
    "track_dirt": {"highway": "track", "surface": "dirt"},
    "track_grade1": {"highway": "track", "tracktype": "grade1"},
    "brick": {"highway": "residential", "surface": "brick"},
    "sidewalk_separate": {"highway": "residential", "sidewalk": "separate"},
    "sidewalk_lr_no": {"highway": "residential", "sidewalk:left": "no",
                       "sidewalk:right": "none"},
    "sidewalk_lr_separate": {"highway": "residential", "sidewalk:left": "separate",
                             "sidewalk:right": "no"},
}


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("wb")
    pbf = tmp / "cases.osm.pbf"
    coords = {}
    with osmium.SimpleWriter(str(pbf)) as wr:
        nid = 1
        for k, name in enumerate(CASES):
            a = (7.40 + k * 0.01, 43.70)
            b = (7.40 + k * 0.01, 43.701)
            wr.add_node(osmium.osm.mutable.Node(id=nid, location=a, version=1))
            wr.add_node(osmium.osm.mutable.Node(id=nid + 1, location=b, version=1))
            coords[name] = (a, b)
            nid += 2
        for k, tags in enumerate(CASES.values()):
            wr.add_way(osmium.osm.mutable.Way(id=100 + k, nodes=[2 * k + 1, 2 * k + 2],
                                              tags=tags, version=1))
    mp = pytest.MonkeyPatch()
    try:
        g = _build(tmp, pbf, "out", True, mp)
    finally:
        mp.undo()
    node_at = {(int(g.nodes_scaled[2 * n]), int(g.nodes_scaled[2 * n + 1])): n
               for n in range(g.num_nodes)}
    out = _out_edges(g)

    def edges(name):
        (alon, alat), (blon, blat) = coords[name]
        na = node_at[(round(alat * 1e7), round(alon * 1e7))]
        nb = node_at[(round(blat * 1e7), round(blon * 1e7))]
        fwd = [e for e in out[na] if e[0] == nb]
        rev = [e for e in out[nb] if e[0] == na]
        return fwd, rev
    return edges


def _both(fwd_rev):
    fwd, rev = fwd_rev
    return fwd + rev


def _contra(es):
    return [e for e in es if e[4] & CONTRA]


def _real(es):
    return [e for e in es if not e[4] & CONTRA]


def test_against_a_one_way_there_is_a_walk_record_not_a_car_edge(cases):
    fwd, rev = cases("residential_oneway")
    assert len(_real(fwd)) == 1 and _real(rev) == []
    (c,) = _contra(rev)
    assert c[1] >> 24 == 0 and c[1] & 0xFFFFFF == _real(fwd)[0][1] & 0xFFFFFF
    assert c[4] & (NO_MOTOR | CONTRA | GEOM_REV) == NO_MOTOR | CONTRA | GEOM_REV
    assert not c[4] & (ONEWAY | ROUNDABOUT | FOOT_DIR_DENY)
    assert c[4] & PUSH                     # no contraflow exemption: push the bike
    fwd, rev = cases("residential_reverse")
    assert _real(fwd) == [] and len(_real(rev)) == 1 and len(_contra(fwd)) == 1


def test_a_one_way_footway_binds_walkers(cases):
    fwd, rev = cases("footway_oneway")
    assert _contra(rev) == []
    fwd, rev = cases("footway_oneway_foot_no")
    assert len(_contra(rev)) == 1


def test_no_walk_record_on_motorways(cases):
    fwd, rev = cases("motorway")
    assert _contra(fwd) == _contra(rev) == []
    assert all(e[4] & FOOT_DENY and e[4] & BIKE_DENY for e in fwd + rev)


def test_bike_contraflow_exemptions(cases):
    (c,) = _contra(cases("bike_contraflow")[1])
    assert not c[4] & PUSH and not c[4] & FOOT_DIR_DENY
    (c,) = _contra(cases("foot_no_bike_contra")[1])
    assert not c[4] & PUSH and c[4] & FOOT_DIR_DENY and c[4] & FOOT_DENY
    assert c[4] & LANE                       # cycleway=opposite_lane
    (c,) = _contra(cases("side_lane_contraflow")[1])
    assert not c[4] & PUSH and c[4] & LANE
    (c,) = _contra(cases("both_opposite_track")[1])
    assert not c[4] & PUSH and c[4] & INFRA and not c[4] & LANE
    # cycleway:left:oneway=-1 on an oneway=-1 way runs WITH traffic.
    fwd, rev = cases("side_lane_with_traffic")
    (c,) = _contra(fwd)
    assert c[4] & PUSH
    # The with-flow lane is not on the contraflow side.
    fwd, rev = cases("with_flow_lane_only")
    assert _real(fwd)[0][4] & LANE
    (c,) = _contra(rev)
    assert c[4] & PUSH and not c[4] & LANE


def test_contraflow_keeps_the_way_bits(cases):
    """Surface, sidewalk, infrastructure and name travel with the record;
    roundabouts get one too (walkers go round either way)."""
    fwd, rev = cases("cycleway_oneway_gravel")
    (c,) = _contra(rev)
    assert c[4] & INFRA and (c[4] >> 15) & 3 == 2 and c[4] & SIDEWALK_NO
    assert c[4] & 0x1F == 18 and not c[4] & PUSH
    fwd, rev = cases("roundabout")
    (c,) = _contra(rev)
    assert c[3] == _real(fwd)[0][3] != 0 and not c[4] & ROUNDABOUT


def test_one_ways_that_do_not_bind_walkers(cases):
    for name in ("pedestrian_oneway", "path_bike_designated_oneway", "mtb_oneway"):
        (c,) = _contra(cases(name)[1])
        assert not c[4] & FOOT_DIR_DENY, name
        assert c[4] & PUSH, name             # still one-way for the bike
    fwd, rev = cases("oneway_bicycle_yes")
    assert not _real(fwd)[0][4] & PUSH and _real(rev)[0][4] & PUSH


def test_access_rules(cases):
    e = _real(cases("private")[0])[0][4]
    assert e & PRIVATE and not e & (FOOT_DENY | BIKE_DENY)
    assert _real(cases("path_vehicle_no")[0])[0][4] & BIKE_DENY
    assert not _real(cases("path_vehicle_no")[0])[0][4] & FOOT_DENY
    assert _real(cases("footway_access_no")[0])[0][4] & FOOT_DENY
    e = _real(cases("use_sidepath")[0])[0][4]
    assert e & BIKE_DENY and not e & FOOT_DENY
    assert _real(cases("dismount")[0])[0][4] & PUSH
    assert _real(cases("pedestrian")[0])[0][4] & PUSH
    assert _real(cases("bridleway")[0])[0][4] & 0x1F == 21
    fwd, rev = cases("motorway_foot_yes")
    assert _contra(rev) == [] and not _real(fwd)[0][4] & FOOT_DENY


def test_steps_footways_and_motorroads(cases):
    assert all(e[4] & PUSH and not e[4] & BIKE_DENY for e in _both(cases("steps")))
    assert not any(e[4] & BIKE_DENY for e in _both(cases("steps_ramp")))
    assert all(e[4] & BIKE_DENY for e in _both(cases("steps_bike_no")))
    assert all(e[4] & PUSH for e in _both(cases("footway")))
    assert not any(e[4] & PUSH for e in _both(cases("footway_bike_yes")))
    assert all(e[4] & FOOT_DENY and e[4] & BIKE_DENY for e in _both(cases("motorroad")))


def test_oneway_foot_on_a_two_way_road(cases):
    fwd, rev = cases("oneway_foot_yes")
    assert not _real(fwd)[0][4] & FOOT_DIR_DENY and _real(rev)[0][4] & FOOT_DIR_DENY


def test_lanes_surface_and_sidewalks(cases):
    (e,) = _real(cases("lane_paved_nosidewalk")[0])
    assert e[4] & LANE and (e[4] >> 15) & 3 == 1 and e[4] & SIDEWALK_NO
    (e,) = _real(cases("cycleway_gravel")[0])
    assert e[4] & INFRA and (e[4] >> 15) & 3 == 2
    surface = {n: (_real(cases(n)[0])[0][4] >> 15) & 3
               for n in ("track_dirt", "track_grade1", "brick")}
    assert surface == {"track_dirt": 3, "track_grade1": 1, "brick": 1}
    side = {n: _real(cases(n)[0])[0][4] & (SIDEWALK_NO | SEPARATE)
            for n in ("sidewalk_separate", "sidewalk_lr_no", "sidewalk_lr_separate")}
    assert side == {"sidewalk_separate": SEPARATE, "sidewalk_lr_no": SIDEWALK_NO,
                    "sidewalk_lr_separate": SEPARATE}


@pytest.mark.parametrize("oneway", ["yes", "-1"])
def test_contraflow_geometry_is_the_real_edges_reversed(tmp_path, monkeypatch, oneway):
    """A four-node one-way: the record points at the same stored geometry
    as the real edge (which runs the other way) and, in the spatial cells,
    decodes to the same points — the reader reverses it (bit 11)."""
    from streetzim.routing.spatial import build_spatial
    from tests.test_routing_worker_v3 import _spatial_graph_from_dir

    pbf = tmp_path / "ow.osm.pbf"
    pts = [(7.40, 43.70), (7.401, 43.7003), (7.402, 43.7001), (7.403, 43.7004)]
    with osmium.SimpleWriter(str(pbf)) as wr:
        for i, loc in enumerate(pts):
            wr.add_node(osmium.osm.mutable.Node(id=i + 1, location=loc, version=1))
        wr.add_way(osmium.osm.mutable.Way(
            id=10, nodes=[1, 2, 3, 4], version=1,
            tags={"highway": "residential", "oneway": oneway}))
    g = _build(tmp_path, pbf, "out", True, monkeypatch)
    allE = [e for n in _out_edges(g) for e in n]
    (real,) = [e for e in allE if not e[4] & CONTRA]
    (con,) = [e for e in allE if e[4] & CONTRA]
    assert con[1] & 0xFFFFFF == real[1] & 0xFFFFFF and con[1] >> 24 == 0
    assert real[2] != 0xFFFFFFFF and con[2] == real[2] and con[4] & GEOM_REV
    build_spatial(g, cell_scale=10, output_dir=tmp_path / "cells")
    sg = _spatial_graph_from_dir(tmp_path / "cells")
    def geom_of(want_contra):
        for n in range(sg.num_nodes):
            for (t, _sd, gl, _ni, ca) in sg.edges_of_node(n):
                if bool(ca & CONTRA) == want_contra:
                    return n, t, sg.decode_geom_for_edge(n, 0, gl)
    rn, rt, rg = geom_of(False)
    cn, ct, cg = geom_of(True)
    assert (cn, ct) == (rt, rn) and cg == rg and len(rg) == 2
