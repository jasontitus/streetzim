"""Parallel cache users only see complete DEMs and mosaics for their own plan."""
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from streetzim import terrain


def test_neighboring_boxes_and_different_layouts_have_distinct_vrt_keys(tmp_path):
    a = terrain.TerrainPlan((7.401, 43.721, 7.431, 43.751), 12)
    b = terrain.TerrainPlan((7.402, 43.722, 7.432, 43.752), 12)
    assert a.key != b.key and a.marker_name != b.marker_name
    world = tmp_path / "world.tif"
    world.touch()
    production = terrain.TerrainPlan(a.bbox, 12, low_zoom_world_vrt=str(world))
    assert a.vrt_key != production.vrt_key


@pytest.mark.parametrize("failure", [OSError(28, "disk full"), KeyboardInterrupt()])
def test_vrt_failure_keeps_previous_mosaic_and_cleans_staging(tmp_path, monkeypatch, failure):
    out = tmp_path / "m.vrt"
    out.write_text("complete previous mosaic")

    def fail(paths, staged, **kwargs):
        Path(staged).write_text("incomplete")
        assert out.read_text() == "complete previous mosaic"
        raise failure

    monkeypatch.setattr(terrain, "_write_vrt_staged", fail)
    with pytest.raises(type(failure)):
        terrain._write_vrt([], str(out))
    assert out.read_text() == "complete previous mosaic"
    assert list(tmp_path.iterdir()) == [out]


def test_concurrent_vrt_writers_use_private_staging(tmp_path, monkeypatch):
    out = tmp_path / "m.vrt"
    out.write_text("old")
    barrier = threading.Barrier(2)
    stages = []

    def build(paths, staged, **kwargs):
        stages.append(staged)
        Path(staged).write_text(paths[0])
        barrier.wait(timeout=5)
        assert out.read_text() == "old"
        barrier.wait(timeout=5)
        return staged

    monkeypatch.setattr(terrain, "_write_vrt_staged", build)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(terrain._write_vrt, [text], str(out)) for text in ("a", "b")]
        assert [f.result() for f in futures] == [str(out), str(out)]
    assert len(set(stages)) == 2
    assert out.read_text() in {"a", "b"}
    assert list(tmp_path.iterdir()) == [out]


def test_no_readable_dem_sources_does_not_publish_empty_vrt(tmp_path, monkeypatch):
    out = tmp_path / "m.vrt"
    out.write_text("previous")
    monkeypatch.setattr(terrain, "_write_vrt_staged", lambda *a, **kw: None)
    assert terrain._write_vrt([], str(out)) is None
    assert out.read_text() == "previous"
    assert list(tmp_path.iterdir()) == [out]


class Response(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {"Content-Length": str(length if length is not None else len(body))}


def test_short_dem_response_keeps_previous_file(tmp_path, monkeypatch):
    out = tmp_path / "cell.tif"
    out.write_bytes(b"previous")
    monkeypatch.setattr(terrain.urllib.request, "urlopen", lambda *a, **kw:
                        Response(b"II*\x00partial", 10000))
    monkeypatch.setattr(terrain.time, "sleep", lambda seconds: None)
    assert terrain._download_dem([("https://example.org/cell", "GLO-30")],
                                 str(out), terrain._DemStats()) == "failed"
    assert out.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [out]


def test_dem_downloaders_cannot_unlink_each_others_partial(tmp_path, monkeypatch):
    out = tmp_path / "cell.tif"
    body = b"II*\x00" + b"x" * 2000
    barrier = threading.Barrier(2)
    stages = []
    original = terrain.os.replace

    def publish(src, dest):
        stages.append(src)
        barrier.wait(timeout=5)
        assert Path(src).read_bytes() == body
        original(src, dest)

    monkeypatch.setattr(terrain.os, "replace", publish)
    monkeypatch.setattr(terrain.urllib.request, "urlopen", lambda *a, **kw: Response(body))
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(terrain._download_dem,
                               [("https://example.org/cell", "GLO-30")],
                               str(out), terrain._DemStats()) for _ in range(2)]
        assert [f.result() for f in futures] == ["ok", "ok"]
    assert len(set(stages)) == 2 and out.read_bytes() == body
    assert list(tmp_path.iterdir()) == [out]
