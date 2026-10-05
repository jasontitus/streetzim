"""resources/viewer/index.html must be exactly its parts joined
(tools/build_viewer.py), without their comments (tools/viewer_compact.py,
tests/test_viewer_compact.py). The parts are what people edit; index.html
is what ZIMs, the in-place patcher and the PWA ship."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_index_html_is_built_from_parts():
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "build_viewer.py"), "--check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_every_part_is_used_and_nonempty():
    sys.path.insert(0, str(ROOT / "tools"))
    import build_viewer
    parts = build_viewer.parts()
    assert len(parts) >= 20
    assert all(p.stat().st_size > 0 for p in parts)
    assert parts[0].name.startswith("000-") and parts[-1].name.startswith("900-")
