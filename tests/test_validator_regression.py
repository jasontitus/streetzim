"""Regression tests for cloud/validate_zim.py.

One test per historical failure class we've ever shipped broken. Each
test asserts the validator FAILS on a known-broken artifact (either a
local ZIM from the workspace or a synthesised in-memory one). If a
future change to the validator stops detecting one of these, that test
goes red before anything lands.

**The contract with this file:**

  When a new failure class is discovered in the wild (Kiwix crash,
  rendered gap, etc.), the first step is to:

  1. Write a failing test here that produces (or points at) the broken
     artifact and asserts validator flags it.
  2. Only THEN update validate_zim.py to make the test pass.

  This inverts the order we got wrong on the blank-terrain-tile bug:
  we had a memory note about the failure mode, we built a validator,
  but never mechanically checked the validator against the known
  pattern. The test file is the mechanical check.

Each test references the source of truth (a memory note, a commit SHA,
or an incident description) so the next person knows WHY the gate is
there.
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud import validate_zim


# -----------------------------------------------------------------------
# Helpers for synthesising broken ZIMs.
# -----------------------------------------------------------------------


def _make_minimal_zim(tmp_path: Path,
                      filename: str = "test.zim",
                      extra_items: list | None = None,
                      metadata_overrides: dict | None = None) -> Path:
    """Build a minimally-valid-looking streetzim ZIM so we can layer a
    single broken behaviour on top and assert the validator catches it
    specifically. libzim won't let us write a totally empty ZIM — we
    need at least a main page.
    """
    try:
        from libzim.writer import Creator, Item, StringProvider, Hint
    except ImportError:
        pytest.skip("libzim not installed")

    class _It(Item):
        def __init__(self, path, mime, data, compress=True):
            super().__init__()
            self._p = path
            self._m = mime
            self._d = data
            self._compress = compress
        def get_path(self): return self._p
        def get_title(self): return self._p
        def get_mimetype(self): return self._m
        def get_contentprovider(self): return StringProvider(self._d)
        def get_hints(self):
            return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: self._compress}

    zim_path = tmp_path / filename
    c = Creator(str(zim_path))
    with c as cc:
        meta = {
            "Title": "regression test",
            "Description": "regression test",
            "Language": "en",
            "Creator": "test",
            "Publisher": "test",
            "Date": "2026-04-23",
            "Name": "test",
        }
        meta.update(metadata_overrides or {})
        for k, v in meta.items():
            cc.add_metadata(k, v)
        # Minimum viable illustration — 48x48 PNG header, enough bytes to
        # satisfy `has_illustration()`.
        png_stub = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 40
        cc.add_metadata("Illustration_48x48@1", png_stub)
        cc.add_item(_It("index.html", "text/html", b"<html></html>"))
        cc.set_mainpath("index.html")
        # Minimal map-config so the validator doesn't short-circuit on
        # missing config.
        cc.add_item(_It("map-config.json", "application/json",
                        json.dumps({"bbox": [0, 0, 1, 1]}).encode()))
        # Minimal vector tile at z0 so vector_tiles check passes.
        cc.add_item(_It("tiles/0/0/0.pbf", "application/x-protobuf",
                        b"\x00" * 1024))
        # Minimal search-data so those checks don't false-positive on
        # "everything is missing". We keep the manifest empty of chunks
        # unless the caller says otherwise.
        cc.add_item(_It("search-data/manifest.json", "application/json",
                        json.dumps({"chunks": {}}).encode()))
        for item in (extra_items or []):
            cc.add_item(item)
    return zim_path


def _mk_item(path, mime, data, compress=True):
    from libzim.writer import Item, StringProvider, Hint
    class _It(Item):
        def __init__(self, p, m, d, c):
            super().__init__()
            self._p, self._m, self._d, self._c = p, m, d, c
        def get_path(self): return self._p
        def get_title(self): return self._p
        def get_mimetype(self): return self._m
        def get_contentprovider(self): return StringProvider(self._d)
        def get_hints(self):
            return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: self._c}
    return _It(path, mime, data, compress)


def _run_validator(zim_path: Path, audit_tiles: bool = False) -> list:
    """Call validate() and return the list of Result records."""
    return validate_zim.validate(str(zim_path), audit_tiles=audit_tiles)


def _find(results, name: str):
    for r in results:
        if r.name == name:
            return r
    return None


# -----------------------------------------------------------------------
# Bug 1: 350 MB __.json chunk crashes Kiwix Desktop on "find".
# Source: user-visible Kiwix crash on Japan spatial + source v4 (2026-04-23).
# Memory note: none — discovered this session.
# Fix commit: prefix_key non-ASCII bucketing; writer + viewer + Swift.
# -----------------------------------------------------------------------


def test_oversized_search_chunk_is_caught(tmp_path: Path):
    """A single search-data chunk ≥ 200 MB must hard-fail the validator."""
    # Allocate a 201 MB blob — above FAIL threshold, below 'obviously
    # OOM' ceiling for a test process.
    big = b'["dummy"]' + b"x" * (201 * 1024 * 1024 - 10)
    zim = _make_minimal_zim(
        tmp_path,
        filename="oversized_search.zim",
        extra_items=[
            _mk_item("search-data/__.json", "application/json", big),
        ],
    )
    # Override the manifest so the validator notices this chunk.
    # Easiest: regenerate the whole ZIM with both entries present.
    # Re-create from scratch including the manifest pointing at __.
    from libzim.writer import Creator
    zim2 = tmp_path / "oversized_search2.zim"
    c = Creator(str(zim2))
    with c as cc:
        for k, v in (("Title", "t"), ("Description", "d"),
                      ("Language", "en"), ("Creator", "x"),
                      ("Publisher", "x"), ("Date", "2026-04-23"),
                      ("Name", "n")):
            cc.add_metadata(k, v)
        cc.add_metadata("Illustration_48x48@1",
                        bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 40)
        cc.add_item(_mk_item("index.html", "text/html", b"<html></html>"))
        cc.set_mainpath("index.html")
        cc.add_item(_mk_item("map-config.json", "application/json",
                             json.dumps({"bbox": [0, 0, 1, 1]}).encode()))
        cc.add_item(_mk_item("tiles/0/0/0.pbf", "application/x-protobuf",
                             b"\x00" * 1024))
        cc.add_item(_mk_item("search-data/manifest.json", "application/json",
                             json.dumps({"chunks": {"__": 1}}).encode()))
        cc.add_item(_mk_item("search-data/__.json", "application/json", big))

    results = _run_validator(zim2)
    r = _find(results, "search_data_sizes")
    assert r is not None, "expected a search_data_sizes check in the report"
    assert r.status == "fail", (
        f"oversized chunk must hard-fail; got status={r.status} "
        f"detail={r.detail!r}"
    )


# -----------------------------------------------------------------------
# Bug 2: VRT-race blank terrain tiles (44 B WebP).
# Source: Iran 2026-04-23 had 9,433 blank tiles; user saw horizontal
#         missing line.
# Memory note: project_terrain_blank_tile_bug.md
# -----------------------------------------------------------------------


def test_blank_terrain_tile_is_caught(tmp_path: Path):
    """Any terrain tile ≤ 500 B must hard-fail the validator.

    We build a ZIM with enough vector coverage that coarse-zoom checks
    pass, then inject a single 44-byte terrain tile at a zoom above the
    COARSE_ZOOM_CUTOFF — that isolates the blank-tile gate.
    """
    import math
    from libzim.writer import Creator
    zim = tmp_path / "blank_terrain.zim"
    c = Creator(str(zim))
    # A bbox small enough that every zoom covers exactly 1 tile. Stay
    # away from tile boundaries (the prime meridian, the equator) so we
    # don't land in the 2-tile intersection case.
    bbox = [10.0, 10.0, 10.001, 10.001]

    def _ll_to_tile(lon, lat, z):
        n = 1 << z
        x = int((lon + 180) / 360 * n)
        rad = math.radians(lat)
        y = int((1 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi)
                / 2 * n)
        return x, y
    with c as cc:
        for k, v in (("Title", "t"), ("Description", "d"),
                      ("Language", "en"), ("Creator", "x"),
                      ("Publisher", "x"), ("Date", "2026-04-23"),
                      ("Name", "n")):
            cc.add_metadata(k, v)
        cc.add_metadata("Illustration_48x48@1",
                        bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 40)
        cc.add_item(_mk_item("index.html", "text/html", b"<html></html>"))
        cc.set_mainpath("index.html")
        cc.add_item(_mk_item(
            "map-config.json", "application/json",
            json.dumps({"bbox": bbox,
                        "hasTerrain": True, "terrainMaxZoom": 11}).encode()))
        # Satisfy vector + terrain coarse-zoom requirements at every
        # zoom by emitting the exact tile the bbox picker wants.
        for z in range(0, 12):
            x, y = _ll_to_tile(10.0005, 10.0005, z)
            cc.add_item(_mk_item(f"tiles/{z}/{x}/{y}.pbf",
                                 "application/x-protobuf",
                                 b"\x00" * 1024))
            cc.add_item(_mk_item(f"terrain/{z}/{x}/{y}.webp", "image/webp",
                                 b"\x00" * 2048))
        # Inject a 44-byte blank at an off-bbox z11 tile — above the
        # coarse cutoff, so the blank gate is the only failure.
        cc.add_item(_mk_item("terrain/11/99/99.webp", "image/webp",
                             b"x" * 44))
        cc.add_item(_mk_item("search-data/manifest.json", "application/json",
                             json.dumps({"chunks": {}}).encode()))

    results = _run_validator(zim, audit_tiles=True)
    r = _find(results, "tile_coverage")
    assert r is not None, "tile_coverage check should run with --audit-tiles"
    assert r.status == "fail", (
        f"blank terrain tile must hard-fail; got status={r.status} "
        f"detail={r.detail!r}"
    )
    assert "blank" in r.detail.lower() or "vrt-race" in r.detail.lower(), (
        f"failure detail should name the pattern; got {r.detail!r}"
    )


# -----------------------------------------------------------------------
# Bug 3: missing main entry — Kiwix Desktop refuses to open.
# Source: Japan spatial 2026-04-23 first attempt; see repackage_zim
#         main-path fix in this session.
# -----------------------------------------------------------------------


def test_missing_main_entry_is_caught(tmp_path: Path):
    """A ZIM with no declared main entry should at least surface a
    SKIP/WARN — the main_entry check must not silently pass.

    Note: the validator currently SKIPs rather than FAILs because some
    legacy source ZIMs (Japan v4) have no main entry and still open in
    Kiwix. If we ever tighten that to a hard-fail, update this test.
    """
    from libzim.writer import Creator
    zim = tmp_path / "no_main.zim"
    c = Creator(str(zim))
    with c as cc:
        for k, v in (("Title", "t"), ("Description", "d"),
                      ("Language", "en"), ("Creator", "x"),
                      ("Publisher", "x"), ("Date", "2026-04-23"),
                      ("Name", "n")):
            cc.add_metadata(k, v)
        cc.add_metadata("Illustration_48x48@1",
                        bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 40)
        cc.add_item(_mk_item("index.html", "text/html", b"<html></html>"))
        # DELIBERATELY omit set_mainpath.
        cc.add_item(_mk_item("map-config.json", "application/json",
                             json.dumps({"bbox": [0, 0, 1, 1]}).encode()))
        cc.add_item(_mk_item("tiles/0/0/0.pbf", "application/x-protobuf",
                             b"\x00" * 1024))
        cc.add_item(_mk_item("search-data/manifest.json", "application/json",
                             json.dumps({"chunks": {}}).encode()))
    results = _run_validator(zim)
    r = _find(results, "main_entry")
    assert r is not None
    assert r.status in ("fail", "skip"), (
        "main_entry missing should at least be flagged (skip or fail); "
        f"got status={r.status!r} detail={r.detail!r}"
    )


# -----------------------------------------------------------------------
# Bug 4: routing entry > 500 MB (fzstd per-cluster ceiling on PWA).
# Source: Iran 2026-04-23 post-fix had 545 MB monolithic graph.bin.
# Memory note: project_routing_optimization.md (fzstd ~500 MB limit).
# -----------------------------------------------------------------------


def test_oversized_routing_entry_is_caught(tmp_path: Path):
    """A single routing-data entry ≥ 500 MB must hard-fail the
    validator (the PWA's fzstd port chokes on a single cluster that
    big)."""
    from libzim.writer import Creator
    zim = tmp_path / "big_routing.zim"
    c = Creator(str(zim))
    with c as cc:
        for k, v in (("Title", "t"), ("Description", "d"),
                      ("Language", "en"), ("Creator", "x"),
                      ("Publisher", "x"), ("Date", "2026-04-23"),
                      ("Name", "n")):
            cc.add_metadata(k, v)
        cc.add_metadata("Illustration_48x48@1",
                        bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 40)
        cc.add_item(_mk_item("index.html", "text/html", b"<html></html>"))
        cc.set_mainpath("index.html")
        cc.add_item(_mk_item(
            "map-config.json", "application/json",
            json.dumps({"bbox": [0, 0, 1, 1], "hasRouting": True}).encode()))
        cc.add_item(_mk_item("tiles/0/0/0.pbf", "application/x-protobuf",
                             b"\x00" * 1024))
        cc.add_item(_mk_item("search-data/manifest.json", "application/json",
                             json.dumps({"chunks": {}}).encode()))
        # 501 MB routing graph — one byte over the ceiling.
        cc.add_item(_mk_item(
            "routing-data/graph.bin", "application/octet-stream",
            b"x" * (501 * 1024 * 1024),
            compress=False,
        ))
    results = _run_validator(zim)
    r = _find(results, "routing")
    assert r is not None
    assert r.status == "fail", (
        f"501 MB routing entry must hard-fail; got status={r.status} "
        f"detail={r.detail!r}"
    )
    assert "MB" in r.detail and "fzstd" in r.detail.lower(), (
        f"failure detail should reference fzstd cap; got {r.detail!r}"
    )


# -----------------------------------------------------------------------
# Bug 5: chunked-manifest size mismatch (torn upload).
# Source: my own _fetch_chunked_blob tests caught this class; the
#         validator doesn't yet — we rely on the reader. Confirm the
#         reader's rejection propagates into a validator failure when
#         a chunk's manifest size doesn't match the payload.
# -----------------------------------------------------------------------


def test_torn_chunk_manifest_is_caught(tmp_path: Path):
    """A chunked-graph ZIM whose chunk size doesn't match the manifest
    must fail the routing probe (reader refuses to reassemble)."""
    from libzim.writer import Creator
    zim = tmp_path / "torn_chunk.zim"
    c = Creator(str(zim))
    with c as cc:
        for k, v in (("Title", "t"), ("Description", "d"),
                      ("Language", "en"), ("Creator", "x"),
                      ("Publisher", "x"), ("Date", "2026-04-23"),
                      ("Name", "n")):
            cc.add_metadata(k, v)
        cc.add_metadata("Illustration_48x48@1",
                        bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 40)
        cc.add_item(_mk_item("index.html", "text/html", b"<html></html>"))
        cc.set_mainpath("index.html")
        cc.add_item(_mk_item(
            "map-config.json", "application/json",
            json.dumps({"bbox": [0, 0, 1, 1], "hasRouting": True}).encode()))
        cc.add_item(_mk_item("tiles/0/0/0.pbf", "application/x-protobuf",
                             b"\x00" * 1024))
        cc.add_item(_mk_item("search-data/manifest.json", "application/json",
                             json.dumps({"chunks": {}}).encode()))
        # Manifest claims the chunk is 1000 B; payload is 100 B. Reader
        # rejects loudly.
        manifest = {
            "schema": 1,
            "total_bytes": 1000,
            "sha256": "0" * 64,
            "chunks": [{"path": "graph-chunk-0000.bin", "bytes": 1000}],
        }
        cc.add_item(_mk_item(
            "routing-data/graph-chunk-manifest.json", "application/json",
            json.dumps(manifest).encode()))
        cc.add_item(_mk_item(
            "routing-data/graph-chunk-0000.bin", "application/octet-stream",
            b"x" * 100))
    results = _run_validator(zim)
    r = _find(results, "routing")
    assert r is not None
    assert r.status == "fail", (
        f"torn chunk manifest must fail routing probe; got {r.status} "
        f"detail={r.detail!r}"
    )


# -----------------------------------------------------------------------
# Bug 6: negative control — validator's structural checks pass on a real
# ZIM (excluding already-known blank-tile and oversized-chunk bugs that
# are being fixed on a separate track). This catches false positives
# from new gates landing too aggressively.
# -----------------------------------------------------------------------


def test_known_good_sv_structural_checks_pass():
    """Structural checks (not tile content) should pass on the shipped
    SV ZIM. Tile-content blanks are tracked separately in
    ``test_all_shipped_regions_flag_known_blanks``.
    """
    zim = ROOT / "osm-silicon-valley-2026-04-22.zim"
    if not zim.is_file():
        pytest.skip(f"{zim.name} not present locally — nothing to control-test")
    # Audit-tiles off: this test covers only format/structure regressions.
    results = _run_validator(zim, audit_tiles=False)
    failed = [r for r in results
              if r.status == "fail" and r.severity == "error"]
    assert not failed, (
        "known-good SV ZIM fails structural validation: "
        + "\n".join(f"{r.name}: {r.detail}" for r in failed)
    )
