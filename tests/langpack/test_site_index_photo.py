"""site_index.py marks the regions whose base map has the sign reader
(search with a photo: map-config's "ocr"), read from the map file in
$STREETZIM_MAPS; a base not there keeps what the last index said."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytest.importorskip("libzim")
from libzim.writer import Creator, Hint, Item, StringProvider  # noqa: E402


class _I(Item):
    def __init__(self, p, d):
        super().__init__(); self._p, self._d = p, d
    def get_path(self): return self._p
    def get_title(self): return self._p
    def get_mimetype(self): return "application/json"
    def get_contentprovider(self): return StringProvider(self._d)
    def get_hints(self): return {Hint.FRONT_ARTICLE: False}


def _zim(path: Path, cfg: dict) -> None:
    with Creator(str(path)) as c:
        c.add_item(_I("map-config.json", json.dumps(cfg).encode()))


def _region(rec: Path, rid: str, base: str) -> None:
    (rec / rid).mkdir(parents=True)
    (rec / rid / "region.json").write_text(json.dumps({
        "id": rid, "title": rid.title(), "base": {"file": base, "size": 1},
        "menu": [{"lang": "de", "recipe": f"recipes/{rid}/de.json"}]}))


def _index(rec: Path, maps: Path) -> dict:
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "site_index.py"), str(rec)],
                       capture_output=True, text=True, env={**os.environ, "STREETZIM_MAPS": str(maps)})
    assert r.returncode == 0, r.stderr
    return {x["id"]: x.get("photo_search") for x in json.loads((rec / "index.json").read_text())["regions"]}


def test_photo_search_from_the_base_map(tmp_path):
    maps, rec = tmp_path / "maps", tmp_path / "recipes"
    maps.mkdir()
    _zim(maps / "osm-kyiv-2026-10-31.zim", {"ocr": "ocr/manifest.json"})
    _zim(maps / "osm-france-2026-10-23.zim", {})
    _region(rec, "kyiv", "osm-kyiv-2026-10-31.zim")
    _region(rec, "france", "osm-france-2026-10-23.zim")
    _region(rec, "gone", "osm-gone-2026-10-01.zim")
    assert _index(rec, maps) == {"kyiv": True, "france": False, "gone": None}
    # the maps moved away: the last index's answer stands
    assert _index(rec, tmp_path / "elsewhere") == {"kyiv": True, "france": False, "gone": None}
