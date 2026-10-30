"""tools/langpack/viewer_base.py: the language versions carry this checkout's
viewer. A copy of the published base with the viewer patched into its slots
(same name, size and UUID) gives recipes that read the live base, carry the
new slots inline, get a new merged UUID and name the viewer in their file
name; a copy is reused while the base and the viewer stay the same."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tests.langpack.conftest import ROOT  # noqa: E402
from tools.langpack import merge_zim, recipe2, viewer_base  # noqa: E402

TOOL = ROOT / "tools" / "langpack" / "viewer_base.py"


def viewer(d: Path, code: str | None, text: str = "viewer 2") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    stamp = f"<script>var SZ_VIEWER_BUILD = '{code}';</script>" if code is not None else ""
    (d / "index.html").write_text(f"<html><body>map, {text}{stamp}</body></html>")
    (d / "places.html").write_text(f"<html><body>places, {text}</body></html>")
    (d / "routing-worker.js").write_text(f"/* routing, {text} */")
    return d


@pytest.fixture()
def vb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("LANGPACK_WORK", str(tmp_path / "work"))
    base = tmp_path / "maps" / "osm-testland-2026-10-13.zim"
    base.parent.mkdir()
    uuid = lpzims.write_base(base, slots=lpzims.VIEWER_V1)
    de = tmp_path / "packs" / "osm_testland_wiki-de_2026-10-13.zim"
    de.parent.mkdir()
    lpzims.write_pack(de, "de", uuid)
    return SimpleNamespace(d=tmp_path, base=base, uuid=uuid, de=de, out=tmp_path / "work" / "viewer-base" / base.stem)


def run(vb: SimpleNamespace, vdir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(TOOL), str(vb.base), str(vb.out), "--viewer-dir", str(vdir)],
                          capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))


def test_the_copy_is_the_base_with_the_new_viewer(vb: SimpleNamespace) -> None:
    before = vb.base.read_bytes()
    r = run(vb, viewer(vb.d / "v2", "0123abcd"))
    assert r.returncode == 0, r.stderr
    copy = Path(r.stdout.strip())
    assert copy == vb.out / vb.base.name
    assert vb.base.read_bytes() == before                       # the published base is never written
    assert copy.stat().st_size == vb.base.stat().st_size and lpzims.read_uuid(copy) == vb.uuid
    assert recipe2.viewer_slots(str(copy)) == recipe2.viewer_slots(str(vb.base))
    assert recipe2.zim_viewer_code(str(copy)) == "0123abcd" and recipe2.zim_viewer_code(str(vb.base)) == ""
    assert json.load(open(f"{copy}.viewer.json"))["code"] == "0123abcd"


def test_its_recipes_read_the_live_base_and_name_the_viewer(vb: SimpleNamespace, tmp_path: Path) -> None:
    copy = viewer_base.make(str(vb.base), str(vb.out), str(viewer(vb.d / "v2", "0123abcd")))
    old, _, _ = recipe2.build_recipe(str(vb.base), [str(vb.de)], "de", region="testland")
    new, _, _ = recipe2.build_recipe(copy, [str(vb.de)], "de", region="testland")
    assert old["file_name"].endswith("_2026-10-13")         # a viewer without a code: no suffix
    assert new["file_name"] == old["file_name"] + ".v0123abcd"
    assert new["base"]["file"] == old["base"]["file"] == vb.base.name and new["base"]["size"] == old["base"]["size"]
    assert new["base"]["sha256"] == old["base"]["sha256"] and new["base"]["mask"] == old["base"]["mask"]
    assert new["fingerprint"] == old["fingerprint"]             # a light gate
    assert new["uuid"] != old["uuid"] and new["md5"] != old["md5"]   # a new book for Kiwix
    # a visitor's build: the LIVE base and the packs, the slots from the recipe
    out = tmp_path / "built.zim"
    got = recipe2.materialize(new, str(vb.base), {"de": str(vb.de)}, out=str(out))
    assert got == new["md5"]
    assert hashlib.md5(out.read_bytes()).hexdigest() == new["file_md5"]
    assert recipe2.zim_viewer_code(str(out)) == "0123abcd"
    assert merge_zim.ZimReader(str(out)).header.uuid.hex() == new["uuid"].replace("-", "")


def test_a_copy_is_reused_until_the_viewer_or_the_base_changes(vb: SimpleNamespace) -> None:
    v2 = viewer(vb.d / "v2", "0123abcd")
    a = viewer_base.make(str(vb.base), str(vb.out), str(v2))
    ino = os.stat(a).st_ino
    assert viewer_base.make(str(vb.base), str(vb.out), str(v2)) == a and os.stat(a).st_ino == ino
    b = viewer_base.make(str(vb.base), str(vb.out), str(viewer(vb.d / "v3", "4567cdef", "viewer 3")))
    assert b == a and os.stat(b).st_ino != ino and recipe2.zim_viewer_code(b) == "4567cdef"
    ino = os.stat(b).st_ino
    os.utime(vb.base)                                         # the base file replaced (or touched): made again
    viewer_base.make(str(vb.base), str(vb.out), str(vb.d / "v3"))
    assert os.stat(b).st_ino != ino
    assert sorted(p.name for p in vb.out.iterdir()) == [vb.base.name, vb.base.name + ".viewer.json"]   # no leftovers


def test_a_base_without_slots_keeps_its_viewer(vb: SimpleNamespace) -> None:
    lpzims.write_base(vb.base)                                # built before viewer slots
    r = run(vb, viewer(vb.d / "v2", "0123abcd"))
    assert r.returncode == 3 and "viewer slots" in r.stderr and r.stdout == ""
    assert not (vb.out / vb.base.name).exists()


def test_a_viewer_without_its_code_is_refused(vb: SimpleNamespace) -> None:
    r = run(vb, viewer(vb.d / "v0", None))
    assert r.returncode == 1 and "SZ_VIEWER_BUILD" in r.stderr
    assert not (vb.out / vb.base.name).exists()


def test_the_bases_lineage_carries_over_to_the_copy(vb: SimpleNamespace) -> None:
    parent = "ab" * 16
    side = {"child_uuid": vb.uuid, "child_md5": hashlib.md5(vb.base.read_bytes()).hexdigest(),
            "child_file": vb.base.name, "parent_uuid": parent, "parents": []}
    Path(f"{vb.base}.parent.json").write_text(json.dumps(side))
    copy = viewer_base.make(str(vb.base), str(vb.out), str(viewer(vb.d / "v2", "0123abcd")))
    u = bytes.fromhex(vb.uuid)
    assert parent in merge_zim.base_lineage(str(vb.base), u)
    assert parent in merge_zim.base_lineage(copy, u)          # packs built for the parent still fit
    assert json.load(open(f"{copy}.parent.json"))["child_md5"] == hashlib.md5(Path(copy).read_bytes()).hexdigest()


def test_no_sidecar_when_the_bases_does_not_name_it(vb: SimpleNamespace) -> None:
    Path(f"{vb.base}.parent.json").write_text(json.dumps({"child_uuid": vb.uuid, "child_md5": "0" * 32,
                                                          "parent_uuid": "ab" * 16}))
    copy = viewer_base.make(str(vb.base), str(vb.out), str(viewer(vb.d / "v2", "0123abcd")))
    assert not os.path.exists(f"{copy}.parent.json")
    assert merge_zim.base_lineage(copy, bytes.fromhex(vb.uuid)) == {vb.uuid}


def fake_root(tmp: Path, body: str) -> Path:
    """A checkout whose cloud/patch_viewer_inplace.py is BODY (python; argv[1]
    is the copy): the real patcher runs first unless BODY says otherwise."""
    root = tmp / "fake-root"
    (root / "cloud").mkdir(parents=True)
    (root / "cloud" / "patch_viewer_inplace.py").write_text(
        "import subprocess, sys\n"
        f"REAL = {str(ROOT / 'cloud' / 'patch_viewer_inplace.py')!r}\n" + body)
    return root


@pytest.mark.parametrize("body,why", [
    ("pass\n", "has viewer (none), not 0123abcd"),                                    # nothing patched
    ("subprocess.run([sys.executable, REAL] + sys.argv[1:], check=True)\n"
     "open(sys.argv[1], 'ab').write(b'x')\n", "changed size or UUID"),                 # a byte more
    ("subprocess.run([sys.executable, REAL] + sys.argv[1:], check=True)\n"
     "f = open(sys.argv[1], 'r+b'); f.seek(8); f.write(bytes(16))\n", "changed size or UUID"),   # another UUID
    ("subprocess.run([sys.executable, REAL] + sys.argv[1:], check=True)\n"
     "b = open(sys.argv[1], 'rb').read(); i = b.index(b'SZVSLOT1:places.html')\n"
     "f = open(sys.argv[1], 'r+b'); f.seek(i); f.write(b'SZVSLOTX')\n", "other viewer slots"),   # a slot lost
    ("sys.exit(5)\n", "patch_viewer_inplace.py rc=5"),
])
def test_a_copy_that_is_not_the_base_with_the_viewer_is_refused(vb: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
                                                               body: str, why: str) -> None:
    monkeypatch.setattr(viewer_base, "ROOT", str(fake_root(vb.d, body)))
    with pytest.raises(viewer_base.ViewerBaseError, match=why.replace("(", r"\(").replace(")", r"\)")):
        viewer_base.make(str(vb.base), str(vb.out), str(viewer(vb.d / "v2", "0123abcd")))
    assert not vb.out.exists() or os.listdir(vb.out) == []                 # nothing half-made left


def test_a_copy_changed_since_is_made_again(vb: SimpleNamespace) -> None:
    v2 = viewer(vb.d / "v2", "0123abcd")
    a = viewer_base.make(str(vb.base), str(vb.out), str(v2))
    ino = os.stat(a).st_ino
    os.utime(a, (1, 1))                                       # the copy touched (or replaced) since
    assert viewer_base.make(str(vb.base), str(vb.out), str(v2)) == a and os.stat(a).st_ino != ino


def test_the_bases_sidecar_is_followed_on_a_reused_copy(vb: SimpleNamespace) -> None:
    v2 = viewer(vb.d / "v2", "0123abcd")
    copy = viewer_base.make(str(vb.base), str(vb.out), str(v2))
    ino, u, parent = os.stat(copy).st_ino, bytes.fromhex(vb.uuid), "ab" * 16
    assert not os.path.exists(f"{copy}.parent.json")
    Path(f"{vb.base}.parent.json").write_text(json.dumps(
        {"child_uuid": vb.uuid, "child_md5": hashlib.md5(vb.base.read_bytes()).hexdigest(), "parent_uuid": parent}))
    assert viewer_base.make(str(vb.base), str(vb.out), str(v2)) == copy and os.stat(copy).st_ino == ino   # reused
    assert parent in merge_zim.base_lineage(copy, u)
    os.remove(f"{vb.base}.parent.json")
    viewer_base.make(str(vb.base), str(vb.out), str(v2))
    assert not os.path.exists(f"{copy}.parent.json") and merge_zim.base_lineage(copy, u) == {vb.uuid}


def test_copies_of_bases_that_are_gone_and_dead_runs_leftovers_go(vb: SimpleNamespace, tmp_path: Path) -> None:
    v2 = viewer(vb.d / "v2", "0123abcd")
    old = tmp_path / "maps" / "osm-testland-2026-09-01.zim"         # last round's base, deleted since
    old.write_bytes(vb.base.read_bytes())
    kept = tmp_path / "maps" / "osm-other-2026-10-13.zim"            # another region's base, still there
    kept.write_bytes(vb.base.read_bytes())
    root = vb.out.parent
    for b in (old, kept):
        viewer_base.make(str(b), str(root / b.stem), str(v2))
    os.remove(old)
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    junk = root / kept.stem / f".{kept.name}.{int(dead.stdout)}.tmp"          # a run that died making a copy
    live = root / kept.stem / f".{kept.name}.{os.getpid()}.tmp"                # one still making it
    junk.write_bytes(b"x"); live.write_bytes(b"x")
    viewer_base.make(str(vb.base), str(vb.out), str(v2))
    assert not (root / old.stem).exists()
    assert (root / kept.stem / kept.name).exists() and live.exists() and not junk.exists()
    assert (vb.out / vb.base.name).exists()
