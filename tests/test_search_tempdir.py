"""create_zim's search chunk folder (GBs for a country) is removed after the
build, whether it succeeds or fails; a failed Netherlands build once left
2.5 GB of streetzim_chunks_* behind in the temp dir."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _build(tmp_path, work):
    pytest.importorskip("libzim.writer")
    mvt = pytest.importorskip("mapbox_vector_tile")
    from streetzim.zim_writer import create_zim
    tile = gzip.compress(mvt.encode([{"name": "poi", "features": [
        {"geometry": "POINT(10 10)", "properties": {"name": "X"}}]}]))
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    feats = tmp_path / "features.jsonl"
    feats.write_text("".join(json.dumps(
        {"name": f"Place {i}", "type": "poi", "subtype": "cafe",
         "lat": 43.73 + i * 1e-4, "lon": 7.42}) + "\n" for i in range(50)))
    create_zim(
        tmp_path / "t.zim", tiles={(14, 8529, 5974): tile}, tile_metadata={},
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "Monaco"}, name="OSM - Monaco",
        bbox=(7.40, 43.72, 7.44, 43.76), search_features_path=str(feats),
        xapian_mode="none", xapian_workdir=str(work))


@pytest.fixture
def systmp(tmp_path, monkeypatch):
    """The system temp dir, where the chunk folder used to be created."""
    import tempfile
    d = tmp_path / "systmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


def _leftovers(*dirs):
    return [p.name for d in dirs for p in d.iterdir() if "streetzim_chunks_" in p.name]


def test_chunk_folder_removed_after_success(tmp_path, systmp):
    work = tmp_path / "work"
    work.mkdir()
    _build(tmp_path, work)
    assert (tmp_path / "t.zim").exists()
    assert _leftovers(work, systmp) == []


def test_chunk_folder_removed_after_failure(tmp_path, systmp, monkeypatch):
    import streetzim.zim_writer as zw

    def disk_full(*a, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(zw, "_search_emit_chunks", disk_full)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(OSError):
        _build(tmp_path, work)
    assert _leftovers(work, systmp) == []
