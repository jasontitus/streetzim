"""Run one memory-heavy step in a child process, so the memory it used goes
back to the system when the step ends.

CPython and glibc keep much of what a step allocated after it returns:
China's routing graph peaked at 14 GB of the build's process and left
6.7 GB behind, which terrain then had to fit beside in a 16 GB container
(it did not: killed at z8, 2026-10-03). A child process returns all of it.

The child is spawned (not forked: no copy of the parent's memory) and gets
what the parent set up for the build: the temporary folder
(tempfile.tempdir), the --cpus budget, and line-buffered output, so its
progress reaches the build log as it happens. Exceptions come back to the
caller as they were raised.
"""
from __future__ import annotations

import multiprocessing
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any


def _child_init(tempdir: str | None, cpus_requested: int | None) -> None:
    from streetzim import cpus
    tempfile.tempdir = tempdir
    if cpus_requested is not None:
        cpus.set_build_cpus(cpus_requested)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def run_in_child(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """fn(*args, **kwargs) in a spawned child process; its result. `fn`
    and the arguments must pickle (a module-level function, plain data)."""
    from streetzim import cpus
    sys.stdout.flush()
    sys.stderr.flush()
    with ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn"),
            initializer=_child_init,
            initargs=(tempfile.tempdir, cpus._requested)) as pool:
        return pool.submit(fn, *args, **kwargs).result()
