"""ops/langpack/fresh_all.py: today's viewer in every live map's download.
Which file is live (as web/generate.py picks it), which maps are current
already, the preview copy, and what counts as a failure."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "ops" / "langpack" / "fresh_all.py"
sys.path.insert(0, str(ROOT))
from cloud.viewer_slots import viewer_code  # noqa: E402

CODE = viewer_code((ROOT / "resources" / "viewer" / "index.html").read_bytes())

# a stand-in for fresh_viewer.py: writes what the real one does that fresh_all.py reads, or fails for "bad"
FAKE = r'''import json, os, sys
rid, zim = sys.argv[1], sys.argv[2]
a = dict(zip(sys.argv[3::2], sys.argv[4::2]))
if rid == "bad":
    sys.exit(4)
out = a["--out"]
os.makedirs(out, exist_ok=True)
nodes = [sys.argv[i + 1] for i, x in enumerate(sys.argv) if x == "--mirror"]
base = {"file": os.path.basename(zim), **({"mirrors": nodes} if nodes else {})}
json.dump({"sources": {"base": base}}, open(os.path.join(out, a["--lang"] + ".ws.json"), "w"))
json.dump({"base": {"file": os.path.basename(zim), "md5": a["--md5"], "etag": a["--etag"]},
           "lang": a["--lang"], "viewer_code": os.environ["CODE"]}, open(os.path.join(out, "fresh.json"), "w"))
open(os.environ["FAKE_LOG"], "a").write(rid + "\n")
open(os.environ["FAKE_LOG"] + ".args", "a").write(" ".join(sys.argv[1:]) + "\n")
'''


def item(d: Path, rid: str, files: list[tuple[str, int, str]]) -> None:
    (d / f"streetzim-{rid}.json").write_text(json.dumps(
        {"files": [{"name": n, "size": str(s), "md5": m, "mtime": "1793000000"} for n, s, m in files]}))


def run(tmp: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(TOOL), "--maps", str(tmp / "maps"), "--site", str(tmp / "site"),
                           "--preview", str(tmp / "preview"), "--meta-dir", str(tmp / "meta"),
                           "--fresh-tool", str(tmp / "fake.py"), *args],
                          capture_output=True, text=True, timeout=120,
                          env={"PATH": "/usr/bin:/bin", "CODE": CODE, "FAKE_LOG": str(tmp / "log"),
                               "PYTHONDONTWRITEBYTECODE": "1"})


def setup(tmp: Path) -> None:
    for x in ("maps", "site", "preview", "meta"):
        (tmp / x).mkdir()
    (tmp / "fake.py").write_text(FAKE)
    # alpha: a dated re-roll beats the plain dated file and the undated one; its local copy is there
    item(tmp / "meta", "alpha", [("osm-alpha.zim", 900, "a" * 32), ("osm-alpha-2026-10-20.zim", 500, "b" * 32),
                                 ("osm-alpha-2026-10-20b.zim", 400, "c" * 32), ("history/osm-alpha-2026-11-01.zim", 1, "d" * 32)])
    (tmp / "maps" / "osm-alpha-2026-10-20b.zim").write_bytes(b"z" * 400)
    item(tmp / "meta", "gone", [("osm-gone-2026-10-20.zim", 300, "e" * 32)])      # no local copy
    item(tmp / "meta", "bad", [("osm-bad-2026-10-20.zim", 200, "f" * 32)])        # fresh_viewer.py fails
    (tmp / "maps" / "osm-bad-2026-10-20.zim").write_bytes(b"z" * 200)


def test_each_live_file_gets_todays_viewer_once(tmp_path: Path) -> None:
    assert CODE
    setup(tmp_path)
    r = run(tmp_path, "--regions", "alpha,gone,bad,none")
    assert r.returncode == 1, r.stdout + r.stderr                     # one failed, one without a copy
    fr = json.loads((tmp_path / "site" / "alpha-viewer" / "fresh.json").read_text())
    assert fr["base"]["file"] == "osm-alpha-2026-10-20b.zim" and fr["base"]["md5"] == "c" * 32
    assert fr["base"]["etag"] == '"%x-%x"' % (1793000000, 400)
    assert json.loads((tmp_path / "preview" / "alpha-viewer" / "fresh.json").read_text()) == fr
    assert not (tmp_path / "site" / "bad-viewer").exists() and not (tmp_path / "preview" / "bad-viewer").exists()
    assert "gone: no local copy of osm-gone-2026-10-20.zim" in r.stdout
    assert "none: no live ZIM on archive.org" in r.stdout
    assert "failed 1: bad" in r.stdout and "no local copy 1: gone" in r.stdout
    assert (tmp_path / "log").read_text().split() == ["alpha"]
    r = run(tmp_path, "--regions", "alpha")                            # current now: nothing made
    assert r.returncode == 0 and f"alpha: osm-alpha-2026-10-20b.zim already has viewer {CODE}" in r.stdout
    assert (tmp_path / "log").read_text().split() == ["alpha"]
    # a newer live file (or another viewer) makes it again
    item(tmp_path / "meta", "alpha", [("osm-alpha-2026-10-27.zim", 450, "9" * 32)])
    (tmp_path / "maps" / "osm-alpha-2026-10-27.zim").write_bytes(b"z" * 450)
    assert run(tmp_path, "--regions", "alpha").returncode == 0
    assert (tmp_path / "log").read_text().split() == ["alpha", "alpha"]
    assert json.loads((tmp_path / "preview" / "alpha-viewer" / "fresh.json").read_text())["base"]["md5"] == "9" * 32


def test_a_dry_run_makes_nothing(tmp_path: Path) -> None:
    setup(tmp_path)
    r = run(tmp_path, "--regions", "alpha", "--dry-run")
    assert r.returncode == 0 and f"alpha: osm-alpha-2026-10-20b.zim (0.00 GB, en) -> viewer {CODE}" in r.stdout
    assert not (tmp_path / "log").exists() and not (tmp_path / "site" / "alpha-viewer").exists()


def test_the_item_s_storage_nodes_go_to_the_index(tmp_path: Path) -> None:
    setup(tmp_path)
    meta = {"d1": "ia600707.us.archive.org", "d2": "ia800707.us.archive.org", "dir": "/22/items/streetzim-alpha",
            "files": [{"name": "osm-alpha-2026-10-20b.zim", "size": "400", "md5": "c" * 32, "mtime": "1793000000"}]}
    (tmp_path / "meta" / "streetzim-alpha.json").write_text(json.dumps(meta))
    nodes = [f"https://ia{n}.us.archive.org/22/items/streetzim-alpha/osm-alpha-2026-10-20b.zim" for n in ("600707", "800707")]
    r = run(tmp_path, "--regions", "alpha")
    assert r.returncode == 0, r.stdout + r.stderr
    for d in ("site", "preview"):
        ix = json.loads((tmp_path / d / "alpha-viewer" / "en.ws.json").read_text())
        assert ix["sources"]["base"]["mirrors"] == nodes, d
    assert (tmp_path / "log").read_text().split() == ["alpha"]
    # the same nodes listed the other way round: current
    (tmp_path / "meta" / "streetzim-alpha.json").write_text(json.dumps({**meta, "d1": meta["d2"], "d2": meta["d1"]}))
    r = run(tmp_path, "--regions", "alpha")
    assert r.returncode == 0 and "already has viewer" in r.stdout
    assert (tmp_path / "log").read_text().split() == ["alpha"]


def test_a_map_made_before_its_nodes_were_recorded_gets_them_written_in(tmp_path: Path) -> None:
    setup(tmp_path)
    (tmp_path / "meta" / "streetzim-alpha.json").write_text(json.dumps({
        "d1": "ia600707.us.archive.org", "d2": "ia800707.us.archive.org", "dir": "/22/items/streetzim-alpha",
        "files": [{"name": "osm-alpha-2026-10-20b.zim", "size": "400", "md5": "c" * 32, "mtime": "1793000000"}]}))
    nodes = [f"https://ia{n}.us.archive.org/22/items/streetzim-alpha/osm-alpha-2026-10-20b.zim" for n in ("600707", "800707")]
    for d in ("site", "preview"):
        (tmp_path / d / "alpha-viewer").mkdir()
        (tmp_path / d / "alpha-viewer" / "en.ws.json").write_text(json.dumps(
            {"uuid": "u", "sources": {"base": {"file": "osm-alpha-2026-10-20b.zim", "size": 400}}, "parts": [[0, 400, "s", "base", 0]]}))
        (tmp_path / d / "alpha-viewer" / "fresh.json").write_text(json.dumps(
            {"base": {"file": "osm-alpha-2026-10-20b.zim", "md5": "c" * 32}, "lang": "en", "viewer_code": CODE}))
    r = run(tmp_path, "--regions", "alpha")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "the item's nodes recorded" in r.stdout and "made 1: alpha" in r.stdout
    assert not (tmp_path / "log").exists()                             # not composed again
    for d in ("site", "preview"):
        ix = json.loads((tmp_path / d / "alpha-viewer" / "en.ws.json").read_text())
        assert ix["sources"]["base"] == {"file": "osm-alpha-2026-10-20b.zim", "size": 400, "mirrors": nodes}, d
        assert ix["uuid"] == "u" and ix["parts"] == [[0, 400, "s", "base", 0]], d
    r = run(tmp_path, "--regions", "alpha")                            # current now
    assert r.returncode == 0 and "already has viewer" in r.stdout
    # a node that is not archive.org's is left out; a dir with ".." leaves both out
    (tmp_path / "meta" / "streetzim-alpha.json").write_text(json.dumps({
        "d1": "evil.example", "d2": "ia800707.us.archive.org", "dir": "/22/items/streetzim-alpha",
        "files": [{"name": "osm-alpha-2026-10-27.zim", "size": "450", "md5": "9" * 32, "mtime": "1793000000"}]}))
    (tmp_path / "maps" / "osm-alpha-2026-10-27.zim").write_bytes(b"z" * 450)
    assert run(tmp_path, "--regions", "alpha").returncode == 0
    ix = json.loads((tmp_path / "site" / "alpha-viewer" / "en.ws.json").read_text())
    assert ix["sources"]["base"]["mirrors"] == ["https://ia800707.us.archive.org/22/items/streetzim-alpha/osm-alpha-2026-10-27.zim"]
    (tmp_path / "meta" / "streetzim-alpha.json").write_text(json.dumps({
        "d1": "ia600707.us.archive.org", "dir": "/22/../x",
        "files": [{"name": "osm-alpha-2026-10-28.zim", "size": "460", "md5": "8" * 32, "mtime": "1793000000"}]}))
    (tmp_path / "maps" / "osm-alpha-2026-10-28.zim").write_bytes(b"z" * 460)
    assert run(tmp_path, "--regions", "alpha").returncode == 0
    assert "mirrors" not in json.dumps(json.loads((tmp_path / "site" / "alpha-viewer" / "en.ws.json").read_text()))


def test_recording_the_nodes_dry_runs_falls_back_and_checks_the_index(tmp_path: Path) -> None:
    setup(tmp_path)
    (tmp_path / "meta" / "streetzim-alpha.json").write_text(json.dumps({
        "d1": "ia600707.us.archive.org", "d2": "ia800707.us.archive.org", "dir": "/22/items/streetzim-alpha",
        "files": [{"name": "osm-alpha-2026-10-20b.zim", "size": "400", "md5": "c" * 32, "mtime": "1793000000"}]}))
    def made_before(index_file: str = "osm-alpha-2026-10-20b.zim", preview_index: bool = True) -> None:
        for d in ("site", "preview"):
            v = tmp_path / d / "alpha-viewer"
            v.mkdir(parents=True, exist_ok=True)
            if d == "site" or preview_index:
                (v / "en.ws.json").write_text(json.dumps({"sources": {"base": {"file": index_file}}}))
            elif (v / "en.ws.json").exists():
                (v / "en.ws.json").unlink()
            (v / "fresh.json").write_text(json.dumps(
                {"base": {"file": "osm-alpha-2026-10-20b.zim", "md5": "c" * 32}, "lang": "en", "viewer_code": CODE}))
    # a dry run says so and writes nothing
    made_before()
    r = run(tmp_path, "--regions", "alpha", "--dry-run")
    assert r.returncode == 0 and "would record the item's nodes" in r.stdout, r.stdout
    assert "mirrors" not in (tmp_path / "site" / "alpha-viewer" / "en.ws.json").read_text()
    # the preview copy's index cannot be written: composed again instead
    made_before(preview_index=False)
    r = run(tmp_path, "--regions", "alpha")
    assert r.returncode == 0 and "could not record the item's nodes" in r.stdout and "made again" in r.stdout, r.stdout
    assert (tmp_path / "log").read_text().split() == ["alpha"]
    # an index that names another file is not current
    (tmp_path / "log").unlink()
    made_before(index_file="osm-alpha-2026-10-01.zim")
    r = run(tmp_path, "--regions", "alpha")
    assert r.returncode == 0 and (tmp_path / "log").read_text().split() == ["alpha"], r.stdout


def test_metadata_archive_org_did_not_give_is_a_failure(tmp_path: Path) -> None:
    setup(tmp_path)
    (tmp_path / "meta" / "streetzim-alpha.json").write_text(json.dumps({"error": "item metadata may be invalid"}))
    r = run(tmp_path, "--regions", "alpha,none")
    assert r.returncode == 1
    assert "alpha: archive.org's metadata for streetzim-alpha failed: item metadata may be invalid" in r.stdout
    assert "failed 1: alpha" in r.stdout and "none: no live ZIM on archive.org" in r.stdout


def test_thorough_reaches_the_composer(tmp_path: Path) -> None:
    """--thorough (the periodic full check) is passed to fresh_viewer.py; by default it is not."""
    setup(tmp_path)
    assert run(tmp_path, "--regions", "alpha").returncode == 0
    item(tmp_path / "meta", "alpha", [("osm-alpha-2026-10-27.zim", 450, "9" * 32)])
    (tmp_path / "maps" / "osm-alpha-2026-10-27.zim").write_bytes(b"z" * 450)
    assert run(tmp_path, "--regions", "alpha", "--thorough").returncode == 0
    args = (tmp_path / "log.args").read_text().splitlines()
    assert len(args) == 2 and "--thorough" not in args[0].split() and args[1].split()[-1] == "--thorough", args
