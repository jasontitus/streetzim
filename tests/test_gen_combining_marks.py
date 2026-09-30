"""tools/gen_combining_marks.py: the viewer's table of canonical combining
marks (the search fold, docs/search-prefix-locality.md) is current for the
running Python, and --check's rules across Unicode versions hold."""
from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

gcm = pytest.importorskip("gen_combining_marks")


def _block(path: Path) -> str:
    blocks = gcm._BLOCK.findall(path.read_text(encoding="utf-8"))  # pyright: ignore[reportPrivateUsage]
    assert len(blocks) == 1, path
    return blocks[0]


def test_committed_tables_are_current_for_this_python():
    for path in gcm.TARGETS:
        assert gcm.check_block(_block(path)) is None, \
            f"{path}: run python3.14 tools/gen_combining_marks.py"
    assert gcm.main(["--check"]) == 0


def test_every_viewer_carries_the_same_table():
    assert len({_block(p) for p in gcm.TARGETS}) == 1


def test_table_is_exactly_the_writers_combining_rule():
    version, cps = gcm.parse_block(_block(gcm.TARGETS[0]))
    have = set(gcm.combining_code_points())
    assert have <= cps
    if version == unicodedata.unidata_version:
        assert have == cps
    # Indic vowel signs and Thai vowels are marks with class 0: kept.
    for ch in "ोाुंிைาิ":
        assert ord(ch) not in cps, f"U+{ord(ch):04X}"
    # Accents, virama, dakuten, niqqud, harakat have a class: dropped.
    for ch in "゙्்ְَ่́̃":
        assert ord(ch) in cps, f"U+{ord(ch):04X}"


def test_render_roundtrips():
    cps = [0x300, 0x301, 0x302, 0x5B0, 0x1E94A]
    version, got = gcm.parse_block(gcm.render("9.9.9", cps))
    assert version == "9.9.9" and got == set(cps)


def test_same_version_requires_the_exact_block():
    cps = [0x300, 0x301, 0x5B0]
    block = gcm.render("15.0.0", cps)
    assert gcm.check_block(block, "15.0.0", cps) is None
    assert "differs" in (gcm.check_block(block, "15.0.0", [0x300, 0x301]) or "")
    edited = block.replace("0x05B0, 0x05B0", "0x05B0, 0x05B1")
    assert "differs" in (gcm.check_block(edited, "15.0.0", cps) or "")


def test_a_newer_table_passes_an_older_python_only_as_a_superset():
    new_cp = 0x0897   # ccc 230 in Unicode 16, unassigned in 15
    old = [0x300, 0x301]
    table = gcm.render("99.0.0", old + [new_cp])
    if unicodedata.category(chr(new_cp)) == "Cn":
        assert gcm.check_block(table, "15.0.0", old) is None
    # a mark the older Python knows but the table lacks
    assert "lacks" in (gcm.check_block(gcm.render("99.0.0", [0x300]), "15.0.0", old) or "")
    # an extra that is ASSIGNED (class 0) in the running Python is wrong
    wrong = gcm.render("99.0.0", old + [0x093E])
    assert "assigns" in (gcm.check_block(wrong, "15.0.0", old) or "")


def test_an_older_table_fails_a_newer_python():
    why = gcm.check_block(gcm.render("1.0.0", [0x300]), "15.0.0", [0x300])
    assert why and "regenerate" in why
