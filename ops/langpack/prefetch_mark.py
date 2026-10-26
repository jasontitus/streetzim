#!/usr/bin/env python3
"""A viewer-swapped base's prefetch mark, from its parent's.

    prefetch_mark.py BASE PACKS_ROOT [--langs L,L,...]

ship_packs.sh step 2 marks a base whose dumps, title maps and Wikidata
text are in the cache: PACKS_ROOT/<base>/prefetched, the pack languages,
then the file's size and mtime. A viewer swap
(tools/langpack/swap_base_viewer.py) changes the viewer alone, and its
<base>.parent.json names the parent, whose mark counts for it too. This
writes the base's mark from the parent's (its languages, this file's size
and mtime) and exits 0; it exits 1 when there is none to take: no sidecar,
no parent mark, other pack languages (--langs), or a parent file beside the
base that the parent's mark does not name (a mark left for an older file),
or a sidecar that does not name this file (its child_uuid, the base's
header UUID: a sidecar left beside a file rebuilt under the same name).
"""
from __future__ import annotations

import json
import os
import sys


def file_id(path: str) -> str:
    """As `stat -c '%s %.9Y'`: the size, the mtime with nine decimals."""
    st = os.stat(path)
    return f"{st.st_size} {st.st_mtime_ns // 10**9}.{st.st_mtime_ns % 10**9:09d}"


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 4) or (len(argv) == 4 and argv[2] != "--langs"):
        print(__doc__, file=sys.stderr)
        return 2
    base, root = argv[0], argv[1]
    langs = argv[3] if len(argv) == 4 else None
    name = os.path.basename(base)
    try:
        side = json.load(open(os.path.realpath(base) + ".parent.json", encoding="utf-8"))
        parent = str(side.get("parent_file") or "")
        child = str(side.get("child_uuid") or "").replace("-", "").lower()
        with open(base, "rb") as fh:
            head = fh.read(24)
    except (OSError, ValueError, AttributeError):
        return 1
    if len(head) < 24 or head[:4] != b"ZIM\x04" or head[8:24].hex() != child:
        return 1
    if not parent.endswith(".zim") or "/" in parent or parent == name:
        return 1
    try:
        lines = open(os.path.join(root, parent[:-4], "prefetched"), encoding="utf-8").read().splitlines()
    except OSError:
        return 1
    if len(lines) < 2 or not lines[0] or (langs is not None and lines[0] != langs):
        return 1
    beside = os.path.join(os.path.dirname(os.path.realpath(base)), parent)
    if os.path.exists(beside) and lines[1] != file_id(beside):
        return 1
    mark = os.path.join(root, name[:-4], "prefetched")
    os.makedirs(os.path.dirname(mark), exist_ok=True)
    tmp = f"{mark}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(f"{lines[0]}\n{file_id(base)}\n")
    os.replace(tmp, mark)
    print(f"prefetch: {name} takes the mark of {parent}, its viewer swap's parent ({lines[0]})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
