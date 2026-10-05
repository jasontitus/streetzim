"""Spatial memory changes must preserve every byte of the established writer."""
from __future__ import annotations

import gc
import struct
import weakref
from pathlib import Path

import numpy as np
import pytest

from streetzim.routing import reader, spatial
from tests.fixtures.spatial_build_reference import build_spatial as reference
from tests.test_szrg_v5_split import _pack_v4_inline
from tests.v4_to_v5_convert import v4_to_v5_bufs


def _graph(seed, n):
    rng = np.random.default_rng(seed)
    # Repeated cells, negative floor boundaries, poles and the antimeridian.
    coords = [(-900_000_000, -1_800_000_000), (900_000_000, 1_800_000_000),
              (-1, -1), (0, 0), (999_999, 1_000_000),
              (1_000_000, 999_999), (-1_000_001, -1_000_000)]
    nodes = [coords[int(i)] for i in rng.integers(0, len(coords), size=n)]
    names = ["", "北京", "Ångström"]
    geoms = [struct.pack("<ii", i, -i) + b"\x01\x02" * i for i in range(7)]
    offsets = np.cumsum([0] + [len(g) for g in geoms], dtype="<u4").tobytes()
    edges = []
    for source in range(n):
        for _ in range(int(rng.integers(0, 5))):
            gi = int(rng.integers(0, 10))
            edges.append((source, int(rng.integers(0, n)), 1234, 30,
                          gi if gi < len(geoms) else 0xFFFFFFFF,
                          int(rng.integers(0, len(names)))))
    data = bytearray(_pack_v4_inline(nodes, edges, names, len(geoms),
                                    sum(map(len, geoms)), offsets, b"".join(geoms)))
    if edges:
        view = np.frombuffer(data, dtype="<u4", offset=32 + 8*n + 4*(n+1),
                             count=len(edges)*5).reshape(-1, 5)
        view[:, 4] = rng.integers(0, 2048, size=len(edges))
    return bytes(data)


@pytest.mark.parametrize("n", [0, 1, 2, 67, 257])
@pytest.mark.parametrize("seed", [0, 1, 29])
@pytest.mark.parametrize("scale", [1, 10, 2000])
def test_exact_reference_bytes_across_blocks(tmp_path, monkeypatch, n, seed, scale):
    data = _graph(seed, n)
    want_index, want_cells, want_meta = reference(reader.parse_szrg_bytes(data), cell_scale=scale)
    # Many block transitions, both inside a cell and between cells.
    monkeypatch.setattr(spatial, "_NODE_BLOCK", 7)
    path = tmp_path / "graph.bin"
    path.write_bytes(data)
    for mapped in (False, True):
        g = reader.load_from_file(path, mapped=mapped)
        index, cells, meta = spatial.build_spatial(g, cell_scale=scale)
        assert (index, cells, meta) == (want_index, want_cells, want_meta)
        dest = tmp_path / str(mapped)
        index, cells, meta = spatial.build_spatial(g, cell_scale=scale, output_dir=dest)
        assert index == want_index == (dest / "graph-cells-index.bin").read_bytes()
        assert {cid: Path(p).read_bytes() for cid, p in cells.items()} == want_cells
        assert meta == want_meta
        assert path.read_bytes() == data


@pytest.mark.parametrize("explicit_companion", [False, True])
def test_mapped_v5_companion_has_identical_cells(tmp_path, explicit_companion):
    v4 = _graph(3, 95)
    main, geoms = v4_to_v5_bufs(v4)
    path, companion = tmp_path / "routing-graph.bin", tmp_path / "routing-graph-geoms.bin"
    path.write_bytes(main)
    companion.write_bytes(geoms)
    g = reader.load_from_file(path, companion if explicit_companion else None, mapped=True)
    assert isinstance(g.geom_blob, memoryview)
    assert g.geom_blob.readonly
    assert g.get_name(1) == "北京"
    assert g.get_name(2) == "Ångström"
    assert spatial.build_spatial(g) == reference(reader.parse_szrg_bytes(v4))


def test_mapped_views_are_read_only_and_own_lifetime(tmp_path):
    path = tmp_path / "graph.bin"
    data = _graph(2, 20)
    path.write_bytes(data)
    g = reader.load_from_file(path, mapped=True)
    assert isinstance(g.geom_blob, memoryview)
    assert not g.nodes_scaled.flags.writeable
    assert not g.edges.flags.writeable
    with pytest.raises(ValueError, match="read-only"):
        g.edges[0] = 0
    with pytest.raises(TypeError):
        g.geom_blob[0] = 0
    mapping = weakref.ref(g.nodes_scaled.base.obj)
    borrowed = g.geom_blob
    del g
    gc.collect()
    assert mapping() is not None
    assert bytes(borrowed) == reader.parse_szrg_bytes(data).geom_blob
    del borrowed
    gc.collect()
    assert mapping() is None


def test_mapping_released_after_parse_failure(tmp_path, monkeypatch):
    path = tmp_path / "graph.bin"
    path.write_bytes(_graph(2, 20)[:40])
    refs = []
    real = reader._file_buffer

    def record(*args):
        buf = real(*args)
        refs.append(weakref.ref(buf))
        return buf

    monkeypatch.setattr(reader, "_file_buffer", record)

    def fail():
        try:
            reader.load_from_file(path, mapped=True)
        except ValueError:
            return
        pytest.fail("truncated arrays were accepted")

    fail()
    gc.collect()
    assert refs and all(ref() is None for ref in refs)


def test_write_failure_does_not_mask_error_or_retain_mapping(tmp_path, monkeypatch):
    path = tmp_path / "graph.bin"
    path.write_bytes(_graph(2, 20))
    refs = []
    real = reader._file_buffer

    def record(*args):
        buf = real(*args)
        refs.append(weakref.ref(buf))
        return buf

    monkeypatch.setattr(reader, "_file_buffer", record)
    bad_output = tmp_path / "not-a-directory"
    bad_output.write_bytes(b"keep")

    def fail():
        try:
            spatial.build_spatial(reader.load_from_file(path, mapped=True), output_dir=bad_output)
        except FileExistsError:
            return
        pytest.fail("output error was swallowed")

    fail()
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert bad_output.read_bytes() == b"keep"


@pytest.mark.parametrize("failure", ["section", "geometry", "close"])
def test_partial_cell_write_failure_releases_mapping(tmp_path, monkeypatch, failure):
    path = tmp_path / "graph.bin"
    path.write_bytes(_graph(2, 20))
    refs = []
    real_buffer, real_open = reader._file_buffer, Path.open

    def record(*args):
        buf = real_buffer(*args)
        refs.append(weakref.ref(buf))
        return buf

    class BrokenWriter:
        def __init__(self, stream):
            self.stream, self.calls = stream, 0

        def __enter__(self):
            return self

        def write(self, data):
            self.calls += 1
            self.stream.write(data[:1])
            if self.calls == {"section": 2, "geometry": 6}.get(failure):
                raise OSError("simulated ENOSPC")
            self.stream.write(data[1:])

        def __exit__(self, *args):
            self.stream.close()
            if failure == "close":
                raise OSError("simulated ENOSPC")

    def open_file(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        return BrokenWriter(stream) if path.name.startswith("graph-cell-") else stream

    monkeypatch.setattr(reader, "_file_buffer", record)
    monkeypatch.setattr(Path, "open", open_file)

    def fail():
        try:
            spatial.build_spatial(reader.load_from_file(path, mapped=True),
                                  output_dir=tmp_path / "out")
        except OSError as exc:
            assert str(exc) == "simulated ENOSPC"
            return
        pytest.fail("write error was swallowed")

    fail()
    gc.collect()
    assert refs and all(ref() is None for ref in refs)
    assert not (tmp_path / "out/graph-cells-index.bin").exists()


def test_mapped_v5_mismatch_releases_both_files(tmp_path, monkeypatch):
    main, geom = v4_to_v5_bufs(_graph(2, 20))
    geom = bytearray(geom)
    struct.pack_into("<I", geom, 8, 8)  # main declares seven geometries
    path, companion = tmp_path / "graph.bin", tmp_path / "graph-geoms.bin"
    path.write_bytes(main)
    companion.write_bytes(geom)
    refs = []
    real = reader._file_buffer

    def record(*args):
        buf = real(*args)
        refs.append(weakref.ref(buf))
        return buf

    monkeypatch.setattr(reader, "_file_buffer", record)

    def fail():
        try:
            reader.load_from_file(path, mapped=True)
        except ValueError as exc:
            assert "does not match" in str(exc)
            return
        pytest.fail("companion count mismatch accepted")

    fail()
    gc.collect()
    assert len(refs) == 2 and all(ref() is None for ref in refs)


def test_geometry_ranges_outside_blob_are_rejected():
    data = bytearray(_graph(2, 20))
    g = reader.parse_szrg_bytes(data)
    g.geom_offsets[:] = len(g.geom_blob) + 1
    with pytest.raises(ValueError, match="geometry offsets outside source blob"):
        spatial.build_spatial(g)
