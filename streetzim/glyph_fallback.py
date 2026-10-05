"""Noto Sans glyphs for the scripts Open Sans lacks, merged into the Open Sans
glyph ranges.

The pinned Open Sans ranges (streetzim/viewer_assets.py) have no Arabic,
Hebrew, Armenian, Georgian, Thai or Lao glyphs: the ranges exist but are
empty, so MapLibre draws nothing for those labels even with the RTL text
plugin. MapLibre asks for one fontstack per layer and range
(``text-font: ["OpenSansBold"]`` -> ``fonts/OpenSansBold/1536-1791.pbf``); a
style listing a second font would make it ask for the combined name
``OpenSansBold,NotoSans...`` for every range, which a ZIM cannot compose on
the fly. So the builder composes it: for the few ranges that hold these
scripts, the Open Sans range gets the Noto Sans glyphs it has no glyph for,
limited to the Unicode blocks of the scripts (lock file ``fonts.fallback``).
The style, the viewer and every other range stay as they were, byte for byte.

A glyph range file is a protocol buffer (``glyphs { repeated fontstack
stacks = 1 }``, ``fontstack { name = 1; range = 2; repeated glyph glyphs = 3
}``, ``glyph { id = 1; ... }``). Merging appends the fallback's glyph
messages, unchanged, to the Open Sans fontstack message; Open Sans keeps
every codepoint it has. Both glyph sets are SDFs rendered the same way
(24 px, 3 px buffer: node-fontnik for openmaptiles, maplibre/font-maker for
the Noto set), so the metrics match.

Only the scripts that occur in the map's labels are merged
(:func:`scripts_in_tiles`), so a ZIM of a region without them does not change.
"""
from __future__ import annotations

import gzip
import re
import zlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _enc_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _fields(buf: bytes) -> Iterator[tuple[int, int | bytes, bytes]]:
    """(field number, value, the field's raw bytes) for each field."""
    i = 0
    while i < len(buf):
        start = i
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        value: int | bytes
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            n, i = _varint(buf, i)
            if i + n > len(buf):
                raise ValueError("truncated field")
            value = buf[i:i + n]
            i += n
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield field, value, buf[start:i]


def _glyph_id(msg: bytes) -> int | None:
    for field, value, _ in _fields(msg):
        if field == 1 and isinstance(value, int):
            return value
    return None


def glyph_ids(pbf: bytes) -> set[int]:
    """The codepoints a glyph range file has glyphs for."""
    ids: set[int] = set()
    for field, stack, _ in _fields(pbf):
        if field == 1 and isinstance(stack, bytes):
            for f2, glyph, _ in _fields(stack):
                if f2 == 3 and isinstance(glyph, bytes):
                    gid = _glyph_id(glyph)
                    if gid is not None:
                        ids.add(gid)
    return ids


def is_glyph_pbf(data: bytes) -> bool:
    """True if ``data`` parses as a glyph range file (an HTML error page
    served with status 200 does not)."""
    try:
        return all(field == 1 and isinstance(v, bytes) for field, v, _ in _fields(data)) \
            and data[:1] == b"\x0a"
    except (ValueError, IndexError):
        return False


def parse_blocks(spec: Iterable[str]) -> list[tuple[int, int]]:
    """["0600-06FF", ...] -> [(0x600, 0x6FF), ...]"""
    out: list[tuple[int, int]] = []
    for s in spec:
        lo, _, hi = s.partition("-")
        a, b = int(lo, 16), int(hi or lo, 16)
        if not 0 <= a <= b <= 0xFFFF:
            raise ValueError(f"bad block {s!r}")
        out.append((a, b))
    return out


def ranges_for(blocks: Iterable[tuple[int, int]]) -> list[str]:
    """The 256-codepoint glyph ranges ("1536-1791") the blocks fall in, sorted."""
    starts = sorted({cp // 256 * 256 for a, b in blocks for cp in range(a, b + 1)})
    return [f"{s}-{s + 255}" for s in starts]


# The fallback glyphs (maplibre/font-maker) put the baseline one pixel of
# the 24 px SDF (1/24 em) lower in the cell than Open Sans's (node-fontnik):
# the same outline gets ``top`` + 1, so unadjusted Noto letters sit 1/24 em
# higher than Open Sans on a shared line. Copied glyphs get this added to
# ``top`` (checked on U+0500-0513, which both fonts draw identically).
FALLBACK_TOP_SHIFT = -1


def _zigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _enc_zigzag(v: int) -> int:
    return v * 2 if v >= 0 else -v * 2 - 1


def _shift_top(glyph: bytes, shift: int) -> bytes:
    """A glyph message with ``shift`` added to its ``top`` (field 6,
    sint32), every other field kept byte for byte and in order."""
    out = bytearray()
    seen = False
    for field, value, raw in _fields(glyph):
        if field == 6 and isinstance(value, int):
            out += b"\x30" + _enc_varint(_enc_zigzag(_zigzag(value) + shift))
            seen = True
        else:
            out += raw
    if not seen:  # top absent means 0
        out += b"\x30" + _enc_varint(_enc_zigzag(shift))
    return bytes(out)


def _stack_head(name: str, range_key: str) -> bytes:
    return (b"\x0a" + _enc_varint(len(name.encode())) + name.encode()
            + b"\x12" + _enc_varint(len(range_key.encode())) + range_key.encode())


def merge_range(primary: bytes | None, fallback: bytes,
                blocks: Iterable[tuple[int, int]], *, name: str = "",
                range_key: str = "", top_shift: int = FALLBACK_TOP_SHIFT) -> bytes:
    """``primary`` plus the glyphs of ``fallback`` whose codepoint lies in
    ``blocks`` and that ``primary`` has no glyph for, each moved by
    ``top_shift`` onto the primary's baseline.

    Returns ``primary`` itself when nothing is added. ``primary`` None (the
    range is absent from the primary font), or one without a fontstack,
    gets a fontstack called ``name`` for ``range_key``."""
    blocks = list(blocks)
    have: set[int] = glyph_ids(primary) if primary is not None else set()
    extra: dict[int, bytes] = {}
    for field, stack, _ in _fields(fallback):
        if field != 1 or not isinstance(stack, bytes):
            continue
        for f2, glyph, _raw in _fields(stack):
            if f2 != 3 or not isinstance(glyph, bytes):
                continue
            gid = _glyph_id(glyph)
            if (gid is not None and gid not in have and gid not in extra
                    and any(a <= gid <= b for a, b in blocks)):
                moved = _shift_top(glyph, top_shift) if top_shift else glyph
                extra[gid] = b"\x1a" + _enc_varint(len(moved)) + moved
    if not extra and primary is not None:
        return primary
    added = b"".join(extra[k] for k in sorted(extra))
    if primary is None:
        stack_msg = _stack_head(name, range_key) + added
        return b"\x0a" + _enc_varint(len(stack_msg)) + stack_msg
    out = bytearray()
    merged = False
    for field, value, raw in _fields(primary):
        if field == 1 and isinstance(value, bytes) and not merged:
            stack_msg = value + added
            out += b"\x0a" + _enc_varint(len(stack_msg)) + stack_msg
            merged = True
        else:
            out += raw
    if not merged:  # no fontstack at all: add a complete one
        stack_msg = _stack_head(name, range_key) + added
        out += b"\x0a" + _enc_varint(len(stack_msg)) + stack_msg
    return bytes(out)


# ---- which scripts a map's labels use -------------------------------------

def _utf8_class(blocks: Iterable[tuple[int, int]]) -> str:
    return "".join(f"\\u{a:04x}-\\u{b:04x}" for a, b in blocks)


def _byte_class(values: Iterable[int]) -> bytes:
    return b"[" + b"".join(b"\\x%02x" % v for v in sorted(set(values))) + b"]"


def _byte_pattern(blocks: Iterable[tuple[int, int]]) -> re.Pattern[bytes]:
    """A UTF-8 encoded character from the blocks: a byte-level prefilter.
    Tile geometry rarely matches, and a tile that matches is then parsed to
    be sure.

    Each character is written as ``[first bytes](?:(?<=first)rest|...)``: a
    pattern that starts with a byte class lets the regex engine skip ahead
    in C, which an alternation of whole sequences does not (10x slower)."""
    by_lead: dict[int, dict[bytes, set[int]]] = {}
    for a, b in blocks:
        for cp in range(a, b + 1):
            enc = chr(cp).encode()
            by_lead.setdefault(enc[0], {}).setdefault(enc[1:-1], set()).add(enc[-1])
    alts: list[bytes] = []
    for lead, mids in sorted(by_lead.items()):
        rest = b"|".join(re.escape(mid) + _byte_class(lasts) for mid, lasts in sorted(mids.items()))
        alts.append(b"(?<=\\x%02x)(?:%s)" % (lead, rest))
    char = _byte_class(by_lead) + b"(?:" + b"|".join(alts) + b")"
    return re.compile(char)


# The properties the style draws as text: every "text-field" in the viewer
# reads only these (tests/test_glyph_fallback.py parses the viewer to check).
# Other keys, e.g. an OpenMapTiles tile's name:ar or name:he, are never
# displayed and must not pull in glyphs. "label" is the driving HUD's
# runtime GeoJSON layer, never a tile key; listing it is harmless (and the
# scan cannot see runtime labels anyway).
LABEL_KEYS = frozenset({"name", "name:latin", "name_int", "label"})


def label_keys() -> frozenset[str]:
    """LABEL_KEYS plus name:<language> for a build in another language
    (create_osm_zim --language sets STREETZIM_TILE_LANGUAGES; the viewer's
    szLabelField draws name:<language> first)."""
    import os
    langs = [x.strip() for x in os.environ.get("STREETZIM_TILE_LANGUAGES", "").split(",")
             if x.strip()]
    return LABEL_KEYS | {f"name:{x}" for x in langs}


def _tile_strings(tile: bytes, keys: frozenset[str] | None = None,
                  match: re.Pattern[str] | None = None) -> Iterator[str]:
    """The non-ASCII string values of a Mapbox Vector Tile that some feature
    carries under one of ``keys`` (and, with ``match``, that it matches).

    The tile is parsed by the protobuf runtime (C, via mapbox-vector-tile's
    generated module); only the features of layers that have both a
    displayed key and a matching string are walked, in Python."""
    import importlib
    # mapbox-vector-tile (a builder dependency) ships the generated module
    pb2: Any = importlib.import_module("mapbox_vector_tile.Mapbox.vector_tile_pb2")
    if keys is None:
        keys = label_keys()
    parsed: Any = pb2.tile()
    try:
        parsed.ParseFromString(tile)
    except Exception as e:  # google.protobuf.message.DecodeError
        raise ValueError(f"not a vector tile: {e}") from None
    layers: Sequence[Any] = parsed.layers
    for layer in layers:
        layer_keys: Sequence[str] = layer.keys
        wanted = {i for i, k in enumerate(layer_keys) if k in keys}
        if not wanted:
            continue
        strings: dict[int, str] = {}
        values: Sequence[Any] = layer.values
        for i, value in enumerate(values):
            s: str = value.string_value
            if s and not s.isascii() and (match is None or match.search(s)):
                strings[i] = s
        if not strings:
            continue
        shown: set[int] = set()
        cand = strings.keys()
        features: Sequence[Any] = layer.features
        for feature in features:
            tags: Sequence[int] = feature.tags
            if cand.isdisjoint(tags[1::2]):  # the common case, decided in C
                continue
            for k, v in zip(tags[0::2], tags[1::2]):
                if v in strings and k in wanted:
                    shown.add(v)
            if len(shown) == len(strings):
                break
        for i in sorted(shown):
            yield strings[i]


def _decompress(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:1] == b"\x78":
        try:
            return zlib.decompress(data)
        except zlib.error:
            return data
    return data


def scripts_in_tiles(tiles: Iterable[bytes],
                     scripts: Mapping[str, Iterable[tuple[int, int]]],
                     stats: dict[str, int] | None = None) -> set[str]:
    """The names of ``scripts`` whose characters occur in a string value of
    any of ``tiles`` (Mapbox Vector Tiles, gzip-compressed or not) that the
    style displays (a feature's LABEL_KEYS). Stops reading once every script
    has been seen. A tile that does not decompress or parse is skipped
    (MapLibre cannot draw it either) and counted in ``stats["unreadable"]``."""
    todo = {name: (_byte_pattern(blocks), re.compile(f"[{_utf8_class(blocks)}]"))
            for name, blocks in ((n, list(b)) for n, b in scripts.items())}
    found: set[str] = set()
    if stats is not None:
        stats.setdefault("unreadable", 0)
    if not todo:
        return found
    # A displayed key is written in the layer's key table as field 3.
    key_bytes = [b"\x1a" + _enc_varint(len(k.encode())) + k.encode() for k in label_keys()]
    for data in tiles:
        if not data:
            continue
        try:
            raw = _decompress(data)
        except (OSError, EOFError, zlib.error):
            if stats is not None:
                stats["unreadable"] += 1
            continue
        hits = [name for name, (bpat, _) in todo.items() if bpat.search(raw)]
        if not hits or not any(k in raw for k in key_bytes):
            continue
        match = re.compile("|".join(todo[name][1].pattern for name in hits))
        try:
            texts = list(_tile_strings(raw, match=match))
        except (ValueError, IndexError):
            if stats is not None:
                stats["unreadable"] += 1
            continue
        for name in hits:
            if any(todo[name][1].search(t) for t in texts):
                found.add(name)
                del todo[name]
        if not todo:
            break
    return found
