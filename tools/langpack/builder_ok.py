#!/usr/bin/env python3
"""Decide whether a pack build may proceed to pack_gate.py (review r2 #4).

    builder_ok.py BUILDER_RC STATS.json STAGED_PACK.zim RUN_MARKER
        -> exit 0 = proceed, 1 = reject (prints why)

The lp-pack builder exits non-zero when its own gate fails. Its gate runs
zimru's zimcheck, which misreads hrefs with HTML entities (&amp;, &#39;) as
dangling (url_internal), so that one kind of failure is excused: the official
zimcheck in pack_gate.py re-checks every link. Anything else the builder
reports (geo-index/article mismatch, metadata, Xapian, stray paths, pack.json,
checksum, a zimcheck that gave no JSON, a crash without stats) rejects the pack.

The stats file must be THIS run's (review r3): build_packs.sh deletes it and
touches RUN_MARKER before the builder starts, so a stats file left by an
earlier build (which may hold only url_internal findings) can never excuse a
crash. Required: stats mtime >= RUN_MARKER mtime, the staged pack exists,
stats mtime >= the pack's mtime (the builder writes stats after the pack and
its check), and stats zim_bytes == the staged pack's size.
"""
import json
import os
import sys


def fresh(stats_path: str, pack_path: str, marker_path: str | None) -> tuple[bool, str]:
    try:
        s = os.stat(stats_path)
    except OSError:
        return False, f"no stats ({stats_path})"
    if marker_path is not None:
        try:
            m = os.stat(marker_path)
        except OSError:
            return False, f"no run marker ({marker_path})"
        if s.st_mtime_ns < m.st_mtime_ns:
            return False, "stats file predates this run (stale)"
    try:
        p = os.stat(pack_path)
    except OSError:
        return False, f"no staged pack ({pack_path})"
    if s.st_mtime_ns < p.st_mtime_ns:
        return False, "stats file is older than the staged pack"
    return True, ""


def decide(rc: int, stats_path: str, pack_path: str, marker_path: str | None) -> tuple[bool, str]:
    ok, why = fresh(stats_path, pack_path, marker_path)
    if not ok:
        return False, f"rc={rc}: {why}"
    try:
        st = json.load(open(stats_path))
    except (OSError, ValueError):
        return False, f"rc={rc} and no readable stats ({stats_path})"
    if st.get("zim_bytes") != os.path.getsize(pack_path):
        return False, f"rc={rc}: stats zim_bytes {st.get('zim_bytes')} != staged pack {os.path.getsize(pack_path)} B"
    chk = st.get("check") or {}
    problems = chk.get("problems")
    if rc == 0:
        return (chk.get("ok") is True), ("ok" if chk.get("ok") is True else f"rc=0 but check not ok: {problems}")
    if rc != 1 or not isinstance(problems, list) or not problems:
        return False, f"rc={rc}, problems={problems}"
    findings = chk.get("zimcheck_findings") or {}
    only_links = all(isinstance(p, str) and p.startswith("zimcheck: url_internal:") for p in problems)
    if only_links and set(findings) == {"url_internal"} and chk.get("zimcheck") != "error":
        return True, f"excused: {findings['url_internal']} zimru url_internal findings (re-checked by pack_gate)"
    return False, f"rc=1 for more than zimru link findings: findings={findings} problems={[p[:80] for p in problems[:3]]}"


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit("usage: builder_ok.py BUILDER_RC STATS.json STAGED_PACK.zim RUN_MARKER")
    ok, why = decide(int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4])
    print(("PROCEED " if ok else "REJECT ") + why)
    sys.exit(0 if ok else 1)
