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
open(os.path.join(out, a["--lang"] + ".ws.json"), "w").write("{}")
json.dump({"base": {"file": os.path.basename(zim), "md5": a["--md5"], "etag": a["--etag"]},
           "lang": a["--lang"], "viewer_code": os.environ["CODE"]}, open(os.path.join(out, "fresh.json"), "w"))
open(os.environ["FAKE_LOG"], "a").write(rid + "\n")
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
