"""Lock the contract that 0-byte vector tiles are dropped at write
time, but real-content tiles (including the 55-byte ocean-only
pattern) are kept.

Why this test: tilemaker emits a 0-byte PBF for every tile coord that
intersects no features (deep ocean, empty desert). Adding all of those
to the ZIM bloats the entry table (3k–191k empties per region as of
2026-04-25) and floods zimcheck's "Empty article" report. We drop them.

Boundary that MUST hold:
  * Anything < 1 byte → skip
  * Anything ≥ 1 byte → keep (even if it's mostly empty padding;
    those still encode renderable layers like "ocean")

If a future refactor lets a 0-byte tile leak through, mobile clients
hit a redundant 404 and zimcheck flags it. If a future refactor
broadens the skip threshold (e.g. < 60 bytes), ocean tiles disappear
and the map shows the ZIM background color where ocean should be.
Both regressions are silent visual bugs — guard against them here.
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestEmptyTileSkipLogic(unittest.TestCase):
    """The logic boils down to `if not tile_data: skip` inside the
    add_item loop. Test that exact invariant on a synthetic stream.
    """

    def _simulate(self, results):
        """Mirror the real loop's skip logic without involving libzim."""
        added = []
        skipped = 0
        for z, x, y, tile_data in results:
            if not tile_data:
                skipped += 1
                continue
            added.append((z, x, y, tile_data))
        return added, skipped

    def test_empty_bytes_skipped(self):
        added, skipped = self._simulate([
            (10, 0, 0, b""),
            (10, 1, 0, b""),
            (10, 2, 0, b"\x00"),  # one byte — keep (not empty in MVT terms)
        ])
        self.assertEqual(skipped, 2)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0][3], b"\x00")

    def test_ocean_tile_kept(self):
        """The 55-byte 'ocean only' pattern from a real Silicon Valley
        build. MUST be kept — MapLibre paints it as ocean."""
        ocean_55 = bytes.fromhex(
            "1a3578020a05776174657228802012171803220f0929a8401ad24000"
            "00d140d140000f120200001a05636c61737322070a056f6365616e"
        )
        self.assertEqual(len(ocean_55), 55)
        added, skipped = self._simulate([(11, 326, 794, ocean_55)])
        self.assertEqual(skipped, 0)
        self.assertEqual(added, [(11, 326, 794, ocean_55)])

    def test_real_content_kept(self):
        """A normal-size tile (large bytes blob) is unconditionally kept."""
        fake_big = b"\x1a" * 5000
        added, skipped = self._simulate([(14, 100, 200, fake_big)])
        self.assertEqual(skipped, 0)
        self.assertEqual(added[0][3], fake_big)

    def test_mixed_stream_counts_correct(self):
        """Mix of empties and content — counts add up exactly."""
        stream = [
            (10, 0, 0, b""),
            (10, 1, 0, b"x"),
            (10, 2, 0, b""),
            (10, 3, 0, b"yz"),
            (10, 4, 0, b""),
        ]
        added, skipped = self._simulate(stream)
        self.assertEqual(skipped, 3)
        self.assertEqual(len(added), 2)
        self.assertEqual(skipped + len(added), len(stream))


class TestSkipBoundsInSourceCode(unittest.TestCase):
    """Read the relevant section of create_osm_zim.py and assert the
    skip predicate is the byte-length-zero check, not a >0-byte
    threshold. Catches the most likely regression: someone changes
    `if not tile_data` to `if len(tile_data) < N` and silently drops
    ocean tiles. The test is intentionally string-based so it doesn't
    require importing the (slow) main module."""

    def test_skip_predicate_is_zero_only(self):
        src = (ROOT / "create_osm_zim.py").read_text(encoding="utf-8")
        # The skip line should look like `if not tile_data:`.
        # Reject thresholded variants like `< 60` or `<= 100` near it.
        marker = "tiles_skipped_empty += 1"
        self.assertIn(marker, src,
            "tiles_skipped_empty counter missing from create_osm_zim.py")
        # Find the lines around the marker.
        lines = src.splitlines()
        marker_idx = next(i for i, L in enumerate(lines) if marker in L)
        window = "\n".join(lines[max(0, marker_idx - 5):marker_idx + 1])
        self.assertIn("if not tile_data", window,
            "skip guard should be `if not tile_data:` "
            "(reject a length-threshold variant)")
        # No threshold operator near the guard.
        self.assertNotRegex(
            window,
            r"len\s*\(\s*tile_data\s*\)\s*[<≤]\s*\d+",
            "skip guard should NOT use a length threshold — "
            "ocean tiles are 55 bytes and must be kept",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
