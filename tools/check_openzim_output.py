#!/usr/bin/env python3
"""Check what a `streetzim` run left in its output folder, the way Zimfarm
would see it (used by CI after building with the openZIM-style command).

    python tools/check_openzim_output.py OUT NAME --title T [--file F] [--routing]

Checks: exactly one finished .zim and no .tmp left behind; the progress
file reached done == total; openZIM's mandatory metadata is present (Name,
Title, Description, Language, Creator, Publisher, Date, 48x48 PNG
Illustration) with the requested Name and Title; the licence never claims
the non-commercial satellite layer; routing is present when asked for.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

MANDATORY = ("Name", "Title", "Description", "Language", "Creator", "Publisher",
             "Date", "Illustration_48x48@1")


def main() -> int:
    ap = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    ap.add_argument("out")
    ap.add_argument("name")
    ap.add_argument("--title", required=True)
    ap.add_argument("--file", help="expected file name (default: <name>_<period>.zim)")
    ap.add_argument("--routing", action="store_true")
    ap.add_argument("--stats", default="task_progress.json")
    a = ap.parse_args()

    from libzim.reader import Archive
    from PIL import Image

    out = Path(a.out)
    problems = []
    zims = sorted(out.glob("*.zim"))
    leftovers = sorted(p.name for p in out.glob("*.tmp"))
    if leftovers:
        problems.append(f"left behind: {leftovers}")
    want = [out / a.file] if a.file else [p for p in zims if p.name.startswith(a.name + "_")]
    if len(want) != 1 or not want[0].exists():
        print(f"FAIL: expected one ZIM for {a.name} in {out}, found {[p.name for p in zims]}")
        return 1
    zim = want[0]

    stats = json.loads((out / a.stats).read_text())
    if not (stats.get("total", 0) > 0 and stats.get("done") == stats["total"]):
        problems.append(f"progress file not complete: {stats}")

    arc = Archive(zim)
    keys = set(arc.metadata_keys)
    missing = [k for k in MANDATORY if k not in keys]
    if missing:
        problems.append(f"missing metadata: {missing}")
    md = {k: arc.get_metadata(k) for k in keys}
    if md.get("Name", b"").decode() != a.name:
        problems.append(f"Name is {md.get('Name')!r}, want {a.name!r}")
    if md.get("Title", b"").decode() != a.title:
        problems.append(f"Title is {md.get('Title')!r}, want {a.title!r}")
    ill = md.get("Illustration_48x48@1")
    if ill:
        img = Image.open(io.BytesIO(ill))
        if (img.format, img.size) != ("PNG", (48, 48)):
            problems.append(f"illustration is {img.format} {img.size}")
    if b"NC-SA" in md.get("License", b""):
        problems.append("License claims the non-commercial satellite layer")
    cfg = json.loads(bytes(arc.get_entry_by_path("map-config.json").get_item().content))
    if a.routing and not cfg.get("hasRouting"):
        problems.append("routing was requested but map-config has no hasRouting")

    for p in problems:
        print(f"FAIL: {p}")
    if not problems:
        print(f"ok: {zim.name}: metadata, illustration, progress "
              f"({stats['done']}/{stats['total']})" + (", routing" if a.routing else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
