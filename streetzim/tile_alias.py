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
up to a size cap are remembered (``MAX_ALIAS_BYTES``, per path prefix; a
prefix it does not list is an error):

* vector tiles (``tiles/``): ``VECTOR_MAX_ALIAS_BYTES`` = 128 B. Every
  duplicate seen so far is a 55-77 B sea/land-fill tile (Monaco: all 138
  are 56-57 B). A 4 KiB cap let 81 of Monaco's 108 distinct tiles into the
  table, 128 B lets in 2, the two fill tiles themselves;
* raster tiles (``satellite/``, ``terrain/``): ``RASTER_MAX_ALIAS_BYTES`` =
  640 B. A one-colour tile is 44-54 B as terrain (lossless WebP), 198-338 B
  as a 256 px satellite tile (the default; WebP q65/q80, AVIF) and 354-560 B
  at 512 px, and deep-ocean Sentinel-2 imagery is ~300-500 B, while imagery
  with any content is several KB. Raster tiles sit in uncompressed
  clusters, so a repeat costs its full size and the cap is set higher.

Keys, and the collision risk
----------------------------
A vector tile is keyed on its bytes themselves: an alias is only ever made
for identical bytes. A raster tile is keyed on an 88-bit BLAKE2b digest
combined with its length, so two tiles of different lengths never match.
The residual risk is two raster tiles of the same length with the same
88-bit digest, which would make the second an alias of the first (wrong
image, right size): for 30 M remembered raster tiles the chance of any
such pair is ~1e-12.

Memory
------
The table remembers every DISTINCT tile under the cap, not only the ones
that turn out to repeat (a later copy can only be known by remembering the
first). Memory is therefore about ``ENTRY_BYTES`` x (distinct tiles under
the cap), with the target path packed into an int. tracemalloc on CPython
3.11, 1 M distinct tiles: 114 B per raster entry (digest+length int key),
and 171-235 B per vector entry of 64-128 B (``VECTOR_ENTRY_BYTES``; the
bytes are the key); the old bytes-digest -> str table took 162 B. So a
million distinct small raster tiles cost ~115 MB, whatever the region; the
caps keep that count low, because a tile with any real content is larger.
Vector entries cost more each, but only fill tiles are under 128 B
(Monaco: 2 of its 108 distinct tiles). On a stream of 1 M distinct vector
tiles of 64-2080 B the table holds 46,875 entries (10.2 MB), where the old
4 KiB cap held all of them (162 MB); as raster tiles, 296,875 (31.9 MB).

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
RASTER_MAX_ALIAS_BYTES = 640
# Path prefix -> size cap. Keyed on the bytes themselves under "tiles/".
MAX_ALIAS_BYTES = {
    "tiles/": VECTOR_MAX_ALIAS_BYTES,
    "satellite/": RASTER_MAX_ALIAS_BYTES,
    "terrain/": RASTER_MAX_ALIAS_BYTES,
}
_RAW_KEY_PREFIX = "tiles/"
ENV_VAR = "STREETZIM_TILE_ALIASES"
# Approximate memory per remembered tile (see the module docstring).
ENTRY_BYTES = 115           # raster
VECTOR_ENTRY_BYTES = 235    # vector, at the 128 B cap

# Raster key: 88-bit digest, then the length in _LEN_BITS bits.
_DIGEST_BYTES = 11
_LEN_BITS = 11
assert RASTER_MAX_ALIAS_BYTES < 1 << _LEN_BITS
# "<prefix>/<z>/<x>/<y>.<ext>" with canonical decimals, so that unpacking
# gives back exactly the same string.
_TILE_PATH = re.compile(
    r"([^/]+)/(0|[1-9][0-9]?)/(0|[1-9][0-9]{0,7})/(0|[1-9][0-9]{0,7})\.([A-Za-z0-9]+)")
_XY_BITS = 24
_Z_BITS = 5


def max_alias_bytes(path: str) -> int:
    """The size cap for a tile at ``path`` (see the module docstring).
    ValueError for a prefix without one: a new tile kind must choose its
    cap, not inherit one."""
    prefix = path[:path.find("/") + 1]
    try:
        return MAX_ALIAS_BYTES[prefix]
    except KeyError:
        raise ValueError(f"no tile-alias size cap for {path!r}; "
                         f"add its prefix to MAX_ALIAS_BYTES") from None


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
        # key (the bytes for a vector tile, digest+length for raster) ->
        # packed target path (int), or the path itself when it does not
        # have the z/x/y shape.
        self._first: dict[bytes | int, int | str] = {}
        # (prefix, ext) pairs, indexed by the high bits of a packed path.
        self._kinds: list[tuple[str, str]] = []
        self._kind_ids: dict[tuple[str, str], int] = {}
        self._vector_entries = 0
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
        key: bytes | int
        raw = path.startswith(_RAW_KEY_PREFIX)
        if raw:
            key = bytes(data)
        else:
            digest = int.from_bytes(
                hashlib.blake2b(data, digest_size=_DIGEST_BYTES).digest(), "little")
            key = (digest << _LEN_BITS) | len(data)
        target = self._first.get(key)
        if target is None:
            self._first[key] = self._pack(path)
            self._vector_entries += raw
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
                f"remembered, ~{self.table_bytes() / 1e6:,.0f} MB)")

    def table_bytes(self) -> int:
        """Estimated memory of the table (ENTRY_BYTES, VECTOR_ENTRY_BYTES)."""
        n_vector = self._vector_entries
        return (n_vector * VECTOR_ENTRY_BYTES
                + (len(self._first) - n_vector) * ENTRY_BYTES)
