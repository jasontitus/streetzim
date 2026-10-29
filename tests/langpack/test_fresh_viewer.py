"""tools/langpack/fresh_viewer.py: a live map with today's viewer as the
build worker serves it, with no new upload (docs/fresh-viewer.md). On a
small base with viewer slots (lpzims): the segment index, read from the base
and the blobs, is exactly the composed file, which carries a valid ZIM
checksum and a new UUID; nothing is written for a base archive.org does not
serve, for a base without slots, or when the gates fail; the worker's own
index check (build-proxy/merged-webseed.js checkIndex) accepts the index."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tests.langpack.conftest import ROOT  # noqa: E402
from tools.langpack import fresh_viewer  # noqa: E402

# swap_base_viewer.py takes only a viewer that reads Wikipedia packs (WIKI_PACKS)
V2 = {"index.html": b"<html><body>map, viewer 2: the search bar stays<script>var WIKI_PACKS=[]</script></body></html>",
      "places.html": b"<html><body>places, viewer 2</body></html>",
      "routing-worker.js": b"/* routing, viewer 2 */"}
WS = "https://streetzim-build-proxy.example.workers.dev"
REV = "abc1234"


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


@pytest.fixture
def fv(tmp_path: Path) -> SimpleNamespace:
    base = tmp_path / "osm-testland-2026-10-13.zim"
    lpzims.write_base(base, slots=lpzims.VIEWER_V1)
    root = tmp_path / "viewer-root"                 # a viewer export: the new viewer, this checkout's tools
    (root / "resources" / "viewer").mkdir(parents=True)
    for name, data in V2.items():
        (root / "resources" / "viewer" / name).write_bytes(data)
    os.symlink(ROOT / "cloud", root / "cloud")
    return SimpleNamespace(base=base, root=root, out=tmp_path / "out", work=tmp_path / "work", tmp=tmp_path)


def run(fv: SimpleNamespace, *extra: str, md5sum: str | None = None, gates: str = "-") -> int:
    return fresh_viewer.main([
        "testland", str(fv.base), "--item", "streetzim-testland", "--lang", "en", "--out", str(fv.out),
        "--webseed", WS, "--md5", md5sum or md5(fv.base), "--etag", '"6ae291e1-2d07ff2"',
        "--viewer-root", str(fv.root), "--rev", REV, "--work", str(fv.work), "--gates", gates,
        "--allow-full-copy", *extra])


def materialize(index: dict, out: Path, base: Path) -> bytes:
    data = b""
    for start, n, kind, ref, off in index["parts"]:
        assert len(data) == start
        if kind == "x":
            blob = (out / index["blobs"][ref]["file"]).read_bytes()
            assert hashlib.sha256(blob).hexdigest() == ref
            data += blob[off:off + n]
        else:
            assert index["sources"][ref]["file"] == base.name and off == start
            with open(base, "rb") as fh:
                fh.seek(off)
                data += fh.read(n)
    return data


def test_the_index_is_the_composed_file(fv: SimpleNamespace) -> None:
    assert run(fv, "--keep") == 0
    fresh = json.loads((fv.out / "fresh.json").read_text())
    index = json.loads((fv.out / "en.ws.json").read_text())
    composed = fv.work / fresh["file"]
    assert fresh["file"] == f"osm-testland-2026-10-13.v{REV}.zim" == index["file"]
    assert fresh["lang"] == index["lang"] == "en" and fresh["viewer"] == REV
    assert index["region"] == fresh["site_region"] == "testland-viewer" and fresh["region"] == "testland"
    data = materialize(index, fv.out, fv.base)
    assert data == composed.read_bytes()
    assert hashlib.md5(data).hexdigest() == fresh["file_md5"]
    assert hashlib.md5(data[:-16]).hexdigest() == data[-16:].hex() == fresh["md5"]   # a valid ZIM checksum
    assert lpzims.read_uuid(composed) != lpzims.read_uuid(fv.base) and fresh["uuid"] in index["webseed"]
    assert index["webseed"] == f"{WS}/merged/testland-viewer/en/{fresh['uuid']}/{fresh['file']}" == fresh["webseed"]
    assert index["sources"] == {"base": {"item": "streetzim-testland", "file": fv.base.name,
                                         "size": fv.base.stat().st_size, "etag": '"6ae291e1-2d07ff2"'}}
    # what differs is the UUID, the viewer and the checksum: a few blobs, the rest the base's own bytes
    assert fresh["blob_bytes"] < 1_500_000 and fresh["subrequests"]["whole"] <= 12
    from libzim.reader import Archive
    a = Archive(str(composed))
    assert a.check() and bytes(a.get_entry_by_path("index.html").get_item().content).startswith(V2["index.html"])
    # the same viewer again: the same file (a rerun publishes the same torrent)
    infohash = fresh["infohash"]
    shutil.rmtree(fv.out)
    assert run(fv) == 0 and json.loads((fv.out / "fresh.json").read_text())["infohash"] == infohash
    assert not fv.work.exists()                       # without --keep the composed file goes


def test_the_worker_accepts_the_index(fv: SimpleNamespace) -> None:
    node = os.environ.get("NODE") or shutil.which("node")
    if not node:
        pytest.skip("no node")
    assert run(fv) == 0
    fresh = json.loads((fv.out / "fresh.json").read_text())
    js = ("import { checkIndex } from %s; import fs from 'node:fs';"
          "const ix = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));"
          "console.log(JSON.stringify(checkIndex(ix, 'testland-viewer', 'en', process.argv[2], process.argv[3])));"
          % json.dumps(str(ROOT / "build-proxy" / "merged-webseed.js")))
    r = subprocess.run([node, "--input-type=module", "-e", js, str(fv.out / "en.ws.json"), fresh["uuid"], fresh["file"]],
                       capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout) == "", r.stdout + r.stderr


def test_nothing_is_published_for_a_file_archive_org_does_not_serve(fv: SimpleNamespace) -> None:
    assert run(fv, md5sum="0" * 32) == 1 and not fv.out.exists()


def test_nothing_is_published_when_the_gates_fail(fv: SimpleNamespace) -> None:
    assert run(fv, gates="/bin/false") == 1 and not fv.out.exists()
    assert not fv.work.exists() or not list(fv.work.glob("*.zim*"))     # the composed file went


def test_a_base_without_viewer_slots_needs_a_rebuild(fv: SimpleNamespace, capsys) -> None:
    lpzims.write_base(fv.base)                        # no slots: the viewer is in a compressed cluster
    assert run(fv) == 1 and not fv.out.exists()
    assert "viewer" in capsys.readouterr().err
