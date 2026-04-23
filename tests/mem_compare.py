"""Peak-memory measurement for Japan routing: monolithic v4 vs spatial.

Spawns each variant in a fresh subprocess so we can read its final peak
RSS from ``resource.getrusage(RUSAGE_SELF)`` without contamination from
the parent's heap (which still holds anything imported). Prints:

  * resident size after graph load
  * resident size after 3 routes
  * max resident size observed (ru_maxrss)
  * cell-loader counters (spatial only — how many cells actually got
    materialised for the test routes)

Usage:
  python tests/mem_compare.py <zim_path> [--pairs N] [--cell-scale N]
"""

from __future__ import annotations

import argparse
import gc
import os
import random
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rss_mb() -> float:
    """Resident set size in MB. On macOS ru_maxrss is bytes; on Linux it's KB."""
    r = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        return r.ru_maxrss / 1_000_000
    return r.ru_maxrss / 1024


def _now_rss_mb() -> float:
    """Current RSS (not high-water) in MB via /proc or ps.

    macOS doesn't expose current RSS through resource.getrusage (only the
    maxrss high-water mark), so shell out to ps."""
    pid = os.getpid()
    if sys.platform == "darwin":
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return int(out) / 1024  # ps reports in KB
    # Linux: /proc/self/statm page_size * rss_pages
    with open(f"/proc/{pid}/statm") as fh:
        _, rss_pages, *_ = fh.read().split()
    return int(rss_pages) * os.sysconf("SC_PAGESIZE") / 1_000_000


def run_monolithic(zim_path: str, pairs_json: str) -> None:
    """Load monolithic v4, convert hot columns (as find_route does), run a
    few routes, report RSS at each stage."""
    import json
    gc.collect()
    print(f"[mono] start — RSS {_now_rss_mb():.1f} MB")
    from libzim.reader import Archive
    from tests.szrg_reader import parse_szrg_bytes
    from tests.szrg_astar import find_route

    t0 = time.time()
    arc = Archive(zim_path)
    buf = bytes(arc.get_entry_by_path("routing-data/graph.bin").get_item().content)
    print(f"[mono] zim read — {time.time()-t0:.1f}s — RSS {_now_rss_mb():.1f} MB")

    t1 = time.time()
    g = parse_szrg_bytes(buf)
    # Drop the source buffer once parsed so it doesn't inflate peak.
    del buf
    gc.collect()
    print(f"[mono] SZRG parse done — {time.time()-t1:.1f}s — "
          f"RSS {_now_rss_mb():.1f} MB "
          f"(nodes={g.num_nodes:,} edges={g.num_edges:,})")

    pairs = json.loads(pairs_json)
    for (s, e) in pairs:
        t2 = time.time()
        r = find_route(g, s, e, max_pops=2_000_000)
        tag = "ok" if r else "unreach"
        print(f"[mono]   {s} -> {e}: {tag}, {time.time()-t2:.2f}s — "
              f"RSS {_now_rss_mb():.1f} MB")
    gc.collect()
    print(f"[mono] FINAL peak RSS {_rss_mb():.1f} MB")


def run_spatial_zim(zim_path: str, pairs_json: str,
                    cache_limit: int | None) -> None:
    """Load a pre-built spatial ZIM via ``load_spatial_from_zim``. This
    is the realistic deployment scenario — no in-memory conversion
    overhead, no v4 ghost in the process."""
    import json
    gc.collect()
    print(f"[spatz] start — RSS {_now_rss_mb():.1f} MB")
    from tests.szrg_spatial import load_spatial_from_zim
    from tests.szrg_spatial_astar import find_route_spatial

    t0 = time.time()
    sg = load_spatial_from_zim(zim_path, cache_limit=cache_limit)
    print(f"[spatz] SZCI index loaded — {time.time()-t0:.1f}s — "
          f"RSS {_now_rss_mb():.1f} MB "
          f"(nodes={sg.num_nodes:,} edges={sg.num_edges:,} "
          f"cells={sg._index.num_cells})")

    pairs = json.loads(pairs_json)
    for (s, e) in pairs:
        t2 = time.time()
        r = find_route_spatial(sg, s, e, max_pops=2_000_000)
        tag = "ok" if r else "unreach"
        print(f"[spatz]   {s} -> {e}: {tag}, {time.time()-t2:.2f}s — "
              f"RSS {_now_rss_mb():.1f} MB (cells_loaded={sg.cells_loaded})")
    gc.collect()
    print(f"[spatz] FINAL peak RSS {_rss_mb():.1f} MB (cells_loaded={sg.cells_loaded})")


def run_spatial(zim_path: str, pairs_json: str, cell_scale: int,
                cache_limit: int | None) -> None:
    """Load spatial: build cells in-memory from v4 (simulates as if the
    ZIM already shipped SZCI+SZRC), then route via lazy loader."""
    import json
    gc.collect()
    print(f"[spat] start — RSS {_now_rss_mb():.1f} MB")
    from libzim.reader import Archive
    from tests.szrg_reader import parse_szrg_bytes
    from tests.szrg_spatial import (
        build_spatial, parse_szci, SpatialGraph,
    )
    from tests.szrg_spatial_astar import find_route_spatial

    t0 = time.time()
    arc = Archive(zim_path)
    buf = bytes(arc.get_entry_by_path("routing-data/graph.bin").get_item().content)
    g4 = parse_szrg_bytes(buf)
    del buf
    gc.collect()
    print(f"[spat] v4 parse done — {time.time()-t0:.1f}s — RSS {_now_rss_mb():.1f} MB")

    t1 = time.time()
    idx_bytes, cells, meta = build_spatial(g4, cell_scale=cell_scale)
    # Simulate on-disk cells — drop the v4 graph + the dict values as soon
    # as we've serialized them. Mirrors the real flow where cells live on
    # disk and are fetched byte-range on demand.
    del g4
    gc.collect()
    print(f"[spat] spatial build — {time.time()-t1:.1f}s — RSS {_now_rss_mb():.1f} MB "
          f"(cells={meta['num_cells']}, idx={len(idx_bytes)/1e6:.1f} MB)")

    # Move cells to a "disk" — a temp directory — and drop the in-memory
    # dict so the loader has to reach back to the filesystem. Also avoids
    # every cell staying resident in the parent's heap.
    import tempfile
    tmp = tempfile.mkdtemp(prefix="spatial_cells_")
    for cid, b in cells.items():
        with open(os.path.join(tmp, f"cell-{cid:05d}.bin"), "wb") as fh:
            fh.write(b)
    cells.clear()
    del cells
    gc.collect()

    def loader(cid: int) -> bytes:
        with open(os.path.join(tmp, f"cell-{cid:05d}.bin"), "rb") as fh:
            return fh.read()

    idx = parse_szci(idx_bytes)
    del idx_bytes
    gc.collect()
    sg = SpatialGraph(idx, loader, cache_limit=cache_limit)
    print(f"[spat] ready (idx in RAM, cells on disk) — RSS {_now_rss_mb():.1f} MB")

    pairs = json.loads(pairs_json)
    for (s, e) in pairs:
        t2 = time.time()
        r = find_route_spatial(sg, s, e, max_pops=2_000_000)
        tag = "ok" if r else "unreach"
        print(f"[spat]   {s} -> {e}: {tag}, {time.time()-t2:.2f}s — "
              f"RSS {_now_rss_mb():.1f} MB (cells_loaded={sg.cells_loaded})")
    gc.collect()
    print(f"[spat] FINAL peak RSS {_rss_mb():.1f} MB (cells_loaded={sg.cells_loaded})")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def pick_pairs(zim_path: str, n: int, seed: int = 42) -> list[tuple[int, int]]:
    """Pick n (start,end) node pairs with meaningful distance. Runs in the
    parent process so subprocesses don't each reparse the graph."""
    import numpy as np
    from libzim.reader import Archive
    from tests.szrg_reader import parse_szrg_bytes
    from tests.szrg_astar import haversine_m

    arc = Archive(zim_path)
    buf = bytes(arc.get_entry_by_path("routing-data/graph.bin").get_item().content)
    g = parse_szrg_bytes(buf)
    ok = np.flatnonzero(np.diff(g.adj_offsets) > 0).tolist()
    nodes = g.nodes_scaled.tolist()

    def hav(a, b):
        return haversine_m(nodes[a*2]/1e7, nodes[a*2+1]/1e7,
                           nodes[b*2]/1e7, nodes[b*2+1]/1e7)

    rng = random.Random(seed)
    pairs: list[tuple[int, int]] = []
    # Mix short / medium / long so we exercise routing of different spans.
    # 20k–80 km range keeps the monolithic A* finishing in reasonable time.
    while len(pairs) < n:
        a = rng.choice(ok); b = rng.choice(ok)
        if a == b: continue
        d = hav(a, b)
        if 20_000 < d < 80_000:
            pairs.append((a, b))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zim", help="Path to SZRG v4 ZIM")
    ap.add_argument("--pairs", type=int, default=3, help="Number of routes per variant")
    ap.add_argument("--cell-scale", type=int, default=1,
                    help="SZCI cell scale (1=1°, 10=0.1°). Default 1 ≈ Japan-sized regions")
    ap.add_argument("--cache-limit", type=int, default=8,
                    help="SpatialGraph LRU cell cache limit (0 = unbounded)")
    ap.add_argument("--variant", choices=("mono", "spat", "spatz", "all"), default="all",
                    help="mono = v4 monolithic; spat = build+route in-memory; "
                         "spatz = load a pre-built spatial ZIM; all = mono+spatz "
                         "(the pair that matters for real deployment memory)")
    ap.add_argument("--spatial-zim", default=None,
                    help="Path to a pre-built spatial ZIM (for --variant spatz/all). "
                         "If omitted and --variant requires it, we error.")
    # Internal — used when re-invoking self as a subprocess for isolation.
    ap.add_argument("--_child", choices=("mono", "spat", "spatz"), default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--_child-zim", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--_pairs-json", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args._child:
        # Running as a child — one variant, then exit with peak RSS printed.
        cache_limit = args.cache_limit if args.cache_limit > 0 else None
        zim_for_child = args._child_zim or args.zim
        if args._child == "mono":
            run_monolithic(zim_for_child, args._pairs_json)
        elif args._child == "spatz":
            run_spatial_zim(zim_for_child, args._pairs_json, cache_limit)
        else:
            run_spatial(zim_for_child, args._pairs_json, args.cell_scale, cache_limit)
        return 0

    # Parent — pick pairs once, then spawn the variants in subprocesses.
    pairs = pick_pairs(args.zim, args.pairs)
    import json
    pairs_json = json.dumps(pairs)
    print(f"[parent] picked {len(pairs)} pairs: {pairs}")
    print(f"[parent] v4 zim: {args.zim} ({os.path.getsize(args.zim)/1e9:.2f} GB)")
    if args.spatial_zim:
        print(f"[parent] spatial zim: {args.spatial_zim} "
              f"({os.path.getsize(args.spatial_zim)/1e9:.2f} GB)")

    if args.variant == "all":
        variants = ("mono", "spatz")
    else:
        variants = (args.variant,)
    for variant in variants:
        print(f"\n{'='*70}\n>> variant: {variant}\n{'='*70}")
        zim_arg = args.zim if variant != "spatz" else (args.spatial_zim or args.zim)
        if variant == "spatz" and not args.spatial_zim:
            print(f"[parent] SKIP spatz: --spatial-zim not provided")
            continue
        cmd = [
            sys.executable, __file__,
            args.zim,
            "--cell-scale", str(args.cell_scale),
            "--cache-limit", str(args.cache_limit),
            "--_child", variant,
            "--_pairs-json", pairs_json,
            "--_child-zim", zim_arg,
        ]
        subprocess.run(cmd, check=False,
                       cwd=str(ROOT),
                       env={**os.environ, "PYTHONPATH": str(ROOT)})

    return 0


if __name__ == "__main__":
    sys.exit(main())
