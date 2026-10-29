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


def test_the_built_viewer_names_itself():
    """SZ_VIEWER_BUILD (the About panel's fingerprint) is the first 8 hex
    digits of the built file's SHA-256, taken with it empty; the parts keep
    it empty."""
    import hashlib
    import re
    data = (ROOT / "resources" / "viewer" / "index.html").read_bytes()
    m = re.findall(rb"var SZ_VIEWER_BUILD = '([0-9a-f]{8})';", data)
    assert len(m) == 1, m
    blank = data.replace(b"var SZ_VIEWER_BUILD = '" + m[0] + b"';", b"var SZ_VIEWER_BUILD = '';")
    assert hashlib.sha256(blank).hexdigest()[:8] == m[0].decode()
    about = (ROOT / "resources" / "viewer" / "src" / "index" / "140-view-home-about.js").read_text(encoding="utf-8")
    assert about.count("var SZ_VIEWER_BUILD = '';") == 1
