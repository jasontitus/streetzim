"""End-to-end test: convert each real v4 ZIM's routing graph to v5 in
memory, re-run the golden-corpus route generation on the converted
graph, and confirm every fingerprint matches the v4 golden.

This is the strongest correctness test we have short of an actual
PWA-vs-PWA browser run: it proves the v5 split format preserves every
byte of routing-relevant data and that the geom column still surfaces
the right indices after round-tripping.

Skipped when the matching golden / ZIM pair isn't available locally.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.szrg_reader import parse_szgm_bytes, parse_szrg_bytes
from tests.szrg_astar import find_route
from tests.v4_to_v5_convert import v4_to_v5_bufs


ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = ROOT / "tests" / "golden"


def _load_v4_graph_buf(zim_path: Path) -> bytes:
    from libzim.reader import Archive
    arc = Archive(str(zim_path))
    entry = arc.get_entry_by_path("routing-data/graph.bin")
    return bytes(entry.get_item().content)


def _discover_corpora() -> list[Path]:
    if not GOLDEN_DIR.is_dir():
        return []
    return sorted(
        p for p in GOLDEN_DIR.glob("*.jsonl")
        if not p.name.startswith(".") and p.stat().st_size > 0
    )


def _read_meta(p: Path) -> dict | None:
    with p.open() as fh:
        line = fh.readline().strip()
        return json.loads(line) if line else None


CORPORA = _discover_corpora()


@pytest.mark.skipif(not CORPORA, reason="no golden corpora under tests/golden/")
@pytest.mark.parametrize("corpus", CORPORA, ids=[p.stem for p in CORPORA])
def test_v5_converter_preserves_routing(corpus: Path):
    """Convert the region's v4 graph to v5 in memory, re-route the first
    N pairs from the golden, and diff fingerprints.

    N is capped to keep test time reasonable — we're verifying the split
    preserves bytes, not re-running full 2000-route corpora. For that,
    use tests/test_route_identity.py against a real v5 ZIM rebuild.
    """
    meta = _read_meta(corpus)
    if meta is None or not meta.get("_meta"):
        pytest.skip(f"{corpus.name} has no _meta header")
    source = meta.get("source")
    if not source:
        pytest.skip(f"{corpus.name} has no source ZIM")
    zim_path = ROOT / source
    if not zim_path.is_file():
        pytest.skip(f"missing source ZIM {source}")

    v4_buf = _load_v4_graph_buf(zim_path)
    assert v4_buf[:4] == b"SZRG", "source ZIM's graph.bin is not SZRG"
    # Only test when the source is v4 (converter refuses other versions).
    version = int.from_bytes(v4_buf[4:8], "little")
    if version != 4:
        pytest.skip(f"{zim_path.name} is SZRG v{version}, converter expects v4")

    g4 = parse_szrg_bytes(v4_buf)
    main_buf, szgm_buf = v4_to_v5_bufs(v4_buf)
    g5 = parse_szrg_bytes(main_buf)
    g5.attach_geoms(szgm_buf)

    # A quick sanity — both should expose the same adj offsets + edges bytes.
    assert bytes(g4.adj_offsets) == bytes(g5.adj_offsets)
    assert bytes(g4.edges) == bytes(g5.edges)
    assert bytes(g4.nodes_scaled) == bytes(g5.nodes_scaled)
    assert bytes(g4.geom_offsets) == bytes(g5.geom_offsets), \
        "geom_offsets diverged after v5 round-trip"
    assert g4.geom_blob == g5.geom_blob, "geom_blob diverged after v5 round-trip"
    assert bytes(g4.name_offsets) == bytes(g5.name_offsets)
    assert g4.names_blob == g5.names_blob

    # Replay first 50 pairs from the golden and diff fingerprints.
    mismatches = 0
    checked = 0
    with corpus.open() as fh:
        fh.readline()  # skip _meta line
        for i, line in enumerate(fh):
            if checked >= 50:
                break
            rec = json.loads(line)
            if rec.get("unreachable"):
                continue
            s, e = rec["s"], rec["e"]
            r4 = find_route(g4, s, e)
            r5 = find_route(g5, s, e)
            if r4 is None or r5 is None:
                mismatches += 1
                continue
            if r4.fingerprint() != r5.fingerprint():
                mismatches += 1
            checked += 1

    assert checked > 0, "no routable pairs in the first 50 golden records"
    assert mismatches == 0, (
        f"{corpus.name}: v5 round-trip diverged on {mismatches}/{checked} "
        "routes"
    )
