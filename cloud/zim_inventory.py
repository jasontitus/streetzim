#!/usr/bin/env python3
"""Where the bytes are in a streetzim ZIM, per component and per zoom.

One pass over dirents plus one decompression of every cluster. Reports, per
component (tiles/z, satellite/z, terrain/z, routing-data, search-data, ...):

  entries, uncompressed bytes, attributed compressed bytes, clusters touched

and, for the derive tool's benefit, how many clusters mix components (each
one is a cluster that a prefix-drop must re-encode instead of copy).

Compressed bytes are attributed to a component as the cluster's on-disk size
times the component's share of the cluster's uncompressed payload.

Usage:
    python3 cloud/zim_inventory.py FILE.zim [--json] [--by-zoom] [--clusters]
    python3 cloud/zim_inventory.py https://archive.org/download/streetzim-brazil/osm-brazil-2026-09-14.zim

A URL is inventoried from its tables alone (header, dirents, cluster pointer
list, raw-cluster offset tables) via HTTP range requests: a few hundred MB
for a 20 GB continent, no download. Compressed clusters that mix components
(a handful per file) are then split by blob count rather than bytes.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cloud.zimfmt import ZimReader  # noqa: E402

ZOOMED = ("tiles", "satellite", "terrain")


def component_of(path: str, namespace: str, by_zoom: bool) -> str:
    if namespace == "X":
        return "xapian" if "xapian" in path else "zim-listing"
    if namespace == "M":
        return "metadata"
    if namespace != "C":
        return f"ns-{namespace}"
    seg = path.split("/", 2)
    if seg[0] in ZOOMED:
        return f"{seg[0]}/{seg[1]}" if by_zoom and len(seg) > 1 else seg[0]
    if seg[0] in ("wiki-article", "wiki-image"):
        return "wiki"
    if len(seg) > 1:
        return seg[0]
    return "app"


def inventory(path: str, *, by_zoom: bool = False, tables_only: bool | None = None,
              verbose: bool = False):
    """``tables_only`` skips inflating clusters: compressed clusters are then
    attributed to components by blob COUNT share instead of byte share (raw
    clusters stay exact via their offset table). Default for URLs."""
    r = ZimReader(path, verbose=verbose)
    if tables_only is None:
        tables_only = r.remote
    # per cluster: component -> uncompressed bytes, and blob->component map
    cluster_comp_bytes: dict[int, Counter] = defaultdict(Counter)
    comp_entries = Counter()
    redirects = 0
    # First pass: dirents (cheap), record which blobs belong to which component
    blob_comp: dict[int, dict[int, str]] = defaultdict(dict)
    for _, d in r.dirents():
        if d.is_redirect:
            redirects += 1
            continue
        comp = component_of(d.path, d.namespace, by_zoom)
        comp_entries[comp] += 1
        blob_comp[d.cluster][d.blob] = comp
    # Second pass: clusters (one decompression each)
    comp_uncomp = Counter()
    comp_comp = Counter()      # attributed on-disk bytes (float)
    comp_clusters: dict[str, set] = defaultdict(set)
    mixed: list[tuple[int, dict]] = []
    unreferenced = 0
    raw_bytes = 0
    approx_clusters = 0
    if tables_only:
        r.preload_cluster_infos()
        r.preload_raw_tables([c for c in range(r.header.cluster_count) if not r.cluster_info(c).compressed])
    for c in range(r.header.cluster_count):
        ci = r.cluster_info(c)
        if tables_only:
            sizes = r.raw_cluster_blob_sizes(c)
            if sizes is None:
                # compressed: no payload read; weight blobs equally, and count
                # the cluster's on-disk size as its "uncompressed" stand-in
                comps_here = blob_comp.get(c, {})
                nb = max(len(comps_here), 1)
                sizes = [ci.size / nb] * nb
                if len({v for v in comps_here.values()}) > 1:
                    approx_clusters += 1
        else:
            sizes = r.blob_sizes(c)
        total = sum(sizes) or 1
        if not ci.compressed:
            raw_bytes += ci.size
        per = Counter()
        for b, s in enumerate(sizes):
            comp = blob_comp[c].get(b)
            if comp is None:
                unreferenced += s
                comp = "unreferenced"
            per[comp] += s
        for comp, s in per.items():
            comp_uncomp[comp] += s
            comp_comp[comp] += ci.size * (s / total)
            comp_clusters[comp].add(c)
        cluster_comp_bytes[c] = per
        real = {k: v for k, v in per.items() if k != "unreferenced"}
        if len(real) > 1:
            mixed.append((c, real))
    rows = []
    for comp in sorted(comp_uncomp, key=lambda k: -comp_comp[k]):
        rows.append({
            "component": comp,
            "entries": comp_entries.get(comp, 0),
            "uncompressed": comp_uncomp[comp],
            "on_disk": int(round(comp_comp[comp])),
            "clusters": len(comp_clusters[comp]),
        })
    return {
        "file": path,
        "size": r.size,
        "entries": r.header.entry_count,
        "redirects": redirects,
        "clusters": r.header.cluster_count,
        "raw_cluster_bytes": raw_bytes,
        "mixed_clusters": len(mixed),
        "mixed": [(c, dict(per)) for c, per in mixed],
        "rows": rows,
        "tables_only": tables_only,
        "approx_mixed_clusters": approx_clusters,
        "fetched_bytes": getattr(r.src, "bytes_fetched", None),
    }


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000 or unit == "GB":
            return f"{n:7.1f} {unit}" if unit != "B" else f"{int(n):7d} B "
        n /= 1000
    return str(n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("zim")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--by-zoom", action="store_true", help="split tiles/satellite/terrain per zoom")
    ap.add_argument("--clusters", action="store_true", help="list every mixed cluster")
    ap.add_argument("--tables-only", action="store_true",
                    help="do not inflate clusters (default for URLs); byte shares of compressed "
                         "mixed clusters become approximate")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    inv = inventory(a.zim, by_zoom=a.by_zoom, tables_only=a.tables_only or None, verbose=a.verbose)
    if a.json:
        print(json.dumps(inv, indent=1, default=list))
        return 0
    if inv["fetched_bytes"] is not None:
        print(f"(remote: fetched {_fmt(inv['fetched_bytes']).strip()} of tables)")
    if inv["tables_only"]:
        print(f"(tables only: {inv['approx_mixed_clusters']} compressed mixed clusters attributed by blob count)")
    print(f"{inv['file']}: {_fmt(inv['size'])}, {inv['entries']} entries "
          f"({inv['redirects']} redirects), {inv['clusters']} clusters, "
          f"{inv['mixed_clusters']} mixed, {_fmt(inv['raw_cluster_bytes'])} in raw clusters")
    unc = not inv["tables_only"]
    print(f"{'component':<22}{'entries':>9}" + (f"{'uncompressed':>14}" if unc else "")
          + f"{'on disk':>12}{'share':>7}{'clusters':>9}")
    for row in inv["rows"]:
        print(f"{row['component']:<22}{row['entries']:>9}" + (f"{_fmt(row['uncompressed']):>14}" if unc else "")
              + f"{_fmt(row['on_disk']):>12}{100*row['on_disk']/inv['size']:>6.1f}%{row['clusters']:>9}")
    if a.clusters:
        for c, per in inv["mixed"]:
            print(f"  mixed c{c}: " + ", ".join(f"{k}={_fmt(v).strip()}" for k, v in sorted(per.items(), key=lambda kv: -kv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
