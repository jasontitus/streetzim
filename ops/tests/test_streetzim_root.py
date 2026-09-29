"""Stage 1 of the ops split: ops Python files live in ops/ and usually run
through a symlink at their old path. Each finds the streetzim checkout with
the same _streetzim_root(); check it in every way the host could start them.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _find_repo() -> Path:
    d = Path(__file__).resolve().parent
    while not (d / "create_osm_zim.py").is_file():
        d = d.parent
    return d


REPO = _find_repo()
USERS = sorted(p.relative_to(REPO) for p in (REPO / "ops").rglob("*.py")
               if "def _streetzim_root" in p.read_text(encoding="utf-8")
               and p.name != Path(__file__).name)


def _root_fn(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "_streetzim_root")
    return ast.get_source_segment(path.read_text(encoding="utf-8"), fn)


def test_every_copy_is_identical():
    assert len(USERS) >= 15
    assert len({_root_fn(REPO / p) for p in USERS}) == 1


@pytest.mark.parametrize("rel", USERS, ids=str)
def test_root_from_old_and_new_paths(rel, monkeypatch, tmp_path):
    monkeypatch.delenv("STREETZIM_ROOT", raising=False)
    src = _root_fn(REPO / rel)
    old = REPO / Path(*rel.parts[1:])            # the symlink at the old path
    for file in {REPO / rel, old} if old.is_symlink() else {REPO / rel}:
        ns = {"os": os, "__file__": str(file)}
        exec(src, ns)
        monkeypatch.chdir(tmp_path)             # cwd must not matter
        assert ns["_streetzim_root"]() == str(REPO)
    ns = {"os": os, "__file__": str(REPO / rel)}
    exec(src, ns)
    monkeypatch.setenv("STREETZIM_ROOT", str(tmp_path))
    assert ns["_streetzim_root"]() == str(tmp_path)


def test_scripts_start_by_either_path(tmp_path):
    # A real script end to end: its --help parses only if its imports and
    # module-level paths resolve.
    for path in ("cloud/serve_zims.py", "ops/cloud/serve_zims.py"):
        r = subprocess.run([sys.executable, str(REPO / path), "--help"],
                           capture_output=True, text=True, cwd=tmp_path,
                           env={k: v for k, v in os.environ.items()
                                if k != "STREETZIM_ROOT"})
        assert r.returncode == 0, r.stderr
