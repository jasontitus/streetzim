"""Store identical map tiles once: later copies become ZIM aliases.

A ZIM alias (libzim ``Creator.add_alias``, python-libzim >= 3.6) is a second
directory entry that points at the SAME cluster and blob as an entry already
added. It is not a redirect: every reader (libzim, kiwix-serve, the /drive
PWA's own reader) sees an ordinary item with the target's mimetype and
bytes, so nothing that fetches ``tiles/z/x/y.pbf`` changes. openzim/maps
stores its tiles the same way.

What it saves is small (docs/tile-aliases.md has the numbers): the dirent is
still written, only the blob and its offset go, and a repeated blob in the
same zstd cluster already compressed to a few bytes. It also removes
zimcheck's "Redundant Data" warning for every duplicate tile.

Which tiles are remembered, and what that costs
-----------------------------------------------
Only a tile with nothing specific to its position can repeat: open sea, the
inside of one landcover polygon, flat terrain. Those are tiny, so only tiles
up to a size cap are hashed (``max_alias_bytes``):

* vector tiles (``tiles/``): ``VECTOR_MAX_ALIAS_BYTES`` = 128 B. Every
  duplicate seen so far is a 55-77 B sea/land-fill tile (Monaco: all 138
  are 56-57 B). A 4 KiB cap let 81 of Monaco's 108 distinct tiles into the
  table, 128 B lets in 2, the two fill tiles themselves;
* raster tiles (``satellite/``, ``terrain/``): ``RASTER_MAX_ALIAS_BYTES`` =
  1 KiB. A one-colour tile is 44-54 B as terrain (lossless WebP), 198-338 B
  as a 256 px satellite tile (WebP q65/q80, AVIF) and 354-560 B at 512 px,
  while real imagery is several KB. Raster tiles sit in uncompressed
  clusters, so a repeat costs its full size and the cap is set higher.

The table remembers every DISTINCT tile under the cap, not only the ones
that turn out to repeat (a later copy can only be known by remembering the
first). Memory is therefore about ``ENTRY_BYTES`` x (distinct tiles under
the cap): an 88-bit digest held as an int key, the target path packed into
an int, and the dict slot. tracemalloc on CPython 3.11, 1 M distinct 96 B
tiles: 110 B per entry (the old bytes-digest -> str table: 162 B). So a
million distinct small tiles cost ~110 MB, whatever the region; the caps
keep that count low, because a tile with any real content is larger. On a
stream of 1 M distinct tiles of 64-2080 B the table holds 46,875 entries
(5.8 MB), where the old 4 KiB cap held all of them (162 MB).

Set ``STREETZIM_TILE_ALIASES=0`` to write every tile as its own item. The
Rust packer (``--zim-builder rust``) has no alias record, so it writes
every tile as its own item too.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

VECTOR_MAX_ALIAS_BYTES = 128
RASTER_MAX_ALIAS_BYTES = 1024
ENV_VAR = "STREETZIM_TILE_ALIASES"
# Approximate memory per remembered tile (see the module docstring).
ENTRY_BYTES = 110

# 88 bits: a CPython int of up to 90 bits is 36 B. The chance of any
# collision among 30 M remembered tiles is ~1e-12.
_DIGEST_BYTES = 11
# "<prefix>/<z>/<x>/<y>.<ext>" with canonical decimals, so that unpacking
# gives back exactly the same string.
_TILE_PATH = re.compile(
    r"([^/]+)/(0|[1-9][0-9]?)/(0|[1-9][0-9]{0,7})/(0|[1-9][0-9]{0,7})\.([A-Za-z0-9]+)")
_XY_BITS = 24
_Z_BITS = 5


def max_alias_bytes(path: str) -> int:
    """The size cap for a tile at ``path`` (see the module docstring)."""
    return VECTOR_MAX_ALIAS_BYTES if path.startswith("tiles/") else RASTER_MAX_ALIAS_BYTES


def aliases_enabled(creator: Any) -> bool:
    """True unless disabled by the environment or the creator has no
    ``add_alias`` (the Rust packer's ManifestCreator, an old libzim)."""
    if os.environ.get(ENV_VAR, "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return callable(getattr(creator, "add_alias", None))


class TileAliaser:
    """First-seen wins: the first tile with given bytes becomes the item,
    each later identical tile an alias of it. Callers add tiles in a fixed
    order (z, x, y for a build; entry order for a repackage), so the same
    input gives the same ZIM."""

    def __init__(self, creator: Any, enabled: bool | None = None) -> None:
        self._creator = creator
        self.enabled = aliases_enabled(creator) if enabled is None else enabled
        # digest (int) -> packed target path (int), or the path itself when
        # it does not have the z/x/y shape.
        self._first: dict[int, int | str] = {}
        # (prefix, ext) pairs, indexed by the high bits of a packed path.
        self._kinds: list[tuple[str, str]] = []
        self._kind_ids: dict[tuple[str, str], int] = {}
        self.aliases = 0
        self.bytes_aliased = 0

    def __len__(self) -> int:
        """How many distinct tiles are remembered."""
        return len(self._first)

    def _pack(self, path: str) -> int | str:
        m = _TILE_PATH.fullmatch(path)
        if m is None:
            return path
        prefix, z, x, y, ext = m.groups()
        zi, xi, yi = int(z), int(x), int(y)
        if zi >= 1 << _Z_BITS or xi >= 1 << _XY_BITS or yi >= 1 << _XY_BITS:
            return path
        kind = (prefix, ext)
        k = self._kind_ids.get(kind)
        if k is None:
            k = self._kind_ids[kind] = len(self._kinds)
            self._kinds.append(kind)
        return (((k << _Z_BITS) | zi) << (2 * _XY_BITS)) | (xi << _XY_BITS) | yi

    def _unpack(self, packed: int | str) -> str:
        if isinstance(packed, str):
            return packed
        mask = (1 << _XY_BITS) - 1
        y = packed & mask
        x = (packed >> _XY_BITS) & mask
        rest = packed >> (2 * _XY_BITS)
        prefix, ext = self._kinds[rest >> _Z_BITS]
        return f"{prefix}/{rest & ((1 << _Z_BITS) - 1)}/{x}/{y}.{ext}"

    def target_for(self, path: str, data: bytes) -> str | None:
        """The path of an earlier tile with exactly these bytes, or None.

        None means: add ``path`` as a normal item (it is remembered as the
        target for later copies when it is under the size cap)."""
        if not self.enabled or not data or len(data) > max_alias_bytes(path):
            return None
        key = int.from_bytes(
            hashlib.blake2b(data, digest_size=_DIGEST_BYTES).digest(), "little")
        target = self._first.get(key)
        if target is None:
            self._first[key] = self._pack(path)
            return None
        return self._unpack(target)

    def add_alias(self, path: str, title: str, target: str, size: int) -> None:
        from libzim.writer import Hint
        self._creator.add_alias(path, title, target, {Hint.FRONT_ARTICLE: False})
        self.aliases += 1
        self.bytes_aliased += size

    def summary(self) -> str:
        if not self.enabled:
            return "tile aliases off"
        return (f"{self.aliases:,} duplicate tiles aliased "
                f"({self.bytes_aliased:,} B, {len(self._first):,} distinct small tiles "
                f"remembered, ~{len(self._first) * ENTRY_BYTES / 1e6:,.0f} MB)")
