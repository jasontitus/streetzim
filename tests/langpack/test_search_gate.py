"""search_gate.py: Kiwix's search (libzim suggestions) must find a merged
file's pack-only article titles. libzim answers from the file's
X/title/xapian, and a merged file keeps the base's (python-libzim writes
one even with full-text indexing off): it knows only the base's entries, so
the pack articles are not found. The gate FAILs on every merge today
(production Greece el: 0 of 8 by suggestion and by full text, 2026-10-22)
and passes where the title index holds the pack's articles (the pack file
itself)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack.conftest import tool  # noqa: E402


def _gate(merged: Path, base: Path, lang: str = "de") -> tuple[int, dict]:
    r = tool("search_gate.py", merged, base, "--lang", lang)
    return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])


def test_a_merge_hides_the_pack_articles_from_kiwix_search(lp: SimpleNamespace, tmp_path: Path) -> None:
    m = tmp_path / "m-de.zim"
    r = tool("merge_zim.py", lp.base, lp.packs["de"], "-o", m, "--lang", "de", "--region", "testland")
    assert r.returncode == 0, r.stdout + r.stderr
    rc, out = _gate(m, lp.base)
    assert rc == 1 and not out["pass"] and out["title_index"], out
    assert out["titles"] == out["pack_only_titles"] == 3 and out["suggest_found"] == 0, out
    # an entry whose language has no pack (English here) samples the first pack
    _, out2 = _gate(m, lp.base, "en")
    assert out2["lang"] == "de"


def test_found_where_the_title_index_has_them(lp: SimpleNamespace) -> None:
    rc, out = _gate(lp.packs["de"], lp.base)
    assert rc == 0 and out["pass"] and out["suggest_found"] == out["titles"] == 3, out


def test_a_file_without_packs_passes(lp: SimpleNamespace) -> None:
    """An entry with no pack (English on an English base without region
    packs): the base's own indexes find everything in it."""
    rc, out = _gate(lp.base, lp.base)
    assert rc == 0 and out["pass"] and "no pack in this file" in out["why"]
