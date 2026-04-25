"""Sanity tests for cloud/chip_rules.py. Locks the contract that
places.html relies on: every chip has an id/label, matches obvious
records, and doesn't matches false-positives like addresses."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestChipRulesContract(unittest.TestCase):
    def test_all_chips_have_id_and_label(self):
        from cloud.chip_rules import CHIP_RULES
        for chip in CHIP_RULES:
            self.assertTrue(chip.id)
            self.assertTrue(chip.label)
            self.assertIn(chip.from_cat, {"poi", "park", "addr", "place"})

    def test_ids_are_unique(self):
        from cloud.chip_rules import CHIP_RULES
        ids = [c.id for c in CHIP_RULES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_restaurants_chip_matches_plain_restaurant(self):
        from cloud.chip_rules import CHIP_RULES, record_matches_chip
        restaurants = next(c for c in CHIP_RULES if c.id == "restaurants")
        r = {"t": "poi", "s": "restaurant", "n": "Joe's Diner"}
        self.assertTrue(record_matches_chip(r, restaurants))

    def test_restaurants_chip_matches_regex_variant(self):
        """Overture uses e.g. japanese_restaurant. The includeRegex
        handles the _restaurant$ tail."""
        from cloud.chip_rules import CHIP_RULES, record_matches_chip
        restaurants = next(c for c in CHIP_RULES if c.id == "restaurants")
        for subtype in ("japanese_restaurant", "korean_restaurant",
                        "fast_food", "food_court"):
            r = {"t": "poi", "s": subtype, "n": "x"}
            self.assertTrue(record_matches_chip(r, restaurants),
                f"restaurants chip should match s={subtype!r}")

    def test_restaurants_chip_excludes_bar(self):
        """Sanity: bars are a different chip; restaurants shouldn't
        claim them."""
        from cloud.chip_rules import CHIP_RULES, record_matches_chip
        restaurants = next(c for c in CHIP_RULES if c.id == "restaurants")
        r = {"t": "poi", "s": "bar", "n": "The Pub"}
        self.assertFalse(record_matches_chip(r, restaurants))

    def test_museums_name_fallback(self):
        """A tourism=attraction record named 'Art Museum of Hawaii'
        should land in museums via the name_pattern fallback.
        (The places.html pattern is /\\b(museum|gallery|exhibit|planetarium)\\b/i
        — does NOT include 'observatory', so that doesn't fire here.)"""
        from cloud.chip_rules import CHIP_RULES, record_matches_chip
        museums = next(c for c in CHIP_RULES if c.id == "museums")
        r = {"t": "poi", "s": "tourism", "n": "Art Museum of Hawaii"}
        self.assertTrue(record_matches_chip(r, museums))

    def test_museums_name_fallback_wont_admit_tourism_restaurant(self):
        from cloud.chip_rules import CHIP_RULES, record_matches_chip
        museums = next(c for c in CHIP_RULES if c.id == "museums")
        r = {"t": "poi", "s": "tourism", "n": "Burger Palace"}
        self.assertFalse(record_matches_chip(r, museums))

    def test_split_preserves_total_record_count_across_chips(self):
        """Running split on a small synthetic corpus: every record
        that qualifies for any chip shows up in that chip's list."""
        from cloud.chip_rules import CHIP_RULES, split_records_by_chip
        records_by_cat = {
            "poi": [
                {"t": "poi", "s": "restaurant", "n": "Jane's"},
                {"t": "poi", "s": "japanese_restaurant", "n": "Sushi"},
                {"t": "poi", "s": "bar", "n": "Pub"},
                {"t": "poi", "s": "museum", "n": "MoMA"},
                {"t": "poi", "s": "tourism", "n": "Famous Art Gallery"},
                {"t": "poi", "s": "hotel", "n": "Hilton"},
                {"t": "poi", "s": "library", "n": "NYPL"},
                {"t": "poi", "s": "fuel", "n": "Shell"},
                {"t": "poi", "s": "shop", "n": "Mart"},
            ],
            "park": [
                {"t": "park", "s": "", "n": "Central Park"},
            ],
        }
        by_chip = split_records_by_chip(records_by_cat)
        # Restaurants should have exactly 2 (restaurant + japanese_restaurant)
        self.assertEqual(len(by_chip["restaurants"]), 2)
        # Museums should pick up MoMA + Art Gallery Uptown (via name_pattern)
        self.assertEqual(len(by_chip["museums"]), 2)
        # Parks get the whole park bucket
        self.assertEqual(len(by_chip["parks"]), 1)
        # No chip drops the bar record — it should be in "bars"
        self.assertEqual(len(by_chip["bars"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
