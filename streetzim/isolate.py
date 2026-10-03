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
import signal
import sys
import tempfile
import traceback
from collections.abc import Callable
from typing import Any


def _child_main(conn: Any, tempdir: str | None, cpus_requested: int | None,
                fn: Callable[..., Any], args: tuple[Any, ...],
                kwargs: dict[str, Any]) -> None:
    from streetzim import cpus
    tempfile.tempdir = tempdir
    if cpus_requested is not None:
        cpus.set_build_cpus(cpus_requested)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    try:
        result = fn(*args, **kwargs)
    except BaseException as e:  # noqa: BLE001 -- every failure goes back to the caller
        tb = traceback.format_exc()
        try:
            conn.send((False, e, tb))
        except Exception:  # noqa: BLE001 -- an exception that does not pickle
            conn.send((False, RuntimeError(tb), tb))
        return
    conn.send((True, result, None))


class ChildTraceback(Exception):
    """The child's traceback, as the cause of the exception it raised."""

    def __str__(self) -> str:
        return f"\n\nin a child process (streetzim.isolate):\n{self.args[0]}"


def _died(name: str, exitcode: int | None) -> RuntimeError:
    if exitcode is not None and exitcode < 0:
        sig = -exitcode
        try:
            signame = signal.Signals(sig).name
        except ValueError:
            signame = f"signal {sig}"
        hint = " (SIGKILL: most often the out-of-memory killer)" if sig == signal.SIGKILL else ""
        how = f"was killed by {signame}{hint}"
    else:
        how = f"exited with code {exitcode} without a result"
    return RuntimeError(f"{name}, run in a child process, {how}")


def run_in_child(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """fn(*args, **kwargs) in a spawned child process; its result. `fn`
    and the arguments must pickle (a module-level function, plain data).

    If this process is interrupted (Ctrl-C, or SIGTERM, which the
    `streetzim` command turns into SystemExit) while it waits, the child is
    stopped too, so the build stops at once and does not leave it writing.
    A child that dies without an answer (the out-of-memory killer) raises
    RuntimeError naming the step and the signal."""
    from streetzim import cpus
    name = getattr(fn, "__qualname__", repr(fn))
    sys.stdout.flush()
    sys.stderr.flush()
    ctx = multiprocessing.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_child_main, name=f"streetzim-{name}",
                        args=(send, tempfile.tempdir, cpus._requested, fn, args, kwargs))
    child.start()
    send.close()
    try:
        try:
            ok, value, tb = receive.recv()
        except EOFError:
            child.join()
            raise _died(name, child.exitcode) from None
        child.join()
    except BaseException:
        if child.is_alive():
            child.terminate()
            child.join(10)
            if child.is_alive():
                child.kill()
                child.join()
        raise
    finally:
        receive.close()
    if ok:
        return value
    raise value from ChildTraceback(tb)
