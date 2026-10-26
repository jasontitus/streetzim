"""cloud/swap_viewer_rust.py (the full re-pack of an older ZIM) writes the
viewer with the UI tables of the base languages and the map's own
(cloud/viewer_slots.py ui_languages), as the build and the other viewer
writers do (tests/test_viewer_tables_per_map.py)."""
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
from libzim.reader import Archive  # noqa: E402

from cloud.viewer_slots import ui_languages  # noqa: E402
from tests.test_repackage_rtl_plugin import OLD_CONFIG, _read, _src  # noqa: E402
from tests.test_viewer_tables_per_map import VIEWER, _tables  # noqa: E402


def test_the_full_viewer_swap_keeps_the_maps_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("streetzim.zim_writer")
    os.environ.setdefault("STREETZIM_ROOT", str(ROOT))
    spec = importlib.util.spec_from_file_location("swap_viewer_rust_tables_under_test",
                                                  ROOT / "ops" / "cloud" / "swap_viewer_rust.py")
    assert spec and spec.loader
    svr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svr)
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: False)
    monkeypatch.setattr(svr, "_fs_type", lambda p: "ext4")
    cfg = {**OLD_CONFIG, "language": "fi", "regionLanguages": ["lt", "lv", "et"]}
    src = _src(tmp_path / "src.zim", cfg)
    dst = tmp_path / "dst.zim"
    (tmp_path / "t").mkdir()
    svr.swap_viewer_rust(str(src), str(dst), tmp_dir=str(tmp_path / "t"))
    arc = Archive(str(dst))
    for name in ("index.html", "places.html"):
        got = _tables(_read(arc, name))
        assert got == ui_languages(cfg, _tables((VIEWER / name).read_bytes())), name
        assert {"fi", "lt", "lv", "et"} <= got and "hu" not in got, name
