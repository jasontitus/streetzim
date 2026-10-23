"""tools/langpack/nvme.py: the language step's room on the NVMe is counted
as parallel_rebuild.sh counts it (free, the runner's builds' projections,
the live reservations, the 100 GB kept free), its reservation is one the map
builds read, the base is copied and checked, dead runs' folders are swept."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.langpack.conftest import ROOT

NVME = ROOT / "tools" / "langpack" / "nvme.py"
GB = 1 << 30


def _start(pid: int) -> str:
    s = Path(f"/proc/{pid}/stat").read_text()
    return s[s.rindex(")") + 2:].split()[19]


@pytest.fixture
def nv(tmp_path: Path) -> SimpleNamespace:
    d = tmp_path
    for x in ("scratch", "acct", "par/status", "parscratch/w0", "packs"):
        (d / x).mkdir(parents=True)
    base = d / "osm-testland-2026-10-13.zim"
    base.write_bytes(os.urandom(1 << 20))
    (d / "packs" / "osm_testland_wiki-de_2026-10-13_aaaaaaaa.zim").write_bytes(b"x" * (1 << 19))
    free = 200 * GB                                    # what the volume has free, here
    env = {**os.environ, "LANGPACK_NVME_FREE": str(free), "LANGPACK_MEM_AVAILABLE": str(64 * GB),
           "NVME_SCRATCH": str(d / "acct"), "PAR_STATE": str(d / "par"),
           "PAR_SCRATCH": str(d / "parscratch"), "LANGPACK_MIN_FREE_GB": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    return SimpleNamespace(d=d, base=base, free=free, env=env)


def _plan(nv: SimpleNamespace, min_gb: float, jobs: int = 4, **extra: str) -> dict:
    r = subprocess.run([sys.executable, str(NVME), "plan", "--scratch", str(nv.d / "scratch"), "--base", str(nv.base),
                        "--packs-dir", str(nv.d / "packs"), "--gate-jobs", str(jobs), "--search-jobs", str(jobs)],
                       capture_output=True, text=True, timeout=60, env={**nv.env, "LANGPACK_NVME_MIN_GB": str(min_gb), **extra})
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_room_is_counted_as_the_runner_counts_it(nv: SimpleNamespace) -> None:
    """free - the runner's builds' still-to-write - live reservations - this
    run's need >= NVME_MIN: each term alone can take the room away."""
    room_gb = nv.free / GB
    p = _plan(nv, 0)
    assert p["use"] and p["gate_jobs"] == 4 and p["search_jobs"] == 4, p
    # what the volume keeps free (100 GB on the host): just more than there is
    assert not _plan(nv, room_gb - 0.001)["use"]
    # a running build of the parallel runner whose projection is more than the room
    (nv.d / "par" / "status" / "rome").write_text(f"running w0 123 456 789 ports=1,2 cpus=0-11 proj={int(room_gb) + 1}G\n")
    p = _plan(nv, 0)
    assert not p["use"] and p["runner_gb"] >= int(room_gb), p
    # ... less what its scratch holds already (here all of it: a sparse file of that size)
    with open(nv.d / "parscratch" / "w0" / "tiles.mbtiles", "wb") as fh:
        fh.truncate((int(room_gb) + 1) * GB)
    assert _plan(nv, 0)["use"]
    (nv.d / "parscratch" / "w0" / "tiles.mbtiles").unlink()
    assert not _plan(nv, 0)["use"]
    # a finished build counts nothing
    (nv.d / "par" / "status" / "rome").write_text(f"done w0 123 456 789 proj={int(room_gb) + 1}G\n")
    assert _plan(nv, 0)["use"]
    # a live reservation in the shared folder (a tile store's, as region-ml-tiles.py writes it) ...
    me = os.getpid()
    (nv.d / "acct" / f".reserve-ml-{me}").write_text(f"{nv.free} {_start(me)}\n")
    p = _plan(nv, 0)
    assert not p["use"] and p["reserved_gb"] > 0, p
    # ... but not a dead process's (another start time: a reused pid)
    (nv.d / "acct" / f".reserve-ml-{me}").write_text(f"{nv.free} 1\n")
    assert _plan(nv, 0)["use"]


def test_jobs_are_lowered_to_what_fits(nv: SimpleNamespace) -> None:
    """Each gate job needs a merged file and its copy (2 x base + packs +
    search pack): with room for the base and two jobs, two jobs."""
    p = _plan(nv, 0)
    assert p["per_gate"] == 2 * (p["base"] + (1 << 19) + p["base"] // 8), p     # base + packs + its search pack
    leave = nv.free - (p["base"] + 2 * p["per_gate"] + p["per_gate"] // 2)    # room for the base and 2.5 jobs
    p = _plan(nv, leave / GB)
    assert p["use"] and p["gate_jobs"] == 2, p
    # and to what MemAvailable allows above LANGPACK_MIN_FREE_GB (4 GB a gate job)
    mem = int(nv.env["LANGPACK_MEM_AVAILABLE"])
    p = _plan(nv, 0, LANGPACK_MIN_FREE_GB=str((mem - 9 * GB) / GB))
    assert p["gate_jobs"] == 2, p


def test_the_reservation_is_read_by_the_map_builds_and_goes_with_its_run(nv: SimpleNamespace) -> None:
    run = nv.d / "scratch" / "run-testland-1"
    run.mkdir()
    parent = subprocess.Popen(["sleep", "60"])
    h = subprocess.Popen([sys.executable, str(NVME), "hold", "--run-dir", str(run), "--need", str(5 * GB),
                          "--parent", str(parent.pid)], env=nv.env)
    try:
        f = nv.d / "acct" / f".reserve-ml-langpack-{h.pid}"
        t0 = time.time()
        while not f.exists() and time.time() - t0 < 30:
            time.sleep(0.1)
        b, st = f.read_text().split()
        assert int(b) == 5 * GB and st == _start(h.pid)
        # what parallel_rebuild.sh's start guard and build-region-fast.sh's stagers read
        sh = (ROOT / "ops" / "cloud" / "parallel_rebuild.sh").read_text()
        fn = sh[sh.index("ml_reserved_gb() {"):sh.index("\n}\n", sh.index("ml_reserved_gb() {")) + 3]
        r = subprocess.run(["bash", "-c", f'gib_up() {{ echo $(( (${{1:-0}} + 1073741823) / 1073741824 )); }}\n{fn}\nml_reserved_gb'],
                           capture_output=True, text=True, env={**nv.env, "ML_SCRATCH": str(nv.d / "acct")})
        assert r.stdout.strip() == "5", r.stdout + r.stderr
        bf = (ROOT / "ops" / "build-region-fast.sh").read_text()
        fn = bf[bf.index("nvme_ml_reserved() {"):bf.index("\n}\n", bf.index("nvme_ml_reserved() {")) + 3]
        r = subprocess.run(["bash", "-c", f"{fn}\nnvme_ml_reserved"], capture_output=True, text=True,
                           env={**nv.env, "NVME_SCRATCH": str(nv.d / "acct")})
        assert r.stdout.strip() == str(5 * GB), r.stdout + r.stderr
        # the run's own folder grows: the reservation shrinks by as much
        (run / "merged.zim").write_bytes(b"\0" * (1 << 20))
        t0 = time.time()
        while int(f.read_text().split()[0]) == 5 * GB and time.time() - t0 < 60:
            time.sleep(0.5)
        assert int(f.read_text().split()[0]) == 5 * GB - (1 << 20)
        parent.kill()
        parent.wait()
        assert h.wait(timeout=30) == 0                          # the run is gone: so is its reservation
        assert not f.exists() and not list((nv.d / "acct").iterdir())
    finally:
        for p in (parent, h):
            if p.poll() is None:
                p.kill()


def test_the_base_is_copied_and_checked(nv: SimpleNamespace) -> None:
    side = Path(str(nv.base) + ".parent.json")
    side.write_text('{"child_uuid": "x"}')
    os.utime(nv.base, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    r = subprocess.run([sys.executable, str(NVME), "stage", str(nv.base), str(nv.d / "scratch" / "b")],
                       capture_output=True, text=True, timeout=60, env=nv.env)
    assert r.returncode == 0, r.stderr
    out = Path(r.stdout.strip())
    assert out.name == nv.base.name and out.read_bytes() == nv.base.read_bytes()
    # the same identity the gates trust (base.identity: size and mtime) and the lineage sidecar beside it
    assert out.stat().st_mtime_ns == nv.base.stat().st_mtime_ns
    assert Path(str(out) + ".parent.json").read_text() == side.read_text()
    assert not list(out.parent.glob("*.part"))


def test_a_copy_that_does_not_match_is_refused(nv: SimpleNamespace, tmp_path: Path) -> None:
    """A write that loses bytes (here: every write of the copy short by a byte)
    fails the check: no staged base."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import builtins, io\n_o = builtins.open\n"
        "class W(io.FileIO):\n    def write(self, b):\n        return super().write(bytes(b)[:-1])\n"
        "def op(f, mode='r', *a, **k):\n"
        "    if str(f).endswith('.part') and 'w' in mode:\n        return W(f, 'w')\n"
        "    return _o(f, mode, *a, **k)\nbuiltins.open = op\n")
    r = subprocess.run([sys.executable, str(NVME), "stage", str(nv.base), str(nv.d / "scratch" / "b")],
                       capture_output=True, text=True, timeout=60, env={**nv.env, "PYTHONPATH": str(shim)})
    assert r.returncode != 0 and "does not match" in r.stderr, r.stdout + r.stderr
    assert not list((nv.d / "scratch" / "b").iterdir())


def test_sweep_removes_only_dead_runs(nv: SimpleNamespace) -> None:
    live = subprocess.Popen(["sleep", "60"])
    try:
        dead = subprocess.Popen(["true"])
        dead.wait()
        runs = {"live": nv.d / "scratch" / f"run-a-{live.pid}", "dead": nv.d / "scratch" / f"run-b-{dead.pid}",
                "reused": nv.d / "scratch" / f"run-c-{live.pid}x", "other": nv.d / "scratch" / f"run-d-{os.getpid()}"}
        for d in runs.values():
            d.mkdir()
        (runs["live"] / ".owner").write_text(f"{live.pid} {_start(live.pid)}\n")
        (runs["other"] / ".owner").write_text(f"{os.getpid()} 1\n")        # this pid, another start time
        r = subprocess.run([sys.executable, str(NVME), "sweep", "--scratch", str(nv.d / "scratch")],
                           capture_output=True, text=True, timeout=60, env=nv.env)
        assert r.returncode == 0, r.stderr
        assert runs["live"].exists() and not runs["dead"].exists() and not runs["other"].exists()
        assert not runs["reused"].exists()                  # no pid in its name: junk in our folder
    finally:
        live.kill()


def test_the_runners_state_comes_from_host_env(nv: SimpleNamespace) -> None:
    """The runner's state folder is LANGPACK_PAR_STATE, which host-env.sh sets
    for the build host (PAR_STATE, the runner's own name, overrides it)."""
    env = {k: v for k, v in nv.env.items() if k != "PAR_STATE"}
    (nv.d / "par" / "status" / "rome").write_text(f"running w0 1 2 3 proj={int(nv.free / GB) + 1}G\n")
    r = subprocess.run([sys.executable, str(NVME), "plan", "--scratch", str(nv.d / "scratch"), "--base", str(nv.base),
                        "--packs-dir", str(nv.d / "packs")], capture_output=True, text=True, timeout=60,
                       env={**env, "LANGPACK_NVME_MIN_GB": "0", "LANGPACK_PAR_STATE": str(nv.d / "par")})
    assert r.returncode == 0 and json.loads(r.stdout)["use"] is False, r.stdout + r.stderr
    r = subprocess.run(["env", "-i", f"HOME={os.environ['HOME']}", f"PATH={os.environ['PATH']}", "bash", "-c",
                        f". {ROOT}/ops/langpack/host-env.sh && echo $LANGPACK_PAR_STATE"], capture_output=True, text=True)
    assert r.stdout.strip() == "/storage/streetzim/rebuild-par", r.stdout + r.stderr
