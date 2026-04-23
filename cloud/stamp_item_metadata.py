#!/usr/bin/env python3
"""Stamp feature flags onto an archive.org streetzim-* item.

The ZIM's own `streetzim-meta.json` carries feature flags (hasRouting,
hasOvertureAddresses, etc.) but those live INSIDE the ZIM — archive.org
doesn't index them. The download page at streetzim.web.app wants to
show "does this ZIM have routing / Overture / terrain / …" as per-card
badges without downloading anything, so we mirror the flags onto the
archive.org item's own metadata fields.

Typical usage — called inline from upload_and_deploy after an `ia upload`:

  python3 cloud/stamp_item_metadata.py streetzim-silicon-valley \\
      --routing --overture --terrain --satellite --wikidata

Or batch-stamp pre-existing items:

  python3 cloud/stamp_item_metadata.py --routing \\
      streetzim-california streetzim-japan …

Flags set (either "yes" or unchanged; we never set "no" so a missing
feature on a prior ZIM keeps whatever value was there):

    streetzim_routing    — SZRG routing graph in `routing-data/graph.bin`
    streetzim_overture   — Overture addresses + places merged in
    streetzim_terrain    — Copernicus GLO-30 terrain tiles included
    streetzim_satellite  — Sentinel-2 satellite imagery included
    streetzim_wikidata   — Wikidata cache for POI enrichment

The web generator keys off these directly; any field absent from a
given item just hides the corresponding badge for that region.
"""
import argparse
import subprocess
import sys

FEATURES = {
    "routing":   "streetzim_routing",
    "overture":  "streetzim_overture",
    "terrain":   "streetzim_terrain",
    "satellite": "streetzim_satellite",
    "wikidata":  "streetzim_wikidata",
}


def stamp(item: str, features: list, dry_run: bool = False) -> int:
    """Run `ia metadata <item> --modify=<key>:yes` for each feature.

    Returns the number of modifications submitted.
    """
    if not features:
        print(f"{item}: no flags — nothing to do")
        return 0
    args = ["ia", "metadata", item]
    for f in features:
        key = FEATURES[f]
        args.extend(["--modify", f"{key}:yes"])
    print(f"{item}: stamping {', '.join(features)}")
    if dry_run:
        print("  (dry-run) " + " ".join(args))
        return 0
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  FAILED: {r.stderr.strip()}", file=sys.stderr)
        return 0
    return len(features)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("items", nargs="+", help="one or more streetzim-* item IDs")
    for flag in FEATURES:
        p.add_argument(f"--{flag}", action="store_true", help=f"set {FEATURES[flag]}=yes")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    features = [f for f in FEATURES if getattr(args, f)]
    total = 0
    for item in args.items:
        total += stamp(item, features, args.dry_run)
    print(f"\nTotal flags set: {total}" + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
