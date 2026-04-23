"""Pick routing pairs with **cell-diverse** start/end distribution.

The default ``pick_valid_pairs`` in generate_golden_corpus picks nodes
uniformly by node ID, which in a real graph weights toward wherever
nodes are densest. On Japan that's Tokyo/Osaka/Nagoya — 1000 pairs only
touch ~30% of the 1°-cell grid. Whole regions (Hokkaido, rural Honshu,
Ryukyu islands) never get exercised.

This picker bins nodes by cell first, then samples pairs **uniformly
across the cell grid**. Every cell with nodes + outgoing edges gets a
guaranteed minimum number of start/end assignments. Cells nobody can
route through (isolated islands, deadend-only clusters) still get
attempted — unreachable-ratio is the natural signal that a cell is
truly disconnected.

Output matches the JSONL schema of generate_golden_corpus (meta header
+ per-pair fingerprints), so diff_corpora and test_spatial_chunking
consume it unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.szrg_reader import SZRG, load_from_zim
from tests.szrg_astar import find_route, haversine_m
from tests.szrg_spatial import cell_of


def _bucket_nodes_by_cell(
    g: SZRG,
    cell_scale: int,
) -> dict[tuple[int, int], list[int]]:
    """Group nodes with at least one outgoing edge by their cell. Tossing
    edge-less nodes up front avoids picking pairs where A* can't even
    start — they'd all be "unreachable" and eat up the quota."""
    nodes = g.nodes_scaled.tolist()
    adj = g.adj_offsets.tolist()
    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(g.num_nodes):
        if adj[i + 1] <= adj[i]:
            continue
        key = cell_of(nodes[i * 2], nodes[i * 2 + 1], cell_scale)
        buckets.setdefault(key, []).append(i)
    return buckets


def pick_cell_diverse_pairs(
    g: SZRG,
    n: int,
    *,
    cell_scale: int,
    seed: int = 42,
    min_dist_m: float = 500.0,
    max_dist_m: float | None = None,
    min_per_cell: int = 2,
) -> list[tuple[int, int]]:
    """Return ``n`` (start, end) pairs, guaranteeing every non-empty cell
    provides at least ``min_per_cell`` start-nodes and ``min_per_cell``
    end-nodes across the corpus. Distance range is enforced straight-line
    (haversine); pairs outside it are retried."""
    rng = random.Random(seed)
    buckets = _bucket_nodes_by_cell(g, cell_scale)
    cells = list(buckets.keys())
    if len(cells) < 2:
        raise ValueError("need ≥2 cells to pick cross-cell pairs")
    nodes_flat = g.nodes_scaled.tolist()

    def hav(a: int, b: int) -> float:
        return haversine_m(nodes_flat[a*2] / 1e7, nodes_flat[a*2+1] / 1e7,
                           nodes_flat[b*2] / 1e7, nodes_flat[b*2+1] / 1e7)

    pairs: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    # Phase 1: quota pass — every cell contributes exactly min_per_cell
    # starts + min_per_cell ends. This is the cell-coverage guarantee.
    # Destinations are a random OTHER cell, retried until the distance
    # filter is happy (up to a cap so we don't loop on weirdly-clustered
    # regions like isolated islands).
    quota = min_per_cell
    for c in cells:
        for _ in range(quota):
            for role in ("start", "end"):
                tries = 0
                while tries < 40:
                    tries += 1
                    if role == "start":
                        a = rng.choice(buckets[c])
                        b_cell = rng.choice(cells)
                        if b_cell == c and len(cells) > 1:
                            continue
                        b = rng.choice(buckets[b_cell])
                    else:
                        b = rng.choice(buckets[c])
                        a_cell = rng.choice(cells)
                        if a_cell == c and len(cells) > 1:
                            continue
                        a = rng.choice(buckets[a_cell])
                    if a == b or (a, b) in seen:
                        continue
                    d = hav(a, b)
                    if d < min_dist_m:
                        continue
                    if max_dist_m is not None and d > max_dist_m:
                        continue
                    seen.add((a, b))
                    pairs.append((a, b))
                    break

    # Phase 2: top-up to N with fully random cell pairs. Keeps total
    # count honest to the caller's request while still benefiting from
    # the quota guarantee above.
    attempts = 0
    cap = (n - len(pairs)) * 50 + 50
    while len(pairs) < n and attempts < cap:
        attempts += 1
        a_cell = rng.choice(cells)
        b_cell = rng.choice(cells)
        if a_cell == b_cell and len(cells) > 1:
            continue
        a = rng.choice(buckets[a_cell])
        b = rng.choice(buckets[b_cell])
        if a == b or (a, b) in seen:
            continue
        d = hav(a, b)
        if d < min_dist_m:
            continue
        if max_dist_m is not None and d > max_dist_m:
            continue
        seen.add((a, b))
        pairs.append((a, b))

    # Shuffle so the JSONL isn't a predictable cell-by-cell scan — that
    # would front-load all Hokkaido pairs and make progress reports bad.
    rng.shuffle(pairs)
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zim", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pairs", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cell-scale", type=int, default=1,
                    help="Cell grid used for diversity — 1 = 1° cells")
    ap.add_argument("--min-per-cell", type=int, default=2,
                    help="Each non-empty cell contributes this many "
                         "start-nodes AND this many end-nodes")
    ap.add_argument("--min-dist-m", type=float, default=500.0)
    ap.add_argument("--max-dist-m", type=float, default=None,
                    help="Straight-line distance cap; None = no cap "
                         "(lets long cross-country routes in)")
    ap.add_argument("--max-pops", type=int, default=2_000_000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--progress-every", type=int, default=50)
    args = ap.parse_args()

    print("[corpus] loading v4 graph...", flush=True)
    t0 = time.time()
    g = load_from_zim(args.zim)
    print(f"[corpus] loaded v{g.version}: {g.num_nodes:,} nodes, "
          f"{g.num_edges:,} edges in {time.time()-t0:.1f}s", flush=True)

    print(f"[corpus] picking cell-diverse pairs (scale={args.cell_scale}, "
          f"seed={args.seed}, min_per_cell={args.min_per_cell})...", flush=True)
    pairs = pick_cell_diverse_pairs(
        g, args.pairs,
        cell_scale=args.cell_scale,
        seed=args.seed,
        min_dist_m=args.min_dist_m,
        max_dist_m=args.max_dist_m,
        min_per_cell=args.min_per_cell,
    )
    print(f"[corpus] picked {len(pairs)} pairs", flush=True)

    # Report cell distribution as a sanity check on diversity.
    buckets = _bucket_nodes_by_cell(g, args.cell_scale)
    start_cells, end_cells = set(), set()
    nodes_flat = g.nodes_scaled.tolist()
    for (a, b) in pairs:
        start_cells.add(cell_of(nodes_flat[a*2], nodes_flat[a*2+1], args.cell_scale))
        end_cells.add(cell_of(nodes_flat[b*2], nodes_flat[b*2+1], args.cell_scale))
    print(f"[corpus] diversity: {len(start_cells)} unique start-cells, "
          f"{len(end_cells)} unique end-cells, of {len(buckets)} non-empty",
          flush=True)

    # Route + fingerprint — reuse generate_golden_corpus's path for
    # consistency. For the cross-country pairs we want the high max_pops
    # cap so Hokkaido↔Kyushu-scale routes don't get truncated.
    from tests.generate_golden_corpus import _run_parallel  # reuse worker pool

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.workers > 1:
        results = _run_parallel(
            pairs,
            zim_path=args.zim, graph_bin_path=None,
            workers=args.workers, max_pops=args.max_pops,
            progress_every=args.progress_every,
        )
    else:
        # Inline serial path — cheaper for tiny graphs.
        results = []
        t1 = time.time()
        for i, (a, b) in enumerate(pairs):
            r = find_route(g, a, b, max_pops=args.max_pops)
            if r is None:
                results.append({"s": a, "e": b, "unreachable": True})
            else:
                results.append(r.fingerprint())
            if args.progress_every and (i + 1) % args.progress_every == 0:
                print(f"[corpus]   {i+1}/{len(pairs)}  "
                      f"{(i+1)/(time.time()-t1):.1f} routes/s", flush=True)

    # Header record mirrors generate_golden_corpus for diff_corpora compat.
    header = {
        "_meta": True,
        "corpus_schema": 1,
        "source": args.zim,
        "seed": args.seed,
        "pairs": len(pairs),
        "min_dist_m": args.min_dist_m,
        "max_dist_m": args.max_dist_m,
        "max_pops": args.max_pops,
        "pair_picker": "cell_diverse",
        "cell_scale": args.cell_scale,
        "min_per_cell": args.min_per_cell,
        "unique_start_cells": len(start_cells),
        "unique_end_cells": len(end_cells),
        "non_empty_cells": len(buckets),
    }
    if args.zim:
        h = hashlib.sha256()
        with open(args.zim, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        header["zim_sha256"] = h.hexdigest()

    found = unreach = 0
    with out_path.open("w") as fh:
        fh.write(json.dumps(header, separators=(",", ":")) + "\n")
        for rec in results:
            if rec.get("unreachable"):
                unreach += 1
            else:
                found += 1
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    print(f"[corpus] done → {out_path}  ({found} routable, "
          f"{unreach} unreachable)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
