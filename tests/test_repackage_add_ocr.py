"""repackage_zim.py --add-ocr adds the sign reader (search with a photo,
docs/ocr.md) to a map built without it: the models for the scripts of its
names, chosen as a build with --ocr chooses them, the reader's entries in
place of any the source has, and map-config's ``ocr``; nothing else of the
map changes."""
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

from streetzim import ocr  # noqa: E402

CONFIG = {"name": "Testland", "center": [23.7, 37.9], "zoom": 12, "hasRouting": True}


class _Item(Item):
    def __init__(self, path, title, mime, data, front=False):
        super().__init__()
        self._a = (path, title, mime, data, front)

    def get_path(self): return self._a[0]
    def get_title(self): return self._a[1]
    def get_mimetype(self): return self._a[2]
    def get_contentprovider(self): return StringProvider(self._a[3])
    def get_hints(self): return {Hint.FRONT_ARTICLE: self._a[4], Hint.COMPRESS: True}


def _src(path: Path, names: list[tuple[str, str | None]], extra: dict[str, bytes] | None = None) -> Path:
    recs = [{"n": n, **({"nn": nn} if nn else {}), "t": "poi", "a": 37.9, "o": 23.7} for n, nn in names]
    with Creator(str(path)) as c:
        for k, v in {"Title": "t", "Description": "d", "Language": "eng", "Creator": "c",
                     "Publisher": "p", "Date": "2026-10-23", "Name": "n"}.items():
            c.add_metadata(k, v)
        c.add_item(_Item("index.html", "Map", "text/html", b"<html>viewer</html>", front=True))
        c.add_item(_Item("map-config.json", "Map Config", "application/json", json.dumps(CONFIG).encode()))
        c.add_item(_Item("search-data/manifest.json", "m", "application/json",
                         json.dumps({"total": len(recs), "chunks": {"ab": len(recs)}}).encode()))
        c.add_item(_Item("search-data/ab.json", "c", "application/json", json.dumps(recs).encode()))
        c.add_item(_Item("tiles/1/0/0.pbf", "t", "application/x-protobuf", b"\x1a\x02tile"))
        for p, data in (extra or {}).items():
            c.add_item(_Item(p, p, "application/octet-stream", data))
        c.set_mainpath("index.html")
    return path


def _read(arc: Archive, path: str) -> bytes:
    return bytes(arc.get_entry_by_path(path).get_item().content)


@pytest.fixture
def fake_entries(monkeypatch):
    """ocr.entries without downloads: one model per recognizer, recorded."""
    plans = []

    def entries(plan, lock=None):
        plans.append(plan)
        out = [(f"ocr/{k}.onnx", k, "application/octet-stream", k.encode() * 3)
               for k in (plan.detector, *plan.recognizers)]
        return out + [(ocr.MANIFEST_ENTRY, "manifest", "application/json",
                       json.dumps({"rec": [{"name": r} for r in plan.recognizers]}).encode())]
    monkeypatch.setattr(ocr, "entries", entries)
    return plans


GREEK = [("Plaka", "Πλάκα"), ("Athens", "Αθήνα"), ("Thermi", "Θέρμη"), ("Cafe", None)]


def test_the_reader_for_the_names_scripts_and_nothing_else_changed(tmp_path, fake_entries):
    from cloud.repackage_zim import repackage
    src = _src(tmp_path / "src.zim", GREEK)
    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=False, add_ocr=True)
    a, b = Archive(str(src)), Archive(str(dst))
    (plan,) = fake_entries
    assert plan.recognizers == ("greek", "latin-cjk")
    assert json.loads(_read(b, "map-config.json")) == {**CONFIG, "ocr": ocr.MANIFEST_ENTRY}
    assert _read(b, "ocr/greek.onnx") == b"greekgreekgreek"
    assert json.loads(_read(b, ocr.MANIFEST_ENTRY))["rec"] == [{"name": "greek"}, {"name": "latin-cjk"}]
    for p in ("index.html", "search-data/ab.json", "search-data/manifest.json", "tiles/1/0/0.pbf"):
        assert _read(b, p) == _read(a, p), p
    assert a.uuid != b.uuid      # a new book for Kiwix


def test_the_scripts_given_win_and_an_old_reader_is_replaced(tmp_path, fake_entries):
    from cloud.repackage_zim import repackage
    src = _src(tmp_path / "src.zim", GREEK, {"ocr/old.onnx": b"old", ocr.MANIFEST_ENTRY: b"{}"})
    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=False, add_ocr=True, ocr_scripts=["Cyrl"])
    b = Archive(str(dst))
    assert fake_entries[0].recognizers == ("cyrillic",)
    assert not b.has_entry_by_path("ocr/old.onnx")
    assert json.loads(_read(b, ocr.MANIFEST_ENTRY))["rec"] == [{"name": "cyrillic"}]


def test_without_add_ocr_nothing_is_added(tmp_path, fake_entries):
    from cloud.repackage_zim import repackage
    src = _src(tmp_path / "src.zim", GREEK)
    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=False)
    b = Archive(str(dst))
    assert not fake_entries and not b.has_entry_by_path(ocr.MANIFEST_ENTRY)
    assert json.loads(_read(b, "map-config.json")) == CONFIG


def test_the_names_sample_reads_own_script_names(tmp_path):
    from cloud.repackage_zim import _ocr_names
    src = Archive(str(_src(tmp_path / "src.zim", GREEK)))
    assert list(_ocr_names(src)) == ["Πλάκα", "Αθήνα", "Θέρμη", "Cafe"]
    assert list(_ocr_names(src, limit=2)) == ["Πλάκα", "Αθήνα"]
