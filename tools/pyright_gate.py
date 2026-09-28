#!/usr/bin/env python3
"""Run pyright (configured in pyproject.toml) and fail only on NEW findings.

    python tools/pyright_gate.py            # CI: compare with the baseline
    python tools/pyright_gate.py --update   # after fixing findings: shrink it

pyright-baseline.json lists the findings the code had when type checking
started, by file, rule and message (not line, so unrelated edits don't
disturb it). A finding beyond those counts fails the gate, and so does a
fixed one until the baseline is updated, so the baseline only shrinks
(--update refuses to add findings). The strict modules have no baseline
entries, so any finding there fails. The gate also fails when pyright did
not really check the tree: an abnormal exit, a configuration error, fewer
files analysed than the configuration covers, or a strict entry that no
longer exists.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "pyright-baseline.json"


def expected_files() -> tuple[int, list[str]]:
    """How many .py files the configuration covers, and missing strict entries."""
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pyright"]
    files: set[Path] = set()
    for inc in cfg["include"]:
        p = ROOT / inc
        files |= set(p.rglob("*.py")) if p.is_dir() else ({p} if p.exists() else set())
    missing = [s for s in cfg.get("strict", []) if not (ROOT / s).exists()]
    return len(files), missing


def findings(pyright: str) -> Counter[str]:
    out = subprocess.run([pyright, "--outputjson", "--pythonpath", sys.executable],
                         cwd=ROOT, capture_output=True, text=True)
    try:
        report = json.loads(out.stdout)
    except json.JSONDecodeError:
        sys.exit(f"pyright did not produce JSON:\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}")
    # 0 = clean, 1 = findings; anything else is pyright itself failing.
    if out.returncode not in (0, 1):
        sys.exit(f"pyright exited {out.returncode}:\n{out.stderr[-2000:]}")
    if "config" in out.stderr.lower() or "error" in out.stderr.lower():
        sys.exit(f"pyright reported a configuration problem:\n{out.stderr[-2000:]}")
    want, missing = expected_files()
    if missing:
        sys.exit(f"strict entries in pyproject.toml that do not exist: {missing}")
    analyzed = report.get("summary", {}).get("filesAnalyzed", 0)
    if analyzed < want:
        sys.exit(f"pyright analysed {analyzed} files; the configuration covers {want}")
    c: Counter[str] = Counter()
    for d in report.get("generalDiagnostics", []):
        if d.get("severity") != "error":
            continue
        f = str(Path(d["file"]).resolve().relative_to(ROOT))
        c[f"{f} | {d.get('rule', '-')} | {d['message'].splitlines()[0]}"] += 1
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    ap.add_argument("--update", action="store_true",
                    help="rewrite the baseline after fixing findings")
    ap.add_argument("--accept-new", action="store_true",
                    help="with --update: also record new findings (avoid)")
    ap.add_argument("--pyright", default="pyright")
    a = ap.parse_args()
    now = findings(a.pyright)
    base = Counter(json.loads(BASELINE.read_text())) if BASELINE.exists() else Counter()
    if a.update:
        added = now - base
        if added and base and not a.accept_new:
            for k in sorted(added):
                print(f"NEW  {k}")
            print("refusing to add new findings to the baseline; fix them "
                  "(or pass --accept-new)")
            return 1
        BASELINE.write_text(json.dumps(dict(sorted(now.items())), indent=1,
                                       ensure_ascii=False) + "\n")
        print(f"wrote {BASELINE.name}: {sum(now.values())} findings")
        return 0
    new = now - base
    fixed = base - now
    for k, n in sorted(new.items()):
        print(f"NEW  {k}" + (f"  (x{n})" if n > 1 else ""))
    for k, n in sorted(fixed.items()):
        print(f"FIXED {k}" + (f"  (x{n})" if n > 1 else ""))
    if new:
        print(f"FAIL: {sum(new.values())} new pyright finding(s)")
        return 1
    if fixed:
        print(f"FAIL: {sum(fixed.values())} baseline finding(s) fixed; shrink the "
              "baseline: python tools/pyright_gate.py --update")
        return 1
    print(f"ok: no new pyright findings ({sum(now.values())} in the baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
