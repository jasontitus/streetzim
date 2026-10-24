"""ops/cloud/parallel_rebuild.sh: several single-region drivers at once.

The real runner runs here with a fake driver (PAR_DRIVER) that records what
it was given and appends a rebuild-old.tsv row the way the real driver does,
and with free NVMe space, MemAvailable and the checkout's HEAD read from
files the test changes (PAR_FREE_GB_CMD, PAR_MEM_GB_CMD, PAR_HEAD_CMD).
Also: the driver's new knobs (its list, log, pid file, ports, REBUILD_NO_SKIP,
REBUILD_STAGE_MBTILES, REBUILD_REGATE_ZIM, locked appends), its gate() retrying
a browser gate that timed out (and never one with a finding), the runner's
--regate mode, and build-region-fast.sh's
BUILD_SCRATCH copy and BUILD_WIKI_ZIM, each run from a copy whose
/storage/streetzim is a temp dir.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "ops" / "cloud" / "parallel_rebuild.sh"
DRIVER = ROOT / "ops" / "cloud" / "rebuild_old_regions.sh"
WRAPPER = ROOT / "ops" / "build-region-fast.sh"

FAKE_DRIVER = r"""#!/bin/bash
# Fake single-region driver. Knobs it stands in for: REBUILD_LIST REBUILD_PIDFILE
# REBUILD_LOG REBUILD_OV_PORT REBUILD_KW_PORT REBUILD_NO_SKIP REBUILD_STAGE_MBTILES
# REBUILD_REGATE_ZIM
id=$(head -1 "$REBUILD_LIST")
echo "start $id $(date +%s.%N) $$ scratch=${BUILD_SCRATCH-unset} ports=$REBUILD_OV_PORT,$REBUILD_KW_PORT ml=${STREETZIM_ML_TILES:-} noskip=${REBUILD_NO_SKIP:-} stage=${REBUILD_STAGE_MBTILES:-} reqwiki=${STREETZIM_REQUIRE_WIKI:-} wikimax=${STREETZIM_WIKI_MAX_PER_MIN:-} keep=${STREETZIM_KEEP_ZIMS:-} wikizim=${BUILD_WIKI_ZIM-unset} ln=${STREETZIM_SEARCH_LN-unset} zstd=${ZSTD_CLEVEL-unset} cache=${STREETZIM_SEARCH_CACHE-unset} rel=${OVERTURE_RELEASE-unset} cpus=$(taskset -cp $$ | sed 's/.*: //') regate=${REBUILD_REGATE_ZIM-unset}" >> "$FAKE_LOG"
echo $$ > "$REBUILD_PIDFILE"
if [ -n "${BUILD_SCRATCH:-}" ]; then mkdir -p "$BUILD_SCRATCH/tmp"; head -c 1024 /dev/zero > "$BUILD_SCRATCH/tmp/blob"; fi
if [ -e "$FAKE_DIR/$id.uploading" ]; then bash -c 'exec -a upload_validated.sh sleep 30' & fi
d=$(cat "$FAKE_DIR/$id.sleep" 2>/dev/null || echo 2.5)
end=$(( $(date +%s%N) / 1000000 + ${d%.*}000 + 10#${d#*.}00 ))
while [ $(( $(date +%s%N) / 1000000 )) -lt $end ]; do sleep 0.1; done
[ -e "$FAKE_DIR/$id.uploading" ] && kill %1 2>/dev/null
st=$(cat "$FAKE_DIR/$id.status" 2>/dev/null || echo uploaded)
printf "%s\t%s\t%s\t%s\n" "$id" "$st" "osm-$id-2026-10-23.zim" "$(date -Iseconds)" >> "$FAKE_TSV"
echo "end $id $(date +%s.%N)" >> "$FAKE_LOG"
"""


def _env(tmp: Path, **kw: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()}
    env.update({
        "PAR_ROOT": str(tmp / "root"), "PAR_STATE": str(tmp / "state"),
        "PAR_SCRATCH": str(tmp / "state" / "scratch"), "PAR_ML_SCRATCH": str(tmp / "ml"),
        "PAR_DRIVER": str(tmp / "driver.sh"), "PAR_WRAPPER": str(tmp / "wrapper.sh"),
        "PAR_UPLOADER": str(tmp / "uploader.sh"), "PAR_TSV": str(tmp / "rebuild-old.tsv"),
        "PAR_VARIANTS": str(tmp / "variants.tsv"), "PAR_REGISTRY": str(tmp / "regions.tsv"),
        "PAR_RUNSH_OUT": str(tmp / "run.out"),
        "PAR_RUNSH": str(tmp / "sz-tests" / "rebuild-all" / "run.sh"),
        "PAR_FREE_GB_CMD": f"cat {tmp}/free", "PAR_MEM_GB_CMD": f"cat {tmp}/mem",
        "PAR_POLL": "0.2", "PAR_STAGGER": "0", "PAR_MEMWATCH_OFF": "1",
        "PAR_WORKERS": "2", "PAR_PORT_BASE": "19811", "PAR_CPUS_PER_WORKER": "2",
        "PAR_WIKI_NVME": "0", "PAR_PRECUT": "0", "PAR_PENDING_EVERY": "1",
        "FAKE_LOG": str(tmp / "fake.log"), "FAKE_DIR": str(tmp / "fake"),
        "FAKE_TSV": str(tmp / "rebuild-old.tsv"),
        # must not reach the driver
        "STREETZIM_SEARCH_LN": "0", "ZSTD_CLEVEL": "3", "STREETZIM_SEARCH_CACHE": "/nope",
        "OVERTURE_RELEASE": "2000-01-01.0",
    })
    env.update(kw)
    return env


def _setup(tmp: Path, queue: list[str] | str, *, free: int = 1000, mem: int = 100,
           sizes: dict[str, str] | None = None, variants: str = "") -> None:
    for d in ("root", "state", "fake", "ml"):
        (tmp / d).mkdir()
    text = queue if isinstance(queue, str) else "\n".join(queue) + "\n"
    (tmp / "state" / "queue.txt").write_text("# test queue\n" + text)
    (tmp / "driver.sh").write_text(FAKE_DRIVER)
    (tmp / "driver.sh").chmod(0o755)
    (tmp / "wrapper.sh").write_text("# BUILD_SCRATCH BUILD_WIKI_ZIM\n")
    (tmp / "uploader.sh").write_text("# STREETZIM_KEEP_ZIMS\n")
    (tmp / "rebuild-old.tsv").write_text("id\tstatus\tzim\tfinished\n")
    (tmp / "free").write_text(f"{free}\n")
    (tmp / "mem").write_text(f"{mem}\n")
    (tmp / "variants.tsv").write_text("# id\tparent\tsatellite\tmax_zoom\n" + variants)
    (tmp / "regions.tsv").write_text("# id\tname\tbbox\n")
    for rid, size in (sizes or {}).items():
        subprocess.run(["truncate", "-s", size, str(tmp / "root" / f"osm-{rid}-2026-01-01.zim")], check=True)


def _run(tmp: Path, env: dict[str, str], timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(RUNNER)], env=env, cwd=tmp, capture_output=True,
                          text=True, timeout=timeout)


def _bg(tmp: Path, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(["bash", str(RUNNER)], env=env, cwd=tmp,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _starts(tmp: Path) -> list[tuple[str, float, float, str]]:
    starts, ends = {}, {}
    for line in (tmp / "fake.log").read_text().splitlines():
        f = line.split()
        if f[0] == "start":
            starts.setdefault(f[1], []).append((float(f[2]), line))
        elif f[0] == "end":
            ends.setdefault(f[1], []).append(float(f[2]))
    out = []
    for rid, lst in starts.items():
        for i, (t, line) in enumerate(lst):
            e = ends.get(rid, [])
            out.append((rid, t, e[i] if i < len(e) else float("inf"), line))
    return sorted(out, key=lambda x: x[1])


def _max_concurrent(runs) -> int:
    ev = sorted([(s, 1) for _, s, _, _ in runs] + [(e, -1) for _, _, e, _ in runs])
    n = m = 0
    for _, d in ev:
        n += d
        m = max(m, n)
    return m


def _wait_log(tmp: Path, needle: str, secs: float = 15) -> str:
    deadline = time.time() + secs
    out = ""
    while time.time() < deadline:
        p = tmp / "state" / "run.out"
        out = p.read_text() if p.exists() else ""
        if needle in out:
            return out
        time.sleep(0.1)
    return out


def _wait_starts(tmp: Path, n: int, secs: float = 15) -> None:
    deadline = time.time() + secs
    while time.time() < deadline:
        f = tmp / "fake.log"
        if f.exists() and f.read_text().count("start ") >= n:
            return
        time.sleep(0.1)


def _status(tmp: Path, rid: str) -> str:
    return (tmp / "state" / "status" / rid).read_text().split()[0]


def _statuses(tmp: Path) -> dict[str, str]:
    d = tmp / "state" / "status"
    if not d.exists():
        return {}
    return {f.name: f.read_text().split()[0] for f in d.iterdir()
            if not f.name.endswith((".tmp", ".mult"))}


def test_lanes_cap_ports_scratch_cpus_and_scrubbed_env(tmp_path):
    q = ["a", "b", "c", "d", "e"]
    _setup(tmp_path, q)
    r = _run(tmp_path, _env(tmp_path))
    assert r.returncode == 0, (tmp_path / "state" / "run.out").read_text()
    runs = _starts(tmp_path)
    # worker 0 takes the largest (last) remaining region, worker 1 the smallest
    assert [x[0] for x in runs] == ["e", "a", "d", "b", "c"]
    assert _max_concurrent(runs) == 2
    lines = [x[3] for x in runs]
    for line in lines:
        for want in ("ml=1", "noskip=1", "stage=1", "reqwiki=1", "wikimax=40", "keep=3",
                     "ln=unset", "zstd=unset", "cache=unset", "rel=unset", "wikizim=unset"):
            assert want in line, (want, line)
        assert f"scratch={tmp_path}/state/scratch/w" in line
    ports = {line.split("ports=")[1].split()[0] for line in lines}
    assert ports == {"19811,19812", "19813,19814"}
    cpus = {line.split("cpus=")[1].split()[0] for line in lines}
    assert cpus == {"0,1", "2,3"} or cpus == {"0-1", "2-3"}
    for rid in q:
        assert _status(tmp_path, rid) == "uploaded"
    for w in (tmp_path / "state" / "scratch").iterdir():
        assert list(w.iterdir()) == []
    table = (tmp_path / "state" / "status.txt").read_text()
    assert "start PDT" in table and "uploaded" in table
    assert "queue done" in (tmp_path / "state" / "run.out").read_text()


def test_groups_in_order(tmp_path):
    _setup(tmp_path, "## eu\na\nb\nc\n## rest\nx\ny\n")
    r = _run(tmp_path, _env(tmp_path))
    assert r.returncode == 0
    runs = _starts(tmp_path)
    order = [x[0] for x in runs]
    assert set(order[:3]) == {"a", "b", "c"} and set(order[3:]) == {"x", "y"}
    last_eu_start = max(x[1] for x in runs if x[0] in "abc")
    assert all(x[1] >= last_eu_start for x in runs if x[0] in "xy")


def test_alone_region_runs_by_itself_after_draining_and_on_hdd(tmp_path):
    q = ["a", "b", "europe", "c"]
    _setup(tmp_path, q)
    (tmp_path / "fake" / "a.sleep").write_text("4.0")
    r = _run(tmp_path, _env(tmp_path, PAR_WORKERS="3"))
    assert r.returncode == 0
    runs = {x[0]: x for x in _starts(tmp_path)}
    eu = runs["europe"]
    for rid in ("a", "b", "c"):
        s, e = runs[rid][1], runs[rid][2]
        assert e <= eu[1] or s >= eu[2], f"{rid} overlapped europe"
    assert "scratch=unset" in eu[3]        # its scratch stays on /storage


def test_at_most_two_big_regions(tmp_path):
    q = ["a", "b", "c"]
    _setup(tmp_path, q, sizes={"a": "9G", "b": "9G", "c": "9G"})
    for rid in q:
        (tmp_path / "fake" / f"{rid}.sleep").write_text("6.0")
    r = _run(tmp_path, _env(tmp_path, PAR_WORKERS="3"))
    assert r.returncode == 0
    assert _max_concurrent(_starts(tmp_path)) == 2
    assert "2 big regions already running" in (tmp_path / "state" / "run.out").read_text()


def test_nvme_start_guard_waits_for_room(tmp_path):
    # 5 GiB ZIMs -> 20 GiB scratch each, plus a's staged 8 GiB tiles; 150 free, 100 reserve.
    _setup(tmp_path, ["a", "b"], free=140, sizes={"a": "5G", "b": "5G"})
    (tmp_path / "root" / "world-data" / "regions").mkdir(parents=True)
    subprocess.run(["truncate", "-s", "8G", str(tmp_path / "root" / "world-data" / "regions" / "a.ml.mbtiles")],
                   check=True)
    r = _run(tmp_path, _env(tmp_path))
    assert r.returncode == 0
    assert _max_concurrent(_starts(tmp_path)) == 1
    out = (tmp_path / "state" / "run.out").read_text()
    assert "waits: NVMe" in out
    assert "projection 28 GiB" in out


def test_memory_guard(tmp_path):
    _setup(tmp_path, ["a"], mem=10)
    p = _bg(tmp_path, _env(tmp_path))
    time.sleep(1.5)
    assert not (tmp_path / "fake.log").exists()
    (tmp_path / "mem").write_text("40\n")
    assert p.wait(timeout=30) == 0
    assert _status(tmp_path, "a") == "uploaded"
    assert "MemAvailable 10 GiB < 30" in (tmp_path / "state" / "run.out").read_text()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


FREEZE_IGNORED = {"torrent": "web/torrents/a.torrent", "langpack ops": "ops/langpack/q.sh",
                  "langpack tools": "tools/langpack/p.py", "langpack tests": "tests/langpack/t.py",
                  "build page": "web/build/index.html", "docs": "docs/x.md"}


@pytest.mark.parametrize("what", [*FREEZE_IGNORED, "code", "code beside docs", "web viewer"])
def test_code_freeze_ignores_torrent_commits(tmp_path, what):
    _setup(tmp_path, ["a", "b"])
    root = tmp_path / "root"
    _git(root, "init", "-q")
    (root / "ops").mkdir()
    (root / "ops" / "x.sh").write_text("v1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    for rid in ("a", "b"):
        (tmp_path / "fake" / f"{rid}.sleep").write_text("3.0")
    p = _bg(tmp_path, _env(tmp_path, PAR_WORKERS="1"))
    _wait_starts(tmp_path, 1)
    if what in FREEZE_IGNORED:
        (root / FREEZE_IGNORED[what]).parent.mkdir(parents=True, exist_ok=True)
        (root / FREEZE_IGNORED[what]).write_text("t")
    elif what == "web viewer":
        (root / "web").mkdir()
        (root / "web" / "app.js").write_text("v2\n")
    else:
        (root / "ops" / "x.sh").write_text("v2\n")
        if what == "code beside docs":
            (root / "docs").mkdir()
            (root / "docs" / "x.md").write_text("t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", what)
    if what in FREEZE_IGNORED:
        assert p.wait(timeout=60) == 0
        assert _status(tmp_path, "b") == "uploaded"
        assert "code changed" not in (tmp_path / "state" / "run.out").read_text()
    else:
        out = _wait_log(tmp_path, "code changed since")
        assert "b waits: the checkout's code changed since" in out
        assert (tmp_path / "fake.log").read_text().count("start ") == 1
        p.kill()
        p.wait()


def test_logs_path_and_tools(tmp_path):
    _setup(tmp_path, ["a"])
    r = _run(tmp_path, _env(tmp_path, PAR_PATH="/usr/bin:/bin"))
    assert r.returncode == 0
    out = (tmp_path / "state" / "run.out").read_text()
    assert "PATH for the drivers: /usr/bin:/bin" in out
    for tool in ("ia", "docker", "osmium", "firebase", "node"):
        assert f"  {tool} -> " in out


def test_watchdog_pauses_newest_then_resumes(tmp_path):
    _setup(tmp_path, ["a", "b"])
    for rid in ("a", "b"):
        (tmp_path / "fake" / f"{rid}.sleep").write_text("8.0")
    p = _bg(tmp_path, _env(tmp_path, PAR_STAGGER="1"))
    _wait_starts(tmp_path, 2)
    (tmp_path / "free").write_text("90\n")
    out = _wait_log(tmp_path, "paused a (w1)")
    assert "paused a (w1)" in out          # w1 started a (the smallest) second
    apid = [line.split()[3] for line in (tmp_path / "fake.log").read_text().splitlines()
            if line.startswith("start a")][0]
    assert Path(f"/proc/{apid}/stat").read_text().split(") ")[1][0] == "T"
    (tmp_path / "free").write_text("130\n")
    assert p.wait(timeout=60) == 0
    assert "resumed a (w1)" in (tmp_path / "state" / "run.out").read_text()
    assert _status(tmp_path, "a") == "uploaded" and _status(tmp_path, "b") == "uploaded"


def test_never_pauses_a_build_that_is_uploading(tmp_path):
    _setup(tmp_path, ["a", "b"])
    for rid in ("a", "b"):
        (tmp_path / "fake" / f"{rid}.sleep").write_text("8.0")
    (tmp_path / "fake" / "a.uploading").write_text("")   # a is the newest (w1)
    p = _bg(tmp_path, _env(tmp_path, PAR_STAGGER="1"))
    _wait_starts(tmp_path, 2)
    time.sleep(0.5)
    (tmp_path / "free").write_text("90\n")
    out = _wait_log(tmp_path, "paused b (w0)")
    assert "paused b (w0)" in out and "paused a" not in out
    (tmp_path / "free").write_text("130\n")
    assert p.wait(timeout=60) == 0


def test_last_running_build_is_never_paused_or_killed(tmp_path):
    _setup(tmp_path, ["a"])
    (tmp_path / "fake" / "a.sleep").write_text("4.0")
    p = _bg(tmp_path, _env(tmp_path))
    _wait_starts(tmp_path, 1)
    (tmp_path / "free").write_text("50\n")
    out = _wait_log(tmp_path, "ALERT")
    assert "not stopping the last build" in out
    assert p.wait(timeout=60) == 0
    out = (tmp_path / "state" / "run.out").read_text()
    assert "paused" not in out and "stopping a" not in out and _status(tmp_path, "a") == "uploaded"


def test_watchdog_kills_newest_and_requeues_when_critical(tmp_path):
    _setup(tmp_path, ["a", "b"])
    for rid in ("a", "b"):
        (tmp_path / "fake" / f"{rid}.sleep").write_text("8.0")
    p = _bg(tmp_path, _env(tmp_path, PAR_STAGGER="1"))
    _wait_starts(tmp_path, 2)
    (tmp_path / "free").write_text("50\n")
    out = _wait_log(tmp_path, "a: stopped, scratch wiped, requeued")
    assert "stopping a in w1" in out and "requeued (projection x2)" in out
    (tmp_path / "fake" / "a.sleep").write_text("0.3")
    (tmp_path / "free").write_text("1000\n")
    assert p.wait(timeout=60) == 0
    assert _status(tmp_path, "a") == "uploaded" and _status(tmp_path, "b") == "uploaded"
    assert (tmp_path / "fake.log").read_text().count("start a") == 2


def test_failed_region_is_loud_exit_1_and_not_retried(tmp_path):
    _setup(tmp_path, ["a", "b"])
    (tmp_path / "fake" / "a.status").write_text("build-failed")
    r = _run(tmp_path, _env(tmp_path))
    assert r.returncode == 1
    out = (tmp_path / "state" / "run.out").read_text()
    assert "FAILED a: build-failed" in out and "FAILED regions this run: a" in out
    assert _status(tmp_path, "a") == "failed" and _status(tmp_path, "b") == "uploaded"
    r2 = _run(tmp_path, _env(tmp_path))
    assert r2.returncode == 0
    assert (tmp_path / "fake.log").read_text().count("start ") == 2
    (tmp_path / "fake" / "a.status").write_text("uploaded")
    r3 = _run(tmp_path, _env(tmp_path, PAR_RETRY_FAILED="1"))
    assert r3.returncode == 0 and _status(tmp_path, "a") == "uploaded"


def test_upload_pending_is_finished_once_listed(tmp_path):
    _setup(tmp_path, ["a"])
    (tmp_path / "fake" / "a.status").write_text("upload-pending")
    (tmp_path / "root" / "osm-a-2026-10-23.zim").write_text("zim")
    env = _env(tmp_path, PAR_LISTED_CMD=f"[ -e {tmp_path}/listed ]",
               PAR_FINISH_CMD=f"echo finish $FINISH_ID $FINISH_ZIM zstd=${{ZSTD_CLEVEL-unset}} keep=$STREETZIM_KEEP_ZIMS >> {tmp_path}/finish.log")
    p = _bg(tmp_path, env)
    out = _wait_log(tmp_path, "listing pending")
    assert "the finisher will complete it" in out
    time.sleep(1.5)
    assert _status(tmp_path, "a") == "upload-pending"
    (tmp_path / "listed").write_text("")
    assert p.wait(timeout=60) == 0
    assert (tmp_path / "finish.log").read_text().strip() == "finish a osm-a-2026-10-23.zim zstd=unset keep=3"
    assert (tmp_path / "rebuild-old.tsv").read_text().splitlines()[-1].startswith(
        "a\tuploaded\tosm-a-2026-10-23.zim\t")
    assert _status(tmp_path, "a") == "uploaded"


def test_resume_requeues_dead_driver(tmp_path):
    _setup(tmp_path, ["a"])
    st = tmp_path / "state" / "status"
    st.mkdir(parents=True)
    (st / "a").write_text("running w0 999999 12345 1 x\n")
    r = _run(tmp_path, _env(tmp_path))
    assert r.returncode == 0 and _status(tmp_path, "a") == "uploaded"
    assert "requeued" in (tmp_path / "state" / "run.out").read_text()


def test_restart_readopts_running_drivers(tmp_path):
    _setup(tmp_path, ["a", "b"])
    for rid in ("a", "b"):
        (tmp_path / "fake" / f"{rid}.sleep").write_text("6.0")
    p = _bg(tmp_path, _env(tmp_path))
    _wait_starts(tmp_path, 2)
    time.sleep(1.5)
    p.kill()                                # the runner dies; its drivers (own sessions) go on
    p.wait()
    r = _run(tmp_path, _env(tmp_path))
    assert r.returncode == 0
    out = (tmp_path / "state" / "run.out").read_text()
    assert "re-adopted running driver" in out
    assert (tmp_path / "fake.log").read_text().count("start ") == 2    # nothing started twice
    assert _status(tmp_path, "a") == "uploaded" and _status(tmp_path, "b") == "uploaded"


def test_refuses_without_the_knobs(tmp_path):
    _setup(tmp_path, ["a"])
    (tmp_path / "driver.sh").write_text("#!/bin/bash\necho old driver\n")
    r = _run(tmp_path, _env(tmp_path))
    assert r.returncode == 1
    assert "not the parallel-capable driver" in (tmp_path / "state" / "run.out").read_text()
    assert not (tmp_path / "fake.log").exists()


def test_stray_kiwix_serve_on_the_slots_ports_is_stopped(tmp_path):
    _setup(tmp_path, ["a"])
    stray = subprocess.Popen(["bash", "-c", "exec -a kiwix-serve python3 -m http.server 19811 --bind 127.0.0.1"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        r = _run(tmp_path, _env(tmp_path, PAR_WORKERS="1"))
        assert r.returncode == 0
        assert f"stopping stray kiwix-serve {stray.pid}" in (tmp_path / "state" / "run.out").read_text()
        assert stray.wait(timeout=10) is not None
    finally:
        if stray.poll() is None:
            stray.kill()


def test_leftover_scratch_is_reaped_at_start(tmp_path):
    _setup(tmp_path, ["a"])
    w = tmp_path / "state" / "scratch" / "w1"
    (w / "tmp").mkdir(parents=True)
    (w / "tmp" / "old").write_text("x")
    (w / ".hidden").write_text("x")
    r = _run(tmp_path, _env(tmp_path))
    assert r.returncode == 0
    assert list(w.iterdir()) == []


def test_variant_skipped_while_parent_builds_elsewhere(tmp_path):
    _setup(tmp_path, ["sw-light", "x"], variants="sw-light\tsw\tno\t13\n")
    fake = tmp_path / "build-region-fast.sh"
    fake.write_text("sleep 30\n")
    other = subprocess.Popen(["bash", str(fake), "sw", "1,2,3,4", "Sw"])
    try:
        p = _bg(tmp_path, _env(tmp_path, PAR_WORKERS="1"))
        deadline = time.time() + 15
        while time.time() < deadline and _statuses(tmp_path).get("x") != "uploaded":
            time.sleep(0.1)
        assert _statuses(tmp_path).get("x") == "uploaded"
        assert "sw-light" not in _statuses(tmp_path)
    finally:
        other.kill()
        other.wait()
    assert p.wait(timeout=30) == 0
    assert _status(tmp_path, "sw-light") == "uploaded"


def test_waits_while_runsh_prepares_its_driver(tmp_path):
    _setup(tmp_path, ["a"])
    runsh = tmp_path / "sz-tests" / "rebuild-all" / "run.sh"
    runsh.parent.mkdir(parents=True)
    runsh.write_text("sleep 30\n")
    (tmp_path / "run.out").write_text("x run.sh at abc: switzerland\n")
    other = subprocess.Popen(["bash", str(runsh)])
    try:
        p = _bg(tmp_path, _env(tmp_path))
        time.sleep(1.5)
        assert not (tmp_path / "fake.log").exists()
        with (tmp_path / "run.out").open("a") as f:
            f.write("y started driver 123: switzerland\n")
        assert p.wait(timeout=30) == 0
    finally:
        other.kill()
        other.wait()
    assert _status(tmp_path, "a") == "uploaded"
    assert "preparing its driver" in (tmp_path / "state" / "run.out").read_text()


def test_region_env_file_reaches_the_driver(tmp_path):
    _setup(tmp_path, ["a"])
    (tmp_path / "state" / "env").mkdir(parents=True)
    (tmp_path / "state" / "env" / "a").write_text(
        "# comment\nSTREETZIM_SEARCH_CACHE=/x/sw.jsonl\nOVERTURE_RELEASE=2026-10-21.0\nbad line\n")
    r = _run(tmp_path, _env(tmp_path))
    assert r.returncode == 0
    log = (tmp_path / "fake.log").read_text()
    assert "cache=/x/sw.jsonl" in log and "rel=2026-10-21.0" in log
    assert "ignoring bad env line" in (tmp_path / "state" / "run.out").read_text()


def test_stop_file(tmp_path):
    _setup(tmp_path, ["a", "b"])
    (tmp_path / "state" / "STOP").write_text("")
    r = _run(tmp_path, _env(tmp_path))
    assert r.returncode == 0
    assert not (tmp_path / "fake.log").exists()


def test_wiki_copy_is_verified_and_reused(tmp_path):
    _setup(tmp_path, ["a"])
    src = tmp_path / "enwiki.zim"
    src.write_bytes(os.urandom(300_000))
    env = _env(tmp_path, PAR_WIKI_NVME="1", PAR_WIKI_SRC=str(src), PAR_WIKI_DIR=str(tmp_path / "wiki"))
    r = _run(tmp_path, env)
    assert r.returncode == 0
    copy = tmp_path / "wiki" / "enwiki.zim"
    assert copy.read_bytes() == src.read_bytes()
    assert (tmp_path / "wiki" / "enwiki.zim.verified").exists()
    assert "md5" in (tmp_path / "state" / "run.out").read_text()
    # a build started once the copy is verified reads it
    (tmp_path / "state" / "status" / "a").unlink()
    r = _run(tmp_path, env)
    assert r.returncode == 0 and "using verified" in (tmp_path / "state" / "run.out").read_text()
    assert (tmp_path / "fake.log").read_text().splitlines()[-2].count(f"wikizim={copy}") == 1


def test_wiki_copy_not_used_when_the_original_changed(tmp_path):
    _setup(tmp_path, ["a"])
    src = tmp_path / "enwiki.zim"
    src.write_bytes(b"x" * 1000)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "enwiki.zim").write_bytes(b"y" * 1000)
    (wiki / "enwiki.zim.verified").write_text("1000 1\n")      # another mtime: stale marker
    env = _env(tmp_path, PAR_WIKI_NVME="1", PAR_WIKI_SRC=str(src), PAR_WIKI_DIR=str(wiki))
    r = _run(tmp_path, env)
    assert r.returncode == 0
    assert (wiki / "enwiki.zim").read_bytes() == src.read_bytes()   # copied again


def test_wiki_copy_skipped_without_room(tmp_path):
    _setup(tmp_path, ["a"], free=120)
    src = tmp_path / "enwiki.zim"
    src.write_bytes(b"x" * 1000)
    env = _env(tmp_path, PAR_WIKI_NVME="1", PAR_WIKI_SRC=str(src), PAR_WIKI_DIR=str(tmp_path / "wiki"))
    r = _run(tmp_path, env)
    assert r.returncode == 0
    assert not (tmp_path / "wiki" / "enwiki.zim").exists()
    assert "wikizim=unset" in (tmp_path / "fake.log").read_text()


def test_precut_first_group_in_pick_order_and_gates_each_build(tmp_path):
    _setup(tmp_path, "## eu\na\nv\nnopbf\n## rest\nx\n", variants="v\ta\tno\t13\n")
    (tmp_path / "regions.tsv").write_text(
        "# id\tname\tbbox\na\tA\t1,2,3,4\tcountry\nv\tV\t1,2,3,4\tcountry\n"
        "nopbf\tN\t1,2,3,4\tcountry\nx\tX\t1,2,3,4\tcountry\n")
    regions = tmp_path / "root" / "world-data" / "regions"
    regions.mkdir(parents=True)
    (regions / "a.osm.pbf").write_text("pbf")
    (regions / "x.osm.pbf").write_text("pbf")
    env = _env(tmp_path, PAR_PRECUT="1",
               PAR_PRECUT_CMD=f'sleep 3; echo "cut $PRECUT_ID $PRECUT_SRC $PRECUT_BBOX" >> {tmp_path}/fake.log')
    r = _run(tmp_path, env)
    assert r.returncode == 0
    lines = (tmp_path / "fake.log").read_text().splitlines()
    cuts = [line for line in lines if line.startswith("cut ")]
    # nopbf has no extract (left to its build); v is cut with its parent's tiles
    assert [c.split()[1:3] for c in cuts] == [["a", "a"], ["v", "a"]]
    for rid in ("a", "v"):
        cut_i = next(i for i, line in enumerate(lines) if line.startswith(f"cut {rid} "))
        start_i = next(i for i, line in enumerate(lines) if line.startswith(f"start {rid} "))
        assert cut_i < start_i, f"{rid} built before its tiles"
    out = (tmp_path / "state" / "run.out").read_text()
    # the small region first, then the large one worker 0 takes
    assert "tiles nopbf: extract missing" in out and "tiles: cutting in this order: a nopbf v" in out
    assert "cut x" not in (tmp_path / "fake.log").read_text()      # only the first group


# ---- the driver's knobs ----------------------------------------------------

def _driver_copy(tmp: Path) -> Path:
    text = DRIVER.read_text().replace("/storage/streetzim", str(tmp))
    p = tmp / "driver-copy.sh"
    p.write_text(text)
    (tmp / "ops").mkdir()
    (tmp / "ops" / "region-bbox.sh").write_text("bbox_stale() { return 1; }\n")
    (tmp / "cloud").mkdir()
    (tmp / "cloud" / "regions.tsv").write_text("# id\tname\tbbox\n")
    (tmp / "tmp").mkdir()
    return p


@pytest.mark.parametrize("noskip", ["0", "1"])
def test_driver_with_a_bad_overture_release_builds_nothing_and_says_so(tmp_path, noskip):
    """The real driver: the reason in its log, and a row for each listed
    region it would build (the runner's finish_slot reads that row)."""
    p = _driver_copy(tmp_path)
    tsv = tmp_path / "rebuild-old.tsv"
    tsv.write_text("id\tstatus\tzim\tfinished\nx\tuploaded\tosm-x.zim\tT\n")
    (tmp_path / "w.list").write_text("x\ny\n")
    env = {**os.environ, "OVERTURE_RELEASE": "latest", "REBUILD_LIST": str(tmp_path / "w.list"),
           "REBUILD_LOG": str(tmp_path / "w.log"), "REBUILD_PIDFILE": str(tmp_path / "w.pid"),
           "REBUILD_OV_PORT": "19999", "REBUILD_KW_PORT": "19998", "REBUILD_NO_SKIP": noskip}
    r = subprocess.run(["bash", str(p)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "OVERTURE_RELEASE 'latest' is not a release name" in (tmp_path / "w.log").read_text()
    rows = [ln.split("\t")[:3] for ln in tsv.read_text().splitlines()[2:]]
    want = [["y", "bad-overture-release", "-"]] if noskip == "0" else \
        [["x", "bad-overture-release", "-"], ["y", "bad-overture-release", "-"]]
    assert rows == want, rows


@pytest.mark.parametrize("noskip", ["0", "1"])
def test_driver_list_log_pidfile_ports_noskip(tmp_path, noskip):
    p = _driver_copy(tmp_path)
    (tmp_path / "rebuild-old.tsv").write_text("id\tstatus\tzim\tfinished\nx\tuploaded\tosm-x.zim\tT\n")
    (tmp_path / "w.list").write_text("x\n")
    env = {**{k: v for k, v in os.environ.items() if k != "OVERTURE_RELEASE"},
           "REBUILD_LIST": str(tmp_path / "w.list"), "REBUILD_LOG": str(tmp_path / "w.log"),
           "REBUILD_PIDFILE": str(tmp_path / "w.pid"), "REBUILD_OV_PORT": "19999",
           "REBUILD_KW_PORT": "19998", "REBUILD_NO_SKIP": noskip}
    subprocess.run(["bash", str(p)], env=env, cwd=tmp_path, check=True, capture_output=True,
                   text=True, timeout=60)
    log = (tmp_path / "w.log").read_text()
    assert (tmp_path / "w.pid").read_text().strip().isdigit()
    assert not (tmp_path / ".rebuild-old.pid").exists() and not (tmp_path / "rebuild-old.log").exists()
    if noskip == "1":
        assert "skip x: no registry row" in log
        assert (tmp_path / "rebuild-old.tsv").read_text().splitlines()[-1].startswith("x\tno-registry\t-\t")
    else:
        assert "skip x: done" in log
    assert "=== rebuild complete ===" in log
    text = p.read_text()
    assert 'OV=${REBUILD_OV_PORT:-9641}; KW=${REBUILD_KW_PORT:-9642}' in text
    assert 'STAGE_MBTILES_NVME="${REBUILD_STAGE_MBTILES:-0}"' in text


def test_driver_rows_append_under_lock(tmp_path):
    text = DRIVER.read_text()
    row = [line for line in text.splitlines() if line.startswith("row(){")][0]
    tsv = tmp_path / "t.tsv"
    script = f'TSV={tsv}\n{row}\nfor i in $(seq 1 200); do row r$i ok z & done; wait\n'
    subprocess.run(["bash", "-c", script], check=True, timeout=60)
    lines = tsv.read_text().splitlines()
    assert len(lines) == 200 and all(len(line.split("\t")) == 4 for line in lines)
    assert (tmp_path / "t.tsv.lock").exists()


# ---- build-region-fast.sh BUILD_SCRATCH / BUILD_WIKI_ZIM ---------------------

STUB_PY = r"""#!/bin/bash
# stub python: create_osm_zim writes --output; validate records its arg
case "$1" in
  */create_osm_zim.py) shift; src=""; while [ $# -gt 0 ]; do [ "$1" = --output ] && out=$2; [ "$1" = --wiki-articles-source ] && src=$2; shift; done
    echo "TMPDIR=$TMPDIR out=$out wiki=$src" >> "$STUB_LOG"; [ -n "${STUB_FAIL:-}" ] && exit 1
    [ -n "${STUB_RACE:-}" ] && echo raced > "$STUB_RACE"
    head -c 4096 /dev/urandom > "$out" ;;
  */validate_zim.py) echo "validate $2" >> "$STUB_LOG" ;;
  *) echo "py $*" >> "$STUB_LOG" ;;
esac
"""


def _wrapper_copy(tmp: Path) -> tuple[Path, dict[str, str]]:
    root = tmp / "root"
    for d in ("ops", "cloud", "tmp", "world-data/regions", "terrain_cache/dem_sources",
              "overture_cache", "venv-linux/bin", "scratch", "wiki-src"):
        (root / d).mkdir(parents=True, exist_ok=True)
    text = WRAPPER.read_text().replace("/storage/streetzim", str(root))
    p = root / "ops" / "build-region-fast.sh"
    p.write_text(text)
    (root / "ops" / "region-clip.sh").write_text("region_clip_args() { CLIP_ARGS=(); }\n")
    (root / "terrain_cache" / "dem_sources" / "world_dem_32k.tif").write_text("x")
    py = root / "venv-linux" / "bin" / "python3"
    py.write_text(STUB_PY)
    py.chmod(0o755)
    env = {**os.environ, "OVERTURE_RELEASE": "r", "STREETZIM_ML_TILES": "0", "WIKI_IMAGES": "off",
           "STUB_LOG": str(tmp / "stub.log")}
    return p, env


def test_wrapper_builds_in_scratch_and_renames_into_place(tmp_path):
    p, env = _wrapper_copy(tmp_path)
    scratch = tmp_path / "root" / "scratch" / "w0"
    env["BUILD_SCRATCH"] = str(scratch)
    subprocess.run(["bash", str(p), "rx", "1,2,3,4", "Rx"], env=env, check=True,
                   capture_output=True, text=True, timeout=60)
    log = (tmp_path / "stub.log").read_text()
    today = time.strftime("%Y-%m-%d")
    final = tmp_path / "root" / f"osm-rx-{today}.zim"
    assert f"TMPDIR={scratch}/tmp out={scratch}/osm-rx-{today}.zim" in log
    assert final.stat().st_size == 4096
    assert f"validate {final}" in log
    assert not (scratch / f"osm-rx-{today}.zim").exists()
    assert not list((tmp_path / "root").glob(".osm-rx-*.copy-*"))


def test_wrapper_without_scratch_is_unchanged(tmp_path):
    p, env = _wrapper_copy(tmp_path)
    subprocess.run(["bash", str(p), "rx", "1,2,3,4", "Rx"], env=env, check=True,
                   capture_output=True, text=True, timeout=60)
    log = (tmp_path / "stub.log").read_text()
    today = time.strftime("%Y-%m-%d")
    assert f"TMPDIR={tmp_path}/root/tmp out={tmp_path}/root/osm-rx-{today}.zim" in log


def test_wrapper_refuses_to_overwrite_a_file_that_appeared(tmp_path):
    p, env = _wrapper_copy(tmp_path)
    today = time.strftime("%Y-%m-%d")
    env["BUILD_SCRATCH"] = str(tmp_path / "root" / "scratch" / "w0")
    env["STUB_RACE"] = str(tmp_path / "root" / f"osm-rx-{today}.zim")
    r = subprocess.run(["bash", str(p), "rx", "1,2,3,4", "Rx"], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode != 0
    assert (tmp_path / "root" / f"osm-rx-{today}.zim").read_text() == "raced\n"
    assert not list((tmp_path / "root").glob(".osm-rx-*.copy-*"))


def test_wrapper_failed_build_leaves_no_final(tmp_path):
    p, env = _wrapper_copy(tmp_path)
    env["BUILD_SCRATCH"] = str(tmp_path / "root" / "scratch" / "w0")
    env["STUB_FAIL"] = "1"
    r = subprocess.run(["bash", str(p), "rx", "1,2,3,4", "Rx"], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode != 0
    assert not list((tmp_path / "root").glob("osm-rx-*.zim"))


def test_wrapper_log_is_appended(tmp_path):
    p, env = _wrapper_copy(tmp_path)
    today = time.strftime("%Y-%m-%d")
    log = tmp_path / "root" / f"rx-rebuild-{today}.log"
    log.write_text("earlier attempt\n")
    subprocess.run(["bash", str(p), "rx", "1,2,3,4", "Rx"], env=env, check=True,
                   capture_output=True, text=True, timeout=60)
    assert log.read_text().startswith("earlier attempt\n")


@pytest.mark.parametrize("good", [True, False])
def test_wrapper_wiki_copy_used_only_when_size_matches(tmp_path, good):
    p, env = _wrapper_copy(tmp_path)
    p.write_text(p.read_text().replace("WIKI_ZIM_SIZE=123980647016", "WIKI_ZIM_SIZE=1000"))
    orig = tmp_path / "root" / "wiki-src" / "wikipedia_en_all_maxi_2026-02.zim"
    orig.write_bytes(b"w" * 1000)
    copy = tmp_path / "copy.zim"
    copy.write_bytes(b"w" * (1000 if good else 999))
    env.update({"WIKI_IMAGES": "all", "BUILD_WIKI_ZIM": str(copy)})
    subprocess.run(["bash", str(p), "rx", "1,2,3,4", "Rx"], env=env, check=True,
                   capture_output=True, text=True, timeout=60)
    assert f"wiki={copy if good else orig}" in (tmp_path / "stub.log").read_text()


def test_upload_validated_keep_count_knob():
    text = (ROOT / "ops" / "cloud" / "upload_validated.sh").read_text()
    assert 'cloud/cleanup_old_zims.py "streetzim-${id}" --keep "${STREETZIM_KEEP_ZIMS:-2}"' in text


def test_wiki_copy_that_fails_verification_is_not_used(tmp_path):
    _setup(tmp_path, ["a"])
    src = tmp_path / "enwiki.zim"
    src.write_bytes(os.urandom(200_000))
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    # md5sum of the copy (a *.part file) disagrees with the stream's
    (fakebin / "md5sum").write_text(
        '#!/bin/bash\nfor a in "$@"; do case "$a" in *.part) echo "deadbeef  $a"; exit 0 ;; esac; done\n'
        'exec /usr/bin/md5sum "$@"\n')
    (fakebin / "md5sum").chmod(0o755)
    env = _env(tmp_path, PAR_WIKI_NVME="1", PAR_WIKI_SRC=str(src), PAR_WIKI_DIR=str(tmp_path / "wiki"))
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    r = _run(tmp_path, env)
    assert r.returncode == 0
    assert "verification failed" in (tmp_path / "state" / "run.out").read_text()
    assert not (tmp_path / "wiki" / "enwiki.zim").exists()
    assert not (tmp_path / "wiki" / "enwiki.zim.verified").exists()
    assert "wikizim=unset" in (tmp_path / "fake.log").read_text()


def test_background_jobs_are_stopped_with_the_runner(tmp_path):
    _setup(tmp_path, "## eu\na\n")
    (tmp_path / "regions.tsv").write_text("# id\tname\tbbox\na\tA\t1,2,3,4\tcountry\n")
    regions = tmp_path / "root" / "world-data" / "regions"
    regions.mkdir(parents=True)
    (regions / "a.osm.pbf").write_text("pbf")
    (tmp_path / "state" / "STOP").write_text("")
    env = _env(tmp_path, PAR_PRECUT="1", PAR_PRECUT_CMD="sleep 30.4242")
    r = _run(tmp_path, env)
    assert r.returncode == 0
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "^sleep 30.4242$"], capture_output=True, text=True).stdout.split()
    assert left == []


def test_no_second_wiki_copy_while_a_writer_holds_the_part_file(tmp_path):
    _setup(tmp_path, ["a"])
    src = tmp_path / "enwiki.zim"
    src.write_bytes(b"x" * 1000)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    part = wiki / "enwiki.zim.part"
    holder = subprocess.Popen(["bash", "-c", f'exec 3>>"{part}"; sleep 30'])
    try:
        time.sleep(0.5)
        env = _env(tmp_path, PAR_WIKI_NVME="1", PAR_WIKI_SRC=str(src), PAR_WIKI_DIR=str(wiki))
        r = _run(tmp_path, env)
        assert r.returncode == 0
        assert "still open by another process" in (tmp_path / "state" / "run.out").read_text()
        assert not (wiki / "enwiki.zim").exists() and part.exists()
    finally:
        holder.kill()
        holder.wait()


def test_cleanup_wiki_mode(tmp_path):
    _setup(tmp_path, ["a"])
    src = tmp_path / "enwiki.zim"
    src.write_bytes(b"x" * 10)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "enwiki.zim").write_bytes(b"x" * 10)
    (wiki / "enwiki.zim.verified").write_text("10 1\n")
    env = _env(tmp_path, PAR_WIKI_SRC=str(src), PAR_WIKI_DIR=str(wiki))
    holder = subprocess.Popen(["bash", "-c", f'exec 3<"{wiki}/enwiki.zim"; sleep 30'])
    try:
        time.sleep(0.5)
        r = subprocess.run(["bash", str(RUNNER), "--cleanup-wiki"], env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 1 and (wiki / "enwiki.zim").exists()
    finally:
        holder.kill()
        holder.wait()
    r = subprocess.run(["bash", str(RUNNER), "--cleanup-wiki"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert not (wiki / "enwiki.zim").exists() and not (wiki / "enwiki.zim.verified").exists()


def test_first_upload_gets_the_item_metadata():
    text = (ROOT / "ops" / "cloud" / "upload_validated.sh").read_text()
    assert '--metadata="mediatype:data" --metadata="collection:opensource" --metadata="title:streetzim-${id}"' in text


def test_a_slot_waiting_for_its_tiles_does_not_hold_up_the_others(tmp_path):
    _setup(tmp_path, "## eu\na\nbig\n")
    (tmp_path / "regions.tsv").write_text("# id\tname\tbbox\na\tA\t1,2,3,4\tcountry\nbig\tB\t1,2,3,4\tcountry\n")
    regions = tmp_path / "root" / "world-data" / "regions"
    regions.mkdir(parents=True)
    for rid in ("a", "big"):
        (regions / f"{rid}.osm.pbf").write_text("pbf")
    env = _env(tmp_path, PAR_PRECUT="1",
               PAR_PRECUT_CMD=f'[ "$PRECUT_ID" = big ] && sleep 5; echo "cut $PRECUT_ID" >> {tmp_path}/fake.log')
    r = _run(tmp_path, env)
    assert r.returncode == 0
    lines = (tmp_path / "fake.log").read_text().splitlines()
    # worker 0 waits for big's tiles; worker 1 starts a meanwhile
    assert lines.index(next(x for x in lines if x.startswith("start a "))) < lines.index("cut big")
    assert lines.index("cut big") < lines.index(next(x for x in lines if x.startswith("start big ")))


# ---- gate(): a browser gate that timed out is run once more -----------------

STUB_NODE = r"""#!/bin/bash
# stub node: prints the scripted output for this call of the gate script $1
n=$(basename "$1" .mjs)
c=$(( $(cat "$GT/$n.n" 2>/dev/null || echo 0) + 1 )); echo "$c" > "$GT/$n.n"
f="$GT/$n.$c"; [ -f "$f" ] || f="$GT/$n.ok"
cat "$f"; exit "$(cat "$f.rc" 2>/dev/null || echo 0)"
"""
OK_OUT = {
    "overlap-check": "viewport {}\nNO OVERLAPS\nOVERLAP-CHECK-DONE\n",
    "device-matrix": "PASS  iPhone SE\n\nall 7 devices passed\n",
    "map-health": '{"styleLoaded":true,"sourceLoaded":{"osm":true},"renderedFeatures":500,"errors":[]}\n',
    "kiwix": "  kiwix viewer smoke rc=0\n",
}
NAV_TIMEOUT = "TimeoutError: Navigation timeout of 240000 ms exceeded\n"
MATRIX_FAIL = "PASS  a\nFAIL  b\n        ! search returned 0\n\n1/7 devices FAILED\n"
KIWIX_FIND_FAIL = "FAIL  Find page returns places from the ZIM\n        clicked=null\n"


def _gate(tmp: Path, outputs: dict[str, tuple[str, int]]) -> tuple[int, dict[str, int], str]:
    """Runs the driver's own gate() with stub node, kiwix-serve and kiwix gate.
    outputs: {"<script>.<call>": (stdout, rc)}; other calls print OK_OUT."""
    text = DRIVER.read_text()
    start = text.index("TIMEOUT_RE=")
    end = text.index("\n}\n", text.index("\ngate(){")) + 3
    gt = tmp / "gt"
    (gt / "tmp").mkdir(parents=True)
    (gt / "cloud").mkdir()
    (gt / "node").write_text(STUB_NODE)
    (gt / "node").chmod(0o755)
    # Both record whether parallel_rebuild.sh --regate's lock (fd 6) reached them.
    (gt / "ks").write_text('#!/bin/bash\n[ -e /proc/$$/fd/6 ] && echo open >> "$GT/ks.fd6" || echo closed >> "$GT/ks.fd6"\n')
    (gt / "ks").chmod(0o755)
    (gt / "cloud" / "kiwix_viewer_gate.sh").write_text(
        '[ -e /proc/$$/fd/6 ] && echo open >> "$GT/kg.fd6" || echo closed >> "$GT/kg.fd6"\nexec "$GT/node" kiwix\n')
    for name, out in OK_OUT.items():
        (gt / f"{name}.ok").write_text(out)
    for key, (out, rc) in outputs.items():
        (gt / key).write_text(out)
        (gt / f"{key}.rc").write_text(f"{rc}\n")
    script = (text[start:end] + "\n"
              'log(){ echo "$*" >> "$LOG"; }\n'
              'ss(){ :; }; curl(){ echo 200; }; pgrep(){ :; }; sleep(){ :; }\n'
              f'KS="$GT/ks" NODE="$GT/node" PY={sys.executable} OV=19901 KW=19902 '
              'LOG="$GT/log" TMPDIR="$GT/tmp"\n'
              'exec 6> "$GT/regate.lock"\n'
              'gate "$GT/osm-x-2026-10-23.zim" Term\n')
    r = subprocess.run(["bash", "-c", script], env={**os.environ, "GT": str(gt)}, cwd=gt,
                       capture_output=True, text=True, timeout=60)
    calls = {n: int((gt / f"{n}.n").read_text()) if (gt / f"{n}.n").exists() else 0 for n in OK_OUT}
    log = (gt / "log").read_text() if (gt / "log").exists() else ""
    return r.returncode, calls, log


GATE_CASES = {
    # name: (scripted outputs, calls per script, gate rc, failed gates)
    "overlap timeout, then pass": (
        {"overlap-check.1": ("Error: " + NAV_TIMEOUT, 1)}, {"overlap-check": 4}, 0, ""),
    "overlap timeout twice": (
        {"overlap-check.1": (NAV_TIMEOUT, 1), "overlap-check.2": (NAV_TIMEOUT, 1)},
        {"overlap-check": 4}, 1, "overlap@320"),
    "overlap killed by timeout(1)": (
        {"overlap-check.3": ("", 124)}, {"overlap-check": 4}, 0, ""),
    "real overlap": (
        {"overlap-check.2": ("OVERLAPS:\n  #a  x  #b   overlap 4x4px\n", 124)},  # then hung closing
        {"overlap-check": 3}, 1, "overlap@390"),
    "overlap crash, no timeout": (
        {"overlap-check.1": ("Error: Cannot find module puppeteer\n", 1)},
        {"overlap-check": 3}, 1, "overlap@320"),
    "matrix: only timeouts": (
        {"device-matrix.1": ("FAIL  iPhone SE\n        ! EXC " + NAV_TIMEOUT + "\n1/7 devices FAILED\n", 1)},
        {"device-matrix": 2}, 0, ""),
    "matrix: killed by timeout(1)": (
        {"device-matrix.1": ("PASS  iPhone SE\n", 124)}, {"device-matrix": 2}, 0, ""),
    "matrix: a failure under load, then pass": (
        {"device-matrix.1": (MATRIX_FAIL, 1)}, {"device-matrix": 2}, 0, ""),
    "matrix: fails twice": (
        {"device-matrix.1": (MATRIX_FAIL, 1), "device-matrix.2": (MATRIX_FAIL, 1)},
        {"device-matrix": 2}, 1, "device-matrix"),
    "render: no result, timeout": (
        {"map-health.1": (NAV_TIMEOUT, 1)}, {"map-health": 2}, 0, ""),
    "render: drew too little": (
        {"map-health.1": ('{"styleLoaded":true,"sourceLoaded":{"osm":true},"renderedFeatures":5}\n'
                          "Timeout 30000ms exceeded\n", 0)},
        {"map-health": 1}, 1, "render"),
    "kiwix: timeout": (
        {"kiwix.1": ("TimeoutError: page.goto: Timeout 240000ms exceeded.\n", 1)}, {"kiwix": 2}, 0, ""),
    "kiwix: a failure under load, then pass": (
        {"kiwix.1": (KIWIX_FIND_FAIL, 1)}, {"kiwix": 2}, 0, ""),
    "kiwix: old viewer twice": (
        {"kiwix.1": ("  FAIL: ZIM still carries the OLD viewer\n", 1),
         "kiwix.2": ("  FAIL: ZIM still carries the OLD viewer\n", 1)}, {"kiwix": 2}, 1, "kiwix"),
    "kiwix: killed by timeout(1) twice": (
        {"kiwix.1": ("", 124), "kiwix.2": ("", 124)}, {"kiwix": 2}, 1, "kiwix"),
}


@pytest.mark.parametrize("case", list(GATE_CASES))
def test_gate_retries_once_overlap_and_render_only_on_a_timeout(tmp_path, case):
    """Overlap/render: once more only when they timed out without a verdict.
    Device matrix/kiwix: once more on any failure; the second run decides."""
    outputs, want_calls, want_rc, want_failed = GATE_CASES[case]
    rc, calls, log = _gate(tmp_path, outputs)
    expect = {"overlap-check": 3, "device-matrix": 1, "map-health": 1, "kiwix": 1, **want_calls}
    assert (rc, calls) == (want_rc, expect), log
    retried = sum(expect.values()) - 6
    assert log.count("once more in") == retried, log
    if want_failed:
        assert f"GATES FAILED: {want_failed}" in log, log
    else:
        assert "GATES FAILED" not in log, log


def test_gate_logs_both_runs_of_the_matrix_and_the_kiwix_gate(tmp_path):
    rc, calls, log = _gate(tmp_path, {"device-matrix.1": (MATRIX_FAIL, 1), "kiwix.1": (KIWIX_FIND_FAIL, 1)})
    assert rc == 0 and calls["device-matrix"] == calls["kiwix"] == 2, log
    assert "device matrix run 1 FAILED (rc 1, devices failed: 1); once more in" in log
    assert "! search returned 0" in log and "device matrix: 7/7 PASS (run 2)" in log
    assert "kiwix in-ZIM gate run 1 FAILED (rc 1: FAIL  Find page returns places from the ZIM)" in log
    assert "kiwix in-ZIM gate: OK (run 2)" in log
    tmpd = tmp_path / "gt" / "tmp"
    assert (tmpd / "ec-osm-x-2026-10-23-matrix.txt.run1").read_text() == MATRIX_FAIL
    assert (tmpd / "ec-osm-x-2026-10-23-kiwix.txt.run1").read_text() == KIWIX_FIND_FAIL
    rc, calls, log = _gate(tmp_path / "b", {})
    assert rc == 0 and "(run 2)" not in log and "device matrix: 7/7 PASS\n" in log, log
    rc, calls, log = _gate(tmp_path / "c", {"kiwix.1": (KIWIX_FIND_FAIL, 1), "kiwix.2": (KIWIX_FIND_FAIL, 1)})
    assert rc == 1 and "kiwix in-ZIM gate: FAIL (run 2)" in log, log


def test_gate_keeps_the_regate_lock_from_kiwix_serve(tmp_path):
    _gate(tmp_path, {})
    gt = tmp_path / "gt"
    assert (gt / "ks.fd6").read_text().split() == ["closed"]
    assert (gt / "kg.fd6").read_text().split() == ["closed"]


def test_gate_browser_timeouts_raised():
    for script, needle, n in (("overlap-check.mjs", "timeout: 240000", 2),
                              ("device-matrix.mjs", "timeout:240000", 2),
                              ("map-health.mjs", "timeout:240000", 1)):
        assert (ROOT / "ops" / "tmp" / script).read_text().count(needle) == n, script
    assert (ROOT / "cloud" / "zim_viewer_smoke.mjs").read_text().count("'domcontentloaded', timeout: 240_000") == 2


# ---- the driver's REBUILD_REGATE_ZIM ---------------------------------------

def _driver_regate(tmp: Path, zim: str, uprc: int = 0) -> tuple[str, str, str]:
    p = _driver_copy(tmp)
    text = p.read_text()
    assert text.count("gate(){  # $1=zim $2=term") == 1
    p.write_text(text.replace("gate(){  # $1=zim $2=term",
                              'gate(){ echo "gate $*" >> "$GT_LOG"; return 0; }\n_gate_real(){', 1))
    (tmp / "cloud" / "regions.tsv").write_text("# id\tname\tbbox\nx\tX\t1,2,3,4\t\t\t\tTerm\n")
    (tmp / "cloud" / "upload_validated.sh").write_text(f'echo "upload $*" >> "$GT_LOG"; exit {uprc}\n')
    (tmp / "venv-linux" / "bin").mkdir(parents=True)
    (tmp / "venv-linux" / "bin" / "python3").write_text('#!/bin/bash\necho "py $*" >> "$GT_LOG"; cat > /dev/null\n')
    (tmp / "venv-linux" / "bin" / "python3").chmod(0o755)
    (tmp / "osm-x-2026-10-23.zim").write_text("zim")
    (tmp / "osm-x-light-2026-10-23.zim").write_text("zim")
    (tmp / "rebuild-old.tsv").write_text("id\tstatus\tzim\tfinished\n")
    (tmp / "w.list").write_text("x\n")
    env = {**os.environ, "REBUILD_LIST": str(tmp / "w.list"), "REBUILD_LOG": str(tmp / "w.log"),
           "REBUILD_PIDFILE": str(tmp / "w.pid"), "REBUILD_OV_PORT": "19999",
           "REBUILD_KW_PORT": "19998", "REBUILD_NO_SKIP": "1", "REBUILD_REGATE_ZIM": zim,
           "GT_LOG": str(tmp / "calls.log")}
    subprocess.run(["bash", str(p)], env=env, cwd=tmp, check=True, capture_output=True, text=True, timeout=60)
    calls = (tmp / "calls.log").read_text() if (tmp / "calls.log").exists() else ""
    return (tmp / "w.log").read_text(), calls, (tmp / "rebuild-old.tsv").read_text().splitlines()[-1]


@pytest.mark.parametrize("uprc,status", [(0, "uploaded"), (6, "upload-pending")])
def test_driver_regate_gates_and_uploads_without_building(tmp_path, uprc, status):
    log, calls, last = _driver_regate(tmp_path, "osm-x-2026-10-23.zim", uprc)
    assert "regate osm-x-2026-10-23.zim" in log and "=== rebuild complete ===" in log
    for step in ("osmium", "build-region-fast", "overture", "Overture", "built "):
        assert step not in log, step
    assert "py cloud/validate_zim.py osm-x-2026-10-23.zim" in calls
    assert "gate osm-x-2026-10-23.zim Term" in calls
    assert "upload x osm-x-2026-10-23.zim" in calls
    assert last.startswith(f"x\t{status}\tosm-x-2026-10-23.zim\t")


@pytest.mark.parametrize("zim", ["osm-x-light-2026-10-23.zim", "osm-x-2026-10-24.zim"])
def test_driver_regate_refuses_a_sibling_or_missing_zim(tmp_path, zim):
    log, calls, last = _driver_regate(tmp_path, zim)
    assert "gate " not in calls and "upload " not in calls
    assert last.startswith(f"x\tno-output\t{zim}\t")


# ---- the runner's --regate --------------------------------------------------

def _hold(lock: Path) -> subprocess.Popen:
    """Holds flock $lock (-o: the sleep does not inherit it) until _release."""
    p = subprocess.Popen(["flock", "-o", str(lock), "sleep", "60"], start_new_session=True)
    deadline = time.time() + 10
    while subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0 and time.time() < deadline:
        time.sleep(0.05)
    return p


def _release(p: subprocess.Popen) -> None:
    os.killpg(p.pid, signal.SIGKILL)
    p.wait()


def _regate(tmp: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(RUNNER), "--regate", *args], env=env, cwd=tmp,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("st,want,rc", [("uploaded", "uploaded", 0), ("upload-pending", "upload-pending", 0),
                                        ("gate-failed", "failed", 1)])
def test_runner_regate_runs_one_driver_and_records_the_result(tmp_path, st, want, rc):
    _setup(tmp_path, ["x", "y"])
    env = _env(tmp_path)
    (tmp_path / "fake" / "x.status").write_text(st)
    (tmp_path / "fake" / "x.sleep").write_text("0.2")
    (tmp_path / "root" / "osm-x-2026-10-23.zim").write_text("zim")
    (tmp_path / "state" / "status").mkdir()
    (tmp_path / "state" / "status" / "x").write_text("failed w0 - - 1 2 gate-failed osm-x-2026-10-23.zim\n")
    # works beside a running runner: it does not take the runner's lock
    holder = _hold(tmp_path / "state" / "run.lock")
    try:
        r = _regate(tmp_path, env, "x", "osm-x-2026-10-23.zim")
    finally:
        _release(holder)
    assert r.returncode == rc, r.stdout + r.stderr
    line = (tmp_path / "fake.log").read_text().splitlines()[0]
    assert line.startswith("start x ") and "regate=osm-x-2026-10-23.zim" in line
    assert "ports=19843,19844" in line and "noskip=1" in line and "keep=3" in line and "ml=1" in line
    assert "zstd=unset" in line and "cache=unset" in line and "scratch=unset" in line
    assert _statuses(tmp_path) == {"x": want}
    assert (tmp_path / "state" / "pending" / "x").exists() == (want == "upload-pending")
    assert f"regate x: {st}" in r.stdout or f"FAILED regate x: {st}" in r.stdout


@pytest.mark.parametrize("why", ["running", "other region's zim", "no zim", "old driver", "locked"])
def test_runner_regate_refusals(tmp_path, why):
    _setup(tmp_path, ["x"])
    env = _env(tmp_path)
    zim = "osm-x-2026-10-23.zim"
    (tmp_path / "root" / zim).write_text("zim")
    (tmp_path / "root" / "osm-x-light-2026-10-23.zim").write_text("zim")
    (tmp_path / "state" / "status").mkdir()
    holder = None
    if why == "running":
        (tmp_path / "state" / "status" / "x").write_text("running w0 1 1 1\n")
    elif why == "other region's zim":
        zim = "osm-x-light-2026-10-23.zim"
    elif why == "no zim":
        zim = "osm-x-2026-10-24.zim"
    elif why == "old driver":
        (tmp_path / "driver.sh").write_text(FAKE_DRIVER.replace("# REBUILD_REGATE_ZIM\n", "")
                                            .replace(" regate=${REBUILD_REGATE_ZIM-unset}", ""))
    else:
        holder = _hold(tmp_path / "state" / "regate.lock")
    try:
        r = _regate(tmp_path, env, "x", zim)
    finally:
        if holder:
            _release(holder)
    assert r.returncode == 1 and "FATAL: regate" in r.stdout, r.stdout + r.stderr
    assert not (tmp_path / "fake.log").exists()


def test_runner_skips_a_region_being_regated_until_the_regate_is_gone(tmp_path):
    _setup(tmp_path, ["x"])
    env = _env(tmp_path)
    (tmp_path / "state" / "status").mkdir()
    (tmp_path / "state" / "status" / "x").write_text("regating - - - 1\n")
    holder = _hold(tmp_path / "state" / "regate.lock")
    try:
        _run(tmp_path, env, timeout=60)
        assert not (tmp_path / "fake.log").exists()
    finally:
        _release(holder)
    _run(tmp_path, env, timeout=60)                # that --regate died: built as usual
    assert _starts(tmp_path)[0][0] == "x" and _status(tmp_path, "x") == "uploaded"
