#!/usr/bin/env python3
"""Renumber a ZIM's clusters in file order, writing a repaired copy.

Why: streetzim/pack.py (before the fix in this commit) numbered a cluster
when its bucket opened but wrote it when the bucket flushed, so cluster 0
could sit deep in the file (22.77 GB into Europe 2026-10-12). libzim reads
everything from the MIME list up to cluster 0 into one buffer at open
(FileImpl::getMimeListEndUpperLimit): a Mac maps it, iOS Kiwix cannot and
says "unable to open file".

What changes: the cluster pointer table (now in offset order), the 4-byte
cluster number of each content dirent, a fresh UUID (Kiwix dedupes books by
UUID) and the trailing MD5. Cluster bytes, dirent sizes, the path and title
pointer tables, X/listing and the Xapian databases are untouched.

One streaming pass: the source is read once, the copy written once, the MD5
taken on the way. The copy is written to <dst>.tmp and renamed at the end.

Usage:
    python3 cloud/renumber_clusters.py <src.zim> <dst.zim>
    python3 cloud/renumber_clusters.py <src.zim> --check   # exit 0 if in order
"""
import argparse
import array
import hashlib
import itertools
import os
import struct
import sys
import uuid

HEADER = struct.Struct("<IHH16sIIQQQQIIQ")
ZIM_MAGIC = 0x044D495A
NO_CLUSTER = 0xFFFD          # redirect (FFFF), link target (FFFE), deleted (FFFD)
CHUNK = 64 << 20


class RenumberError(Exception):
    pass


def read_layout(fh):
    fh.seek(0)
    head = fh.read(HEADER.size)
    if len(head) != HEADER.size:
        raise RenumberError("short header")
    (magic, major, _minor, old_uuid, count, nclusters, path_pos, _title_pos,
     cluster_pos, _mime_pos, _main, _layout, checksum_pos) = HEADER.unpack(head)
    if magic != ZIM_MAGIC or major not in (5, 6):
        raise RenumberError("not a ZIM file")
    size = os.fstat(fh.fileno()).st_size
    if checksum_pos + 16 != size:
        raise RenumberError(f"checksum at {checksum_pos} but the file is {size} bytes")
    fh.seek(cluster_pos)
    offsets = struct.unpack(f"<{nclusters}Q", fh.read(8 * nclusters))
    if len(set(offsets)) != nclusters:
        raise RenumberError("duplicate cluster offsets")
    return old_uuid, count, nclusters, path_pos, cluster_pos, checksum_pos, offsets


def in_file_order(offsets):
    return all(a < b for a, b in itertools.pairwise(offsets))


def renumber(src, dst, order=None, new_uuid=None):
    """Copy src to dst with clusters renumbered: new cluster k is old
    cluster order[k] (default: by file offset). Returns a summary dict."""
    with open(src, "rb") as fh:
        old_uuid, count, nclusters, path_pos, cluster_pos, checksum_pos, offsets = read_layout(fh)
        if order is None:
            order = sorted(range(nclusters), key=offsets.__getitem__)
        if sorted(order) != list(range(nclusters)):
            raise RenumberError("order is not a permutation of the clusters")
        new_of = [0] * nclusters
        for new, old in enumerate(order):
            new_of[old] = new
        if new_uuid is None:
            new_uuid = uuid.uuid5(uuid.UUID(bytes=old_uuid), "clusters-in-file-order").bytes
        # The dirent area, found through the path pointer table.
        fh.seek(path_pos)
        pointers = array.array("Q")
        pointers.frombytes(fh.read(8 * count))
        if sys.byteorder != "little":
            pointers.byteswap()
        if len(pointers) != count:
            raise RenumberError("short path pointer table")
        lo = min(pointers)
        hi = min(max(pointers) + 4096, checksum_pos)
        if not (lo < hi <= checksum_pos):
            raise RenumberError("dirent pointers outside the file")
        fh.seek(lo)
        area = bytearray(hi - lo)          # read into place: one copy (Europe: 1.7 GB)
        if fh.readinto(area) != len(area):
            raise RenumberError("short read of the dirent area")
        changed = 0
        for pointer in pointers:
            at = pointer - lo
            if at < 0 or at + 2 > len(area):
                raise RenumberError(f"dirent at {pointer} outside the dirent area")
            mime, = struct.unpack_from("<H", area, at)
            if mime >= NO_CLUSTER:
                continue
            if at + 16 > len(area):
                raise RenumberError(f"dirent at {pointer} outside the dirent area")
            old, = struct.unpack_from("<I", area, at + 8)
            if old >= nclusters:
                raise RenumberError(f"dirent at {pointer} names cluster {old} of {nclusters}")
            if new_of[old] != old:
                struct.pack_into("<I", area, at + 8, new_of[old])
                changed += 1
        table = struct.pack(f"<{nclusters}Q", *(offsets[i] for i in order))
        patches = sorted([(8, new_uuid), (cluster_pos, table), (lo, area)])
        for (a, pa), (b, _pb) in itertools.pairwise(patches):
            if a + len(pa) > b:
                raise RenumberError("the pointer table overlaps the dirent area")
        tmp = dst + ".tmp"
        digest = hashlib.md5(usedforsecurity=False)
        fh.seek(0)
        with open(tmp, "wb") as out:
            position = 0
            while position < checksum_pos:
                chunk = bytearray(fh.read(min(CHUNK, checksum_pos - position)))
                if not chunk:
                    raise RenumberError("source shrank while copying")
                end = position + len(chunk)
                for start, data in patches:
                    s, e = max(start, position), min(start + len(data), end)
                    if s < e:
                        chunk[s - position:e - position] = data[s - start:e - start]
                digest.update(chunk)
                out.write(chunk)
                position = end
            out.write(digest.digest())
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dst)
    return {"clusters": nclusters, "renumbered": sum(new_of[i] != i for i in range(nclusters)),
            "dirents": count, "dirents_changed": changed,
            "uuid": str(uuid.UUID(bytes=new_uuid)), "old_uuid": str(uuid.UUID(bytes=old_uuid)),
            "md5": digest.hexdigest()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--check", action="store_true", help="exit 0 if the clusters are in file order, 1 if not")
    ap.add_argument("--check-first", action="store_true",
                    help="exit 0 if cluster 0 is the first in the file (what libzim's open needs), 1 if not")
    args = ap.parse_args(argv)
    with open(args.src, "rb") as fh:
        offsets = read_layout(fh)[-1]
    first = min(offsets) if offsets else 0
    print(f"{args.src}: {len(offsets)} clusters, cluster 0 at {offsets[0] if offsets else 0} "
          f"(first in file at {first}), {'in' if in_file_order(offsets) else 'NOT in'} file order", flush=True)
    if args.check:
        return 0 if in_file_order(offsets) else 1
    if args.check_first:
        return 0 if not offsets or offsets[0] == first else 1
    if not args.dst:
        ap.error("dst is required without --check")
    if os.path.abspath(args.dst) == os.path.abspath(args.src):
        ap.error("dst must differ from src")
    summary = renumber(args.src, args.dst)
    print(" ".join(f"{k}={v}" for k, v in summary.items()), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RenumberError as e:
        print(f"renumber_clusters: {e}", file=sys.stderr)
        sys.exit(2)
