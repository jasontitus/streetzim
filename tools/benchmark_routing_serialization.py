#!/usr/bin/env python3
"""Measure the removed geometry bytes() copy in fresh synthetic subprocesses.

Tracemalloc starts after constructing the geometry bytearray, so its peak
isolates serialization allocations. This does not measure complete graph
construction, process-tree memory or end-to-end build throughput.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc


def sample(mib: int, variant: str) -> dict:
    geometry = bytearray(mib * 1024 * 1024)
    with tempfile.TemporaryDirectory(prefix="streetzim-geometry-benchmark-") as scratch:
        path = Path(scratch) / "geometry.bin"
        tracemalloc.start()
        start = time.perf_counter()
        with path.open("wb") as output:
            output.write(bytes(geometry) if variant == "copy" else geometry)
        elapsed = time.perf_counter() - start
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"variant": variant, "geometry_bytes": len(geometry),
                "written_bytes": path.stat().st_size, "duration_seconds": elapsed,
                "serialization_python_peak_bytes": peak,
                "serialization_python_retained_bytes": retained,
                "process_peak_rss_bytes": rss * (1 if sys.platform == "darwin" else 1024),
                "output_sha256": digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mib", type=int, default=64)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-variant", choices=("copy", "direct"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mib < 1:
        parser.error("--mib must be positive")
    if args.child_variant:
        print(json.dumps(sample(args.mib, args.child_variant)))
        return
    samples = []
    for variant in ("copy", "direct"):
        output = subprocess.check_output(
            [sys.executable, str(Path(__file__).resolve()), "--mib", str(args.mib),
             "--child-variant", variant], text=True)
        samples.append(json.loads(output))
    assert samples[0]["output_sha256"] == samples[1]["output_sha256"]
    report = {"schema": 1, "measurement": "synthetic geometry serialization; fresh process per variant; "
              "tracemalloc excludes preallocated geometry; not whole graph/build peak",
              "python": sys.version, "platform": sys.platform, "samples": samples}
    serialized = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
