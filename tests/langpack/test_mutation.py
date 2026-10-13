"""tools/langpack/mutation_test.py on the small ZIMs: every merger mutant is
caught (verify_merge.py, the merger's own checks, or zimcheck when the
official one is installed) and materialize() refuses every corrupted recipe
or input."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack.conftest import tool  # noqa: E402


def test_every_mutant_is_killed(lp: SimpleNamespace, tmp_path: Path, work: Path) -> None:
    r = tool("mutation_test.py", lp.base, "de", *lp.packs.values(), "--workdir", tmp_path, timeout=1800)
    lines = r.stdout.splitlines()
    assert not [ln for ln in lines if ln.startswith("SURVIVED")], r.stdout
    assert lines[-1] == "SUMMARY 18/18 mutants killed", r.stdout[-3000:] + r.stderr[-2000:]
    assert r.returncode == 0
