"""Publication policy failures must not execute excluded code or regress aliases."""
import json
import subprocess
from pathlib import Path

import pytest

from tools import docker_publish_tags as policy


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", "--quiet", str(origin))
    git(tmp_path, "init", "--initial-branch=main", "--quiet", str(work))
    git(work, "config", "user.name", "Workflow tests")
    git(work, "config", "user.email", "tests@example.invalid")
    git(work, "remote", "add", "origin", str(origin))
    version = work / "streetzim" / "__about__.py"
    version.parent.mkdir()
    version.write_text('__version__ = "1.2.3"\n')
    git(work, "add", ".")
    git(work, "commit", "--quiet", "-m", "Initial release")
    git(work, "push", "--quiet", "--set-upstream", "origin", "main")
    monkeypatch.chdir(work)
    return work, version, git(work, "rev-parse", "HEAD")


def test_current_main_and_stale_rerun(repo):
    work, version, sha = repo
    assert policy.select_tags("main", sha, version) == ["dev", "sha-" + sha[:12]]
    version.write_text('__version__ = "2.0.0"\n')
    git(work, "commit", "--quiet", "-am", "New main")
    git(work, "push", "--quiet")
    git(work, "checkout", "--quiet", sha)
    assert policy.select_tags("main", sha, version) == []


def test_tag_named_origin_main_cannot_shadow_remote_branch(repo):
    work, version, sha = repo
    git(work, "tag", "origin/main", sha)
    version.write_text('__version__ = "2.0.0"\n')
    git(work, "commit", "--quiet", "-am", "New main")
    git(work, "push", "--quiet")
    git(work, "checkout", "--quiet", sha)
    assert git(work, "rev-parse", "refs/remotes/origin/main") != sha
    assert policy.select_tags("main", sha, version) == []


@pytest.mark.parametrize("annotated", [False, True])
def test_release_and_old_rerun_do_not_downgrade_latest(repo, annotated):
    work, version, sha = repo
    options = ["-a", "-m", "Release"] if annotated else []
    git(work, "tag", *options, "v1.2.3")
    git(work, "push", "--quiet", "origin", "--tags")
    assert policy.select_tags("v1.2.3", sha, version) == ["1.2.3", "latest"]
    version.write_text('__version__ = "2.0.0"\n')
    git(work, "commit", "--quiet", "-am", "Higher version")
    git(work, "tag", "v2.0.0")
    git(work, "push", "--quiet", "origin", "main", "--tags")
    git(work, "checkout", "--quiet", sha)
    assert policy.select_tags("v1.2.3", sha, version) == ["1.2.3"]


@pytest.mark.parametrize("ref", ["feature", "v-surprise", "v1.2.3rc1", "v1.2", "v01.2.3", "v1.2.3\n", "v1.2.3;echo BAD"])
def test_excluded_refs_do_not_read_or_execute_source(tmp_path, monkeypatch, ref):
    def forbidden(*args):
        raise AssertionError("excluded ref reached Git")
    monkeypatch.setattr(policy, "_git", forbidden)
    source = tmp_path / "__about__.py"
    marker = tmp_path / "executed"
    source.write_text(f'__version__ = "1.2.3"\nopen({str(marker)!r}, "w").write("BAD")\n')
    assert policy.select_tags(ref, "a" * 40, source) == []
    assert not marker.exists()


def test_version_is_data(tmp_path):
    marker = tmp_path / "executed"
    source = tmp_path / "__about__.py"
    source.write_text(f'open({str(marker)!r}, "w").write("BAD")\n__version__: str = "1.2.3"\n')
    with pytest.raises(ValueError):
        policy.read_version(source)
    assert not marker.exists()
    source.write_text('"""Package version."""\n__version__: str = "1.2.3"\n')
    assert policy.read_version(source) == "1.2.3"


@pytest.mark.parametrize("body", ['__version__ = str("1.2.3")', '__version__ = 123', '__version__ = "1.2.3rc1"', '__version__ = "1.2.3"\n__version__ = "2.0.0"', 'pass', '__version__ = "1.2.3\\nlatest"', '__version__ = "1.2.3"\n__version__ += "rc1"', '__version__ = "1.2.3"\nif True: __version__ = "2.0.0"', '__version__ = "1.2.3"\ndel __version__', '__version__ = "1.2.3"\n(__version__, other) = ("2.0.0", 1)'])
def test_invalid_versions_fail_closed(tmp_path, body):
    source = tmp_path / "__about__.py"
    source.write_text(body)
    with pytest.raises(ValueError):
        policy.read_version(source)


def test_version_must_fit_docker_tag_limit(tmp_path):
    source = tmp_path / "__about__.py"
    source.write_text(f'__version__ = "1.{"2" * 127}.3"\n')
    with pytest.raises(ValueError, match="128-character"):
        policy.read_version(source)


def test_missing_tag_and_version_mismatch(repo):
    work, version, sha = repo
    assert policy.select_tags("v1.2.3", sha, version) == []
    git(work, "tag", "v2.0.0")
    with pytest.raises(ValueError, match="does not match"):
        policy.select_tags("v2.0.0", sha, version)
    git(work, "commit", "--quiet", "--allow-empty", "-m", "Another commit")
    with pytest.raises(ValueError, match="checked-out"):
        policy.select_tags("main", sha, version)


def test_annotated_tag_object_is_resolved_to_tested_commit(repo):
    work, version, sha = repo
    git(work, "tag", "-a", "-m", "Release", "v1.2.3")
    git(work, "push", "--quiet", "origin", "--tags")
    tag_sha = git(work, "rev-parse", "v1.2.3")
    assert tag_sha != sha
    assert policy.select_tags("v1.2.3", tag_sha, version) == ["1.2.3", "latest"]


def artifact(attempt=1, **updates):
    out = {"id": 100 + attempt, "name": f"streetzim-image-42-attempt-{attempt}",
           "expired": False, "workflow_run": {"id": 42, "head_sha": "a" * 40}}
    out.update(updates)
    return out


def test_paginated_artifacts_and_failed_job_rerun():
    pages = [{"artifacts": [artifact(1)]}, {"artifacts": [artifact(3), artifact(4)]}]
    assert policy.select_artifact(pages, 42, 3, "a" * 40) == 103
    assert policy.select_artifact(pages, 42, 2, "a" * 40) == 101


@pytest.mark.parametrize("provenance", [None, {}])
def test_nullable_or_missing_provenance_is_skipped(provenance):
    pages = [{"artifacts": [artifact(2, workflow_run=provenance), artifact()]}]
    assert policy.select_artifact(pages, 42, 2, "a" * 40) == 101


def test_null_artifact_name_is_skipped():
    pages = [{"artifacts": [artifact(name=None), artifact(2)]}]
    assert policy.select_artifact(pages, 42, 2, "a" * 40) == 102


@pytest.mark.parametrize("bad", [artifact(expired=True), artifact(name="coverage"), artifact(name="streetzim-image-43-attempt-1"), artifact(workflow_run={"id": 43, "head_sha": "a" * 40}), artifact(workflow_run={"id": 42, "head_sha": "b" * 40}), artifact(3)])
def test_wrong_or_expired_artifact_is_not_selected(bad):
    assert policy.select_artifact([{"artifacts": [bad, artifact(2)]}], 42, 2, "a" * 40) == 102


def test_ambiguous_and_missing_artifacts_fail_closed():
    with pytest.raises(ValueError, match="ambiguous"):
        policy.select_artifact([{"artifacts": [artifact(), artifact(id=999)]}], 42, 1, "a" * 40)
    with pytest.raises(ValueError, match="rerun all CI jobs"):
        policy.select_artifact([{"artifacts": []}], 42, 1, "a" * 40)
    with pytest.raises(ValueError, match="artifact ID"):
        policy.select_artifact([{"artifacts": [artifact(id=True)]}], 42, 1, "a" * 40)


def test_cli_artifact_selection_outputs_only_id(tmp_path):
    data = tmp_path / "pages.json"
    data.write_text(json.dumps([{"artifacts": [artifact()]}]))
    script = Path(policy.__file__).resolve()
    result = subprocess.run(["python3", str(script), "--sha", "a" * 40, "--artifact-list", str(data),
                             "--run-id", "42", "--run-attempt", "2"], capture_output=True, text=True, check=True)
    assert result.stdout == "artifact_id=101\n"
