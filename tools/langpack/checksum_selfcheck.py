#!/usr/bin/env python3
"""Checksum self-check of merged ZIMs against their recipes.

    checksum_selfcheck.py RECIPE.json MERGED.zim [RECIPE2.json MERGED2.zim ...]

For each pair: file size == recipe size; MD5 over [0, size-16) == the
trailing 16 bytes == recipe "md5"; MD5 of the whole file == recipe
"file_md5"; libzim Archive.check() (its own MD5 + integrity) is True; the
header UUID == recipe uuid.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

from libzim.reader import Archive


def main():
    args = sys.argv[1:]
    fails = 0
    for rp, zp in zip(args[0::2], args[1::2]):
        r = json.load(open(rp))
        size = os.path.getsize(zp)
        h = hashlib.md5(usedforsecurity=False)
        with open(zp, "rb") as fh:
            left = size - 16
            while left:
                b = fh.read(min(16 << 20, left))
                h.update(b)
                left -= len(b)
            tail = fh.read(16)
        inner = h.hexdigest()
        whole = h.copy()
        whole.update(tail)
        with open(zp, "rb") as fh:
            fh.seek(8)
            hdr_uuid = str(uuid.UUID(bytes=fh.read(16)))
        a = Archive(Path(zp))
        checks = {
            "size == recipe": size == r["size"],
            "md5[0,size-16) == trailing 16 bytes": inner == tail.hex(),
            "== recipe md5": inner == r["md5"],
            "whole-file md5 == recipe file_md5": whole.hexdigest() == r.get("file_md5"),
            "header uuid == recipe uuid": hdr_uuid == r["uuid"],
            "libzim Archive.check()": a.check(),
        }
        ok = all(checks.values())
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {os.path.basename(zp)}  size={size} md5={inner} "
              + " ".join(f"[{k}: {'ok' if v else 'FAIL'}]" for k, v in checks.items()))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
