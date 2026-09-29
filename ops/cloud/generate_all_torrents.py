#!/usr/bin/env python3
"""For every streetzim-* item on archive.org, generate a clean
single-file .torrent in web/torrents/ pointing to the current ZIM via
HTTP webseed. Run after every upload-and-deploy cycle, or on-demand to
refresh.

Why: archive.org's auto-generated `_archive.torrent` includes the full
file list (active ZIMs + every history backup), so users with default
torrent clients pull 3–5x more bytes than they need. Our torrent has
exactly one file (the current ZIM), webseeds from archive.org's HTTP
URL, and uses DHT + archive.org's open trackers for peer discovery.

Pieces are hashed from the LOCAL copy of the ZIM when one is on disk. It is
byte-identical -- it is the file that was uploaded -- and hashing it costs a
disk read instead of a download. Without a local copy, hashing means streaming
the whole ZIM back from archive.org, so by default such a region is SKIPPED
rather than silently pulling tens of gigabytes; the whole set is ~700 GB.
Pass --allow-download to accept that cost deliberately.

Usage:
  python3 cloud/generate_all_torrents.py             # all regions, local files
  python3 cloud/generate_all_torrents.py africa      # just one region
  python3 cloud/generate_all_torrents.py --skip-existing  # don't re-do existing
  python3 cloud/generate_all_torrents.py --allow-download  # stream what is absent
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from typing import Dict, List, Optional

# Reuse the streamer + bencoder from build_torrent.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_torrent import build_torrent  # noqa: E402

# Same regex as cloud/cleanup_old_zims.py — matches dated, year-month,
# and undated ZIM filenames so we pick the right one on every item.
DATED_ZIM_FULL = re.compile(r"^osm-(.+)-(\d{4}-\d{2}-\d{2}[a-z]?)\.zim$")
DATED_ZIM_MONTH = re.compile(r"^osm-(.+)-(\d{4}-\d{2})\.zim$")
UNDATED_ZIM = re.compile(r"^osm-(.+)\.zim$")


def parse_zim_filename(name: str) -> Optional[tuple]:
    if "history/" in name:
        return None
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


def list_streetzim_items() -> List[str]:
    """Enumerate all streetzim-* items via the search API."""
    url = (
        "https://archive.org/advancedsearch.php?"
        "q=identifier%3Astreetzim-*"
        "&fl%5B%5D=identifier"
        "&rows=100"
        "&output=json"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return sorted(d["identifier"] for d in data["response"]["docs"])


def latest_zim_for_item(identifier: str) -> Optional[str]:
    """Return the newest active ZIM filename in `identifier`, or None.

    Newest is determined by lex-sortable date key (YYYY-MM-DD[suffix]).
    Items with no active ZIM (just metadata) return None.
    """
    url = f"https://archive.org/metadata/{identifier}"
    req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        meta = json.load(resp)
    candidates = []
    for f in meta.get("files", []):
        name = f.get("name", "")
        parsed = parse_zim_filename(name)
        if parsed:
            candidates.append(parsed)
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]  # name


def torrent_for_item(identifier: str, out_dir: str,
                     skip_existing: bool = False,
                     allow_download: bool = False,
                     local_dir: str = ".") -> bool:
    """Generate a torrent for `identifier`. Returns True on success."""
    region_id = identifier[len("streetzim-"):]
    out_path = os.path.join(out_dir, f"{region_id}.torrent")
    if skip_existing and os.path.exists(out_path):
        print(f"  {region_id}: skip (existing torrent kept)")
        return True
    zim_name = latest_zim_for_item(identifier)
    if not zim_name:
        print(f"  {region_id}: no active ZIM yet, skipping")
        return False
    url = f"https://archive.org/download/{identifier}/{zim_name}"
    # Hash from disk when we have the file. build_torrent() with no local_path
    # streams the ZIM back from archive.org, which for the full set is ~700 GB
    # of downloads -- a footgun, since generate.py's own "regenerate with
    # cloud/generate_all_torrents.py" hint points straight at it.
    local_path = os.path.join(local_dir, zim_name)
    if not os.path.exists(local_path):
        if not allow_download:
            print(f"  {region_id}: {zim_name} not on disk — skipping "
                  "(re-run with --allow-download to stream it back)")
            return False
        local_path = None
    print(f"\n--- {region_id} ({zim_name}"
          f"{'' if local_path else ', streaming from archive.org'}) ---")
    try:
        build_torrent(
            url,
            out_path,
            comment=f"streetzim {region_id} — current build ({zim_name})",
            local_path=local_path,
        )
    except Exception as e:
        print(f"  FAILED: {e}", file=sys.stderr)
        return False
    return True


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("regions", nargs="*",
                   help="region IDs to process (omit for all)")
    p.add_argument("--out-dir", default="web/torrents",
                   help="Directory to write .torrent files")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip regions that already have a .torrent")
    p.add_argument("--allow-download", action="store_true",
                   help="Stream a ZIM back from archive.org when it is not on "
                        "disk (the full set is ~700 GB; off by default)")
    p.add_argument("--local-dir", default=".",
                   help="Where the uploaded ZIMs live (default: cwd)")
    args = p.parse_args()

    if args.regions:
        items = [f"streetzim-{r}" for r in args.regions]
    else:
        items = list_streetzim_items()

    os.makedirs(args.out_dir, exist_ok=True)
    print(f"Generating torrents in {args.out_dir} for {len(items)} item(s)")
    ok = 0
    for ident in items:
        if torrent_for_item(ident, args.out_dir,
                            skip_existing=args.skip_existing,
                            allow_download=args.allow_download,
                            local_dir=args.local_dir):
            ok += 1
    print(f"\nDone: {ok}/{len(items)} succeeded")
    return 0 if ok == len(items) else 1


if __name__ == "__main__":
    sys.exit(main())
