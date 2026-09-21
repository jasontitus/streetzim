#!/usr/bin/env python3
"""Prove a derived ZIM differs from its source only as the recipe intended.

    python3 cloud/verify_derived.py SRC.zim DST.zim [--expect-dropped PREFIX ...]

Checks, with python-libzim (an independent reader from cloud/zimfmt.py):
  * Archive.check() and the MD5 trailer
  * every source entry not under an expected-dropped prefix is present in DST
    with byte-identical content (except the files the derive rewrites)
  * every entry under an expected-dropped prefix is absent
  * main page resolves, fulltext + title indexes present, a search runs
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REWRITTEN = {"map-config.json", "streetzim-meta.json", "wiki-geo-index.json"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("dst")
    ap.add_argument("--expect-dropped", nargs="*", default=[], metavar="PREFIX")
    ap.add_argument("--query", default="bridge")
    ap.add_argument("--expect-stripped-addresses", action="store_true",
                    help="search-data leaves may differ, but only by losing address records")
    a = ap.parse_args()
    from libzim.reader import Archive
    from libzim.search import Query, Searcher
    from libzim.suggestion import SuggestionSearcher
    from cloud.zimfmt import verify_checksum
    src, dst = Archive(a.src), Archive(a.dst)
    ok = True
    def rep(name, cond, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'OK  ' if cond else 'FAIL'} {name} {extra}")
    rep("Archive.check()", dst.check())
    rep("md5 trailer", verify_checksum(a.dst))
    rep("main page", dst.has_main_entry and (dst.main_entry.get_redirect_entry().path
        if dst.main_entry.is_redirect else dst.main_entry.path) == "index.html")
    rep("fulltext index", dst.has_fulltext_index)
    rep("title index", dst.has_title_index)
    same = diff = missing = dropped = present = 0
    for i in range(src.entry_count):
        e = src._get_entry_by_id(i)
        p = e.path
        if any(p.startswith(pre) for pre in a.expect_dropped):
            dropped += 1
            if dst.has_entry_by_path(p):
                present += 1
            continue
        if not dst.has_entry_by_path(p):
            missing += 1
            if missing <= 5: print("    missing:", p)
            continue
        if e.is_redirect:
            continue
        if p in REWRITTEN:
            continue
        if a.expect_stripped_addresses and p.startswith("search-data/"):
            if p == "search-data/manifest.json":
                continue
            import json
            try:
                sa = json.loads(bytes(e.get_item().content))
                da = json.loads(bytes(dst.get_entry_by_path(p).get_item().content))
            except Exception:  # noqa: BLE001
                diff += 1; print("    unparsable:", p); continue
            def _t(rec): return rec.get("t") or rec.get("type")
            want = [r_ for r_ in sa if _t(r_) != "addr"]
            if da == want:
                same += 1
            else:
                diff += 1
                if diff <= 5: print("    leaf not (source minus addresses):", p)
            continue
        if bytes(e.get_item().content) == bytes(dst.get_entry_by_path(p).get_item().content):
            same += 1
        else:
            diff += 1
            if diff <= 5: print("    differs:", p)
    rep("kept entries identical" + (" (search leaves: source minus addresses)" if a.expect_stripped_addresses else ""),
        diff == 0 and missing == 0, f"({same} identical, {diff} differ, {missing} missing)")
    rep("dropped entries absent", present == 0, f"({dropped} expected dropped, {present} still present)")
    for k in ("Title", "Name", "Counter"):
        print(f"       {k} = {dst.get_metadata(k)[:90]!r}")
    print(f"       uuid {'changed' if src.uuid != dst.uuid else 'PRESERVED'}: {dst.uuid}")
    try:
        s = Searcher(dst).search(Query().set_query(a.query))
        n = s.getEstimatedMatches()
        rep(f"fulltext search {a.query!r}", n >= 0, f"({n} matches)")
        g = SuggestionSearcher(dst).suggest(a.query[:3].title())
        rep("title suggestions", True, f"({g.getEstimatedMatches()} matches)")
    except Exception as ex:  # noqa: BLE001
        rep("search", False, str(ex))
    # Second, independent reader: zimru's zimcheck (Rust), when a binary is
    # around. Two implementations agreeing on the file is the real defence
    # against a format detail this tool got subtly wrong.
    zimcheck = os.environ.get("ZIMCHECK_BIN") or shutil.which("zimcheck") or next(
        (p for p in ("/home/user/zimru/target/release/zimcheck",
                     str(Path(__file__).resolve().parent.parent.parent / "zimru/target/release/zimcheck"))
         if os.path.exists(p)), None)
    if zimcheck:
        res = subprocess.run([zimcheck, "-A", a.dst], capture_output=True, text=True, timeout=3600)
        rep(f"zimru zimcheck -A ({Path(zimcheck).name})", res.returncode == 0,
            (res.stdout.strip().splitlines() or [""])[-1][:100])
    else:
        print("  skip zimru zimcheck (no binary; set ZIMCHECK_BIN or build ../zimru)")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
