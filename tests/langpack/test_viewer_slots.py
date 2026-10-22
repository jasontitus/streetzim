"""Plan review B2: a base patched in place (cloud/patch_viewer_inplace.py:
new viewer slots, same UUID, same everything else) must never give a merged
file an old UUID, and needs only new recipes: they carry the viewer slots
inline, hash the base with the slots zeroed, and keep their fingerprint, so
a visitor's copy one viewer patch old still builds the new file."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tests.langpack.conftest import ROOT, tool  # noqa: E402
from tools.langpack import merge_zim, recipe2  # noqa: E402

V2 = {"index.html": b"<html><body>map, viewer 2: a longer one</body></html>",
      "places.html": b"<html><body>places, viewer 2</body></html>",
      "routing-worker.js": b"/* routing, viewer 2 */"}


def patch(src: Path, dst: Path, viewer: dict[str, bytes], tmp: Path) -> None:
    shutil.copy(src, dst)
    vd = tmp / f"viewer-{dst.stem}"
    vd.mkdir()
    for name, data in viewer.items():
        (vd / name).write_bytes(data)
    r = subprocess.run([sys.executable, str(ROOT / "cloud" / "patch_viewer_inplace.py"), str(dst), "--viewer-dir", str(vd)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.fixture(scope="module")
def sl(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    d = tmp_path_factory.mktemp("slots")
    a = d / "osm-testland-2026-10-13.zim"
    uuid = lpzims.write_base(a, slots=lpzims.VIEWER_V1)
    de = d / "osm_testland_wiki-de_2026-10-13.zim"
    lpzims.write_pack(de, "de", uuid)
    b = d / "patched" / a.name                    # the same base, its viewer patched in place
    b.parent.mkdir()
    patch(a, b, V2, d)
    assert lpzims.read_uuid(b) == uuid and a.stat().st_size == b.stat().st_size
    assert a.read_bytes() != b.read_bytes()
    return SimpleNamespace(d=d, a=a, b=b, de=de)


def test_the_merged_uuid_follows_the_bases_bytes(sl: SimpleNamespace) -> None:
    pa = merge_zim.build_plan(str(sl.a), [str(sl.de)], "de", region="testland")
    pb = merge_zim.build_plan(str(sl.b), [str(sl.de)], "de", region="testland")
    pa2 = merge_zim.build_plan(str(sl.a), [str(sl.de)], "de", region="testland")
    assert pa.header.uuid != pb.header.uuid           # same base UUID, new viewer: a new merged UUID
    assert pa.header.uuid == pa2.header.uuid          # the same inputs: the same file


def test_slots_travel_inline_and_the_base_is_hashed_without_them(sl: SimpleNamespace) -> None:
    ra, _, sa = recipe2.build_recipe(str(sl.a), [str(sl.de)], "de", region="testland")
    rb, _, sb = recipe2.build_recipe(str(sl.b), [str(sl.de)], "de", region="testland")
    names = [x["name"] for x in rb["base"]["slots"]]
    assert sorted(names) == ["index.html", "places.html", "routing-worker.js"]
    assert rb["base"]["mask"][:3] == [[x["off"], x["len"]] for x in rb["base"]["slots"]]
    assert rb["base"]["mask"][3] == [sl.b.stat().st_size - 16, 16] and rb["base"]["mask"] == ra["base"]["mask"]
    assert sb["slot_bytes"] == sum(x["len"] for x in rb["base"]["slots"]) == 1024 * 1024 + 256 * 1024 + 128 * 1024
    assert not any(s["src"] == "base" and s["off"] < o + n and o < s["off"] + s["len"]
                   for s in rb["segments"] for o, n in rb["base"]["mask"])
    assert ra["base"]["sha256"] == rb["base"]["sha256"]              # masked: the patch does not show
    assert recipe2.chunk_hashes(str(sl.a)) != recipe2.chunk_hashes(str(sl.b))
    assert ra["fingerprint"] == rb["fingerprint"] and ra["md5"] != rb["md5"] and ra["uuid"] != rb["uuid"]


def test_an_old_copy_builds_the_new_file(sl: SimpleNamespace, tmp_path: Path) -> None:
    rb, plan, _ = recipe2.build_recipe(str(sl.b), [str(sl.de)], "de", region="testland")
    merged = tmp_path / "m.zim"
    merge_zim.merge(str(sl.b), [str(sl.de)], str(merged), "de", region="testland")
    out = tmp_path / "from-old.zim"
    recipe2.materialize(rb, str(sl.a), {"de": str(sl.de)}, str(out))      # the visitor's copy: viewer 1
    assert out.read_bytes() == merged.read_bytes()
    from libzim.reader import Archive
    assert bytes(Archive(out).get_entry_by_path("index.html").get_item().content).startswith(V2["index.html"])
    # anything else changed in the copy fails its chunk
    bad = tmp_path / "bad.zim"
    data = bytearray(sl.a.read_bytes())
    data[rb["base"]["range"][1] - 100] ^= 0xFF                             # in the base's dirents
    bad.write_bytes(bytes(data))
    with pytest.raises(merge_zim.MergeError, match="SHA-256"):
        recipe2.materialize(rb, str(bad), {"de": str(sl.de)}, str(tmp_path / "x.zim"))


def test_a_recipe_reading_a_slot_from_the_base_is_refused(sl: SimpleNamespace, tmp_path: Path) -> None:
    rb, _, _ = recipe2.build_recipe(str(sl.b), [str(sl.de)], "de", region="testland")
    off, n = rb["base"]["mask"][0]
    bad = json.loads(json.dumps(rb))
    bad["segments"].insert(1, {"src": "base", "off": off, "len": 16})
    with pytest.raises(merge_zim.MergeError, match="masked viewer slot"):
        recipe2.materialize(bad, str(sl.b), {"de": str(sl.de)}, str(tmp_path / "x.zim"), trust_md5=False)


def test_verify_and_site_check_accept_the_patched_base(sl: SimpleNamespace, tmp_path: Path) -> None:
    merged = tmp_path / "m.zim"
    merge_zim.merge(str(sl.b), [str(sl.de)], str(merged), "de", region="testland")
    r = tool("verify_merge.py", merged, sl.b, sl.de, "--lang", "de", "--region", "testland", "--queries", "Alpha Town")
    assert "PASS  UUID derived" in r.stdout, r.stdout[-2000:]


def test_the_fingerprint_follows_every_generated_byte(sl: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-review B2-1: a merger change that moves one generated byte (the
    padding after the MIME list) without a MERGER_VERSION bump changes the
    fingerprint, so it is no light gate; a viewer patch, which changes only
    the slots and the UUID, keeps it."""
    ra, _, _ = recipe2.build_recipe(str(sl.a), [str(sl.de)], "de", region="testland")
    rb, _, _ = recipe2.build_recipe(str(sl.b), [str(sl.de)], "de", region="testland")
    assert ra["uuid"] != rb["uuid"] and ra["fingerprint"] == rb["fingerprint"]
    real = recipe2.pieces

    def changed(plan):
        first = True
        for pc in real(plan):
            if first and not isinstance(pc, merge_zim.Ref):
                first = False
                pc = bytes(pc[:-1]) + b"\x01"
            yield pc
    monkeypatch.setattr(recipe2, "pieces", changed)
    rc, _, _ = recipe2.build_recipe(str(sl.a), [str(sl.de)], "de", region="testland")
    assert rc["md5"] != ra["md5"] and rc["merger"] == ra["merger"]
    assert rc["fingerprint"] != ra["fingerprint"]
