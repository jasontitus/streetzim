#!/usr/bin/env python3
"""The published base with this checkout's viewer, for the language
versions' gates and recipes (ops/langpack/ship_packs.sh).

    viewer_base.py BASE.zim OUT_DIR [--viewer-dir resources/viewer]

Prints the copy's path, OUT_DIR/<BASE's file name>: a reflink copy of BASE
that cloud/patch_viewer_inplace.py patched in place. It has the same name,
size and UUID as BASE, and the same bytes outside the three viewer slots
and the trailing checksum, which are the ranges a recipe's base.mask leaves
out. So a recipe built from the copy reads the live base for everything
else and carries the slots inline, and a visitor's file has this viewer.
The copy's MD5 is not the base's, so the merged UUID (merge_zim.derive_uuid)
changes too, and Kiwix takes the file for a new book instead of the old one.

A copy made for the same base file (size, mtime, inode) and the same viewer
files is reused. When <BASE>.parent.json names BASE, a sidecar naming the
copy (its MD5) is kept beside it, so packs built for the base's parent
still fit. Copies of bases that are gone are removed (OUT_DIR's parent
holds one folder per base), as are copies half-made by a run that died.

Exit 0 with the path on stdout; 3 when BASE has no viewer slots (built
before 2026-09-20: its versions keep the base's viewer); 1 anything else.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from cloud.viewer_slots import VIEWER_FILES, viewer_code  # noqa: E402
from tools.langpack.merge_zim import ZimReader, file_md5  # noqa: E402
from tools.langpack.recipe2 import viewer_slots, zim_viewer_code  # noqa: E402

NO_SLOTS = 3


class ViewerBaseError(Exception):
    pass


class NoSlots(ViewerBaseError):
    pass


def viewer_sha(vdir: str) -> str:
    h = hashlib.sha256()
    for name in VIEWER_FILES:
        with open(os.path.join(vdir, name), "rb") as fh:
            h.update(name.encode() + b"\0" + fh.read() + b"\0")
    return h.hexdigest()


def identity(path: str) -> list[int]:
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns, st.st_ino]


TMP = re.compile(r"^\..+\.(\d+)\.tmp(\.parent\.json)?$")     # a copy being made: .<name>.<pid>.tmp


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def prune(root: str, keep: str) -> None:
    """Under ROOT (one folder per base): the folders of bases that are gone
    (a rebuild's new file replaced them, and a reflink copy alone would pin
    the whole file), and copies left half-made by a run that died."""
    try:
        dirs = os.listdir(root)
    except OSError:
        return
    for d in dirs:
        p = os.path.join(root, d)
        if not os.path.isdir(p):
            continue
        for f in os.listdir(p):
            m = TMP.match(f)
            if m and not alive(int(m.group(1))):
                os.remove(os.path.join(p, f))
        if os.path.realpath(p) == os.path.realpath(keep):
            continue
        for f in os.listdir(p):
            if not f.endswith(".viewer.json"):
                continue
            try:
                bp = json.load(open(os.path.join(p, f))).get("base_path")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(bp, str) and bp and not os.path.exists(bp):
                shutil.rmtree(p, ignore_errors=True)
                break


def sync_sidecar(base: str, out: str, uuid: bytes) -> None:
    """The copy's lineage is the base's: <BASE>.parent.json, when it names
    BASE (its UUID and MD5, as merge_zim.base_lineage reads it), again beside
    the copy naming the copy; else none. Done on every call, a reused copy
    included: the base's sidecar can come or go without the base changing."""
    side, oside = base + ".parent.json", out + ".parent.json"
    want = None
    try:
        d = json.load(open(side))
        if (isinstance(d, dict) and str(d.get("child_uuid") or "").replace("-", "").lower() == uuid.hex()
                and d.get("child_md5") == file_md5(base)):
            want = dict(d, child_md5=file_md5(out), child_file=os.path.basename(out))
    except (OSError, ValueError):
        want = None
    if want is None:
        if os.path.exists(oside):
            os.remove(oside)
        return
    try:
        if json.load(open(oside)) == want:
            return
    except (OSError, ValueError):
        pass
    tmp = f"{oside}.{os.getpid()}.part"
    with open(tmp, "w") as fh:
        json.dump(want, fh, indent=1)
    os.replace(tmp, oside)


def make(base: str, out_dir: str, vdir: str) -> str:
    base = os.path.realpath(base)
    name = os.path.basename(base)
    out = os.path.join(out_dir, name)
    stamp_path = out + ".viewer.json"
    want = {"base": identity(base), "viewer_sha": viewer_sha(vdir)}
    code = viewer_code(open(os.path.join(vdir, "index.html"), "rb").read())
    if not code:
        raise ViewerBaseError(f"{vdir}/index.html has no viewer code (SZ_VIEWER_BUILD): build it with tools/build_viewer.py")
    prune(os.path.dirname(os.path.abspath(out_dir)), out_dir)
    uuid = ZimReader(base).header.uuid
    try:
        stamp = json.load(open(stamp_path))
        if {k: stamp.get(k) for k in want} == want and stamp.get("copy") == identity(out):
            sync_sidecar(base, out, uuid)
            return out
    except (OSError, ValueError):
        pass
    slots = viewer_slots(base)
    if len(slots) != len(VIEWER_FILES):
        raise NoSlots(f"{name} has {len(slots)} of {len(VIEWER_FILES)} viewer slots: built before "
                      "viewer slots (2026-09-20); its versions keep the base's viewer")
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, f".{name}.{os.getpid()}.tmp")
    try:
        r = subprocess.run(["cp", "--reflink=auto", base, tmp], capture_output=True, text=True)
        if r.returncode:
            raise ViewerBaseError(f"cp {base}: {r.stderr.strip()}")
        os.chmod(tmp, 0o644)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "cloud", "patch_viewer_inplace.py"), tmp,
                            "--viewer-dir", vdir], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), cwd=ROOT)
        if r.returncode:
            raise ViewerBaseError(f"patch_viewer_inplace.py rc={r.returncode}: {(r.stdout + r.stderr).strip()[-800:]}")
        # what a recipe relies on: the same size, UUID and slot ranges, and this viewer in the slots
        if os.path.getsize(tmp) != os.path.getsize(base) or ZimReader(tmp).header.uuid != uuid:
            raise ViewerBaseError(f"the patched copy of {name} changed size or UUID")
        if viewer_slots(tmp) != slots:
            raise ViewerBaseError(f"the patched copy of {name} has other viewer slots than the base")
        got = zim_viewer_code(tmp)
        if got != code:
            raise ViewerBaseError(f"the patched copy of {name} has viewer {got or '(none)'}, not {code}")
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    sync_sidecar(base, out, uuid)
    with open(stamp_path + ".tmp", "w") as fh:
        json.dump(dict(want, copy=identity(out), code=code, base_path=base), fh)
    os.replace(stamp_path + ".tmp", stamp_path)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("base")
    ap.add_argument("out_dir")
    ap.add_argument("--viewer-dir", default=os.path.join(ROOT, "resources", "viewer"))
    a = ap.parse_args(argv)
    try:
        print(make(a.base, a.out_dir, os.path.abspath(a.viewer_dir)), flush=True)
    except ViewerBaseError as e:
        print(f"viewer_base: {e}", file=sys.stderr, flush=True)
        return NO_SLOTS if isinstance(e, NoSlots) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
