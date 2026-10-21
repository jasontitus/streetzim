#!/usr/bin/env python3
"""The seal on a region's checked site files, and the atomic swap of folders.

    checked.py write DIR --summary LINE   after site_check.py passed on DIR
    checked.py verify DIR                 exit 0 only if DIR is exactly what was checked
    checked.py swap NEW DEST              DEST := NEW in one step (the old DEST is
                                          left at NEW; a missing DEST is a rename)

DIR is make_site.py's output for one region (recipes/<region>/).
checked.json there records the SHA-256 of every other file in DIR and
site_check.py's summary line. ship_packs.sh writes it only after the check
passed; publish_site.sh refuses a folder whose files are not exactly those.

swap: renameat2(RENAME_EXCHANGE) on Linux, so a reader (the site deploy)
never sees DEST missing or half written; elsewhere, or where the
filesystem refuses, two renames.
"""
from __future__ import annotations

import ctypes
import datetime
import hashlib
import json
import os
import sys

SEAL = "checked.json"


def digests(d: str) -> dict[str, str]:
    out = {}
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if name == SEAL or not os.path.isfile(p):
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 20), b""):
                h.update(b)
        out[name] = h.hexdigest()
    return out


def write(d: str, summary: str) -> None:
    seal = {"checked": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "site_check": summary, "files": digests(d)}
    tmp = os.path.join(d, SEAL + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(seal, fh, indent=1)
    os.replace(tmp, os.path.join(d, SEAL))


def verify(d: str) -> tuple[bool, str]:
    try:
        seal = json.load(open(os.path.join(d, SEAL)))
    except (OSError, ValueError) as e:
        return False, f"no readable {SEAL} in {d} ({e}): not checked by ship_packs.sh"
    want, got = seal.get("files") or {}, digests(d)
    if not want:
        return False, f"{SEAL} lists no files"
    if set(want) != set(got):
        return False, f"files differ from the checked ones: {sorted(set(want) ^ set(got))[:5]}"
    bad = [n for n in want if want[n] != got[n]]
    if bad:
        return False, f"changed since the check: {bad[:5]}"
    return True, str(seal.get("site_check", ""))


def swap(new: str, dest: str) -> None:
    if not os.path.lexists(dest):
        os.rename(new, dest)
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        f = libc.renameat2
        f.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        AT_FDCWD, RENAME_EXCHANGE = -100, 2
        if f(AT_FDCWD, os.fsencode(new), AT_FDCWD, os.fsencode(dest), RENAME_EXCHANGE) == 0:
            return
    except (AttributeError, OSError):
        pass
    old = dest + ".old"
    if os.path.lexists(old):
        raise SystemExit(f"checked.py: {old} is in the way")
    os.rename(dest, old)
    os.rename(new, dest)
    os.rename(old, new)


def main(argv: list[str] | None = None) -> int:
    a = sys.argv[1:] if argv is None else argv
    if len(a) >= 2 and a[0] == "write":
        summary = a[a.index("--summary") + 1] if "--summary" in a else ""
        write(a[1], summary)
        return 0
    if len(a) == 2 and a[0] == "verify":
        ok, why = verify(a[1])
        print(why, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if len(a) == 3 and a[0] == "swap":
        swap(a[1], a[2])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
