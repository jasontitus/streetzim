"""Satellite work queues are bounded, and failed sources stay retryable."""
import io
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PIL import Image
import pytest

from streetzim import satellite


def test_bounded_results_consume_only_a_window_and_propagate_failures():
    consumed = []

    def coordinates():
        for n in range(1000):
            consumed.append(n)
            yield (n,)

    class ImmediatePool:
        def submit(self, fn, *args):
            future = Future()
            try:
                future.set_result(fn(*args))
            except BaseException as e:
                future.set_exception(e)
            return future

    gen = satellite._bounded_tile_results(ImmediatePool(), lambda n: n, coordinates(), 8)
    first = next(gen)
    assert first in range(8)
    assert consumed == list(range(8))
    assert sorted([first, *gen]) == list(range(1000))
    assert consumed == list(range(1000))

    def fail(n):
        raise RuntimeError('encode failed')

    consumed.clear()
    with pytest.raises(RuntimeError, match='encode failed'):
        list(satellite._bounded_tile_results(ImmediatePool(), fail, coordinates(), 8))
    assert consumed == list(range(8))  # never queues the rest after a failure


def test_bounded_results_cancel_queued_work_when_closed():
    class PausedPool:
        def __init__(self):
            self.futures = []

        def submit(self, fn, *args):
            future = Future()
            if not self.futures:
                future.set_result(fn(*args))
            self.futures.append(future)
            return future

    pool = PausedPool()
    gen = satellite._bounded_tile_results(pool, lambda n: n, ((n,) for n in range(100)), 4)
    assert next(gen) == 0
    gen.close()
    assert len(pool.futures) == 4
    assert all(f.cancelled() for f in pool.futures[1:])


def test_bounded_results_visit_every_coordinate_once():
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = list(satellite._bounded_tile_results(pool, lambda z, x, y: (z, x, y),
                   ((3, x, y) for x in range(10) for y in range(20)), 8))
    assert sorted(got) == [(3, x, y) for x in range(10) for y in range(20)]


@pytest.mark.parametrize('tile_size', [256, 512])
@pytest.mark.parametrize('max_zoom', [0, 4])  # sequential and parallel paths
@pytest.mark.parametrize('missing', [False, True])
def test_cached_or_failed_sources_are_reported_correctly(
        tmp_path, monkeypatch, capsys, tile_size, max_zoom, missing):
    sources = tmp_path / 'sources'
    out = tmp_path / 'tiles'
    monkeypatch.setattr(satellite, 'CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(satellite, 'satellite_cache_dirs', lambda source: (str(sources), str(out)))
    monkeypatch.setattr(satellite.os, 'cpu_count', lambda: 1)
    requested = []
    im = Image.new('RGB', (256, 256), (50, 100, 150))
    encoded = io.BytesIO()
    im.save(encoded, 'JPEG')

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return encoded.getvalue()

    def fetch(req, timeout):
        requested.append(req.full_url)
        if missing:
            raise OSError('source unreachable')
        return Response()

    monkeypatch.setattr(satellite.urllib.request, 'urlopen', fetch)
    import time
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)
    bbox = '-60,-30,60,30'
    count = satellite.download_satellite_tiles(bbox, str(out), max_zoom=max_zoom,
                                             sat_format='webp', tile_size=tile_size)
    log = capsys.readouterr().out
    if missing:
        assert count == 0
        assert not list(out.rglob('*.webp'))
        assert 'holes in imagery' in log
        assert '(0 cached)' in log
    else:
        files = list(out.rglob('*.webp'))
        assert count == len(files) > 0
        assert 'holes in imagery' not in log
        for path in files:
            with Image.open(path) as tile:
                assert tile.size == (tile_size, tile_size)
                tile.load()
        requested.clear()
        assert satellite.download_satellite_tiles(
            bbox, str(out), max_zoom=max_zoom, sat_format='webp', tile_size=tile_size) == count
        assert not requested
    assert not list(Path(tmp_path).rglob('*.tmp'))


def test_missing_avif_codec_fails_before_download_or_cache_creation(tmp_path, monkeypatch):
    import sys
    from PIL import features
    monkeypatch.setattr(features, 'check', lambda name: False)
    monkeypatch.setitem(sys.modules, 'pillow_avif', None)
    monkeypatch.setattr(satellite, 'CACHE_DIR', str(tmp_path / 'cache'))

    def forbidden(*args, **kwargs):
        raise AssertionError('network request before codec validation')

    monkeypatch.setattr(satellite.urllib.request, 'urlopen', forbidden)
    with pytest.raises(RuntimeError, match='--satellite-format webp'):
        satellite.download_satellite_tiles('-1,-1,1,1', str(tmp_path / 'avif'),
                                          max_zoom=0, sat_format='avif')
    assert not (tmp_path / 'avif').exists()
    assert not (tmp_path / 'cache').exists()


def test_truncated_cached_jpeg_is_refetched_before_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr(satellite, 'CACHE_DIR', str(tmp_path))
    sources, dest = satellite.satellite_cache_dirs(sat_format='webp', tile_size=256)
    cached = Path(sources) / '0/0/0.jpg'
    cached.parent.mkdir(parents=True)
    buffer = io.BytesIO()
    Image.new('RGB', (256, 256), 'red').save(buffer, 'JPEG')
    bad = buffer.getvalue()[:-200]
    # This is the failure mode: the JPEG header opens, but pixels are cut short.
    with Image.open(io.BytesIO(bad)) as image:
        assert image.size == (256, 256)
        with pytest.raises(OSError):
            image.load()
    cached.write_bytes(bad)
    buffer = io.BytesIO()
    Image.new('RGB', (256, 256), 'blue').save(buffer, 'JPEG')
    good = buffer.getvalue()
    fetched = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fetch(req, timeout):
        fetched.append(req.full_url)
        assert not cached.exists()  # damaged source was evicted before retry
        return Response(good)

    monkeypatch.setattr(satellite.urllib.request, 'urlopen', fetch)
    assert satellite.download_satellite_tiles('-1,-1,1,1', dest, max_zoom=0,
                                             sat_format='webp') == 1
    assert len(fetched) == 1 and cached.read_bytes() == good
    with Image.open(Path(dest) / '0/0/0.webp') as image:
        pixel = image.convert('RGB').getpixel((128, 128))
    assert pixel[2] > 200 and pixel[0] < 60
    assert not list(tmp_path.rglob('*.tmp'))
