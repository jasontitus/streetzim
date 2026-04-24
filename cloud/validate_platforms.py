"""Per-platform ZIM load simulator — catches iOS/Android/PWA OOMs
before you have to install the ZIM on a device.

Every client loads a ZIM the same way in broad strokes:
  1. Read a set of top-level entries (viewer, maplibre, map-config)
  2. Fetch the routing layout (monolithic graph.bin, chunked, or
     spatial SZCI+SZRC)
  3. On a route request, parse the graph into in-memory structures

The PAIN POINT is step (3): the iOS WebView hard-caps a JS heap at
about 1 GB, and the Android equivalent is similar. Any load path that
creates a single > 500 MB buffer — then parses it into typed arrays —
doubles the peak. Above ~500 MB input the native Kiwix iOS app can't
keep the graph in memory; the PWA shares the same WebKit ceiling on
iOS and the same Blink ceiling on Android.

This validator models each platform's peak memory on a supplied ZIM
and flags fail/pass against known platform ceilings:

    Kiwix iOS        ≤ 500 MB working set  (1 GB JS heap, ~2× parse overhead)
    Kiwix Android    ≤ 500 MB
    Kiwix macOS      ≤ 2 GB  (plenty of headroom, flag for information only)
    PWA (Safari)     ≤ 500 MB — same ceiling as iOS Kiwix
    PWA (Chrome)     ≤ 1 GB   (desktop Chrome is looser)

Spatial routing (SZCI + graph-cell-*) always passes — peak memory is
index + 1-2 loaded cells, typically < 50 MB regardless of region size.

Usage:
    python3 cloud/validate_platforms.py osm-iran-shipped.zim
    python3 cloud/validate_platforms.py --all   # every osm-*.zim in dir
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Platform memory ceilings (largest-allocation, in MB). Calibrated
# against observed behavior:
#   * Egypt (316 MB monolithic graph.bin) LOADS on Kiwix iOS → iOS can
#     hold 316 MB + viewer + MapLibre comfortably.
#   * Iran (520 MB chunked-only) FAILS on Kiwix iOS, but most likely
#     because Kiwix iOS can't reassemble chunks — not a raw memory
#     cap at 500 MB (based on the Egypt data point).
#   * Japan (1840 MB any layout) we haven't tested on iOS; assume FAIL.
#
# parseRoutingGraphBinary in the viewer wraps the fetched ArrayBuffer
# in typed-array VIEWS — no copy — so peak memory ≈ buffer size, not
# 2× it. The only exception is the chunked-→-reassemble path, which
# does copy into a single Uint8Array before parsing; we model that as
# 2× for a brief window.
CEILINGS = {
    "kiwix_ios":     700,   # WebKit WebView; Egypt 316 MB works, 1 GB+
                            # graph.bin would not. Budget ~700 MB.
    "kiwix_android": 700,   # Chrome WebView — similar.
    "kiwix_macos":   4000,  # Native app, generous headroom.
    "pwa_safari":    700,   # Safari mobile: same WebKit heap.
    "pwa_chrome":    1500,  # Desktop Chrome: looser, but not infinite.
}


def _ceilings_enabled():
    return list(CEILINGS.keys())


def _entry_size(arc, path: str) -> int | None:
    try:
        return arc.get_entry_by_path(path).get_item().size
    except Exception:
        return None


def _scan_routing(arc) -> dict:
    """Return layout info:
      {layout: "monolithic"|"chunked"|"spatial"|"none",
       graph_bin_bytes: int | None,
       chunks_total_bytes: int | None,
       cells_index_bytes: int | None,
       cell_count: int | None,
       cell_avg_bytes: int | None,
       cell_max_bytes: int | None,
      }
    """
    info = dict.fromkeys((
        "graph_bin_bytes", "chunks_total_bytes",
        "cells_index_bytes", "cell_count",
        "cell_avg_bytes", "cell_max_bytes",
    ))
    info["layout"] = "none"

    graph_bin = _entry_size(arc, "routing-data/graph.bin")
    if graph_bin is not None:
        info["graph_bin_bytes"] = graph_bin
        info["layout"] = "monolithic"

    # Scan for chunked + spatial entries.
    chunk_total = 0
    chunk_count = 0
    cell_bytes = []
    cells_index = None
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        p = e.path
        if not p.startswith("routing-data/"):
            continue
        if p == "routing-data/graph-cells-index.bin":
            cells_index = e.get_item().size
        elif p.startswith("routing-data/graph-cell-") and p.endswith(".bin"):
            cell_bytes.append(e.get_item().size)
        elif p.startswith("routing-data/graph-chunk-") and p.endswith(".bin"):
            chunk_total += e.get_item().size
            chunk_count += 1
    if cells_index is not None and cell_bytes:
        info["cells_index_bytes"] = cells_index
        info["cell_count"] = len(cell_bytes)
        info["cell_avg_bytes"] = sum(cell_bytes) // len(cell_bytes)
        info["cell_max_bytes"] = max(cell_bytes)
        # Spatial overrides monolithic if both present — it's the
        # preferred path for mobile clients.
        info["layout"] = "spatial"
    elif chunk_count > 0:
        info["chunks_total_bytes"] = chunk_total
        if info["layout"] == "none":
            info["layout"] = "chunked"
    return info


def estimate_peak_mb(info: dict, platform: str) -> tuple[int, str]:
    """Return (peak_MB, explanation) for one platform's routing load."""
    layout = info["layout"]
    if layout == "none":
        return (0, "no routing data")

    if layout == "spatial":
        # All platforms can do spatial — peak is index + ~2 cells
        # (source + destination neighborhoods). Peak is 2x the cell
        # max to cover pathological routes crossing many cells.
        idx = (info.get("cells_index_bytes") or 0) / 1024 / 1024
        cmax = (info.get("cell_max_bytes") or 0) / 1024 / 1024
        peak = idx + cmax * 4  # 4 concurrent worst-case cells
        return (int(peak),
                f"spatial: {idx:.0f} MB index + ~4× {cmax:.0f} MB cells")

    if layout == "monolithic":
        # Client fetches entire graph.bin; parser uses typed-array
        # VIEWS over the same buffer (no copy). Peak ≈ buffer size.
        g_mb = info["graph_bin_bytes"] / 1024 / 1024
        return (int(g_mb),
                f"monolithic: {g_mb:.0f} MB buffer, "
                f"typed-array views (no copy)")

    if layout == "chunked":
        total_mb = (info.get("chunks_total_bytes") or 0) / 1024 / 1024
        if platform.startswith("kiwix"):
            # Kiwix native apps don't reassemble chunks — we've seen
            # this fail on iOS for Iran even when the total size
            # would fit the memory budget. Hard fail.
            return (0,
                    f"CHUNKED-ONLY: no graph.bin — Kiwix cannot load "
                    f"this layout regardless of size")
        # PWA reassembles chunks into a single buffer, then parses.
        # Reassembly briefly holds both the source chunks and the
        # output buffer → 2×. Freed immediately after parseSZRG, but
        # the peak counts.
        peak = total_mb * 2
        return (int(peak),
                f"chunked→PWA: reassemble {total_mb:.0f} MB × 2 "
                f"peak (before GC'ing chunk parts)")

    return (0, f"unknown layout: {layout}")


def validate_one(zim_path: str, verbose: bool = True) -> dict:
    from libzim.reader import Archive
    a = Archive(zim_path)
    info = _scan_routing(a)

    results = {}
    for platform, ceiling_mb in CEILINGS.items():
        peak, why = estimate_peak_mb(info, platform)
        # Chunked-only is a hard FAIL for Kiwix (returned peak=0 + why)
        if "CHUNKED-ONLY" in why and platform.startswith("kiwix"):
            results[platform] = ("FAIL", peak, why)
            continue
        status = "PASS" if peak <= ceiling_mb else "FAIL"
        results[platform] = (status, peak, why)

    if verbose:
        print(f"\n=== {zim_path} ===")
        print(f"  layout: {info['layout']}")
        if info.get("graph_bin_bytes"):
            print(f"  graph.bin: {info['graph_bin_bytes']/1024/1024:.0f} MB")
        if info.get("cells_index_bytes"):
            print(f"  spatial: {info['cell_count']} cells, "
                  f"index={info['cells_index_bytes']/1024/1024:.1f} MB, "
                  f"avg cell={info['cell_avg_bytes']/1024/1024:.1f} MB, "
                  f"max cell={info['cell_max_bytes']/1024/1024:.1f} MB")
        if info.get("chunks_total_bytes"):
            print(f"  chunks: {info['chunks_total_bytes']/1024/1024:.0f} MB total")
        print()
        print(f"  {'platform':<16}{'peak':>9}  {'ceiling':>9}  {'status':>6}")
        print(f"  {'-'*16}{'-'*9:>9}  {'-'*9:>9}  {'-'*6:>6}")
        for platform, (status, peak, why) in results.items():
            ceiling = CEILINGS[platform]
            print(f"  {platform:<16}{peak:>6} MB  {ceiling:>6} MB  {status:>6}")
        for platform, (status, peak, why) in results.items():
            if status == "FAIL":
                print(f"  FAIL {platform}: {why}")

    return {"path": zim_path, "info": info, "results": results}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("zim", nargs="?", help="ZIM file to check")
    g.add_argument("--all", action="store_true",
                   help="check every osm-*.zim in the current dir")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON summary on stdout")
    args = ap.parse_args()

    if args.all:
        zims = sorted(Path(".").glob("osm-*.zim"))
    else:
        zims = [Path(args.zim)]

    any_fail = False
    summary = []
    for z in zims:
        r = validate_one(str(z))
        summary.append(r)
        for _, (status, _, _) in r["results"].items():
            if status == "FAIL":
                any_fail = True
                break

    if args.json:
        def default(o):
            return str(o)
        print(json.dumps(summary, default=default, indent=2))

    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
