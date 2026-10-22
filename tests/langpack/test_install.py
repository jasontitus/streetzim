"""ops/langpack/install.sh in a sandbox: the dry run changes nothing and says
what it would do; --apply installs zim-tools from a tarball whose SHA-256 it
checks, runs npm only in ops/langpack, seeds the wpcache, and a second run
finds everything there."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from tests.langpack.conftest import ROOT


def _sandbox(tmp: Path) -> tuple[Path, dict]:
    root = tmp / "repo"
    (root / "ops" / "langpack").mkdir(parents=True)
    for f in ("install.sh", "host-env.sh", "package.json", "package-lock.json"):
        shutil.copy(ROOT / "ops" / "langpack" / f, root / "ops" / "langpack" / f)
    (root / "node_modules" / "puppeteer").mkdir(parents=True)
    # a zim-tools tarball whose zimcheck says it is 3.8.0
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        body = b"#!/bin/sh\necho 'zim-tools 3.8.0'\n"
        ti = tarfile.TarInfo("zim-tools_linux-x86_64-3.8.0/zimcheck")
        ti.size, ti.mode = len(body), 0o755
        t.addfile(ti, io.BytesIO(body))
    tb = tmp / "zim-tools.tar.gz"
    tb.write_bytes(buf.getvalue())
    npm = tmp / "npm"
    npm.write_text("#!/bin/sh\npwd > \"$NPM_LOG\"\necho \"$@\" >> \"$NPM_LOG\"\nmkdir -p node_modules/playwright-core && "
                   "echo '{\"version\": \"1.59.1\"}' > node_modules/playwright-core/package.json && "
                   "touch node_modules/playwright-core/index.mjs\n")
    npm.chmod(0o755)
    seed = tmp / "seed"
    (seed / "dumps").mkdir(parents=True)
    (seed / "dumps" / "x.sql.gz").write_bytes(b"x")
    (tmp / "wiki").mkdir()
    # du on PATH records a call: nothing may walk the shared HDD (review LOW)
    (tmp / "bin").mkdir()
    (tmp / "bin" / "du").write_text(f"#!/bin/sh\necho \"$@\" >> {tmp / 'du.log'}\n")
    (tmp / "bin" / "du").chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "TOOLS": str(tmp / "tools"), "ZIM_TOOLS_TARBALL": str(tb),
           "ZIM_TOOLS_SHA256": hashlib.sha256(tb.read_bytes()).hexdigest(), "ZIM_TOOLS_URL": "http://127.0.0.1:9/none",
           "SEED_WPCACHE": str(seed), "LANGPACK_WORK": str(tmp / "work"), "NPM": str(npm), "NPM_LOG": str(tmp / "npm.log"),
           "TMPDIR": str(tmp), "PYTHON": sys.executable, "WIKI_SRC": str(tmp / "wiki"),
           "KIWIX_SERVE": "/bin/true", "NODE": "/bin/true", "CHROME_PATH": "/bin/true",
           "PACK_GATE_ZIMCHECK": str(tmp / "tools" / "zim-tools_linux-x86_64-3.8.0" / "zimcheck")}
    env.pop("PLAYWRIGHT_CORE", None)
    return root, env


def _run(root: Path, env: dict, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(root / "ops" / "langpack" / "install.sh"), *args], capture_output=True,
                          text=True, timeout=120, env=env)


def test_dry_run_then_apply_then_nothing_to_do(tmp_path: Path) -> None:
    root, env = _sandbox(tmp_path)
    r = _run(root, env)
    assert r.returncode == 3 and "would unpack" in r.stdout and "would run:" in r.stdout, r.stdout + r.stderr
    assert not (tmp_path / "tools").exists() and not (tmp_path / "work").exists()
    r = _run(root, env, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "tools" / "zim-tools_linux-x86_64-3.8.0" / "zimcheck").exists()
    cwd, args = (tmp_path / "npm.log").read_text().splitlines()
    assert cwd == str(root / "ops" / "langpack")                       # npm in ops/langpack only
    assert f"--prefix {root / 'ops' / 'langpack'}" in args, args          # and told so (review LOW)
    # the temporary folder inside $TOOLS, not beside it in the shared root (review LOW)
    assert f"{tmp_path / 'tools'}/.zimtools." in r.stdout, r.stdout
    assert (tmp_path / "work" / "wpcache" / "dumps" / "x.sql.gz").read_bytes() == b"x"
    r = _run(root, env)
    assert r.returncode == 0 and r.stdout.count("present:") == 3, r.stdout + r.stderr
    assert not (tmp_path / "du.log").exists()


def test_a_broken_zim_tools_folder_is_replaced_not_nested_into(tmp_path: Path) -> None:
    """Review LOW: a zim-tools folder whose zimcheck is not 3.8.0 is moved
    aside; `mv` into it would nest the new one inside and keep the broken one."""
    root, env = _sandbox(tmp_path)
    zt = tmp_path / "tools" / "zim-tools_linux-x86_64-3.8.0"
    zt.mkdir(parents=True)
    (zt / "zimcheck").write_text("#!/bin/sh\necho 'zim-tools 3.7.0'\n")
    (zt / "zimcheck").chmod(0o755)
    r = _run(root, env, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    out = subprocess.run([str(zt / "zimcheck"), "--version"], capture_output=True, text=True).stdout
    assert out.startswith("zim-tools 3.8.0") and not (zt / "zim-tools_linux-x86_64-3.8.0").exists(), out
    aside = [p.name for p in (tmp_path / "tools").iterdir() if p.name.startswith(zt.name + ".broken-")]
    assert len(aside) == 1, list((tmp_path / "tools").iterdir())


def test_a_tarball_with_another_checksum_is_refused(tmp_path: Path) -> None:
    root, env = _sandbox(tmp_path)
    env["ZIM_TOOLS_SHA256"] = "0" * 64           # the local copy does not match: download (fails here)
    r = _run(root, env, "--apply")
    assert r.returncode != 0 and not (tmp_path / "tools" / "zim-tools_linux-x86_64-3.8.0").exists()


def test_npm_never_runs_without_ops_langpacks_own_package_json(tmp_path: Path) -> None:
    root, env = _sandbox(tmp_path)
    (root / "ops" / "langpack" / "package-lock.json").unlink()
    r = _run(root, env, "--apply")
    assert r.returncode == 1 and "refusing to run npm" in r.stderr
    assert not (tmp_path / "npm.log").exists()
