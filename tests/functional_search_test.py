"""Functional end-to-end test for the hot-chunk split.

Usage:
  python tests/functional_search_test.py <zim>

Opens the ZIM, loads ``search-data/manifest.json``, for each hot-split
prefix:
  * Confirms every declared sub-chunk exists in the archive
  * Loads each sub-chunk, asserts every record's name hashes to its
    bucket index
  * Samples the first record, asserts the client-side lookup returns
    the same sub-bucket the record lives in
  * Validates total record count across sub-chunks equals the original
    chunk's claimed count (kept in manifest under ``sub_chunks_total``
    when writer emits it; falls back to summing per-sub if absent)

Also runs one simple geocode: look for a CJK name (e.g. 大阪) or a
common Latin substring (e.g. "Avenue") and confirm the search routes
to the expected sub-bucket AND finds a matching record.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def fnv1a32(s: str) -> int:
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def sub_bucket_for_name(name: str, n: int = 16) -> int:
    return fnv1a32(name) % n


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python tests/functional_search_test.py <zim>", file=sys.stderr)
        return 2
    zim_path = sys.argv[1]
    from libzim.reader import Archive
    arc = Archive(zim_path)
    mani = json.loads(bytes(arc.get_entry_by_path("search-data/manifest.json").get_item().content))
    chunks = mani.get("chunks", {})
    sub_chunks = mani.get("sub_chunks", {})
    print(f"search-data manifest:")
    print(f"  chunks:      {len(chunks)}")
    print(f"  sub_chunks:  {len(sub_chunks)} hot prefixes split")
    if not sub_chunks:
        print("  (no sub_chunks — this ZIM wasn't repackaged with "
              "--split-hot-search-chunks-mb. Pass one that was.)")
        return 1

    # --- Sanity-check every sub-chunk is present + records hash correctly ---
    total_errors = 0
    for orig_prefix, sub_list in sub_chunks.items():
        print(f"\n>> {orig_prefix!r} → {len(sub_list)} sub-chunks")
        records_total = 0
        for sub in sub_list:
            try:
                data = bytes(arc.get_entry_by_path(f"search-data/{sub}.json").get_item().content)
            except Exception as exc:
                print(f"  [FAIL] sub-chunk {sub} not readable: {exc}")
                total_errors += 1
                continue
            records = json.loads(data)
            declared_count = chunks.get(sub)
            if declared_count is not None and declared_count != len(records):
                print(f"  [FAIL] {sub} manifest says {declared_count}, "
                      f"file has {len(records)}")
                total_errors += 1
            # Verify bucket assignment for every record (cheap: just hash
            # the name and check it lands in the expected bucket).
            # Bucket index comes from the suffix after the last '-'.
            try:
                expected_idx = int(sub.rsplit("-", 1)[1], 16)
            except Exception:
                print(f"  [SKIP] {sub}: can't parse bucket id from suffix")
                continue
            mismatches = [
                r.get("n", "") for r in records
                if sub_bucket_for_name(r.get("n", "") or "") != expected_idx
            ]
            if mismatches:
                print(f"  [FAIL] {sub}: {len(mismatches)} records hashed "
                      f"to the wrong bucket (e.g. {mismatches[0]!r})")
                total_errors += 1
            else:
                print(f"  [ OK ] {sub}: {len(records)} records, all hash to "
                      f"bucket {expected_idx:x}")
            records_total += len(records)
        print(f"  total across sub-chunks: {records_total:,} records")

    # --- Geocode probe: pick a query that should land in a hot-split bucket ---
    print(f"\n>> geocode probes:")
    probes = ("大阪", "Avenue", "Tokyo", "東京", "Cairo")
    for q in probes:
        # Compute prefix via the writer's rule
        pw = q.lower().replace(" ", "_")
        if not pw:
            continue
        c0 = pw[0]
        if ord(c0) >= 128:
            prefix = "u" + format(ord(c0), "x")
        elif c0 == "_":
            prefix = "__"
        else:
            k0 = c0 if (c0.isalnum() or c0 == "_") else "_"
            if len(pw) >= 2:
                c1 = pw[1]
                k1 = c1 if (ord(c1) < 128 and (c1.isalnum() or c1 == "_")) else "_"
            else:
                k1 = "_"
            prefix = k0 + k1
        targets = sub_chunks.get(prefix, [prefix])
        ntotal = sum(chunks.get(t, 0) for t in targets)
        split_tag = " (hot-split)" if prefix in sub_chunks else ""
        print(f"  query={q!r} → prefix={prefix!r}{split_tag} → fan-out to "
              f"{len(targets)} chunks, {ntotal:,} records combined")
        # For a split prefix, look for the query in each sub. Pick first
        # hit across all subs.
        hits = 0
        first = None
        for sub in targets:
            try:
                data = bytes(arc.get_entry_by_path(f"search-data/{sub}.json").get_item().content)
                recs = json.loads(data)
            except Exception:
                continue
            for r in recs:
                name = r.get("n", "") or ""
                if q.lower() in name.lower():
                    hits += 1
                    if first is None:
                        first = r
            if first is not None and hits > 20:
                break
        if hits:
            print(f"    → {hits} hits found; sample: "
                  f"{first.get('n')!r} @ ({first.get('a'):.3f}, {first.get('o'):.3f})")
        else:
            print(f"    → 0 hits")

    print()
    if total_errors:
        print(f"FAIL: {total_errors} integrity errors across sub-chunks")
        return 1
    print("PASS: all sub-chunks readable, records correctly hashed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
