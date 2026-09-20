"""Shared format for the fixed-size viewer slots.

Both cloud/swap_viewer_rust.py (writes them) and
cloud/patch_viewer_inplace.py (overwrites them) must agree byte-for-byte on
this header, so it lives in exactly one place.

Layout of a slot, always exactly `slot` bytes:

    <content><opener>SZVSLOT1:<name padded 24>:<slot len 12 digits>:<newlines><closer>

The marker sits AFTER the content so the file still parses as HTML/JS from
byte zero, and it records the slot length so the patcher never infers it.
"""

SLOT_MAGIC = b"SZVSLOT1"
NAME_WIDTH = 24
LEN_WIDTH = 12
VIEWER_FILES = ("index.html", "places.html", "routing-worker.js")

# Right-sized per file rather than a flat 1 MB each: index.html is the one
# that actually changes and keeps 2.3x growth room, the other two are small
# and static. Flat 1 MB cost 3 MB uncompressed per ZIM; this costs 1.4 MB.
SLOT_SIZES = {
    "index.html":        1024 * 1024,
    "places.html":        256 * 1024,
    "routing-worker.js":  128 * 1024,
}


def comment_delims(name):
    return (b"/*", b"*/") if name.endswith(".js") else (b"<!--", b"-->")


def slot_header(name, slot_len):
    opener, _ = comment_delims(name)
    return (opener + SLOT_MAGIC + b":" + name.encode().ljust(NAME_WIDTH, b" ")
            + b":" + str(slot_len).encode().rjust(LEN_WIDTH, b"0") + b":")


def slot_needle(name):
    """What the patcher scans for."""
    return SLOT_MAGIC + b":" + name.encode().ljust(NAME_WIDTH, b" ") + b":"


def pad_to_slot(name, data, slot_len=None):
    slot_len = slot_len or SLOT_SIZES[name]
    opener, closer = comment_delims(name)
    head = slot_header(name, slot_len)
    overhead = len(head) + len(closer)
    if len(data) + overhead > slot_len:
        raise SystemExit(
            f"viewer slot overflow: {name} is {len(data)} B + {overhead} B "
            f"marker > {slot_len} B slot. Raise SLOT_SIZES[{name!r}] in "
            f"cloud/viewer_slots.py and re-pack that region with "
            f"swap_viewer_rust.py; do NOT truncate.")
    return data + head + (b"\n" * (slot_len - len(data) - overhead)) + closer
