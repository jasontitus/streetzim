"""streetzim.isolate.run_in_child: a step in a spawned child process, with
the build's temporary folder and --cpus budget, its result and its
exceptions coming back as they were."""
from __future__ import annotations

import os
import sys
import tempfile
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
    with pytest.raises(FileNotFoundError, match="no such extract"):
        run_in_child(_fail)
