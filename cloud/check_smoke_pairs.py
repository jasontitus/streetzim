#!/usr/bin/env python3
"""Validate (and optionally fix) the routing smoke pairs in cloud/regions.tsv.

The browser gate requires the routed line to start and end within 100 m
of the requested points, and the routing gate needs both engines to find
a route. A hand-picked coordinate on a plaza, inside a gated compound or
on a naval base fails both without anything being wrong with the ZIM —
DC's Capitol and Hawaii's Pearl Harbor did exactly that on the first
queue pilot.

For every region that has a local spatial ZIM (newest osm-<id>-*.zim),
snap both endpoints with the viewer's own rule, report the gap, and route
A* between the snapped vertices. With --fix, rewrite the registry's
coordinates to the snapped vertex whenever the gap exceeds --max-gap and
the route works, so the pair is a real, reachable road vertex.

Usage:
  venv-linux/bin/python3 cloud/check_smoke_pairs.py [--fix] [--max-gap 80] [--only a,b]
"""
import argparse, glob, math, os, re, sys, time

ROOT = "/storage/streetzim"
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "cloud"))
from tests.szrg_spatial import load_spatial_from_zim  # noqa: E402
from tests.szrg_spatial_astar import find_route_spatial  # noqa: E402
import route_cli                                      # noqa: E402


def newest_zim(rid):
    c = sorted(glob.glob(os.path.join(ROOT, f"osm-{rid}-20??-??-??.zim")))
    return c[-1] if c else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default=os.path.join(ROOT, "cloud", "regions.tsv"))
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--max-gap", type=float, default=80.0)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    only = set(x for x in a.only.split(",") if x)

    lines = open(a.registry, encoding="utf-8").read().split("\n")
    out, changed, bad = [], 0, []
    for line in lines:
        if not line or line.startswith("#"):
            out.append(line); continue
        f = line.split("\t")
        rid, src, dst = f[0], f[4], f[5]
        if only and rid not in only:
            out.append(line); continue
        zim = newest_zim(rid)
        if not zim:
            print(f"  {rid:28s} (no local ZIM — cannot check)", flush=True)
            out.append(line); continue
        try:
            g = load_spatial_from_zim(zim)
        except Exception as e:
            print(f"  {rid:28s} {os.path.basename(zim)}: not a spatial ZIM ({e})", flush=True)
            out.append(line); continue
        pts = []
        snap_err = None
        for label, s, mode in (("src", src, "origin"), ("dst", dst, "dest")):
            lat, lon = (float(v) for v in s.split(","))
            try:
                node, gap = route_cli.nearest_node(g, lat, lon, mode)
                nlat, nlon = (v / 1e7 for v in g.node_coords_e7(node))
            except Exception as e:      # older ZIMs can hold empty cells
                snap_err = f"{label} snap error: {type(e).__name__}: {e}"
                break
            pts.append((label, lat, lon, node, gap, nlat, nlon))
        if snap_err:
            print(f"  {rid:28s} {snap_err}  {os.path.basename(zim)}", flush=True)
            bad.append(rid); out.append(line); continue
        t0 = time.time()
        try:
            import inspect
            kw = {}
            params = inspect.signature(find_route_spatial).parameters
            for name in ("max_pops", "pop_limit", "max_expansions"):
                if name in params:
                    kw[name] = 3_000_000; break
            r = find_route_spatial(g, pts[0][3], pts[1][3], **kw)
            ok = bool(r and (r.get("time") if isinstance(r, dict) else r))
        except Exception as e:
            ok, r = False, None
        secs = time.time() - t0
        gaps = f"gap src {pts[0][4]:.0f} m, dst {pts[1][4]:.0f} m"
        status = "route OK" if ok else "NO ROUTE"
        need = [p for p in pts if p[4] > a.max_gap]
        print(f"  {rid:28s} {status:9s} {gaps:26s} {secs:4.1f}s  {os.path.basename(zim)}", flush=True)
        if not ok:
            bad.append(rid)
        if a.fix and ok and need:
            f[4] = f"{pts[0][5]:.5f},{pts[0][6]:.5f}"
            f[5] = f"{pts[1][5]:.5f},{pts[1][6]:.5f}"
            print(f"      -> snapped to vertices: {f[4]} / {f[5]}", flush=True)
            changed += 1
        out.append("\t".join(f))
    if a.fix and changed:
        open(a.registry, "w", encoding="utf-8").write("\n".join(out))
        print(f"\nrewrote {changed} pair(s) in {a.registry}")
    if bad:
        print(f"\nUNROUTABLE pairs (fix by hand): {', '.join(bad)}")
        sys.exit(2)


if __name__ == "__main__":
    main()
