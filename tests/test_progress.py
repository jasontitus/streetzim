"""Zimfarm progress file (--stats-filename): driven by the builder's phase
headers, monotonic, never above the total, and atomically replaced."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import common  # noqa: E402
from streetzim.progress import StatsFile  # noqa: E402


def read(p):
    return json.loads(Path(p).read_text())


def test_phase_headers_drive_the_file(tmp_path, capsys):
    p = tmp_path / "sub" / "task_progress.json"
    s = StatsFile(p).attach()
    try:
        s.write(0, 5)
        assert read(p) == {"done": 0, "total": 5}
        common.print("[1/5] Acquiring OSM data...")
        assert read(p) == {"done": 0, "total": 5}
        common.print("[3/5] Processing tiles...")
        assert read(p) == {"done": 2, "total": 5}
        common.print("    Added 12 tiles")               # not a header
        assert read(p)["done"] == 2
        common.print("[2/5] out of order")                 # never goes back
        assert read(p)["done"] == 2
        s.finish()
        assert read(p) == {"done": 5, "total": 5}
    finally:
        s.detach()
    common.print("[4/5] after detach")
    assert read(p) == {"done": 5, "total": 5}
    assert s.on_phase not in common.PHASE_LISTENERS
    assert not list(p.parent.glob("*.tmp"))


def test_done_is_capped_at_total(tmp_path):
    s = StatsFile(tmp_path / "p.json")
    s.write(9, 4)
    assert read(tmp_path / "p.json") == {"done": 4, "total": 4}
