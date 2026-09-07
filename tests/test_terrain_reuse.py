"""A cached terrain tile is only reused if it could hold real elevation.

A DEM fetch that failed at generation time leaves a 44-byte blank webp.
The build used to reuse any existing file, so that blank was baked into
every later ZIM for the same tile — the 2026-07 Carolinas blank-terrain
ship, and alaska/argentina/mexico failing the terrain gate in the 2026-09
round on stale entries from May.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import create_osm_zim as coz  # noqa: E402


def test_threshold_default_rejects_a_blank_tile():
    assert coz._TERRAIN_MIN_REUSE_BYTES >= 200, (
        "a 44-byte blank must fall below the reuse threshold")


def _reusable(path):
    try:
        return os.path.getsize(path) >= coz._TERRAIN_MIN_REUSE_BYTES
    except OSError:
        return False


def test_blank_missing_and_real_tiles(tmp_path):
    blank = tmp_path / "blank.webp"
    blank.write_bytes(b"\0" * 44)                 # the failed-DEM signature
    real = tmp_path / "real.webp"
    real.write_bytes(b"\0" * 4096)                # a tile with elevation in it
    assert not _reusable(str(blank)), "blank tile must be regenerated"
    assert not _reusable(str(tmp_path / "absent.webp")), "missing tile must be generated"
    assert _reusable(str(real)), "a real tile must still be reused"
