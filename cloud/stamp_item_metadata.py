#!/usr/bin/env python3
"""Stamp feature flags onto an archive.org streetzim-* item.

The ZIM's own `streetzim-meta.json` carries feature flags (hasRouting,
hasOvertureAddresses, etc.) but those live INSIDE the ZIM — archive.org
doesn't index them. The download page at streetzim.web.app wants to
show "does this ZIM have routing / Overture / terrain / …" as per-card
badges without downloading anything, so we mirror the flags onto the
archive.org item's own metadata fields.

Typical usage — called inline from upload_validated.sh after `ia upload`:

  python3 cloud/stamp_item_metadata.py streetzim-switzerland-light \\
      --from-zim osm-switzerland-light-2026-09-26.zim

That reads the flags out of the ZIM being shipped, so the badges can
never claim more than the file delivers. Until 2026-09-27 the caller
passed all five flags unconditionally, which put a "Satellite" badge on
both Light variants — the two products whose whole point is that they
carry no satellite imagery.

Explicit flags still work, for batch-stamping pre-existing items:

  python3 cloud/stamp_item_metadata.py --routing \\
      streetzim-california streetzim-japan …

Fields set:

    streetzim_routing    — SZRG routing graph in `routing-data/graph.bin`
    streetzim_overture   — Overture addresses + places merged in
    streetzim_terrain    — Copernicus GLO-30 terrain tiles included
    streetzim_satellite  — Sentinel-2 satellite imagery included
    streetzim_wikidata   — Wikidata cache for POI enrichment

An explicit `--<flag>` only ever writes "yes": a feature we know nothing
about must not be stamped absent. `--from-zim` DOES write "no", because
there the ZIM is authoritative for all five at once — but only for keys
the meta actually carries, so an older ZIM that predates a flag leaves
that field alone rather than denying a feature it may well have.

The web generator keys off these directly; any field absent from a
given item just hides the corresponding badge for that region.
"""
import argparse
import json
import subprocess
import sys

FEATURES = {
    "routing":   "streetzim_routing",
    "overture":  "streetzim_overture",
    "terrain":   "streetzim_terrain",
    "satellite": "streetzim_satellite",
    "wikidata":  "streetzim_wikidata",
}

# streetzim-meta.json key per feature. Order matches FEATURES for readability.
META_KEYS = {
    "routing":   "hasRouting",
    "overture":  "hasOvertureAddresses",
    "terrain":   "hasTerrain",
    "satellite": "hasSatellite",
    "wikidata":  "hasWikidata",
}


def flags_from_zim(path: str) -> dict:
    """Read streetzim-meta.json out of a ZIM and return {feature: "yes"|"no"}.

    Keys the meta does not carry are omitted, not guessed. Raises on an
    unreadable ZIM or missing meta so the caller can fall back loudly.
    """
    from libzim.reader import Archive          # imported late: only this path needs it
    archive = Archive(path)
    meta = json.loads(bytes(
        archive.get_entry_by_path("streetzim-meta.json").get_item().content))
    out = {}
    for feature, key in META_KEYS.items():
        if key in meta:
            out[feature] = "yes" if meta[key] else "no"
    return out


def stamp(item: str, flags: dict, dry_run: bool = False) -> int:
    """Run one `ia metadata <item> --modify=<key>:<value>` per flag.

    `flags` maps feature name -> "yes"/"no". Returns the number of
    modifications submitted.
    """
    if not flags:
        print(f"{item}: no flags — nothing to do")
        return 0
    args = ["ia", "metadata", item]
    for feature, value in flags.items():
        args.extend(["--modify", f"{FEATURES[feature]}:{value}"])
    shown = ", ".join(f"{f}={v}" for f, v in flags.items())
    print(f"{item}: stamping {shown}")
    if dry_run:
        print("  (dry-run) " + " ".join(args))
        return 0
    try:
        r = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError:
        # `ia` lives in the venv; callers must export its bin on PATH.
        print("  FAILED: `ia` not on PATH", file=sys.stderr)
        return 0
    if r.returncode != 0:
        print(f"  FAILED: {r.stderr.strip()}", file=sys.stderr)
        return 0
    return len(flags)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("items", nargs="+", help="one or more streetzim-* item IDs")
    p.add_argument("--from-zim", metavar="ZIM",
                   help="read all five flags from this ZIM's streetzim-meta.json")
    for flag in FEATURES:
        p.add_argument(f"--{flag}", action="store_true", help=f"set {FEATURES[flag]}=yes")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.from_zim:
        try:
            flags = flags_from_zim(args.from_zim)
        except Exception as exc:                                  # noqa: BLE001
            print(f"FATAL: cannot read flags from {args.from_zim}: {exc}",
                  file=sys.stderr)
            return 1
        if not flags:
            print(f"FATAL: {args.from_zim} carries no recognised feature keys",
                  file=sys.stderr)
            return 1
        explicit = [f for f in FEATURES if getattr(args, f)]
        for f in explicit:                    # a flag on the command line still wins
            flags[f] = "yes"
    else:
        flags = {f: "yes" for f in FEATURES if getattr(args, f)}

    total = 0
    for item in args.items:
        total += stamp(item, flags, args.dry_run)
    print(f"\nTotal flags set: {total}" + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
