"""Diff two ZIMs by entry-path and content-hash — catches any entry
the fresh build silently dropped or mangled vs. the source.

Usage:
  python cloud/diff_zim.py osm-japan-fixed.zim osm-japan-fresh.zim

Reports:
  * entries in src not in dst (DROPPED)
  * entries in dst not in src (ADDED — e.g., new sub-chunks, swapped viewer)
  * entries in both with different content hashes (MODIFIED — flags swaps)

Per-namespace counts so you can see "japan fresh has 0 wikidata files"
loud and clear.
"""
from __future__ import annotations

import hashlib
import sys
from collections import Counter


def scan(path: str) -> dict:
    from libzim.reader import Archive
    a = Archive(path)
    out = {}
    for i in range(a.entry_count):
        e = a._get_entry_by_id(i)
        if e.is_redirect:
            continue
        p = e.path
        try:
            h = hashlib.sha256(bytes(e.get_item().content)).hexdigest()[:16]
        except Exception as exc:
            h = f"<err:{exc}>"
        out[p] = h
    return out


def namespace(p: str) -> str:
    if "/" in p:
        return p.split("/", 1)[0]
    return p  # treat index.html etc. as its own ns


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: diff_zim.py <src.zim> <dst.zim>", file=sys.stderr)
        return 2
    src_path, dst_path = sys.argv[1], sys.argv[2]
    print(f"scanning {src_path}...")
    src = scan(src_path)
    print(f"  {len(src):,} entries")
    print(f"scanning {dst_path}...")
    dst = scan(dst_path)
    print(f"  {len(dst):,} entries")

    src_paths = set(src)
    dst_paths = set(dst)
    only_src = src_paths - dst_paths
    only_dst = dst_paths - src_paths
    both = src_paths & dst_paths
    modified = {p for p in both if src[p] != dst[p]}

    print(f"\n=== summary ===")
    print(f"  src only:  {len(only_src):,}")
    print(f"  dst only:  {len(only_dst):,}")
    print(f"  modified:  {len(modified):,}")
    print(f"  unchanged: {len(both) - len(modified):,}")

    # Namespace tallies.
    def tally(paths, name):
        c = Counter(namespace(p) for p in paths)
        print(f"\n=== {name} by namespace ===")
        for ns, n in sorted(c.items(), key=lambda kv: -kv[1])[:20]:
            print(f"  {n:>8,d}  {ns}")

    tally(only_src, "DROPPED (in src only — potential bug)")
    tally(only_dst, "ADDED (in dst only — expected for swaps/splits)")
    tally(modified, "MODIFIED (content differs)")

    # Concrete samples so we see the actual paths.
    if only_src:
        print("\n=== first 20 DROPPED paths ===")
        for p in sorted(only_src)[:20]:
            print(f"  - {p}")
    if modified:
        print("\n=== first 10 MODIFIED paths ===")
        for p in sorted(modified)[:10]:
            print(f"  * {p}  {src[p]} → {dst[p]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
