#!/usr/bin/env python3
"""Prune old dated ZIMs from streetzim-* archive.org items.

Each rebuild uploads a fresh `osm-<id>-YYYY-MM-DD.zim`, which historically
meant every item accumulated one file per build. That:
  - Confuses users who see 4+ files and have to guess which is current.
  - Churns archive.org's auto-generated torrent (infohash changes on
    every file-list change, so BitTorrent seeders of older versions
    can't help the new swarm).

Policy (2026-04-22): keep the **two newest** dated ZIMs per item. The
newest is today's; the penultimate stays around briefly so in-flight
partial downloads of the previous version can finish. Everything older
gets `ia delete`d. Once rebuild churn slows we can tighten this to
"keep newest only".

Usage:
  python3 cloud/cleanup_old_zims.py              # all streetzim-* items
  python3 cloud/cleanup_old_zims.py streetzim-silicon-valley
  python3 cloud/cleanup_old_zims.py --keep 1     # tighter policy
  python3 cloud/cleanup_old_zims.py --dry-run
"""
import argparse
import json
import re
import subprocess
import sys
from typing import Dict, List, Tuple

# Filename shapes we manage, oldest-style first:
#   osm-<id>.zim                                 (legacy undated)
#   osm-<id>-YYYY-MM.zim                         (legacy year-month)
#   osm-<id>-YYYY-MM-DD[a-z]?.zim                (current; suffix b/c/… for same-day re-rolls)
#
# Pre-2026-05-01 the regex required full YYYY-MM-DD, so legacy undated
# and year-month files snuck past `--keep 2` forever. Audit on
# 2026-05-01 found africa with 4 ZIMs (an undated one, a 2026-04, and
# two dated), california with 5, midwest-us with 5, etc. The torrent
# `streetzim-<id>_archive.torrent` lists every file in the item, so
# users were getting torrents containing 3+ stale ZIMs.
#
# Sort key promotes the shorter shapes so they compare oldest:
#   undated         → ""
#   YYYY-MM         → "YYYY-MM-00"   (sorts before any same-month YYYY-MM-DD)
#   YYYY-MM-DD[s]   → "YYYY-MM-DD[s]"
DATED_ZIM_FULL  = re.compile(r"^osm-(.+)-(\d{4}-\d{2}-\d{2}[a-z]?)\.zim$")
DATED_ZIM_MONTH = re.compile(r"^osm-(.+)-(\d{4}-\d{2})\.zim$")
UNDATED_ZIM     = re.compile(r"^osm-(.+)\.zim$")


def parse_zim_filename(name: str):
    """Return (sort_key, name) if `name` is a managed ZIM; else None."""
    m = DATED_ZIM_FULL.match(name)
    if m:
        return (m.group(2), name)
    m = DATED_ZIM_MONTH.match(name)
    if m:
        return (m.group(2) + "-00", name)
    m = UNDATED_ZIM.match(name)
    if m:
        return ("", name)
    return None


def ia(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(["ia", *args], capture_output=True, text=True)


def list_all_streetzim_items() -> List[str]:
    """Enumerate all streetzim-* items this uploader owns."""
    # `ia search` with a collection filter is the canonical way; we match
    # by identifier prefix since all our items follow `streetzim-<id>`.
    out = ia(["search", "identifier:streetzim-*", "--field=identifier", "--parameters=rows:200"])
    if out.returncode != 0:
        print("ia search failed:", out.stderr, file=sys.stderr)
        sys.exit(1)
    ids = []
    for line in out.stdout.splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        # `ia search` outputs JSON lines; fall back to raw strings.
        try:
            j = json.loads(line)
            ident = j.get("identifier")
            if ident:
                ids.append(ident)
        except Exception:
            ids.append(line)
    return sorted(set(ids))


def item_zim_files(item: str) -> List[Dict]:
    """Return ZIM files in an archive.org item, oldest-first by date."""
    out = ia(["metadata", item])
    if out.returncode != 0:
        print(f"ia metadata {item} failed:", out.stderr, file=sys.stderr)
        return []
    try:
        meta = json.loads(out.stdout)
    except Exception as e:
        print(f"invalid metadata for {item}: {e}", file=sys.stderr)
        return []
    zims: List[Tuple[str, str]] = []
    for f in meta.get("files", []):
        name = f.get("name") or ""
        if "history/" in name:
            continue
        parsed = parse_zim_filename(name)
        if parsed:
            zims.append(parsed)
    zims.sort()  # ascending by date (undated first, year-month next, full date newest)
    return [{"date": d, "name": n} for d, n in zims]


def prune(item: str, keep: int, dry_run: bool) -> Tuple[int, int]:
    """Delete all but the `keep` newest dated ZIMs. Returns (kept, deleted)."""
    files = item_zim_files(item)
    if len(files) <= keep:
        return len(files), 0
    victims = files[:-keep]
    keepers = files[-keep:]
    print(f"\n{item}: {len(files)} dated ZIMs; keeping {keep}, deleting {len(victims)}")
    for k in keepers:
        print(f"  KEEP   {k['name']}")
    for v in victims:
        tag = "DRY-RUN" if dry_run else "DELETE "
        print(f"  {tag} {v['name']}")
        if dry_run:
            continue
        # `ia delete <item> <file>` — requires `ia configure` auth with
        # write access. We tolerate individual failures so one bad file
        # doesn't abort the whole cleanup run.
        r = ia(["delete", item, v["name"]])
        if r.returncode != 0:
            print(f"    FAILED: {r.stderr.strip()}", file=sys.stderr)
    return keep, len(victims)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("items", nargs="*",
                   help="streetzim-* identifiers (omit for all)")
    p.add_argument("--keep", type=int, default=2,
                   help="Keep the N newest dated ZIMs per item (default 2)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be deleted without calling ia delete")
    args = p.parse_args()

    if args.keep < 1:
        print("--keep must be >= 1", file=sys.stderr)
        return 2

    items = args.items or list_all_streetzim_items()
    if not items:
        print("no items found", file=sys.stderr)
        return 1
    print(f"Processing {len(items)} item(s); policy: keep {args.keep} newest")

    total_deleted = 0
    for item in items:
        _, deleted = prune(item, args.keep, args.dry_run)
        total_deleted += deleted
    print(f"\nTotal deletions: {total_deleted}" + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
