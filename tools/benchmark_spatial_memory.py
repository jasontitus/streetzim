#!/usr/bin/env python3
"""Measure spatial conversion in a fresh process, including Linux anon/cache.

Generate input separately from measurement. Example (synthetic, not China):
  python tools/benchmark_spatial_memory.py generate --graph /data/graph.bin \
      --nodes 4000000 --edges 9600000
  python tools/benchmark_spatial_memory.py run --graph /data/graph.bin \
      --output /data/current --report /data/current.json

Run reference/current in separate, equally limited Docker containers for
cgroup comparisons. The reference is the frozen 29ec7da spatial writer in
tests/fixtures. Validation hashes are computed after timing/sampling stops.
No country data, network access or publishing is needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import shutil
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def generate(path, nodes, edges, geom_size, cells):
    if nodes < 1 or edges < nodes or geom_size < 8 or geom_size % 2 or cells < 1:
        raise ValueError("need nodes > 0, edges >= nodes, even geom-size >= 8, cells > 0")
    if max(nodes, edges, nodes * geom_size) > 0xFFFFFFFF or cells > 10_000:
        raise ValueError("synthetic layout exceeds SZRG uint32 or coordinate limits")
    width = math.ceil(math.sqrt(cells))
    block = 1 << 18
    name = b"Benchmark"
    with path.open("xb") as f:
        f.write(b"SZRG" + struct.pack("<7I", 4, nodes, edges, nodes,
                                       nodes * geom_size, 2, len(name)))
        for s in range(0, nodes, block):
            ids = np.arange(s, min(s + block, nodes), dtype=np.uint64)
            cell = (ids * 104729) % cells
            coords = np.empty((len(ids), 2), dtype="<i4")
            coords[:, 0] = (cell // width).astype(np.int64) * 1_000_000 - 50_000_000
            coords[:, 1] = (cell % width).astype(np.int64) * 1_000_000 - 50_000_000
            coords.tofile(f)
        for s in range(0, nodes + 1, block):
            ids = np.arange(s, min(s + block, nodes + 1), dtype=np.uint64)
            ((ids * edges) // nodes).astype("<u4").tofile(f)
        for s in range(0, edges, block):
            ids = np.arange(s, min(s + block, edges), dtype=np.uint64)
            source = ((ids + 1) * nodes - 1) // edges
            table = np.empty((len(ids), 5), dtype="<u4")
            table[:, 0] = (source + 1 + ids % 2) % nodes
            table[:, 1] = (30 << 24) | 1234
            table[:, 2] = source
            table[:, 3] = 1
            table[:, 4] = ids % 3
            table.tofile(f)
        for s in range(0, nodes + 1, block):
            (np.arange(s, min(s + block, nodes + 1), dtype=np.uint64)
             * geom_size).astype("<u4").tofile(f)
        geom = struct.pack("<ii", 0, 0) + b"\x02\x02" * ((geom_size - 8) // 2)
        for s in range(0, nodes, block):
            f.write(geom * min(block, nodes - s))
        f.write(struct.pack("<3I", 0, 0, len(name)) + name)
    print(json.dumps({"graph": str(path), "nodes": nodes, "edges": edges,
                      "geometry_bytes": nodes * geom_size, "bytes": path.stat().st_size,
                      "synthetic": True, "cells": cells}))


def _memory():
    result = {}
    if sys.platform == "linux":
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
        for key in ("VmRSS", "RssAnon", "RssFile"):
            result[key] = int(status[key].split()[0]) * 1024
        for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                result["Pss"] = int(line.split()[1]) * 1024
        cg = Path("/sys/fs/cgroup")
        if (cg / "memory.current").exists():
            result["cgroup_current"] = int((cg / "memory.current").read_text())
            result["cgroup_peak"] = int((cg / "memory.peak").read_text())
            stat = dict(line.split() for line in (cg / "memory.stat").read_text().splitlines())
            for key in ("anon", "file", "kernel"):
                result["cgroup_" + key] = int(stat.get(key, 0))
    return result


def _validate_report_destination(output, report):
    # Do this before loading/copying a potentially multi-GB source. Refusing
    # every existing path also protects input aliases and prior evidence,
    # including hard links and dangling symlinks.
    try:
        report.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"report path already exists: {report}")
    if report.resolve().is_relative_to(output.resolve()):
        raise ValueError("report must be outside the output directory")


def run(graph, output, report, reference, interval, resident_mib=0, preparation=None):
    _validate_report_destination(output, report)
    from streetzim.routing.reader import load_from_file
    if reference:
        from tests.fixtures.spatial_build_reference import build_spatial
    else:
        from streetzim.routing.spatial import build_spatial
    output.mkdir(parents=True, exist_ok=False)
    # Touch each page to model retained parent state. This is a synthetic
    # reservation, not a measurement of the production build's parent.
    reservation = bytearray(resident_mib * 1024 * 1024)
    for offset in range(0, len(reservation), 4096):
        reservation[offset] = 1
    memory_before_load = _memory()
    stopped = threading.Event()
    samples, errors, peak = [], [], {}
    stage = "load"
    started = time.perf_counter()

    def sample():
        while not stopped.is_set():
            try:
                row = _memory()
                for key, value in row.items():
                    peak[key] = max(peak.get(key, 0), value)
                samples.append({"seconds": time.perf_counter() - started, "stage": stage, **row})
            except (OSError, ValueError, KeyError) as exc:
                errors.append(str(exc))
            stopped.wait(interval)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        g = load_from_file(graph, mapped=not reference)
        loaded = time.perf_counter()
        stage = "spatial"
        _, _cells, meta = build_spatial(g, output_dir=output)
        finished = time.perf_counter()
        memory_at_end = _memory()
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    finally:
        stopped.set()
        thread.join()
    # Reading outputs for parity is deliberately excluded from resource timing.
    digest = hashlib.sha256()
    output_bytes = 0
    for path in sorted(output.glob("*.bin")):
        digest.update(path.name.encode() + b"\0")
        output_bytes += path.stat().st_size
        with path.open("rb") as f:
            for data in iter(lambda: f.read(1 << 20), b""):
                digest.update(data)
    cg = Path("/sys/fs/cgroup")
    result = {"variant": "reference-29ec7da" if reference else "current",
              "python": sys.version, "numpy": np.__version__, "platform": sys.platform,
              "input_bytes": graph.stat().st_size, "nodes": g.num_nodes, "edges": g.num_edges,
              "load_s": loaded - started, "spatial_s": finished - loaded,
              "wall_s": finished - started, "sample_interval_s": interval,
              "process_peak_rss_bytes": rss * (1 if sys.platform == "darwin" else 1024),
              "sampled_peaks_bytes": peak, "memory_at_end": memory_at_end,
              "memory_before_load": memory_before_load,
              "sampling_errors": errors, "samples": samples,
              "output_bytes": output_bytes, "output_sha256": digest.hexdigest(),
              "cells": meta["num_cells"],
              "resident_reservation_bytes": len(reservation),
              "input_preparation": preparation,
              "scope": "spatial conversion with optional synthetic parent reservation; excludes graph generation and validation hashes. Cgroup peak includes input preparation."}
    if (cg / "memory.events").exists():
        result["cgroup_events"] = dict(line.split() for line in (cg / "memory.events").read_text().splitlines())
        result["memory_limit"] = (cg / "memory.max").read_text().strip()
        result["swap_limit"] = (cg / "memory.swap.max").read_text().strip()
    # Exclusive creation also prevents clobbering a path created after the
    # preflight (including a newly created symlink).
    with report.open("x") as destination:
        destination.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="action", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--graph", type=Path, required=True)
    gen.add_argument("--nodes", type=int, default=4_000_000)
    gen.add_argument("--edges", type=int, default=9_600_000)
    gen.add_argument("--geom-size", type=int, default=48)
    gen.add_argument("--cells", type=int, default=4096)
    measure = sub.add_parser("run")
    measure.add_argument("--graph", type=Path, required=True)
    measure.add_argument("--output", type=Path, required=True)
    measure.add_argument("--report", type=Path, required=True)
    measure.add_argument("--reference", action="store_true")
    measure.add_argument("--interval", type=float, default=0.1)
    measure.add_argument("--copy-input", action="store_true", help=
                         "Copy input to a new disk inode inside this cgroup before measurement, so its file cache is charged here. TMPDIR must not be tmpfs.")
    measure.add_argument("--resident-mib", type=int, default=0, help=
                         "Touch and retain this many MiB to model parent memory (synthetic).")
    args = ap.parse_args()
    if args.action == "generate":
        generate(args.graph, args.nodes, args.edges, args.geom_size, args.cells)
    else:
        if not math.isfinite(args.interval) or args.interval <= 0:
            ap.error("--interval must be finite and positive")
        if args.resident_mib < 0:
            ap.error("--resident-mib must be non-negative")
        # Validate the caller's paths before --copy-input substitutes a
        # private graph path; the original source must remain protected.
        _validate_report_destination(args.output, args.report)
        if args.copy_input:
            with tempfile.TemporaryDirectory(prefix="spatial-memory-") as directory:
                graph = Path(directory) / "graph.bin"
                memory_before_copy = _memory()
                started = time.perf_counter()
                with args.graph.open("rb") as source, graph.open("xb") as target:
                    shutil.copyfileobj(source, target, length=8 << 20)
                preparation = {"private_input_copy": True,
                               "copy_s": time.perf_counter() - started,
                               "memory_before_copy": memory_before_copy,
                               "memory_after_copy": _memory(),
                               "source": str(args.graph), "disk_directory": directory}
                run(graph, args.output, args.report, args.reference, args.interval,
                    args.resident_mib, preparation)
        else:
            run(args.graph, args.output, args.report, args.reference, args.interval,
                args.resident_mib, {"private_input_copy": False, "source": str(args.graph)})


if __name__ == "__main__":
    main()
