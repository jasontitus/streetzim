"""The builder writes MapLibre's RTL text plugin into the ZIM and names it in
map-config.json (the viewer's 137-rtl-text.js loads it only when a tile has
RTL labels). Also: the viewer, now carrying the dark theme and the inline
POI icons, still fits its in-place patch slot with room to grow."""
import json
import shutil

import pytest

from cloud.viewer_slots import SLOT_SIZES, pad_to_slot
from streetzim import viewer_assets as va
from streetzim import zim_writer

PLUGIN = va.VENDOR / va.RTL_TEXT_PLUGIN / va.RTL_TEXT_PLUGIN_ENTRY


class FakeItem:
    def __init__(self, path, title, mimetype, content, is_front=False, compress=True, namespace=None):
        self.path, self.mimetype, self.content = path, mimetype, content


class FakeCreator:
    def __init__(self):
        self.items = {}

    def add_item(self, item):
        self.items[item.path] = item


def test_vendored_plugin_matches_the_lock_file_and_carries_its_licence():
    lock = va.load_lock()[va.RTL_TEXT_PLUGIN]
    # Provenance: the npm tarball, as for MapLibre.
    assert lock["tarball"].startswith("https://registry.npmjs.org/@mapbox/mapbox-gl-rtl-text/")
    assert lock["integrity"].startswith("sha512-")
    files = va.vendored(va.RTL_TEXT_PLUGIN)
    assert set(files) == {va.RTL_TEXT_PLUGIN_ENTRY, "LICENSE.md"}
    data = PLUGIN.read_bytes()
    licence = files["LICENSE.md"].read_text()
    assert "Redistribution and use in source and binary forms" in licence  # BSD-2
    assert "Unicode" in licence                                            # ICU
    # It registers itself with MapLibre's worker hook.
    assert b"registerRTLTextPlugin" in data


def _build(tmp_path):
    js = tmp_path / "maplibre-gl.js"; js.write_text("//")
    css = tmp_path / "maplibre-gl.css"; css.write_text("/**/")
    html = tmp_path / "index.html"; html.write_text("<html></html>")
    c = FakeCreator()
    zim_writer._add_viewer(c, FakeItem, maplibre_js_path=js, maplibre_css_path=css,
                           viewer_html_path=html, map_config={}, name="t")
    zim_writer._add_map_config(c, FakeItem, map_config={"name": "t"}, has_wiki_articles=False)
    return c.items, json.loads(c.items["map-config.json"].content)


def test_plugin_entry_and_config_key_are_written_together(tmp_path):
    items, cfg = _build(tmp_path)
    entry = items[zim_writer.RTL_TEXT_PLUGIN_ENTRY]
    assert entry.mimetype == "application/javascript"
    assert cfg["rtlTextPlugin"] == zim_writer.RTL_TEXT_PLUGIN_ENTRY
    body = entry.content
    version = va.load_lock()[va.RTL_TEXT_PLUGIN]["version"]
    assert body.startswith(f"/*! @mapbox/mapbox-gl-rtl-text {version}".encode())
    assert b"Redistribution and use" in body.split(b"*/", 1)[0]
    assert body.endswith(PLUGIN.read_bytes())


def _vendor_copy(tmp_path):
    dest = tmp_path / "vendor"
    shutil.copytree(va.VENDOR, dest)
    return dest


def test_a_missing_copy_fails_the_build(tmp_path):
    """Required, like MapLibre: no build silently loses RTL shaping."""
    vendor = _vendor_copy(tmp_path)
    (vendor / va.RTL_TEXT_PLUGIN / va.RTL_TEXT_PLUGIN_ENTRY).unlink()
    with pytest.raises(va.IntegrityError, match="is missing"):
        va.rtl_text_plugin_entry(vendor=vendor)


def test_a_tampered_copy_fails_the_build(tmp_path):
    vendor = _vendor_copy(tmp_path)
    (vendor / va.RTL_TEXT_PLUGIN / va.RTL_TEXT_PLUGIN_ENTRY).write_bytes(b"alert(1)")
    with pytest.raises(va.IntegrityError, match="sha256"):
        va.rtl_text_plugin_entry(vendor=vendor)


def test_viewer_fits_its_slot_with_headroom():
    html = (zim_writer.VIEWER_DIR / "index.html").read_bytes()
    assert len(pad_to_slot("index.html", html)) == SLOT_SIZES["index.html"]
    # Room for at least as much again: published ZIMs cannot grow the slot
    # without a full re-pack (docs/viewer-slots.md).
    assert len(html) * 1.5 < SLOT_SIZES["index.html"]
