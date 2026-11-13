"""ops/batch_extracts.py: the queue's search files in one pass over the world
file, and its extracts a few regions per planet pass, installed the way the
driver installs its own (so the driver finds them and skips its cut)."""
import json
import os
import stat
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "ops"))
import batch_extracts as be  # noqa: E402

REGISTRY = """# id\tname\tbbox\ttier
small\tSmall\t10.0,10.0,11.0,11.0\tcountry
other\tOther\t20.0,-5.0,22.0,-3.0\tcountry
wrap\tWrap\t179.0,-20.0,-179.0,-18.0\tcountry
"""


def _root(tmp_path):
    (tmp_path / "cloud").mkdir()
    (tmp_path / "cloud/regions.tsv").write_text(REGISTRY)
    (tmp_path / "world-data/regions").mkdir(parents=True)
    (tmp_path / "search_cache").mkdir()
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _rec(name, lat, lon):
    return json.dumps({"name": name, "type": "place", "lat": lat, "lon": lon}, separators=(",", ":")) + "\n"


def _world(root, lines):
    p = root / be.WORLD_SEARCH
    p.write_text("".join(lines))
    (root / (be.WORLD_SEARCH + ".schema")).write_text('{"search_schema": 2, "languages": []}')
    return p


def _driver_ready(pbf, bbox):
    """The driver's own test (ops/region-bbox.sh), run by bash on the files."""
    script = (f'. {ROOT}/ops/region-bbox.sh; '
              f'[ -s "{pbf}" ] && ! bbox_stale "{pbf}" "{bbox}" && pbf_complete "{pbf}"')
    return subprocess.run(["bash", "-c", script], capture_output=True).returncode == 0


def test_search_keeps_the_box_with_a_margin_and_writes_the_sidecars(tmp_path):
    root = _root(tmp_path)
    _world(root, [_rec("in", 10.5, 10.5), _rec("edge", 11.005, 10.0), _rec("out", 12.0, 10.5),
                  _rec("other", -4.0, 21.0), _rec("east", -19.0, 179.5), _rec("west", -19.0, -179.5),
                  _rec("far", -19.0, 0.0)])
    be.main(["--root", str(root), "--skip-pbf", "--", "small", "other", "wrap"])
    reg = root / "world-data/regions"
    names = lambda rid: [json.loads(l)["name"] for l in (reg / f"{rid}.search.jsonl").read_text().splitlines()]  # noqa: E731
    assert names("small") == ["in", "edge"]
    assert names("other") == ["other"]
    assert names("wrap") == ["east", "west"]
    assert (reg / "small.search.jsonl.bbox").read_text() == "10.0,10.0,11.0,11.0\n"
    assert '"search_schema": 2' in (reg / "small.search.jsonl.schema").read_text()
    assert not list(reg.glob("*.part"))


def test_search_replaces_a_symlink_to_the_world_file_but_never_a_real_cut(tmp_path):
    root = _root(tmp_path)
    world = _world(root, [_rec("in", 10.5, 10.5), _rec("other", -4.0, 21.0)])
    reg = root / "world-data/regions"
    os.symlink(world, reg / "small.search.jsonl")
    (reg / "other.search.jsonl").write_text("mine\n")
    be.main(["--root", str(root), "--skip-pbf", "--", "small", "other"])
    assert not os.path.islink(reg / "small.search.jsonl")
    assert "in" in (reg / "small.search.jsonl").read_text()
    assert (reg / "other.search.jsonl").read_text() == "mine\n"
    assert world.read_text().count("\n") == 2  # the world file is untouched


def _fake_osmium(tmp_path, fail=False):
    log = tmp_path / "osmium.log"
    p = tmp_path / "osmium"
    p.write_text(f"""#!/usr/bin/env python3
import json, sys
a = sys.argv[1:]
cfg = json.load(open(a[a.index("-c") + 1]))
open({str(log)!r}, "a").write(" ".join(e["output"] for e in cfg["extracts"]) + " " + a[a.index("-s") + 1] + "\\n")
if {fail!r}:
    sys.exit(1)
for e in cfg["extracts"]:
    open(cfg["directory"] + "/" + e["output"], "w").write("pbf " + ",".join(map(str, e["bbox"])))
""")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p, log


def test_pbfs_go_in_batches_and_are_installed_as_the_driver_installs_them(tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / be.PLANET).parent.mkdir(parents=True, exist_ok=True)
    (root / be.PLANET).write_text("planet")
    osm, log = _fake_osmium(tmp_path)
    monkeypatch.setenv("STREETZIM_OSMIUM", str(osm))
    be.main(["--root", str(root), "--skip-search", "--batch", "1", "--", "small", "other", "wrap"])
    assert log.read_text().splitlines() == ["small.osm.pbf smart", "other.osm.pbf smart"]  # wrap: the driver's
    reg = root / "world-data/regions"
    assert (reg / "small.osm.pbf").read_text() == "pbf 10.0,10.0,11.0,11.0"
    assert _driver_ready(reg / "small.osm.pbf", "10.0,10.0,11.0,11.0")
    assert _driver_ready(reg / "other.osm.pbf", "20.0,-5.0,22.0,-3.0")
    assert not (reg / "wrap.osm.pbf").exists()
    # A second run finds them ready and reads no planet.
    be.main(["--root", str(root), "--skip-search", "--", "small", "other"])
    assert len(log.read_text().splitlines()) == 2


def test_a_ready_extract_is_left_and_a_failed_pass_installs_nothing(tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / be.PLANET).parent.mkdir(parents=True, exist_ok=True)
    (root / be.PLANET).write_text("planet")
    reg = root / "world-data/regions"
    pbf = reg / "small.osm.pbf"
    pbf.write_text("driver's own")
    (reg / "small.osm.pbf.bbox").write_text("10.0,10.0,11.0,11.0\n")
    st = pbf.stat()
    (reg / "small.osm.pbf.strategy").write_text(f"smart {st.st_size} {int(st.st_mtime)}\n")
    osm, log = _fake_osmium(tmp_path, fail=True)
    monkeypatch.setenv("STREETZIM_OSMIUM", str(osm))
    be.main(["--root", str(root), "--skip-search", "--", "small", "other"])
    assert log.read_text().splitlines() == ["other.osm.pbf smart"]
    assert pbf.read_text() == "driver's own"
    assert not (reg / "other.osm.pbf").exists()
    assert not list((root / "tmp/batch-extracts").glob("*.osm.pbf"))


def test_a_stale_extract_is_cut_again(tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / be.PLANET).parent.mkdir(parents=True, exist_ok=True)
    (root / be.PLANET).write_text("planet")
    reg = root / "world-data/regions"
    (reg / "small.osm.pbf").write_text("old box")
    (reg / "small.osm.pbf.bbox").write_text("9.0,9.0,11.0,11.0\n")
    st = (reg / "small.osm.pbf").stat()  # complete, but for another box
    (reg / "small.osm.pbf.strategy").write_text(f"smart {st.st_size} {int(st.st_mtime)}\n")
    osm, log = _fake_osmium(tmp_path)
    monkeypatch.setenv("STREETZIM_OSMIUM", str(osm))
    be.main(["--root", str(root), "--skip-search", "--", "small"])
    assert (reg / "small.osm.pbf").read_text() == "pbf 10.0,10.0,11.0,11.0"
    assert _driver_ready(reg / "small.osm.pbf", "10.0,10.0,11.0,11.0")


def test_unknown_ids_are_skipped(tmp_path, capsys):
    root = _root(tmp_path)
    assert be.main(["--root", str(root), "--skip-pbf", "--skip-search", "--", "nowhere", "small"]) == 0
    assert "nowhere: no registry row" in capsys.readouterr().out
