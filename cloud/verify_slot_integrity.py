#!/usr/bin/env python3
"""Prove a slotted (and optionally patched) ZIM differs from its source ONLY
in the three viewer files.

This is the structural half of the slot regression suite: it hashes every
blob in both archives and reports any path that was added, dropped, or whose
bytes changed. Anything beyond index.html / places.html / routing-worker.js
is a bug in the slot mechanism.

Usage:
    python3 cloud/verify_slot_integrity.py <source.zim> <slotted.zim>
"""
import hashlib
import sys
from libzim.reader import Archive

VIEWER = {"index.html", "places.html", "routing-worker.js"}


def inventory(path):
    a = Archive(path)
    out = {}
    meta = {}
    for i in range(a.all_entry_count):
        try:
            e = a._get_entry_by_id(i)
        except Exception:
            continue
        p = e.path
        try:
            if e.is_redirect:
                out[p] = ("redirect", e.get_redirect_entry().path)
                continue
            it = e.get_item()
            blob = bytes(it.content)
            # mimetype and title matter as much as bytes: a text/html entry
            # served as application/octet-stream breaks the viewer, and the
            # content hash would not notice.
            out[p] = ("blob", hashlib.sha256(blob).hexdigest(), len(blob),
                      it.mimetype, e.title)
        except Exception as ex:
            out[p] = ("error", str(ex)[:40])
    for k in a.metadata_keys:
        try:
            meta[k] = hashlib.sha256(bytes(a.get_metadata(k))).hexdigest()[:16]
        except Exception:
            meta[k] = "unreadable"
    return a, out, meta


def main():
    src_path, dst_path = sys.argv[1], sys.argv[2]
    sa, src, smeta = inventory(src_path)
    da, dst, dmeta = inventory(dst_path)

    print(f"  entries      src={sa.all_entry_count:<7} slotted={da.all_entry_count}")
    print(f"  articles     src={sa.article_count:<7} slotted={da.article_count}")
    print(f"  fulltext idx src={sa.has_fulltext_index:<7} slotted={da.has_fulltext_index}")
    print(f"  title idx    src={sa.has_title_index:<7} slotted={da.has_title_index}")
    print(f"  checksum ok  src={sa.check():<7} slotted={da.check()}")
    # Navigation entry points: if the main page moves or a redirect chain
    # breaks, Kiwix opens a blank book and every other test still passes.
    def mainp(arc):
        try:
            m = arc.main_entry
            while m.is_redirect:
                m = m.get_redirect_entry()
            return m.path
        except Exception as ex:
            return f"ERR {ex}"
    print(f"  main entry   src={mainp(sa):<20} slotted={mainp(da)}")
    print(f"  uuid         src={str(sa.uuid)[:8]}  slotted={str(da.uuid)[:8]}")

    added = sorted(set(dst) - set(src))
    dropped = sorted(set(src) - set(dst))
    changed = sorted(p for p in (set(src) & set(dst)) if src[p] != dst[p])
    unexpected = [p for p in changed if p not in VIEWER]

    print(f"\n  added:   {len(added)}  {added[:4]}")
    print(f"  dropped: {len(dropped)}  {dropped[:4]}")
    print(f"  changed: {len(changed)}")
    for p in changed[:8]:
        s, d = src[p], dst[p]
        tag = "VIEWER" if p in VIEWER else "UNEXPECTED"
        ssz = s[2] if s[0] == "blob" else "-"
        dsz = d[2] if d[0] == "blob" else "-"
        extra = ""
        if s[0] == "blob" and d[0] == "blob":
            if s[3] != d[3]:
                extra += f"  MIME {s[3]} -> {d[3]}"
            if s[4] != d[4]:
                extra += f"  TITLE {s[4]!r} -> {d[4]!r}"
        print(f"    [{tag}] {p}  {ssz} -> {dsz} B{extra}")

    meta_diff = {k for k in set(smeta) | set(dmeta) if smeta.get(k) != dmeta.get(k)}
    print(f"\n  metadata keys differing: {sorted(meta_diff) or 'none'}")

    ok = not added and not dropped and not unexpected
    print(f"\n  RESULT: {'PASS - only viewer files differ' if ok else 'FAIL'}")
    if unexpected:
        print(f"    unexpected changes: {unexpected[:10]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
