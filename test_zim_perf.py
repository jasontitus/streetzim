#!/usr/bin/env python3
"""
test_zim_perf.py - Reproduce and test the libzim spin-lock issue.

Creates ZIM files with many small compressible items to test queue throughput.
Uses ZSTD_CLEVEL=22 (matching production builds) and realistic tile sizes.

Usage:
    ZSTD_CLEVEL=22 python3 test_zim_perf.py
"""
import os
import sys
import time
import tempfile
import struct

# Add venv to path
venv_site = os.path.join(os.path.dirname(__file__), "venv", "lib")
for d in os.listdir(venv_site):
    if d.startswith("python"):
        sys.path.insert(0, os.path.join(venv_site, d, "site-packages"))
        break

from libzim.writer import Creator, Item, StringProvider, Hint


class TestItem(Item):
    def __init__(self, path, data, compress=True):
        super().__init__()
        self._path = path
        self._data = data
        self._compress = compress

    def get_path(self):
        return self._path

    def get_title(self):
        return ""

    def get_mimetype(self):
        return "application/x-protobuf"

    def get_contentprovider(self):
        return StringProvider(self._data)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: self._compress}


def make_compressible_tile(i, size=2000):
    """Generate compressible data similar to a PBF vector tile.

    Real PBF tiles have repeated field tags, varint-encoded coordinates,
    and string tables — all highly compressible patterns.
    """
    # Mix of repeated patterns (like PBF field tags) and varying data (coordinates)
    pattern = struct.pack("<I", i) * 10  # repeated 4-byte int
    coords = bytes(range(256)) * (size // 256)  # sequential bytes (compressible)
    filler = b"\x12\x08" * (size // 4)  # PBF-like field tags
    data = (pattern + coords + filler)[:size]
    return data


def run_test(num_tiles, cluster_size_kb, num_workers, throttle_ms=0, tile_size=2000):
    """Run a single ZIM creation test and return stats."""
    # Pre-generate tiles
    tiles = []
    for i in range(num_tiles):
        data = make_compressible_tile(i, tile_size)
        tiles.append((f"tiles/14/{i // 256}/{i % 256}.pbf", data))

    with tempfile.NamedTemporaryFile(suffix=".zim", delete=True) as f:
        output_path = f.name

    creator = Creator(output_path)
    creator.config_indexing(False, "")
    creator.config_clustersize(cluster_size_kb * 1024)
    creator.config_nbworkers(num_workers)
    creator.set_mainpath("index.html")

    stall_count = 0
    rates = []

    with creator:
        creator.add_item(TestItem("index.html", b"<html>test</html>", compress=True))

        start = time.time()
        batch_start = time.time()
        batch_size = 1000

        for i, (path, data) in enumerate(tiles):
            creator.add_item(TestItem(path, data, compress=True))

            # Optional throttle: yield to compression workers periodically
            if throttle_ms > 0 and (i + 1) % batch_size == 0:
                time.sleep(throttle_ms / 1000.0)

            if (i + 1) % batch_size == 0:
                batch_elapsed = time.time() - batch_start
                batch_rate = batch_size / batch_elapsed if batch_elapsed > 0 else 0
                rates.append(batch_rate)

                if batch_elapsed > 10:
                    stall_count += 1

                batch_start = time.time()

        insert_time = time.time() - start

    # Time includes finalization (the `with` block exit)
    total_time = time.time() - start

    # Clean up
    file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    for suffix in ["", "_fulltext.idx.tmp", "_title.idx.tmp"]:
        try:
            p = output_path + suffix
            if os.path.isdir(p):
                import shutil
                shutil.rmtree(p)
            elif os.path.isfile(p):
                os.unlink(p)
        except Exception:
            pass
    try:
        os.unlink(output_path)
    except Exception:
        pass

    avg_rate = num_tiles / insert_time if insert_time > 0 else 0
    min_rate = min(rates) if rates else 0

    return {
        "insert_time": insert_time,
        "total_time": total_time,
        "avg_rate": avg_rate,
        "min_rate": min_rate,
        "stalls": stall_count,
        "file_size_mb": file_size / (1024 * 1024),
    }


def main():
    zstd_level = os.environ.get("ZSTD_CLEVEL", "not set")
    print(f"libzim ZIM creation performance test (ZSTD_CLEVEL={zstd_level})")

    num_tiles = 500_000

    configs = [
        # (label, cluster_kb, workers, throttle_ms)
        ("2MB cluster, 20 workers, no throttle", 2048, 20, 0),
        ("2MB cluster,  4 workers, no throttle", 2048, 4, 0),
        ("2MB cluster,  4 workers, 50ms throttle", 2048, 4, 50),
        ("2MB cluster,  4 workers, 100ms throttle", 2048, 4, 100),
        ("4MB cluster,  4 workers, no throttle", 4096, 4, 0),
        ("4MB cluster,  4 workers, 50ms throttle", 4096, 4, 50),
    ]

    print(f"Tiles: {num_tiles:,} x ~2KB compressible data")
    print(f"{'Config':<50} {'Insert':>8} {'Total':>8} {'Rate':>10} {'MinRate':>10} {'Stalls':>7} {'Size':>8}")
    print("-" * 110)

    for label, cluster_kb, workers, throttle in configs:
        sys.stdout.write(f"  {label:<48} ")
        sys.stdout.flush()

        try:
            stats = run_test(num_tiles, cluster_kb, workers, throttle)
            print(
                f"{stats['insert_time']:>7.1f}s "
                f"{stats['total_time']:>7.1f}s "
                f"{stats['avg_rate']:>9.0f}/s "
                f"{stats['min_rate']:>9.0f}/s "
                f"{stats['stalls']:>6} "
                f"{stats['file_size_mb']:>7.1f}MB"
            )
        except Exception as e:
            print(f"  FAILED: {e}")

    print()
    print("Insert = time to add all items | Total = insert + finalization")
    print("Stalls = batches of 1000 tiles that took >10s (queue contention)")
    print("Throttle = sleep between batches to let compression workers drain")


if __name__ == "__main__":
    main()
