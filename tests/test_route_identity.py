"""Route-identity pytest integration.

Runs the golden corpora (under tests/golden/*.jsonl) against the current
source ZIMs (as declared in each corpus's _meta.source) and asserts every
fingerprint matches.

Skipped when:
  - No golden corpora exist yet (run tests/run_identity_suite.sh --generate)
  - The source ZIM referenced in the corpus metadata isn't present locally

To use these tests to verify a v5 rebuild:
  1. Re-generate golden corpora against current v4 ZIMs (once, slow).
  2. Rebuild the ZIMs with v5.
  3. Run pytest tests/test_route_identity.py — any diverging route fails.

The in-test candidate corpus is cached under tests/golden/.pytest-candidate/
so re-running the test against the same ZIM is fast after the first run.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = ROOT / "tests" / "golden"
CAND_DIR = GOLDEN_DIR / ".pytest-candidate"


def _discover_corpora() -> list[Path]:
    if not GOLDEN_DIR.is_dir():
        return []
    return sorted(
        p for p in GOLDEN_DIR.glob("*.jsonl")
        if not p.name.startswith(".") and p.stat().st_size > 0
    )


def _read_meta(p: Path) -> dict | None:
    with p.open() as fh:
        line = fh.readline().strip()
        if not line:
            return None
        rec = json.loads(line)
        return rec if rec.get("_meta") else None


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


CORPORA = _discover_corpora()


@pytest.mark.skipif(not CORPORA, reason="no golden corpora in tests/golden/")
@pytest.mark.parametrize("corpus", CORPORA, ids=[p.stem for p in CORPORA])
def test_route_identity(corpus: Path):
    meta = _read_meta(corpus)
    if meta is None:
        pytest.skip(f"{corpus.name} has no _meta header — regenerate with current tools")

    source_zim = meta.get("source")
    if not source_zim or not (ROOT / source_zim).is_file():
        pytest.skip(f"source ZIM not present: {source_zim}")

    zim_path = ROOT / source_zim
    # If the candidate ZIM hashes to the same bytes the golden was made from,
    # re-running A* is pointless — the corpora would trivially match.
    golden_hash = meta.get("zim_sha256")
    current_hash = _sha256_file(zim_path)
    if golden_hash and golden_hash == current_hash:
        pytest.skip(f"{zim_path.name} unchanged since golden was generated "
                    "(same sha256 — the diff would be trivial)")

    CAND_DIR.mkdir(parents=True, exist_ok=True)
    cand = CAND_DIR / corpus.name

    cmd = [
        sys.executable, "-m", "tests.generate_golden_corpus",
        "--zim", str(zim_path),
        "--out", str(cand),
        "--pairs", str(meta.get("pairs", 2000)),
        "--seed", str(meta.get("seed", 42)),
        "--min-dist-m", str(meta.get("min_dist_m", 500)),
        "--workers", str(min(4, os.cpu_count() or 2)),
        "--progress-every", "0",
    ]
    if meta.get("max_dist_m") is not None:
        cmd += ["--max-dist-m", str(meta["max_dist_m"])]
    if meta.get("max_pops") is not None:
        cmd += ["--max-pops", str(meta["max_pops"])]

    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, (
        f"corpus regen failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )

    diff = subprocess.run(
        [sys.executable, "-m", "tests.diff_corpora", str(corpus), str(cand)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert diff.returncode == 0, (
        f"{corpus.name} diverges:\n"
        f"stdout: {diff.stdout}\nstderr: {diff.stderr}"
    )
