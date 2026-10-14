#!/usr/bin/env python3
"""Make a base with a new viewer and a fresh UUID, keeping everything else.

    swap_base_viewer.py SRC.zim DST.zim VIEWER_EXPORT_DIR REV [--allow-full-copy]

1. DST = reflink copy of SRC (XFS, Btrfs: no data copied; SRC is never
   written). Where the filesystem cannot reflink, it stops, unless
   --allow-full-copy says to copy every byte (warned loudly: slow, and the
   disk holds the base twice).
2. VIEWER_EXPORT_DIR/cloud/patch_viewer_inplace.py overwrites the three
   viewer slots (index.html, places.html, routing-worker.js) in DST with
   VIEWER_EXPORT_DIR/resources/viewer (a `git archive` of the branch, or
   this checkout: pass the repository root).
3. A fresh UUID, derived from (old UUID, viewer revision) so a rerun gives
   the same file, then the MD5 is recomputed.
4. Entry-by-entry comparison: same keys, same dirents; every blob byte-
   identical except the three viewer files; header identical except UUID
   and checksum. Plus Archive.check().
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid as uuidlib
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from libzim.reader import Archive  # noqa: E402

from tools.langpack.zimfmt import ZimReader  # noqa: E402

NS = uuidlib.UUID("2d6c1b6e-6a0f-4f1e-9b0e-6b9a3f1f5a13")
PY = sys.executable
VIEWER = {b"index.html", b"places.html", b"routing-worker.js"}


def md5_body(path: str) -> bytes:
    size = os.path.getsize(path)
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        left = size - 16
        while left:
            b = fh.read(min(16 << 20, left))
            h.update(b)
            left -= len(b)
    return h.digest()


def md5_whole(path: str) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        while b := fh.read(16 << 20):
            h.update(b)
    return h.hexdigest()


def copy_base(src: str, dst: str, allow_full: bool, cp: str = "cp") -> str:
    """DST as a reflink of SRC; a full copy only when allowed. Returns how."""
    if os.path.exists(dst):
        os.remove(dst)
    if subprocess.run([cp, "--reflink=always", src, dst], capture_output=True).returncode == 0:
        return "reflink"
    if os.path.exists(dst):
        os.remove(dst)
    if not allow_full:
        raise SystemExit(f"swap_base_viewer: cannot reflink {src} to {dst} on this filesystem; "
                         "pass --allow-full-copy to copy every byte instead")
    print(f"WARNING: swap_base_viewer: no reflink here; making a FULL copy of {src} "
          f"({os.path.getsize(src) / 1e9:.1f} GB)", file=sys.stderr, flush=True)
    subprocess.run([cp, src, dst], check=True)
    return "full"


def main():
    args = [x for x in sys.argv[1:] if x != "--allow-full-copy"]
    allow_full = len(args) != len(sys.argv) - 1
    src, dst, vexp, rev = args[:4]
    src, dst, vexp = (os.path.abspath(x) for x in (src, dst, vexp))
    t0 = time.perf_counter()
    how = copy_base(src, dst, allow_full)
    os.chmod(dst, 0o644)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([PY, os.path.join(vexp, "cloud", "patch_viewer_inplace.py"), dst,
                        "--viewer-dir", os.path.join(vexp, "resources", "viewer")],
                       capture_output=True, text=True, env=env, cwd=vexp)
    print(r.stdout.strip()[-800:], r.stderr.strip()[-400:], sep="\n")
    if r.returncode:
        raise SystemExit(f"patch_viewer_inplace failed rc={r.returncode}")
    old_uuid = ZimReader(src).header.uuid
    new_uuid = uuidlib.uuid5(NS, f"{old_uuid.hex()}:viewer:{rev}").bytes
    with open(dst, "r+b") as fh:
        fh.seek(8)
        fh.write(new_uuid)
    digest = md5_body(dst)
    with open(dst, "r+b") as fh:
        fh.seek(os.path.getsize(dst) - 16)
        fh.write(digest)

    # ---- comparison -------------------------------------------------------------
    za, zb = ZimReader(src), ZimReader(dst)
    ha, hb = za.header, zb.header
    hdr_same = all(getattr(ha, f) == getattr(hb, f) for f in ha.__dataclass_fields__ if f != "uuid")
    da, db_ = list(za.iter_dirents()), list(zb.iter_dirents())
    dirents_same = len(da) == len(db_) and all(
        (x.key, x.mime, x.title_raw, x.cluster, x.blob, x.target, x.offset) ==
        (y.key, y.mime, y.title_raw, y.cluster, y.blob, y.target, y.offset) for x, y in zip(da, db_))
    aa, ab = Archive(Path(src)), Archive(Path(dst))
    differ, n = [], 0
    for x in da:
        if x.is_redirect:
            continue
        n += 1
        if x.ns == ord("C"):
            ca = bytes(aa.get_entry_by_path(x.path.decode()).get_item().content)
            cb = bytes(ab.get_entry_by_path(x.path.decode()).get_item().content)
        else:
            assert x.cluster is not None and x.blob is not None
            ca, cb = za.blob_bytes(x.cluster, x.blob), zb.blob_bytes(x.cluster, x.blob)
        if ca != cb:
            differ.append(x.path.decode())
    # bytes outside the three slots: compare the whole files minus header uuid / md5 / slot ranges
    res = {
        "copy": how, "src": src, "dst": dst, "size": os.path.getsize(dst),
        "old_uuid": str(uuidlib.UUID(bytes=old_uuid)), "new_uuid": str(uuidlib.UUID(bytes=new_uuid)),
        "header_same_except_uuid": hdr_same, "dirents_same": dirents_same, "items_compared": n,
        "items_differing": differ, "only_viewer_differs": {d.encode() for d in differ} <= VIEWER,
        "archive_check": ab.check(), "viewer_has_wikipacks": b"WIKI_PACKS" in bytes(
            ab.get_entry_by_path("index.html").get_item().content),
        "old_viewer_has_wikipacks": b"WIKI_PACKS" in bytes(aa.get_entry_by_path("index.html").get_item().content),
        "s": round(time.perf_counter() - t0, 1),
    }
    ok = res["header_same_except_uuid"] and res["dirents_same"] and res["only_viewer_differs"] \
        and res["archive_check"] and res["viewer_has_wikipacks"] and len(differ) >= 1
    side_path = dst + ".parent.json"
    if os.path.exists(side_path):
        os.remove(side_path)
    if ok:
        # record the parent only on PASS (review S4, r2 #2): packs built against SRC fit DST.
        # The sidecar names its child (UUID + MD5) so it cannot vouch for another file, and
        # carries forward only parents that SRC's own sidecar validly records.
        from tools.langpack.merge_zim import base_lineage
        parents = sorted(base_lineage(src, old_uuid, check_md5=True) - {old_uuid.hex()})
        side = {"child_uuid": new_uuid.hex(), "child_md5": md5_whole(dst), "child_file": os.path.basename(dst),
                "parent_uuid": old_uuid.hex(), "parent_file": os.path.basename(src), "parents": parents,
                "viewer_rev": rev, "changed": ["index.html", "places.html", "routing-worker.js", "uuid", "md5"]}
        with open(side_path, "w") as fh:
            json.dump(side, fh, indent=1)
        res["sidecar"] = side
    else:
        os.remove(dst)                       # a failed swap leaves nothing usable behind
        res["removed_output"] = True
    print(json.dumps(res, indent=1))
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
