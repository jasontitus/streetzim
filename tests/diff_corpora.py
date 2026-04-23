"""Compare two route-corpus JSONL files line-by-line and report mismatches.

Usage:
  python -m tests.diff_corpora \\
      tests/golden/silicon-valley.jsonl \\
      tests/golden/silicon-valley-v5.jsonl

Exits 0 if every fingerprint is identical, 1 if any differ.

Interpretation of mismatches:
  - "s"/"e" diff          : pair selection desynced (bug in seed/pair-picker
                            or in the SZRG header, changes node ordering)
  - "n" diff (node seq)   : A* picked a different path — most likely the
                            edge/adj arrays deserialize to different bytes
  - "d"/"t" diff only     : dist or speed unpacking is off (should never
                            happen alone — node_sequence change implies this)
  - "g" diff              : geom_idx column differs (v5 split may have
                            moved geoms into a companion file)
  - "rd" diff only        : coalesced road labels differ but path matches
                            — class_access or name_idx column changed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_jsonl(p: Path) -> tuple[dict | None, list[dict]]:
    """Returns (header_meta, route_records). Header is None if the corpus
    has no metadata line (old format), otherwise the _meta dict."""
    out: list[dict] = []
    header: dict | None = None
    with p.open() as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if i == 0 and rec.get("_meta"):
                header = rec
                continue
            out.append(rec)
    return header, out


def diff_record(a: dict, b: dict) -> list[str]:
    """Return a list of human-readable mismatch messages for two records."""
    msgs: list[str] = []
    for k in ("s", "e", "unreachable", "d", "t", "n", "g", "rd"):
        if a.get(k) != b.get(k):
            if k in ("n", "g", "rd"):
                # Collections — point to the first diverging index.
                la, lb = a.get(k) or [], b.get(k) or []
                first = -1
                for i, (x, y) in enumerate(zip(la, lb)):
                    if x != y:
                        first = i
                        break
                if first < 0:
                    first = min(len(la), len(lb))
                msgs.append(
                    f"  field {k!r} len={len(la)}|{len(lb)} "
                    f"first_diff_at={first} "
                    f"A={la[first] if first < len(la) else '<missing>'} "
                    f"B={lb[first] if first < len(lb) else '<missing>'}"
                )
            else:
                msgs.append(f"  field {k!r}: A={a.get(k)!r} B={b.get(k)!r}")
    return msgs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("golden", help="Reference corpus (typically v4)")
    ap.add_argument("candidate", help="Corpus to validate (typically v5)")
    ap.add_argument("--max-report", type=int, default=20,
                    help="Stop printing after this many divergent pairs")
    ap.add_argument("--epsilon-m", type=float, default=0.0,
                    help="Treat distance diffs below this many meters as equal "
                         "(only applied when node sequence matches exactly)")
    args = ap.parse_args()

    a_meta, a_records = load_jsonl(Path(args.golden))
    b_meta, b_records = load_jsonl(Path(args.candidate))

    # Sanity: the two corpora should have been built from the same seed +
    # pair-selection knobs, or the pair lists don't line up.
    if a_meta and b_meta:
        for k in ("seed", "pairs", "min_dist_m", "max_dist_m", "max_pops"):
            if a_meta.get(k) != b_meta.get(k):
                print(f"WARN: meta field {k!r} differs: "
                      f"golden={a_meta.get(k)!r} candidate={b_meta.get(k)!r}",
                      file=sys.stderr)

    if len(a_records) != len(b_records):
        print(f"FAIL: lengths differ — golden={len(a_records)} "
              f"candidate={len(b_records)}", file=sys.stderr)
        return 1

    bad = 0
    shown = 0
    for i, (a, b) in enumerate(zip(a_records, b_records)):
        # Fast path — equal after round-trip through JSON?
        if a == b:
            continue
        # Allow tiny float drift when node sequences match.
        if (args.epsilon_m > 0
                and a.get("n") == b.get("n") and a.get("rd") == b.get("rd")
                and a.get("g") == b.get("g")
                and abs((a.get("d") or 0) - (b.get("d") or 0)) < args.epsilon_m
                and abs((a.get("t") or 0) - (b.get("t") or 0)) < args.epsilon_m):
            continue
        bad += 1
        if shown < args.max_report:
            print(f"[diff] pair {i} "
                  f"({a.get('s')}→{a.get('e')}):", file=sys.stderr)
            for m in diff_record(a, b):
                print(m, file=sys.stderr)
            shown += 1

    total = len(a_records)
    if bad == 0:
        print(f"PASS: {total} pairs identical")
        return 0
    print(f"FAIL: {bad}/{total} pairs differ "
          f"({bad * 100 / total:.2f}%)  — showed up to {args.max_report}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
