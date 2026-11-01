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
V2 = {"index.html": b"<html><body>map, viewer 2: the search bar stays<script>var WIKI_PACKS=[]; "
                    b"var SZ_VIEWER_BUILD = '0123abcd';</script></body></html>",
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


def etag(p: Path) -> str:
    return '"6ae291e1-%x"' % p.stat().st_size          # archive.org's: "<mtime hex>-<size hex>"


def run(fv: SimpleNamespace, *extra: str, md5sum: str | None = None, gates: str = "-", tag: str | None = None,
        rev: str | None = REV) -> int:
    return fresh_viewer.main([
        "testland", str(fv.base), "--item", "streetzim-testland", "--lang", "en", "--out", str(fv.out),
        "--webseed", WS, "--md5", md5sum or md5(fv.base), "--etag", tag or etag(fv.base),
        "--viewer-root", str(fv.root), "--work", str(fv.work), "--gates", gates,
        "--allow-full-copy", *(["--rev", rev] if rev else []), *extra])


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
    assert fresh["file"] == "osm-testland-2026-10-13.v0123abcd.zim" == index["file"]     # the code About shows
    assert fresh["lang"] == index["lang"] == "en" and fresh["viewer"] == REV and fresh["viewer_code"] == "0123abcd"
    assert index["region"] == fresh["site_region"] == "testland-viewer" and fresh["region"] == "testland"
    data = materialize(index, fv.out, fv.base)
    assert data == composed.read_bytes()
    assert hashlib.md5(data).hexdigest() == fresh["file_md5"]
    assert hashlib.md5(data[:-16]).hexdigest() == data[-16:].hex() == fresh["md5"]   # a valid ZIM checksum
    assert lpzims.read_uuid(composed) != lpzims.read_uuid(fv.base) and fresh["uuid"] in index["webseed"]
    assert index["webseed"] == f"{WS}/merged/testland-viewer/en/{fresh['uuid']}/{fresh['file']}" == fresh["webseed"]
    assert index["sources"] == {"base": {"item": "streetzim-testland", "file": fv.base.name,
                                         "size": fv.base.stat().st_size, "etag": etag(fv.base)}}
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


def test_the_item_s_storage_nodes_go_in_the_index(fv: SimpleNamespace, capsys) -> None:
    # the worker's fallback when archive.org's front door answers 503 for the item
    nodes = [f"https://ia800707.us.archive.org/22/items/streetzim-testland/{fv.base.name}",
             f"https://ia600707.us.archive.org/22/items/streetzim-testland/{fv.base.name}"]
    assert run(fv, *[x for m in nodes for x in ("--mirror", m)]) == 0
    assert json.loads((fv.out / "en.ws.json").read_text())["sources"]["base"]["mirrors"] == nodes
    # none given: none written
    assert run(fv) == 0 and "mirrors" not in json.loads((fv.out / "en.ws.json").read_text())["sources"]["base"]
    # the file's name percent-encoded is the same file
    enc = nodes[0].replace("osm-testland", "osm%2Dtestland")
    assert run(fv, "--mirror", enc) == 0
    assert json.loads((fv.out / "en.ws.json").read_text())["sources"]["base"]["mirrors"] == [enc]
    # four at most
    assert run(fv, *[x for i in range(5) for x in ("--mirror", nodes[0].replace("ia800707", f"ia80070{i}"))]) == 1
    assert "at most 4 --mirror URLs" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["https://example.org/22/items/streetzim-testland/osm-testland-2026-10-13.zim",
                                 "https://ia800707.us.archive.org/22/items/streetzim-testland/other.zim",
                                 "http://ia800707.us.archive.org/22/items/streetzim-testland/osm-testland-2026-10-13.zim",
                                 "https://ia800707.us.archive.org/22/items/../streetzim-testland/osm-testland-2026-10-13.zim",
                                 "https://evil.com/.archive.org/streetzim-testland/osm-testland-2026-10-13.zim"])
def test_a_mirror_must_be_an_archive_org_node_for_the_file(fv: SimpleNamespace, url: str, capsys) -> None:
    assert run(fv, "--mirror", url) == 1
    assert "not an archive.org storage node's URL" in capsys.readouterr().err
    assert not (fv.out / "en.ws.json").exists()


def test_nothing_is_published_for_a_file_archive_org_does_not_serve(fv: SimpleNamespace) -> None:
    assert run(fv, md5sum="0" * 32) == 1 and not fv.out.exists()


def test_nothing_is_published_when_the_gates_fail(fv: SimpleNamespace) -> None:
    assert run(fv, gates="/bin/false") == 1 and not fv.out.exists()
    assert not fv.work.exists() or not list(fv.work.glob("*.zim*"))     # the composed file went


def test_a_base_without_viewer_slots_needs_a_rebuild(fv: SimpleNamespace, capsys) -> None:
    lpzims.write_base(fv.base)                        # no slots: the viewer is in a compressed cluster
    assert run(fv) == 1 and not fv.out.exists()
    assert "viewer" in capsys.readouterr().err


@pytest.mark.parametrize("tag", ['6ae291e1-16a00f', '"6ae291e1-2d07ff2"', ""])
def test_archive_org_s_etag_is_required_as_the_worker_sends_it(fv: SimpleNamespace, tag: str) -> None:
    """Without its quotes (the shell ate them), for a file of another size, or
    none: the worker's If-Range would make archive.org answer 200 to every
    range and the file would read as replaced (or a replaced one as not)."""
    if tag == "":
        with pytest.raises(SystemExit):              # argparse: --etag is required
            fresh_viewer.main(["testland", str(fv.base), "--item", "streetzim-testland", "--lang", "en",
                               "--out", str(fv.out), "--webseed", WS, "--md5", md5(fv.base)])
    else:
        assert run(fv, tag=tag) == 1
    assert not fv.out.exists()


def test_bytes_left_out_of_the_index_fail_its_own_checksum(fv: SimpleNamespace, monkeypatch) -> None:
    """A changed run the index missed (one slot left out) would serve the
    base's old bytes there: the index's own checksum does not match its last
    16 bytes, and nothing is published."""
    real = fresh_viewer.slot_windows
    monkeypatch.setattr(fresh_viewer, "slot_windows", lambda p: real(p)[1:])
    assert run(fv) == 1 and not fv.out.exists()


def test_a_viewer_without_its_code_is_refused(fv: SimpleNamespace, capsys) -> None:
    """The file is named after the viewer's code (About shows it): a viewer
    without one is refused before anything is made."""
    (fv.root / "resources" / "viewer" / "index.html").write_bytes(b"<html><body>map<script>var WIKI_PACKS=[]</script></body></html>")
    assert run(fv) == 1 and not fv.out.exists() and not (fv.work.exists() and os.listdir(fv.work))
    assert "no viewer code (SZ_VIEWER_BUILD)" in capsys.readouterr().err


def test_the_composed_file_must_carry_the_viewer_it_is_named_after(fv: SimpleNamespace, monkeypatch) -> None:
    """A composed file with another viewer in it (here the base's own) is not
    published under the code's name, on either path."""
    def stale(zim, work, repo, viewer, name, full_copy=False):
        os.makedirs(work, exist_ok=True)
        out = os.path.join(work, name)
        shutil.copy(zim, out)
        return out, {}
    monkeypatch.setattr(fresh_viewer, "compose", stale)
    assert run(fv, "--thorough") == 1 and not fv.out.exists()
    real = fresh_viewer.compose_fast

    def stale_fast(zim, work, repo, code, name, full_copy=False):
        out, uuid, windows = real(zim, work, repo, code, name, full_copy)
        shutil.copy(zim, out)                          # the base's own viewer back in
        return out, uuid, windows
    monkeypatch.setattr(fresh_viewer, "compose_fast", stale_fast)
    assert run(fv) == 1 and not fv.out.exists()


def test_the_uuid_follows_the_viewer_not_the_commit(fv: SimpleNamespace) -> None:
    """Two commits with the same viewer bytes (a comment in a part, compacted
    away) are the same book: the same UUID and file."""
    assert run(fv, rev="abc1234") == 0
    a = json.loads((fv.out / "fresh.json").read_text())
    assert run(fv, rev="def5678") == 0
    b = json.loads((fv.out / "fresh.json").read_text())
    assert (a["uuid"], a["file"], a["file_md5"]) == (b["uuid"], b["file"], b["file_md5"]) and a["viewer"] != b["viewer"]


def test_the_uuid_must_be_the_one_the_swap_wrote(fv: SimpleNamespace, monkeypatch) -> None:
    real = fresh_viewer.compose
    monkeypatch.setattr(fresh_viewer, "compose", lambda *a, **k: (real(*a, **k)[0], {"new_uuid": "0" * 8 + "-0000-0000-0000-" + "0" * 12}))
    assert run(fv, "--thorough") == 1 and not fv.out.exists()
    fast = fresh_viewer.compose_fast
    monkeypatch.setattr(fresh_viewer, "compose_fast",
                        lambda *a, **k: (lambda r: (r[0], "0" * 8 + "-0000-0000-0000-" + "0" * 12, r[2]))(fast(*a, **k)))
    assert run(fv) == 1 and not fv.out.exists()


def test_the_one_pass_publishes_what_the_thorough_path_does(fv: SimpleNamespace, monkeypatch) -> None:
    """The default path (the map read once) and --thorough (the swap, its
    comparison and every MD5 afresh) publish the same file: the same UUID,
    MD5s, torrent, index and blobs; the worker's bytes are that file."""
    assert run(fv, "--thorough") == 0
    slow = {p.name: p.read_bytes() for p in fv.out.iterdir()}
    shutil.rmtree(fv.out)
    assert run(fv) == 0
    fast = {p.name: p.read_bytes() for p in fv.out.iterdir()}
    strip = lambda b: {k: v for k, v in json.loads(b).items() if k != "made"}  # noqa: E731
    assert strip(fast.pop("fresh.json")) == strip(slow.pop("fresh.json"))
    assert fast == slow
    index = json.loads(fast["en.ws.json"])
    data = materialize(index, fv.out, fv.base)
    assert hashlib.md5(data, usedforsecurity=False).hexdigest() == index["file_md5"]
    assert hashlib.md5(data[:-16], usedforsecurity=False).digest() == data[-16:]


def test_the_one_pass_reads_the_base_for_its_md5_once(fv: SimpleNamespace, monkeypatch) -> None:
    """archive.org's file is checked against its MD5 once, then by its path,
    size, mtime and inode (cached_md5), until it changes; --thorough reads it
    every time; a file of another MD5 is refused however it was cached."""
    assert run(fv) == 0
    reads = []
    real = fresh_viewer.md5_file
    monkeypatch.setattr(fresh_viewer, "md5_file", lambda p: reads.append(p) or real(p))
    assert run(fv) == 0 and reads == []
    assert run(fv, "--thorough") == 0 and len(reads) == 1
    os.utime(fv.base, (1_700_000_000, 1_700_000_000))          # touched: read again
    assert run(fv, tag=etag(fv.base)) == 0 and len(reads) == 2
    assert run(fv, md5sum="0" * 32) == 1                       # not the listed file: refused
    # patched in place with its mtime put back (touch -r): the change time says so, read again
    archived = md5(fv.base)
    assert run(fv, md5sum=archived, tag=etag(fv.base)) == 0
    st = os.stat(fv.base)
    data = bytearray(fv.base.read_bytes())
    data[200] ^= 0xFF
    with open(fv.base, "r+b") as fh:
        fh.write(bytes(data))
    os.utime(fv.base, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(fv.base).st_mtime_ns == st.st_mtime_ns and os.stat(fv.base).st_size == st.st_size
    n = len(reads)
    assert run(fv, md5sum=archived, tag=etag(fv.base)) == 1 and len(reads) == n + 1


def test_a_slot_not_where_it_was_written_is_refused(fv: SimpleNamespace, monkeypatch) -> None:
    real = fresh_viewer.compose_fast
    monkeypatch.setattr(fresh_viewer, "compose_fast",
                        lambda *a, **k: (lambda r: (r[0], r[1], r[2][:-1] + [(r[2][-1][0] + 1, r[2][-1][1])]))(real(*a, **k)))
    assert run(fv) == 1 and not fv.out.exists()


def test_an_earlier_viewer_s_files_go(fv: SimpleNamespace) -> None:
    fv.out.mkdir()
    (fv.out / ("ws-" + "a" * 32 + ".bin")).write_bytes(b"an earlier viewer's blob")
    (fv.out / "de.ws.json").write_text("{}")
    (fv.out / "keep.txt").write_text("not ours")
    assert run(fv) == 0
    index = json.loads((fv.out / "en.ws.json").read_text())
    names = sorted(p.name for p in fv.out.iterdir())
    assert names == sorted(["en.ws.json", "en.torrent", "fresh.json", "keep.txt"] + [b["file"] for b in index["blobs"].values()]), names


def test_an_uncommitted_viewer_is_refused(fv: SimpleNamespace, capsys) -> None:
    """The new UUID and the file's name come from the viewer's revision:
    other bytes under the same revision would be a book Kiwix takes for one
    it has."""
    git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-C", str(fv.root)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "resources"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "viewer"], check=True)
    (fv.root / "resources" / "viewer" / "index.html").write_bytes(V2["index.html"] + b"<!-- edited -->")
    assert run(fv, rev=None) == 1 and not fv.out.exists()
    assert "uncommitted" in capsys.readouterr().err
