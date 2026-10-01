"""Build failures release background resources as well as deleting scratch files."""
import threading
from types import SimpleNamespace

import pytest

from streetzim import common, zim_writer


@pytest.mark.parametrize("platform, factor", [("linux", 1024), ("darwin", 1)])
def test_peak_rss_reports_bytes_on_both_platforms(monkeypatch, platform, factor):
    import resource
    import sys
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(resource, "getrusage", lambda who: SimpleNamespace(ru_maxrss=123))
    assert common.peak_rss_bytes() == 123 * factor


@pytest.mark.parametrize("failure", ["source", "gzip", "writer"])
def test_tile_failure_stops_watchdog_and_closes_source(tmp_path, monkeypatch, failure):
    before = [t.ident for t in threading.enumerate() if t.name == "streetzim-tile-watchdog"]
    closed = []

    def tiles(*a, **kw):
        try:
            yield (1, 0, 0, b"\x1f\x8bcorrupt" if failure == "gzip" else b"valid")
            yield (1, 0, 1, b"valid")
        finally:
            closed.append(True)

    def estimate(*a, **kw):
        if failure == "source":
            raise OSError("unreadable source")
        return 2

    class Creator:
        def add_item(self, item):
            raise OSError("disk full")

    monkeypatch.setattr(zim_writer, "estimate_tile_total", estimate)
    monkeypatch.setattr(zim_writer, "iter_tiles_from_mbtiles", tiles)
    with pytest.raises(SystemExit if failure == "gzip" else OSError):
        zim_writer._add_vector_tiles(Creator(), lambda *a, **kw: object(),
                                    output_path=tmp_path / "m.zim", tiles=None,
                                    mbtiles_path="tiles", tile_count=2, bbox=None,
                                    zim_builder="python", max_zoom=14)
    assert [t.ident for t in threading.enumerate() if t.name == "streetzim-tile-watchdog"] == before
    assert closed == ([] if failure == "source" else [True])
