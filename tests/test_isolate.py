"""streetzim.isolate.run_in_child: a step in a spawned child process, with
the build's temporary folder and --cpus budget, its result and its
exceptions coming back as they were."""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim import cpus  # noqa: E402
from streetzim.isolate import run_in_child  # noqa: E402


def _state(x, *, y):
    from streetzim import cpus as child_cpus
    return (os.getpid(), tempfile.gettempdir(), child_cpus._requested, x + y)


def _fail():
    raise FileNotFoundError("no such extract")


def test_runs_in_another_process_with_the_builds_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(cpus, "_requested", 3)
    monkeypatch.setenv("OSMIUM_POOL_THREADS", "3")
    pid, tmp, requested, total = run_in_child(_state, 2, y=5)
    assert pid != os.getpid()
    assert (tmp, requested, total) == (str(tmp_path), 3, 7)


def test_an_exception_comes_back_as_raised():
    with pytest.raises(FileNotFoundError, match="no such extract") as e:
        run_in_child(_fail)
    assert "in a child process" in str(e.value.__cause__)
    assert "_fail" in str(e.value.__cause__)


def _killed():
    os.kill(os.getpid(), signal.SIGKILL)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill -0: alive only if not reaped-pending.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[1][0] != "Z"
    except FileNotFoundError:
        return False


def _record_pids_then_wait(path, ignore_term=False):
    """Write this pid and a grandchild's (an osmium stand-in), then wait."""
    import subprocess
    if ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    proc = subprocess.Popen(["sleep", "60"])
    Path(path).write_text(f"{os.getpid()} {proc.pid}")
    proc.wait()


def _run_wait_in_subprocess(path):
    import subprocess
    subprocess.run(["sh", "-c", f"echo $$ > {path}.gc; exec sleep 60"], check=True)


def _big():
    return b"x" * (20 << 20)


def _exits():
    sys.exit(0)


class _NeedsTwo(Exception):
    def __init__(self, a, b):
        super().__init__(f"{a}{b}")


def _raise_needs_two():
    raise _NeedsTwo("x", "y")


def test_a_child_killed_by_the_oom_killer_says_so():
    with pytest.raises(RuntimeError, match=r"_killed, run in a child process, was killed "
                                           r"by SIGKILL \(SIGKILL: most often the out-of-memory"):
        run_in_child(_killed)


def _interrupt_after(seconds, fn, *args):
    """fn(*args) with SystemExit raised in this process after `seconds`,
    as the `streetzim` command's SIGTERM handler does."""
    def interrupt(signum, frame):
        raise SystemExit(143)

    old = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    t = time.monotonic()
    try:
        with pytest.raises(SystemExit):
            run_in_child(fn, *args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    return time.monotonic() - t


def _wait_for(path):
    for _ in range(200):
        if path.exists() and path.read_text().strip():
            return path.read_text().split()
        time.sleep(0.05)
    raise AssertionError(f"{path} never written")


def test_an_interrupted_wait_stops_the_child_and_what_it_started(tmp_path):
    """SIGTERM to a `streetzim` build becomes SystemExit while it waits: the
    child, and a subprocess.run it is waiting on (osmium), stop with it."""
    gc = tmp_path / "pids.gc"
    took = _interrupt_after(2, _run_wait_in_subprocess, str(tmp_path / "pids"))
    assert took < 5
    (grandchild,) = _wait_for(gc)
    time.sleep(0.5)
    assert not _alive(int(grandchild))


def test_the_child_itself_is_gone_when_run_in_child_returns(tmp_path):
    pids = tmp_path / "pids"
    _interrupt_after(2, _record_pids_then_wait, str(pids))
    child, grandchild = _wait_for(pids)
    time.sleep(0.5)
    assert not _alive(int(child)) and not _alive(int(grandchild))


def test_a_child_ignoring_sigterm_is_killed_with_what_it_started(tmp_path):
    pids = tmp_path / "pids"
    took = _interrupt_after(2, _record_pids_then_wait, str(pids), True)
    assert took < 4
    child, grandchild = _wait_for(pids)
    time.sleep(0.5)
    assert not _alive(int(child)) and not _alive(int(grandchild))


def _terminate_self():
    os.kill(os.getpid(), signal.SIGTERM)


def test_a_child_killed_by_sigterm_says_so():
    with pytest.raises(RuntimeError, match=r"_terminate_self, run in a child process, "
                                           r"was killed by SIGTERM$"):
        run_in_child(_terminate_self)


def test_a_large_result_comes_back():
    """More than a pipe holds: joining the child before reading would hang."""
    def hung(signum, frame):
        raise AssertionError("run_in_child hung on a large result")

    old = signal.signal(signal.SIGALRM, hung)
    signal.alarm(30)
    try:
        assert len(run_in_child(_big)) == 20 << 20
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_a_step_calling_sys_exit_fails_instead_of_ending_the_build():
    with pytest.raises(RuntimeError, match=r"_exits called sys.exit\(0\)"):
        run_in_child(_exits)


def test_an_exception_that_cannot_be_rebuilt_keeps_its_traceback():
    with pytest.raises(RuntimeError, match="_NeedsTwo: xy"):
        run_in_child(_raise_needs_two)


def _run_and_record(path):
    run_in_child(_record_pids_then_wait, path)


def test_a_child_whose_parent_dies_kills_itself_and_what_it_started(tmp_path):
    """The parent SIGKILLed (or hung up) without stopping its child: the
    child and its osmium must not run on, orphaned."""
    import multiprocessing
    pids = tmp_path / "pids"
    parent = multiprocessing.get_context("spawn").Process(
        target=_run_and_record, args=(str(pids),))
    parent.start()
    child, grandchild = _wait_for(pids)
    os.kill(parent.pid, signal.SIGKILL)
    parent.join()
    for _ in range(100):
        if not _alive(int(child)) and not _alive(int(grandchild)):
            break
        time.sleep(0.1)
    assert not _alive(int(child)) and not _alive(int(grandchild))


def _sleep_long():
    time.sleep(60)


def test_stop_kills_a_child_that_has_no_group_yet():
    """Interrupted before the child's setpgid: killpg finds no group, and
    the child alone (which has started nothing) is killed."""
    import multiprocessing

    from streetzim.isolate import _stop
    child = multiprocessing.get_context("spawn").Process(target=_sleep_long)
    child.start()
    t = time.monotonic()
    _stop(child)
    assert time.monotonic() - t < 10 and child.exitcode == -signal.SIGKILL


def _record_pid_then_hold_the_gil(path):
    """A long C loop that never lets another thread run, as a pyosmium
    node pass does."""
    Path(path).write_text(f"{os.getpid()} {os.getpid()}")
    sum(range(10 ** 12))


def _run_hold_and_record(path):
    run_in_child(_record_pid_then_hold_the_gil, path)


def test_a_child_holding_the_gil_still_dies_with_its_parent(tmp_path):
    import multiprocessing
    pids = tmp_path / "pids"
    parent = multiprocessing.get_context("spawn").Process(
        target=_run_hold_and_record, args=(str(pids),))
    parent.start()
    child, _ = _wait_for(pids)
    os.kill(parent.pid, signal.SIGKILL)
    parent.join()
    for _ in range(50):
        if not _alive(int(child)):
            break
        time.sleep(0.1)
    alive = _alive(int(child))
    if alive:
        os.kill(int(child), signal.SIGKILL)
    assert not alive
