"""streetzim/cpus.py: how many cores a build uses, and that every parallel
step asks it instead of os.cpu_count()."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import cpus  # noqa: E402

GIB = 1 << 30


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.setattr(cpus, "_requested", None)


def cgroup(tmp_path, rel, files):
    """A fake cgroup v2 tree: `files` maps a directory under the root ("" is
    the root) to its {name: content}; this process sits in `rel`."""
    root = tmp_path / "cg"
    for d, fs in files.items():
        (root / d).mkdir(parents=True, exist_ok=True)
        for name, text in fs.items():
            (root / d / name).write_text(text)
    (root / rel.strip("/")).mkdir(parents=True, exist_ok=True)
    proc = tmp_path / "cgroup"
    proc.write_text(f"0::{rel}\n")
    return str(root), str(proc)


def test_no_limits_uses_every_usable_core(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    root, proc = cgroup(tmp_path, "/user.slice/session-1.scope", {
        "user.slice": {"memory.max": "max\n", "cpu.max": "max 100000\n"},
        "user.slice/session-1.scope": {"memory.max": "max\n", "cpu.max": "max 100000\n"}})
    assert cpus.detect(root, proc) == (36, "36 usable cores")


def test_memory_limit_allows_one_core_per_gib_per_cpu(tmp_path, monkeypatch):
    # docker run --memory 16g --cpu-shares 3072: the share (cpu.weight) is
    # not read, the memory limit is.
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    root, proc = cgroup(tmp_path, "/", {"": {"memory.max": f"{16 * GIB}\n",
                                             "cpu.max": "max 100000\n",
                                             "cpu.weight": "240\n"}})
    n, why = cpus.detect(root, proc)
    assert n == 16 // cpus.GIB_PER_CPU == 4
    assert "memory limit 16.0 GiB" in why
    # Less than one core's worth still gets one core.
    root, proc = cgroup(tmp_path / "small", "/", {"": {"memory.max": f"{GIB}\n"}})
    assert cpus.detect(root, proc)[0] == 1


def test_cpu_quota_rounds_up_to_whole_cores(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    root, proc = cgroup(tmp_path, "/", {"": {"cpu.max": "250000 100000\n"}})
    assert cpus.detect(root, proc) == (3, "36 usable cores, CPU quota 3")


def test_the_tightest_limit_of_any_ancestor_applies(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    root, proc = cgroup(tmp_path, "/a/b", {
        "a": {"memory.max": f"{8 * GIB}\n", "cpu.max": "600000 100000\n"},
        "a/b": {"memory.max": f"{64 * GIB}\n", "cpu.max": "max 100000\n"}})
    assert cpus.detect(root, proc)[0] == 2          # 8 GiB / 4, under the quota of 6


def test_fewer_usable_cores_than_the_limits_allow(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 2)
    root, proc = cgroup(tmp_path, "/", {"": {"memory.max": f"{64 * GIB}\n",
                                             "cpu.max": "800000 100000\n"}})
    assert cpus.detect(root, proc) == (2, "2 usable cores")


def test_no_unified_cgroup_or_unreadable_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 8)
    v1 = tmp_path / "cgroup-v1"
    v1.write_text("12:memory:/docker/abc\n11:cpu,cpuacct:/docker/abc\n")
    assert cpus.detect(str(tmp_path), str(v1))[0] == 8
    assert cpus.detect(str(tmp_path), str(tmp_path / "missing"))[0] == 8
    root, proc = cgroup(tmp_path / "junk", "/", {"": {"memory.max": "lots\n",
                                                     "cpu.max": "x y\n"}})
    assert cpus.detect(root, proc)[0] == 8


def test_cpus_flag_wins_and_none_goes_back_to_detecting(monkeypatch):
    monkeypatch.setattr(cpus, "detect", lambda: (7, "detected"))
    assert cpus.build_cpus() == 7
    cpus.set_build_cpus(3)
    assert cpus.build_cpus() == 3
    cpus.set_build_cpus(None)
    assert cpus.build_cpus() == 7
    with pytest.raises(ValueError):
        cpus.set_build_cpus(0)


def test_this_machine_detects_at_least_one_core():
    n, why = cpus.detect()
    assert n >= 1 and "usable cores" in why


# Every parallel step of the builder sizes itself from build_cpus(). A bare
# os.cpu_count() sees every core of the machine inside a container, which is
# how tilemaker reached 7.9 GB on a small map. streetzim/satellite.py is left
# out on purpose: its threads wait on the network and hold little memory.
BUILDER = ["create_osm_zim.py", "streetzim/search_extract.py", "streetzim/terrain.py",
           "streetzim/zim_writer.py", "streetzim/tiles.py", "streetzim/addresses.py",
           "streetzim/overture.py", "streetzim/mbtiles.py", "streetzim/routing/build.py",
           "streetzim/cli.py", "streetzim/common.py"]


@pytest.mark.parametrize("path", BUILDER)
def test_builder_never_sizes_a_pool_from_the_machine(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    assert not re.search(r"\bcpu_count\s*\(", src), f"{path} calls cpu_count(); use build_cpus()"


def test_tilemaker_gets_the_thread_count(tmp_path, monkeypatch):
    from streetzim import tiles
    runs = []
    monkeypatch.setattr(tiles.subprocess, "run", lambda cmd, check: runs.append(cmd))
    monkeypatch.setattr(tiles.os.path, "getsize", lambda p: 0)
    monkeypatch.setattr(tiles, "required_shapefiles", lambda: [])
    cpus.set_build_cpus(3)
    tiles.generate_tiles("in.pbf", str(tmp_path / "t.mbtiles"), bbox="7.40,43.72,7.44,43.76")
    (cmd,) = runs
    assert cmd[cmd.index("--threads") + 1] == "3"


def test_create_osm_zim_cpus_flag(monkeypatch, capsys):
    import create_osm_zim
    assert create_osm_zim.build_parser().parse_args(["--cpus", "5"]).cpus == 5
    with pytest.raises(SystemExit):
        create_osm_zim.main(["--cpus", "0"])
    assert "--cpus must be at least 1" in capsys.readouterr().err
