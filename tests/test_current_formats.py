"""The builder writes only the current routing formats (docs/formats.md):
SZRG v4 for plain --routing, SZCI v3 + SZRC v2 for --spatial-chunk-scale.
Legacy versions are read, never written; the retired writer flags fail
clearly instead of silently producing something else."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

osmium = pytest.importorskip("osmium")


def _tiny_pbf(path: Path) -> None:
    """A named residential street crossing a primary road, near Monaco."""
    nodes = {1: (7.420, 43.730), 2: (7.421, 43.731), 3: (7.422, 43.732),
             4: (7.420, 43.732), 5: (7.422, 43.730)}
    w = osmium.SimpleWriter(str(path))
    try:
        for nid, (lon, lat) in nodes.items():
            w.add_node(osmium.osm.mutable.Node(id=nid, location=(lon, lat), version=1))
        w.add_way(osmium.osm.mutable.Way(id=10, nodes=[1, 2, 3], version=1, tags={
            "highway": "residential", "name": "Rue Grimaldi"}))
        w.add_way(osmium.osm.mutable.Way(id=11, nodes=[4, 2, 5], version=1, tags={
            "highway": "primary", "name": "Boulevard Albert Ier"}))
    finally:
        w.close()


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    from streetzim.routing.build import extract_routing_graph
    d = tmp_path_factory.mktemp("rt")
    pbf = d / "tiny.osm.pbf"
    _tiny_pbf(pbf)
    out = extract_routing_graph(str(pbf), str(d))
    return d, out


def test_routing_graph_is_szrg_v4_and_nothing_else(graph):
    d, out = graph
    assert isinstance(out, str) and out.endswith("routing-graph.bin")
    buf = Path(out).read_bytes()
    assert buf[:4] == b"SZRG"
    assert struct.unpack_from("<I", buf, 4)[0] == 4
    assert not list(d.glob("*geoms*")), "no SZGM companion is written any more"
    from streetzim.routing.reader import load_from_file
    g = load_from_file(out)
    assert g.version == 4 and g.num_edges > 0 and g.has_geoms


def test_spatial_layout_is_szci_v3_with_szrc_v2_cells(graph, tmp_path):
    from streetzim.routing.reader import load_from_file
    from streetzim.routing.spatial import build_spatial
    idx, cells, _meta = build_spatial(load_from_file(graph[1]), cell_scale=10,
                                      output_dir=tmp_path)
    idx = idx if isinstance(idx, (bytes, bytearray)) else (tmp_path / "graph-cells-index.bin").read_bytes()
    assert idx[:4] == b"SZCI" and struct.unpack_from("<I", idx, 4)[0] == 3
    cell_files = sorted(tmp_path.glob("graph-cell-*.bin"))
    assert cell_files
    for f in cell_files:
        b = f.read_bytes()
        assert b[:4] == b"SZRC" and struct.unpack_from("<I", b, 4)[0] == 2, f.name


@pytest.mark.parametrize("module,argv", [
    ("create_osm_zim", ["--split-graph", "--area", "monaco"]),
    ("cloud.repackage_zim", ["--split-graph", "in.zim", "out.zim"]),
])
def test_retired_split_graph_flag_fails_clearly(module, argv, monkeypatch):
    import importlib
    mod = importlib.import_module(module)
    monkeypatch.setattr(sys, "argv", [module] + argv)
    with pytest.raises(SystemExit) as e:
        mod.main(argv) if module == "create_osm_zim" else mod.main()
    assert "retired" in str(e.value)
