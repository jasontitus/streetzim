#!/usr/bin/env python3
"""End-to-end check of the builder's zoom clustering, run when both binaries
exist (not a pytest test: needs a zimru checkout and cargo):

  python3 tests/e2e_cluster_break.py

Writes a manifest through ManifestCreator with three "zooms" of tiles and a
cluster_break at every boundary plus a smaller target inside, packs it with
streetzim-pack (built with --features cluster_break), and asserts with
cloud/zimfmt that no cluster mixes zooms, that the target changed, and that
the default target is back for what follows. Also runs zimru's zimcheck.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREETZIM_MANIFEST_ZSTD", "0")
from cloud.manifest_writer import ManifestCreator  # noqa: E402
from cloud.zimfmt import ZimReader  # noqa: E402


class _Item:
    def __init__(self, path, data, mime="application/x-protobuf", compress=True, front=False):
        self._path, self._data, self._mimetype, self._compress = path, data, mime, compress
        self._title = path
        self._is_front = front


def _png_48() -> bytes:
    import struct
    import zlib
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * 48 for _ in range(48))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 48, 48, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main() -> int:
    out = Path(tempfile.mkdtemp()) / "cb.zim"
    def tile_bytes():
        return os.urandom(3000) + b"x" * 5000        # ~8 KB, half incompressible, unique
    with ManifestCreator(str(out), compression_level=3, verbose=False) as c:
        c.config_clustersize(64 * 1024)                # build default: 64 KiB
        c.set_mainpath("index.html")
        for k, v in (("Title", "cb"), ("Description", "cluster break e2e"), ("Language", "eng"),
                     ("Creator", "streetzim"), ("Publisher", "streetzim"), ("Date", "2026-09-21"),
                     ("Name", "cb_e2e")):
            c.add_metadata(k, v)
        c.add_illustration(48, _png_48())
        c.add_item(_Item("index.html", b"<html>hi</html>", "text/html", front=True))
        c.cluster_break(16 * 1024)                     # tiles: 16 KiB target
        for z in (12, 13, 14):
            if z > 12:
                c.cluster_break()
            for i in range(12):
                c.add_item(_Item(f"tiles/{z}/{i}/0.pbf", tile_bytes()))
        c.cluster_break(64 * 1024)                     # back to default
        for i in range(24):
            c.add_item(_Item(f"search-data/s{i:02d}.json", tile_bytes(), "application/json"))
    r = ZimReader(str(out))
    per_cluster = defaultdict(set)
    sizes = {}
    for _, d in r.dirents():
        if d.namespace == "C" and not d.is_redirect:
            comp = "/".join(d.path.split("/")[:2]) if d.path.startswith("tiles/") else d.path.split("/")[0]
            per_cluster[d.cluster].add(comp)
    for c in per_cluster:
        sizes[c] = sum(r.blob_sizes(c))
    mixed = {c: s for c, s in per_cluster.items() if len(s) > 1}
    tile_clusters = [c for c, s in per_cluster.items() if any(x.startswith("tiles/") for x in s)]
    search_clusters = [c for c, s in per_cluster.items() if "search-data" in s]
    print(f"clusters: {r.header.cluster_count}; mixed: {mixed}")
    print("tile clusters (uncompressed bytes):", sorted((c, sizes[c]) for c in tile_clusters))
    print("search clusters (uncompressed bytes):", sorted((c, sizes[c]) for c in search_clusters))
    ok = True
    if mixed:
        print("FAIL: zooms share a cluster"); ok = False
    # 12 tiles * 8 KB = 96 KB per zoom at a 16 KiB target -> several clusters per zoom
    if not all(sizes[c] <= 3 * 16 * 1024 for c in tile_clusters):
        print("FAIL: tile clusters not honouring the 16 KiB target"); ok = False
    if len(tile_clusters) < 9:
        print(f"FAIL: expected >= 9 tile clusters (3 zooms x >= 3), got {len(tile_clusters)}"); ok = False
    # search: 24 * 8 KB = 192 KB at 64 KiB -> ~3-4 clusters, each larger than a tile cluster
    if not (2 <= len(search_clusters) <= 5):
        print(f"FAIL: expected 2-5 search clusters at the restored 64 KiB target, got {len(search_clusters)}"); ok = False
    zimcheck = os.environ.get("ZIMCHECK_BIN", "/home/user/zimru/target/release/zimcheck")
    if os.path.exists(zimcheck):
        res = subprocess.run([zimcheck, "-A", str(out)], capture_output=True, text=True)
        print("zimcheck:", (res.stdout.strip().splitlines() or [""])[-1]); ok &= res.returncode == 0
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
