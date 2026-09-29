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

Only tiles up to ``MAX_ALIAS_BYTES`` are hashed. Duplicate tiles are the
ones with no position-specific geometry (open sea, a tile inside one
landcover polygon), which are tens to hundreds of bytes; bounding the size
bounds the memory the hash table takes on a continent-sized build.

Set ``STREETZIM_TILE_ALIASES=0`` to write every tile as its own item. The
Rust packer (``--zim-builder rust``) has no alias record, so it writes
every tile as its own item too.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

MAX_ALIAS_BYTES = 4096
ENV_VAR = "STREETZIM_TILE_ALIASES"


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
        self._first: dict[bytes, str] = {}
        self.aliases = 0
        self.bytes_aliased = 0

    def target_for(self, path: str, data: bytes) -> str | None:
        """The path of an earlier tile with exactly these bytes, or None.

        None means: add ``path`` as a normal item (it is remembered as the
        target for later copies)."""
        if not self.enabled or not data or len(data) > MAX_ALIAS_BYTES:
            return None
        key = hashlib.blake2b(data, digest_size=16).digest()
        target = self._first.get(key)
        if target is None:
            self._first[key] = path
            return None
        return target

    def add_alias(self, path: str, title: str, target: str, size: int) -> None:
        from libzim.writer import Hint
        self._creator.add_alias(path, title, target, {Hint.FRONT_ARTICLE: False})
        self.aliases += 1
        self.bytes_aliased += size

    def summary(self) -> str:
        if not self.enabled:
            return "tile aliases off"
        return (f"{self.aliases:,} duplicate tiles aliased "
                f"({self.bytes_aliased:,} B, {len(self._first):,} distinct small tiles)")
