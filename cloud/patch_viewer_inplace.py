#!/usr/bin/env python3
"""Overwrite the viewer inside a ZIM in place, without re-packing it.

Why: cloud/swap_viewer_rust.py rewrites every entry and re-compresses every
cluster to change 600 KB of viewer. That is ~75 s for a 226 MB ZIM and hours
for europe. When the ZIM was built with fixed-size viewer slots (see
_pad_to_slot in swap_viewer_rust.py) the three viewer files sit in an
UNCOMPRESSED cluster at known byte offsets, padded to a fixed length, so a
new viewer is a seek + write + checksum recompute.

What it does NOT do: shrink the upload. Archive.org has no partial update, so
the whole file still ships. This saves the re-pack, not the transfer.

Usage:
    python3 cloud/patch_viewer_inplace.py <zim> [--viewer-dir resources/viewer]
    python3 cloud/patch_viewer_inplace.py <zim> --dry-run
"""
import argparse
import hashlib
import mmap
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cloud.viewer_slots import (  # noqa: E402
    SLOT_MAGIC, VIEWER_FILES, comment_delims, slot_header, slot_needle)
# libzim writes a 16-byte MD5 of everything before it as the final bytes.
CHECKSUM_LEN = 16


def _find_slot(mm: mmap.mmap, name: str):
    """Locate one slot. Returns (content_start, slot_len, head_len, is_js).

    The marker sits AFTER the content, at the head of the padding, and carries
    the slot length so we never have to infer it:
        <content><!--SZVSLOT1:name.....:000001048576:\n\n\n...-->
    """
    needle = slot_needle(name)
    pos = mm.find(needle)
    if pos < 0:
        return None
    # step back over the comment opener that precedes the magic
    is_js = name.endswith(".js")
    opener, _closer = comment_delims(name)
    marker_start = pos - len(opener)
    if mm[marker_start:pos] != opener:
        raise SystemExit(f"{name}: marker found at {pos} but opener missing")
    after = pos + len(needle)
    slot_len = int(mm[after:after + 12].decode())
    # header runs opener..':' after the length field
    head_len = (after + 12 + 1) - marker_start
    closer = b"*/" if is_js else b"-->"
    # The blob is exactly slot_len bytes and ends with the closer. Filler is
    # newlines, so the first closer after the marker is this slot's end.
    cpos = mm.find(closer, after)
    if cpos < 0:
        raise SystemExit(f"{name}: slot closer not found")
    blob_end = cpos + len(closer)
    content_start = blob_end - slot_len
    if content_start < 0:
        raise SystemExit(f"{name}: computed negative slot start")
    return content_start, slot_len, head_len, is_js


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zim")
    ap.add_argument("--viewer-dir", default="resources/viewer")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    zim = Path(args.zim)
    vdir = Path(args.viewer_dir)
    if not zim.is_file():
        raise SystemExit(f"no such ZIM: {zim}")

    t0 = time.time()
    size = zim.stat().st_size
    mode = "rb" if args.dry_run else "r+b"
    with open(zim, mode) as fh:
        access = mmap.ACCESS_READ if args.dry_run else mmap.ACCESS_WRITE
        mm = mmap.mmap(fh.fileno(), 0, access=access)
        try:
            plan = []
            for name in VIEWER_FILES:
                found = _find_slot(mm, name)
                if not found:
                    raise SystemExit(
                        f"{name}: no viewer slot in this ZIM. It was built "
                        f"before slots existed -- use swap_viewer_rust.py.")
                start, slot_len, head_len, is_js = found
                new = (vdir / name).read_bytes()
                _opener, closer = comment_delims(name)
                head = slot_header(name, slot_len)
                overhead = len(head) + len(closer)
                if len(new) + overhead > slot_len:
                    raise SystemExit(
                        f"{name}: new viewer is {len(new)} B + {overhead} B "
                        f"marker > {slot_len} B slot. Raise "
                        f"SLOT_SIZES[{name!r}] in cloud/viewer_slots.py and "
                        f"re-pack this region with swap_viewer_rust.py; "
                        f"refusing to truncate.")
                plan.append((name, start, slot_len, new, head, closer))
                print(f"  {name:20} slot@{start} len={slot_len} "
                      f"new={len(new)} B free={slot_len - len(new) - overhead}")

            if args.dry_run:
                print("  dry-run: nothing written")
                return 0

            for name, start, slot_len, new, head, closer in plan:
                filler = b"\n" * (slot_len - len(new) - len(head) - len(closer))
                mm[start:start + slot_len] = new + head + filler + closer
            mm.flush()
        finally:
            mm.close()

        # Recompute the trailing MD5 over everything before it.
        if not args.dry_run:
            fh.seek(0)
            h = hashlib.md5()
            remaining = size - CHECKSUM_LEN
            while remaining > 0:
                chunk = fh.read(min(8 << 20, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
            fh.seek(size - CHECKSUM_LEN)
            fh.write(h.digest())
            fh.flush()
            os.fsync(fh.fileno())
            print(f"  checksum rewritten: {h.hexdigest()[:12]}")

    print(f"  patched {zim.name} ({size/1e6:.1f} MB) in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
