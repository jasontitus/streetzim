"""A ZIM's viewer carries the UI tables of the base languages and the map's
own (cloud/viewer_slots.py ui_languages), whichever tool writes it: the
build (tests/test_viewer_slot_fit.py), cloud/repackage_zim.py's viewer swap,
and cloud/patch_viewer_inplace.py (the language bases' in-place patch); the
full re-pack: ops/tests/test_swap_viewer_tables.py."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("libzim")
from libzim.reader import Archive  # noqa: E402

from cloud.viewer_slots import VIEWER_FILES, ui_languages  # noqa: E402
from tests.langpack import lpzims  # noqa: E402
from tests.test_repackage_rtl_plugin import OLD_CONFIG, _read, _src  # noqa: E402

VIEWER = ROOT / "resources" / "viewer"


def _tables(page: bytes) -> set[str]:
    m = re.search(rb'<script type="application/json" id="sz-i18n">(.*?)</script>', page, re.S)
    assert m, "no sz-i18n block"
    return set(json.loads(m.group(1))["t"])


def test_the_repackage_viewer_swap_keeps_the_maps_tables(tmp_path: Path) -> None:
    from cloud.repackage_zim import repackage
    cfg = {**OLD_CONFIG, "regionLanguages": ["lt", "lv", "et"]}
    src = _src(tmp_path / "src.zim", cfg)
    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=True)
    arc = Archive(str(dst))
    for name in ("index.html", "places.html"):
        got = _tables(_read(arc, name).split(b"<!--SZVSLOT1:")[0])
        assert got == ui_languages(cfg, _tables((VIEWER / name).read_bytes())), name


def test_the_in_place_patch_keeps_the_maps_tables(tmp_path: Path) -> None:
    base = tmp_path / "osm-testland-2026-10-26.zim"
    lpzims.write_base(base, slots=lpzims.VIEWER_V1, cfg_extra={"regionLanguages": ["lt"]})
    vd = tmp_path / "viewer"
    vd.mkdir()
    block = {"v": 2, "k": "K", "t": {"de": "D", "lt": "L", "zz": "Z"}, "m": {"lt": {"_machine": True}}}
    for name in VIEWER_FILES:
        body = (b'<script type="application/json" id="sz-i18n">' + json.dumps(block).encode() + b"</script>"
                if name != "routing-worker.js" else b"/* routing */")
        (vd / name).write_bytes(body)
    r = subprocess.run([sys.executable, str(ROOT / "cloud" / "patch_viewer_inplace.py"), str(base), "--viewer-dir", str(vd)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    arc = Archive(str(base))
    for name in ("index.html", "places.html"):
        assert _tables(_read(arc, name)) == {"de", "lt"}, name

