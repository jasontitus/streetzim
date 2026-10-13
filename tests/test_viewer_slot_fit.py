"""The viewer files fit their fixed slots, with room to spare
(cloud/viewer_slots.py, docs/viewer-slots.md).

Published ZIMs carry index.html, places.html and routing-worker.js in
fixed-size uncompressed slots; a file that outgrows its slot cannot be
patched in, only re-packed. pad_to_slot refuses an overflow at packing time;
check_viewer_fits refuses it when create_osm_zim.py or
cloud/repackage_zim.py starts; this test refuses it, with a margin, in CI.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cloud.viewer_slots import (  # noqa: E402
    SLOT_SIZES, VIEWER_FILES, check_viewer_fits, pad_to_slot, slot_free)

VIEWER = ROOT / "resources" / "viewer"
# Room each slot must keep. One more UI table adds ~12.3 KB to places.html
# (docs/i18n.md#adding-a-language); a change that leaves less than this
# fails here, before the slot itself is the limit.
MARGIN = 16 * 1024


@pytest.mark.parametrize("name", VIEWER_FILES)
def test_viewer_file_fits_its_slot_with_margin(name: str) -> None:
    size = (VIEWER / name).stat().st_size
    free = slot_free(name, size)
    assert free >= MARGIN, (
        f"{name}: {size} B leaves {free} B of its {SLOT_SIZES[name]} B slot, under the "
        f"{MARGIN} B margin (docs/viewer-slots.md: shrink the viewer or plan bigger slots)")


@pytest.mark.parametrize("name", VIEWER_FILES)
def test_slot_free_is_what_pad_to_slot_allows(name: str) -> None:
    edge = slot_free(name, 0)
    assert slot_free(name, edge) == 0
    assert len(pad_to_slot(name, b"x" * edge)) == SLOT_SIZES[name]
    assert slot_free(name, edge + 1) == -1
    with pytest.raises(SystemExit, match="viewer slot overflow"):
        pad_to_slot(name, b"x" * (edge + 1))


def _viewer(tmp_path: Path, **over: int) -> Path:
    for name in VIEWER_FILES:
        size = over.get(name.replace(".", "_").replace("-", "_"), 1000)
        (tmp_path / name).write_bytes(b"x" * size)
    return tmp_path


def test_check_viewer_fits(tmp_path: Path) -> None:
    check_viewer_fits(VIEWER)                                   # the real viewer
    check_viewer_fits(_viewer(tmp_path))
    edge = slot_free("places.html", 0)
    check_viewer_fits(_viewer(tmp_path, places_html=edge))      # exactly full
    with pytest.raises(SystemExit, match=r"places\.html is \d+ B, 1 B over its 262144 B slot"):
        check_viewer_fits(_viewer(tmp_path, places_html=edge + 1))
    with pytest.raises(SystemExit, match=r"routing-worker\.js .* over"):
        check_viewer_fits(_viewer(tmp_path, places_html=10, routing_worker_js=200_000))
    (tmp_path / "places.html").unlink()                         # a missing file is no overflow
    (tmp_path / "routing-worker.js").write_bytes(b"")
    check_viewer_fits(tmp_path)


def test_create_osm_zim_checks_before_anything_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import create_osm_zim

    class Stop(Exception):
        pass

    def stop(**kw: object) -> None:
        raise Stop
    monkeypatch.setattr(create_osm_zim, "_openzim_options", stop)
    monkeypatch.setattr(create_osm_zim, "VIEWER_DIR", _viewer(tmp_path, index_html=2_000_000))
    with pytest.raises(SystemExit, match=r"index\.html is 2000000 B"):
        create_osm_zim.main([])
    monkeypatch.setattr(create_osm_zim, "VIEWER_DIR", _viewer(tmp_path))
    with pytest.raises(Stop):                                   # fits: the build goes on
        create_osm_zim.main([])


def test_repackage_checks_before_opening_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cloud import repackage_zim
    monkeypatch.setattr(repackage_zim, "VIEWER_DIR", _viewer(tmp_path, places_html=300_000))
    monkeypatch.setattr(sys, "argv", ["repackage_zim.py", str(tmp_path / "missing.zim"), str(tmp_path / "out.zim")])
    with pytest.raises(SystemExit, match=r"places\.html is 300000 B"):
        repackage_zim.main()
    # Without a viewer swap there is nothing to fit: it goes on (and fails
    # on the missing source instead).
    monkeypatch.setattr(sys, "argv", ["repackage_zim.py", "--no-swap-viewer",
                                      str(tmp_path / "missing.zim"), str(tmp_path / "out.zim")])
    with pytest.raises(BaseException) as e:
        repackage_zim.main()
    assert "viewer slot overflow" not in str(e.value)
