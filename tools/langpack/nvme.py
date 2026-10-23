#!/usr/bin/env python3
"""The language step's scratch on the NVMe, shared with the map builds.

    nvme.py plan  --scratch DIR --base BASE --packs-dir DIR [--gate-jobs N] [--search-jobs N]
    nvme.py hold  --run-dir DIR --need BYTES --parent PID
    nvme.py stage BASE DIR
    nvme.py sweep --scratch DIR

ship_packs.sh puts a run's merged files, temporary files and search-pack
builds, and a verified copy of the base, in its own folder on the NVMe
(DIR/run-<region>-<pid>), when the volume has room for them beside what the
map builds may still write. /mnt/data is shared with another project and
must keep NVME_MIN_GB (100) free, so the room is counted as
ops/cloud/parallel_rebuild.sh counts it before it starts a build:

    free - what running builds may still write (the parallel runner's
    projections less what their scratch holds) - the live reservations
    (the .reserve-ml-* files in $NVME_SCRATCH, as ops/region-ml-tiles.py
    reads them) - this run's need >= NVME_MIN_GB

and the run reserves what it may still write in a reservation of its own
there (`hold`: .reserve-ml-langpack-<pid>, "<bytes> <start time>", rewritten
every 30 s), which the runner, the tile stores and the stagers count as used.

plan: the gate and search jobs that fit (each gate job holds a merged file
and its copy from the recipe; each search build its index files), also no
more than MemAvailable allows above LANGPACK_MIN_FREE_GB. Prints JSON; "use"
false when not even one job fits (the run then works on /storage as before).
stage: copies the base (and its .parent.json, which base_lineage reads) into
DIR with its timestamps, checks the copy's size and MD5 against the stream it
was copied from, prints the copy's path. sweep: removes run folders whose
process is gone. Tests: LANGPACK_NVME_FREE (the free bytes to count, as
parallel_rebuild.sh's PAR_FREE_GB_CMD), NVME_SCRATCH, PAR_STATE, PAR_SCRATCH.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GB = 1 << 30
ACCT = "/mnt/data/tilemaker/streetzim-scratch"     # build-region-fast.sh's NVME_SCRATCH: the reservations
PAR_STATE = "/storage/streetzim/rebuild-par"
PAR_SCRATCH = "/mnt/data/tilemaker/streetzim-par"
REFRESH = 30
GATE_MEM_GB = 4                 # a gate job: merge or verify (under 8 GB --as), kiwix-serve, one Chromium
SEARCH_MEM_GB = 2               # a search build: python-libzim's writer (about 600 MB on Switzerland)


def _ml():
    """ops/region-ml-tiles.py, whose reading of the reservations this shares."""
    spec = importlib.util.spec_from_file_location("region_ml_tiles", os.path.join(ROOT, "ops", "region-ml-tiles.py"))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def acct_dir() -> str:
    return os.environ.get("NVME_SCRATCH") or ACCT


def min_bytes() -> int:
    return int(float(os.environ.get("LANGPACK_NVME_MIN_GB") or os.environ.get("PAR_NVME_MIN_GB") or 100) * GB)


def proc_start(pid: int | str) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        return s[s.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def apparent_bytes(path: str) -> int:
    total = 0
    for top, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(top, f)).st_size
            except OSError:
                pass
    return total


def runner_committed() -> int:
    """What the parallel runner's running builds may still write: each one's
    projection (proj=<n>G in its status file) less what its scratch holds,
    as parallel_rebuild.sh's start guard counts it (GiB, rounded up)."""
    state = os.environ.get("PAR_STATE") or PAR_STATE
    scratch = os.environ.get("PAR_SCRATCH") or PAR_SCRATCH
    total = 0
    for p in glob.glob(os.path.join(state, "status", "*")):
        if p.endswith((".tmp", ".mult")):
            continue
        try:
            f = open(p).readline().split()
        except OSError:
            continue
        if len(f) < 2 or f[0] != "running":
            continue
        proj = next((int(x[5:-1]) for x in f if x.startswith("proj=") and x.endswith("G") and x[5:-1].isdigit()), 0)
        used = -(-apparent_bytes(os.path.join(scratch, f[1])) // GB)
        total += max(0, proj - used) * GB
    return total


def reserved_by_others(own: str | None = None) -> int:
    """The live reservations in the shared folder (ops/region-ml-tiles.py's
    nvme_reserved), less this run's own."""
    d = acct_dir()
    if not os.path.isdir(d):
        return 0
    total = _ml().nvme_reserved(d)
    if own and os.path.exists(own):
        try:
            b, st = open(own).read().split()
            pid = os.path.basename(own).rsplit("-", 1)[-1]
            if proc_start(pid) == st:
                total -= int(b)
        except ValueError:
            pass
    return max(0, total)


def mem_available() -> int:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return 0


def plan(scratch: str, base: str, packs_dir: str, gate_jobs: int, search_jobs: int) -> dict:
    os.makedirs(scratch, exist_ok=True)
    bsize = os.path.getsize(base)
    packs = [p for p in glob.glob(os.path.join(packs_dir, "*.zim")) if "_search-" not in os.path.basename(p)]
    psize = sum(os.path.getsize(p) for p in packs)
    searches = [os.path.getsize(p) for p in glob.glob(os.path.join(packs_dir, "*_search-*.zim"))]
    search = max(searches + [bsize // 8])            # a search pack: about a ninth of the base on Switzerland
    merged = bsize + psize + search                   # at most the base, every pack and its search pack
    per_gate, per_search = 2 * merged, 4 * search     # a merged file and its copy from the recipe; a build's index files
    free = int(os.environ.get("LANGPACK_NVME_FREE") or shutil.disk_usage(scratch).free)
    others = reserved_by_others()
    committed = runner_committed()
    room = free - others - committed - min_bytes()
    floor = int(float(os.environ.get("LANGPACK_MIN_FREE_GB") or 0) * GB)
    mem = mem_available() - floor
    g = max(0, min(gate_jobs, mem // (GATE_MEM_GB * GB), (room - bsize) // per_gate if room > bsize else 0))
    s = max(0, min(search_jobs, mem // (SEARCH_MEM_GB * GB), (room - bsize) // per_search if room > bsize else 0))
    need = bsize + max(g * per_gate, s * per_search) + GB
    row = {"use": g >= 1 and s >= 1, "gate_jobs": int(g), "search_jobs": int(s), "need": int(need),
           "base": bsize, "per_gate": per_gate, "per_search": per_search,
           "free_gb": round(free / GB, 1), "reserved_gb": round(others / GB, 1), "runner_gb": round(committed / GB, 1),
           "min_gb": round(min_bytes() / GB), "mem_gb": round(mem_available() / GB, 1),
           "per_gate_gb": round(per_gate / GB, 1), "per_search_gb": round(per_search / GB, 1)}
    if not row["use"]:
        row["why"] = (f"NVMe {row['free_gb']} GiB free - {row['reserved_gb']} reserved - {row['runner_gb']} the "
                      f"runner's builds may still write - {row['min_gb']} kept free leaves no room for the base and "
                      f"one job" if g < 1 or s < 1 else "")
        if mem < GATE_MEM_GB * GB:
            row["why"] = f"MemAvailable {row['mem_gb']} GiB leaves no room above LANGPACK_MIN_FREE_GB"
    return row


def hold(run_dir: str, need: int, parent: int) -> int:
    """Reserve what this run may still write (need - what run_dir holds)
    while `parent` lives; the file goes when the parent does, or on a signal."""
    d = acct_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f".reserve-ml-langpack-{os.getpid()}")
    tmp = os.path.join(d, f".langpack-reserve-tmp-{os.getpid()}")    # not matching the readers' .reserve-ml-* glob
    since = proc_start(os.getpid())
    pstart = proc_start(parent)

    def write() -> None:
        with open(tmp, "w") as f:
            f.write(f"{max(0, need - apparent_bytes(run_dir))} {since}\n")
        os.replace(tmp, path)

    def stop(*_a) -> None:
        for p in (path, tmp):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        while pstart is not None and proc_start(parent) == pstart:
            write()
            for _ in range(REFRESH * 4):
                if proc_start(parent) != pstart:
                    break
                time.sleep(0.25)
    finally:
        stop()
    return 0


def stage(base: str, dest: str) -> str:
    """The base copied into dest (timestamps kept), checked: the copy's size
    and MD5 equal the stream it was copied from. Its .parent.json too."""
    os.makedirs(dest, exist_ok=True)
    out = os.path.join(dest, os.path.basename(base))
    part = out + ".part"
    h = hashlib.md5(usedforsecurity=False)
    with open(base, "rb") as src, open(part, "wb") as dst:
        while b := src.read(16 << 20):
            h.update(b)
            dst.write(b)
    want = h.hexdigest()
    g = hashlib.md5(usedforsecurity=False)
    with open(part, "rb") as fh:
        while b := fh.read(16 << 20):
            g.update(b)
    if g.hexdigest() != want or os.path.getsize(part) != os.path.getsize(base):
        os.unlink(part)
        raise SystemExit(f"stage: the copy of {base} does not match what was read (md5 {g.hexdigest()} != {want})")
    shutil.copystat(base, part)
    os.replace(part, out)
    side = os.path.realpath(base) + ".parent.json"
    if os.path.exists(side):
        shutil.copy2(side, out + ".parent.json")
    return out


def sweep(scratch: str) -> list[str]:
    """Remove run folders (run-<region>-<pid>) whose process is gone or is
    another process now (its start time, in <folder>/.owner)."""
    gone = []
    for d in glob.glob(os.path.join(scratch, "run-*-*")):
        pid = d.rsplit("-", 1)[-1]
        try:
            owner = open(os.path.join(d, ".owner")).read().split()
        except OSError:
            owner = []
        if not pid.isdigit() or proc_start(pid) is None or (len(owner) == 2 and proc_start(pid) != owner[1]):
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d)
    return gone


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--scratch", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--packs-dir", required=True)
    p.add_argument("--gate-jobs", type=int, default=4)
    p.add_argument("--search-jobs", type=int, default=4)
    h = sub.add_parser("hold")
    h.add_argument("--run-dir", required=True)
    h.add_argument("--need", type=int, required=True)
    h.add_argument("--parent", type=int, required=True)
    s = sub.add_parser("stage")
    s.add_argument("base")
    s.add_argument("dest")
    w = sub.add_parser("sweep")
    w.add_argument("--scratch", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        print(json.dumps(plan(a.scratch, a.base, a.packs_dir, a.gate_jobs, a.search_jobs)))
    elif a.cmd == "hold":
        return hold(a.run_dir, a.need, a.parent)
    elif a.cmd == "stage":
        print(stage(a.base, a.dest))
    else:
        for d in sweep(a.scratch):
            print(f"removed {d}: its run is gone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
