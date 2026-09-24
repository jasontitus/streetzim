#!/usr/bin/env python3
"""Emit the viewer-rollout work list: every live region, SMALLEST FIRST.

Columns: id <TAB> zim <TAB> search-term <TAB> bytes

Source of truth is web/index.html -- the rendered site names, per region, the
exact file the Download button points at. Deriving from the local directory
instead would pick up rejected builds and superseded dates.

Run at rollout start, not days ahead: regions that ship between now and then
must be in the list too (they cost nothing -- if a region already carries the
current viewer, the patch is a no-op, the bytes are unchanged and
`ia upload --checksum` skips the transfer).

Regions whose viewer is NOT in fixed slots are written to the tail with a
`#noslot ` prefix: patch_viewer_inplace cannot touch them, they need
cloud/swap_viewer_rust.py (full re-pack, hours). Commented out so the rollout
skips them rather than burning a slot-less region's upload on a failed patch.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOT = {"index.html": 1048576, "places.html": 262144, "routing-worker.js": 131072}


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "rollout-viewer.tsv")
    site = open(os.path.join(REPO, "web", "index.html"), encoding="utf-8").read()
    live = sorted(set(re.findall(
        r"archive\.org/download/streetzim-([^/]+)/(osm-[^\"]+?\.zim)", site)))

    terms = {}
    with open(os.path.join(REPO, "cloud", "regions.tsv"), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            c = line.rstrip("\n").split("\t")
            if len(c) > 6:
                terms[c[0]] = c[6]

    from libzim.reader import Archive
    rows, noslot, missing = [], [], []
    for rid, zim in live:
        path = os.path.join(REPO, zim)
        if not os.path.exists(path):
            missing.append(zim)
            continue
        size = os.path.getsize(path)
        term = terms.get(rid) or "station"
        try:
            a = Archive(path)
            slotted = all(a.get_entry_by_path(n).get_item().size == v
                          for n, v in SLOT.items())
        except Exception as exc:                       # noqa: BLE001
            print(f"  {rid}: unreadable ({exc}) — skipped", flush=True)
            continue
        (rows if slotted else noslot).append((size, rid, zim, term))

    rows.sort()
    noslot.sort()
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("id\tzim\tterm\tbytes\n")
        for size, rid, zim, term in rows:
            fh.write(f"{rid}\t{zim}\t{term}\t{size}\n")
        for size, rid, zim, term in noslot:
            fh.write(f"#noslot {rid}\t{zim}\t{term}\t{size}\n")

    print(f"{len(rows)} patchable, {sum(r[0] for r in rows)/1e9:.1f} GB", flush=True)
    if noslot:
        print(f"{len(noslot)} without slots (commented out, need swap_viewer_rust): "
              + ", ".join(r[1] for r in noslot), flush=True)
    if missing:
        print(f"{len(missing)} listed on the site but absent locally: {missing}", flush=True)


if __name__ == "__main__":
    main()
