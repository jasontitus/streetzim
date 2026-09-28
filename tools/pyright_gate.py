#!/usr/bin/env python3
"""Run pyright (configured in pyproject.toml) and fail only on NEW findings.

    python tools/pyright_gate.py            # CI: compare with the baseline
    python tools/pyright_gate.py --update   # after fixing findings: shrink it

pyright-baseline.json lists the findings the code had when type checking
started, by file, rule and message (not line, so unrelated edits don't
disturb it). A finding beyond those counts fails the gate; a fixed one is
reported so the baseline can be updated. The strict modules have no
baseline entries, so any finding there fails.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "pyright-baseline.json"


def findings(pyright: str) -> Counter[str]:
    out = subprocess.run([pyright, "--outputjson", "--pythonpath", sys.executable],
                         cwd=ROOT, capture_output=True, text=True)
    try:
        report = json.loads(out.stdout)
    except json.JSONDecodeError:
        sys.exit(f"pyright did not produce JSON:\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}")
    c: Counter[str] = Counter()
    for d in report.get("generalDiagnostics", []):
        if d.get("severity") != "error":
            continue
        f = str(Path(d["file"]).resolve().relative_to(ROOT))
        c[f"{f} | {d.get('rule', '-')} | {d['message'].splitlines()[0]}"] += 1
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--update", action="store_true", help="rewrite the baseline")
    ap.add_argument("--pyright", default="pyright")
    a = ap.parse_args()
    now = findings(a.pyright)
    if a.update:
        BASELINE.write_text(json.dumps(dict(sorted(now.items())), indent=1,
                                       ensure_ascii=False) + "\n")
        print(f"wrote {BASELINE.name}: {sum(now.values())} findings")
        return 0
    base = Counter(json.loads(BASELINE.read_text())) if BASELINE.exists() else Counter()
    new = now - base
    fixed = base - now
    for k, n in sorted(new.items()):
        print(f"NEW  {k}" + (f"  (x{n})" if n > 1 else ""))
    if fixed:
        print(f"{sum(fixed.values())} baseline finding(s) fixed; run "
              "python tools/pyright_gate.py --update")
    if new:
        print(f"FAIL: {sum(new.values())} new pyright finding(s)")
        return 1
    print(f"ok: no new pyright findings ({sum(now.values())} in the baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
