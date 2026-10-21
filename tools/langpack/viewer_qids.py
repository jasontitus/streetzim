#!/usr/bin/env python3
"""Q-IDs with an article in the base AND in PACK (if given), preferred first.
    viewer_qids.py BASE.zim [PACK.zim|-] Q1,Q2,...   -> prints up to 8 Q-IDs, comma-separated"""
import json, re, sys
from pathlib import Path
from libzim.reader import Archive
Q = re.compile(r"^Q\d+$")
def qids(path, geo):
    a = Archive(path)
    if not a.has_entry_by_path(geo):
        return set()
    m = json.loads(bytes(a.get_entry_by_path(geo).get_item().content))
    out = set()
    for g in m.values():
        for v in (g[3:5] if isinstance(g, list) else []):
            if isinstance(v, str) and Q.match(v):
                out.add(v); break
    return out
base, pack, pref = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
common = qids(base, "wiki-geo-index.json")
if pack != "-":
    from libzim.reader import Archive as A
    a = A(Path(pack))
    pre = next(e for e in (a._get_entry_by_id(i) for i in range(a.all_entry_count)) if e.path.startswith("~w/")).path.split("/")[1]
    common &= qids(pack, f"~w/{pre}/wiki-geo-index.json")
print(",".join([q for q in pref if q in common] + sorted(common - set(pref), key=lambda q: int(q[1:]))[:8]))
