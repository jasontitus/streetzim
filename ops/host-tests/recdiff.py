"""recdiff.py A.zim B.zim : for search-data/ and category-index/ JSON lists,
how many files are byte-identical, hold the same records in another order,
or hold different records (and how many records differ each way)."""
import collections, json, sys
from libzim.reader import Archive
def recs(path):
    a = Archive(path); out = {}
    for i in range(a.all_entry_count):
        e = a._get_entry_by_id(i)
        if e.is_redirect: continue
        if e.path.startswith(("search-data/", "category-index/")) and e.path.endswith(".json"):
            out[e.path] = bytes(e.get_item().content)
    return out
A, B = recs(sys.argv[1]), recs(sys.argv[2])
same = reord = diff = only36 = only4 = 0
for p in sorted(set(A) | set(B)):
    if A.get(p) == B.get(p): same += 1; continue
    if p not in A or p not in B: diff += 1; continue
    x, y = json.loads(A[p]), json.loads(B[p])
    if not isinstance(x, list) or not isinstance(y, list):
        diff += 1; continue
    cx = collections.Counter(json.dumps(r, sort_keys=True) for r in x)
    cy = collections.Counter(json.dumps(r, sort_keys=True) for r in y)
    if cx == cy: reord += 1
    else: diff += 1; only36 += sum((cx - cy).values()); only4 += sum((cy - cx).values())
print(f"files: identical {same}, same records reordered {reord}, different records {diff}; "
      f"records only in A {only36}, only in B {only4}")
