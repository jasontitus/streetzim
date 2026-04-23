"""Generate golden route corpora from current-format SZRG graphs.

Usage:
  python -m tests.generate_golden_corpus \\
      --zim osm-silicon-valley-2026-04-22.zim \\
      --out tests/golden/silicon-valley.jsonl \\
      --pairs 5000 --workers 6

Picks random valid (start_node, end_node) pairs from the ZIM's routing
graph, runs A* on each, and writes a JSONL file where each line is the
fingerprint of one route. When v5 lands, re-run against the v5 ZIM and
diff — any fingerprint mismatch is a serialization regression.

Deterministic: uses a seeded RNG (default seed = 42). Re-runs with the
same seed + workers pick the same pairs in the same order, and workers
consume disjoint slices of that ordered list before re-merging, so the
final JSONL line order is identical regardless of worker count.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import random
import sys
import time
from pathlib import Path

# Allow `python -m tests.generate_golden_corpus` or direct ``python tests/...``
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.szrg_reader import SZRG, load_from_file, load_from_zim
from tests.szrg_astar import find_route, haversine_m


def pick_valid_pairs(g: SZRG, n: int, *,
                     min_dist_m: float = 500.0,
                     max_dist_m: float | None = None,
                     seed: int = 42,
                     max_attempts_mult: int = 50) -> list[tuple[int, int]]:
    """Pick `n` random (start, end) node pairs that both have out-edges and
    a straight-line distance in [min_dist_m, max_dist_m]."""
    rng = random.Random(seed)
    num_nodes = g.num_nodes
    adj = g.adj_offsets.tolist()
    # Nodes that have at least one outgoing edge.
    ok_nodes = [i for i in range(num_nodes) if adj[i + 1] > adj[i]]
    if len(ok_nodes) < 2:
        return []

    nodes_flat = g.nodes_scaled.tolist()

    def hav(a: int, b: int) -> float:
        return haversine_m(nodes_flat[a * 2] / 1e7, nodes_flat[a * 2 + 1] / 1e7,
                           nodes_flat[b * 2] / 1e7, nodes_flat[b * 2 + 1] / 1e7)

    pairs: list[tuple[int, int]] = []
    attempts = 0
    cap = n * max_attempts_mult
    seen: set[tuple[int, int]] = set()
    while len(pairs) < n and attempts < cap:
        attempts += 1
        a = rng.choice(ok_nodes)
        b = rng.choice(ok_nodes)
        if a == b or (a, b) in seen:
            continue
        d = hav(a, b)
        if d < min_dist_m:
            continue
        if max_dist_m is not None and d > max_dist_m:
            continue
        seen.add((a, b))
        pairs.append((a, b))
    return pairs


# --- Worker plumbing for multiprocessing ------------------------------------
# A shared SZRG graph per-worker, initialised once per process.
_WORKER_G: SZRG | None = None


def _worker_init(zim_path: str | None, graph_bin_path: str | None) -> None:
    global _WORKER_G
    if graph_bin_path:
        _WORKER_G = load_from_file(graph_bin_path)
    else:
        _WORKER_G = load_from_zim(zim_path)


def _worker_route(args: tuple[int, int, int | None]) -> tuple[int, dict]:
    """Returns (index, fingerprint-dict) so the caller can restore order."""
    idx, (a, b), max_pops = args[0], (args[1], args[2]), args[3]
    r = find_route(_WORKER_G, a, b, max_pops=max_pops)
    if r is None:
        return idx, {"s": a, "e": b, "unreachable": True}
    return idx, r.fingerprint()


def _run_parallel(pairs: list[tuple[int, int]], *,
                  zim_path: str | None, graph_bin_path: str | None,
                  workers: int, max_pops: int | None,
                  progress_every: int) -> list[dict]:
    tasks = [(i, a, b, max_pops) for i, (a, b) in enumerate(pairs)]
    results: list[dict] = [None] * len(pairs)  # type: ignore[assignment]
    t1 = time.time()
    done = 0
    with mp.get_context("fork").Pool(
        processes=workers,
        initializer=_worker_init,
        initargs=(zim_path, graph_bin_path),
    ) as pool:
        # imap_unordered = as-available; we still place into the result list by index.
        for idx, rec in pool.imap_unordered(_worker_route, tasks, chunksize=4):
            results[idx] = rec
            done += 1
            if progress_every and done % progress_every == 0:
                rate = done / (time.time() - t1)
                reach = sum(1 for r in results if r and not r.get("unreachable"))
                unreach = sum(1 for r in results if r and r.get("unreachable"))
                print(f"[corpus]   {done}/{len(pairs)}  "
                      f"{rate:.1f} routes/s  "
                      f"found={reach} unreach={unreach}", flush=True)
    return results  # type: ignore[return-value]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zim", help="Source ZIM (will read routing-data/graph.bin from it)")
    ap.add_argument("--graph-bin", help="Raw SZRG file (skip ZIM extraction)")
    ap.add_argument("--out", required=True, help="Output JSONL path")
    ap.add_argument("--pairs", type=int, default=2000, help="How many routes to record")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed for pair selection")
    ap.add_argument("--min-dist-m", type=float, default=500.0,
                    help="Skip pairs closer than this (straight-line)")
    ap.add_argument("--max-dist-m", type=float, default=None,
                    help="Skip pairs farther than this (straight-line)")
    ap.add_argument("--max-pops", type=int, default=None,
                    help="Cap A* node-pops per route (for continent-scale runs)")
    ap.add_argument("--workers", type=int, default=1, help="Parallel A* workers")
    ap.add_argument("--progress-every", type=int, default=200)
    args = ap.parse_args()

    if not args.zim and not args.graph_bin:
        ap.error("need --zim or --graph-bin")

    print("[corpus] loading graph...", flush=True)
    t0 = time.time()
    if args.graph_bin:
        g = load_from_file(args.graph_bin)
    else:
        g = load_from_zim(args.zim)
    print(f"[corpus] loaded v{g.version}: {g.num_nodes} nodes, "
          f"{g.num_edges} edges, {g.num_geoms} geoms "
          f"(in {time.time()-t0:.1f}s)", flush=True)

    print(f"[corpus] picking {args.pairs} pairs (seed={args.seed}, "
          f"min_dist={args.min_dist_m:.0f}m"
          + (f", max_dist={args.max_dist_m:.0f}m" if args.max_dist_m else "")
          + ")...", flush=True)
    pairs = pick_valid_pairs(g, args.pairs,
                             min_dist_m=args.min_dist_m,
                             max_dist_m=args.max_dist_m,
                             seed=args.seed)
    print(f"[corpus] picked {len(pairs)}", flush=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Free the parent's graph before forking workers so we don't
    # duplicate it into every child (copy-on-write makes this mostly
    # harmless but noisy in Activity Monitor).
    del g

    t1 = time.time()
    if args.workers <= 1:
        # Serial path — reuse the existing graph, don't re-parse in a worker.
        if args.graph_bin:
            g = load_from_file(args.graph_bin)
        else:
            g = load_from_zim(args.zim)
        results: list[dict] = []
        for i, (a, b) in enumerate(pairs):
            r = find_route(g, a, b, max_pops=args.max_pops)
            if r is None:
                results.append({"s": a, "e": b, "unreachable": True})
            else:
                results.append(r.fingerprint())
            if args.progress_every and (i + 1) % args.progress_every == 0:
                rate = (i + 1) / (time.time() - t1)
                reach = sum(1 for x in results if not x.get("unreachable"))
                unreach = (i + 1) - reach
                print(f"[corpus]   {i+1}/{len(pairs)}  "
                      f"{rate:.1f} routes/s  "
                      f"found={reach} unreach={unreach}", flush=True)
    else:
        results = _run_parallel(pairs,
                                zim_path=args.zim, graph_bin_path=args.graph_bin,
                                workers=args.workers, max_pops=args.max_pops,
                                progress_every=args.progress_every)

    # Write JSONL in pair order (deterministic).
    # Line 0 = a header record with metadata for this corpus; diff_corpora
    # skips metadata-only records so old corpora without a header still work.
    header_rec = {
        "_meta": True,
        "corpus_schema": 1,
        "source": args.zim or args.graph_bin,
        "seed": args.seed,
        "pairs": len(pairs),
        "min_dist_m": args.min_dist_m,
        "max_dist_m": args.max_dist_m,
        "max_pops": args.max_pops,
    }
    if args.zim:
        h = hashlib.sha256()
        with open(args.zim, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        header_rec["zim_sha256"] = h.hexdigest()
    found = unreach = 0
    with out_path.open("w") as fh:
        fh.write(json.dumps(header_rec, separators=(",", ":")) + "\n")
        for rec in results:
            if rec.get("unreachable"):
                unreach += 1
            else:
                found += 1
            fh.write(json.dumps(rec, separators=(",", ":")))
            fh.write("\n")
    print(f"[corpus] done → {out_path}  ({found} found, "
          f"{unreach} unreachable, {time.time()-t1:.1f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
