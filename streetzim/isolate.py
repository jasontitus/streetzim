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
import pickle
import signal
import sys
import tempfile
import traceback
from collections.abc import Callable
from typing import Any


def _parent_of(pid: int) -> int | None:
    """`pid`'s parent pid from /proc (Linux), None without /proc. It changes
    when the parent exits, not when it is reaped, so a zombie parent (one
    whose own parent is still reading its output) counts as gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return int(f.read().rsplit(b")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def _proc_is_ours() -> bool:
    """/proc shows this PID namespace's processes (not so under
    `unshare --pid` without a new /proc mount, where the pids it lists are
    another namespace's)."""
    import os
    try:
        return os.readlink("/proc/self") == str(os.getpid())
    except OSError:
        return False


def _parent_gone(me: int, parent: int, use_proc: bool = True) -> bool:
    import os
    ppid = _parent_of(me) if use_proc else None
    if ppid is not None:
        return ppid != parent
    try:  # no /proc: a signal probe (a zombie parent still answers)
        os.kill(parent, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # the pid is another user's now: reused
        return True
    return False


def _watch_parent(parent: int) -> None:
    """Fork a watcher into this (new) process group: when `parent` is gone
    it SIGKILLs the group, this process and the osmium it started with it.
    A process, not a thread: pyosmium holds the GIL through a whole node
    pass (18 s for a 165 MB extract, minutes for China), which would stall
    a thread's check that long. The watcher leaves when this process does."""
    import os
    import time
    me = os.getpid()
    if os.fork() != 0:
        return
    try:
        use_proc = _proc_is_ours()
        while os.getppid() == me:
            if _parent_gone(me, parent, use_proc):
                os.killpg(0, signal.SIGKILL)
            time.sleep(1)
    finally:
        os._exit(0)


def _child_main(conn: Any, parent: int, tempdir: str | None, cpus_requested: int | None,
                fn: Callable[..., Any], args: tuple[Any, ...],
                kwargs: dict[str, Any]) -> None:
    import os

    from streetzim import cpus
    # A process group of its own, which run_in_child stops with one SIGKILL:
    # the child and the osmium it started go together. Not SIGTERM turned
    # into SystemExit: raised from a signal handler inside a pyosmium pass,
    # that segfaults pyosmium (4.3.1), every time, and the crash reporter
    # may write the core (gigabytes) to the root disk. The group also keeps
    # a terminal's Ctrl-C to the run_in_child caller, which stops it.
    os.setpgid(0, 0)
    # Out of the terminal's process group, the child no longer gets its
    # SIGHUP, nor the SIGKILL of a `kill -9 -- -PGID`: if the parent dies
    # without stopping it, the group kills itself. `parent` comes from the
    # parent, not getppid() here: a parent dead before this line would
    # otherwise be read as its reaper. (Terminal job control, Ctrl-Z and
    # `stty tostop`, applies to the parent only.)
    _watch_parent(parent)
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
        if isinstance(e, SystemExit):
            # Not the caller's exit: a step that called sys.exit failed.
            e = RuntimeError(f"{getattr(fn, '__qualname__', fn)} called sys.exit({e.code!r})")
        try:
            # Must come back whole: pickling alone passes an exception
            # whose __init__ cannot be called with its args again.
            pickle.loads(pickle.dumps(e))
            conn.send((False, e, tb))
        except Exception:  # noqa: BLE001 -- an exception that does not travel
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


def _stop(child: Any) -> None:
    """SIGKILL the child's process group (the child and what it started),
    then reap it. Nothing in the step needs to unwind: its scratch files
    are in the build's work folder, which the build removes."""
    import os
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Not yet its own group (stopped as it started): the child alone,
        # which has started nothing yet.
        child.kill()
    child.join()


def run_in_child(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """fn(*args, **kwargs) in a spawned child process; its result. `fn`
    and the arguments must pickle (a module-level function, plain data).

    If this process is interrupted (Ctrl-C, or SIGTERM, which the
    `streetzim` command turns into SystemExit) while it waits, the child and
    everything it started are killed at once, so the build stops and does
    not leave them writing.
    A child that dies without an answer (the out-of-memory killer) raises
    RuntimeError naming the step and the signal."""
    import os

    from streetzim import cpus
    name = getattr(fn, "__qualname__", repr(fn))
    sys.stdout.flush()
    sys.stderr.flush()
    ctx = multiprocessing.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_child_main, name=f"streetzim-{name}",
                        args=(send, os.getpid(), tempfile.tempdir, cpus._requested,
                              fn, args, kwargs))
    child.start()
    send.close()
    try:
        try:
            # Read before joining: a large result fills the pipe, and the
            # child cannot exit until it is read.
            ok, value, tb = receive.recv()
        except EOFError:
            child.join()
            raise _died(name, child.exitcode) from None
        child.join()
    except BaseException:
        _stop(child)
        raise
    finally:
        receive.close()
        if child.exitcode is not None:
            child.close()
    if ok:
        return value
    raise value from ChildTraceback(tb)
