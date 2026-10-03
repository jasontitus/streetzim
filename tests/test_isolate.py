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


def _sleep_then_mark(path):
    time.sleep(20)
    Path(path).write_text("finished")


def test_a_child_killed_by_the_oom_killer_says_so():
    with pytest.raises(RuntimeError, match=r"_killed, run in a child process, was killed "
                                           r"by SIGKILL \(SIGKILL: most often the out-of-memory"):
        run_in_child(_killed)


def test_an_interrupted_wait_stops_the_child_at_once(tmp_path):
    """SIGTERM to a `streetzim` build becomes SystemExit while it waits: the
    child must stop with it, not run on to the end of the step."""
    mark = tmp_path / "mark"

    def interrupt(signum, frame):
        raise SystemExit(143)

    old = signal.signal(signal.SIGALRM, interrupt)
    signal.alarm(2)
    t = time.monotonic()
    try:
        with pytest.raises(SystemExit):
            run_in_child(_sleep_then_mark, str(mark))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert time.monotonic() - t < 10
    time.sleep(1)
    assert not mark.exists()
