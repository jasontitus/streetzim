"""Where tilemaker keeps the extract's nodes, ways and relations: in memory,
or in files in a store folder (tilemaker's --store), which it maps into
memory and deletes when it exits.

Measured with tilemaker v3.0.0 at 4 threads in a --memory 16g container
(anonymous memory at its peak, wall time; docs/zimfarm.md, "CPUs and
memory"): Luxembourg (48 MB extract) 1.48 GB in memory, 0.46 GB on disk,
12 s both; Switzerland (547 MB) 2.74 / 0.67 GB, 96 / 90 s; the Netherlands
(1.40 GB) 4.02 / 0.65 GB, 194 / 184 s. The disk store was no slower at any
size, so what keeps memory as the choice for small regions is the disk the
store takes: 2.5 GB for the Netherlands (1.8 times what tilemaker read),
at least 14 GB for China's 6.5 GB cut (2.2 times; sampled once, mid-run).

The decision is made on the file tilemaker reads (the extract cut to the
area, when the builder cut it), just before tilemaker runs. Stdlib only.
"""
from __future__ import annotations

import os
import shutil

MODES = ("auto", "memory", "disk")
DIR_NAME = "tilemaker-store"
# auto: disk above this size of what tilemaker reads (the Netherlands' 1.4 GB
# took 4 GB in memory; China's 6.5 GB would take about 15 GB, near a 16 GiB
# task's limit)...
DISK_ABOVE_BYTES = 1 << 30
# ...or when the estimate below is over this share of the memory limit (the
# rest is the Python process that runs tilemaker, the page cache and the
# estimate's error). The estimate fits the three regions above within 15%
# (over, for the Netherlands): a fixed 0.5 GB, 0.25 GB per thread
# (docs/zimfarm.md) and 2.2 bytes per byte read.
LIMIT_SHARE = 0.5
FIXED_BYTES = 0.5e9
THREAD_BYTES = 0.25e9
EXTRACT_FACTOR = 2.2
# Free disk the store needs, per byte read: 1.8 and 2.2 were measured, and
# running out is fatal (tilemaker dies of SIGBUS on its mapped files).
FREE_FACTOR = 3.0


class StoreError(Exception):
    """--tilemaker-store disk, without the disk for it."""


def memory_estimate(read_bytes: int, threads: int) -> float:
    """tilemaker's memory with its in-memory store, in bytes (an estimate)."""
    return FIXED_BYTES + THREAD_BYTES * threads + EXTRACT_FACTOR * read_bytes


def choose(mode: str, read_bytes: int, threads: int,
           memory_limit: int | None) -> tuple[bool, str]:
    """(use the disk store, why), for `mode` and the size tilemaker reads,
    before any disk space check."""
    if mode != "auto":
        return mode == "disk", f"--tilemaker-store {mode}"
    gb = read_bytes / 1e9
    if read_bytes > DISK_ABOVE_BYTES:
        return True, f"auto: tilemaker reads {gb:.2f} GB, over {DISK_ABOVE_BYTES >> 30} GiB"
    est = memory_estimate(read_bytes, threads)
    if memory_limit is not None and est > LIMIT_SHARE * memory_limit:
        return True, (f"auto: tilemaker would take about {est / 1e9:.1f} GB in memory "
                      f"({threads} threads, {gb:.2f} GB to read), over "
                      f"{LIMIT_SHARE:.0%} of the {memory_limit / 1e9:.1f} GB limit")
    return False, (f"auto: tilemaker reads {gb:.2f} GB, "
                   f"about {est / 1e9:.1f} GB in memory")


def free_bytes(path: str) -> int:
    """Free space on the filesystem `path` is (or will be) on."""
    p = os.path.abspath(path)
    while not os.path.exists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return shutil.disk_usage(p).free


def decide(mode: str, pbf: str, store: str, threads: int,
           memory_limit: int | None) -> str | None:
    """The store folder to give tilemaker, or None for memory. Prints why.
    Raises StoreError when disk is forced and the store's filesystem has
    less than FREE_FACTOR times `pbf` free; auto then falls back to memory."""
    size = os.path.getsize(pbf)
    disk, why = choose(mode, size, threads, memory_limit)
    if disk:
        need, free = FREE_FACTOR * size, free_bytes(store)
        if free < need:
            short = (f"{free / 1e9:.1f} GB free in {store}, and the store needs "
                     f"about {need / 1e9:.1f} GB ({FREE_FACTOR:g} times the "
                     f"{size / 1e9:.2f} GB tilemaker reads)")
            if mode == "disk":
                raise StoreError(f"--tilemaker-store disk: {short}; free some disk "
                                 "or give --tmp on a larger filesystem")
            print(f"    WARNING: tilemaker store: memory, not disk: {short}", flush=True)
            return None
    print(f"    tilemaker store: {'disk, ' + store if disk else 'memory'} ({why})",
          flush=True)
    return store if disk else None
