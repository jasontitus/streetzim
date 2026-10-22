"""The parent-UUID sidecar (<base>.parent.json) that lets a pack built
against a base keep fitting the same base after a viewer swap (new UUID).
It counts only when it names THIS file: child_uuid == the header UUID and
child_md5 == the file's MD5; the merger, verify_merge.py and pack_gate.py
all check both."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tools.langpack import merge_zim  # noqa: E402
from tools.langpack.merge_zim import MergeError, base_lineage  # noqa: E402


def _uuid(p: Path) -> bytes:
    return bytes.fromhex(lpzims.read_uuid(p))


def test_valid_sidecar_adds_the_parent(lp: SimpleNamespace, tmp_path: Path) -> None:
    child = tmp_path / "child.zim"
    cu = lpzims.child_base(lp.base, child)
    assert base_lineage(str(child), bytes.fromhex(cu)) == {cu, lp.base_uuid}
    assert base_lineage(str(child), bytes.fromhex(cu), check_md5=False) == {cu, lp.base_uuid}


def test_sidecar_beside_another_file_is_ignored(lp: SimpleNamespace, tmp_path: Path) -> None:
    child = tmp_path / "child.zim"
    lpzims.child_base(lp.base, child)
    other = tmp_path / "other.zim"
    lpzims.rewrite_uuid(lp.base, other, b"\x11" * 16)
    shutil.copy(str(child) + ".parent.json", str(other) + ".parent.json")
    assert base_lineage(str(other), _uuid(other)) == {_uuid(other).hex()}
    assert base_lineage(str(other), _uuid(other), check_md5=False) == {_uuid(other).hex()}
    # even with the right MD5 for this file, the child_uuid must be this file's
    side = json.loads(Path(str(child) + ".parent.json").read_text())
    Path(str(other) + ".parent.json").write_text(json.dumps(dict(side, child_md5=lpzims.md5_of(other))))
    assert base_lineage(str(other), _uuid(other)) == {_uuid(other).hex()}


def test_tampered_md5_or_modified_file_is_ignored(lp: SimpleNamespace, tmp_path: Path) -> None:
    child = tmp_path / "child.zim"
    cu = lpzims.child_base(lp.base, child, child_md5="0" * 32)
    assert base_lineage(str(child), bytes.fromhex(cu)) == {cu}                    # default: MD5 checked
    assert lp.base_uuid in base_lineage(str(child), bytes.fromhex(cu), check_md5=False)
    mod = tmp_path / "mod.zim"
    mu = lpzims.child_base(lp.base, mod)
    data = bytearray(mod.read_bytes())
    data[len(data) // 2] ^= 1                                                     # UUID unchanged
    mod.write_bytes(bytes(data))
    assert base_lineage(str(mod), bytes.fromhex(mu)) == {mu}


def test_bad_sidecars_add_nothing(lp: SimpleNamespace, tmp_path: Path) -> None:
    child = tmp_path / "child.zim"
    cu = lpzims.child_base(lp.base, child, parents=["not-a-uuid", "b" * 32])
    assert base_lineage(str(child), bytes.fromhex(cu)) == {cu, lp.base_uuid, "b" * 32}
    Path(str(child) + ".parent.json").write_text("{not json")
    assert base_lineage(str(child), bytes.fromhex(cu)) == {cu}


@pytest.mark.parametrize("sidecar,fits", [("valid", True), ("tampered_md5", False), ("modified_file", False),
                                          ("none", False)])
def test_merge_onto_a_swapped_base(sidecar: str, fits: bool, lp: SimpleNamespace, tmp_path: Path) -> None:
    """A pack built against the parent merges onto the child only through a
    sidecar that checks out (review r3: the merger checks child_md5 too)."""
    child = tmp_path / "child.zim"
    if sidecar == "valid":
        lpzims.child_base(lp.base, child)
    elif sidecar == "tampered_md5":
        lpzims.child_base(lp.base, child, child_md5="0" * 32)
    elif sidecar == "none":
        lpzims.child_base(lp.base, child, sidecar=False)
    else:
        lpzims.child_base(lp.base, child)
        data = bytearray(child.read_bytes())
        data[len(data) // 2] ^= 1
        child.write_bytes(bytes(data))
    if fits:
        plan = merge_zim.build_plan(str(child), [str(lp.packs["th"])], "th")
        assert plan.packs[0].base_uuid == lp.base_uuid
    else:
        with pytest.raises(MergeError, match="base_uuid"):
            merge_zim.build_plan(str(child), [str(lp.packs["th"])], "th")


def test_merged_file_from_a_swapped_base_verifies(lp: SimpleNamespace, tmp_path: Path) -> None:
    from tests.langpack.conftest import tool
    child = tmp_path / "osm-testland-b.zim"
    lpzims.child_base(lp.base, child)
    out = tmp_path / "m.zim"
    merge_zim.merge(str(child), [str(lp.packs["fr"])], str(out), "fr")
    r = tool("verify_merge.py", out, child, lp.packs["fr"], "--lang", "fr")
    assert r.returncode == 0, r.stdout[-2000:]
    assert "every pack.json base_uuid is this base or a recorded parent (S4)" in r.stdout
    side = json.loads(Path(str(child) + ".parent.json").read_text())
    assert side["child_uuid"] == lpzims.read_uuid(child)
