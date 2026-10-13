"""The world z13 map leaves paths out: folding z14 paths into every z13
tile of the planet (streetzim/path_fold.py, on by default with --max-zoom
13) would add hours and a fifth to its tiles."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_world_z13_build_leaves_paths_out():
    script = (ROOT / "ops" / "build-world-z13.sh").read_text()
    assert "--max-zoom 13 --no-fold-paths" in script
