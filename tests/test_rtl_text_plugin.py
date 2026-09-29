"""The builder writes MapLibre's RTL text plugin into the ZIM and names it in
map-config.json (the viewer's 137-rtl-text.js loads it only when a tile has
RTL labels). Also: the viewer, now carrying the dark theme and the inline
POI icons, still fits its in-place patch slot with room to grow."""
import hashlib
import json

import pytest

from cloud.viewer_slots import SLOT_SIZES, pad_to_slot
from streetzim import zim_writer


class FakeItem:
    def __init__(self, path, title, mimetype, content, is_front=False, compress=True, namespace=None):
        self.path, self.mimetype, self.content = path, mimetype, content


class FakeCreator:
    def __init__(self):
        self.items = {}

    def add_item(self, item):
        self.items[item.path] = item


def test_vendored_plugin_matches_the_pinned_hash_and_carries_its_licence():
    data = zim_writer.RTL_TEXT_PLUGIN_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == zim_writer.RTL_TEXT_PLUGIN_SHA256
    licence = (zim_writer.RTL_TEXT_PLUGIN_PATH.parent / "LICENSE.md").read_text()
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
    assert body.startswith(b"/*! @mapbox/mapbox-gl-rtl-text 0.3.0")
    assert b"Redistribution and use" in body.split(b"*/", 1)[0]
    assert body.endswith(zim_writer.RTL_TEXT_PLUGIN_PATH.read_bytes())


def test_without_the_plugin_neither_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(zim_writer, "RTL_TEXT_PLUGIN_PATH", tmp_path / "absent.js")
    items, cfg = _build(tmp_path)
    assert zim_writer.RTL_TEXT_PLUGIN_ENTRY not in items
    assert "rtlTextPlugin" not in cfg


def test_a_tampered_copy_fails_the_build(tmp_path, monkeypatch):
    bad = tmp_path / "vendor" / "mapbox-gl-rtl-text.js"
    bad.parent.mkdir()
    bad.write_bytes(b"alert(1)")
    (bad.parent / "LICENSE.md").write_text("x")
    monkeypatch.setattr(zim_writer, "RTL_TEXT_PLUGIN_PATH", bad)
    with pytest.raises(RuntimeError, match="sha256"):
        zim_writer._rtl_text_plugin_bytes()


def test_viewer_fits_its_slot_with_headroom():
    html = (zim_writer.VIEWER_DIR / "index.html").read_bytes()
    assert len(pad_to_slot("index.html", html)) == SLOT_SIZES["index.html"]
    # Room for at least as much again: published ZIMs cannot grow the slot
    # without a full re-pack (docs/viewer-slots.md).
    assert len(html) * 1.5 < SLOT_SIZES["index.html"]
