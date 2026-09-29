"""Check the line between the builder ("core") and operations ("ops").

    python tools/check_boundary.py [--root DIR]

Stage 1 of the ops split (ops/README.md): operations files live in ops/,
with a symlink at each old path the build host runs or reads, and some
operations files are still in place (ops/in-place.txt). This fails when:
- a symlink does not point to ops/<its own path>, or dangles;
- core Python imports a module that exists only in ops/: `cloud.X`, a bare
  `import X` / `from X import`, or a relative import, where X is the stem
  of a moved .py file;
- core names an ops-only path, dotted module (`cloud.X`) or script file
  name (`upload_validated.sh`): in a Python string (docstrings aside), or on
  a non-comment line of shell, JavaScript, HTML, the Dockerfile, TOML,
  JSON or YAML. Core must not call ops;
- ruff.toml's extend-exclude differs from the tracked .py symlinks (they are
  linted once, in ops/). ruff.toml itself is exempt from the name rule,
  since it lists those paths on purpose.
Core is every tracked regular file outside ops/ that ops/in-place.txt does
not list. Docs (*.md), .github/ and this file may mention ops paths.
tests/test_check_boundary.py checks each rule on a scratch repository.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

LINE_SCANNED = (".sh", ".js", ".mjs", ".html", ".toml", ".json", ".yml", ".yaml")
HASH_COMMENTS = (".sh", ".toml", ".yml", ".yaml")
# ruff.toml lists the symlinks on purpose; the checker and its test name ops
# paths as data.
NOT_SCANNED = {"ruff.toml", "package-lock.json", "tools/check_boundary.py",
               "tests/test_check_boundary.py"}


def tracked(root: Path) -> list[tuple[str, str]]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-s", "-z"], check=True,
                         capture_output=True).stdout.decode("utf-8", "surrogateescape")
    files = []
    for rec in out.split("\0"):
        if rec:
            meta, path = rec.split("\t", 1)
            files.append((meta.split()[0], path))
    return files


def in_place(root: Path) -> list[str]:
    lines = (root / "ops" / "in-place.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def is_in_place(path: str, entries: list[str]) -> bool:
    return any(path == e or (e.endswith("/") and path.startswith(e)) for e in entries)


def ruff_excludes(root: Path) -> set[str]:
    text = (root / "ruff.toml").read_text(encoding="utf-8")
    try:
        import tomllib                          # Python 3.11+
        return set(tomllib.loads(text).get("extend-exclude", []))
    except ImportError:                          # 3.10: the list is plain strings
        block = re.search(r"^extend-exclude = \[(.*?)\]", text, re.S | re.M)
        return set(re.findall(r'"([^"]+)"', block.group(1) if block else ""))


def check(root: Path) -> tuple[list[str], str]:
    files = tracked(root)
    entries = in_place(root)
    errors: list[str] = []
    links = [p for mode, p in files if mode == "120000"]
    regular = [p for mode, p in files if mode != "120000"]
    tracked_paths = {p for _, p in files}

    # 1. every symlink points to ops/<same path>, which is tracked
    for p in links:
        target = os.readlink(root / p)
        want = os.path.relpath(os.path.join("ops", p), os.path.dirname(p) or ".")
        if target != want:
            errors.append(f"{p}: symlink to {target}, expected {want}")
        elif os.path.join("ops", p) not in tracked_paths:
            errors.append(f"{p}: points to untracked ops/{p}")

    core = [p for p in regular if not p.startswith("ops/") and not is_in_place(p, entries)]
    # Names that belong to ops only: never also a core file's stem or name.
    core_stems = {Path(p).stem for p in core if p.endswith(".py")}
    core_names = {Path(p).name for p in core}
    ops_py_stems = {Path(p).stem for p in links if p.endswith(".py")} - core_stems
    ops_modules = {f"cloud.{Path(p).stem}" for p in links
                   if p.startswith("cloud/") and p.endswith(".py")}
    ops_names = {Path(p).name for p in links
                 if p.endswith((".sh", ".py", ".mjs", ".js"))} - core_names
    alts = sorted(set(links) | ops_modules | ops_names, key=len, reverse=True)
    name_re = re.compile(r"(?<![\w.-])(?:" + "|".join(re.escape(a) for a in alts)
                         + r")(?![\w-])|(?<![\w.-])ops/")

    def check_imports(p: str, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                if node.level:                      # relative: from . import X
                    mods += [a.name for a in node.names]
            for m in mods:
                if m in ops_modules or m.split(".")[0] in ops_py_stems:
                    errors.append(f"{p}:{node.lineno}: imports ops module {m}")

    for p in core:
        if p.endswith(".md") or p.startswith(".github/") or p in NOT_SCANNED:
            continue
        full = root / p
        if p.endswith(".py"):
            tree = ast.parse(full.read_text(encoding="utf-8"), p)
            check_imports(p, tree)
            docstrings = set()
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and id(node) not in docstrings:
                    m = name_re.search(node.value)
                    if m:
                        errors.append(f"{p}:{node.lineno}: names ops {m.group(0)!r}")
        elif p.endswith(LINE_SCANNED) or Path(p).name == "Dockerfile":
            hash_comments = p.endswith(HASH_COMMENTS) or Path(p).name == "Dockerfile"
            for i, line in enumerate(full.read_text(encoding="utf-8",
                                                    errors="replace").splitlines(), 1):
                code = line.strip()
                if (hash_comments and code.startswith("#")) or \
                        code.startswith(("//", "*", "/*", "<!--")):
                    continue
                m = name_re.search(line)
                if m:
                    errors.append(f"{p}:{i}: names ops {m.group(0)!r}")

    # 4. ruff excludes exactly the .py symlinks
    excluded = ruff_excludes(root)
    py_links = {p for p in links if p.endswith(".py")}
    if excluded != py_links:
        errors.append(f"ruff.toml extend-exclude differs from the .py symlinks: "
                      f"missing {sorted(py_links - excluded)}, extra {sorted(excluded - py_links)}")

    summary = (f"{len(core)} core files, {len(links)} symlinks into ops/, "
               f"{len(entries)} in-place ops entries")
    return errors, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="repository to check (default: this checkout)")
    root = Path(ap.parse_args(argv).root)
    errors, summary = check(root)
    for e in errors:
        print(e)
    if errors:
        print(f"FAIL: {len(errors)} boundary problem(s)")
        return 1
    print(f"ok: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
