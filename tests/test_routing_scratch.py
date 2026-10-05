"""Each routing run uses a unique node-location scratch file, removes it,
and reclaims those older than six hours that killed runs left behind."""
import os
from pathlib import Path
import time
import hashlib

import pytest


@pytest.mark.parametrize("failure", [None, "index", "pass2"])
def test_routing_reclaims_old_foreign_index_and_cleans_own(tmp_path, monkeypatch, failure):
    osmium = pytest.importorskip("osmium")
    from streetzim.routing.build import extract_routing_graph

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # An old index may belong to a long build still running: unlinking it
    # must not disturb that build, which already has it open.
    foreign = scratch / "streetzim_node_loc_another_active_build.bin"
    foreign.write_bytes(b"another build's index")
    old_time = time.time() - 7 * 3600
    os.utime(foreign, (old_time, old_time))
    in_use = open(foreign, "r+b")
    recent = scratch / "streetzim_node_loc_recent_build.bin"
    recent.write_bytes(b"a recent build's index")
    unrelated = scratch / "other_old_file.bin"
    unrelated.write_bytes(b"not ours")
    os.utime(unrelated, (old_time, old_time))
    monkeypatch.setenv("STREETZIM_NODE_LOC_DIR", str(scratch))

    pbf = tmp_path / "road.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=(7.42, 43.73), version=1))
        writer.add_node(osmium.osm.mutable.Node(id=2, location=(7.43, 43.74), version=1))
        writer.add_way(osmium.osm.mutable.Way(id=10, nodes=[1, 2], version=1,
                                             tags={"highway": "residential"}))

    def fail(*args, **kwargs):
        raise RuntimeError("routing failed")

    if failure == "index":
        monkeypatch.setattr(osmium.index, "create_map", fail)
    elif failure == "pass2":
        monkeypatch.setattr(osmium, "apply", fail)

    if failure:
        with pytest.raises(RuntimeError, match="routing failed"):
            extract_routing_graph(str(pbf), str(tmp_path))
    else:
        from streetzim.routing.reader import load_from_file
        out = extract_routing_graph(str(pbf), str(tmp_path))
        graph = load_from_file(out)
        assert graph.num_nodes == 2 and graph.num_edges == 2
        assert Path(out).read_bytes().startswith(b"SZRG")
    assert not foreign.exists()
    with in_use:
        assert in_use.read() == b"another build's index"
        in_use.write(b" still writable")
        in_use.seek(0)
        assert in_use.read().endswith(b"still writable")
    assert sorted(scratch.iterdir()) == [unrelated, recent]


def test_monaco_graph_matches_pre_review_reference(tmp_path, monkeypatch):
    """The real fixture's SZRG bytes must survive the serialization/lifetime fixes."""
    pytest.importorskip("osmium")
    from streetzim.routing.build import extract_routing_graph

    fixture = Path(__file__).parent / "fixtures/monaco-full/monaco.osm.pbf"
    monkeypatch.setenv("STREETZIM_NODE_LOC_DIR", str(tmp_path))
    monkeypatch.setenv("STREETZIM_ROUTING_WALKBIKE", "0")  # predates walk/bike
    graph = extract_routing_graph(str(fixture), str(tmp_path))
    # Produced independently from d5c32b6 using this checked-in Monaco PBF.
    assert hashlib.sha256(Path(graph).read_bytes()).hexdigest() == (
        "50d8ddfba81d055780f6fd8dcadd205e0b81e89a10673e8c3010344772c8635a")
