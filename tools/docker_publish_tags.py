#!/usr/bin/env python3
"""Read-only policy for publishing the CI-tested Docker image.

Metadata is literal data, never imported. Latest belongs to the highest stable
Git tag, including a tag whose CI has not passed yet. Keep release tags immutable.
Artifact selection supports rerunning only the other failed CI jobs.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any

STABLE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def read_version(path: Path) -> str:
    """Accept only a docstring and one literal assignment; execute nothing."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    body = tree.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    value = None
    if len(body) == 1:
        node = body[0]
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "__version__"):
            value = node.value
        elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == "__version__" and isinstance(node.annotation, ast.Name)
                and node.annotation.id == "str"):
            value = node.value
    if not isinstance(value, ast.Constant):
        raise ValueError("__version__ must have one literal string assignment")
    version = value.value
    if not isinstance(version, str) or not STABLE.fullmatch("v" + version):
        raise ValueError("__version__ must be a stable X.Y.Z string")
    if len(version) > 128:
        raise ValueError("the version exceeds Docker's 128-character tag limit")
    return version


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def select_tags(ref: str, sha: str, version_path: Path) -> list[str]:
    if not SHA.fullmatch(sha):
        raise ValueError("the tested commit must be a 40-character hexadecimal SHA")
    if ref != "main" and (len(ref) > 129 or not STABLE.fullmatch(ref)):
        return []  # Excluded refs never cause metadata reads or Git commands.
    commit = _git("rev-parse", "--verify", f"{sha}^{{commit}}")
    if _git("rev-parse", "HEAD") != commit:
        raise ValueError("the checked-out source is not the CI-tested commit")
    if ref == "main":
        _git("fetch", "-q", "origin", "main")
        current = _git("rev-parse", "refs/remotes/origin/main")
        return ["dev", "sha-" + commit[:12]] if current == commit else []
    tag = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{ref}^{{commit}}"],
                         capture_output=True, text=True, check=False)
    if tag.returncode or tag.stdout.strip() != commit:
        return []
    version = read_version(version_path)
    if ref != "v" + version:
        raise ValueError(f"tag {ref} does not match package version {version}")
    _git("fetch", "-q", "origin", "--tags")
    versions = [tuple(int(p) for p in m.groups())
                for t in _git("tag", "--list").splitlines()
                if len(t) <= 129 and (m := STABLE.fullmatch(t))]
    candidate = tuple(int(p) for p in version.split("."))
    return [version, "latest"] if candidate == max(versions, default=candidate) else [version]


def select_artifact(pages: list[dict[str, Any]], run_id: int, attempt: int, sha: str) -> int:
    """Select the newest validated Docker artifact for this exact upstream run."""
    if run_id < 1 or attempt < 1 or not SHA.fullmatch(sha):
        raise ValueError("invalid upstream run identity")
    pattern = re.compile(rf"streetzim-image-{run_id}-attempt-([1-9][0-9]*)\Z")
    eligible: dict[int, int] = {}
    for page in pages:
        for artifact in page["artifacts"]:
            name = artifact.get("name")
            match = pattern.fullmatch(name) if isinstance(name, str) else None
            if match is None or artifact.get("expired") is not False:
                continue
            produced_attempt = int(match[1])
            provenance = artifact.get("workflow_run", {})
            if (not isinstance(provenance, dict) or produced_attempt > attempt
                    or provenance.get("id") != run_id
                    or provenance.get("head_sha") != sha):
                continue
            artifact_id = artifact.get("id")
            if type(artifact_id) is not int or artifact_id < 1:
                raise ValueError("invalid artifact ID")
            if produced_attempt in eligible:
                raise ValueError("ambiguous Docker artifacts for one CI attempt")
            eligible[produced_attempt] = artifact_id
    if not eligible:
        raise ValueError("no unexpired CI-tested Docker image; rerun all CI jobs on a commit with image export support")
    return eligible[max(eligible)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--version-path", type=Path, default=Path("streetzim/__about__.py"))
    parser.add_argument("--artifact-list", type=Path)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--run-attempt", type=int)
    args = parser.parse_args()
    try:
        if args.artifact_list is not None:
            if args.run_id is None or args.run_attempt is None:
                parser.error("artifact selection needs --run-id and --run-attempt")
            artifact_id = select_artifact(json.loads(args.artifact_list.read_text()),
                                          args.run_id, args.run_attempt, args.sha)
            print(f"artifact_id={artifact_id}")
        else:
            if args.ref is None:
                parser.error("tag selection needs --ref")
            tags = select_tags(args.ref, args.sha, args.version_path)
            print(f"publish={'true' if tags else 'false'}")
            print("tags=" + " ".join(tags))
            if tags:
                print("commit_sha=" + _git("rev-parse", "HEAD"))
    except (ValueError, OSError, SyntaxError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Docker publication policy failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
