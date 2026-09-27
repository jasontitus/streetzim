"""Regenerating torrents must not silently download the whole catalogue.

cloud/build_torrent.py hashes pieces from a local file when given one, and
otherwise streams the ZIM back over HTTP. cloud/generate_all_torrents.py called
it with no local path at all, so "regenerate with cloud/generate_all_torrents.py"
-- the hint web/generate.py prints whenever a torrent drifts behind its region's
live ZIM -- meant re-downloading every region: roughly 700 GB to repair a
handful of small files. On 2026-09-27 seven torrents were stale and every one of
their ZIMs was already on disk.

These tests pin the cheap path and the refusal.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def gat():
    sys.path.insert(0, str(ROOT / "cloud"))
    spec = importlib.util.spec_from_file_location(
        "generate_all_torrents", ROOT / "cloud" / "generate_all_torrents.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["generate_all_torrents"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def spy(gat, monkeypatch):
    """Capture build_torrent's arguments instead of hashing anything."""
    calls = []
    monkeypatch.setattr(gat, "build_torrent",
                        lambda url, out, **kw: calls.append((url, out, kw)))
    monkeypatch.setattr(gat, "latest_zim_for_item",
                        lambda ident: "osm-testland-2026-09-27.zim")
    return calls


def test_a_local_zim_is_hashed_from_disk(gat, spy, tmp_path):
    (tmp_path / "osm-testland-2026-09-27.zim").write_bytes(b"not a real zim")
    ok = gat.torrent_for_item("streetzim-testland", str(tmp_path),
                              local_dir=str(tmp_path))
    assert ok
    assert len(spy) == 1
    assert spy[0][2]["local_path"] == str(tmp_path / "osm-testland-2026-09-27.zim"), (
        "build_torrent was not pointed at the local file, so it would stream "
        "the ZIM back from archive.org")


def test_a_missing_zim_is_skipped_not_downloaded(gat, spy, tmp_path):
    ok = gat.torrent_for_item("streetzim-testland", str(tmp_path),
                              local_dir=str(tmp_path))
    assert ok is False
    assert not spy, "build_torrent was called with no local file and no opt-in"


def test_downloading_is_possible_but_must_be_asked_for(gat, spy, tmp_path):
    ok = gat.torrent_for_item("streetzim-testland", str(tmp_path),
                              allow_download=True, local_dir=str(tmp_path))
    assert ok
    assert spy[0][2]["local_path"] is None, "should stream when asked to"


def test_the_webseed_url_still_names_the_live_zim(gat, spy, tmp_path):
    (tmp_path / "osm-testland-2026-09-27.zim").write_bytes(b"x")
    gat.torrent_for_item("streetzim-testland", str(tmp_path), local_dir=str(tmp_path))
    url, out, _ = spy[0]
    assert url == ("https://archive.org/download/streetzim-testland/"
                   "osm-testland-2026-09-27.zim")
    assert out.endswith("testland.torrent")
