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
    root.mkdir(parents=True, exist_ok=True)
    (root / "cgroup.controllers").write_text("cpu memory pids\n")   # a v2 root
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
    assert n == 16 // cpus.GIB_PER_CPU == 8
    assert "memory limit 16 GiB" in why
    # Less than one core's worth still gets one core.
    root, proc = cgroup(tmp_path / "small", "/", {"": {"memory.max": f"{GIB}\n"}})
    assert cpus.detect(root, proc)[0] == 1
    # Compression threads leave the memory rule out.
    assert cpus.detect(root, proc, memory_rule=False) == (36, "36 usable cores")


@pytest.mark.parametrize("limit, cores", [
    (6 * GIB, 3), (8 * GIB, 4), (10 * GIB, 5), (12 * GIB, 6), (14 * GIB, 7), (16 * GIB, 8),
    (int(7.99 * GIB), 4),        # rounded to 8 GiB first, not floored to 3 cores
    (16 * 10**9, 7),             # 14.9 GiB, as a limit given in decimal GB
    (int(9.4 * GIB), 4), (int(9.6 * GIB), 5), (3 * GIB, 1),
])
def test_memory_rule_for_the_recommended_recipes(tmp_path, monkeypatch, limit, cores):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    root, proc = cgroup(tmp_path, "/", {"": {"memory.max": f"{limit}\n"}})
    assert cpus.detect(root, proc)[0] == cores


def test_cpu_quota_rounds_up_to_whole_cores(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    root, proc = cgroup(tmp_path, "/", {"": {"cpu.max": "250000 100000\n"}})
    assert cpus.detect(root, proc) == (3, "36 usable cores, CPU quota 3")


def test_the_tightest_limit_of_any_ancestor_applies(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    root, proc = cgroup(tmp_path, "/a/b", {
        "a": {"memory.max": f"{8 * GIB}\n", "cpu.max": "600000 100000\n"},
        "a/b": {"memory.max": f"{64 * GIB}\n", "cpu.max": "max 100000\n"}})
    assert cpus.detect(root, proc)[0] == 4          # 8 GiB / 2, under the quota of 6


def test_the_smallest_quota_of_any_ancestor_applies(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    root, proc = cgroup(tmp_path, "/a/b", {
        "a": {"cpu.max": "300000 100000\n"},
        "a/b": {"cpu.max": "800000 100000\n"}})
    assert cpus.detect(root, proc) == (3, "36 usable cores, CPU quota 3")
    root, proc = cgroup(tmp_path / "inner", "/a/b", {
        "a": {"cpu.max": "800000 100000\n"},
        "a/b": {"cpu.max": "300000 100000\n"}})
    assert cpus.detect(root, proc)[0] == 3


def v1_tree(root, quota=None, period="100000", mem=None):
    for sub, name, val in (("cpu", "cpu.cfs_quota_us", quota), ("cpu", "cpu.cfs_period_us", period),
                           ("memory", "memory.limit_in_bytes", mem)):
        if val is not None:
            (root / sub).mkdir(parents=True, exist_ok=True)
            (root / sub / name).write_text(f"{val}\n")


def test_cgroup_v1_limits_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    proc = tmp_path / "cgroup"
    proc.write_text("12:memory:/docker/abc\n11:cpu,cpuacct:/docker/abc\n")
    v1_tree(tmp_path / "a", quota="250000", mem=str(16 * GIB))
    assert cpus.detect(str(tmp_path / "a"), str(proc)) == (3, "36 usable cores, CPU quota 3")
    v1_tree(tmp_path / "b", quota="-1", mem=str(9 * GIB))
    assert cpus.detect(str(tmp_path / "b"), str(proc))[0] == 4
    # "No limit" in v1 is a huge number, not a limit.
    v1_tree(tmp_path / "c", quota="-1", mem="9223372036854771712")
    assert cpus.detect(str(tmp_path / "c"), str(proc)) == (36, "36 usable cores")


def test_hybrid_host_reads_v1_not_the_missing_v2_files(tmp_path, monkeypatch):
    # A 0:: line, but the root is a v1 tmpfs (no cgroup.controllers).
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    proc = tmp_path / "cgroup"
    proc.write_text("12:memory:/docker/abc\n0::/docker/abc\n")
    root = tmp_path / "cg"
    (root / "docker" / "abc").mkdir(parents=True)
    v1_tree(root, mem=str(6 * GIB))
    assert cpus.detect(str(root), str(proc))[0] == 3


def test_usable_cores_follow_the_affinity_mask(monkeypatch):
    monkeypatch.delattr(cpus.os, "process_cpu_count", raising=False)
    monkeypatch.setattr(cpus.os, "sched_getaffinity", lambda pid: {0, 5}, raising=False)
    assert cpus._usable_cores() == 2
    monkeypatch.setattr(cpus.os, "process_cpu_count", lambda: 3, raising=False)
    assert cpus._usable_cores() == 3


def test_without_limits_the_count_is_what_it_was(tmp_path, monkeypatch):
    # The production build host: no cgroup limits, full affinity. The count
    # must be os.cpu_count(), as every pool used before.
    monkeypatch.delattr(cpus.os, "process_cpu_count", raising=False)
    monkeypatch.setattr(cpus.os, "sched_getaffinity",
                        lambda pid: set(range(cpus.os.cpu_count() or 1)), raising=False)
    root, proc = cgroup(tmp_path, "/user.slice", {"user.slice": {"memory.max": "max\n",
                                                                  "cpu.max": "max 100000\n"}})
    assert cpus.detect(root, proc)[0] == cpus.os.cpu_count()


def test_a_cgroup_name_that_is_not_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 4)
    root, _ = cgroup(tmp_path, "/", {"": {"memory.max": f"{64 * GIB}\n"}})
    proc = tmp_path / "cgroup-bytes"
    proc.write_bytes(b"0::/\xff\xfe\n")
    assert cpus.detect(root, str(proc))[0] == 4


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


def test_compression_cpus_keep_the_quota_and_the_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "_usable_cores", lambda: 36)
    root, proc = cgroup(tmp_path, "/", {"": {"memory.max": f"{4 * GIB}\n",
                                             "cpu.max": "600000 100000\n"}})
    assert cpus.detect(root, proc)[0] == 2
    assert cpus.detect(root, proc, memory_rule=False)[0] == 6
    monkeypatch.setattr(cpus, "detect", lambda memory_rule=True: (2 if memory_rule else 6, ""))
    assert (cpus.build_cpus(), cpus.compression_cpus()) == (2, 6)
    cpus.set_build_cpus(3)
    assert (cpus.build_cpus(), cpus.compression_cpus()) == (3, 3)


def test_cpus_flag_wins_and_none_goes_back_to_detecting(monkeypatch):
    monkeypatch.setattr(cpus, "detect", lambda memory_rule=True: (7, "detected"))
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
# how tilemaker reached 7.9 GB on a small map; so does a pool given no size.
# streetzim/satellite.py is left out on purpose: its threads wait on the
# network and hold little memory.
BUILDER = sorted({str(p.relative_to(ROOT)) for p in (ROOT / "streetzim").rglob("*.py")}
                 - {"streetzim/satellite.py", "streetzim/cpus.py"}) + ["create_osm_zim.py"]
MACHINE_SIZED = re.compile(
    r"cpu_count\s*\(|sched_getaffinity"
    r"|\b(?:Pool|ProcessPoolExecutor|ThreadPoolExecutor)\(\s*\)"
    r"|max_workers\s*=\s*None|\.Pool\(\s*\)")


@pytest.mark.parametrize("path", BUILDER)
def test_builder_never_sizes_a_pool_from_the_machine(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    m = MACHINE_SIZED.search(src)
    assert not m, f"{path}: {m.group(0)!r} sizes by the machine; use build_cpus()"


@pytest.mark.parametrize("text", ["os.cpu_count()", "os.process_cpu_count()",
                                  "len(os.sched_getaffinity(0))", "Pool()",
                                  "ctx.Pool()", "ThreadPoolExecutor(max_workers=None)",
                                  "ProcessPoolExecutor( )"])
def test_the_guard_catches_each_machine_sized_form(text):
    assert MACHINE_SIZED.search(text)


def test_builder_files_are_found():
    assert "streetzim/zim_writer.py" in BUILDER and "streetzim/routing/build.py" in BUILDER


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


def test_main_sets_the_count_and_a_second_run_does_not_keep_it(monkeypatch):
    import create_osm_zim

    class Stop(Exception):
        pass

    def stop(**kw):
        raise Stop
    monkeypatch.setattr(create_osm_zim, "_openzim_options", stop)
    monkeypatch.setattr(cpus, "detect", lambda memory_rule=True: (7, "detected"))
    with pytest.raises(Stop):
        create_osm_zim.main(["--cpus", "3"])
    assert cpus.build_cpus() == 3
    with pytest.raises(Stop):
        create_osm_zim.main([])
    assert cpus.build_cpus() == 7


def test_zim_writer_sizes_compression_without_the_memory_rule():
    src = (ROOT / "streetzim" / "zim_writer.py").read_text(encoding="utf-8")
    assert "num_workers = zim_workers or min(compression_cpus(), 20)" in src
    assert "ThreadPoolExecutor(max_workers=compression_cpus())" in src
