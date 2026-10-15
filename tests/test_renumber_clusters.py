"""cloud/renumber_clusters.py: a repaired copy reads the same, with clusters in file order."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud import renumber_clusters as rc  # noqa: E402
from streetzim import pack as p  # noqa: E402

libzim = pytest.importorskip("libzim.reader")


def _packed(tmp_path):
    records = [{"kind": "config", "compression": "zstd", "cluster_size_target": 64},
               {"kind": "metadata", "name": "Title", "value": "t"}]
    for i in range(12):
        records.append({"kind": "item", "path": f"p{i}", "mime": "text/html" if i % 3 else "text/plain",
                        "content": f"body {i} " * (i + 3), "compress": i % 4 != 0})
    records.append({"kind": "redirect", "path": "alias", "target": "p5"})
    manifest = tmp_path / "manifest"
    manifest.write_text("".join(json.dumps(r) + "\n" for r in records))
    out = tmp_path / "good.zim"
    p.pack(manifest, out, threads=1)
    return out


def _offsets(path):
    data = path.read_bytes()
    h = rc.HEADER.unpack(data[:80])
    return list(struct.unpack_from(f"<{h[5]}Q", data, h[8])), h[3]


def _contents(path):
    a = libzim.Archive(path)
    assert a.check()
    seen = {}
    for i in range(a.all_entry_count):
        e = a._get_entry_by_id(i)
        if e.is_redirect:
            seen[e.path] = ("->", e.get_redirect_entry().path)
        else:
            item = e.get_item()
            seen[e.path] = (e.title, item.mimetype, bytes(item.content))
    seen["@main"] = a.main_entry.path if a.has_main_entry else None
    return seen


def test_scrambled_then_repaired_reads_the_same(tmp_path):
    good = _packed(tmp_path)
    offsets, good_uuid = _offsets(good)
    n = len(offsets)
    assert n >= 4 and offsets == sorted(offsets)
    want = _contents(good)

    # The bug's shape: numbers not in file order (here: reversed).
    scrambled = tmp_path / "scrambled.zim"
    rc.renumber(str(good), str(scrambled), order=list(range(n))[::-1])
    s_offsets, _ = _offsets(scrambled)
    assert s_offsets == offsets[::-1]
    assert _contents(scrambled) == want
    assert rc.main([str(scrambled), "--check"]) == 1

    fixed = tmp_path / "fixed.zim"
    summary = rc.renumber(str(scrambled), str(fixed))
    f_offsets, f_uuid = _offsets(fixed)
    assert f_offsets == offsets
    assert summary["renumbered"] == n - (n % 2) and summary["dirents_changed"] > 0
    assert _contents(fixed) == want
    assert rc.main([str(fixed), "--check"]) == 0
    assert f_uuid not in (good_uuid, _offsets(scrambled)[1])
    assert not Path(str(fixed) + ".tmp").exists()


def test_refuses_its_own_source_and_a_truncated_file(tmp_path):
    good = _packed(tmp_path)
    with pytest.raises(SystemExit):
        rc.main([str(good), str(good)])
    cut = tmp_path / "cut.zim"
    cut.write_bytes(good.read_bytes()[:-1])
    with pytest.raises(rc.RenumberError, match="checksum"):
        rc.renumber(str(cut), str(tmp_path / "out.zim"))
    assert not (tmp_path / "out.zim").exists()


def test_redirect_dirents_keep_their_target(tmp_path):
    good = _packed(tmp_path)
    n = len(_offsets(good)[0])
    out = tmp_path / "r.zim"
    rc.renumber(str(good), str(out), order=list(range(n))[::-1])
    a = libzim.Archive(out)
    assert a.get_entry_by_path("alias").get_redirect_entry().path == "p5"


def test_patches_straddling_chunk_boundaries(tmp_path, monkeypatch):
    good = _packed(tmp_path)
    n = len(_offsets(good)[0])
    order = list(range(n))[::-1]
    whole = tmp_path / "whole.zim"
    rc.renumber(str(good), str(whole), order=order)
    for chunk in (1, 7, 64, 4093):
        monkeypatch.setattr(rc, "CHUNK", chunk)
        out = tmp_path / f"c{chunk}.zim"
        rc.renumber(str(good), str(out), order=order)
        assert out.read_bytes() == whole.read_bytes(), chunk


def test_check_first_looks_at_cluster_zero_only(tmp_path):
    good = _packed(tmp_path)
    n = len(_offsets(good)[0])
    swapped = tmp_path / "swapped.zim"      # 0 first, the rest out of order
    rc.renumber(str(good), str(swapped), order=[0] + list(range(1, n))[::-1])
    assert rc.main([str(swapped), "--check"]) == 1
    assert rc.main([str(swapped), "--check-first"]) == 0
    late = tmp_path / "late.zim"            # 0 last in the file: the bug
    rc.renumber(str(good), str(late), order=list(range(n))[::-1])
    assert rc.main([str(late), "--check-first"]) == 1
