"""Regression tests for ``cloud/upgrade_spatial_zim.py``.

The upgrader was at one point dropping the source's libzim Xapian fulltext
index because it iterated only ``src.entry_count`` (user namespace) and
filtered out everything past it (the ``X/fulltext/xapian`` entry plus
metadata). The fix is to enable libzim's native indexing on the upgrader's
Creator (``config_indexing(True, lang)``) so libzim re-emits ``fulltext/xapian``
from the entries we add. These tests pin that behavior:

  * ``test_upgrade_preserves_fulltext_index`` — a tiny v1 spatial ZIM with
    indexable HTML entries, after upgrade, ``has_fulltext_index`` is True
    and a Xapian search returns the expected hits.

  * ``test_upgrade_preserves_szci_v2_and_search_chunks`` — sanity check
    on the rest of the upgrade contract (SZCI v1→v2, search-data
    pass-through), so a future "fix" that breaks fulltext won't sneak by
    just because the routing surface still works.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.szrg_reader import parse_szrg_bytes  # noqa: E402
from tests.szrg_spatial import (  # noqa: E402
    SZCI_MAGIC, SZCI_VERSION_INLINE, SZRC_MAGIC,
    build_spatial, parse_szci, parse_szrc,
)
from tests.test_spatial_chunking import _pack_v4_graph  # noqa: E402


def _build_v1_spatial_zim(zim_path: Path, *, with_fulltext: bool) -> dict:
    """Build a tiny v1 spatial ZIM that mirrors a real StreetZim's layout
    enough for the upgrader to succeed:

      * index.html (mainPath) + a few text/html entries with distinctive
        body text so Xapian has something to index (only meaningful when
        ``with_fulltext`` is True — otherwise we're checking that the
        upgrader still works on a source that never had the index either).
      * search-data/manifest.json + one search-data/<prefix>.json leaf
      * routing-data/graph-cells-index.bin (SZCI v1, inline nodes)
      * routing-data/graph-cell-NNNNN.bin per non-empty cell

    Returns a dict describing the indexed corpus so the test can pick a
    query word that's known to be unique to one entry.
    """
    try:
        from libzim.writer import (
            Creator, Item, StringProvider, Hint,
        )
    except ImportError:
        pytest.skip("libzim not installed")

    # Tiny 4-node ring graph — same shape used in test_spatial_chunking.
    nodes = [
        (        0,         0),
        (2_000_000,         0),
        (        0, 2_000_000),
        (2_000_000, 2_000_000),
    ]
    edges = [
        (0, 1, 1000, 30, 0xFFFFFFFF, 0),
        (1, 3, 1000, 30, 0xFFFFFFFF, 0),
        (3, 2, 1000, 30, 0xFFFFFFFF, 0),
        (2, 0, 1000, 30, 0xFFFFFFFF, 0),
    ]
    g = parse_szrg_bytes(_pack_v4_graph(nodes, edges))
    idx_bytes_v3, cells_v3, _meta = build_spatial(g, cell_scale=10)
    idx_v3 = parse_szci(idx_bytes_v3)

    # Production builds now emit SZCI v3. This fixture intentionally
    # reconstructs the legacy v1 representation so the standalone
    # v1→v2 upgrader remains covered.
    nodes_blob = bytearray()
    cells_v1 = {}
    cell_meta = bytearray()
    for cid in range(idx_v3.num_cells):
        base = int(idx_v3.cell_base_node[cid])
        cell = parse_szrc(cells_v3[cid], base_node=base)
        nodes_blob.extend(cell.nodes_scaled.tobytes())
        cell_meta.extend(struct.pack(
            "<iiIII",
            int(idx_v3.cell_lat_idx[cid]),
            int(idx_v3.cell_lon_idx[cid]),
            cell.node_count,
            int(idx_v3.cell_edge_count[cid]),
            int(idx_v3.cell_geom_count[cid]),
        ))
        # SZRC v1 stores explicit global IDs where v2 stores coordinates.
        old_tail = cells_v3[cid][28 + cell.node_count * 8:]
        old_nodes = struct.pack(
            f"<{cell.node_count}I",
            *range(base, base + cell.node_count),
        )
        cells_v1[cid] = (
            SZRC_MAGIC
            + struct.pack("<6I", 1, cid, cell.node_count,
                          int(idx_v3.cell_edge_count[cid]),
                          int(idx_v3.cell_geom_count[cid]),
                          len(cell.geom_blob))
            + old_nodes + old_tail
        )
    idx_bytes_v1 = (
        SZCI_MAGIC
        + struct.pack("<7I", SZCI_VERSION_INLINE, idx_v3.num_nodes,
                      idx_v3.num_edges, idx_v3.num_names,
                      len(idx_v3.names_blob), idx_v3.num_cells,
                      idx_v3.cell_scale)
        + bytes(nodes_blob) + bytes(cell_meta)
        + idx_v3.name_offsets.tobytes() + idx_v3.names_blob
    )

    # --- Construct ZIM ----------------------------------------------------
    class _Item(Item):
        def __init__(self, path, mime, data, *, title=None):
            super().__init__()
            self._p = path
            self._m = mime
            self._d = data
            self._t = title or path
        def get_path(self):           return self._p
        def get_title(self):          return self._t
        def get_mimetype(self):       return self._m
        def get_contentprovider(self): return StringProvider(self._d)
        def get_hints(self):
            return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: True}

    # Use distinctive words so a Xapian search lands on exactly one doc.
    corpus = {
        "page-vandalism.html":   "vandalism is the keyword for this page",
        "page-quagmire.html":    "quagmire shows up only here",
        "page-flummery.html":    "flummery has a unique word too",
    }

    creator = Creator(str(zim_path))
    if with_fulltext:
        creator.config_indexing(True, "eng")
    with creator as c:
        for k, v in [
            ("Title", "test"), ("Description", "test"), ("Language", "eng"),
            ("Creator", "test"), ("Publisher", "test"), ("Date", "2026-05-04"),
            ("Name", "test"), ("Tags", ""),
        ]:
            c.add_metadata(k, v)
        c.add_item(_Item("index.html", "text/html", b"<html><body>main</body></html>"))
        c.set_mainpath("index.html")
        for path, body in corpus.items():
            c.add_item(_Item(
                path, "text/html",
                f"<html><body><h1>{path}</h1><p>{body}</p></body></html>"
                .encode("utf-8"),
                title=path,
            ))
        # Search manifest + one tiny leaf so the upgrader's "scan
        # search-data" phase has something to enumerate.
        manifest = {"chunks": {"aa": 1}, "sub_chunks": {}}
        c.add_item(_Item(
            "search-data/manifest.json", "application/json",
            json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
        ))
        c.add_item(_Item(
            "search-data/aa.json", "application/json",
            json.dumps([{"n": "Aaron", "id": 1}], separators=(",", ":")).encode("utf-8"),
        ))
        # SZCI v1 (inline nodes_scaled) + cell files.
        c.add_item(_Item(
            "routing-data/graph-cells-index.bin",
            "application/octet-stream",
            idx_bytes_v1,
        ))
        for cell_id, cell_bytes in cells_v1.items():
            c.add_item(_Item(
                f"routing-data/graph-cell-{cell_id:05d}.bin",
                "application/octet-stream",
                cell_bytes,
            ))
    return {"corpus": corpus}


def test_upgrade_preserves_fulltext_index(tmp_path):
    """Source has fulltext, upgrader must produce a ZIM that ALSO has
    fulltext — both the ``has_fulltext_index`` flag *and* a working
    Xapian search."""
    try:
        from libzim.reader import Archive
        from libzim.search import Searcher, Query
    except ImportError:
        pytest.skip("libzim not installed")

    from cloud.upgrade_spatial_zim import upgrade

    src = tmp_path / "src.zim"
    dst = tmp_path / "dst.zim"
    info = _build_v1_spatial_zim(src, with_fulltext=True)

    src_arc = Archive(str(src))
    assert src_arc.has_fulltext_index, "fixture should have fulltext"

    # Sanity: the source can find our distinctive word.
    src_results = list(Searcher(src_arc).search(
        Query().set_query("vandalism")).getResults(0, 5))
    assert any("vandalism" in r for r in src_results), (
        f"source ZIM didn't surface 'vandalism' via Xapian: {src_results}")
    src_arc = None  # release before upgrader opens it

    upgrade(str(src), str(dst), split_hot_search_chunks_mb=10)

    dst_arc = Archive(str(dst))
    assert dst_arc.has_fulltext_index, (
        "upgrade dropped the fulltext index — verify the Creator has "
        "config_indexing(True, lang) before adding entries"
    )
    # The X/fulltext/xapian entry should be present in the X namespace.
    saw_xapian = False
    for i in range(dst_arc.entry_count, dst_arc.all_entry_count):
        e = dst_arc._get_entry_by_id(i)
        if e.path == "fulltext/xapian":
            saw_xapian = True
            break
    assert saw_xapian, "no 'fulltext/xapian' entry in upgraded ZIM"

    dst_results = list(Searcher(dst_arc).search(
        Query().set_query("vandalism")).getResults(0, 5))
    assert any("vandalism" in r for r in dst_results), (
        f"upgrade-rebuilt fulltext search lost 'vandalism': {dst_results}")


def test_upgrade_preserves_szci_v2_and_search_chunks(tmp_path):
    """The upgrade must rewrite SZCI v1 → v2 and carry through
    search-data leaves byte-identically (when none exceed the resplit
    threshold)."""
    try:
        from libzim.reader import Archive
    except ImportError:
        pytest.skip("libzim not installed")

    from cloud.upgrade_spatial_zim import upgrade
    from tests.szrg_spatial import (
        SZCI_MAGIC, SZCI_VERSION_INLINE, SZCI_VERSION_SHARDED,
    )

    src = tmp_path / "src.zim"
    dst = tmp_path / "dst.zim"
    _build_v1_spatial_zim(src, with_fulltext=False)

    src_arc = Archive(str(src))
    src_idx = bytes(src_arc.get_entry_by_path(
        "routing-data/graph-cells-index.bin").get_item().content)
    assert src_idx[:4] == SZCI_MAGIC
    assert struct.unpack_from("<I", src_idx, 4)[0] == SZCI_VERSION_INLINE
    src_leaf = bytes(src_arc.get_entry_by_path(
        "search-data/aa.json").get_item().content)
    src_arc = None

    upgrade(str(src), str(dst), split_hot_search_chunks_mb=10)

    dst_arc = Archive(str(dst))
    dst_idx = bytes(dst_arc.get_entry_by_path(
        "routing-data/graph-cells-index.bin").get_item().content)
    assert dst_idx[:4] == SZCI_MAGIC
    assert struct.unpack_from("<I", dst_idx, 4)[0] == SZCI_VERSION_SHARDED, (
        "upgrade should rewrite SZCI to v2 (sharded nodes_scaled)")

    dst_leaf = bytes(dst_arc.get_entry_by_path(
        "search-data/aa.json").get_item().content)
    assert dst_leaf == src_leaf, "below-threshold search leaf must pass through byte-identical"
    worker = bytes(dst_arc.get_entry_by_path(
        "routing-worker.js").get_item().content)
    assert b"handleSnap" in worker, "viewer upgrade must add the routing worker"
