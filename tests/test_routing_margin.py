"""--clip-poly: the road pieces a clip cuts off past the border, closed to
cars (streetzim/routing/margin.py).

What must hold: a node past the border that the main network cannot reach,
and whose drive component is neither the main network nor mostly inside
the border (a cut stub, what the cut left of the neighbour's roads), loses
car access on its outgoing edges, so the car snap passes it over and a
destination there routes to a road that leads in. Nothing inside the
border changes (an exclave joined to a bigger neighbour town included);
a road the main network reaches stays (a one-way spur leading away); a
component mostly inside stays; routes are exactly what they were wherever
they still exist; walking and cycling are untouched; only class_access
bit 9 changes; and the build closes them (in a child of its own) when, and
only when, a clip is active.
"""
from __future__ import annotations

import hashlib
import shutil
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

osmium = pytest.importorskip("osmium")
np = pytest.importorskip("numpy")
shapely = pytest.importorskip("shapely")
from shapely.geometry import box  # noqa: E402

from streetzim import clip  # noqa: E402
from streetzim.routing.build import extract_routing_graph  # noqa: E402
from streetzim.routing.margin import close_cut_off_roads  # noqa: E402
from streetzim.routing.reader import load_from_file  # noqa: E402
from streetzim.routing.restrictions import read_sidecar  # noqa: E402
from streetzim.routing.spatial import build_spatial  # noqa: E402
from streetzim.routing.spatial_astar import find_route_spatial  # noqa: E402
from tests.test_routing_worker_v3 import _spatial_graph_from_dir  # noqa: E402

# The region: lon 7.40-7.50, lat 43.70-43.80. A 6 x 6 grid of two-way
# streets fills it (36 nodes, past the snap's 32-node reach test); its
# western column runs just inside the border.
BORDER = box(7.40, 43.70, 7.50, 43.80)
G0, GS = (7.401, 43.72), 0.01
GRID = {f"g{i}{j}": (G0[0] + i * GS, G0[1] + j * GS) for i in range(6) for j in range(6)}
NODES = {
    **GRID,
    "c1": (7.38, 43.74), "c2": (7.36, 43.74),     # crossing: two-way out of g02
    "k": (7.36, 43.77),                           # one-way c1 -> k -> k2: a spur leading away
    "k2": (7.355, 43.775),
    "s": (7.37, 43.70),                           # one-way s -> g00: a way out, no way in
    "f1": (7.35, 43.79), "f2": (7.355, 43.79),    # a fragment, two-way, joined to nothing
    "x": (7.399, 43.765),                         # one-way primary g04 -> x -> g05: out and back
    "w": (7.365, 43.705),                         # footway s - w (walkers only)
    # An exclave: a two-way triangle, two nodes inside, one past the
    # eastern border, joined to nothing else. Mostly inside: stays open.
    "e1": (7.49, 43.785), "e2": (7.495, 43.79), "e3": (7.505, 43.79),
    # The neighbour's piece: a two-way triangle, one node inside the
    # outline, two past it, joined to nothing else. Mostly outside: n2, n3
    # close; n1 is inside and stays.
    "n1": (7.405, 43.705), "n2": (7.39, 43.705), "n3": (7.395, 43.71),
    # Exactly half inside, joined to nothing: not "mostly", so h2 closes.
    "h1": (7.495, 43.705), "h2": (7.505, 43.705),
    # An exclave (a square inside) joined two-way to a bigger neighbour
    # town past the border, both cut off from the main network: the town
    # closes, the exclave's own roads stay.
    "m1": (7.47, 43.705), "m2": (7.48, 43.705), "m3": (7.48, 43.715), "m4": (7.47, 43.715),
    "t1": (7.47, 43.695), "t2": (7.48, 43.695), "t3": (7.49, 43.695),
    "t4": (7.49, 43.69), "t5": (7.48, 43.69), "t6": (7.47, 43.69),
}
# The nodes that lose car access.
CUT = ("s", "f1", "f2", "n2", "n3", "h2", "t1", "t2", "t3", "t4", "t5", "t6")


def _ways():
    ways = []
    for i in range(6):
        for j in range(6):
            if i < 5:
                ways.append(([f"g{i}{j}", f"g{i + 1}{j}"], {}))
            if j < 5:
                ways.append(([f"g{i}{j}", f"g{i}{j + 1}"], {}))
    ways += [
        (["g02", "c1"], {}), (["c1", "c2"], {}),
        (["c1", "k"], {"oneway": "yes"}), (["k", "k2"], {"oneway": "yes"}),
        # Cyclists may ride it both ways: a speed-0 record g00 -> s, not a way in for cars.
        (["s", "g00"], {"oneway": "yes", "oneway:bicycle": "no"}),
        (["f1", "f2"], {}),
        # Split at x so x is a graph node (a way's inner nodes are geometry).
        (["g04", "x"], {"oneway": "yes", "highway": "primary"}),
        (["x", "g05"], {"oneway": "yes", "highway": "primary"}),
        (["s", "w"], {"highway": "footway"}),
        (["g00", "s"], {"motor_vehicle": "no"}),  # a street closed to cars (bit 9 only)
        (["g05", "f1"], {"highway": "footway"}),  # walkers reach the fragment; cars cannot
        (["e1", "e2"], {}), (["e2", "e3"], {}), (["e3", "e1"], {}),
        (["n1", "n2"], {}), (["n2", "n3"], {}), (["n3", "n1"], {}),
        (["h1", "h2"], {}),
        (["m1", "m2"], {}), (["m2", "m3"], {}), (["m3", "m4"], {}), (["m4", "m1"], {}),
        (["t1", "t2"], {}), (["t2", "t3"], {}), (["t3", "t4"], {}), (["t4", "t5"], {}),
        (["t5", "t6"], {}), (["t6", "t1"], {}), (["m1", "t1"], {}), (["m2", "t2"], {}),
    ]
    return ways


def _build(tmp_path, monkeypatch, name="g"):
    pbf = tmp_path / "net.osm.pbf"
    if not pbf.exists():
        nid = {k: i + 1 for i, k in enumerate(NODES)}
        with osmium.SimpleWriter(str(pbf)) as wr:
            for k, (lon, lat) in NODES.items():
                wr.add_node(osmium.osm.mutable.Node(id=nid[k], location=(lon, lat), version=1))
            for i, (ns, tags) in enumerate(_ways()):
                t = {"highway": "residential", **tags}
                wr.add_way(osmium.osm.mutable.Way(id=100 + i, nodes=[nid[n] for n in ns],
                                                  tags=t, version=1))
    monkeypatch.setenv("STREETZIM_NODE_LOC_DIR", str(tmp_path))
    out = tmp_path / name
    out.mkdir()
    return Path(extract_routing_graph(str(pbf), str(out)))


def _spatial(graph_path, tmp_path, name):
    d = tmp_path / f"cells-{name}"
    build_spatial(load_from_file(graph_path, mapped=True), cell_scale=10, output_dir=d,
                  restrictions=read_sidecar(graph_path))
    return _spatial_graph_from_dir(d)


def _index(graph_path):
    """{name: node id} from the graph's coordinates."""
    raw = open(graph_path, "rb").read()
    n = struct.unpack_from("<I", raw, 8)[0]
    co = np.frombuffer(raw, "<i4", 2 * n, 32)
    by = {(round(co[2 * i + 1] / 1e7, 4), round(co[2 * i] / 1e7, 4)): i for i in range(n)}
    return {k: by[(round(lon, 4), round(lat, 4))] for k, (lon, lat) in NODES.items()
            if (round(lon, 4), round(lat, 4)) in by}


def _sid(sg, name):
    """`name`'s node in a spatial graph (build_spatial numbers nodes by cell)."""
    lon, lat = NODES[name]
    return sg.nearest_node(round(lat * 1e7), round(lon * 1e7), raw=True)


def _edges(graph_path):
    raw = open(graph_path, "rb").read()
    n, e = struct.unpack_from("<2I", raw, 8)
    adj = np.frombuffer(raw, "<u4", n + 1, 32 + 8 * n).astype(np.int64)
    ed = np.frombuffer(raw, "<u4", 5 * e, 32 + 8 * n + 4 * (n + 1)).reshape(e, 5)
    src = np.repeat(np.arange(n), np.diff(adj))
    return src, ed


def _route(sg, lonlat_a, lonlat_b, mode="drive"):
    s = sg.nearest_node(round(lonlat_a[1] * 1e7), round(lonlat_a[0] * 1e7), "origin", travel_mode=mode)
    t = sg.nearest_node(round(lonlat_b[1] * 1e7), round(lonlat_b[0] * 1e7), "dest", travel_mode=mode)
    r = find_route_spatial(sg, s, t, travel_mode=mode)
    return s, t, r


@pytest.fixture
def graphs(tmp_path, monkeypatch):
    """(original graph path, closed copy path, (nodes, edges) closed)."""
    orig = _build(tmp_path, monkeypatch)
    closed = tmp_path / "closed" / "routing-graph.bin"
    closed.parent.mkdir()
    shutil.copy(orig, closed)
    side = orig.with_name("routing-restrictions.bin")
    if side.exists():
        shutil.copy(side, closed.with_name(side.name))
    return orig, closed, close_cut_off_roads(str(closed), BORDER)


def test_only_cut_off_margin_nodes_lose_car_access(graphs):
    orig, closed, (n_nodes, n_edges) = graphs
    ix = _index(orig)
    src, before = _edges(orig)
    _, after = _edges(closed)
    changed = before[:, 4] != after[:, 4]
    assert not (before[:, :4] != after[:, :4]).any()                 # only class_access
    assert ((before[:, 4] ^ after[:, 4]) & ~np.uint32(0x200) == 0).all()  # only bit 9
    assert (after[changed, 4] & 0x200).all()
    closed_nodes = {int(v) for v in np.unique(src[changed])}
    # s: a way out, none in. f1, f2: joined to nothing. n2, n3: the
    # neighbour's piece. h2: half inside is not mostly. t1-t6: the town.
    assert closed_nodes == {ix[k] for k in CUT}
    assert (n_nodes, n_edges) == (len(CUT), int(changed.sum()))
    # The crossing, the out-and-back loop, the exclave e1-e3 (e3 past the
    # border), the spur k the main network reaches, and everything inside
    # (n1, h1, the exclave m1-m4) stay open.
    for a in ("c1", "c2", "x", "e1", "e2", "e3", "k", "n1", "h1", "m1", "m2", "m3", "m4"):
        assert ix[a] not in closed_nodes


def test_a_destination_on_a_cut_off_road_routes_to_a_road_that_leads_in(graphs, tmp_path):
    orig, closed, _ = graphs
    inside = GRID["g33"]
    near_s = (7.369, 43.701)
    sg = _spatial(orig, tmp_path, "orig")
    # Before: the snap takes s (a way out, so it passes the reach test),
    # and no route reaches it.
    _, t, r = _route(sg, inside, near_s)
    assert t == _sid(sg, "s") and r is None
    sg = _spatial(closed, tmp_path, "closed")
    _, t, r = _route(sg, inside, near_s)
    assert t == _sid(sg, "g00") and r is not None


def test_routes_between_nodes_inside_are_unchanged(graphs, tmp_path):
    orig, closed, _ = graphs
    a, b = _spatial(orig, tmp_path, "orig"), _spatial(closed, tmp_path, "closed")
    names = sorted(GRID)
    for i, p in enumerate(names):
        for q in names[i + 1::5]:
            ra = find_route_spatial(a, _sid(a, p), _sid(a, q))
            rb = find_route_spatial(b, _sid(b, p), _sid(b, q))
            assert ra is not None and rb is not None
            assert ra.node_sequence == rb.node_sequence
            assert ra.total_dist_m == rb.total_dist_m
    # In the exclaves, and to the end of the spur leading away.
    for p, q in [("e1", "e3"), ("e3", "e2"), ("m1", "m3"), ("m3", "m2"), ("g33", "k2")]:
        ra = find_route_spatial(a, _sid(a, p), _sid(a, q))
        rb = find_route_spatial(b, _sid(b, p), _sid(b, q))
        assert ra is not None and ra.node_sequence == rb.node_sequence
    # The primary loop out of the region and back is still the fast way.
    rb = find_route_spatial(b, _sid(b, "g04"), _sid(b, "g05"))
    assert rb is not None and _sid(b, "x") in rb.node_sequence


@pytest.mark.parametrize("mode", ["walk", "bike"])
def test_walking_and_cycling_are_untouched(graphs, tmp_path, mode):
    orig, closed, _ = graphs
    a, b = _spatial(orig, tmp_path, "orig"), _spatial(closed, tmp_path, "closed")
    for p, q in [("g33", "s"), ("g00", "f1"), ("g02", "k"), ("s", "w")]:
        ra = find_route_spatial(a, _sid(a, p), _sid(a, q), travel_mode=mode)
        rb = find_route_spatial(b, _sid(b, p), _sid(b, q), travel_mode=mode)
        assert (ra is None) == (rb is None)
        if ra is not None:
            assert ra.node_sequence == rb.node_sequence
    assert find_route_spatial(b, _sid(b, "g33"), _sid(b, "s"), travel_mode="walk") is not None


def test_no_car_road_without_bit_9(graphs, tmp_path):
    # Older graphs mark a footway by its class alone (ordinals 16-20, as
    # every reader's is_no_motor) and a record against a one-way by speed 0
    # alone: neither the footway to the fragment nor the cyclists' record
    # g00 -> s may count as a way in for cars there.
    orig, closed, _ = graphs
    old = tmp_path / "old" / "routing-graph.bin"
    old.parent.mkdir()
    shutil.copy(orig, old)
    raw = bytearray(old.read_bytes())
    n, e = struct.unpack_from("<2I", raw, 8)
    ed = np.frombuffer(raw, "<u4", 5 * e, 32 + 8 * n + 4 * (n + 1)).reshape(e, 5).copy()
    footway = ((ed[:, 4] & 0x1F) >= 16) & ((ed[:, 4] & 0x1F) <= 20)
    contra = (ed[:, 4] & 0x400) != 0                    # against a one-way, speed 0
    assert footway.any() and contra.any() and (ed[footway | contra, 4] & 0x200).all()
    assert ((ed[contra, 1] >> 24) == 0).all()
    footway |= contra
    ed[footway, 4] &= ~np.uint32(0x200)
    raw[32 + 8 * n + 4 * (n + 1):32 + 8 * n + 4 * (n + 1) + 20 * e] = ed.tobytes()
    old.write_bytes(bytes(raw))
    close_cut_off_roads(str(old), BORDER)
    src, after_old = _edges(old)
    _, after_new = _edges(closed)
    ix = _index(orig)
    drive = ~footway
    assert (after_old[drive, 4] == after_new[drive, 4]).all()
    assert {int(v) for v in np.unique(src[(after_old[:, 4] & 0x200 != 0) & drive
                                         & (_edges(orig)[1][:, 4] & 0x200 == 0)])} \
        == {ix[k] for k in CUT}


def test_the_main_network_stays_open_however_small_the_region(tmp_path, monkeypatch):
    # A region holding four of the grid's 36 nodes (a Monaco): the grid is
    # mostly outside, but it is the main network, so it stays open; the
    # pieces joined to nothing still close.
    g = _build(tmp_path, monkeypatch)
    ix = _index(g)
    src, before = _edges(g)
    close_cut_off_roads(str(g), box(7.4005, 43.7195, 7.4115, 43.7305))
    _, after = _edges(g)
    closed_nodes = {int(v) for v in np.unique(src[before[:, 4] != after[:, 4]])}
    assert not closed_nodes & {ix[k] for k in GRID}
    assert {ix["f1"], ix["s"]} <= closed_nodes
    # The main network is the largest component, not the one with the most
    # nodes inside: here the exclave m1-m4 has four inside, the grid one.
    g2 = _build(tmp_path, monkeypatch, "g2")
    src, before = _edges(g2)
    close_cut_off_roads(str(g2), box(7.445, 43.70, 7.485, 43.725))
    _, after = _edges(g2)
    closed_nodes = {int(v) for v in np.unique(src[before[:, 4] != after[:, 4]])}
    assert not closed_nodes & {ix[k] for k in GRID}


def test_nothing_past_the_border_leaves_the_file_as_it_was(tmp_path, monkeypatch):
    g = _build(tmp_path, monkeypatch)
    digest = hashlib.sha256(g.read_bytes()).hexdigest()
    assert close_cut_off_roads(str(g), box(7.0, 43.0, 8.0, 44.0)) == (0, 0)
    assert hashlib.sha256(g.read_bytes()).hexdigest() == digest


def test_not_a_v4_graph_is_refused(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"NOPE" + bytes(28))
    with pytest.raises(ValueError, match="not an SZRG"):
        close_cut_off_roads(str(p), BORDER)
    p.write_bytes(b"SZRG" + struct.pack("<7I", 5, 1, 1, 0, 0, 0, 0))
    with pytest.raises(ValueError, match="v5"):
        close_cut_off_roads(str(p), BORDER)


def _poly(tmp_path):
    p = tmp_path / "region.poly"
    p.write_text("region\n1\n   7.40 43.70\n   7.50 43.70\n   7.50 43.80\n"
                 "   7.40 43.80\n   7.40 43.70\nEND\nEND\n")
    return p


def _routing_step(tmp_path, name, pbf):
    """create_osm_zim's routing step, as a build runs it (spawned children)."""
    from types import SimpleNamespace
    import create_osm_zim
    out = tmp_path / name
    out.mkdir()
    return Path(create_osm_zim._build_routing(
        args=SimpleNamespace(pbf=None, spatial_chunk_scale=0), bbox_str="7.3,43.6,7.6,43.85",
        include_routing=True, include_wikidata=False, pbf_path=None, tmpdir=str(out),
        total_steps=6, work_pbf=str(pbf), work_pbf_cut=True))


def test_the_build_closes_cut_off_roads_only_with_a_clip(tmp_path, monkeypatch):
    plain = _build(tmp_path, monkeypatch, "plain")
    pbf = tmp_path / "net.osm.pbf"
    unclipped = _routing_step(tmp_path, "unclipped", pbf)
    assert unclipped.read_bytes() == plain.read_bytes()
    c = clip.Clip.from_poly_file(str(_poly(tmp_path)), buffer=10)
    assert c.border is not None and c.border.equals(BORDER)
    clip.set_active(c)
    try:
        clipped = _routing_step(tmp_path, "clipped", pbf)
    finally:
        clip.set_active(None)
    src, a = _edges(plain)
    _, b = _edges(clipped)
    assert a.shape == b.shape and not (a[:, :4] != b[:, :4]).any()
    ix = _index(plain)
    changed = {int(v) for v in np.unique(src[a[:, 4] != b[:, 4]])}
    assert changed == {ix[k] for k in CUT}
