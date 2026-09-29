"""The ops wrappers default OVERTURE_RELEASE to `latest` and resolve it once,
before any overture_cache/<theme>-<id>-<release>.parquet name is formed.
Runs each wrapper's real resolve block against a stub python."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# script, variable, block start, python the block calls
WRAPPERS = {
    "queue": ("ops/build-refresh-queue.sh", "OVERTURE_RELEASE",
              'if [ "$OVERTURE_RELEASE" = latest ]; then', "$PY"),
    "ship": ("ops/ship-region.sh", "OVERTURE_RELEASE",
             'if [ "$OVERTURE_RELEASE" = latest ]; then', "$PY"),
    "cloud": ("ops/cloud/build_region.sh", "release",
              'if [ "$release" = latest ]; then', "./venv312/bin/python3"),
}


def _block(script: str, start: str) -> str:
    text = (ROOT / script).read_text()
    i = text.index(start)
    return text[i:text.index("\nfi\n", i) + 4]


def _run(tmp_path: Path, which: str, value: str, py_ok: bool = True):
    script, var, start, _py = WRAPPERS[which]
    py = tmp_path / "venv312" / "bin" / "python3"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text('#!/bin/sh\necho "$*" >> "$STUB_LOG"\n' +
                  ('echo 2026-09-23.1\n' if py_ok else 'echo boom >&2; exit 1\n'))
    py.chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.unlink(missing_ok=True)
    body = (f'log() {{ echo "$*"; }}\nfail=0; id=r; PY="{py}"\n'
            f'{var}={value}\n{_block(script, start)}\n'
            f'echo "RESULT=${var} fail=$fail"\n')
    r = subprocess.run(["bash", "-c", body], cwd=tmp_path, capture_output=True, text=True,
                       env={**os.environ, "STUB_LOG": str(calls)})
    return r, (calls.read_text() if calls.exists() else "")


@pytest.mark.parametrize("which", sorted(WRAPPERS))
def test_default_is_latest(which):
    script, var, _, _ = WRAPPERS[which]
    text = (ROOT / script).read_text()
    assert '"${OVERTURE_RELEASE:-latest}"' in text
    # Resolved before the first cache file name that uses it.
    assert text.index(WRAPPERS[which][2]) < text.index(f"-${{{var}}}.parquet")


@pytest.mark.parametrize("which", sorted(WRAPPERS))
def test_latest_resolves_once_for_both_themes(tmp_path, which):
    r, calls = _run(tmp_path, which, "latest")
    assert r.returncode == 0, r.stderr
    assert "RESULT=2026-09-23.1 fail=0" in r.stdout
    assert calls.splitlines() == ["download_overture_data.py addresses places --print-release"]


@pytest.mark.parametrize("which", sorted(WRAPPERS))
def test_pinned_release_is_kept(tmp_path, which):
    r, calls = _run(tmp_path, which, "2026-08-19.0")
    assert "RESULT=2026-08-19.0 fail=0" in r.stdout and calls == ""


@pytest.mark.parametrize("which", sorted(WRAPPERS))
def test_unresolvable_latest_stops(tmp_path, which):
    r, _ = _run(tmp_path, which, "latest", py_ok=False)
    if which == "queue":          # preflight collects failures, then exits
        assert "fail=1" in r.stdout
    else:
        assert r.returncode != 0 and "RESULT" not in r.stdout
    assert "OVERTURE_RELEASE=<release>" in r.stdout


def test_build_region_fast_refuses_unresolved_latest(tmp_path):
    text = (ROOT / "ops/build-region-fast.sh").read_text()
    start = 'if [ "$OVERTURE_RELEASE" = latest ]; then'
    i = text.index(start)
    block = text[i:text.index("\nfi\n", i) + 4]
    r = subprocess.run(["bash", "-c", f"OVERTURE_RELEASE=latest\n{block}\necho built"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "built" not in r.stdout
