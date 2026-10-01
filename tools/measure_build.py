#!/usr/bin/env python3
"""Run a build and record wall/CPU time, process-tree memory and disk use.

    python tools/measure_build.py --json out.json --watch /tmp/sz --watch out -- \
        streetzim --name ... --output out --tmp /tmp/sz ...

Linux samples RSS and PSS from /proc; macOS samples RSS using ps and marks
PSS unavailable (null). RSS counts shared pages once per process; Linux
PSS splits them between processes and is the better sizing measurement.
Short spikes between samples can be missed. The command's OS high-water
RSS is also recorded, but is a per-process maximum, not a tree sum. Disk
scans run less often to limit overhead on large caches. Samples are bounded
and their columns and sampling errors are included in the report.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

PAGE = os.sysconf("SC_PAGE_SIZE")


def _pss(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def _descendants(root: int, children: dict[int, list[int]]) -> list[int]:
    todo = [root]
    seen: set[int] = set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        todo.extend(children.get(pid, ()))
    return list(seen)


def tree_mem(root: int) -> tuple[int, int | None]:
    """(RSS, PSS) bytes for root and descendants; unavailable PSS is None."""
    children: dict[int, list[int]] = {}
    if sys.platform == "darwin":
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,rss="], text=True, timeout=5)
        sizes: dict[int, int] = {}
        for line in output.splitlines():
            pid, ppid, kb = (int(v) for v in line.split())
            children.setdefault(ppid, []).append(pid)
            sizes[pid] = kb * 1024
        return sum(sizes.get(p, 0) for p in _descendants(root, children)), None
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
    pss_complete = True
    for pid in _descendants(root, children):
        try:
            with open(f"/proc/{pid}/statm") as f:
                rss += int(f.read().split()[1]) * PAGE
        except (OSError, IndexError, ValueError):
            continue  # exited between the process listing and this read
        value = _pss(pid)
        if value is None:
            pss_complete = False
        else:
            pss += value
    return rss, pss if pss_complete else None


def du(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_blocks * 512
    except OSError:
        return 0  # removed while being sampled
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    ap.add_argument("--json", required=True, help="write the measurements here")
    ap.add_argument("--watch", action="append", default=[], help="file/folder to measure (repeat)")
    ap.add_argument("--interval", type=float, default=2.0, help="memory sample interval (seconds)")
    ap.add_argument("--disk-interval", type=float, default=10.0, help="disk scan interval (seconds)")
    ap.add_argument("--max-samples", type=int, default=10000, help="retain this many recent samples")
    ap.add_argument("--log", help="write the command's output here (default: inherit)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command")
    if any(not math.isfinite(v) or v <= 0 for v in (a.interval, a.disk_interval)):
        ap.error("sample intervals must be finite and greater than zero")
    if a.max_samples < 1:
        ap.error("--max-samples must be greater than zero")
    if sys.platform not in ("linux", "darwin"):
        ap.error("process-tree measurements support Linux and macOS")

    # Do not count the same bytes twice when watched paths overlap.
    paths = sorted({Path(w).resolve() for w in a.watch}, key=lambda p: len(p.parts))
    watch: list[Path] = []
    for path in paths:
        if not any(path.is_relative_to(parent) for parent in watch):
            watch.append(path)
    peak: dict[str, int | None] = {"rss": None, "pss": None, "disk": 0}
    samples: deque[tuple[float, int, int | None, int]] = deque(maxlen=a.max_samples)
    errors: list[str] = []
    sample_count = error_count = 0
    log = open(a.log, "w") if a.log else None
    stop = threading.Event()
    t0 = time.monotonic()
    proc = None
    th = None
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        def terminate(signum: int, frame: object) -> None:
            raise SystemExit(128 + signum)
        previous_sigterm = signal.signal(signal.SIGTERM, terminate)
    try:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT if log else None,
                                start_new_session=True)

        def sample():
            nonlocal sample_count, error_count
            disk = 0
            last_disk = -math.inf
            while not stop.is_set():
                try:
                    rss, pss = tree_mem(proc.pid)
                    now = time.monotonic()
                    if now - last_disk >= a.disk_interval:
                        disk = sum(du(w) for w in watch)
                        peak["disk"] = max(peak["disk"] or 0, disk)
                        last_disk = now
                    peak["rss"] = max(peak["rss"] or 0, rss)
                    if pss is not None:
                        peak["pss"] = max(peak["pss"] or 0, pss)
                    samples.append((round(now - t0, 4), rss, pss, disk))
                    sample_count += 1
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    error_count += 1
                    if len(errors) < 10:
                        errors.append(str(exc))
                stop.wait(a.interval)

        th = threading.Thread(target=sample, daemon=True)
        th.start()
        # wait4 attributes CPU to this command, excluding the monitor's ps
        # subprocesses and any other children. Popen must not reap it first.
        _, status, ru = os.wait4(proc.pid, 0)
        rc = os.waitstatus_to_exitcode(status)
        proc.returncode = rc
    except BaseException:
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # A root that exited promptly may have left a descendant
                # ignoring TERM. Kill the remaining group either way.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        raise
    finally:
        stop.set()
        if th is not None:
            th.join()
        if log is not None:
            log.close()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    wall = time.monotonic() - t0
    peak["disk"] = max(peak["disk"] or 0, sum(du(w) for w in watch))
    result: dict[str, Any] = {
        "command": cmd,
        "exit_code": rc,
        "platform": sys.platform,
        "wall_s": round(wall, 4),
        "cpu_s": round(ru.ru_utime + ru.ru_stime, 4),
        "cpus": os.cpu_count(),
        "detected_cpus": _detected_cpus(),
        "peak_pss_bytes": peak["pss"],
        "peak_rss_bytes": peak["rss"],
        "peak_child_rss_bytes": ru.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        "peak_disk_bytes": peak["disk"],
        "peak_pss_gb": round(peak["pss"] / 1e9, 4) if peak["pss"] is not None else None,
        "peak_rss_gb": round(peak["rss"] / 1e9, 4) if peak["rss"] is not None else None,
        "peak_disk_gb": round((peak["disk"] or 0) / 1e9, 4),
        "interval_s": a.interval,
        "disk_interval_s": a.disk_interval,
        "watched": [str(w) for w in watch],
        "sample_columns": ["elapsed_s", "rss_bytes", "pss_bytes", "disk_bytes"],
        "sample_count": sample_count,
        "samples_dropped": max(0, sample_count - len(samples)),
        "sampling_error_count": error_count,
        "sampling_errors": errors,
        "samples": list(samples),
    }
    Path(a.json).write_text(json.dumps(result, indent=1))
    memory = (f"{result['peak_pss_gb']} GB PSS" if peak["pss"] is not None
              else "PSS unavailable")
    print(f"exit {rc}: wall {wall / 60:.1f} min, cpu {result['cpu_s'] / 60:.1f} min, "
          f"peak memory {memory} ({result['peak_rss_gb']} GB RSS), "
          f"peak disk {result['peak_disk_gb']} GB; sampling errors: {error_count}",
          file=sys.stderr)
    return 128 - rc if rc < 0 else rc


def _detected_cpus() -> list[Any] | None:
    """streetzim.cpus.detect() as [cores, reason], when streetzim is importable."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from streetzim import cpus
        return list(cpus.detect())
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
