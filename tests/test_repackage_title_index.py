"""repackage_zim.py must keep the ZIM's title index (Kiwix's search
suggestions). libzim only indexes front articles and the builder marks the
main page front; repackage used to re-add every entry as non-front, so the
output had no title index at all, on plain libzim-built sources too."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("libzim")
from libzim.reader import Archive  # noqa: E402
from libzim.suggestion import SuggestionSearcher  # noqa: E402
from libzim.writer import Creator, Hint, Item, StringProvider  # noqa: E402


class _Item(Item):
    def __init__(self, path, title, mime, data, front=False):
        super().__init__()
        self._a = (path, title, mime, data, front)

    def get_path(self): return self._a[0]
    def get_title(self): return self._a[1]
    def get_mimetype(self): return self._a[2]
    def get_contentprovider(self): return StringProvider(self._a[3])
    def get_hints(self): return {Hint.FRONT_ARTICLE: self._a[4], Hint.COMPRESS: True}


@pytest.mark.parametrize("swap_viewer", [False, True])
def test_title_index_survives_repackage(tmp_path, swap_viewer):
    from cloud.repackage_zim import repackage
    src = tmp_path / "src.zim"
    with Creator(str(src)).config_indexing(True, "en") as c:
        for k, v in {"Title": "t", "Description": "d", "Language": "eng",
                     "Creator": "c", "Publisher": "p", "Date": "2026-09-28",
                     "Name": "n"}.items():
            c.add_metadata(k, v)
        c.add_item(_Item("index.html", "Monaco", "text/html",
                         b"<html><body>Monaco map</body></html>", front=True))
        c.set_mainpath("index.html")
    assert Archive(str(src)).has_title_index

    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=swap_viewer)
    arc = Archive(str(dst))
    assert arc.has_title_index
    assert SuggestionSearcher(arc).suggest("mona").getEstimatedMatches() >= 1
