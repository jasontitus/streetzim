"""cloud/swap_viewer_rust.py keeps each entry's front-article flag. It
wrote every page and redirect as no front article (2026-10-29: Taipei had
5,276 of its 5,963 pages front before a swap and 0 after; Bahrain, Thailand
and Switzerland (de) likewise), and Kiwix counts no articles and opens no
random one in such a file."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
pytest.importorskip("libzim")


def _fronts(path: Path) -> set[str]:
    from tools.langpack.index_pack import front_bitmap, is_front
    from tools.langpack.zimfmt import ZimReader
    z = ZimReader(str(path))
    bits = front_bitmap(z)
    return {d.path.decode() for d in z.iter_dirents() if d.ns == ord("C") and is_front(bits, d.index)}


def test_the_swap_keeps_the_front_articles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("streetzim.zim_writer")
    from tests.langpack import lpzims
    os.environ.setdefault("STREETZIM_ROOT", str(ROOT))
    spec = importlib.util.spec_from_file_location("swap_viewer_rust_front_under_test",
                                                  ROOT / "ops" / "cloud" / "swap_viewer_rust.py")
    assert spec and spec.loader
    svr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svr)
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: False)
    monkeypatch.setattr(svr, "_fs_type", lambda p: "ext4")
    src = tmp_path / "osm-testland-2026-10-13.zim"
    lpzims.write_base(src, slots=lpzims.VIEWER_V1,
                      pages=[("search/manama-73.html", "Manama", "The capital")],
                      front_redirects=[("~7", "Al Manamah", "search/manama-73.html")])
    before = _fronts(src)
    # the fixture's: its articles, the extra page and redirect (front); wiki-article/Gamma (a redirect, not)
    assert {"wiki-article/Alpha_Town", "search/manama-73.html", "~7"} <= before
    assert "wiki-article/Gamma" not in before
    dst = tmp_path / "out" / src.name
    dst.parent.mkdir()
    (tmp_path / "t").mkdir()
    svr.swap_viewer_rust(str(src), str(dst), tmp_dir=str(tmp_path / "t"))
    assert _fronts(dst) == before
