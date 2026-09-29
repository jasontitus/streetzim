"""tools/check_full_profile.py: what CI asserts about a --profile full ZIM."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

pytest.importorskip("libzim")
from libzim.writer import Creator, Hint, Item, StringProvider  # noqa: E402

import check_full_profile as cfp  # noqa: E402

FULL = {"hasOvertureAddresses": True, "hasWikidata": True, "hasWikiArticles": True}
LICENCE = ("Map data: ODbL (OpenStreetMap); Place info: CC0 (Wikidata) / CC BY-SA 4.0 "
           "(Wikipedia); Address enrichment: Overture Maps Foundation")


class _Item(Item):
    def __init__(self, path: str, data: bytes):
        super().__init__()
        self._p, self._d = path, data

    def get_path(self): return self._p
    def get_title(self): return self._p
    def get_mimetype(self): return "application/json"
    def get_contentprovider(self): return StringProvider(self._d)
    def get_hints(self): return {Hint.FRONT_ARTICLE: False}


def _zim(tmp_path: Path, config: dict, licence: str) -> str:
    path = tmp_path / "m.zim"
    with Creator(str(path)).config_indexing(False, "eng") as c:
        c.set_mainpath("map-config.json")
        c.add_metadata("License", licence)
        c.add_item(_Item("map-config.json", json.dumps(config).encode()))
    return str(path)


def test_a_full_zim_passes(tmp_path, capsys):
    assert cfp.main([_zim(tmp_path, FULL, LICENCE)]) == 0
    assert "ok:" in capsys.readouterr().out


def test_missing_overture_fails_even_when_soft(tmp_path, capsys):
    z = _zim(tmp_path, {**FULL, "hasOvertureAddresses": False},
             LICENCE.replace("Overture", "Nobody"))
    assert cfp.main([z, "--soft-wikimedia"]) == 1
    err = capsys.readouterr().err
    assert "hasOvertureAddresses" in err and "credit Overture" in err


def test_missing_wikimedia_fails_hard_and_warns_soft(tmp_path, capsys):
    z = _zim(tmp_path, {**FULL, "hasWikiArticles": False}, LICENCE)
    assert cfp.main([z]) == 1
    assert "hasWikiArticles" in capsys.readouterr().err
    assert cfp.main([z, "--soft-wikimedia"]) == 0
    assert "::warning::map-config.json: hasWikiArticles" in capsys.readouterr().out
