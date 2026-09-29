"""Corner cases of the fallback-glyph merge (streetzim/glyph_fallback.py):
Noto Sans glyphs copied into the Open Sans glyph range files.

Three layers:

* synthetic range files: what merge_range does with shared ranges, absent
  or odd primaries, duplicates, placeholders, field order; every output is
  decoded by a strict proto2 decoder built from MapLibre's glyphs.proto;
* the pinned ranges themselves (fetched through the verified cache like a
  build; skipped when neither the cache nor the network has them): the
  merge equals @mapbox/glyph-pbf-composite's first-font-wins composite for
  every subset of scripts, the vendored MapLibre's own parser reads every
  merged range, the SDF parameters and metrics of the two fonts agree, and
  the codepoints labels need are there;
* the build around it (generate_sdf_font_glyphs, fallback_scripts_in_tiles)
  against fake servers: shared ranges, absent ranges, waived errors,
  thread counts, offline and prefetched caches.
"""
from __future__ import annotations

import functools
import gzip
import hashlib
import io
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim import glyph_fallback as gf, tiles, viewer_assets as va  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import pin_viewer_assets as pin  # noqa: E402

PARSER_JS = ROOT / "tests" / "lib" / "maplibre_glyph_parse.mjs"
NODE = shutil.which("node")


# ---- a strict decoder: MapLibre's glyphs.proto, proto2, required fields ----

def _glyph_classes():
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    F = descriptor_pb2.FieldDescriptorProto
    fdp = descriptor_pb2.FileDescriptorProto(name="sz_glyphs_test.proto", package="sztest.glyphs",
                                             syntax="proto2")

    def msg(name, fields):
        m = fdp.message_type.add(name=name)
        for fname, num, typ, label, tname in fields:
            f = m.field.add(name=fname, number=num, type=typ, label=label)
            if tname:
                f.type_name = tname
    REQ, OPT, REP = F.LABEL_REQUIRED, F.LABEL_OPTIONAL, F.LABEL_REPEATED
    msg("glyph", [("id", 1, F.TYPE_UINT32, REQ, None), ("bitmap", 2, F.TYPE_BYTES, OPT, None),
                  ("width", 3, F.TYPE_UINT32, REQ, None), ("height", 4, F.TYPE_UINT32, REQ, None),
                  ("left", 5, F.TYPE_SINT32, REQ, None), ("top", 6, F.TYPE_SINT32, REQ, None),
                  ("advance", 7, F.TYPE_UINT32, REQ, None)])
    msg("fontstack", [("name", 1, F.TYPE_STRING, REQ, None), ("range", 2, F.TYPE_STRING, REQ, None),
                      ("glyphs", 3, F.TYPE_MESSAGE, REP, ".sztest.glyphs.glyph")])
    msg("glyphs", [("stacks", 1, F.TYPE_MESSAGE, REP, ".sztest.glyphs.fontstack")])
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("sztest.glyphs.glyphs"))


GlyphsMsg = _glyph_classes()

# wire type each field must use: 0 varint, 2 length-delimited
_WIRE = {"glyphs": {1: 2}, "fontstack": {1: 2, 2: 2, 3: 2},
         "glyph": {1: 0, 2: 2, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0}}


def _wire_check(buf: bytes, kind: str) -> None:
    """Each field has the wire type glyphs.proto gives it (a packed or
    fixed-width encoding of a scalar would be misread by MapLibre's pbf
    reader) and no singular field repeats (a lenient decoder keeps the
    last value, MapLibre too, so a repeat would hide a merge bug)."""
    seen: list[int] = []
    i = 0
    while i < len(buf):
        key, i = gf._varint(buf, i)
        field, wire = key >> 3, key & 7
        assert field in _WIRE[kind], f"{kind}: unknown field {field}"
        assert wire == _WIRE[kind][field], f"{kind}.{field}: wire type {wire}"
        if wire == 0:
            _, i = gf._varint(buf, i)
        else:
            n, i = gf._varint(buf, i)
            sub = buf[i:i + n]
            assert len(sub) == n, f"{kind}.{field}: truncated"
            i += n
            if kind == "glyphs":
                _wire_check(sub, "fontstack")
            elif kind == "fontstack" and field == 3:
                _wire_check(sub, "glyph")
        seen.append(field)
    if kind in ("glyph", "fontstack"):
        singular = [f for f in seen if not (kind == "fontstack" and f == 3)]
        assert len(singular) == len(set(singular)), f"{kind}: a singular field repeats: {seen}"


def strict_decode(pbf: bytes):
    """The glyphs message, or AssertionError: parses as proto2 with every
    required field (fontstack name and range; glyph id, width, height, left,
    top, advance), no unknown field, the proto's wire types, no glyph id
    twice in a fontstack, and every bitmap (w+6)*(h+6) bytes (3 px buffer)
    or absent for an empty glyph."""
    from google.protobuf import unknown_fields
    _wire_check(pbf, "glyphs")
    m = GlyphsMsg()
    m.ParseFromString(pbf)
    missing = m.FindInitializationErrors()
    assert not missing, f"required fields missing: {missing[:5]}"
    assert len(unknown_fields.UnknownFieldSet(m)) == 0
    for st in m.stacks:
        assert len(unknown_fields.UnknownFieldSet(st)) == 0
        ids = [g.id for g in st.glyphs]
        assert len(ids) == len(set(ids)), "duplicate glyph ids"
        for g in st.glyphs:
            if g.width and g.height:
                assert len(g.bitmap) == (g.width + 6) * (g.height + 6), f"glyph {g.id:#x}: bitmap size"
            else:
                assert not g.bitmap, f"glyph {g.id:#x}: bitmap on an empty glyph"
    return m


# ---- synthetic range files --------------------------------------------------

def _v(n: int) -> bytes:
    return gf._enc_varint(n)


def _ld(field: int, data: bytes) -> bytes:
    return _v(field << 3 | 2) + _v(len(data)) + data


def _zz(n: int) -> int:
    return (n << 1) ^ (n >> 31)


def glyph(cp: int, w: int = 5, h: int = 7, left: int = 1, top: int = -10, adv: int = 8,
          fill: int = 0, *, id_last: bool = False) -> bytes:
    body = (_ld(2, bytes([fill]) * ((w + 6) * (h + 6))) if w and h else b"") + \
        b"\x18" + _v(w) + b"\x20" + _v(h) + b"\x28" + _v(_zz(left)) + b"\x30" + _v(_zz(top)) + b"\x38" + _v(adv)
    gid = b"\x08" + _v(cp)
    return _ld(3, body + gid if id_last else gid + body)


def rng_pbf(name: str, rng: str, glyphs: list[bytes]) -> bytes:
    return _ld(1, _ld(1, name.encode()) + _ld(2, rng.encode()) + b"".join(glyphs))


def decoded(pbf: bytes) -> dict[int, object]:
    m = strict_decode(pbf)
    assert len(m.stacks) == 1
    return {g.id: g for g in m.stacks[0].glyphs}


SHARED = "64256-64511"
ARMENIAN_LIGS = [0xFB13, 0xFB14, 0xFB15, 0xFB16, 0xFB17]
HEBREW_PRES = [0xFB1D, 0xFB2A, 0xFB4F]
ARABIC_PRES = [0xFB50, 0xFB8A, 0xFBFF]
LATIN_LIGS = [0xFB00, 0xFB01]
BLOCKS = {  # the lock's blocks for the scripts sharing 64256-64511
    "Arabic": [(0x600, 0x6FF), (0xFB50, 0xFBFF), (0xFE70, 0xFEFF)],
    "Hebrew": [(0x590, 0x5FF), (0xFB1D, 0xFB4F)],
    "Armenian": [(0x530, 0x58F)],
}


def _shared_pair():
    primary = rng_pbf("Open Sans Regular", SHARED, [glyph(c, fill=1) for c in LATIN_LIGS])
    fallback = rng_pbf("Noto Sans Regular", SHARED,
                       [glyph(c, fill=2) for c in LATIN_LIGS + ARMENIAN_LIGS + HEBREW_PRES + ARABIC_PRES])
    return primary, fallback


@pytest.mark.parametrize("scripts, added", [
    (("Hebrew",), HEBREW_PRES),
    (("Arabic",), ARABIC_PRES),
    (("Arabic", "Hebrew"), HEBREW_PRES + ARABIC_PRES),
    (("Armenian",), []),  # the Armenian block does not reach this range
])
def test_shared_range_gets_only_the_detected_scripts(scripts, added):
    primary, fallback = _shared_pair()
    blocks = [b for s in scripts for b in BLOCKS[s]]
    out = gf.merge_range(primary, fallback, blocks, name="Open Sans Regular", range_key=SHARED)
    got = decoded(out)
    assert set(got) == set(LATIN_LIGS) | set(added)
    assert all(got[c].bitmap[:1] == b"\x01" for c in LATIN_LIGS)  # Open Sans kept
    assert all(got[c].bitmap[:1] == b"\x02" for c in added)
    if not added:
        assert out is primary


def test_a_later_build_with_other_scripts_gets_their_union_not_a_leftover():
    """Each build merges into the pinned Open Sans range, never into a
    previous build's output: Hebrew-then-Arabic is exactly Arabic, and
    Arabic+Hebrew is the union."""
    primary, fallback = _shared_pair()
    he = gf.merge_range(primary, fallback, BLOCKS["Hebrew"])
    ar = gf.merge_range(primary, fallback, BLOCKS["Arabic"])
    both = gf.merge_range(primary, fallback, BLOCKS["Hebrew"] + BLOCKS["Arabic"])
    assert set(decoded(both)) == set(decoded(he)) | set(decoded(ar))
    assert not set(decoded(ar)) & set(HEBREW_PRES)
    # block order does not matter, and the same inputs give the same bytes
    assert gf.merge_range(primary, fallback, BLOCKS["Arabic"] + BLOCKS["Hebrew"]) == both


def test_merge_is_deterministic_whatever_the_fallback_glyph_order():
    primary, _ = _shared_pair()
    cps = HEBREW_PRES + ARABIC_PRES + ARMENIAN_LIGS
    outs = set()
    for seed in range(5):
        order = cps[:]
        random.Random(seed).shuffle(order)
        fb = rng_pbf("Noto", SHARED, [glyph(c) for c in order])
        outs.add(gf.merge_range(primary, fb, BLOCKS["Hebrew"] + BLOCKS["Arabic"]))
    assert len(outs) == 1
    ids = [g.id for g in strict_decode(outs.pop()).stacks[0].glyphs]
    assert ids[len(LATIN_LIGS):] == sorted(HEBREW_PRES + ARABIC_PRES)  # appended in id order


def test_open_sans_wins_for_codepoints_both_have():
    primary = rng_pbf("Open Sans Regular", "1536-1791", [glyph(0x60C, w=4, fill=1)])
    fallback = rng_pbf("Noto", "1536-1791", [glyph(0x60C, w=9, fill=2), glyph(0x627, fill=2)])
    got = decoded(gf.merge_range(primary, fallback, [(0x600, 0x6FF)]))
    assert got[0x60C].width == 4 and got[0x60C].bitmap[:1] == b"\x01"
    assert got[0x627].bitmap[:1] == b"\x02"


def test_an_empty_open_sans_glyph_still_blocks_the_fallback():
    """Documents the rule: "Open Sans has it" means "has a glyph message",
    even a zero-size one. Harmless for the pinned fonts (the only such
    codepoint in the merged blocks is U+FEFF, zero-width in both; see
    test_pinned_open_sans_has_no_placeholder_that_hides_noto_ink), but a
    future Open Sans pin with .notdef-like placeholders would hide Noto."""
    primary = rng_pbf("Open Sans Regular", "1536-1791", [glyph(0x627, w=0, h=0, adv=0)])
    fallback = rng_pbf("Noto", "1536-1791", [glyph(0x627, fill=2)])
    out = gf.merge_range(primary, fallback, [(0x600, 0x6FF)])
    assert out is primary and decoded(out)[0x627].width == 0


def test_absent_open_sans_range_becomes_a_complete_fontstack():
    fallback = rng_pbf("Noto", "1536-1791", [glyph(0x627), glyph(0x644), glyph(0x700)])
    out = gf.merge_range(None, fallback, [(0x600, 0x6FF)], name="Open Sans Bold", range_key="1536-1791")
    m = strict_decode(out)
    assert (m.stacks[0].name, m.stacks[0].range) == ("Open Sans Bold", "1536-1791")
    assert [g.id for g in m.stacks[0].glyphs] == [0x627, 0x644]
    assert gf.is_glyph_pbf(out)


def test_absent_open_sans_range_and_nothing_to_add():
    """No Open Sans file and no fallback glyph in the blocks: an empty but
    valid fontstack (not None), so the build ships a range file."""
    fallback = rng_pbf("Noto", "1536-1791", [glyph(0x700)])
    out = gf.merge_range(None, fallback, [(0x600, 0x6FF)], name="Open Sans Bold", range_key="1536-1791")
    assert decoded(out) == {}


def test_primary_without_a_fontstack_gets_a_complete_one():
    out = gf.merge_range(b"", rng_pbf("Noto", "1536-1791", [glyph(0x627)]), [(0x600, 0x6FF)],
                         name="Open Sans Bold", range_key="1536-1791")
    strict_decode(out)


def test_fallback_duplicates_and_id_position():
    """A fallback with a codepoint twice contributes it once (the first);
    an id written after the other fields is still recognised."""
    fallback = rng_pbf("Noto", "1536-1791", [glyph(0x627, fill=3, id_last=True), glyph(0x627, fill=4),
                                             glyph(0x628, id_last=True)])
    primary = rng_pbf("Open Sans", "1536-1791", [])
    got = decoded(gf.merge_range(primary, fallback, [(0x600, 0x6FF)]))
    assert set(got) == {0x627, 0x628} and got[0x627].bitmap[:1] == b"\x03"


def test_only_the_first_primary_fontstack_is_extended():
    two = rng_pbf("Open Sans", "1536-1791", [glyph(0x600)]) + rng_pbf("Other", "1536-1791", [glyph(0x601)])
    out = gf.merge_range(two, rng_pbf("Noto", "1536-1791", [glyph(0x627)]), [(0x600, 0x6FF)])
    m = strict_decode(out)
    assert [[g.id for g in s.glyphs] for s in m.stacks] == [[0x600, 0x627], [0x601]]


def test_strict_decoder_rejects_what_it_should():
    ok = rng_pbf("Open Sans", "0-255", [glyph(0x41)])
    strict_decode(ok)
    bad = {
        "no range": _ld(1, _ld(1, b"Open Sans") + glyph(0x41)),
        "duplicate id": rng_pbf("Open Sans", "0-255", [glyph(0x41), glyph(0x41)]),
        "bitmap size": _ld(1, _ld(1, b"x") + _ld(2, b"0-255") + _ld(3, b"\x08\x41" + _ld(2, b"\0" * 5)
                                                                    + b"\x18\x05\x20\x05\x28\x00\x30\x00\x38\x05")),
        "missing advance": _ld(1, _ld(1, b"x") + _ld(2, b"0-255") + _ld(3, b"\x08\x41\x18\x00\x20\x00\x28\x00\x30\x00")),
        "packed-looking id": _ld(1, _ld(1, b"x") + _ld(2, b"0-255") + _ld(3, _ld(1, b"\x41")
                                                                          + b"\x18\x00\x20\x00\x28\x00\x30\x00\x38\x00")),
        "unknown field": _ld(1, _ld(1, b"x") + _ld(2, b"0-255") + b"\x20\x01"),
    }
    from google.protobuf.message import DecodeError
    for why, pbf in bad.items():
        with pytest.raises((AssertionError, DecodeError)):
            strict_decode(pbf)
            pytest.fail(why)


# ---- the pinned ranges ---------------------------------------------------------

LOCK = va.load_lock()
FB = va.font_fallback(LOCK)
assert FB is not None
STACK_NAMES = LOCK["fonts"]["fontstacks"]
OS_RANGES = {(fr.stack, fr.range_key): fr for fr in va.font_ranges(LOCK)}
NOTO_RANGES = {(fr.stack, fr.range_key): fr for fr in FB.ranges}
ALL_BLOCKS = [b for bl in FB.scripts.values() for b in bl]
MERGE_RANGES = gf.ranges_for(ALL_BLOCKS)


_UNAVAILABLE: list[str] = []


@functools.cache
def _pinned(stack: str, rng: str) -> bytes | None:
    """A pinned range, from the verified cache or downloaded like a build;
    the test is skipped when neither works (once for the whole module)."""
    if _UNAVAILABLE:
        pytest.skip(_UNAVAILABLE[0])
    fr = OS_RANGES.get((stack, rng)) or NOTO_RANGES[(stack, rng)]
    if fr.sha256 is None:
        return None
    try:
        return va.fetch_verified(fr.url, fr.sha256, attempts=2)
    except va.DownloadError as e:
        _UNAVAILABLE.append(f"pinned glyph ranges not in the cache and not downloadable: {e}")
        pytest.skip(_UNAVAILABLE[0])


def _range_of(cp: int) -> str:
    s = cp // 256 * 256
    return f"{s}-{s + 255}"


def shipped(stack: str, rng: str, scripts) -> bytes | None:
    """The range file a build that detected ``scripts`` ships for ``stack``."""
    blocks = [b for s in sorted(scripts) for b in FB.scripts[s]]
    prim = _pinned(stack, rng)
    if rng not in gf.ranges_for(blocks):
        return prim
    fb = _pinned(FB.for_stack[stack], rng)
    if fb is None:
        return prim
    return gf.merge_range(prim, fb, blocks, name=STACK_NAMES[stack], range_key=rng)


def shipped_ids(stack: str, scripts) -> set[int]:
    """Glyph ids across the merge ranges and the ranges holding what
    uppercasing or the bidi controls could produce."""
    ids: set[int] = set()
    for rng in sorted(set(MERGE_RANGES) | {"7168-7423", "8192-8447", "11520-11775"}):
        data = shipped(stack, rng, scripts)
        if data is not None:
            ids |= gf.glyph_ids(data)
    return ids


def composite_oracle(primary: bytes, fallback: bytes, blocks, name: str) -> list[tuple]:
    """@mapbox/glyph-pbf-composite's combine([primary, fallback-in-blocks],
    name), as tileserver-gl composes font stacks: decode both, keep the
    first font's glyph for an id, append the others (their ``top`` moved by
    FALLBACK_TOP_SHIFT, as ours are by design), sort by id. Returned as
    comparable tuples (id, bitmap, width, height, left, top, advance) plus
    the fontstack name and range. (The npm module itself was run over every
    subset of scripts for the report, before Mtavruli and Nuskhuri joined
    Georgian: 720 files, 103,152 glyphs, 0 diffs.)"""
    p = GlyphsMsg()
    p.ParseFromString(primary)
    f = GlyphsMsg()
    f.ParseFromString(fallback)
    seen = {g.id for g in p.stacks[0].glyphs}
    rows = [(g.id, g.bitmap, g.width, g.height, g.left, g.top, g.advance) for g in p.stacks[0].glyphs]
    for g in f.stacks[0].glyphs:
        if g.id not in seen and any(a <= g.id <= b for a, b in blocks):
            # by design, fallback glyphs move onto the Open Sans baseline
            rows.append((g.id, g.bitmap, g.width, g.height, g.left,
                         g.top + gf.FALLBACK_TOP_SHIFT, g.advance))
            seen.add(g.id)
    rows.sort()
    return [(name, p.stacks[0].range)] + rows


def _as_rows(pbf: bytes) -> list[tuple]:
    m = strict_decode(pbf)
    assert len(m.stacks) == 1
    st = m.stacks[0]
    return [(st.name, st.range)] + sorted((g.id, g.bitmap, g.width, g.height, g.left, g.top, g.advance)
                                          for g in st.glyphs)


SUBSETS = [c for n in range(1, len(FB.scripts) + 1) for c in itertools.combinations(sorted(FB.scripts), n)]


def test_merge_matches_the_composite_oracle_for_every_subset_of_scripts():
    """Every one of the 63 script subsets x 3 stacks x the ranges it
    touches: same glyph ids, bitmaps and metrics as the first-wins
    composite, and strictly valid."""
    checked = 0
    for subset in SUBSETS:
        blocks = [b for s in subset for b in FB.scripts[s]]
        for stack, fb_stack in FB.for_stack.items():
            for rng in gf.ranges_for(blocks):
                prim, fb = _pinned(stack, rng), _pinned(fb_stack, rng)
                ours = gf.merge_range(prim, fb, blocks, name=STACK_NAMES[stack], range_key=rng)
                assert _as_rows(ours) == composite_oracle(prim, fb, blocks, STACK_NAMES[stack]), \
                    (subset, stack, rng)
                checked += 1
    assert checked == 936


def test_merged_ranges_keep_every_open_sans_byte():
    for stack in FB.for_stack:
        for rng in MERGE_RANGES:
            prim = _pinned(stack, rng)
            out = shipped(stack, rng, FB.scripts)
            # same fontstack header and Open Sans glyphs, then the additions
            (p_stack,) = [v for f, v, _ in gf._fields(prim)]
            (o_stack,) = [v for f, v, _ in gf._fields(out)]
            assert o_stack.startswith(p_stack), (stack, rng)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_vendored_maplibre_parses_every_merged_range(tmp_path):
    files = {}
    for stack in FB.for_stack:
        for rng in MERGE_RANGES:
            p = tmp_path / f"{stack}-{rng}.pbf"
            p.write_bytes(shipped(stack, rng, FB.scripts))
            files[str(p)] = gf.glyph_ids(p.read_bytes())
    res = subprocess.run([NODE, str(PARSER_JS), *files], capture_output=True, text=True, timeout=120)
    lines = [json.loads(line) for line in res.stdout.splitlines()]
    assert res.returncode == 0, [x for x in lines if "error" in x] or res.stderr
    assert len(lines) == len(files) == 24
    for x in lines:
        assert len(x["ids"]) == len(set(x["ids"])) == x["n"]
        assert set(x["ids"]) == files[x["file"]]


def _sdf_profile(pbf: bytes) -> tuple[int, int]:
    """(steepest step between neighbouring pixels, highest value on a
    bitmap's outer row) over a range file. An SDF encodes distance as
    255 * (1 - (d / radius + cutoff)): the step is 255 / radius (32 for
    radius 8) and the outer row, buffer px outside the box, is bounded by
    the cutoff."""
    step = border = 0
    for g in decoded(pbf).values():
        if not g.bitmap:
            continue
        w = g.width + 6
        rows = [g.bitmap[i:i + w] for i in range(0, len(g.bitmap), w)]
        step = max(step, *(abs(r[i + 1] - r[i]) for r in rows for i in range(w - 1)))
        border = max(border, *rows[0], *rows[-1])
    return step, border


def test_sdf_parameters_match():
    """Both generators (node-fontnik for openmaptiles, maplibre/font-maker
    for Noto; both sdf-glyph-foundry): 24 px glyphs, 3 px buffer (every
    bitmap (w+6)*(h+6), checked by strict_decode), radius 8 (steepest step
    32), the same cutoff (outer-row maximum within a few units), and the
    same em (Cyrillic U+0500-0513, in both 1280-1535 files: equal heights
    to within 1 px)."""
    for stack, fb_stack in FB.for_stack.items():
        profiles = {}
        for s in (stack, fb_stack):
            for rng in MERGE_RANGES:
                prof = _sdf_profile(_pinned(s, rng))
                if prof != (0, 0):  # not only empty glyphs (U+FEFF)
                    profiles[(s, rng)] = prof
        assert {p[0] for p in profiles.values()} == {32}, profiles
        borders = [p[1] for p in profiles.values()]
        assert max(borders) - min(borders) <= 8, profiles
        os_ = decoded(_pinned(stack, "1280-1535"))
        noto = decoded(_pinned(fb_stack, "1280-1535"))
        common = sorted(set(os_) & set(noto))
        assert len(common) == 20
        assert all(abs(os_[c].height - noto[c].height) <= 1 for c in common)
        assert sum(abs(os_[c].height - noto[c].height) for c in common) <= 3


def test_noto_sits_one_twentyfourth_em_higher():
    """The generators' `top` differs by one: the same outline (Cyrillic
    U+0500-0513, identical height in both fonts) gets top + 1 from
    font-maker, zero-size glyphs -25 vs -26. MapLibre places a glyph at
    y = -top, so Noto glyphs are drawn 1/24 em HIGHER than Open Sans (4 px
    at 96 px, measured in Chromium: flat-bottomed U+05DD ends 4 px above
    'H'). docs/viewer-supply-chain.md says "lower"."""
    for stack, fb_stack in FB.for_stack.items():
        os_ = decoded(_pinned(stack, "1280-1535"))
        noto = decoded(_pinned(fb_stack, "1280-1535"))
        dtop = [noto[c].top - os_[c].top for c in sorted(set(os_) & set(noto))]
        assert dtop == [1] * 20, (stack, dtop)
        assert decoded(_pinned(stack, "65024-65279"))[0xFEFF].top == -26
        assert decoded(_pinned(fb_stack, "65024-65279"))[0xFEFF].top == -25


def test_merged_glyphs_share_the_open_sans_baseline():
    out = decoded(shipped("OpenSansRegular", "1280-1535", FB.scripts))
    noto = decoded(_pinned("NotoSansRegular", "1280-1535"))
    os_ = decoded(_pinned("OpenSansRegular", "1280-1535"))
    # a merged Armenian glyph must sit where the same Noto outline would if
    # it had Open Sans's convention (the offset measured on U+0500-0513)
    offset = noto[0x500].top - os_[0x500].top
    assert out[0x531].top == noto[0x531].top - offset


def test_pinned_open_sans_has_no_placeholder_that_hides_noto_ink():
    for stack, fb_stack in FB.for_stack.items():
        for rng in MERGE_RANGES:
            os_ = decoded(_pinned(stack, rng))
            noto = decoded(_pinned(fb_stack, rng))
            for cp, g in os_.items():
                if any(a <= cp <= b for a, b in ALL_BLOCKS) and cp in noto:
                    assert (g.width, g.height) != (0, 0) or (noto[cp].width, noto[cp].height) == (0, 0), \
                        (stack, hex(cp))


KNOWN_NOT_IN_NOTO = {0x061D, 0xFBC2, 0x05EF}  # newer than the pinned Noto build


def test_every_assigned_codepoint_of_the_blocks_is_covered():
    for stack in FB.for_stack:
        ids = shipped_ids(stack, FB.scripts)
        for name, blocks in FB.scripts.items():
            assigned = {cp for a, b in blocks for cp in range(a, b + 1)
                        if unicodedata.category(chr(cp)) != "Cn"}
            assert assigned - ids <= KNOWN_NOT_IN_NOTO, (stack, name, sorted(map(hex, assigned - ids))[:10])


@pytest.mark.parametrize("what, text", [
    ("Arabic-Indic digits", "٠١٢٣٤٥٦٧٨٩"),
    ("Extended Arabic-Indic digits", "۰۱۲۳۴۵۶۷۸۹"),
    ("RTL plugin shaping: lam-alef and final/medial forms", "ﻻﻵﺎﻟﻠﮋﭘ"),
    ("Hebrew niqqud and cantillation", "יְרוּשָׁלַיִם֑֥"),
    ("Hebrew presentation forms", "יִשׁבּﭏ"),
    ("Thai combining vowels and tone marks", "กี้ ที่นี่ ปั้น"),
    ("Lao combining vowels and tone marks", "ເຂົ້າ ນີ້ ຫຼວງ"),
    ("Georgian Mkhedruli", "თბილისი"),
    ("Armenian", "Երևան Հայաստան"),
    ("Mixed Latin + Arabic, digits in RTL", "Café القدس 12"),
])
def test_label_text_is_covered(what, text):
    """Every character of these labels has a glyph in each stack (the
    space and Latin come from the untouched 0-255 range)."""
    for stack in FB.for_stack:
        ids = shipped_ids(stack, FB.scripts) | gf.glyph_ids(_pinned(stack, "0-255"))
        missing = {c for c in text if ord(c) not in ids}
        assert not missing, (what, stack, [f"U+{ord(c):04X}" for c in missing])


def test_bidi_controls_are_not_in_open_sans_and_need_no_glyph():
    """ZWNJ/ZWJ/LRM/RLM (U+200C-200F) are in no pinned font and no merged
    block. MapLibre skips a codepoint with no glyph (zero advance), which is
    what these zero-width controls should get: rendered "me\\u200cdan" (Persian) and
    "\\u200fshalom\\u200e" (Hebrew) in Chromium without a gap or error. Guard: were one
    ever given a visible glyph, it would draw a box."""
    for stack in FB.for_stack:
        ids = gf.glyph_ids(_pinned(stack, "8192-8447"))
        assert not ids & {0x200C, 0x200D, 0x200E, 0x200F}
        assert 0x200B in ids  # zero width space: present, empty
        zwsp = decoded(_pinned(stack, "8192-8447"))[0x200B]
        assert (zwsp.width, zwsp.height, zwsp.advance) == (0, 0, 0)


VIEWER = (ROOT / "resources" / "viewer" / "index.html").read_text(encoding="utf-8")


def test_every_text_font_of_the_style_has_its_fallback():
    stacks = set()
    for m in re.finditer(r"""["']text-font["']\s*:\s*\[([^\]]*)\]""", VIEWER):
        fonts = re.findall(r"""["']([^"']+)["']""", m.group(1))
        assert len(fonts) == 1, f"a text-font with {len(fonts)} fonts would ask for a combined stack: {fonts}"
        stacks |= set(fonts)
    assert stacks == {"OpenSansRegular", "OpenSansBold", "OpenSansItalic"}
    assert FB.for_stack == {"OpenSansRegular": "NotoSansRegular", "OpenSansBold": "NotoSansMedium",
                            "OpenSansItalic": "NotoSansRegular"}
    assert 'glyphs": glyphUrl' in VIEWER


def _uppercase_needs(stack: str) -> dict[str, list[str]]:
    """Characters that `"text-transform": "uppercase"` (MapLibre calls
    toLocaleUpperCase, the same Unicode mapping as str.upper) turns a
    covered character into, which have no glyph."""
    ids = shipped_ids(stack, FB.scripts)
    out: dict[str, list[str]] = {}
    for name, blocks in FB.scripts.items():
        miss = sorted({f"U+{ord(u):04X}" for a, b in blocks for cp in range(a, b + 1)
                       if cp in ids for u in chr(cp).upper() if ord(u) not in ids and ord(u) > 0x7F})
        if miss:
            out[name] = miss
    return out


def test_style_uppercases_place_labels():
    """The premise of the next test: suburbs (Regular), states and countries
    (Bold) are drawn with text-transform uppercase."""
    assert VIEWER.count('"text-transform": "uppercase"') >= 3
    assert "ვაკე".upper() == "ᲕᲐᲙᲔ"  # Mkhedruli uppercases to Mtavruli (Unicode 11)


def test_uppercased_labels_keep_their_glyphs():
    for stack in FB.for_stack:
        assert _uppercase_needs(stack) == {}, stack


def test_uppercase_gap_is_only_georgian():
    """Everything else survives uppercasing (Armenian's own capitals are in
    its block; U+0587 ech-yiwn uppercases to two covered letters)."""
    for stack in FB.for_stack:
        assert set(_uppercase_needs(stack)) <= {"Georgian"}


def test_armenian_ligatures_are_merged():
    ids = shipped_ids("OpenSansRegular", {"Armenian"})
    assert set(ARMENIAN_LIGS) <= ids


# ---- label scan: what triggers a script ---------------------------------------

def mvt(*values: str, key: str = "name") -> bytes:
    feats = b"".join(_ld(2, _ld(2, _v(0) + _v(i)) + _ld(4, _v(9) + _v(2) + _v(2)))
                     for i in range(len(values)))
    layer = (_ld(1, b"place") + feats + _ld(3, key.encode())
             + b"".join(_ld(4, _ld(1, s.encode())) for s in values))
    return _ld(3, layer)


@pytest.mark.parametrize("text, script", [
    ("ۿ", "Arabic"),            # one rare letter
    ("٩", "Arabic"),            # one Arabic-Indic digit
    ("ﻻ", "Arabic"),            # a presentation form in the data itself
    ("ﭏ", "Hebrew"),            # Hebrew ligature alef-lamed only
    ("ְ", "Hebrew"),            # a lone niqqud
    ("և", "Armenian"),
    ("ჼ", "Georgian"),
    ("฿", "Thai"),              # the baht sign
    ("ໟ", "Lao"),
])
@pytest.mark.parametrize("key", ["name", "name:latin", "name_int"])
def test_a_single_rare_character_is_enough(text, script, key):
    for z, tile in ((0, mvt("x" + text, key=key)), (14, gzip.compress(mvt(text, key=key)))):
        assert tiles.fallback_scripts_in_tiles({(z, 0, 0): tile}, LOCK) == {script}


def test_script_found_at_any_zoom_among_other_tiles():
    tset = {(z, x, 0): mvt("Main Street") for z in range(15) for x in range(3)}
    tset[(3, 0, 0)] = mvt("شارع")          # only at a low zoom
    tset[(14, 9, 9)] = gzip.compress(mvt("רחוב"))  # only at the highest
    assert tiles.fallback_scripts_in_tiles(tset, LOCK) == {"Arabic", "Hebrew"}


def test_mtavruli_only_label_is_georgian():
    assert tiles.fallback_scripts_in_tiles({(14, 0, 0): mvt("ᲗᲑᲘᲚᲘᲡᲘ")}, LOCK) == {"Georgian"}


def test_armenian_ligature_only_label_is_armenian():
    assert tiles.fallback_scripts_in_tiles({(14, 0, 0): mvt("ﬓ")}, LOCK) == {"Armenian"}


def test_out_of_scope_scripts_are_not_detected():
    """Arabic Supplement/Extended-A (U+0750-077F, 08A0-08FF: some Pakistani
    and African languages), Syriac, Devanagari: no fallback, drawn blank."""
    for text in ("ݐݑ", "ࢠ", "ܐܪܡ", "नई दिल्ली"):
        assert tiles.fallback_scripts_in_tiles({(14, 0, 0): mvt(text)}, LOCK) == set()


def test_runtime_hud_labels_are_invisible_to_the_scan():
    """The HUD's GeoJSON `label` never reaches a tile; a map whose tiles
    have no Arabic gets no Arabic glyphs, whatever the HUD shows (known
    limit, docs/viewer-supply-chain.md)."""
    assert tiles.fallback_scripts_in_tiles({(14, 0, 0): mvt("Main Street")}, LOCK) == set()
    assert tiles.fallback_scripts_in_tiles({(14, 0, 0): mvt("شارع", key="label")}, LOCK) == {"Arabic"}


# ---- the build with fake servers -------------------------------------------------

FAKE_OS, FAKE_FB = "https://fonts.example", "https://fb.example/fonts"
SIX = dict(LOCK["fonts"]["fallback"]["scripts"])


def fake_served(url: str) -> bytes:
    """Distinct bytes per stack and range (identical files would share a
    cache entry, and a download failure of one would go unnoticed)."""
    stack, rng = url.rsplit("/", 2)[1:]
    stack, rng = urllib.parse.unquote(stack), rng[:-4]
    start = int(rng.split("-")[0])
    if url.startswith(FAKE_FB):
        return rng_pbf(stack, rng, [glyph(c, fill=2) for c in range(start, start + 256, 3)])
    return rng_pbf(stack, rng, [glyph(start + 1, fill=1)])


def fake_lock(*, os_absent=(), fb_absent=()) -> dict:
    stacks = dict(LOCK["fonts"]["fontstacks"])
    fb_stacks = dict(LOCK["fonts"]["fallback"]["fontstacks"])
    fb_ranges = pin.fallback_ranges({"scripts": SIX})
    lock = json.loads(json.dumps(LOCK))
    lock["fonts"]["base_url"] = FAKE_OS
    lock["fonts"]["ranges"] = {
        s: {r: None if (s, r) in os_absent else
            hashlib.sha256(fake_served(f"{FAKE_OS}/{n}/{r}.pbf")).hexdigest() for r in pin.RANGES}
        for s, n in stacks.items()}
    fb = lock["fonts"]["fallback"]
    fb["base_url"] = FAKE_FB
    fb["ranges"] = {s: {r: None if (s, r) in fb_absent else
                        hashlib.sha256(fake_served(f"{FAKE_FB}/{n}/{r}.pbf")).hexdigest()
                        for r in fb_ranges} for s, n in fb_stacks.items()}
    return lock


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_net(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(va, "BAKED", tmp_path / "baked")
    monkeypatch.setattr(va.time, "sleep", lambda s: None)
    monkeypatch.delenv("STREETZIM_ALLOW_FONT_ERRORS", raising=False)
    state = {"fail": (), "calls": [], "jitter": None, "offline": False}

    def urlopen(req, timeout=None):
        url = req.full_url
        state["calls"].append(url)
        if state["offline"] or any(f in url for f in state["fail"]):
            raise OSError("unreachable")
        if state["jitter"]:
            time.sleep(state["jitter"].random() * 0.002)
        return _Resp(fake_served(url))
    monkeypatch.setattr(va.urllib.request, "urlopen", urlopen)
    return state


def _merged_keys(fonts, plain):
    return {k for k in fonts if plain.get(k) != fonts[k]}


def test_all_six_scripts_merge_into_eighteen_ranges(fake_net):
    lock = fake_lock()
    plain = tiles.generate_sdf_font_glyphs(lock, scripts=set())
    fonts = tiles.generate_sdf_font_glyphs(lock, scripts=None)  # streamed / >200 MB
    changed = _merged_keys(fonts, plain)
    assert changed == {(s, r) for s in STACK_NAMES for r in MERGE_RANGES} | {("NotoSans", "OFL.txt")}
    for (s, r) in changed - {("NotoSans", "OFL.txt")}:
        strict_decode(fonts[(s, r)])


def test_thai_only_leaves_lao_out_of_the_shared_range(fake_net):
    fonts = tiles.generate_sdf_font_glyphs(fake_lock(), scripts={"Thai"})
    ids = gf.glyph_ids(fonts[("OpenSansBold", "3584-3839")])
    assert any(0xE00 <= i <= 0xE7F for i in ids) and not any(0xE80 <= i <= 0xEFF for i in ids)
    both = tiles.generate_sdf_font_glyphs(fake_lock(), scripts={"Thai", "Lao"})
    assert gf.glyph_ids(both[("OpenSansBold", "3584-3839")]) > ids


def test_open_sans_range_absent_from_the_lock_is_created(fake_net):
    lock = fake_lock(os_absent={("OpenSansItalic", "1536-1791")})
    fonts = tiles.generate_sdf_font_glyphs(lock, scripts={"Arabic"})
    m = strict_decode(fonts[("OpenSansItalic", "1536-1791")])
    assert (m.stacks[0].name, m.stacks[0].range) == ("Open Sans Italic", "1536-1791")
    assert {g.id for g in m.stacks[0].glyphs} == set(range(1536, 1792, 3))
    # a range absent from the lock that no script needs stays absent
    lock = fake_lock(os_absent={("OpenSansItalic", "1792-2047")})
    assert ("OpenSansItalic", "1792-2047") not in tiles.generate_sdf_font_glyphs(lock, scripts={"Arabic"})


def test_open_sans_range_failing_under_the_escape_hatch_gets_noto_only(fake_net, monkeypatch):
    monkeypatch.setenv("STREETZIM_ALLOW_FONT_ERRORS", "1")
    fake_net["fail"] = ("Open%20Sans%20Bold/1536-1791",)
    fonts = tiles.generate_sdf_font_glyphs(fake_lock(), scripts={"Arabic"})
    m = strict_decode(fonts[("OpenSansBold", "1536-1791")])
    assert 1537 not in {g.id for g in m.stacks[0].glyphs}  # the Open Sans glyph is gone
    assert m.stacks[0].name == "Open Sans Bold"


def test_fallback_range_absent_from_the_lock_is_skipped(fake_net):
    lock = fake_lock(fb_absent={("NotoSansMedium", "1536-1791")})
    fonts = tiles.generate_sdf_font_glyphs(lock, scripts={"Arabic"})
    assert gf.glyph_ids(fonts[("OpenSansBold", "1536-1791")]) == {1537}
    assert len(gf.glyph_ids(fonts[("OpenSansRegular", "1536-1791")])) > 1


def test_one_noto_weight_failing_under_the_escape_hatch(fake_net, monkeypatch, capsys):
    """Only NotoSansMedium unreachable: Bold keeps its blank Arabic while
    Regular and Italic get theirs, and the build says only "1 errors" -- not
    which script or stack lost its fallback (MEDIUM: worth a warning)."""
    monkeypatch.setenv("STREETZIM_ALLOW_FONT_ERRORS", "1")
    fake_net["fail"] = ("Noto%20Sans%20Medium/1536-1791",)
    fonts = tiles.generate_sdf_font_glyphs(fake_lock(), scripts={"Arabic"})
    assert gf.glyph_ids(fonts[("OpenSansBold", "1536-1791")]) == {1537}
    assert len(gf.glyph_ids(fonts[("OpenSansRegular", "1536-1791")])) > 1
    assert ("NotoSans", "OFL.txt") in fonts
    out = capsys.readouterr().out
    assert "1 errors" in out


def test_same_bytes_whatever_the_thread_count_and_completion_order(fake_net, monkeypatch):
    import concurrent.futures as cf
    real = cf.ThreadPoolExecutor
    lock = fake_lock()
    results = []
    for workers, seed in ((1, 0), (16, 1), (16, 2), (3, 3)):
        monkeypatch.setattr(cf, "ThreadPoolExecutor", lambda max_workers=None, _w=workers, **kw: real(_w))
        monkeypatch.setenv("STREETZIM_CACHE_DIR", str(Path(os.environ["STREETZIM_CACHE_DIR"]) / str(seed)))
        fake_net["jitter"] = random.Random(seed)
        results.append(tiles.generate_sdf_font_glyphs(lock, scripts={"Arabic", "Hebrew", "Thai"}))
    assert all(r == results[0] for r in results)  # same keys, same bytes


def test_offline_rebuild_from_the_cache_is_identical(fake_net):
    lock = fake_lock()
    first = tiles.generate_sdf_font_glyphs(lock, scripts=None)
    fake_net["offline"] = True
    n = len(fake_net["calls"])
    again = tiles.generate_sdf_font_glyphs(lock, scripts=None)
    assert again == first
    assert len(fake_net["calls"]) == n  # nothing fetched


def test_prefetched_image_cache_serves_the_fallback(fake_net, tmp_path, monkeypatch):
    lock = fake_lock()
    monkeypatch.setattr(va, "load_lock", lambda path=va.LOCK: lock)
    baked = tmp_path / "baked"
    pin.prefetch(baked)
    online = tiles.generate_sdf_font_glyphs(lock, scripts=None)
    # a fresh user cache, no network: only the baked copy
    monkeypatch.setenv("STREETZIM_CACHE_DIR", str(tmp_path / "empty"))
    fake_net["offline"] = True
    assert tiles.generate_sdf_font_glyphs(lock, scripts=None) == online


def test_merge_cost_is_small():
    """All 24 pinned ranges merge in well under a second (measured ~25 ms)."""
    for stack, fb_stack in FB.for_stack.items():
        for rng in MERGE_RANGES:
            _pinned(stack, rng), _pinned(fb_stack, rng)
    t = time.perf_counter()
    for stack in FB.for_stack:
        for rng in MERGE_RANGES:
            shipped(stack, rng, FB.scripts)
    assert time.perf_counter() - t < 2.0


def test_concurrent_merges_do_not_interfere():
    """merge_range keeps no state: the same merge in 8 threads at once."""
    prim, fb = _shared_pair()
    want = gf.merge_range(prim, fb, BLOCKS["Arabic"] + BLOCKS["Hebrew"])
    got = []

    def run():
        got.append(gf.merge_range(prim, fb, BLOCKS["Arabic"] + BLOCKS["Hebrew"]))
    ts = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert got == [want] * 8
