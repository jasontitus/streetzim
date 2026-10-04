"""The ZIM step's spatial routing cells: normally built right after the
routing step (zim_writer.prepare_spatial_cells, while the build holds
little), else in a child process at the ZIM step. Either way the ZIM gets
exactly what build_spatial makes: the index and every cell."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("osmium")

from streetzim import isolate, zim_writer  # noqa: E402
from streetzim.routing.build import extract_routing_graph  # noqa: E402
from streetzim.routing.reader import load_from_file  # noqa: E402
from streetzim.routing.spatial import build_spatial  # noqa: E402
from tests.test_routing_build_memory import _network  # noqa: E402

# Fine cells, so the small test network spans many.
SCALE = 2000


class _Item:
    def __init__(self, path, title, mime, content, compress=True):
        self.path, self.content = path, content


class _Creator:
    def __init__(self):
        self.items = []

    def add_item(self, item):
        self.items.append(item)


@pytest.fixture
def graph(tmp_path):
    out = tmp_path / "rt"
    out.mkdir()
    return extract_routing_graph(str(_network(tmp_path / "net.osm.pbf", 5)), str(out))


@pytest.fixture
def child_calls(monkeypatch):
    calls = []
    real = isolate.run_in_child

    def recording(fn, *a, **k):
        calls.append(fn.__name__)
        return real(fn, *a, **k)

    monkeypatch.setattr(isolate, "run_in_child", recording)
    return calls


def _zim_items(graph):
    creator = _Creator()
    zim_writer._add_routing_graph(creator, _Item, routing_graph_path=graph,
                                  routing_graph_chunk_mb=0, spatial_chunk_scale=SCALE)
    return {i.path: Path(i.content).read_bytes() for i in creator.items}


def _expected(graph):
    index, cells, _ = build_spatial(load_from_file(graph), cell_scale=SCALE)
    want = {"routing-data/graph-cells-index.bin": index}
    want.update({f"routing-data/graph-cell-{cid:05d}.bin": b for cid, b in cells.items()})
    return want


def test_cells_built_at_the_zim_step_are_build_spatials(graph, child_calls):
    want = _expected(graph)
    assert len(want) > 10
    assert _zim_items(graph) == want
    assert child_calls == ["_spatial_cell_files"]


def test_cells_prepared_after_routing_are_used_as_they_are(graph, child_calls):
    want = _expected(graph)
    isolate.run_in_child(zim_writer.prepare_spatial_cells, graph, SCALE)
    child_calls.clear()
    assert _zim_items(graph) == want
    assert child_calls == []


def test_prepared_cells_for_another_graph_or_scale_are_not_used(graph, child_calls):
    zim_writer.prepare_spatial_cells(graph, SCALE + 1)
    assert zim_writer._prepared_spatial_cells(graph, SCALE) is None
    zim_writer.prepare_spatial_cells(graph, SCALE)
    assert zim_writer._prepared_spatial_cells(graph, SCALE) is not None
    st = os.stat(graph)
    os.utime(graph, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
    assert zim_writer._prepared_spatial_cells(graph, SCALE) is None
    assert _zim_items(graph) == _expected(graph)
    assert child_calls == ["_spatial_cell_files"]


def test_the_routing_step_prepares_the_cells(graph, tmp_path, monkeypatch):
    """create_osm_zim's routing step builds the cells for the ZIM step."""
    import argparse

    import create_osm_zim as c
    calls = []

    def fake_child(fn, *a, **k):
        calls.append(fn.__name__)
        return graph if fn.__name__ == "extract_routing_graph" else fn(*a, **k)

    monkeypatch.setattr(c, "run_in_child", fake_child)
    args = argparse.Namespace(pbf="x.pbf", spatial_chunk_scale=SCALE)
    got = c._build_routing(args=args, bbox_str=None, include_routing=True,
                           include_wikidata=False, pbf_path="x.pbf", tmpdir=str(tmp_path),
                           total_steps=6, work_pbf=None, work_pbf_cut=False)
    assert got == graph
    assert calls == ["extract_routing_graph", "prepare_spatial_cells"]
    assert zim_writer._prepared_spatial_cells(graph, SCALE) is not None
