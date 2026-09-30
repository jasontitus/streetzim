"""How many CPU cores a build uses at once.

Every parallel step asks build_cpus(): tilemaker's threads, the search
step's worker processes, libzim's compression threads, the tile
decompression threads and terrain. Each step keeps its own cap on top.

os.cpu_count() is not the answer inside a container. Docker's --cpu-shares,
which is how Zimfarm gives a task its CPUs, hides no cores, so a build on a
36-core machine started 36 tilemaker threads and 32 search processes, and its
memory grew with the machine rather than the map: tilemaker alone took 7.9 GB
for Luxembourg (1.4 GB with 4 threads, in the same time) and was killed in a
4 GB container.

The default is the cores this process may run on, capped by a CPU quota
(cgroup cpu.max, as `docker run --cpus` sets) and by the memory limit
(cgroup memory.max, as `docker run --memory` sets): one core per
GIB_PER_CPU of memory. A CPU share (cpu.weight) is relative to the other
containers on the machine and says nothing about a core count, so it is not
read. With no limits, as on a build host outside Docker, the default is
every core, as before. --cpus sets it outright.

Stdlib only.
"""
from __future__ import annotations

import math
import os

# Memory per core for the default. A 16 GiB Zimfarm task gets 4 cores, the
# count the builder's published measurements (docs/zimfarm.md) were made at.
GIB_PER_CPU = 4

_requested: int | None = None


def set_build_cpus(n: int | None) -> None:
    """Use `n` cores (from --cpus); None goes back to detecting them."""
    global _requested
    if n is not None and n < 1:
        raise ValueError(f"--cpus must be at least 1, not {n}")
    _requested = n


def build_cpus() -> int:
    """The number of cores the build may use at once (at least 1)."""
    if _requested is not None:
        return _requested
    return detect()[0]


def detect(cgroup_root: str = "/sys/fs/cgroup",
           proc_self_cgroup: str = "/proc/self/cgroup") -> tuple[int, str]:
    """(cores, how they were found), from the cores this process may run on
    and the cgroup's CPU quota and memory limit."""
    n = _usable_cores()
    why = [f"{n} usable cores"]
    dirs = _cgroup_dirs(cgroup_root, proc_self_cgroup)
    quota = _cpu_quota(dirs)
    if quota is not None and quota < n:
        n = quota
        why.append(f"CPU quota {quota}")
    mem = _memory_limit(dirs)
    if mem is not None:
        by_mem = max(1, mem // (GIB_PER_CPU << 30))
        if by_mem < n:
            n = by_mem
            why.append(f"memory limit {mem / (1 << 30):.1f} GiB at {GIB_PER_CPU} GiB per core")
    return max(1, n), ", ".join(why)


def _usable_cores() -> int:
    count = getattr(os, "process_cpu_count", None)     # Python 3.13+
    if count is not None:
        return count() or 1
    try:
        return len(os.sched_getaffinity(0)) or 1
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _cgroup_dirs(root: str, proc_self_cgroup: str) -> list[str]:
    """This process's cgroup v2 directory and its ancestors up to `root`,
    innermost first (a limit on any of them applies). Empty when the
    unified hierarchy is not in use."""
    try:
        with open(proc_self_cgroup, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    rel = next((ln[3:] for ln in lines if ln.startswith("0::")), None)
    if rel is None:
        return []
    parts = [p for p in rel.split("/") if p and p != ".."]
    dirs = [os.path.join(root, *parts[:i]) for i in range(len(parts), -1, -1)]
    return [d for d in dirs if os.path.isdir(d)]


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _cpu_quota(dirs: list[str]) -> int | None:
    """The smallest cpu.max quota, rounded up to whole cores."""
    best = None
    for d in dirs:
        v = _read(os.path.join(d, "cpu.max"))
        if not v:
            continue
        fields = v.split()
        if len(fields) != 2 or fields[0] == "max":
            continue
        try:
            quota, period = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if quota > 0 and period > 0:
            cores = max(1, math.ceil(quota / period))
            best = cores if best is None else min(best, cores)
    return best


def _memory_limit(dirs: list[str]) -> int | None:
    """The smallest memory.max in bytes, or None when there is none."""
    best = None
    for d in dirs:
        v = _read(os.path.join(d, "memory.max"))
        if not v or v == "max":
            continue
        try:
            b = int(v)
        except ValueError:
            continue
        if b > 0:
            best = b if best is None else min(best, b)
    return best
