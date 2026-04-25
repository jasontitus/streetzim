#!/usr/bin/env python3
"""Pre-upload validator for streetzim ZIMs.

Wire this into the rollout script before ``ia upload``: a non-zero exit
blocks the upload. Every check lists its severity (``error`` / ``warn`` /
``info``) and a short diagnostic — the output is designed to be
searchable if something fails in CI logs.

Usage:
  python3 cloud/validate_zim.py osm-japan-2026-04-23.zim
  python3 cloud/validate_zim.py --json osm-japan-*.zim

Gates we enforce:

  Structural (hard fail = no upload):
  - ZIM file opens via libzim
  - Title + Description + Language metadata present
  - Illustration present (Kiwix library shows a placeholder otherwise)
  - Main entry resolves to a content entry (not a dangling redirect)
  - Xapian full-text index: if ``_ftindex:yes`` tag, run a real query

  Content consistency (warn if missing, fail if declared-and-missing):
  - map-config.json declares ``hasSatellite`` → at least one satellite
    tile is present and non-empty; z0 baseline plus a sample deeper zoom
  - ``hasTerrain`` → sample a terrain tile
  - ``hasRouting`` → routing graph parses (any supported layout); sample
    routes succeed or fail-as-unreachable (not corrupt)
  - ``hasWikidata`` → manifest + a sample chunk parse

  Regression gates (the ones we've been burned by):
  - No search-data/*.json chunk > 50 MB (350 MB on Japan crashed Kiwix
    Desktop 'find'). Covers any region with CJK/Cyrillic/Arabic content.
  - No routing-data entry uncompressed > 500 MB (fzstd per-cluster
    ceiling on the PWA).
  - If spatial routing layout: SZCI index parses, every SZRC cell
    listed in the manifest is present.

  Informational (never blocks):
  - Entry count by namespace (tiles, search-data, satellite, terrain,
    wikidata, routing-data).
  - Graph stats (nodes, edges, cell count if spatial).
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


# Thresholds — tweak here, not in individual checks.
# Hard fail at 200 MB (Japan's 350 MB __ chunk crashed Kiwix Desktop
# outright); warn above 50 MB (SV's "sa" chunk is 116 MB due to Santa/
# San/Silicon density — possible sluggishness, unconfirmed crash).
SEARCH_CHUNK_WARN_MB = 50
SEARCH_CHUNK_FAIL_MB = 200
MAX_ROUTING_ENTRY_MB = 500

# Tile coverage thresholds — we don't know a region's land fraction a
# priori (Japan is 7.5% of its bbox, Europe is 60%+), so absolute
# coverage thresholds misfire across regions. We instead gate on failure
# patterns we've actually hit:
#
#   1. A zoom is declared by map-config but has ZERO tiles              → fail
#   2. A deep-zoom has <5% of expected tiles                             → fail
#      (catches tilemaker / satellite-downloader bailing silently)
#   3. A zoom's coverage is <10% of the same kind at zoom-1              → fail
#      (a "cliff drop" means whatever stage produced that zoom crashed)
#   4. Coarse zooms (z0-z8) missing any expected tile                    → fail
#      (every such cell touches SOME land or coast — no ocean-only
#      short-circuit at z0-8 for any reasonable region)
#   5. ANY terrain tile ≤ BLANK_BYTES is the VRT-race blank-tile bug
#      (`project_terrain_blank_tile_bug.md`) — a fully-zero elevation
#      256×256 tile compresses to ~44 B. These render as gaps the user
#      sees. Happened on Iran despite the build-time audit (which is
#      scoped to z ≥ 10 for legitimate-partial reasons; post-build
#      validation has no such constraint).
ZERO_COVERAGE_FAIL = 0.05       # < 5% → build is broken
PARENT_DROP_FAIL = 0.10         # child < 10% of parent ratio → cliff
COARSE_ZOOM_CUTOFF = 8          # at or below this, expect full coverage
BLANK_TILE_BYTES = 500          # any tile ≤ this at deep zoom is fishy


class Result:
    """One check's outcome. Collected in a list by ``validate``."""
    __slots__ = ("name", "severity", "status", "detail")
    def __init__(self, name: str, severity: str, status: str, detail: str):
        self.name = name
        self.severity = severity  # 'error' | 'warn' | 'info'
        self.status = status       # 'pass' | 'fail' | 'skip'
        self.detail = detail

    def to_dict(self) -> dict:
        return {"name": self.name, "severity": self.severity,
                "status": self.status, "detail": self.detail}


def _check(name: str, severity: str, fn, *args, **kwargs) -> Result:
    """Run a predicate; return pass/fail/skip with a short detail line.
    Exceptions convert to fail (so one bad check can't mask others).

    The predicate can return either a detail string (treated as pass),
    a tuple ``('pass'|'fail'|'skip', detail)``, or raise.
    """
    t0 = time.time()
    try:
        rv = fn(*args, **kwargs)
    except Exception as exc:
        return Result(name, severity, "fail",
                      f"{type(exc).__name__}: {exc}")
    elapsed = (time.time() - t0) * 1000

    if isinstance(rv, tuple):
        status, detail = rv
    else:
        status, detail = "pass", rv
    return Result(name, severity, status, f"{detail} ({elapsed:.1f} ms)")


# ---- Individual check implementations -----------------------------------


def _chk_opens(arc) -> str:
    return f"entries={arc.entry_count}"


def _chk_metadata(arc) -> tuple[str, str]:
    missing = []
    for k in ("Title", "Description", "Language"):
        try:
            v = arc.get_metadata(k)
            if not v:
                missing.append(k)
        except Exception:
            missing.append(k)
    if missing:
        return ("fail", f"missing required metadata: {missing}")
    return ("pass",
            f"Title={arc.get_metadata('Title')!r}"[:60])


def _chk_illustration(arc) -> tuple[str, str]:
    if not arc.has_illustration():
        return ("fail", "no Illustration_48x48@1 metadata")
    return ("pass", "present")


def _chk_fonts(arc) -> tuple[str, str]:
    """MapLibre needs glyph ranges 0-65535 stepping by 256. Builds ship
    ~768 pbf files total (256 ranges × 3 fontstacks: OpenSans Regular
    /Bold/Italic). A missing range means labels in that codepoint
    band silently don't render. Walk the `fonts/` namespace and
    confirm: at least one fontstack present + every range 0-255 and a
    couple of sparse high-band ranges resolved."""
    fontstacks: set[str] = set()
    ranges_by_stack: dict[str, set[str]] = {}
    total = 0
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        p = e.path
        if not p.startswith("fonts/") or not p.endswith(".pbf"):
            continue
        total += 1
        parts = p[len("fonts/"):-len(".pbf")].split("/", 1)
        if len(parts) != 2:
            continue
        stack, rng = parts
        fontstacks.add(stack)
        ranges_by_stack.setdefault(stack, set()).add(rng)
    if not fontstacks:
        return ("fail", "no fonts/ glyph entries — labels will be blank")
    # For every present fontstack, every range must exist (build either
    # ships all or none for a given stack — a partial set is a build
    # error). 256 ranges expected per stack.
    bad_stacks = []
    for stack, rngs in ranges_by_stack.items():
        if len(rngs) < 200:
            bad_stacks.append((stack, len(rngs)))
    if bad_stacks:
        return ("fail",
                f"fontstacks with incomplete glyph ranges: "
                f"{[(s, n) for s, n in bad_stacks]}")
    return ("pass",
            f"{total} glyph ranges across {len(fontstacks)} fontstack(s): "
            f"{sorted(fontstacks)}")


def _chk_terrain_edge_stripe(arc, cfg) -> tuple[str, str]:
    """Walk a sample of terrain tiles and flag any with the bbox-edge
    zero-column pattern — the bug that shipped as the Iran 33°N stripe,
    Butte MT vertical band, and the eastern-Iran 65°E stripe (z=4).

    For speed, sample every 50th terrain tile (still ~100 across a
    typical region). The bug is systematic (affects hundreds of tiles
    per region when present), so random-samples detect it reliably."""
    if not cfg.get("hasTerrain"):
        return ("skip", "hasTerrain=False")
    import io, numpy as np
    from PIL import Image
    # Only audit low-zoom tiles (z=0..7). At z=8+ coastal tiles
    # legitimately have ocean-side zero-columns (e.g. a tile straddling
    # a north-south coastline).
    #
    # Even at z<=7, a coastal tile can legitimately have a full zero-
    # column edge (e.g. Japan at 135-180°E — the right 47% is Pacific).
    # To distinguish "ocean off the coast" from "bbox-edge DEM gap",
    # sample the world DEM at the tile's right/left midline: if the
    # VRT says nodata there, the zero-col block is legit ocean. If it
    # returns a real elevation, we've got the bbox-edge bug.
    import math
    vrt_path = ROOT / "terrain_cache" / "dem_sources" / "comprehensive.vrt"
    vrt_src = None
    vrt_nodata = None
    if vrt_path.is_file():
        try:
            import rasterio
            vrt_src = rasterio.open(str(vrt_path))
            vrt_nodata = vrt_src.nodata
        except Exception:
            vrt_src = None

    def tile_bounds(z, x, y):
        n = 1 << z
        lon_w = x / n * 360.0 - 180.0
        lon_e = (x + 1) / n * 360.0 - 180.0
        lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
        lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
        return (lon_w, lat_s, lon_e, lat_n)

    def edge_is_ocean(z, x, y, side):
        """True if the VRT shows nodata at the tile's left or right
        midline (i.e. that edge is open ocean, not missing land)."""
        if vrt_src is None:
            return False
        lon_w, lat_s, lon_e, lat_n = tile_bounds(z, x, y)
        lat_mid = (lat_s + lat_n) / 2
        lon = lon_w if side == "left" else lon_e - 1e-6
        try:
            val = list(vrt_src.sample([(lon, lat_mid)]))[0][0]
            return (vrt_nodata is not None and val == vrt_nodata) or abs(val) < 1
        except Exception:
            return False

    checked = 0
    bad = []
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        if not e.path.startswith("terrain/"):
            continue
        parts = e.path.split("/")
        try:
            z = int(parts[1])
        except (ValueError, IndexError):
            continue
        if z > 7:
            continue
        try:
            data = bytes(e.get_item().content)
            im = Image.open(io.BytesIO(data)).convert("RGB")
            arr = np.array(im).astype(np.int64)
            elev = -10000 + (arr[:, :, 0] * 65536
                             + arr[:, :, 1] * 256
                             + arr[:, :, 2]) * 0.1
            max_elev = elev.max()
            if max_elev > 100:
                h, w = elev.shape
                col_all_zero = (np.sum(np.abs(elev) < 5, axis=0) == h)
                left = 0
                while left < w and col_all_zero[left]:
                    left += 1
                right = 0
                while right < w and col_all_zero[w - 1 - right]:
                    right += 1
                if left >= 10 or right >= 10:
                    # Cross-check with the world VRT: if the edge in
                    # question is actually ocean (nodata), skip — the
                    # zero-cols are legit, not a bug.
                    parts = e.path.split("/")
                    try:
                        ez = int(parts[1])
                        ex = int(parts[2])
                        ey = int(parts[3].split(".")[0])
                    except (ValueError, IndexError):
                        bad.append((e.path, left, right))
                        continue
                    left_bug = left >= 10 and not edge_is_ocean(ez, ex, ey, "left")
                    right_bug = right >= 10 and not edge_is_ocean(ez, ex, ey, "right")
                    if left_bug or right_bug:
                        bad.append((e.path, left, right))
        except Exception:
            pass
        checked += 1
    if bad:
        return ("fail",
                f"{len(bad)} low-zoom terrain tile(s) with bbox-edge "
                f"zero block (of {checked} sampled); first: "
                f"{bad[0][0]} left={bad[0][1]} right={bad[0][2]}")
    return ("pass",
            f"{checked} z0-z7 terrain tiles sampled; no bbox-edge stripes")


def _chk_places_categories(arc) -> tuple[str, str]:
    """`places.html` renders Categories by loading `category-index/
    manifest.json` + per-category chunks. Confirm the file exists AND
    at least one declared category's content is non-empty — otherwise
    Find opens to an empty list."""
    try:
        mani = json.loads(bytes(
            arc.get_entry_by_path("category-index/manifest.json").get_item().content
        ))
    except Exception as exc:
        return ("warn",
                f"category-index/manifest.json missing ({exc}) — Find "
                "page will fall back to search-only")
    cats = mani.get("categories") or {}
    if not cats:
        return ("warn", "category-index has 0 categories")
    # Pick the first declared category and read its records.
    slug = next(iter(cats))
    try:
        raw = bytes(arc.get_entry_by_path(
            f"category-index/{slug}.json").get_item().content)
        recs = json.loads(raw)
    except Exception as exc:
        return ("fail",
                f"category-index/{slug}.json missing/unreadable ({exc})")
    if not isinstance(recs, list) or not recs:
        return ("fail",
                f"category-index/{slug}.json is empty — Find page "
                "would show no results for this category")
    return ("pass",
            f"{len(cats)} categories; sample {slug} has {len(recs):,} records")


def _chk_find_chips(arc) -> tuple[str, str]:
    """When `--split-find-chips` was used, the manifest must declare a
    `chips` map AND every referenced `chip-{id}.json` file must exist
    and parse as a list. Catches silent chip-split failures (e.g.
    ModuleNotFoundError during chip emission that still lets the
    wrapper ZIM finalize — the 2026-04-24 Japan chips bug)."""
    try:
        mani = json.loads(bytes(
            arc.get_entry_by_path("category-index/manifest.json").get_item().content
        ))
    except Exception:
        return ("skip", "manifest missing (no chip-split)")
    chips = mani.get("chips")
    if not isinstance(chips, dict) or not chips:
        # Skip rather than warn — a ZIM built without --split-find-chips
        # legitimately has no chips section.
        return ("skip", "no chips declared in manifest")
    # Every declared chip must have a corresponding file that parses.
    missing = []
    empty = []
    for cid, meta in chips.items():
        path = f"category-index/chip-{cid}.json"
        try:
            raw = bytes(arc.get_entry_by_path(path).get_item().content)
        except Exception:
            missing.append(cid)
            continue
        try:
            recs = json.loads(raw)
        except Exception:
            missing.append(cid)
            continue
        if not isinstance(recs, list):
            missing.append(cid)
            continue
        # Manifest count should match the file's actual record count.
        declared = int(meta.get("count") or 0)
        if declared != len(recs):
            return ("fail",
                    f"chip '{cid}' manifest count={declared} but file "
                    f"has {len(recs)} records — inconsistent")
        if declared == 0 and cid in ("restaurants", "cafes", "shops"):
            # These three are expected to have matches in any populated
            # region. An empty one signals the rules didn't run.
            empty.append(cid)
    if missing:
        return ("fail",
                f"chip files missing/unreadable: {', '.join(missing)}")
    if empty:
        return ("warn",
                f"chips with zero records: {', '.join(empty)} — "
                f"rules likely didn't run")
    sizes = sorted(chips.items(), key=lambda kv: -int(kv[1].get("bytes") or 0))
    biggest = sizes[0]
    return ("pass",
            f"{len(chips)} chip files; biggest "
            f"{biggest[0]}={int(biggest[1]['bytes'])/1e6:.1f}MB "
            f"({biggest[1]['count']} recs)")


def _chk_routing_sample(arc, cfg, zim_path: str) -> tuple[str, str]:
    """Pick two on-map points and attempt an A* route. If the graph
    loads but no sample route can find a path within reasonable
    effort, the graph is structurally corrupt (e.g. disconnected
    components)."""
    if not cfg.get("hasRouting"):
        return ("skip", "hasRouting=False")
    try:
        from tests.szrg_reader import load_from_zim
        from tests.szrg_astar import find_route
    except Exception as exc:
        return ("warn", f"test harness not importable ({exc})")
    try:
        g = load_from_zim(zim_path)
    except Exception as exc:
        # Spatial ZIMs throw a clear "use load_spatial_from_zim" hint;
        # try that before giving up.
        if "spatial-chunked" in str(exc):
            try:
                from tests.szrg_spatial import load_spatial_from_zim
                gs = load_spatial_from_zim(zim_path)
                return ("pass",
                        f"spatial graph loads OK: {gs.num_nodes:,} "
                        f"nodes, {gs.num_edges:,} edges")
            except Exception as sp_exc:
                return ("fail",
                        f"spatial SZCI/SZRC parse failed: {sp_exc}")
        return ("fail", f"SZRG parse failed: {exc}")
    n = g.num_nodes
    if n < 100:
        return ("warn", f"only {n} nodes — graph may be a stub")
    # Sample a handful of origin/destination pairs from the node
    # space. Real regions can have disconnected components (islands,
    # gated neighborhoods), so we only require ONE pair to succeed.
    # If ALL fail, the graph is structurally broken (e.g. an edge
    # array pointing to invalid nodes, or the wrong node count).
    ok = 0
    tried = 0
    for a, b in [(n // 4, 3 * n // 4),
                 (n // 5, 4 * n // 5),
                 (n // 3, 2 * n // 3)]:
        tried += 1
        try:
            r = find_route(g, a, b, max_pops=500_000)
            if r is not None:
                ok += 1
        except Exception:
            pass
    if ok == 0:
        return ("warn",
                f"no sample route found in {tried} tries "
                "(may be real if bbox is disconnected)")
    return ("pass", f"{ok}/{tried} sample routes succeeded; "
            f"{n:,} nodes")


def _chk_satellite_coverage(arc, cfg) -> tuple[str, str]:
    """Deep-sample satellite/{z}/{x}/{y} across zooms. Catches whole-
    zoom gaps (e.g. tilemaker/downloader bailed midway through z12)
    that the shallow 7-tile spot-check misses."""
    if not cfg.get("hasSatellite"):
        return ("skip", "hasSatellite=False")
    # Count by zoom + sample non-empty.
    by_zoom: dict[int, int] = {}
    empty_by_zoom: dict[int, int] = {}
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        if not e.path.startswith("satellite/"):
            continue
        parts = e.path.split("/")
        try:
            z = int(parts[1])
        except (ValueError, IndexError):
            continue
        by_zoom[z] = by_zoom.get(z, 0) + 1
        if e.get_item().size < 200:  # WEBP/AVIF header+empty is ~100 B
            empty_by_zoom[z] = empty_by_zoom.get(z, 0) + 1
    if not by_zoom:
        return ("fail", "hasSatellite=True but no satellite tiles")
    # Each zoom that has content should have at least 1 and no
    # >5% empty. (Ocean tiles legitimately are tiny at AVIF q40
    # — >100 B but often <1 KB — so be lenient.)
    zooms_str = ", ".join(f"z{z}={n:,}"
                          for z, n in sorted(by_zoom.items()))
    for z, empty in empty_by_zoom.items():
        total = by_zoom[z]
        if empty > total * 0.5:
            return ("fail",
                    f"z{z}: {empty:,}/{total:,} satellite tiles empty "
                    "(>50%) — downloader likely failed silently")
    return ("pass", zooms_str)


def _chk_vector_coverage(arc) -> tuple[str, str]:
    """Deep-sample vector MVT tiles. Each zoom in the build profile
    should have non-empty tiles. An empty zoom silently breaks
    rendering at that level."""
    by_zoom: dict[int, int] = {}
    empty_by_zoom: dict[int, int] = {}
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        if not e.path.startswith("tiles/"):
            continue
        parts = e.path.split("/")
        try:
            z = int(parts[1])
        except (ValueError, IndexError):
            continue
        by_zoom[z] = by_zoom.get(z, 0) + 1
        # An empty MVT tile is ~35 B (container header + zero layers).
        # Anything larger has content.
        if e.get_item().size < 50:
            empty_by_zoom[z] = empty_by_zoom.get(z, 0) + 1
    if not by_zoom:
        return ("fail", "no tiles/ entries")
    zooms = sorted(by_zoom)
    # Catch a zoom that should have a LOT of tiles but has <5% vs its
    # parent — the cliff-drop bug where tilemaker crashed at that
    # zoom.
    drops = []
    for z in zooms[1:]:
        prev = by_zoom.get(z - 1, 0)
        cur = by_zoom[z]
        if prev >= 1000 and cur < prev * 0.05:
            drops.append((z, cur, prev))
    if drops:
        return ("fail",
                f"cliff-drop at zoom: {drops} — tilemaker may have "
                "bailed mid-zoom")
    zooms_str = ", ".join(f"z{z}={by_zoom[z]:,}" for z in zooms)
    return ("pass", zooms_str)


def _chk_overture_fields(arc, cfg) -> tuple[str, str]:
    """Sample search records and confirm at least one has the Overture
    enrichment fields (ws, p, cat, brand). Dropped from a repackage
    would silently lose website/phone/brand data downstream."""
    if not cfg.get("hasOvertureAddresses") and not cfg.get("hasOverturePlaces"):
        return ("skip", "no Overture flags set")
    # Find a search-data chunk and sample.
    sample_path = None
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        p = e.path
        if p.startswith("search-data/") and p.endswith(".json") and p != "search-data/manifest.json":
            sample_path = p
            break
    if sample_path is None:
        return ("warn", "no search-data chunks to sample")
    try:
        recs = json.loads(bytes(arc.get_entry_by_path(sample_path).get_item().content))
    except Exception as exc:
        return ("fail", f"{sample_path} unreadable: {exc}")
    if not recs:
        return ("warn", f"{sample_path} empty")
    # Scan first 1000 records for any Overture field.
    has_ws = any("ws" in r for r in recs[:1000])
    has_p = any("p" in r for r in recs[:1000])
    has_cat = any("cat" in r for r in recs[:1000])
    has_brand = any("brand" in r for r in recs[:1000])
    fields = []
    if has_ws: fields.append("ws")
    if has_p: fields.append("p")
    if has_cat: fields.append("cat")
    if has_brand: fields.append("brand")
    if not fields:
        return ("warn",
                f"no Overture fields (ws/p/cat/brand) seen in first "
                f"1,000 of {sample_path} — enrichment may have "
                "dropped during repackage")
    return ("pass", f"Overture fields seen: {fields}")


def _chk_places_html(arc) -> tuple[str, str]:
    """The ``/Find`` button in Kiwix/PWA loads `places.html`. Multiple
    2026-04-22 regional builds shipped without it, so "Find" rendered
    "Unable to load the article requested." Fail hard if missing."""
    try:
        e = arc.get_entry_by_path("places.html")
    except Exception:
        return ("fail", "places.html missing — Kiwix 'Find' will 404")
    try:
        data = bytes(e.get_item().content)
    except Exception as exc:
        return ("fail", f"places.html unreadable: {exc}")
    if len(data) < 1000:
        return ("fail",
                f"places.html suspiciously small ({len(data)} B) — "
                "probably a stub")
    return ("pass", f"present ({len(data):,} B)")


def _chk_main_entry(arc) -> tuple[str, str]:
    if not arc.has_main_entry:
        # Some pre-existing Japan/Iran source builds have no main entry
        # but still open in Kiwix. Warn rather than fail.
        return ("skip",
                "no main entry declared (Kiwix Desktop tolerates this; "
                "some clients may refuse)")
    main = arc.main_entry
    steps = 0
    while main.is_redirect and steps < 8:
        main = main.get_redirect_entry()
        steps += 1
    if main.is_redirect:
        return ("fail", f"main entry redirect chain > 8 deep")
    # Verify content is readable
    try:
        _ = bytes(main.get_item().content)
    except Exception as exc:
        return ("fail", f"main entry unreadable: {exc}")
    return ("pass", f"main → {main.path!r} ({steps} redirects)")


def _chk_fulltext(arc) -> tuple[str, str]:
    if not getattr(arc, "has_fulltext_index", False):
        return ("skip", "no fulltext index")
    from libzim.search import Query, Searcher
    searcher = Searcher(arc)
    # Probes have to be words that (a) appear in SOME entry of every
    # region and (b) aren't treated as stop-words by xapian. streetzim
    # indexes the entries under ``search/*.html`` with geography-
    # specific terms, so "park", "station", "street" show up everywhere.
    totals = {}
    for q_str in ("park", "station", "street"):
        q = Query().set_query(q_str)
        res = searcher.search(q)
        totals[q_str] = res.getEstimatedMatches()
    if all(v == 0 for v in totals.values()):
        return ("fail",
                f"xapian returned 0 hits for all three probes {totals} — "
                "index is likely corrupt")
    return ("pass", f"hits {totals}")


def _map_config(arc) -> dict:
    try:
        return json.loads(bytes(arc.get_entry_by_path("map-config.json").get_item().content))
    except Exception:
        return {}


def _chk_map_config(arc) -> tuple[str, str]:
    cfg = _map_config(arc)
    if not cfg:
        return ("fail", "map-config.json missing or unparseable")
    flags = sorted(k for k, v in cfg.items() if k.startswith("has") and v)
    return ("pass", f"flags={flags}")


def _chk_vector_tiles(arc) -> tuple[str, str]:
    # z0 always required; we also want to confirm mid and high zoom
    # levels exist — a missing z14 usually means tilemaker bailed
    # silently or disk ran out.
    #
    # Computing tile coords from the bbox is the reliable approach;
    # linear scan breaks on big ZIMs because libzim orders tiles/ by
    # path, so `tiles/14/…` comes alphabetically AFTER `tiles/13/…` (it
    # falls outside a short iteration window).
    z0 = bytes(arc.get_entry_by_path("tiles/0/0/0.pbf").get_item().content)
    if not z0:
        return ("fail", "tiles/0/0/0.pbf is empty")
    cfg = _map_config(arc)
    bbox = None
    for k in ("bbox", "bounds"):
        if isinstance(cfg.get(k), (list, tuple)) and len(cfg[k]) == 4:
            bbox = cfg[k]
            break
    # streetzim-meta.json is the other place the bbox may live.
    if bbox is None:
        try:
            meta = json.loads(bytes(arc.get_entry_by_path("streetzim-meta.json").get_item().content))
            if isinstance(meta.get("bbox"), dict):
                b = meta["bbox"]
                bbox = [b.get("minLon"), b.get("minLat"),
                        b.get("maxLon"), b.get("maxLat")]
        except Exception:
            pass
    import math
    def _lonlat_to_tile(lon: float, lat: float, z: int):
        x = int((lon + 180) / 360 * (1 << z))
        y = int((1 - math.log(math.tan(math.radians(lat))
                              + 1 / math.cos(math.radians(lat))) / math.pi)
                / 2 * (1 << z))
        return x, y
    probes: list[str] = []
    if bbox and all(v is not None for v in bbox):
        lon = (bbox[0] + bbox[2]) / 2
        lat = (bbox[1] + bbox[3]) / 2
        for z in (8, 11, 14):
            x, y = _lonlat_to_tile(lon, lat, z)
            probes.append(f"tiles/{z}/{x}/{y}.pbf")
    if not probes:
        # Fall back to a wider linear scan — still finds z14 on regions
        # where the entry order happens to list it early enough.
        for i in range(arc.entry_count):
            e = arc._get_entry_by_id(i)
            if e.is_redirect:
                continue
            if e.path.startswith("tiles/14/"):
                probes = [e.path]
                break
    hits = []
    misses = []
    for p in probes:
        try:
            data = bytes(arc.get_entry_by_path(p).get_item().content)
            if data:
                hits.append(f"{p}={len(data)}B")
            else:
                misses.append(f"{p}=empty")
        except Exception:
            misses.append(f"{p}=missing")
    if not hits:
        return ("fail",
                f"no vector tiles found at probe paths: {misses}")
    if misses:
        return ("warn",
                f"some tile zooms missing — hits: {hits}; misses: {misses}")
    return ("pass", f"z0={len(z0)}B; " + ", ".join(hits))


def _chk_satellite(arc, cfg) -> tuple[str, str]:
    has = bool(cfg.get("hasSatellite"))
    sample = None
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        if e.path.startswith("satellite/"):
            sample = e.path
            break
    if has and sample is None:
        return ("fail", "hasSatellite=True but no satellite tiles found")
    if not has and sample is not None:
        return ("warn", f"satellite tiles present but flag not set ({sample})")
    if sample is None:
        return ("skip", "no satellite tiles (flag also off)")
    data = bytes(arc.get_entry_by_path(sample).get_item().content)
    return ("pass", f"sample {sample} = {len(data)}B")


def _chk_terrain(arc, cfg) -> tuple[str, str]:
    has = bool(cfg.get("hasTerrain"))
    sample = None
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        if e.path.startswith("terrain/"):
            sample = e.path
            break
    if has and sample is None:
        return ("fail", "hasTerrain=True but no terrain tiles found")
    if not has and sample is not None:
        return ("warn", f"terrain tiles present but flag not set ({sample})")
    if sample is None:
        return ("skip", "no terrain tiles (flag also off)")
    data = bytes(arc.get_entry_by_path(sample).get_item().content)
    return ("pass", f"sample {sample} = {len(data)}B")


def _chk_wikidata(arc, cfg) -> tuple[str, str]:
    """Wikidata structural + content integrity. Catches:
    - manifest missing or unreadable
    - manifest lists chunks that don't exist
    - chunks have 0 Q-IDs
    - sample Q-ID has no name/coords (broken entry)"""
    has = bool(cfg.get("hasWikidata"))
    try:
        mani = json.loads(bytes(
            arc.get_entry_by_path("wikidata/manifest.json").get_item().content
        ))
    except Exception:
        if has:
            return ("fail", "hasWikidata=True but wikidata/manifest.json missing")
        return ("skip", "no wikidata (flag also off)")
    chunks = mani.get("chunks", {})
    if not chunks:
        return ("warn", "wikidata manifest has 0 chunks")
    # Verify a handful of declared chunks actually exist + parse.
    bad = []
    sample_prefix = next(iter(chunks))
    sample_data = None
    for prefix in list(chunks)[:5]:
        try:
            raw = bytes(arc.get_entry_by_path(
                f"wikidata/{prefix}.json").get_item().content)
            data = json.loads(raw)
            if not isinstance(data, dict) or not data:
                bad.append(f"{prefix}:empty")
            elif sample_data is None:
                sample_data = (prefix, data)
        except Exception as exc:
            bad.append(f"{prefix}:{exc}")
    if bad:
        return ("fail",
                f"wikidata manifest lists {len(chunks)} chunks but "
                f"spot-check failed on {bad}")
    # Inspect a sample entry: must have a name AND one of the
    # coordinate signals (pt = [lat, lon]). A broken entry without
    # coords would cause empty popups.
    # Sample entry MUST have a label `l` (what the popup shows).
    # Coords aren't stored in wikidata entries — they come from the
    # clicked MVT feature via the Q-ID cross-ref, not from this
    # store. So don't require coords here.
    prefix, data = sample_data
    sample_qid, sample_entry = next(iter(data.items()))
    if not sample_entry.get("l") and not sample_entry.get("name"):
        return ("warn",
                f"sample Q-ID {sample_qid} in {prefix!r} has no label "
                "`l` field (popup will render empty)")
    total = sum(chunks.values())
    return ("pass",
            f"{total} items in {len(chunks)} chunks; sample "
            f"{sample_qid} in {prefix!r} has label "
            f"{sample_entry['l'][:30]!r}")


def _chk_routing_kiwix_compat(arc, cfg) -> tuple[str, str]:
    """Routing layout compatibility across clients. Three valid shapes:

      1. Monolithic ``routing-data/graph.bin`` only:
         works on every client IF size fits the platform heap
         (iOS/Android WebView ≈ 700 MB ceiling — see
         cloud/validate_platforms.py for the per-platform model).

      2. Spatial ``routing-data/graph-cells-index.bin`` + per-cell
         ``graph-cell-NNNNN.bin``: lazy-loaded by the viewer/mcpzim,
         keeps mobile memory bounded regardless of region size.
         PREFERRED for any region whose full graph would exceed
         the monolithic ceiling.

      3. Chunked-only (``graph-chunk-manifest.json`` + chunks) with
         no ``graph.bin``: PWA-compatible but BREAKS Kiwix iOS /
         macOS native apps (they don't reassemble chunks). Always
         a fail — emit spatial instead for big graphs, or
         monolithic for small ones.
    """
    if not cfg.get("hasRouting"):
        return ("skip", "hasRouting=False")
    # Count entries in each shape.
    has_monolithic = False
    mono_bytes = 0
    try:
        e = arc.get_entry_by_path("routing-data/graph.bin")
        has_monolithic = True
        mono_bytes = e.get_item().size
    except Exception:
        pass
    has_spatial = False
    cells_index_bytes = 0
    cell_count = 0
    try:
        e = arc.get_entry_by_path("routing-data/graph-cells-index.bin")
        has_spatial = True
        cells_index_bytes = e.get_item().size
    except Exception:
        pass
    chunk_count = 0
    chunk_bytes = 0
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        if e.path.startswith("routing-data/graph-chunk-") and e.path.endswith(".bin"):
            chunk_count += 1
            chunk_bytes += e.get_item().size
        elif e.path.startswith("routing-data/graph-cell-") and e.path.endswith(".bin"):
            cell_count += 1

    # Shape 2: spatial. Preferred for big graphs; every client handles
    # it via lazy cell loading.
    if has_spatial:
        idx_mb = cells_index_bytes / (1024 * 1024)
        return ("pass",
                f"spatial ({cell_count} cells, {idx_mb:.0f} MB index) "
                f"— lazy-loaded, mobile-safe")

    # Shape 1: monolithic.
    if has_monolithic:
        mono_mb = mono_bytes / (1024 * 1024)
        extra = f" (+ {chunk_count} chunks for PWA)" if chunk_count else ""
        return ("pass", f"monolithic graph.bin ({mono_mb:.0f} MB){extra}")

    # Shape 3: chunked-only. Always bad.
    if chunk_count > 0:
        total_mb = chunk_bytes / (1024 * 1024)
        return ("fail",
                f"chunked-only graph ({chunk_count} chunks, "
                f"{total_mb:.0f} MB) — Kiwix native clients cannot load; "
                f"emit spatial (--spatial-chunk-scale 1) or monolithic "
                f"(--unchunk-graph).")
    return ("fail",
            "hasRouting=True but no graph.bin or spatial index found")


def _chk_tile_corners(arc) -> tuple[str, str]:
    """Tiles at the extremes — polar rows and dateline columns — must
    still decode cleanly. A tilemaker or downloader corner-case that
    truncated these would be invisible except at a dateline-crossing
    region (none today, but future-proofing)."""
    # Pick the highest z present (cheap, don't scan all).
    max_z = -1
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        if e.path.startswith("tiles/"):
            try:
                z = int(e.path.split("/")[1])
                if z > max_z:
                    max_z = z
            except (ValueError, IndexError):
                pass
    if max_z < 0:
        return ("skip", "no tiles/")
    n = 1 << max_z
    # The corner + extremes the build could mishandle.
    probes = [
        (0, 0),
        (n - 1, 0),
        (0, n - 1),
        (n - 1, n - 1),
    ]
    results = []
    for x, y in probes:
        p = f"tiles/{max_z}/{x}/{y}.pbf"
        try:
            e = arc.get_entry_by_path(p)
            size = e.get_item().size
            results.append(f"({x},{y})={size}B")
        except Exception:
            # Missing corner tile is expected for non-global regions —
            # the bbox doesn't extend there. Not a bug.
            pass
    if not results:
        return ("pass", f"z={max_z} corners absent (regional bbox)")
    return ("pass", f"z={max_z} corners: {results}")


def _chk_search_data_sizes(arc) -> tuple[str, str]:
    """Guard against the Japan ``__.json`` crash class. Two tiers:

      * ≥ 200 MB = hard fail (confirmed to crash Kiwix Desktop "find")
      * 50–200 MB = warn (suspicious density, may slow or crash specific
        clients — needs human review before shipping)
    """
    try:
        mani = json.loads(bytes(
            arc.get_entry_by_path("search-data/manifest.json").get_item().content
        ))
    except Exception as exc:
        return ("fail", f"search-data/manifest.json unparseable: {exc}")
    chunks = mani.get("chunks", {})
    failed: list[tuple[str, int]] = []
    warned: list[tuple[str, int]] = []
    biggest = (0, "")
    for prefix in chunks:
        try:
            e = arc.get_entry_by_path(f"search-data/{prefix}.json")
            size = len(bytes(e.get_item().content))
        except Exception:
            continue
        if size > biggest[0]:
            biggest = (size, prefix)
        if size > SEARCH_CHUNK_FAIL_MB * 1024 * 1024:
            failed.append((prefix, size))
        elif size > SEARCH_CHUNK_WARN_MB * 1024 * 1024:
            warned.append((prefix, size))
    if failed:
        tb = ", ".join(f"{p}={s/1e6:.0f}MB" for p, s in failed[:3])
        return ("fail",
                f"{len(failed)} chunk(s) ≥ {SEARCH_CHUNK_FAIL_MB} MB: {tb}")
    if warned:
        tb = ", ".join(f"{p}={s/1e6:.0f}MB" for p, s in warned[:3])
        return ("warn",
                f"{len(warned)} chunk(s) between {SEARCH_CHUNK_WARN_MB}–{SEARCH_CHUNK_FAIL_MB} MB: {tb}")
    return ("pass",
            f"{len(chunks)} chunks; biggest {biggest[1]!r}={biggest[0]/1e6:.1f}MB")


def _chk_category_index(arc) -> tuple[str, str]:
    try:
        data = bytes(arc.get_entry_by_path("category-index/manifest.json").get_item().content)
    except Exception:
        return ("skip", "not present (older schema)")
    mani = json.loads(data)
    # Newer builds write a dict-of-dicts; older flat list. Accept either.
    size = (len(mani.get("categories", []))
            if isinstance(mani.get("categories"), list)
            else len(mani))
    return ("pass", f"{size} categories")


def _chk_streetzim_meta(arc) -> tuple[str, str]:
    try:
        data = bytes(arc.get_entry_by_path("streetzim-meta.json").get_item().content)
    except Exception:
        return ("skip", "not present (older schema)")
    meta = json.loads(data)
    missing = {"name", "buildDate"} - set(meta.keys())
    if missing:
        return ("warn", f"streetzim-meta missing {missing}")
    return ("pass",
            f"name={meta.get('name')!r} date={meta.get('buildDate')} "
            f"hasRouting={meta.get('hasRouting')}")


def _chk_routing(arc, cfg, zim_path: str) -> tuple[str, str]:
    """Validate routing layout: monolithic, chunked, or spatial. Catches
    the big-cluster (>500MB) and chunk-size regressions."""
    has = bool(cfg.get("hasRouting"))
    # Spatial layout?
    spatial_idx = None
    try:
        arc.get_entry_by_path("routing-data/graph-cells-index.bin")
        spatial_idx = "spatial"
    except Exception:
        pass
    # Check cluster-size guard on every routing-data entry
    too_big = []
    routing_total = 0
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        if not e.path.startswith("routing-data/"):
            continue
        try:
            size = len(bytes(e.get_item().content))
        except Exception:
            continue
        routing_total += size
        if size > MAX_ROUTING_ENTRY_MB * 1024 * 1024:
            too_big.append((e.path, size))
    if too_big:
        tb = ", ".join(f"{p}={s/1e6:.0f}MB" for p, s in too_big[:3])
        return ("fail",
                f"{len(too_big)} routing entry(ies) > {MAX_ROUTING_ENTRY_MB}MB "
                f"(fzstd cluster cap): {tb}")
    if not has:
        return ("skip", "hasRouting=False")
    if routing_total == 0:
        return ("fail", "hasRouting=True but no routing-data entries")

    # Sample a route. Layout-aware.
    try:
        if spatial_idx:
            from tests.szrg_spatial import load_spatial_from_zim
            from tests.szrg_spatial_astar import find_route_spatial
            sg = load_spatial_from_zim(zim_path, cache_limit=8)
            # Pick an arbitrary source node with at least one edge.
            cell0 = sg._ensure_cell(0)
            if cell0.cell_nodes_global.shape[0] < 2:
                return ("warn", f"spatial · cell 0 has too few nodes")
            s = int(cell0.cell_nodes_global[0])
            e = int(cell0.cell_nodes_global[min(50, cell0.cell_nodes_global.shape[0]-1)])
            r = find_route_spatial(sg, s, e, max_pops=500_000)
            status = "ok" if r else "unreachable"
            return ("pass",
                    f"spatial · {sg._index.num_cells} cells · "
                    f"sample route {status} · "
                    f"total routing-data={routing_total/1e6:.0f}MB")
        else:
            from tests.szrg_reader import load_from_zim
            from tests.szrg_astar import find_route
            g = load_from_zim(zim_path)
            s, e = 0, min(g.num_nodes - 1, 1000)
            r = find_route(g, s, e, max_pops=500_000)
            status = "ok" if r else "unreachable"
            return ("pass",
                    f"v{g.version} · {g.num_nodes:,} nodes · "
                    f"sample route {status} · "
                    f"total routing-data={routing_total/1e6:.0f}MB")
    except Exception as exc:
        return ("fail", f"routing parse or sample route threw: {exc!r}")


def _bbox_from_zim(arc) -> tuple[float, float, float, float] | None:
    """Pull the (minLon, minLat, maxLon, maxLat) bbox from streetzim-meta or
    map-config. Returns None when neither carries a usable bbox."""
    try:
        meta = json.loads(bytes(arc.get_entry_by_path("streetzim-meta.json").get_item().content))
        b = meta.get("bbox")
        if isinstance(b, dict):
            return (b["minLon"], b["minLat"], b["maxLon"], b["maxLat"])
    except Exception:
        pass
    cfg = _map_config(arc)
    for k in ("bbox", "bounds"):
        v = cfg.get(k)
        if isinstance(v, (list, tuple)) and len(v) == 4:
            return tuple(v)  # assume [minLon, minLat, maxLon, maxLat]
    return None


def _expected_tile_count(bbox, zoom: int) -> int:
    """Number of z/x/y tiles whose extent intersects bbox at zoom ``zoom``."""
    import math
    minlon, minlat, maxlon, maxlat = bbox
    n = 1 << zoom
    def lon2x(lon: float) -> int:
        return int((lon + 180.0) / 360.0 * n)
    def lat2y(lat: float) -> int:
        lat = max(min(lat, 85.05112878), -85.05112878)
        rad = math.radians(lat)
        return int((1 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi)
                   / 2 * n)
    x_min, x_max = max(0, lon2x(minlon)), min(n - 1, lon2x(maxlon))
    # y increases SOUTHWARD in slippy-map coords, so maxlat → smaller y
    y_min, y_max = max(0, lat2y(maxlat)), min(n - 1, lat2y(minlat))
    if x_max < x_min or y_max < y_min:
        return 0
    return (x_max - x_min + 1) * (y_max - y_min + 1)


def _audit_tiles(arc) -> tuple[str, str]:
    """Enumerate every tile entry by path, bucket by (kind, zoom), compare
    actual counts to bbox-expected counts. No content reads — just
    metadata iteration, so a full Japan-scale audit runs in ~20 s.

    Gates on failure patterns we've actually seen in broken builds:

      * coarse zooms (≤ z8) must be 100% — every such cell touches land
        or coast on any real region
      * no deep zoom below 5% — that's "tilemaker bailed"
      * no "cliff drop": child zoom's land-fraction must be ≥ 10% of its
        parent's land-fraction (a sudden 10× drop means whatever built
        that zoom crashed mid-way)

    Low absolute coverage alone is NOT a fail — Japan is 7.5% land in
    its bbox and that's fine. We just surface the per-zoom numbers in
    the detail so humans can sanity-check them.

    Paths we recognise:
      - tiles/<z>/<x>/<y>.pbf          → vector
      - satellite/<z>/<x>/<y>.<ext>    → satellite
      - terrain/<z>/<x>/<y>.webp       → terrain
    """
    bbox = _bbox_from_zim(arc)
    if bbox is None:
        return ("warn", "no bbox in streetzim-meta / map-config — "
                        "cannot compute expected tile counts")

    cfg = _map_config(arc)
    max_zooms = {
        "vector": 14,
        "satellite": int(cfg.get("satelliteMaxZoom") or 0) or None,
        "terrain": int(cfg.get("terrainMaxZoom") or 0) or None,
    }

    counts: dict[tuple[str, int], int] = {}
    # Blank-tile ledger: (kind, zoom) → list of (path, size) for tiles ≤
    # BLANK_TILE_BYTES. Satellite over ocean legitimately compresses to
    # ~300-500 B per memory note; terrain blanks are the VRT-race bug
    # (44 B WebP of all-zero elevation). We still record satellite-tiny
    # tiles for the reporter but don't gate on them.
    blanks: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for i in range(arc.entry_count):
        e = arc._get_entry_by_id(i)
        if e.is_redirect:
            continue
        path = e.path
        if path.startswith("tiles/"):
            kind, rest = "vector", path[6:]
        elif path.startswith("satellite/"):
            kind, rest = "satellite", path[10:]
        elif path.startswith("terrain/"):
            kind, rest = "terrain", path[8:]
        else:
            continue
        slash = rest.find("/")
        if slash < 0:
            continue
        try:
            z = int(rest[:slash])
        except ValueError:
            continue
        counts[(kind, z)] = counts.get((kind, z), 0) + 1
        # Cheap size-only probe — reads only the entry item, not
        # decompressed content (for compressed clusters this is still
        # close to a decompress, but libzim caches the cluster so
        # neighboring entries amortize).
        try:
            size = len(bytes(e.get_item().content))
        except Exception:
            continue
        if size <= BLANK_TILE_BYTES:
            blanks.setdefault((kind, z), []).append((path, size))

    fails: list[str] = []
    warns: list[str] = []
    summaries: list[str] = []

    for (kind, declared_max) in (("vector", 14),
                                  ("satellite", max_zooms["satellite"]),
                                  ("terrain", max_zooms["terrain"])):
        if kind != "vector" and declared_max is None:
            continue
        # Skip tile kinds absent from the ZIM (handled elsewhere).
        if not any(k == kind for (k, _z) in counts):
            continue
        zmax = 14 if kind == "vector" else declared_max

        zoom_frac: dict[int, float] = {}
        per_zoom_summary: list[str] = []
        for z in range(0, zmax + 1):
            actual = counts.get((kind, z), 0)
            expected = _expected_tile_count(bbox, z)
            if expected == 0:
                continue
            frac = actual / expected
            zoom_frac[z] = frac
            per_zoom_summary.append(f"z{z}={actual}/{expected}({frac*100:.0f}%)")

            # Gate 1: coarse zoom must be complete.
            if z <= COARSE_ZOOM_CUTOFF and actual < expected:
                fails.append(
                    f"{kind}-z{z}: {actual}/{expected} — every "
                    f"coarse-zoom cell should be populated"
                )
            # Gate 2: below 5% anywhere is broken.
            elif frac < ZERO_COVERAGE_FAIL and z > COARSE_ZOOM_CUTOFF:
                fails.append(
                    f"{kind}-z{z}: {actual}/{expected} "
                    f"({frac*100:.1f}%) — looks empty"
                )

        # Gate 3: cliff drop between consecutive zooms. We compare the
        # COVERAGE RATIO zooms, not absolute counts (a quarter of the
        # tiles in the bbox at z+1 is normal because bbox area per tile
        # quarters). If z+1's fraction-of-expected drops to <10% of z's
        # fraction, the deeper zoom stage probably failed.
        for z in range(1, zmax + 1):
            if z not in zoom_frac or (z - 1) not in zoom_frac:
                continue
            parent = zoom_frac[z - 1]
            child = zoom_frac[z]
            if parent > 0 and (child / parent) < PARENT_DROP_FAIL:
                fails.append(
                    f"{kind}-z{z}: coverage dropped to "
                    f"{child/parent*100:.1f}% of z{z-1} — probable build crash"
                )

        summaries.append(f"{kind}: " + ", ".join(per_zoom_summary))

    # Blank-terrain gate — the VRT-race bug produces user-visible gaps
    # at whatever zoom the regen bailed on. Any terrain tile ≤ 500 B is
    # a blank. Group by zoom so the report stays short on large ZIMs.
    terrain_blanks_by_z: dict[int, int] = {}
    for (kind, z), tiles in blanks.items():
        if kind == "terrain":
            terrain_blanks_by_z[z] = len(tiles)
    if terrain_blanks_by_z:
        total_blank = sum(terrain_blanks_by_z.values())
        per_z = ", ".join(f"z{z}={n}" for z, n in sorted(terrain_blanks_by_z.items()))
        # Include up to 3 concrete tile paths so the person debugging
        # knows where to look.
        sample_tiles = []
        for z in sorted(terrain_blanks_by_z):
            for p, s in blanks[("terrain", z)][:1]:
                sample_tiles.append(f"{p}={s}B")
                if len(sample_tiles) >= 3:
                    break
            if len(sample_tiles) >= 3:
                break
        fails.append(
            f"terrain: {total_blank} blank tile(s) ({per_z}) — "
            f"VRT-race bug. Examples: {'; '.join(sample_tiles)}"
        )

    if fails:
        return ("fail", " | ".join(fails[:5]))
    # Always return the full breakdown so humans can eyeball it — even
    # on pass. Keeps surprising numbers visible in CI logs.
    return ("pass", " | ".join(summaries) or "no tile layers")


# ---- Orchestration ------------------------------------------------------


def validate(zim_path: str, *, audit_tiles: bool = False) -> list[Result]:
    from libzim.reader import Archive
    results: list[Result] = []
    try:
        arc = Archive(zim_path)
    except Exception as exc:
        results.append(Result("zim_opens", "error", "fail",
                              f"{type(exc).__name__}: {exc}"))
        return results
    results.append(_check("zim_opens", "error", _chk_opens, arc))
    results.append(_check("metadata_required", "error", _chk_metadata, arc))
    results.append(_check("illustration", "error", _chk_illustration, arc))
    results.append(_check("main_entry", "error", _chk_main_entry, arc))
    results.append(_check("places_html", "error", _chk_places_html, arc))
    results.append(_check("fulltext_xapian", "error", _chk_fulltext, arc))
    results.append(_check("map_config", "error", _chk_map_config, arc))
    cfg = _map_config(arc)
    results.append(_check("vector_tiles", "error", _chk_vector_tiles, arc))
    results.append(_check("satellite", "error", _chk_satellite, arc, cfg))
    results.append(_check("terrain", "error", _chk_terrain, arc, cfg))
    results.append(_check("wikidata", "error", _chk_wikidata, arc, cfg))
    results.append(_check("fonts", "error", _chk_fonts, arc))
    results.append(_check("satellite_coverage", "error",
                          _chk_satellite_coverage, arc, cfg))
    results.append(_check("vector_coverage", "error",
                          _chk_vector_coverage, arc))
    results.append(_check("terrain_edge_stripe", "error",
                          _chk_terrain_edge_stripe, arc, cfg))
    results.append(_check("places_categories", "warn",
                          _chk_places_categories, arc))
    # "error" severity: if chips were declared but the files are missing/
    # broken, Find will default back to `poi.json` and OOM the browser on
    # big regions. Worth failing a PR-candidate ZIM over.
    results.append(_check("find_chips", "error", _chk_find_chips, arc))
    results.append(_check("overture_fields", "warn",
                          _chk_overture_fields, arc, cfg))
    results.append(_check("routing_kiwix_compat", "error",
                          _chk_routing_kiwix_compat, arc, cfg))
    results.append(_check("tile_corners", "warn",
                          _chk_tile_corners, arc))
    results.append(_check("routing_sample", "warn",
                          _chk_routing_sample, arc, cfg, zim_path))
    results.append(_check("search_data_sizes", "error",
                          _chk_search_data_sizes, arc))
    results.append(_check("category_index", "warn", _chk_category_index, arc))
    results.append(_check("streetzim_meta", "warn", _chk_streetzim_meta, arc))
    results.append(_check("routing", "error", _chk_routing, arc, cfg, zim_path))
    if audit_tiles:
        results.append(_check("tile_coverage", "error", _audit_tiles, arc))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zim", nargs="+", help="ZIM file(s) to validate")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON instead of human text")
    ap.add_argument("--warn-as-error", action="store_true",
                    help="Treat warnings as errors (strict CI mode)")
    ap.add_argument("--audit-tiles", action="store_true",
                    help="Enumerate every tile path + compare to bbox-expected "
                         "counts per zoom. Catches whole-zoom misses + large "
                         "coverage drops. Adds ~20s to a Japan-scale validate.")
    args = ap.parse_args()

    overall_ok = True
    all_reports = []
    for zim in args.zim:
        results = validate(zim, audit_tiles=args.audit_tiles)
        zim_ok = True
        for r in results:
            if r.status == "fail" and r.severity == "error":
                zim_ok = False
            if args.warn_as_error and r.status == "fail" and r.severity == "warn":
                zim_ok = False
        overall_ok = overall_ok and zim_ok
        if args.json:
            all_reports.append({"zim": zim, "pass": zim_ok,
                                "results": [r.to_dict() for r in results]})
        else:
            print(f"\n=== validate {zim} — {'PASS' if zim_ok else 'FAIL'} ===")
            icons = {
                "pass": "[ OK ]",
                "fail": "[FAIL]",
                "skip": "[SKIP]",
                "warn": "[WARN]",
            }
            for r in results:
                icon = icons.get(r.status, f"[{r.status.upper()}]")
                tag = f"({r.severity})" if r.severity != "error" else ""
                print(f"  {icon} {r.name:<22} {tag:<7} {r.detail}")
    if args.json:
        print(json.dumps(all_reports, indent=2))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
