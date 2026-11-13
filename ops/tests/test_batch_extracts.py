"""ops/batch_extracts.py: the queue's search files in one pass over the world
file, and its extracts a few regions per planet pass, installed the way the
driver installs its own (so the driver finds them and skips its cut), with
the list of what is still to come that the runner holds builds back on."""
import fcntl
import json
import os
import stat
import subprocess
import sys

import pytest

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


def test_search_keeps_the_box_with_a_margin_and_writes_the_sidecars(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _world(root, [_rec("in", 10.5, 10.5), _rec("edge", 11.005, 10.0), _rec("out", 12.0, 10.5),
                  _rec("other", -4.0, 21.0), _rec("east", -19.0, 179.5), _rec("west", -19.0, -179.5),
                  _rec("far", -19.0, 0.0)])
    pending = tmp_path / "pending"
    pending.write_text("small\n")  # the extracts' list: not this run's to touch
    seen = []
    cut = be._cut_search
    monkeypatch.setattr(be, "_cut_search", lambda *a: (seen.append((tmp_path / "pending.search").read_text()), cut(*a)))
    be.main(["--root", str(root), "--skip-pbf", "--pending", str(pending), "--", "small", "other", "wrap"])
    assert seen == ["small\nother\nwrap\n"] and not (tmp_path / "pending.search").exists()
    assert pending.read_text() == "small\n"
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
    (reg / "other.search.jsonl.bbox").write_text("20.0,-5.0,22.0,-3.0\n")
    (reg / "other.search.jsonl.schema").write_text('{"search_schema": 2}')
    be.main(["--root", str(root), "--skip-pbf", "--", "small", "other"])
    assert not os.path.islink(reg / "small.search.jsonl")
    assert "in" in (reg / "small.search.jsonl").read_text()
    assert (reg / "other.search.jsonl").read_text() == "mine\n"
    assert not list(reg.glob("*.stale-*"))
    assert world.read_text().count("\n") == 2  # the world file is untouched


def test_a_search_cut_the_driver_would_refuse_is_parked_and_cut_again(tmp_path):
    """Cut for another box, or without native-script names (no schema-2
    sidecar): the driver would park it and read the whole world file."""
    root = _root(tmp_path)
    _world(root, [_rec("in", 10.5, 10.5), _rec("other", -4.0, 21.0)])
    reg = root / "world-data/regions"
    (reg / "small.search.jsonl").write_text("old box\n")
    (reg / "small.search.jsonl.bbox").write_text("9.0,9.0,11.0,11.0\n")
    (reg / "small.search.jsonl.schema").write_text('{"search_schema": 2}')
    (reg / "other.search.jsonl").write_text("old schema\n")
    (reg / "other.search.jsonl.bbox").write_text("20.0,-5.0,22.0,-3.0\n")
    be.main(["--root", str(root), "--skip-pbf", "--", "small", "other"])
    assert "in" in (reg / "small.search.jsonl").read_text()
    assert (reg / "small.search.jsonl.bbox").read_text() == "10.0,10.0,11.0,11.0\n"
    assert "other" in (reg / "other.search.jsonl").read_text()
    assert '"search_schema": 2' in (reg / "other.search.jsonl.schema").read_text()
    parked = sorted(p.name for p in reg.glob("*.stale-*"))
    assert [p.split(".stale-")[0] for p in parked if not p.endswith((".bbox", ".schema"))] == [
        "other.search.jsonl", "small.search.jsonl"]
    kept = {p.read_text() for p in reg.glob("*.stale-*") if not p.name.endswith((".bbox", ".schema"))}
    assert kept == {"old box\n", "old schema\n"}
    assert [p.read_text() for p in reg.glob("small.search.jsonl.stale-*.bbox")] == ["9.0,9.0,11.0,11.0\n"]


def test_no_search_cut_from_a_world_file_without_native_script_names(tmp_path):
    root = _root(tmp_path)
    world = _world(root, [_rec("in", 10.5, 10.5)])
    (root / (be.WORLD_SEARCH + ".schema")).write_text('{"search_schema": 1}')
    reg = root / "world-data/regions"
    os.symlink(world, reg / "small.search.jsonl")
    be.main(["--root", str(root), "--skip-pbf", "--", "small"])
    assert os.path.islink(reg / "small.search.jsonl")


def _fake_osmium(tmp_path, fail="False", pending=None, nap=0):
    """Logs each pass (its outputs, the strategy and, with `pending`, the ids
    then in that file and in its .search twin); fails when `fail` (an
    expression of n, the number of extracts) is true."""
    log = tmp_path / "osmium.log"
    p = tmp_path / "osmium"
    p.write_text(f"""#!/usr/bin/env python3
import json, os, sys, time
a = sys.argv[1:]
cfg = json.load(open(a[a.index("-c") + 1]))
n = len(cfg["extracts"])
line = " ".join(e["output"] for e in cfg["extracts"]) + " " + a[a.index("-s") + 1]
for f in ({str(pending)!r}, {str(pending)!r} + ".search") if {pending is not None!r} else ():
    line += " | " + (" ".join(open(f).read().split()) if os.path.exists(f) else "-")
open({str(log)!r}, "a").write(line + "\\n")
time.sleep({nap!r})
if {fail}:
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
    real = os.replace

    def replace(src, dst):  # the marks are there before the file is
        if str(dst).endswith(".osm.pbf"):
            assert os.path.exists(f"{dst}.strategy") and os.path.exists(f"{dst}.bbox"), dst
        return real(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    be.main(["--root", str(root), "--skip-search", "--batch", "1", "--", "small", "other", "wrap"])
    monkeypatch.setattr(os, "replace", real)
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
    osm, log = _fake_osmium(tmp_path, fail="True")
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


REGISTRY4 = REGISTRY + "third\tThird\t30.0,10.0,31.0,11.0\tcountry\nfourth\tFourth\t40.0,10.0,41.0,11.0\tcountry\n"


def _planet(tmp_path, registry=REGISTRY4):
    root = _root(tmp_path)
    (root / "cloud/regions.tsv").write_text(registry)
    (root / be.PLANET).parent.mkdir(parents=True, exist_ok=True)
    (root / be.PLANET).write_text("planet")
    return root


def test_the_pending_lists_name_what_is_still_to_come_and_go_when_it_is_there(tmp_path, monkeypatch, capfd):
    """The runner holds a region back while its id is in --pending (extracts)
    or its .search twin: each pass sees the regions after it, a ready extract
    is never listed, and the search pass runs beside the planet passes."""
    root = _planet(tmp_path)
    _world(root, [_rec("in", 10.5, 10.5), _rec("other", -4.0, 21.0)])
    reg = root / "world-data/regions"
    (reg / "fourth.osm.pbf").write_text("ready")
    st = (reg / "fourth.osm.pbf").stat()
    (reg / "fourth.osm.pbf.strategy").write_text(f"smart {st.st_size} {int(st.st_mtime)}\n")
    (reg / "fourth.search.jsonl").write_text("mine\n")
    (reg / "fourth.search.jsonl.schema").write_text('{"search_schema": 2}')
    pending = tmp_path / "state.pending"
    pending.write_text("small\nother\nthird\nfourth\n")   # as the runner leaves it
    osm, log = _fake_osmium(tmp_path, pending=pending, nap=1)
    monkeypatch.setenv("STREETZIM_OSMIUM", str(osm))
    be.main(["--root", str(root), "--batch", "2", "--pending", str(pending), "--", "small", "other", "third", "fourth"])
    passes = [ln.split(" | ") for ln in log.read_text().splitlines()]
    assert [p[:2] for p in passes] == [["small.osm.pbf other.osm.pbf smart", "small other third"],
                                       ["third.osm.pbf smart", "third"]]
    # the search pass, in a process of its own, cuts for the same three: fourth has its own
    assert f"search: one pass over {be.WORLD_SEARCH} for 3 regions" in capfd.readouterr().out
    assert (reg / "fourth.search.jsonl").read_text() == "mine\n"
    assert not pending.exists() and not (tmp_path / "state.pending.search").exists()
    assert "in" in (reg / "small.search.jsonl").read_text()
    assert _driver_ready(reg / "third.osm.pbf", "30.0,10.0,31.0,11.0")


def test_a_failed_pass_is_tried_once_more_as_two_halves(tmp_path, monkeypatch):
    root = _planet(tmp_path)
    pending = tmp_path / "state.pending"
    osm, log = _fake_osmium(tmp_path, fail="n > 2", pending=pending)   # as when 17 at once ran out of memory
    monkeypatch.setenv("STREETZIM_OSMIUM", str(osm))
    be.main(["--root", str(root), "--skip-search", "--batch", "3", "--pending", str(pending),
             "--", "small", "other", "third", "fourth"])
    assert [ln.split(" | ")[:2] for ln in log.read_text().splitlines()] == [
        ["small.osm.pbf other.osm.pbf third.osm.pbf smart", "small other third fourth"],
        ["small.osm.pbf other.osm.pbf smart", "small other third fourth"],
        ["third.osm.pbf smart", "third fourth"],
        ["fourth.osm.pbf smart", "fourth"]]
    reg = root / "world-data/regions"
    assert all((reg / f"{r}.osm.pbf").exists() for r in ("small", "other", "third", "fourth"))
    assert not pending.exists()


def test_two_failed_passes_in_a_row_leave_the_rest_to_the_builds(tmp_path, monkeypatch, capsys):
    """Both halves of a failed pass failing too is not one region's fault."""
    root = _planet(tmp_path)
    pending = tmp_path / "state.pending"
    osm, log = _fake_osmium(tmp_path, fail="True", pending=pending)
    monkeypatch.setenv("STREETZIM_OSMIUM", str(osm))
    assert be.main(["--root", str(root), "--skip-search", "--batch", "4", "--pending", str(pending),
                    "--", "small", "other", "third", "fourth", "wrap"]) == 0
    assert [ln.split(" | ")[0] for ln in log.read_text().splitlines()] == [   # halves are not halved again
        "small.osm.pbf other.osm.pbf third.osm.pbf fourth.osm.pbf smart",
        "small.osm.pbf other.osm.pbf smart", "third.osm.pbf fourth.osm.pbf smart"]
    out = capsys.readouterr().out
    assert "left to their builds: small other\n" in out and "in a row; left to their builds: third fourth\n" in out
    assert not list((root / "world-data/regions").glob("*.osm.pbf")) and not pending.exists()


def test_one_region_that_cannot_be_cut_costs_only_itself(tmp_path, monkeypatch, capsys):
    """Wherever it sits in its pass: the other half and the later passes are
    still cut."""
    root = _planet(tmp_path)
    bad = 'any(e["output"] == "small.osm.pbf" for e in cfg["extracts"])'
    osm, log = _fake_osmium(tmp_path, fail=bad)
    monkeypatch.setenv("STREETZIM_OSMIUM", str(osm))
    be.main(["--root", str(root), "--skip-search", "--batch", "2", "--", "small", "other", "third", "fourth"])
    assert log.read_text().splitlines() == [
        "small.osm.pbf other.osm.pbf smart", "small.osm.pbf smart", "other.osm.pbf smart",
        "third.osm.pbf fourth.osm.pbf smart"]
    reg = root / "world-data/regions"
    assert sorted(p.name for p in reg.glob("*.osm.pbf")) == ["fourth.osm.pbf", "other.osm.pbf", "third.osm.pbf"]
    assert "left to their builds: small\n" in capsys.readouterr().out


def test_a_pass_that_never_ends_is_stopped_and_not_tried_again_as_halves(tmp_path, monkeypatch, capsys):
    root = _planet(tmp_path)
    osm, log = _fake_osmium(tmp_path, nap=30)
    monkeypatch.setenv("STREETZIM_OSMIUM", str(osm))
    monkeypatch.setattr(be, "PASS_TIMEOUT_S", 1)
    be.main(["--root", str(root), "--skip-search", "--batch", "2", "--", "small", "other", "third", "fourth"])
    out = capsys.readouterr().out
    assert "osmium rc=timeout" in out and "left to their builds: third fourth\n" in out
    assert log.read_text().splitlines() == ["small.osm.pbf other.osm.pbf smart", "third.osm.pbf fourth.osm.pbf smart"]
    assert not list((root / "world-data/regions").glob("*.osm.pbf"))


def test_a_second_batch_in_the_same_root_does_nothing(tmp_path, monkeypatch, capsys):
    """One that outlived its runner is still writing these files: the next
    one neither cuts nor touches the lists."""
    root = _planet(tmp_path)
    _world(root, [_rec("in", 10.5, 10.5)])
    osm, log = _fake_osmium(tmp_path)
    monkeypatch.setenv("STREETZIM_OSMIUM", str(osm))
    pending = tmp_path / "state.pending"
    for lock, args in ((".batch-extracts.lock", ["--skip-search"]), (".batch-search.lock", ["--skip-pbf"])):
        pending.write_text("small\n")
        (tmp_path / "state.pending.search").write_text("small\n")
        with open(root / "tmp" / lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            assert be.main(["--root", str(root), *args, "--pending", str(pending), "--", "small"]) == 3
        assert "another batch is cutting" in capsys.readouterr().out
        assert pending.read_text() == (tmp_path / "state.pending.search").read_text() == "small\n"
        assert not log.exists() and not list((root / "world-data/regions").iterdir())
    # and with the locks free again, both parts are cut
    assert be.main(["--root", str(root), "--pending", str(pending), "--", "small"]) == 0
    reg = root / "world-data/regions"
    assert (reg / "small.osm.pbf").exists() and "in" in (reg / "small.search.jsonl").read_text()


def test_a_search_cut_that_cannot_be_installed_leaves_no_parts(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _world(root, [_rec("in", 10.5, 10.5), _rec("other", -4.0, 21.0)])
    real = be.shutil.copyfile

    def full_disk(src, dst):
        if "other" in os.path.basename(dst):
            raise OSError(28, "No space left on device")
        return real(src, dst)
    monkeypatch.setattr(be.shutil, "copyfile", full_disk)
    with pytest.raises(OSError):
        be.main(["--root", str(root), "--skip-pbf", "--", "small", "other"])
    reg = root / "world-data/regions"
    assert sorted(p.name for p in reg.iterdir()) == [
        "small.search.jsonl", "small.search.jsonl.bbox", "small.search.jsonl.schema"]


def test_unknown_ids_are_skipped(tmp_path, capsys):
    root = _root(tmp_path)
    assert be.main(["--root", str(root), "--skip-pbf", "--skip-search", "--", "nowhere", "small"]) == 0
    assert "nowhere: no registry row" in capsys.readouterr().out
