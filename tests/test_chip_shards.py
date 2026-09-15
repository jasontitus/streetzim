"""cloud/chip_shards.py: geographic Find-chip shards.

Locks the contract the viewers' chip-shards block and the validator rely
on: every record lands in exactly one shard, inside that shard's bbox,
in its original relative order; shard files reassemble to the chip."""
from __future__ import annotations

import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud.chip_shards import (  # noqa: E402
    LAYOUT_GEO, plan_chip, read_chip_records,
)


def _rec(i, lat, lon, pad=40):
    return {"n": f"Place {i:07d}" + "x" * pad, "t": "poi", "s": "shop",
            "a": lat, "o": lon, "l": "Town"}


def _inside(rec, bbox, tol=0.0):
    s, w, n, e = bbox
    lat, lon = rec["a"], rec["o"]
    if not (s - tol <= lat <= n + tol):
        return False
    if w <= e:
        return w - tol <= lon <= e + tol
    return lon >= w - tol or lon <= e + tol


def _materialize(plan, chip_id="shops"):
    return {path: blob for path, _title, blob in plan.files(chip_id, "Shops")}


class TestPlanChip(unittest.TestCase):
    def _check(self, records, target):
        plan = plan_chip(records, target)
        files = _materialize(plan)
        entry = plan.manifest_entry("Shops")
        whole = json.dumps(records, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
        self.assertEqual(entry["count"], len(records))
        self.assertEqual(entry["bytes"], len(whole))
        if not plan.sharded:
            self.assertEqual(list(files.values()), [whole])
            self.assertNotIn("sub_chunks", entry)
            return plan, entry, files
        self.assertEqual(entry["layout"], LAYOUT_GEO)
        self.assertNotIn("n_sub_buckets", entry)
        self.assertEqual(len(entry["sub_chunks"]), len(entry["shards"]))
        seen = []
        total = 0
        for suffix, row in zip(entry["sub_chunks"], entry["shards"]):
            blob = files[f"category-index/chip-shops-{suffix}.json"]
            recs = json.loads(blob)
            s, w, n, e, count, nbytes = row
            self.assertEqual(count, len(recs))
            self.assertEqual(nbytes, len(blob))
            total += count
            for r in recs:
                if s is None:
                    self.assertTrue(all(v is None for v in (s, w, n, e)))
                else:
                    self.assertTrue(_inside(r, (s, w, n, e)), (r, row))
            seen.extend(recs)
        self.assertEqual(total, len(records))
        # Every record exactly once (records here have unique names).
        self.assertEqual(sorted(r["n"] for r in seen),
                         sorted(r["n"] for r in records))
        # Original relative order inside each shard.
        by_name = {r["n"]: i for i, r in enumerate(records)}
        for suffix in entry["sub_chunks"]:
            order = [by_name[r["n"]] for r in json.loads(
                files[f"category-index/chip-shops-{suffix}.json"])]
            self.assertEqual(order, sorted(order))
        # read_chip_records reassembles the chip from the files.
        back = read_chip_records(lambda p: files[p], "shops", entry)
        self.assertEqual(len(back), len(records))
        return plan, entry, files

    def test_small_chip_stays_single_file(self):
        recs = [_rec(i, 40 + i * 1e-3, -74) for i in range(10)]
        plan, entry, files = self._check(recs, 1 << 20)
        self.assertFalse(plan.sharded)
        self.assertEqual(list(files), ["category-index/chip-shops.json"])

    def test_zero_target_never_splits(self):
        recs = [_rec(i, 40, -74) for i in range(500)]
        plan, _, _ = self._check(recs, 0)
        self.assertFalse(plan.sharded)

    def test_random_region_shards_respect_target(self):
        rng = random.Random(7)
        recs = [_rec(i, rng.uniform(25, 49), rng.uniform(-125, -67))
                for i in range(20000)]
        target = 64 * 1024
        plan, entry, _ = self._check(recs, target)
        self.assertTrue(plan.sharded)
        biggest = max(row[5] for row in entry["shards"])
        self.assertLessEqual(biggest, target)
        # Byte-median splits keep shards from getting tiny.
        self.assertGreater(min(row[5] for row in entry["shards"]), target // 8)

    def test_dense_city_is_split_further(self):
        rng = random.Random(3)
        recs = [_rec(i, 40.7 + rng.gauss(0, 0.02), -74.0 + rng.gauss(0, 0.02))
                for i in range(15000)]
        recs += [_rec(15000 + i, rng.uniform(30, 45), rng.uniform(-90, -70))
                 for i in range(1500)]
        _, entry, _ = self._check(recs, 64 * 1024)
        city = [row for row in entry["shards"]
                if row[0] >= 40.4 and row[2] <= 41.0]
        self.assertGreater(len(city), 5)

    def test_antimeridian_shards_wrap_instead_of_spanning_the_globe(self):
        rng = random.Random(11)
        recs = []
        for i in range(6000):
            lon = rng.uniform(178.5, 180.0) if i % 2 else rng.uniform(-180.0, -178.5)
            recs.append(_rec(i, rng.uniform(51, 53), lon))
        _, entry, _ = self._check(recs, 32 * 1024)
        for s, w, n, e, _c, _b in entry["shards"]:
            width = (e - w) if w <= e else (e + 360 - w)
            self.assertLess(width, 5.0, (w, e))
        # (The k-d split cuts raw longitude, so both sides of ±180 end up
        # in different leaves; the w > e form is covered directly by
        # test_straddling_leaf_gets_wrapping_bbox.)

    def test_straddling_leaf_gets_wrapping_bbox(self):
        from cloud.chip_shards import _lon_interval
        import numpy as np
        w, e = _lon_interval(np.array([179.5, -179.5, 179.9]))
        self.assertGreater(w, e)
        self.assertLessEqual(w, 179.5)
        self.assertGreaterEqual(e, -179.5)
        w, e = _lon_interval(np.array([-10.0, 10.0]))
        self.assertEqual((w, e), (-10.0, 10.0))

    def test_coincident_points_still_split(self):
        recs = [_rec(i, 35.0, 139.0) for i in range(3000)]
        _, entry, _ = self._check(recs, 16 * 1024)
        self.assertGreater(len(entry["shards"]), 4)
        self.assertLessEqual(max(r[5] for r in entry["shards"]), 16 * 1024)

    def test_records_without_coordinates_go_to_null_bbox_shards(self):
        rng = random.Random(5)
        recs = [_rec(i, rng.uniform(0, 10), rng.uniform(0, 10)) for i in range(4000)]
        for i, bad in enumerate([None, "12.5", float("nan"), True, 200.0]):
            r = _rec(90000 + i, 1.0, 1.0)
            r["a"] = bad
            recs.append(r)
        missing = _rec(99999, 0, 0)
        del missing["o"]
        recs.append(missing)
        # json can't carry NaN portably; the builder's records never do,
        # but make sure the planner itself doesn't crash or bbox them.
        recs = [r for r in recs if not (isinstance(r.get("a"), float) and r["a"] != r["a"])]
        _, entry, _ = self._check(recs, 32 * 1024)
        null_rows = [r for r in entry["shards"] if r[0] is None]
        self.assertEqual(sum(r[4] for r in null_rows), 5)
        self.assertIs(entry["shards"][-1][0], None)

    def test_deterministic(self):
        rng = random.Random(9)
        recs = [_rec(i, rng.uniform(-40, -10), rng.uniform(110, 155)) for i in range(5000)]
        a = plan_chip(recs, 32 * 1024)
        b = plan_chip(recs, 32 * 1024)
        self.assertEqual(a.manifest_entry("x"), b.manifest_entry("x"))
        self.assertEqual(_materialize(a), _materialize(b))

    def test_unicode_names_keep_utf8_bytes(self):
        recs = [{"n": "東京タワー" * 20, "t": "poi", "s": "shop", "a": 35.0 + i * 1e-4,
                 "o": 139.0} for i in range(400)]
        _, entry, files = self._check(recs, 16 * 1024)
        blob = next(iter(files.values()))
        self.assertIn("東京".encode("utf-8"), blob)


class _FakeArchive:
    """Just enough of libzim.reader.Archive for validate_zim's chip check."""
    def __init__(self, files):
        self.files = files

    def get_entry_by_path(self, path):
        if path not in self.files:
            raise KeyError(path)
        blob = self.files[path]

        class _Item:
            content = blob

        class _Entry:
            def get_item(self_inner):
                return _Item()
        return _Entry()


class TestValidatorGeoChips(unittest.TestCase):
    def _zim(self, mutate=None):
        rng = random.Random(21)
        chips = {}
        files = {}
        for cid, n in (("restaurants", 5000), ("cafes", 3000), ("shops", 6000)):
            recs = [_rec(i, rng.uniform(40, 45), rng.uniform(-80, -70)) for i in range(n)]
            plan = plan_chip(recs, 64 * 1024)
            for path, _t, blob in plan.files(cid, cid):
                files[path] = blob
            chips[cid] = plan.manifest_entry(cid)
        if mutate:
            mutate(chips, files)
        files["category-index/manifest.json"] = json.dumps(
            {"total": 0, "categories": {}, "chips": chips}).encode()
        return _FakeArchive(files)

    def test_valid_geo_layout_passes(self):
        from cloud.validate_zim import _chk_find_chips
        status, msg = _chk_find_chips(self._zim())
        self.assertEqual(status, "pass", msg)
        self.assertIn("3 geo-sharded", msg)

    def test_record_outside_its_shard_bbox_fails(self):
        from cloud.validate_zim import _chk_find_chips

        def swap(chips, files):
            meta = chips["shops"]
            a, b = meta["sub_chunks"][0], meta["sub_chunks"][-1]
            pa = f"category-index/chip-shops-{a}.json"
            pb = f"category-index/chip-shops-{b}.json"
            ra, rb = json.loads(files[pa]), json.loads(files[pb])
            ra[0], rb[0] = rb[0], ra[0]
            files[pa] = json.dumps(ra, separators=(",", ":")).encode()
            files[pb] = json.dumps(rb, separators=(",", ":")).encode()
            meta["shards"][0][5] = len(files[pa])
            meta["shards"][-1][5] = len(files[pb])
        status, msg = _chk_find_chips(self._zim(swap))
        self.assertEqual(status, "fail")
        self.assertIn("outside shard bbox", msg)

    def test_misaligned_shards_fail(self):
        from cloud.validate_zim import _chk_find_chips

        def drop_row(chips, files):
            chips["cafes"]["shards"].pop()
        status, msg = _chk_find_chips(self._zim(drop_row))
        self.assertEqual(status, "fail")
        self.assertIn("misaligned", msg)

    def test_count_mismatch_fails(self):
        from cloud.validate_zim import _chk_find_chips

        def bump(chips, files):
            chips["restaurants"]["shards"][0][4] += 1
        status, msg = _chk_find_chips(self._zim(bump))
        self.assertEqual(status, "fail")

    def test_located_record_in_null_bbox_shard_fails(self):
        from cloud.validate_zim import _chk_find_chips

        def hide(chips, files):
            meta = chips["cafes"]
            path = "category-index/chip-cafes-gfff.json"
            blob = json.dumps([_rec(1, 41.0, -75.0)], separators=(",", ":")).encode()
            files[path] = blob
            meta["sub_chunks"].append("gfff")
            meta["shards"].append([None, None, None, None, 1, len(blob)])
            meta["count"] += 1
        status, msg = _chk_find_chips(self._zim(hide))
        self.assertEqual(status, "fail")
        self.assertIn("no-bbox shard", msg)

    def test_legacy_single_file_verdicts_unchanged(self):
        from cloud.validate_zim import _chk_find_chips

        def monolith(chips, files, size_mb):
            recs = []
            for cid in list(chips):
                for sfx in chips[cid].get("sub_chunks", []):
                    recs.extend(json.loads(files.pop(f"category-index/chip-{cid}-{sfx}.json")))
            files.clear()
            files["category-index/chip-shops.json"] = json.dumps(recs).encode()
            chips.clear()
            chips["shops"] = {"label": "Shops", "count": len(recs),
                              "bytes": size_mb * 1024 * 1024}
        s1, m1 = _chk_find_chips(self._zim(lambda c, f: monolith(c, f, 250)))
        self.assertEqual(s1, "fail", m1)
        s2, m2 = _chk_find_chips(self._zim(lambda c, f: monolith(c, f, 60)))
        self.assertEqual(s2, "warn", m2)
        s3, m3 = _chk_find_chips(self._zim(lambda c, f: monolith(c, f, 5)))
        self.assertEqual(s3, "pass", m3)

    def test_big_name_hash_layout_warns(self):
        from cloud.validate_zim import _chk_find_chips

        def legacy(chips, files):
            meta = chips["shops"]
            recs = []
            for sfx in meta["sub_chunks"]:
                recs.extend(json.loads(files.pop(f"category-index/chip-shops-{sfx}.json")))
            files["category-index/chip-shops-0.json"] = json.dumps(recs).encode()
            chips["shops"] = {"label": "Shops", "count": len(recs),
                              "bytes": 60 * 1024 * 1024,
                              "sub_chunks": ["0"], "n_sub_buckets": 1}
        status, msg = _chk_find_chips(self._zim(legacy))
        self.assertEqual(status, "warn", msg)
        self.assertIn("name-hash", msg)


class TestReadChipRecords(unittest.TestCase):
    def test_legacy_name_bucket_layout(self):
        files = {
            "category-index/chip-cafes-0.json": b'[{"n":"a"}]',
            "category-index/chip-cafes-1.json": b'[{"n":"b"},{"n":"c"}]',
        }
        meta = {"count": 3, "sub_chunks": ["0", "1"], "n_sub_buckets": 2}
        self.assertEqual([r["n"] for r in read_chip_records(files.__getitem__, "cafes", meta)],
                         ["a", "b", "c"])

    def test_single_file_layout(self):
        files = {"category-index/chip-fuel.json": b'[{"n":"x"}]'}
        self.assertEqual(read_chip_records(files.__getitem__, "fuel", {"count": 1}),
                         [{"n": "x"}])


if __name__ == "__main__":
    unittest.main()
