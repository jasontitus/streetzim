"""cloud/tile_order.py, the cluster_break manifest record, and address
stripping helpers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud.tile_order import hilbert_index, order_tiles, tile_sort_key  # noqa: E402


def test_hilbert_is_bijection_and_local():
    for z in (1, 2, 3, 5):
        n = 1 << z
        idx = {hilbert_index(z, x, y) for x in range(n) for y in range(n)}
        assert idx == set(range(n * n))
    # consecutive curve positions are grid neighbours
    z, n = 4, 16
    pos = {hilbert_index(z, x, y): (x, y) for x in range(n) for y in range(n)}
    for d in range(n * n - 1):
        (x0, y0), (x1, y1) = pos[d], pos[d + 1]
        assert abs(x0 - x1) + abs(y0 - y1) == 1


def test_order_tiles_zoom_major_and_source_passthrough():
    coords = [(14, 5, 5), (13, 1, 1), (14, 0, 0), (12, 3, 3), (14, 5, 4)]
    out = list(order_tiles(coords, "zoom-hilbert"))
    assert [z for z, _, _ in out] == [12, 13, 14, 14, 14]
    z14 = [t for t in out if t[0] == 14]
    assert z14 == sorted(z14, key=lambda t: hilbert_index(14, t[1], t[2]))
    assert list(order_tiles(coords, "source")) == coords
    assert tile_sort_key("zoom-xy", 3, 2, 1) == (2 << 3) | 1
    with pytest.raises(ValueError):
        tile_sort_key("spiral", 1, 0, 0)


def test_manifest_cluster_break_record(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "0")
    from cloud.manifest_writer import ManifestCreator
    mc = ManifestCreator(str(tmp_path / "out.zim"))
    mc._write_record(mc._config)
    mc.cluster_break()
    mc.cluster_break(2 * 1024 * 1024)
    mc._mf.flush()
    recs = [json.loads(l) for l in open(mc._manifest_path, encoding="utf-8") if l.strip()]
    assert recs[1] == {"kind": "cluster_break"}
    assert recs[2] == {"kind": "cluster_break", "cluster_size_target": 2097152}
    mc._mf.close()


def test_strip_address_records():
    from cloud.derive_zim import _is_address_leaf_name, strip_address_records
    leaf = json.dumps([{"n": "Main St 12", "t": "addr"}, {"n": "Main St", "t": "street"},
                       {"n": "Old 7", "type": "addr"}, {"n": "Cafe", "t": "poi"}]).encode()
    new, kept, dropped = strip_address_records(leaf)
    assert (kept, dropped) == (2, 2)
    assert [r["n"] for r in json.loads(new)] == ["Main St", "Cafe"]
    same, kept, dropped = strip_address_records(json.dumps([{"n": "x", "t": "poi"}]).encode())
    assert dropped == 0 and json.loads(same) == [{"n": "x", "t": "poi"}]
    assert _is_address_leaf_name("10~0~0~a")
    assert _is_address_leaf_name("ab~a")
    assert not _is_address_leaf_name("10~0~p")
    assert not _is_address_leaf_name("a")          # a legacy prefix named 'a'
    assert not _is_address_leaf_name("ab-3")
