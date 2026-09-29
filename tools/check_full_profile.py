#!/usr/bin/env python3
"""Check that a `streetzim --profile full` ZIM holds what full promises:
Overture data, Wikidata facts and Wikipedia articles, each flagged in
map-config.json and credited in the License metadata.

    python tools/check_full_profile.py out/osm_en_monaco_2026-09.zim
    python tools/check_full_profile.py --soft-wikimedia ZIM   # weekly live job

Overture is always checked hard: streetzim fails the build when Overture
cannot be fetched, so a full ZIM without it is a bug. With
--soft-wikimedia, missing Wikidata or Wikipedia content is a warning (a
GitHub `::warning::`), because the live APIs rate limit and the build
degrades by design (docs/zimfarm.md, "Failure policy").
"""
from __future__ import annotations

import argparse
import json
import sys

# (map-config flag, text License must contain, is it a Wikimedia source)
CHECKS = [
    ("hasOvertureAddresses", "Overture", False),
    ("hasWikidata", "Wikidata", True),
    ("hasWikiArticles", "Wikipedia", True),
]


def problems(zim: str) -> list[tuple[str, bool]]:
    """[(message, is_wikimedia)] for everything the ZIM lacks."""
    from libzim.reader import Archive
    arc = Archive(zim)
    config = json.loads(bytes(arc.get_entry_by_path("map-config.json").get_item().content))
    licence = bytes(arc.get_metadata("License")).decode("utf-8")
    out: list[tuple[str, bool]] = []
    for flag, credit, wikimedia in CHECKS:
        if config.get(flag) is not True:
            out.append((f"map-config.json: {flag} is {config.get(flag)!r}, not true", wikimedia))
        if credit not in licence:
            out.append((f"License does not credit {credit}: {licence!r}", wikimedia))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("zim")
    ap.add_argument("--soft-wikimedia", action="store_true",
                    help="only warn when Wikidata or Wikipedia content is missing")
    args = ap.parse_args(argv)
    failed = False
    for msg, wikimedia in problems(args.zim):
        if wikimedia and args.soft_wikimedia:
            print(f"::warning::{msg}")
        else:
            print(f"FAIL: {msg}", file=sys.stderr)
            failed = True
    if failed:
        return 1
    print(f"ok: {args.zim} has Overture, Wikidata and Wikipedia content, credited in License"
          + (" (Wikimedia checked softly)" if args.soft_wikimedia else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
