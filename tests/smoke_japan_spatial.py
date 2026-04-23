"""Comprehensive read-path smoke tests for a spatial ZIM.

Usage:
  python tests/smoke_japan_spatial.py osm-japan-2026-04-22-spatial.zim

Walks every subsystem a Kiwix Desktop / PWA / mcpzim session would hit:

  * ZIM metadata (Title, Description, main_entry, illustration)
  * Full-text (xapian) search  -- runs real queries
  * Suggestion searcher        -- typically disabled on streetzim
  * streetzim-meta.json        -- build-stamped summary
  * map-config.json            -- feature flags
  * search-data manifest + sample chunk parse
  * tile probe                 -- read a z14 tile from every tile layer
  * routing                    -- load SZCI + run a real A*
  * satellite probe            -- single AVIF tile
  * terrain probe              -- single WebP tile
  * wikidata probe             -- manifest + sample chunk parse

Fails fast on anything that raises; prints a compact per-section PASS /
details line. Kiwix-Desktop-specific crash on "find" won't reproduce
here (that's a UI-side issue) but any data-layer corruption that might
trigger it will surface.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def section(name: str):
    """Decorator: wrap a probe so its failure message is localised."""
    def _wrap(fn):
        def _run(*a, **kw):
            t0 = time.time()
            try:
                detail = fn(*a, **kw)
            except Exception as exc:
                print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
                raise
            elapsed_ms = (time.time() - t0) * 1000
            print(f"[ OK ] {name}  ({elapsed_ms:.1f} ms)  {detail}")
        return _run
    return _wrap


@section("ZIM open + metadata")
def probe_metadata(arc) -> str:
    want = ("Title", "Description", "Language", "Creator",
            "Publisher", "Date", "Name", "Flavour", "Scraper", "License")
    present = {}
    for k in want:
        try:
            v = arc.get_metadata(k)
            if isinstance(v, bytes):
                v = v.decode("utf-8", errors="replace")
            present[k] = v[:40] + ("…" if len(v) > 40 else "")
        except Exception:
            pass
    missing = sorted(set(want) - set(present))
    assert "Title" in present, "missing Title metadata (required)"
    has_illus = bool(arc.has_illustration())
    main_desc = "(no main entry)"
    if arc.has_main_entry:
        main = arc.main_entry
        while main.is_redirect:
            main = main.get_redirect_entry()
        main_desc = f"main={main.path!r}"
    return (f"entries={arc.entry_count} {main_desc} "
            f"illus={has_illus} missing_meta={missing or 'none'}")


@section("Full-text (xapian) search")
def probe_fulltext(arc) -> str:
    from libzim.search import Query, Searcher
    if not getattr(arc, "has_fulltext_index", False):
        return "no fulltext index (skip)"
    searcher = Searcher(arc)
    # Two query strings — one common English, one expected to be region-specific.
    totals = {}
    for q_str in ("park", "station"):
        q = Query().set_query(q_str)
        res = searcher.search(q)
        totals[q_str] = res.getEstimatedMatches()
    assert all(v > 0 for v in totals.values()), f"no hits: {totals}"
    return f"hits {totals}"


@section("Suggestion searcher")
def probe_suggestions(arc) -> str:
    from libzim.suggestion import SuggestionSearcher
    s = SuggestionSearcher(arc)
    sugg = s.suggest("Tokyo")
    n = sugg.getEstimatedMatches()
    return (f"{n} suggestions"
            + (" (streetzim doesn't emit a title index — expected 0)" if n == 0 else ""))


@section("streetzim-meta.json")
def probe_streetzim_meta(arc) -> str:
    try:
        data = bytes(arc.get_entry_by_path("streetzim-meta.json").get_item().content)
    except Exception:
        return "not present (older schema — ok)"
    meta = json.loads(data)
    required = {"name", "buildDate", "hasRouting"}
    missing = required - set(meta.keys())
    assert not missing, f"missing meta keys: {missing}"
    routing = meta.get("routingGraph", {}) or {}
    return (f"name={meta.get('name')!r} date={meta.get('buildDate')} "
            f"hasRouting={meta.get('hasRouting')} "
            f"routingNodes={routing.get('nodes')} routingEdges={routing.get('edges')}")


@section("map-config.json")
def probe_map_config(arc) -> str:
    data = bytes(arc.get_entry_by_path("map-config.json").get_item().content)
    cfg = json.loads(data)
    flags = [k for k, v in cfg.items() if k.startswith("has") and v]
    return f"flags={flags}"


@section("search-data manifest + chunk")
def probe_search_data(arc) -> str:
    mani = json.loads(bytes(arc.get_entry_by_path("search-data/manifest.json").get_item().content))
    chunks = mani.get("chunks", {})
    assert isinstance(chunks, dict), "search-data manifest missing chunks dict"
    assert len(chunks) > 0, "search-data manifest has no chunks"
    # Size-check the top 3 chunks. Entries larger than ~50 MB tend to
    # wedge Kiwix Desktop's "find" UI (and any client that naively JSON-
    # parses the whole thing) — the '__' bucket on Japan is 349 MB of
    # CJK/Cyrillic names.
    chunk_sizes: list[tuple[int, str]] = []
    for prefix in chunks:
        try:
            e = arc.get_entry_by_path(f"search-data/{prefix}.json")
            size = len(bytes(e.get_item().content))
            chunk_sizes.append((size, prefix))
        except Exception:
            pass
    chunk_sizes.sort(reverse=True)
    biggest_size, biggest_prefix = chunk_sizes[0]
    chunk = json.loads(bytes(arc.get_entry_by_path(f"search-data/{biggest_prefix}.json").get_item().content))
    assert isinstance(chunk, list) and len(chunk) > 0
    sample = chunk[0]
    required = {"n", "t", "a", "o"}
    missing_keys = required - set(sample.keys())
    assert not missing_keys, f"chunk sample missing keys {missing_keys}"

    warn = ""
    big_chunks = [(sz, p) for sz, p in chunk_sizes if sz > 50 * 1024 * 1024]
    if big_chunks:
        worst = big_chunks[0]
        warn = (f"  ⚠ {len(big_chunks)} chunk(s) >50MB — "
                f"biggest {worst[1]!r}={worst[0]/1e6:.0f}MB "
                "can crash Kiwix Desktop 'find'")
    return (f"{len(chunks)} chunks, biggest={biggest_prefix!r} "
            f"({biggest_size/1e6:.1f}MB, {chunks[biggest_prefix]} records){warn}")


@section("category-index")
def probe_category_index(arc) -> str:
    try:
        data = bytes(arc.get_entry_by_path("category-index/manifest.json").get_item().content)
    except Exception:
        return "not present (older schema — ok)"
    mani = json.loads(data)
    sample_cat = sorted(mani.keys())[:3]
    return f"{len(mani)} categories — sampling {sample_cat}"


@section("tile probe (z0 + a random z14)")
def probe_tiles(arc) -> str:
    # z0 root tile always exists
    z0 = bytes(arc.get_entry_by_path("tiles/0/0/0.pbf").get_item().content)
    assert len(z0) > 0, "z0 tile is empty"
    # Walk to find the first z14 tile path
    z14_sample = None
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect: continue
        if e.path.startswith("tiles/14/"):
            z14_sample = e.path
            break
    assert z14_sample, "no z14 tile found"
    data = bytes(arc.get_entry_by_path(z14_sample).get_item().content)
    assert len(data) > 0, f"z14 tile {z14_sample} is empty"
    return f"z0={len(z0)}B, z14 sample {z14_sample} = {len(data)}B"


@section("satellite tile probe (if present)")
def probe_satellite(arc) -> str:
    sample = None
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect: continue
        if e.path.startswith("satellite/"):
            sample = e.path
            break
    if sample is None:
        return "no satellite tiles (skip)"
    data = bytes(arc.get_entry_by_path(sample).get_item().content)
    return f"sample {sample} = {len(data)}B"


@section("terrain tile probe (if present)")
def probe_terrain(arc) -> str:
    sample = None
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect: continue
        if e.path.startswith("terrain/"):
            sample = e.path
            break
    if sample is None:
        return "no terrain tiles (skip)"
    data = bytes(arc.get_entry_by_path(sample).get_item().content)
    return f"sample {sample} = {len(data)}B"


@section("wikidata probe (if present)")
def probe_wikidata(arc) -> str:
    try:
        mani = json.loads(bytes(arc.get_entry_by_path("wikidata/manifest.json").get_item().content))
    except Exception:
        return "not present"
    prefix = next(iter(mani.get("chunks", {})), None)
    if prefix is None:
        return "manifest has no chunks"
    chunk = json.loads(bytes(arc.get_entry_by_path(f"wikidata/{prefix}.json").get_item().content))
    return f"{sum(mani.get('chunks',{}).values())} total wikidata items; sample chunk {prefix!r} has {len(chunk)}"


@section("routing: graph parse + sample A*")
def probe_routing(arc, zim_path: str) -> str:
    # Auto-detect layout: spatial SZCI, chunked v5, or monolithic v4.
    try:
        arc.get_entry_by_path("routing-data/graph-cells-index.bin")
        return _probe_routing_spatial(zim_path)
    except Exception:
        pass
    return _probe_routing_monolithic(zim_path)


def _probe_routing_monolithic(zim_path: str) -> str:
    from tests.szrg_reader import load_from_zim
    from tests.szrg_astar import find_route
    g = load_from_zim(zim_path)
    if g.num_nodes < 2:
        return f"only {g.num_nodes} nodes — nothing to route"
    # First pair of reachable nodes (nodes 0..9 have edges in every ZIM)
    s_node = 0
    # Pick an end node that's on a separate island of traversal — just
    # grab something mid-graph.
    e_node = g.num_nodes // 2
    r = find_route(g, s_node, e_node, max_pops=500_000)
    status = "unreachable" if r is None else f"{len(r.node_sequence)} nodes, {r.total_dist_m/1000:.1f} km"
    return f"v{g.version} monolithic · nodes={g.num_nodes:,} · {s_node}→{e_node}: {status}"


def _probe_routing_spatial(zim_path: str) -> str:
    from tests.szrg_spatial import load_spatial_from_zim
    from tests.szrg_spatial_astar import find_route_spatial
    sg = load_spatial_from_zim(zim_path, cache_limit=32)
    # Pick two arbitrary graph-valid nodes + route between them
    import numpy as np
    adj_offsets = []
    # We have nodes_scaled but need adj_offsets per cell (spatial graph doesn't
    # expose a global adj_offsets). Use the first cell's local nodes + a real route.
    cell0 = sg._ensure_cell(0)
    nodes_in_cell = [int(cell0.cell_nodes_global[i])
                     for i in range(min(cell0.cell_nodes_global.shape[0], 100))]
    # Route between the first and last of cell 0's nodes — almost certainly
    # reachable since they share outgoing edges that stay in-cell most of
    # the time.
    if len(nodes_in_cell) >= 2:
        s_node, e_node = nodes_in_cell[0], nodes_in_cell[-1]
        r = find_route_spatial(sg, s_node, e_node, max_pops=500_000)
        if r is None:
            return f"A* returned None for {s_node}->{e_node} (possibly unreachable in cell)"
        return (f"v{sg._index.version} index · nodes={sg.num_nodes:,} "
                f"cells={sg._index.num_cells} · "
                f"test route {s_node}→{e_node}: "
                f"{len(r.node_sequence)} nodes, {r.total_dist_m/1000:.1f} km, "
                f"cells_loaded={sg.cells_loaded}")
    return "cell 0 has too few nodes to test routing"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zim", help="path to spatial ZIM to smoke-test")
    args = ap.parse_args()

    zim_path = args.zim
    print(f"=== smoke-testing {zim_path} ===")
    print(f"    size: {Path(zim_path).stat().st_size/1e9:.2f} GB")
    from libzim.reader import Archive
    arc = Archive(zim_path)

    failed = False
    for probe in (
        lambda: probe_metadata(arc),
        lambda: probe_streetzim_meta(arc),
        lambda: probe_map_config(arc),
        lambda: probe_fulltext(arc),
        lambda: probe_suggestions(arc),
        lambda: probe_search_data(arc),
        lambda: probe_category_index(arc),
        lambda: probe_tiles(arc),
        lambda: probe_satellite(arc),
        lambda: probe_terrain(arc),
        lambda: probe_wikidata(arc),
        lambda: probe_routing(arc, zim_path),
    ):
        try:
            probe()
        except Exception:
            failed = True
            # The @section wrapper already printed a [FAIL] line.
    print()
    print(f"=== {'FAIL' if failed else 'PASS'} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
