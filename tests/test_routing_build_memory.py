"""The routing-graph builder's memory work (streetzim/routing/build.py):
junction ids in a sorted array, refs spilled to files, geometries spilled and
deduplicated afterwards, only the highway nodes in a location index that is
in memory or in a file by size. None of it may change a byte of the graph:
every build here is compared with the dict-based builder it replaced
(tests/fixtures/routing_build_reference.py), on random road networks that
have the cases the old code handled in-pass (repeated geometry, one-ways
both ways, loops, missing nodes, the blob cap, hash collisions).
"""
from __future__ import annotations

import hashlib
import importlib.util
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

from streetzim.routing import build  # noqa: E402

HIGHWAYS = ["residential", "primary", "motorway", "footway", "service",
            "track", "steps", "construction", "unknown_class", ""]


def _reference():
    spec = importlib.util.spec_from_file_location(
        "routing_build_reference",
        Path(__file__).parent / "fixtures" / "routing_build_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _network(path, seed, n_nodes=400, n_ways=260, lon0=7.40, lat0=43.72,
             step=0.0005, grid=12):
    """A random road network as a sorted PBF. Nodes sit on a small grid so
    different nodes share a location (different ways then have the same
    geometry bytes, which is what the dedup finds); some ways repeat or
    reverse another's nodes, close on themselves, use a node the file does
    not have, or are not roads."""
    osmium = pytest.importorskip("osmium")
    rng = random.Random(seed)
    base = 5_000_000_000                       # ids past 32 bits
    ids = [base + 3 * i for i in range(n_nodes)]
    loc = {}
    for nid in ids:
        loc[nid] = (round(lon0 + step * rng.randrange(grid), 7),
                    round(lat0 + step * rng.randrange(grid), 7))
    missing = base + 3 * n_nodes + 1          # referenced, never written
    ways = []
    for _ in range(n_ways):
        kind = rng.random()
        if ways and kind < 0.1:
            nodes = list(rng.choice(ways)[0])                 # same nodes
        elif ways and kind < 0.18:
            nodes = list(reversed(rng.choice(ways)[0]))       # reversed
        else:
            k = rng.randrange(1, 9)
            start = rng.randrange(n_nodes)
            nodes = [ids[(start + rng.randrange(-20, 21)) % n_nodes]
                     for _ in range(k)]
            if k > 2 and rng.random() < 0.1:
                nodes.append(nodes[0])                        # a loop
            if rng.random() < 0.03:
                nodes.insert(rng.randrange(len(nodes) + 1), missing)
        tags = {}
        hw = rng.choice(HIGHWAYS)
        if hw:
            tags["highway"] = hw
        elif rng.random() < 0.5:
            tags["building"] = "yes"
        r = rng.random()
        if r < 0.15:
            tags["oneway"] = "yes"
        elif r < 0.25:
            tags["oneway"] = "-1"
        elif r < 0.3:
            tags["oneway"] = "no"
        if rng.random() < 0.08:
            tags["junction"] = rng.choice(["roundabout", "circular",
                                           "mini_roundabout"])
        if rng.random() < 0.5:
            tags["name"] = f"Street {rng.randrange(30)}"
        if rng.random() < 0.2:
            tags["ref"] = f"D{rng.randrange(9)}"
        if rng.random() < 0.1:
            tags["access"] = "no"
        if rng.random() < 0.1:
            tags["foot"] = "no"
        ways.append((nodes, tags))
    with osmium.SimpleWriter(str(path)) as wr:
        for nid in ids:
            wr.add_node(osmium.osm.mutable.Node(id=nid, location=loc[nid],
                                                version=1))
        for i, (nodes, tags) in enumerate(ways):
            wr.add_way(osmium.osm.mutable.Way(id=100 + i, nodes=nodes,
                                              version=1, tags=tags))
    return path


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _build(fn, pbf, out, **kw):
    out.mkdir()
    g = fn(str(pbf), str(out), **kw)
    return _sha(g) if g else None


@pytest.fixture
def scratch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_NODE_LOC_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("index", ["memory", "file"])
def test_graph_bytes_match_the_dict_builder(scratch_env, monkeypatch, seed, index):
    pytest.importorskip("osmium")
    tmp = scratch_env
    pbf = _network(tmp / "net.osm.pbf", seed)
    want = _build(_reference().extract_routing_graph, pbf, tmp / "ref")
    monkeypatch.setenv("STREETZIM_ROUTING_NODE_INDEX", index)
    assert _build(build.extract_routing_graph, pbf, tmp / "new") == want
    # The scratch the builder made is gone: refs, geometries, the highway
    # extract and the index file.
    assert not list(tmp.glob("streetzim_node_loc_*"))
    assert sorted(p.name for p in (tmp / "new").iterdir()) == ["routing-graph.bin"]


def test_same_bytes_without_the_osmium_tool(scratch_env, monkeypatch):
    pytest.importorskip("osmium")
    tmp = scratch_env
    pbf = _network(tmp / "net.osm.pbf", 5)
    want = _build(_reference().extract_routing_graph, pbf, tmp / "ref")
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    assert _build(build.extract_routing_graph, pbf, tmp / "new") == want


def test_hash_collisions_are_resolved_by_bytes(scratch_env, monkeypatch):
    """With a hash that collides all the time, the dedup must still keep
    every distinct geometry and merge only equal ones."""
    pytest.importorskip("osmium")
    tmp = scratch_env
    pbf = _network(tmp / "net.osm.pbf", 6)
    want = _build(_reference().extract_routing_graph, pbf, tmp / "ref")
    monkeypatch.setattr(build, "_geom_hash", lambda b: len(b) % 3)
    assert _build(build.extract_routing_graph, pbf, tmp / "new") == want


@pytest.mark.parametrize("share", [0.0, 0.3, 0.7, 0.999])
def test_blob_cap_cuts_where_the_dict_builder_did(scratch_env, monkeypatch, share):
    pytest.importorskip("osmium")
    tmp = scratch_env
    pbf = _network(tmp / "net.osm.pbf", 7, n_nodes=4000, n_ways=400, grid=200)
    ref = _reference()
    full = Path(ref.extract_routing_graph(str(pbf), str(tmp))).read_bytes()
    geom_bytes = np.frombuffer(full[4:32], dtype="<u4")[4]
    assert geom_bytes > 1000
    cap = int(geom_bytes * share)
    monkeypatch.setattr(ref, "GEOM_BLOB_CAP", cap)
    monkeypatch.setattr(build, "GEOM_BLOB_CAP", cap)
    want = _build(ref.extract_routing_graph, pbf, tmp / "ref")
    assert _build(build.extract_routing_graph, pbf, tmp / "new") == want


def test_antimeridian_join_matches(scratch_env, monkeypatch):
    """Roads split at ±180 join into one vertex, as before, with the
    location index the size rule picked (here a file)."""
    osmium = pytest.importorskip("osmium")
    tmp = scratch_env
    pbf = tmp / "fiji.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as wr:
        pts = [(1, 179.99, -16.8), (2, 180.0, -16.81), (3, -180.0, -16.81),
               (4, -179.99, -16.82), (5, 179.995, -16.7), (6, -179.995, -16.9)]
        for nid, lon, lat in pts:
            wr.add_node(osmium.osm.mutable.Node(id=nid, location=(lon, lat), version=1))
        wr.add_way(osmium.osm.mutable.Way(id=10, nodes=[1, 5, 2], version=1,
                                         tags={"highway": "primary"}))
        wr.add_way(osmium.osm.mutable.Way(id=11, nodes=[3, 6, 4], version=1,
                                         tags={"highway": "primary"}))
    box = [172.8, -23.2, 183.5, -11.2]
    want = _build(_reference().extract_routing_graph, pbf, tmp / "ref",
                  bbox=box, precut=True)
    monkeypatch.setenv("STREETZIM_ROUTING_NODE_INDEX", "file")
    specs = []
    real = osmium.index.create_map
    monkeypatch.setattr(osmium.index, "create_map",
                        lambda spec: specs.append(spec) or real(spec))
    real_twins = build._antimeridian_twins

    def twins(*a, idx):
        specs.append(idx)
        return real_twins(*a, idx=idx)

    monkeypatch.setattr(build, "_antimeridian_twins", twins)
    assert _build(build.extract_routing_graph, pbf, tmp / "new",
                  bbox=box, precut=True) == want
    # One index file for the antimeridian pass, another for Pass 2.
    assert [s.split(",")[0] for s in specs] == ["sparse_file_array"] * 2
    assert specs[0] != specs[1]
    assert not list(tmp.glob("streetzim_node_loc_*"))


def test_file_index_is_a_file_in_the_scratch_dir(scratch_env, monkeypatch):
    osmium = pytest.importorskip("osmium")
    tmp = scratch_env
    pbf = _network(tmp / "net.osm.pbf", 8)
    seen = []
    real = osmium.index.create_map

    def create_map(spec):
        kind, _, path = spec.partition(",")
        seen.append((kind, path, Path(path).exists() if path else None))
        return real(spec)

    monkeypatch.setattr(osmium.index, "create_map", create_map)
    monkeypatch.setenv("STREETZIM_ROUTING_NODE_INDEX", "file")
    _build(build.extract_routing_graph, pbf, tmp / "f")
    monkeypatch.setenv("STREETZIM_ROUTING_NODE_INDEX", "memory")
    _build(build.extract_routing_graph, pbf, tmp / "m")
    assert len(seen) == 2
    kind, path, existed = seen[0]
    assert kind == "sparse_file_array" and existed
    assert Path(path).parent == tmp and Path(path).name.startswith("streetzim_node_loc_")
    assert not Path(path).exists()
    assert seen[1] == ("sparse_mem_array", "", None)


def test_no_highways_is_no_graph(scratch_env):
    osmium = pytest.importorskip("osmium")
    tmp = scratch_env
    pbf = tmp / "none.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as wr:
        wr.add_node(osmium.osm.mutable.Node(id=1, location=(7.4, 43.7), version=1))
        wr.add_node(osmium.osm.mutable.Node(id=2, location=(7.5, 43.7), version=1))
        wr.add_way(osmium.osm.mutable.Way(id=3, nodes=[1, 2], version=1,
                                         tags={"building": "yes"}))
    (tmp / "out").mkdir()
    assert build.extract_routing_graph(str(pbf), str(tmp / "out")) is None
    assert list((tmp / "out").iterdir()) == []


# --- dedup_geoms against the in-pass dict it replaced ---------------------

def _online(cands, cap):
    """The old Pass 2: a dict of bytes, the cap checked as a segment starts."""
    blob = bytearray()
    seen = {}
    out = []
    near = False
    for data, first in cands:
        if first:
            near = len(blob) >= cap
        if near:
            out.append(0xFFFFFFFF)
            continue
        gi = seen.get(data)
        if gi is None:
            gi = seen[data] = len(seen)
            blob += data
        out.append(gi)
    return out, bytes(blob)


def _stream(rng, n):
    pool = [bytes(rng.randrange(256) for _ in range(rng.randrange(1, 6)))
            for _ in range(max(1, n // 3))]
    cands = []
    while len(cands) < n:
        if rng.random() < 0.5:
            cands.append((rng.choice(pool), 1))
            cands.append((rng.choice(pool), 0))
        else:
            cands.append((rng.choice(pool), 1))
    return cands


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("hasher", ["hash", "weak", "constant"])
def test_dedup_geoms_matches_the_dict(seed, hasher):
    rng = random.Random(seed)
    cands = _stream(rng, rng.randrange(1, 300))
    total = sum(len(d) for d in {d for d, _ in cands})
    cap = rng.choice([0, 1, 7, 40, total // 2, 1 << 40])
    h = {"hash": hash, "weak": len, "constant": lambda b: 0}[hasher]
    want, want_blob = _online(cands, cap)
    geom_of, keep = build.dedup_geoms(
        [len(d) for d, _ in cands], [h(d) for d, _ in cands],
        [f for _, f in cands], lambda k: cands[k][0], cap)
    assert geom_of.tolist() == want
    assert b"".join(d for (d, _), k in zip(cands, keep) if k) == want_blob


def test_dedup_geoms_empty():
    geom_of, keep = build.dedup_geoms([], [], [], lambda k: b"")
    assert len(geom_of) == 0 and len(keep) == 0


# --- choose_node_index ------------------------------------------------------

def test_choose_node_index():
    GB = 1 << 30
    assert build.choose_node_index("file", 10, None)[0] is True
    assert build.choose_node_index("memory", 10**9, 1 * GB)[0] is False
    # Small: memory, with or without a limit.
    assert build.choose_node_index("auto", 1_000_000, None)[0] is False
    assert build.choose_node_index("auto", 1_000_000, 16 * GB)[0] is False
    # Over the absolute bound: 3 x 16 B x 23 M = 1.1 GB.
    assert build.choose_node_index("auto", 23_000_000, None)[0] is True
    # Under it, but over a tenth of a small limit: 3 x 16 x 5 M = 240 MB > 0.2 GB.
    assert build.choose_node_index("auto", 5_000_000, 2 * GB)[0] is True
    assert build.choose_node_index("auto", 5_000_000, 16 * GB)[0] is False
    with pytest.raises(ValueError):
        build.choose_node_index("disk", 1, None)
