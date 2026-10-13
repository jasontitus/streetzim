#!/usr/bin/env python3
"""Whether an archive.org item already holds a file with a given MD5.

    ia metadata streetzim-<id> | archive_has_file.py <file name> <md5>

Exit 0 when the item's metadata (JSON on stdin) lists <file name> with that
MD5, 1 when it does not (not listed, another MD5, or metadata that is not
an item's), 2 on a usage error.

upload_validated.sh asks this before `ia upload`. `ia upload --checksum`
skips a file only when the item has NO pending task (internetarchive's
item.py), and archive.org's derive queue can hold a task for a day: a
rerun to finish a "listing pending" upload re-sent the whole ZIM and left
a permanent history/ copy. A listed file with the same MD5 is the same
file, whatever tasks are queued, so it is not sent again.
"""
from __future__ import annotations

import json
import re
import sys

_MD5 = re.compile(r"^[0-9a-f]{32}$")


def has_file(meta: object, name: str, md5: str) -> bool:
    """`meta` (an item's metadata) lists `name` with MD5 `md5`."""
    md5 = md5.strip().lower()
    if not _MD5.match(md5) or not isinstance(meta, dict):
        return False
    files = meta.get("files")
    if not isinstance(files, list):
        return False
    for f in files:
        if isinstance(f, dict) and f.get("name") == name:
            return str(f.get("md5", "")).strip().lower() == md5
    return False


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        meta = json.load(sys.stdin)
    except ValueError:
        return 1
    return 0 if has_file(meta, args[0], args[1]) else 1


if __name__ == "__main__":
    sys.exit(main())
