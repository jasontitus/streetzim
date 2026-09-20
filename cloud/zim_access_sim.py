#!/usr/bin/env python3
"""Count the reads a map interaction costs against a ZIM's cluster layout.

A viewer fetching a tile makes libzim (or the PWA's zim-reader.js) locate the
dirent, then read and inflate the *whole cluster* holding the blob. So what a
view costs is not "how many tiles" but "how many distinct clusters, and how
many compressed bytes those clusters are". This tool computes that for
scripted interactions so two layouts of the same content can be compared,
e.g. a source ZIM against a --regroup-tiles derivation.

    python3 cloud/zim_access_sim.py A.zim [B.zim ...] --scenario zoom-in --lat 47.3769 --lon 8.5417
    python3 cloud/zim_access_sim.py A.zim B.zim --scenario pan --zoom 14 --screens 6
    python3 cloud/zim_access_sim.py A.zim B.zim --all --lat 47.3769 --lon 8.5417

Model: MapLibre with 512px vector tiles at integer zoom floor(z) covering the
viewport plus a one-tile margin; 256px raster (satellite/terrain) sources are
fetched one zoom deeper. A libzim-style LRU of --cache clusters (default 16,
libzim's default) persists across the steps of a scenario.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cloud.zimfmt import ZimReader  # noqa: E402

SCREENS = {"phone": (390, 844), "tablet": (820, 1180), "desktop": (1440, 900)}


def lonlat_to_pixel(lon: float, lat: float, z: float, tile_px: int = 512) -> tuple[float, float]:
    n = tile_px * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tiles_for_view(lon: float, lat: float, zoom: float, w: int, h: int,
                   tile_px: int = 512, margin: int = 1) -> list[tuple[int, int, int]]:
    z = int(math.floor(zoom))
    scale = 2 ** (zoom - z)                # fractional zoom shrinks tile footprint
    cx, cy = lonlat_to_pixel(lon, lat, z, tile_px)
    half_w = w / 2 / scale
    half_h = h / 2 / scale
    x0 = int(math.floor((cx - half_w) / tile_px)) - margin
    x1 = int(math.floor((cx + half_w) / tile_px)) + margin
    y0 = int(math.floor((cy - half_h) / tile_px)) - margin
    y1 = int(math.floor((cy + half_h) / tile_px)) + margin
    n = 2 ** z
    out = []
    for x in range(x0, x1 + 1):
        for y in range(max(0, y0), min(n - 1, y1) + 1):
            out.append((z, x % n, y))
    return out


class Layout:
    """path -> (cluster, blob) plus cluster sizes, for one ZIM."""

    def __init__(self, path: str):
        self.path = path
        r = ZimReader(path)
        self.r = r
        self.index: dict[str, tuple[int, int]] = {}
        for _, d in r.dirents():
            if d.namespace == "C" and not d.is_redirect:
                self.index[d.path] = (d.cluster, d.blob)
        self.csize = [r.cluster_info(c).size for c in range(r.header.cluster_count)]
        self.ccomp = [r.cluster_info(c).compressed for c in range(r.header.cluster_count)]
        self.cfg = __import__("json").loads(r.get("map-config.json") or b"{}")
        # file extension per raster component, from the file itself (terrain is
        # png in some builds and webp in others)
        self.ext = {}
        for p in self.index:
            comp = p.split("/", 1)[0]
            if comp in ("satellite", "terrain") and comp not in self.ext:
                self.ext[comp] = p.rsplit(".", 1)[-1]

    def has(self, comp: str) -> bool:
        return {"satellite": self.cfg.get("hasSatellite"), "terrain": self.cfg.get("hasTerrain")}.get(comp, True)

    def max_zoom(self, comp: str) -> int:
        return int({"tiles": self.cfg.get("maxZoom", 14),
                    "satellite": self.cfg.get("satelliteMaxZoom", 14),
                    "terrain": self.cfg.get("terrainMaxZoom", 12)}[comp])


class Session:
    def __init__(self, layout: Layout, cache: int, measure: bool = False):
        self.L = layout
        self.cache_n = cache
        self.lru: OrderedDict[int, None] = OrderedDict()
        self.archive = None
        if measure:
            from libzim.reader import Archive
            self.archive = Archive(layout.path)
        self.reset_totals()

    def reset_totals(self):
        self.reads = 0; self.bytes = 0; self.hits = 0; self.misses_404 = 0; self.requests = 0
        self.ms = 0.0

    def fetch(self, path: str):
        self.requests += 1
        loc = self.L.index.get(path)
        if loc is None:
            self.misses_404 += 1
            return
        if self.archive is not None:
            import time
            t = time.perf_counter()
            bytes(self.archive.get_entry_by_path(path).get_item().content)
            self.ms += (time.perf_counter() - t) * 1000
        c = loc[0]
        if c in self.lru:
            self.lru.move_to_end(c); self.hits += 1
            return
        self.reads += 1
        self.bytes += self.L.csize[c]
        self.lru[c] = None
        if len(self.lru) > self.cache_n:
            self.lru.popitem(last=False)


def view_requests(L: Layout, lon, lat, zoom, w, h, layers) -> list[str]:
    reqs = []
    z_vec = min(zoom, L.max_zoom("tiles"))
    if "tiles" in layers:
        for z, x, y in tiles_for_view(lon, lat, z_vec, w, h, 512):
            reqs.append(f"tiles/{z}/{x}/{y}.pbf")
    for comp in ("satellite", "terrain"):
        ext = L.ext.get(comp, "png")
        if comp in layers and L.has(comp):
            zr = min(zoom + 1, L.max_zoom(comp))   # 256px raster: one zoom deeper
            for z, x, y in tiles_for_view(lon, lat, zr, w, h, 256):
                reqs.append(f"{comp}/{z}/{x}/{y}.{ext}")
    return reqs


def run_scenario(L: Layout, name: str, lon, lat, w, h, layers, cache, *, zoom=14, screens=6, measure=False):
    S = Session(L, cache, measure)
    steps = []
    if name == "zoom-in":
        for z in range(4, 15):
            S.reset_totals()
            for p in view_requests(L, lon, lat, z, w, h, layers): S.fetch(p)
            steps.append((f"z{z}", S.reads, S.bytes, S.requests, S.misses_404, S.ms))
    elif name == "pan":
        # pan east one screen at a time at a fixed zoom
        x0, y0 = lonlat_to_pixel(lon, lat, zoom, 512)
        n = 512 * 2 ** zoom
        for i in range(screens):
            S.reset_totals()
            px = x0 + i * w
            lon_i = px / n * 360.0 - 180.0
            for p in view_requests(L, lon_i, lat, zoom, w, h, layers): S.fetch(p)
            steps.append((f"screen{i}", S.reads, S.bytes, S.requests, S.misses_404, S.ms))
    elif name == "startup":
        S.reset_totals()
        for p in ("index.html", "map-config.json", "maplibre-gl.js", "maplibre-gl.css"):
            S.fetch(p)
        z0 = L.cfg.get("zoom", 11)
        c = L.cfg.get("center", [lon, lat])
        for p in view_requests(L, c[0], c[1], z0, w, h, layers): S.fetch(p)
        steps.append((f"first view z{z0}", S.reads, S.bytes, S.requests, S.misses_404, S.ms))
    return steps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zims", nargs="+")
    ap.add_argument("--scenario", choices=["zoom-in", "pan", "startup"], default="zoom-in")
    ap.add_argument("--all", action="store_true", help="run every scenario")
    ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--zoom", type=int, default=14, help="pan zoom")
    ap.add_argument("--screens", type=int, default=6)
    ap.add_argument("--screen", choices=SCREENS, default="phone")
    ap.add_argument("--layers", default="tiles", help="comma list of tiles,satellite,terrain")
    ap.add_argument("--cache", type=int, default=16, help="LRU clusters (libzim default 16)")
    ap.add_argument("--measure", action="store_true",
                    help="also perform the fetches with python-libzim and report wall ms per step")
    a = ap.parse_args()
    layers = set(a.layers.split(","))
    w, h = SCREENS[a.screen]
    layouts = [Layout(p) for p in a.zims]
    lon, lat = a.lon, a.lat
    if lon is None or lat is None:
        c = layouts[0].cfg.get("center", [0, 0]); lon, lat = c[0], c[1]
    scenarios = ["startup", "zoom-in", "pan"] if a.all else [a.scenario]
    for sc in scenarios:
        print(f"\n## {sc}  ({a.screen} {w}x{h}, layers={','.join(sorted(layers))}, "
              f"cache={a.cache} clusters, at {lat:.4f},{lon:.4f}"
              + (f", zoom {a.zoom}, {a.screens} screens" if sc == "pan" else "") + ")")
        results = [run_scenario(L, sc, lon, lat, w, h, layers, a.cache, zoom=a.zoom,
                                screens=a.screens, measure=a.measure) for L in layouts]
        names = [Path(L.path).name for L in layouts]
        colw = 38 if a.measure else 30
        cols = "reads  MB-read  reqs 404" + ("      ms" if a.measure else "")
        print(f"{'step':<14}" + "".join(f"{n[:colw-2]:>{colw}}" for n in names))
        print(f"{'':<14}" + "".join(f"{cols:>{colw}}" for _ in names))
        tot = [[0, 0, 0, 0, 0.0] for _ in layouts]
        for si in range(len(results[0])):
            line = f"{results[0][si][0]:<14}"
            for li, res in enumerate(results):
                _, reads, byts, reqs, m404, ms = res[si]
                tot[li][0] += reads; tot[li][1] += byts; tot[li][2] += reqs; tot[li][3] += m404; tot[li][4] += ms
                line += f"{reads:>10}{byts/1e6:>9.2f}{reqs:>6}{m404:>5}" + (f"{ms:>8.0f}" if a.measure else "")
            print(line)
        line = f"{'total':<14}"
        for t in tot:
            line += f"{t[0]:>10}{t[1]/1e6:>9.2f}{t[2]:>6}{t[3]:>5}" + (f"{t[4]:>8.0f}" if a.measure else "")
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
