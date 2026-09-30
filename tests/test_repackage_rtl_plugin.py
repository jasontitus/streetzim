"""repackage_zim.py's viewer swap adds MapLibre's RTL text plugin and its
map-config key to a ZIM built before them, so the swapped viewer shapes
Arabic/Hebrew labels there too; a source that has them keeps them."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("libzim")
from libzim.reader import Archive  # noqa: E402
from libzim.writer import Creator, Hint, Item, StringProvider  # noqa: E402

from streetzim import viewer_assets as va  # noqa: E402

ENTRY = va.RTL_TEXT_PLUGIN_ENTRY


class _Item(Item):
    def __init__(self, path, title, mime, data, front=False):
        super().__init__()
        self._a = (path, title, mime, data, front)

    def get_path(self): return self._a[0]
    def get_title(self): return self._a[1]
    def get_mimetype(self): return self._a[2]
    def get_contentprovider(self): return StringProvider(self._a[3])
    def get_hints(self): return {Hint.FRONT_ARTICLE: self._a[4], Hint.COMPRESS: True}


def _src(path: Path, config: dict, extra: dict[str, bytes] | None = None) -> Path:
    with Creator(str(path)) as c:
        for k, v in {"Title": "t", "Description": "d", "Language": "eng",
                     "Creator": "c", "Publisher": "p", "Date": "2026-09-28",
                     "Name": "n"}.items():
            c.add_metadata(k, v)
        c.add_item(_Item("index.html", "Monaco", "text/html",
                         b"<html><body>old viewer</body></html>", front=True))
        c.add_item(_Item("map-config.json", "Map Config", "application/json",
                         json.dumps(config).encode()))
        for p, data in (extra or {}).items():
            c.add_item(_Item(p, p, "application/javascript", data))
        c.set_mainpath("index.html")
    return path


def _read(arc: Archive, path: str) -> bytes:
    return bytes(arc.get_entry_by_path(path).get_item().content)


OLD_CONFIG = {"name": "Monaco", "center": [7.42, 43.74], "zoom": 13, "hasRouting": True}


def test_old_zim_gains_the_plugin_and_the_key(tmp_path):
    from cloud.repackage_zim import repackage
    src = _src(tmp_path / "src.zim", OLD_CONFIG)
    assert not Archive(str(src)).has_entry_by_path(ENTRY)
    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=True)
    arc = Archive(str(dst))
    assert _read(arc, ENTRY) == va.rtl_text_plugin_entry()
    assert arc.get_entry_by_path(ENTRY).get_item().mimetype == "application/javascript"
    cfg = json.loads(_read(arc, "map-config.json"))
    assert cfg == {**OLD_CONFIG, "rtlTextPlugin": ENTRY}


def test_without_the_viewer_swap_nothing_is_added(tmp_path):
    from cloud.repackage_zim import repackage
    src = _src(tmp_path / "src.zim", OLD_CONFIG)
    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=False)
    arc = Archive(str(dst))
    assert not arc.has_entry_by_path(ENTRY)
    assert json.loads(_read(arc, "map-config.json")) == OLD_CONFIG


def test_a_source_that_has_them_keeps_them(tmp_path):
    from cloud.repackage_zim import repackage
    cfg = {**OLD_CONFIG, "rtlTextPlugin": ENTRY}
    raw = json.dumps(cfg).encode()
    src = _src(tmp_path / "src.zim", cfg, {ENTRY: b"/* the source's copy */"})
    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=True)
    arc = Archive(str(dst))
    assert _read(arc, ENTRY) == b"/* the source's copy */"
    assert _read(arc, "map-config.json") == raw          # passed through untouched
