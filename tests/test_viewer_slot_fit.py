"""The viewer files fit their fixed slots, with room to spare
(cloud/viewer_slots.py, docs/viewer-slots.md).

Published ZIMs carry index.html, places.html and routing-worker.js in
fixed-size uncompressed slots; a file that outgrows its slot cannot be
patched in, only re-packed. pad_to_slot refuses an overflow at packing time;
check_viewer_fits refuses it when create_osm_zim.py or
cloud/repackage_zim.py starts; this test refuses it, with a margin, in CI.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cloud.viewer_slots import (  # noqa: E402
    SLOT_SIZES, UI_REGION_TABLES, VIEWER_FILES, check_viewer_fits, pad_to_slot, registry_map_configs,
    select_ui_tables, slot_free, ui_languages, viewer_page)

VIEWER = ROOT / "resources" / "viewer"
# Room each slot must keep. One more UI table adds ~3.5 KB to places.html and
# ~6.8 KB to index.html, compressed (docs/i18n.md#adding-a-language); a change
# that leaves less than this fails here, before the slot itself is the limit.
MARGIN = 16 * 1024


@pytest.mark.parametrize("name", VIEWER_FILES)
def test_viewer_file_fits_its_slot_with_margin(name: str) -> None:
    """For every map the registry describes: the file as that ZIM carries it
    (its UI tables: cloud/viewer_slots.py ui_languages)."""
    size = max(len(viewer_page(name, VIEWER, cfg)) for cfg in registry_map_configs())
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


def _tables(page: bytes) -> set[str]:
    m = re.search(rb'<script type="application/json" id="sz-i18n">(.*?)</script>', page, re.S)
    assert m, "no sz-i18n block"
    return set(json.loads(m.group(1))["t"])


def test_a_zim_carries_the_base_tables_and_its_own() -> None:
    """The built viewer inlines every table; a ZIM's copy keeps the core
    languages' and Swedish, and its own: map-config language, and the first
    UI_REGION_TABLES of regionLanguages (largest first) that have a table
    (Caucasus: ka, hy, az, ku of thirteen)."""
    from streetzim.languages import MATRIX_TILE_LANGUAGES
    base = set(MATRIX_TILE_LANGUAGES) | {"sv"}
    assert ui_languages(None) == base == ui_languages({})
    assert ui_languages({"language": "rm", "regionLanguages": ["lt", "LV", "et-EE"]}) == base | {"rm", "lt", "lv", "et"}
    row = [f"q{i}" for i in range(UI_REGION_TABLES + 3)]
    assert ui_languages({"regionLanguages": row}) == base | set(row[:UI_REGION_TABLES])
    assert ui_languages({"regionLanguages": ["ru", *row]}, {"q7", "q2", "ru"}) == base | {"q2", "q7"}   # ru: base
    # base languages first in a row (Europe's: en, de, ru, fr, ...) do not use up the six
    assert ui_languages({"regionLanguages": ["en", "de", "ru", "fr", *row]}, base | set(row)) == base | set(row[:UI_REGION_TABLES])
    page = b'<p>x</p><script type="application/json" id="sz-i18n">' + json.dumps(
        {"v": 2, "k": "K", "t": {"de": "D", "lt": "L", "zz": "Z"}, "m": {"lt": {"_machine": True}, "zz": {}}}).encode() + b"</script>"
    out = select_ui_tables(page, {"de", "lt"})
    block = json.loads(re.search(rb'id="sz-i18n">(.*?)</script>', out, re.S).group(1))
    assert block == {"v": 2, "k": "K", "t": {"de": "D", "lt": "L"}, "m": {"lt": {"_machine": True}}}
    assert out.startswith(b"<p>x</p>") and out.endswith(b"</script>")
    assert select_ui_tables(page, {"de", "lt", "zz", "fr"}) == page             # nothing to drop
    assert select_ui_tables(b"<p>no tables</p>", {"de"}) == b"<p>no tables</p>"


@pytest.mark.parametrize("name", ["index.html", "places.html"])
def test_the_built_pages_tables_per_map(name: str) -> None:
    built = _tables((VIEWER / name).read_bytes())
    cfg = {"regionLanguages": ["lt", "lv", "et"]}
    assert _tables(viewer_page(name, VIEWER, cfg)) == ui_languages(cfg, built)
    assert _tables(viewer_page(name, VIEWER, None)) == ui_languages(None, built)
    # six own languages without a table before one with: that one still counts
    cfg = {"regionLanguages": [f"x{i}" for i in range(UI_REGION_TABLES)] + ["sr"]}
    assert "sr" in built and "sr" in _tables(viewer_page(name, VIEWER, cfg))


def test_check_viewer_fits_for_one_map_and_for_the_registry(tmp_path: Path) -> None:
    """A table only one registry map carries can overflow that map's slot."""
    big = "x" * slot_free("places.html", 0)                                 # over the slot with the block around it
    page = ('<script type="application/json" id="sz-i18n">'
            + json.dumps({"v": 2, "k": "K", "t": {"de": "D", "lt": big}, "m": {}}) + "</script>")
    (tmp_path / "places.html").write_text(page)
    check_viewer_fits(tmp_path, {"regionLanguages": ["fr"]})                  # lt dropped: fits
    with pytest.raises(SystemExit, match=r"places\.html"):
        check_viewer_fits(tmp_path, {"regionLanguages": ["lt"]})
    with pytest.raises(SystemExit, match=r"places\.html"):
        check_viewer_fits(tmp_path)                                            # the Baltics' row has lt


def test_a_built_zim_gets_its_tables(tmp_path: Path) -> None:
    """streetzim/zim_writer.py _add_viewer writes index.html and places.html
    with the map's tables only."""
    from streetzim import zim_writer
    items: dict[str, bytes] = {}

    class Item:
        def __init__(self, path, title, mime, content, *a, **k):
            items[path] = content

    class Creator:
        def add_item(self, item):
            pass
    js, css = tmp_path / "m.js", tmp_path / "m.css"
    js.write_text("//")
    css.write_text("/**/")
    zim_writer._add_viewer(Creator(), Item, maplibre_js_path=str(js), maplibre_css_path=str(css),
                           viewer_html_path=str(VIEWER / "index.html"),
                           map_config={"regionLanguages": ["lt", "lv", "et"]}, name="t")
    for name in ("index.html", "places.html"):
        assert _tables(items[name]) == ui_languages({"regionLanguages": ["lt", "lv", "et"]}, _tables((VIEWER / name).read_bytes())), name

