#!/usr/bin/env python3
"""Measure the search finisher's retained parent heap on synthetic places.

Workers execute inline and tracemalloc is enabled, to isolate the parent
place-grid lifetime. These figures are not end-to-end build measurements or
multiprocess peak RSS. Run revisions in separate processes for comparable RSS:

    python tools/benchmark_search_memory.py --baseline-ref REF --output before.json
    python tools/benchmark_search_memory.py --output after.json

The output checksum verifies feature parity. Existing build measurement tools
should be used for complete builds and subprocess memory.
"""
from __future__ import annotations

import argparse
import builtins
import concurrent.futures
import functools
import gc
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]


class _InlinePool:
    def __init__(self, initializer, initargs, **kwargs):
        self._initargs = initargs  # ProcessPoolExecutor keeps these after shutdown
        initializer(*initargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, fn, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def _rss_bytes():
    try:
        rss_kib = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], text=True)
        return int(rss_kib.strip()) * 1024
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def benchmark(rows: int, baseline_ref: str | None = None) -> dict:
    if rows < 1:
        raise ValueError("rows must be positive")
    source_path = ROOT / "streetzim/search_extract.py"
    source = (subprocess.check_output(
        ["git", "show", f"{baseline_ref}:streetzim/search_extract.py"], cwd=ROOT)
        if baseline_ref else source_path.read_bytes())
    with tempfile.TemporaryDirectory(prefix="streetzim-search-memory-") as scratch:
        directory = Path(scratch)
        module_path = directory / "search_extract_benchmark.py"
        module_path.write_bytes(source)
        spec = importlib.util.spec_from_file_location("search_extract_benchmark", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.print = functools.partial(builtins.print, file=sys.stderr, flush=True)
        raw_path = directory / "raw.jsonl"
        with raw_path.open("w", encoding="utf-8") as output:
            for i in range(rows):
                record = {"name": f"Place {i:08d}", "type": "place", "subtype": "city",
                          "lat": -80.0 + ((i * 17) % 16000) / 100,
                          "lon": -170.0 + ((i * 29) % 34000) / 100}
                output.write(json.dumps(record, separators=(",", ":")) + "\n")
        del record
        gc.collect()
        rss_before = _rss_bytes()
        real_pool = concurrent.futures.ProcessPoolExecutor
        concurrent.futures.ProcessPoolExecutor = _InlinePool
        tracemalloc.start()
        try:
            start = time.perf_counter()
            output_path = module._finish_features_streaming(str(raw_path), scratch, rows)
            elapsed = time.perf_counter() - start
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            rss_after = _rss_bytes()
        finally:
            concurrent.futures.ProcessPoolExecutor = real_pool
            tracemalloc.stop()
        checksum = hashlib.sha256()
        actual_rows = 0
        with open(output_path, "rb") as output:
            for line in output:
                actual_rows += 1
                checksum.update(line)
        return {
            "schema": 1,
            "measurement": "synthetic parent retained heap; inline workers; tracemalloc enabled",
            "source_revision": baseline_ref or "working tree",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "rows_requested": rows,
            "rows_written": actual_rows,
            "output_sha256": checksum.hexdigest(),
            "duration_seconds": elapsed,
            "python_retained_bytes": current,
            "python_peak_bytes": peak,
            "parent_rss_before_bytes": rss_before,
            "parent_rss_after_bytes": rss_after,
            "grid_still_live": module._place_grid is not None,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--baseline-ref", help="read search_extract.py from this Git revision")
    parser.add_argument("--output", type=Path, help="also save the JSON result here")
    args = parser.parse_args()
    if args.rows < 1:
        parser.error("--rows must be positive")
    result = benchmark(args.rows, args.baseline_ref)
    serialized = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
