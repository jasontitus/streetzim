"""tools/check_boundary.py, rule by rule, on a scratch git repository: every
way core could reach into ops/ is flagged, and what is allowed (docstrings,
comments, in-place ops files, ruff.toml's own list) is not."""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check_boundary", ROOT / "tools" / "check_boundary.py")
assert spec and spec.loader
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _link(root: Path, rel: str) -> None:
    os.symlink(os.path.relpath(os.path.join("ops", rel), os.path.dirname(rel) or "."),
               root / rel)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    _write(r, "ops/cloud/opsmod.py", "X = 1\n")
    _write(r, "ops/cloud/opsjob.sh", "#!/bin/bash\necho ops\n")
    _write(r, "ops/in-place.txt", "# ops left in place\nweb/\ncloud/queue.list\n")
    _write(r, "ruff.toml", 'extend-exclude = [\n  "cloud/opsmod.py",\n]\n')
    _write(r, "cloud/coremod.py", '"""Core. Mentions cloud/opsjob.sh in a docstring."""\n'
                                  "# and cloud.opsmod in a comment\nY = 2\n")
    _write(r, "web/app.js", "fetch('cloud/opsjob.sh')\n")          # in place: ops
    _write(r, "cloud/queue.list", "cloud/opsjob.sh\n")             # in place: ops
    (r / "cloud").mkdir(exist_ok=True)
    _link(r, "cloud/opsmod.py")
    _link(r, "cloud/opsjob.sh")
    return r


def _check(r: Path) -> list[str]:
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    errors, _ = cb.check(r)
    return errors


def test_clean_repo_passes(repo):
    assert _check(repo) == []


@pytest.mark.parametrize("rel,text,expect", [
    ("cloud/a.py", "import cloud.opsmod\n", "imports ops module cloud.opsmod"),
    ("cloud/b.py", "from cloud import opsmod\n", "imports ops module cloud.opsmod"),
    ("cloud/c.py", "import opsmod\n", "imports ops module opsmod"),
    ("cloud/d.py", "from . import opsmod\n", "imports ops module opsmod"),
    ("cloud/e.py", "import importlib\nm = importlib.import_module('cloud.opsmod')\n",
     "names ops 'cloud.opsmod'"),
    ("cloud/f.py", "import os\np = os.path.join('cloud', 'opsjob.sh')\n",
     "names ops 'opsjob.sh'"),
    ("cloud/g.py", "p = 'ops/cloud/opsjob.sh'\n", "names ops"),
    ("cloud/h.sh", '#!/bin/bash\nD=$(dirname "$0")\nbash "$D"/opsjob.sh\n',
     "names ops 'opsjob.sh'"),
    ("Dockerfile", "FROM x\nRUN bash cloud/opsjob.sh\n", "names ops 'cloud/opsjob.sh'"),
    ("pyproject.toml", '[x]\nscript = "cloud/opsjob.sh"\n', "names ops 'cloud/opsjob.sh'"),
    ("config.json", '{"run": "cloud/opsjob.sh"}\n', "names ops 'cloud/opsjob.sh'"),
])
def test_each_rule_flags(repo, rel, text, expect):
    _write(repo, rel, text)
    errors = _check(repo)
    assert any(f"{rel}:" in e and expect in e for e in errors), errors


def test_allowed_mentions_are_not_flagged(repo):
    _write(repo, "cloud/i.sh", "#!/bin/bash\n# calls cloud/opsjob.sh? no, a comment\n")
    _write(repo, "cloud/j.js", "// cloud/opsjob.sh in a comment\n")
    _write(repo, "docs/x.md", "Run `cloud/opsjob.sh` on the host.\n")
    _write(repo, "cloud/opsjob_notes.py", "S = 'myopsjob.shx'\n")   # not the name
    assert _check(repo) == []


def test_symlink_target_and_ruff_list(repo):
    (repo / "cloud" / "opsjob.sh").unlink()
    os.symlink("elsewhere.sh", repo / "cloud" / "opsjob.sh")
    _write(repo, "ruff.toml", "extend-exclude = []\n")
    errors = _check(repo)
    assert any("cloud/opsjob.sh: symlink to elsewhere.sh" in e for e in errors)
    assert any("ruff.toml extend-exclude differs" in e for e in errors)
