#!/usr/bin/env python3
"""Run a build and record what it costs: wall time, CPU time, peak memory of
the whole process tree (tilemaker, osmium and worker processes included),
and peak disk use of the folders it writes to.

    python tools/measure_build.py --json out.json --watch /tmp/sz --watch out -- \\
        streetzim --name ... --output out --tmp /tmp/sz ...

Memory is summed over the command and all its descendants, sampled every
--interval seconds (short spikes between samples can be missed), two ways:
PSS, which splits pages shared between processes (forked workers) among
them and so adds up to real use, and RSS, which counts shared pages once per
process and so overstates it. Size a machine by PSS.
Disk is the total size of the --watch folders, sampled at the same rate.
Linux only (reads /proc). Used for docs/zimfarm.md.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PAGE = os.sysconf("SC_PAGE_SIZE")


def _pss(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def tree_mem(root: int) -> tuple[int, int]:
    """(RSS, PSS) in bytes of `root` and every descendant."""
    children: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(d))
    rss = pss = 0
    todo = [root]
    while todo:
        pid = todo.pop()
        try:
            with open(f"/proc/{pid}/statm") as f:
                rss += int(f.read().split()[1]) * PAGE
        except (OSError, IndexError, ValueError):
            pass
        pss += _pss(pid)
        todo.extend(children.get(pid, ()))
    return rss, pss


def du(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_blocks * 512
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    ap.add_argument("--json", required=True, help="write the measurements here")
    ap.add_argument("--watch", action="append", default=[], help="folder to measure (repeat)")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--log", help="write the command's output here (default: inherit)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command")

    watch = [Path(w) for w in a.watch]
    peak = {"rss": 0, "pss": 0, "disk": 0}
    samples: list[tuple[float, int, int, int]] = []
    log = open(a.log, "w") if a.log else None
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT if log else None)
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            rss, pss = tree_mem(proc.pid)
            disk = sum(du(w) for w in watch)
            peak["rss"] = max(peak["rss"], rss)
            peak["pss"] = max(peak["pss"], pss)
            peak["disk"] = max(peak["disk"], disk)
            samples.append((round(time.time() - t0, 1), rss, pss, disk))
            stop.wait(a.interval)

    th = threading.Thread(target=sample, daemon=True)
    th.start()
    rc = proc.wait()
    stop.set()
    th.join()
    wall = time.time() - t0
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    result: dict[str, Any] = {
        "command": cmd,
        "exit_code": rc,
        "wall_s": round(wall, 1),
        "cpu_s": round(ru.ru_utime + ru.ru_stime, 1),
        "cpus": os.cpu_count(),
        "peak_pss_gb": round(peak["pss"] / 1e9, 2),
        "peak_rss_gb": round(peak["rss"] / 1e9, 2),
        "peak_disk_gb": round(peak["disk"] / 1e9, 2),
        "watched": [str(w) for w in watch],
        "samples": samples,
    }
    Path(a.json).write_text(json.dumps(result, indent=1))
    print(f"exit {rc}: wall {wall / 60:.1f} min, cpu {result['cpu_s'] / 60:.1f} min, "
          f"peak memory {result['peak_pss_gb']} GB PSS ({result['peak_rss_gb']} GB RSS), "
          f"peak disk {result['peak_disk_gb']} GB",
          file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
