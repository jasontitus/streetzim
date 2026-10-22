"""The pipeline's shell and browser scripts parse, and name no build-host
paths (configuration comes from arguments and environment variables)."""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from tests.langpack.conftest import ROOT, TOOLS

SCRIPTS = sorted(TOOLS.glob("*.sh")) + sorted((ROOT / "ops" / "langpack").glob("*.sh"))
MJS = sorted(TOOLS.glob("*.mjs"))


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_parse(path) -> None:  # noqa: ANN001
    r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("path", MJS, ids=lambda p: p.name)
def test_browser_gates_parse(path) -> None:  # noqa: ANN001
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_host_paths() -> None:
    bad = []
    for p in sorted(TOOLS.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".sh", ".mjs", ".json"):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"/storage/|/home/\w", line):
                    bad.append(f"{p.relative_to(ROOT)}:{i}")
    assert not bad, bad


def test_siblings_are_imported_through_the_package() -> None:
    """`from merge_zim import ...` works only when run from tools/langpack and
    makes a second copy of the module (two MergeError classes)."""
    stems = "|".join(p.stem for p in TOOLS.glob("*.py"))
    bare = re.compile(rf"^\s*(from ({stems}) import|import ({stems})\b)")
    bad = [f"{p.name}:{i}" for p in sorted(TOOLS.glob("*.py"))
           for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if bare.match(line)]
    assert not bad, bad
