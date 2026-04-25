"""Single source of truth for the Find-page category-chip rules.

The Find page (``resources/viewer/places.html``) used to load the full
``category-index/poi.json`` (1+ GB on Japan) and filter by subtype on
the client. Loading that blob OOM'd Chrome on large ZIMs.

Fix: at build time, pre-filter each chip's subset into its own smaller
``category-index/chip-{id}.json`` file. places.html then fetches only
the file it needs for the chosen chip.

This module owns the chip-definitions. It's imported by:
  * create_osm_zim.py — during ZIM build, emits one chip file per entry
  * cloud/repackage_zim.py — optional chip-split on existing ZIMs via
    ``--split-find-chips``
  * tests — assertions that every chip rule has at least one match in a
    known-good ZIM

The viewer (``places.html``) now only needs the chip ids + labels;
filtering is done upstream. Keep the ``CHIP_RULES`` list here as the
authoritative source, mirror ``{id, label}`` minimal entries in
places.html for the chip-bar UI.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ChipRule:
    """One Find-page chip. Records whose ``t`` matches ``from_cat`` AND
    at least one of ``subtypes`` / ``include_regex`` /
    ``name_include`` matches are in the chip."""
    id: str
    label: str
    from_cat: str  # OMT top-level type the records live in (poi, park)
    subtypes: tuple[str, ...] = ()
    include_regex: re.Pattern | None = None
    # Fallback via name: records whose ``t`` is in ``name_subtypes`` AND
    # whose name matches ``name_pattern`` are admitted. Used for museums
    # named "Royal Observatory" etc. that are tagged only as
    # tourism=attraction.
    name_subtypes: tuple[str, ...] = ()
    name_pattern: re.Pattern | None = None


CHIP_RULES: list[ChipRule] = [
    ChipRule(id="restaurants", label="Restaurants", from_cat="poi",
             subtypes=("restaurant", "fast_food", "food_court", "ice_cream"),
             include_regex=re.compile(r"_restaurant$|^food_")),
    ChipRule(id="cafes", label="Cafés", from_cat="poi",
             subtypes=("cafe", "coffee_shop", "bakery", "tea_room",
                       "ice_cream_parlor")),
    ChipRule(id="bars", label="Bars", from_cat="poi",
             subtypes=("bar", "pub", "biergarten", "nightclub", "beer",
                       "alcohol_shop", "wine_bar", "sports_bar",
                       "cocktail_bar", "dive_bar", "beer_bar",
                       "brewery", "wine_store", "liquor_store")),
    ChipRule(id="hotels", label="Hotels", from_cat="poi",
             subtypes=("hotel", "motel", "hostel", "bed_and_breakfast",
                       "lodging", "inn", "guest_house", "resort",
                       "campsite")),
    ChipRule(id="museums", label="Museums", from_cat="poi",
             subtypes=("museum", "art_gallery", "planetarium", "observatory"),
             include_regex=re.compile(r"_museum$|_gallery$"),
             name_subtypes=("tourism", "attraction"),
             name_pattern=re.compile(r"\b(museum|gallery|exhibit|planetarium)\b",
                                     re.IGNORECASE)),
    ChipRule(id="landmarks", label="Landmarks", from_cat="poi",
             subtypes=("historic", "castle", "monument",
                       "historical_landmark",
                       "landmark_and_historical_building", "memorial"),
             name_subtypes=("tourism", "attraction"),
             name_pattern=re.compile(r"\b(landmark|monument|memorial|historic)\b",
                                     re.IGNORECASE)),
    ChipRule(id="parks", label="Parks", from_cat="park"),
    ChipRule(id="libraries", label="Libraries", from_cat="poi",
             subtypes=("library", "public_library")),
    ChipRule(id="health", label="Health", from_cat="poi",
             subtypes=("hospital", "pharmacy", "clinic", "doctors",
                       "dentist", "urgent_care_clinic", "veterinary")),
    ChipRule(id="shops", label="Shops", from_cat="poi",
             subtypes=("shop", "supermarket", "mall", "marketplace",
                       "department_store", "convenience", "grocery",
                       "clothing_store", "jewelry_store"),
             include_regex=re.compile(r"_store$|^store$")),
    ChipRule(id="fuel", label="Gas", from_cat="poi",
             subtypes=("fuel", "charging_station", "gas_station",
                       "ev_charging_station")),
]


def record_matches_chip(rec: dict, chip: ChipRule) -> bool:
    """True if `rec` belongs in this chip's Find-page slice. Expects the
    canonical streetzim record shape: ``t`` top-level type, ``s`` subtype,
    ``n`` name."""
    if rec.get("t") != chip.from_cat:
        # The "parks" chip uses from_cat='park', and park records have t='park'.
        return False
    s = rec.get("s") or ""
    if s in chip.subtypes:
        return True
    if chip.include_regex and chip.include_regex.search(s):
        return True
    # Name-based fallback (museums, landmarks). Requires BOTH a match on
    # the generic subtype bucket AND the text pattern — prevents
    # tourist-trap POIs from flooding the chip.
    if chip.name_subtypes and s in chip.name_subtypes and chip.name_pattern:
        n = rec.get("n") or ""
        if chip.name_pattern.search(n):
            return True
    return False


def split_records_by_chip(records_by_cat: dict) -> dict[str, list]:
    """One pass: return ``{chip_id: [records…]}`` for every chip.

    ``records_by_cat`` is ``{cat_name: [records]}`` — i.e. what the
    category-index already accumulates, keyed by the OMT ``t`` field.
    """
    out: dict[str, list] = {c.id: [] for c in CHIP_RULES}
    for chip in CHIP_RULES:
        src = records_by_cat.get(chip.from_cat, [])
        if not src:
            continue
        if chip.id == "parks" and not chip.subtypes and not chip.include_regex:
            # The "parks" chip is just the whole park category; avoid
            # re-filtering and keep the reference.
            out[chip.id] = list(src)
            continue
        dst = []
        for r in src:
            if record_matches_chip(r, chip):
                dst.append(r)
        out[chip.id] = dst
    return out
