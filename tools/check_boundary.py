"""Check the line between the builder ("core") and operations ("ops").

    python tools/check_boundary.py

Stage 1 of the ops split (ops/README.md): operations files live in ops/,
with a symlink at each old path the build host runs or reads, and some
operations files are still in place (ops/in-place.txt). This fails when:
- a symlink does not point to ops/<its own path>, or dangles;
- core Python imports a module that exists only in ops/ (cloud.X with the
  real file in ops/cloud/X.py);
- core code names an ops-only path in a string (docstrings aside) or, for
  shell and JavaScript, on a non-comment line: core must not call ops;
- ruff.toml's extend-exclude differs from the tracked .py symlinks (they are
  linted once, in ops/).
Core is every tracked regular file outside ops/ that ops/in-place.txt does
not list. Docs (*.md) and the CI workflow may mention ops paths.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tracked() -> list[tuple[str, str]]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s"], check=True,
                         capture_output=True, text=True).stdout
    return [(line.split(None, 3)[0], line.split(None, 3)[3]) for line in out.splitlines()]


def in_place() -> list[str]:
    lines = (ROOT / "ops" / "in-place.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def is_in_place(path: str, entries: list[str]) -> bool:
    return any(path == e or (e.endswith("/") and path.startswith(e)) for e in entries)


def main() -> int:
    files = tracked()
    entries = in_place()
    errors: list[str] = []
    links = [p for mode, p in files if mode == "120000"]
    tracked_paths = {p for _, p in files}

    # 1. every symlink points to ops/<same path>, which is tracked
    for p in links:
        target = os.readlink(ROOT / p)
        want = os.path.relpath(os.path.join("ops", p), os.path.dirname(p) or ".")
        if target != want:
            errors.append(f"{p}: symlink to {target}, expected {want}")
        elif os.path.join("ops", p) not in tracked_paths:
            errors.append(f"{p}: points to untracked ops/{p}")

    ops_only_paths = set(links)
    ops_modules = {f"cloud.{Path(p).stem}" for p in links
                   if p.startswith("cloud/") and p.endswith(".py")}
    core = [p for mode, p in files if mode != "120000" and not p.startswith("ops/")
            and not is_in_place(p, entries)]
    path_re = re.compile("|".join(re.escape(p) for p in sorted(ops_only_paths, key=len,
                                                                   reverse=True))
                         + r"|(?<![\w.-])ops/")

    for p in core:
        if p.endswith(".md") or p.startswith(".github/") or p == "tools/check_boundary.py":
            continue
        full = ROOT / p
        if p.endswith(".py"):
            tree = ast.parse(full.read_text(encoding="utf-8"), p)
            docstrings = set()
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                else:
                    names = []
                for n in names:
                    if n in ops_modules:
                        errors.append(f"{p}:{node.lineno}: imports ops module {n}")
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and id(node) not in docstrings and path_re.search(node.value):
                    errors.append(f"{p}:{node.lineno}: names ops path "
                                  f"{path_re.search(node.value).group(0)!r}")
        elif p.endswith((".sh", ".js", ".mjs", ".html")):
            for i, line in enumerate(full.read_text(encoding="utf-8",
                                                    errors="replace").splitlines(), 1):
                code = line.strip()
                if code.startswith(("#", "//", "*", "/*")):
                    continue
                m = path_re.search(line)
                if m:
                    errors.append(f"{p}:{i}: names ops path {m.group(0)!r}")

    # 4. ruff excludes exactly the .py symlinks
    text = (ROOT / "ruff.toml").read_text(encoding="utf-8")
    try:
        import tomllib                          # Python 3.11+
        excluded = set(tomllib.loads(text).get("extend-exclude", []))
    except ImportError:                          # 3.10: the list is plain strings
        block = re.search(r"^extend-exclude = \[(.*?)\]", text, re.S | re.M)
        excluded = set(re.findall(r'"([^"]+)"', block.group(1) if block else ""))
    py_links = {p for p in links if p.endswith(".py")}
    if excluded != py_links:
        errors.append(f"ruff.toml extend-exclude differs from the .py symlinks: "
                      f"missing {sorted(py_links - excluded)}, extra {sorted(excluded - py_links)}")

    for e in errors:
        print(e)
    if errors:
        print(f"FAIL: {len(errors)} boundary problem(s)")
        return 1
    print(f"ok: {len(core)} core files, {len(links)} symlinks into ops/, "
          f"{len(entries)} in-place ops entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
