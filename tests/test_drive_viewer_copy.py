"""The website's copy of the viewer (web/drive/viewer, made by
scripts/sync-drive-viewer.sh) is byte-identical to resources/viewer.

Ops, not core: it tests web/, and moves with web/ in stage 2 of the ops
split (ops/README.md). The core JS tests read resources/viewer only.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["index.html", "places.html", "routing-worker.js"])
def test_drive_viewer_matches_resources(name):
    src = ROOT / "resources" / "viewer" / name
    copy = ROOT / "web" / "drive" / "viewer" / name
    assert copy.read_bytes() == src.read_bytes(), (
        f"web/drive/viewer/{name} differs from resources/viewer/{name}; "
        "run scripts/sync-drive-viewer.sh")
