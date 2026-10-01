#!/usr/bin/env python3
"""Reproducible large-body serialization and optional packing benchmark.

Run with the same arguments before and after a change. Each repetition is a
fresh process; wait4 reports that process's peak RSS and CPU time on macOS and
Linux. RSS includes resident mapped file pages and excludes unmapped
filesystem cache.

    python tools/benchmark_zim_pack.py --json result.json --mib 128 \
        --pack-bin rust/streetzim-pack/target/release/streetzim-pack

--manifest-writer can select a saved baseline copy of manifest_writer.py.
The default compressible byte pattern isolates serialization and streaming
overhead; this is not a representative tile-compression throughput benchmark.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def measured(command: list[str], *, env: dict[str, str]) -> dict:
    started = time.monotonic()
    proc = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL)
    try:
        _, status, usage = os.wait4(proc.pid, 0)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    proc.returncode = os.waitstatus_to_exitcode(status)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, command)
    return {
        "wall_s": time.monotonic() - started,
        "cpu_s": usage.ru_utime + usage.ru_stime,
        "peak_rss_bytes": int(usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)),
    }


def serialize(module_path: Path, output: Path, size: int) -> None:
    spec = importlib.util.spec_from_file_location("bench_manifest_writer", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    creator = module.ManifestCreator(str(output), keep_stage=True)
    creator._run_packer = lambda: None
    data = (bytes(range(256)) * ((size + 255) // 256))[:size]
    with creator:
        creator.add_item(SimpleNamespace(
            _path="large.bin", _title="", _mimetype="application/octet-stream",
            _data=data, _file_path=None, _compress=False))


def positive(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--json", type=Path)
    parser.add_argument("--mib", type=positive, default=128)
    parser.add_argument("--repetitions", type=positive, default=3)
    parser.add_argument("--threads", type=positive, default=2)
    parser.add_argument("--stream-items", type=positive, default=1,
                        help="number of large file items per Rust pack")
    parser.add_argument("--in-flight-mib", type=positive,
                        help="raw cluster-pipeline byte budget (excludes streamed encoders)")
    parser.add_argument("--compression-level", type=int, default=3)
    parser.add_argument("--pack-bin", type=Path)
    parser.add_argument("--python-packer", action="store_true",
                        help="benchmark the Python replacement instead of a binary")
    parser.add_argument("--manifest-writer", type=Path,
                        default=ROOT / "cloud/manifest_writer.py")
    parser.add_argument("--_serialize", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.pack_bin and args.python_packer:
        parser.error("choose --pack-bin or --python-packer")
    size = args.mib * 1024 * 1024
    if args._serialize:
        serialize(args.manifest_writer, args._serialize, size)
        return 0
    if not hasattr(os, "wait4"):
        parser.error("per-process RSS measurement needs macOS or Linux wait4")
    if not args.json:
        parser.error("--json is required")

    env = dict(os.environ, STREETZIM_MANIFEST_ZSTD="0",
               RAYON_NUM_THREADS=str(args.threads))
    result = {"platform": sys.platform, "python": sys.version.split()[0],
              "payload_bytes": size, "threads": args.threads,
              "stream_items": args.stream_items,
              "compression_level": args.compression_level,
              "max_in_flight_bytes": args.in_flight_mib * 1024**2 if args.in_flight_mib else 0,
              "manifest_writer": str(args.manifest_writer.resolve()),
              "pack_binary": str(args.pack_bin.resolve()) if args.pack_bin else None,
              "packer": "python" if args.python_packer else "executable",
              "measurements": {}}
    with tempfile.TemporaryDirectory(prefix="streetzim-pack-bench-") as tmp:
        work = Path(tmp)
        for repetition in range(args.repetitions):
            target = work / f"serialize-{repetition}"
            target.mkdir()
            measurement = measured([
                sys.executable, str(Path(__file__).resolve()), "--_serialize",
                str(target / "out.zim"), "--mib", str(args.mib),
                "--manifest-writer", str(args.manifest_writer.resolve()),
            ], env=env)
            measurement["stage_bytes"] = sum(p.stat().st_size for p in target.rglob("*")
                                             if p.is_file())
            result["measurements"].setdefault("serialize", []).append(measurement)
        if args.pack_bin or args.python_packer:
            command = ([sys.executable, "-m", "streetzim.pack"] if args.python_packer
                       else [str(args.pack_bin.resolve())])
            source = work / "large.bin"
            block = bytes(range(256)) * 4096
            with source.open("wb") as body:
                remaining = size
                while remaining:
                    piece = block[:remaining]
                    body.write(piece)
                    remaining -= len(piece)
            for compress in (False, True):
                mode = "pack_zstd" if compress else "pack_raw"
                manifest = work / f"{mode}.jsonl"
                manifest.write_text(json.dumps({
                    "kind": "config", "compression": "zstd", "compression_level": args.compression_level,
                    "cluster_size_target": 2 * 1024 * 1024,
                    "max_in_flight_bytes": result["max_in_flight_bytes"],
                }) + "\n" + "".join(json.dumps({
                    "kind": "item", "path": f"large-{i}.bin", "mime": "application/octet-stream",
                    "file": str(source), "streaming": True, "size": size,
                    "compress": compress,
                }) + "\n" for i in range(args.stream_items)))
                for repetition in range(args.repetitions):
                    out = work / f"{mode}-{repetition}.zim"
                    measurement = measured(command + [str(manifest), str(out)], env=env)
                    measurement["output_bytes"] = out.stat().st_size
                    result["measurements"].setdefault(mode, []).append(measurement)
                    out.unlink()
    result["medians"] = {
        mode: {key: statistics.median(row[key] for row in rows) for key in rows[0]}
        for mode, rows in result["measurements"].items()
    }
    args.json.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["medians"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
