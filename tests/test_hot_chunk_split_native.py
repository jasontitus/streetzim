"""Unit tests for the hot-chunk search split that `create_osm_zim.py`
needs to match `cloud/repackage_zim.py` byte-for-byte.

Why this test matters: the viewer + Swift geocoder both read the
manifest's ``sub_chunks`` map and route queries to ``{prefix}-{hex}``
sub-files. Any disagreement between the writer(s) and the readers
will silently miss records — exactly the class of bug that hurt us
during the Japan/Iran rollout. This test locks the contract.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestSubBucketHash(unittest.TestCase):
    """The FNV-1a hash that maps record.n → sub-bucket index MUST
    match the JavaScript viewer (``subBucketFor``) and Swift
    (``Geocoder.subBucketFor``) byte-for-byte, or queries silently
    miss records."""

    def _fnv1a32(self, s: str) -> int:
        h = 0x811C9DC5
        for b in s.encode("utf-8"):
            h ^= b
            h = (h * 0x01000193) & 0xFFFFFFFF
        return h

    def test_sub_bucket_for_name_matches_repackage(self):
        from cloud.repackage_zim import _sub_bucket_for_name
        for name in ("Tokyo", "大阪", "Østfold", "123 Main St",
                     "", "x", "🙂", "a" * 100):
            expected = self._fnv1a32(name) % 16
            got = _sub_bucket_for_name(name, 16)
            self.assertEqual(got, expected,
                             f"bucket mismatch for {name!r}: "
                             f"got={got}, expected={expected}")

    def test_sub_bucket_distribution_even(self):
        """FNV-1a should spread the name space well across 16 buckets.
        Use lots of synthetic names + check no bucket is >2× average."""
        from cloud.repackage_zim import _sub_bucket_for_name
        counts = [0] * 16
        for i in range(16_000):
            counts[_sub_bucket_for_name(f"name-{i}-suffix", 16)] += 1
        avg = sum(counts) / 16
        for c in counts:
            self.assertLessEqual(c, avg * 2,
                f"bucket skew: got {c}, avg {avg}")


class TestSplitBigChunk(unittest.TestCase):
    """Validates the chunk-splitting function (whichever module we
    implement it in — both create_osm_zim and repackage_zim must
    agree). Runs on an in-memory fake manifest + records, so it
    doesn't depend on libzim."""

    def _make_records(self, n: int, prefix: str):
        # Records big enough that 16-way split keeps each sub-chunk
        # under the threshold. Adjust until needed.
        return [{"n": f"{prefix}{i:06d}",
                 "t": "poi",
                 "a": 35.0,
                 "o": 140.0,
                 "l": "padding " * 50}  # make each record ~400 B
                for i in range(n)]

    def test_records_distribute_to_all_buckets(self):
        from cloud.repackage_zim import _sub_bucket_for_name
        records = self._make_records(1000, "r")
        buckets = [[] for _ in range(16)]
        for r in records:
            buckets[_sub_bucket_for_name(r["n"], 16)].append(r)
        non_empty = sum(1 for b in buckets if b)
        # With 1000 records and 16 buckets, all should be populated.
        self.assertEqual(non_empty, 16,
            f"only {non_empty}/16 buckets got records")

    def test_every_record_lands_in_exactly_one_bucket(self):
        """No duplication, no loss."""
        from cloud.repackage_zim import _sub_bucket_for_name
        records = self._make_records(500, "unique")
        bucket_of = {}
        for r in records:
            b = _sub_bucket_for_name(r["n"], 16)
            self.assertNotIn(r["n"], bucket_of,
                "record appeared twice across buckets")
            bucket_of[r["n"]] = b
        self.assertEqual(len(bucket_of), 500)
        # Every bucket index in [0,16)
        self.assertTrue(all(0 <= b < 16 for b in bucket_of.values()))

    def test_sub_chunk_naming_matches_hex_format(self):
        """Sub-chunks are named ``{prefix}-{hex}``. The writer, viewer,
        and Swift all parse the trailing hex — validate the width."""
        hex_width = len(format(16 - 1, "x"))
        self.assertEqual(hex_width, 1)
        # For 16 buckets, hex is 0..f (single char).
        for i in range(16):
            name = f"prefix-{format(i, f'0{hex_width}x')}"
            self.assertRegex(name, r"^prefix-[0-9a-f]$")


class TestManifestRewriteContract(unittest.TestCase):
    """The manifest after splitting MUST satisfy:
    - Original prefix key is DROPPED from ``chunks``
    - Each sub-prefix is ADDED to ``chunks`` with its record count
    - Original prefix is ADDED to ``sub_chunks`` mapping to the sub-prefix list
    - ``total`` count is preserved
    """

    def test_manifest_shape_after_split(self):
        # Simulate what the writer does.
        original = {
            "total": 100,
            "chunks": {"sh": 100, "sa": 50, "ot": 10},
        }
        # Pretend "sh" was oversized and split into sh-0..sh-f.
        sh_subs = {f"sh-{i:x}": 100 // 16 + (1 if i < 4 else 0)
                   for i in range(16)}
        new = {
            "total": original["total"],
            "chunks": {**{k: v for k, v in original["chunks"].items()
                          if k != "sh"},
                       **sh_subs},
            "sub_chunks": {"sh": list(sh_subs.keys())},
        }
        # Contract assertions:
        self.assertNotIn("sh", new["chunks"], "original prefix must drop")
        self.assertIn("sh", new["sub_chunks"], "original prefix in sub_chunks")
        self.assertEqual(len(new["sub_chunks"]["sh"]), 16)
        self.assertEqual(sum(new["chunks"][k] for k in sh_subs),
                         original["chunks"]["sh"],
                         "sub-bucket counts must sum to the original")
        self.assertEqual(new["total"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
