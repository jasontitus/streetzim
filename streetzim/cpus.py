"""How many CPU cores a build uses at once.

Every parallel step asks build_cpus(): tilemaker's threads, the search
step's worker processes and terrain's. Each step keeps its own cap on top.
libzim's compression threads and the tile decompression threads ask
compression_cpus(), which leaves out the memory rule: they cost about 43 MB
each (20 took 1 GB in all), and capping them costs time (4 compression
threads took 71 s where 20 took 17 s).

os.cpu_count() is not the answer inside a container. Docker's --cpu-shares,
which is how Zimfarm gives a task its CPUs, hides no cores, so a build on a
36-core machine started 36 tilemaker threads and 32 search processes, and its
memory grew with the machine rather than the map: tilemaker alone took 7.9 GB
for Luxembourg (1.4 GB with 4 threads, in the same time) and was killed in a
4 GB container.

The default is the cores this process may run on, capped by a CPU quota
(cgroup cpu.max, as `docker run --cpus` sets) and by the memory limit
(cgroup memory.max, as `docker run --memory` sets): one core per
GIB_PER_CPU of memory, the limit first rounded to whole GiB. A CPU share
(cpu.weight) is relative to the other containers on the machine and says
nothing about a core count, so it is not read. cgroup v1 (memory.limit_in_bytes,
cpu.cfs_quota_us) is read when there is no v2 hierarchy. With no limits, as
on a build host outside Docker, the default is every core, as before.
--cpus sets it outright; a Zimfarm recipe should set it to its `cpu`.

set_build_cpus() also exports the count as OSMIUM_POOL_THREADS, which sizes
libosmium's worker pool in the osmium tool and pyosmium (otherwise sized
from the machine's cores), unless the environment already sets it.

Stdlib only.
"""
from __future__ import annotations

import math
import os

# Memory per core for the default. tilemaker costs about 0.25 GB per thread
# on top of a fixed part that grows with the region (the Netherlands: 3.8 GB
# at 4 threads, 4.7 GB at 8, 11.9 GB at 36), and each thread saves time
# (4 threads took 185 s there, 8 took 106 s, 36 took 60 s). A 16 GiB task
# gets 8 cores; the recipes docs/zimfarm.md recommends (6 to 14 GiB) get
# 3 to 7.
GIB_PER_CPU = 2

_requested: int | None = None
_exported: str | None = None          # the OSMIUM_POOL_THREADS this module set


def set_build_cpus(n: int | None) -> None:
    """Use `n` cores (from --cpus); None goes back to detecting them. Also
    sizes libosmium's pool for the processes this one starts."""
    global _requested, _exported
    if n is not None and n < 1:
        raise ValueError(f"--cpus must be at least 1, not {n}")
    _requested = n
    current = os.environ.get("OSMIUM_POOL_THREADS")
    if current is None or current == _exported:       # not the user's own setting
        _exported = str(build_cpus())
        os.environ["OSMIUM_POOL_THREADS"] = _exported


def build_cpus() -> int:
    """The number of cores the build may use at once (at least 1)."""
    if _requested is not None:
        return _requested
    return detect()[0]


def compression_cpus() -> int:
    """Cores for libzim's compression threads and the tile decompression
    threads: --cpus, else build_cpus() without the memory rule."""
    if _requested is not None:
        return _requested
    return detect(memory_rule=False)[0]


def detect(cgroup_root: str = "/sys/fs/cgroup",
           proc_self_cgroup: str = "/proc/self/cgroup",
           memory_rule: bool = True) -> tuple[int, str]:
    """(cores, how they were found), from the cores this process may run on
    and the cgroup's CPU quota and (with memory_rule) memory limit."""
    n = _usable_cores()
    why = [f"{n} usable cores"]
    quota, mem = _limits(cgroup_root, proc_self_cgroup)
    if quota is not None and quota < n:
        n = quota
        why.append(f"CPU quota {quota}")
    if mem is not None and memory_rule:
        gib = round(mem / (1 << 30))        # 7.99g and 8e9 bytes count as 8 and 7
        by_mem = max(1, gib // GIB_PER_CPU)
        if by_mem < n:
            n = by_mem
            why.append(f"memory limit {gib} GiB at {GIB_PER_CPU} GiB per core")
    return max(1, n), ", ".join(why)


def memory_limit(cgroup_root: str = "/sys/fs/cgroup",
                 proc_self_cgroup: str = "/proc/self/cgroup") -> int | None:
    """This process's cgroup memory limit in bytes; None when there is none."""
    return _limits(cgroup_root, proc_self_cgroup)[1]


def _limits(cgroup_root: str, proc_self_cgroup: str) -> tuple[int | None, int | None]:
    """(CPU quota in cores, memory limit in bytes), from cgroup v2 or v1."""
    # v2 when the root is a unified hierarchy (on a hybrid host it is a v1
    # tmpfs, and the v2 files looked for below would simply be missing).
    v2 = os.path.exists(os.path.join(cgroup_root, "cgroup.controllers"))
    dirs = _cgroup_dirs(cgroup_root, proc_self_cgroup) if v2 else []
    if dirs:
        return _cpu_quota(dirs), _memory_limit(dirs)
    return _v1_limits(cgroup_root)


def _usable_cores() -> int:
    count = getattr(os, "process_cpu_count", None)     # Python 3.13+
    if count is not None:
        return count() or 1
    try:
        affinity = getattr(os, "sched_getaffinity", None)
        if affinity is None:
            return os.cpu_count() or 1
        return len(affinity(0)) or 1
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _cgroup_dirs(root: str, proc_self_cgroup: str) -> list[str]:
    """This process's cgroup v2 directory and its ancestors up to `root`,
    innermost first (a limit on any of them applies). Empty when the
    unified hierarchy is not in use."""
    try:
        with open(proc_self_cgroup, encoding="utf-8", errors="surrogateescape") as f:
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
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


# cgroup v1 reports "no limit" as a huge number (PAGE_COUNTER_MAX pages).
_V1_UNLIMITED = 1 << 60


def _v1_limits(root: str) -> tuple[int | None, int | None]:
    """(CPU quota in cores, memory limit in bytes) from a cgroup v1 mount,
    as a container sees its own limits at the controller roots."""
    quota = None
    q = _read(os.path.join(root, "cpu", "cpu.cfs_quota_us"))
    p = _read(os.path.join(root, "cpu", "cpu.cfs_period_us"))
    try:
        if q is not None and p is not None and int(q) > 0 and int(p) > 0:
            quota = max(1, math.ceil(int(q) / int(p)))
    except ValueError:
        pass
    mem = None
    m = _read(os.path.join(root, "memory", "memory.limit_in_bytes"))
    try:
        if m is not None and 0 < int(m) < _V1_UNLIMITED:
            mem = int(m)
    except ValueError:
        pass
    return quota, mem


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
