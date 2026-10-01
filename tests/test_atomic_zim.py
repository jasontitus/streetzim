"""A destination is always a completed archive, even after a failed rebuild."""
import os
from pathlib import Path

import pytest

from streetzim import zim_writer as writer
from tests.test_search_tempdir import _build


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", [OSError(28, "disk full"), KeyboardInterrupt(),
                                   SystemExit(143)])
def test_failed_build_preserves_destination_and_cleans_staging(
        tmp_path, monkeypatch, existing, failure):
    target = tmp_path / "t.zim"
    if existing:
        target.write_bytes(b"previous complete archive")

    def fail(*args, **kwargs):
        raise failure

    # Exercise libzim's real exception finalization, not only the wrapper.
    monkeypatch.setattr(writer, "_search_emit_chunks", fail)
    with pytest.raises(type(failure)):
        _build(tmp_path, tmp_path)
    assert target.read_bytes() == b"previous complete archive" if existing else not target.exists()
    assert not list(tmp_path.glob(".t.zim.building-*"))


def test_old_archive_visible_until_successful_publication(tmp_path, monkeypatch):
    target = tmp_path / "map.zim"
    target.write_bytes(b"old")
    stages = []

    def build(stage, *args, **kwargs):
        stages.append(Path(stage))
        assert target.read_bytes() == b"old"
        Path(stage).write_bytes(b"new complete archive")
        assert target.read_bytes() == b"old"

    monkeypatch.setattr(writer, "_create_zim", build)
    writer.create_zim(target)
    assert target.read_bytes() == b"new complete archive"
    assert stages[0].parent.parent == tmp_path
    assert not stages[0].parent.exists()


def test_failed_publication_keeps_old_archive(tmp_path, monkeypatch):
    target = tmp_path / "map.zim"
    target.write_bytes(b"old")
    monkeypatch.setattr(writer, "_create_zim", lambda stage: Path(stage).write_bytes(b"new"))

    def fail(*args):
        raise OSError(13, "destination not writable")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        writer.create_zim(target)
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".map.zim.building-*"))


def test_failed_rust_manifest_is_kept_at_the_reported_path(tmp_path, monkeypatch):
    target = tmp_path / "map.zim"
    target.write_bytes(b"old")
    manifests = []

    def build(stage):
        folder = Path(stage).parent / "map.zim.pack-stage-unique"
        folder.mkdir()
        manifest = folder / "manifest.jsonl"
        manifest.write_text('{"op":"config"}\n')
        manifests.append(manifest)
        raise RuntimeError(f"Manifest preserved at {manifest}")

    monkeypatch.setattr(writer, "_create_zim", build)
    with pytest.raises(RuntimeError, match="Manifest preserved at"):
        writer.create_zim(target)
    assert manifests[0].exists()
    assert target.read_bytes() == b"old"


def test_successful_rebuild_is_a_readable_complete_archive(tmp_path):
    from libzim.reader import Archive
    target = tmp_path / "t.zim"
    target.write_bytes(b"old")
    _build(tmp_path, tmp_path)
    archive = Archive(str(target))
    assert archive.check()
    assert archive.main_entry.get_redirect_entry().path == "index.html"
    assert not list(tmp_path.glob(".t.zim.building-*"))


@pytest.mark.parametrize("flags", [["--workers", "0"], ["--workers", "-1"],
                                  ["--zim-builder", "rust"], ["--xapian", "builder"]])
def test_invalid_resource_or_index_options_fail_before_build(monkeypatch, flags):
    import create_osm_zim as builder

    def started(**kwargs):
        pytest.fail("invalid configuration reached the build")

    monkeypatch.setattr(builder, "_openzim_options", started)
    with pytest.raises(SystemExit, match="2"):
        builder.main(["--area", "monaco"] + flags)


@pytest.mark.parametrize("missing", ["packer", "indexer"])
def test_missing_accelerator_fails_before_build(monkeypatch, missing):
    import create_osm_zim as builder
    from cloud import manifest_writer

    def started(**kwargs):
        pytest.fail("missing accelerator reached the build")

    def absent(*args):
        raise RuntimeError("accelerator is unavailable")

    monkeypatch.setattr(builder, "_openzim_options", started)
    monkeypatch.setattr(manifest_writer, "resolve_pack_command",
                        absent if missing == "packer" else lambda *args: ["/valid/packer"])
    monkeypatch.setattr(builder, "_resolve_xapianbuilder_binary", absent)
    with pytest.raises(SystemExit, match="2"):
        builder.main(["--area", "monaco", "--zim-builder", "rust", "--xapian", "builder"])


def test_indexer_relative_path_is_resolved_for_subprocess(tmp_path, monkeypatch):
    from streetzim.zim_writer import _resolve_xapianbuilder_binary

    executable = tmp_path / "indexer"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    resolved = _resolve_xapianbuilder_binary("indexer")
    assert resolved == str(executable.resolve())
    import subprocess
    subprocess.run([resolved], check=True)


@pytest.mark.parametrize("via_env", [False, True])
def test_explicit_missing_indexer_does_not_use_another_binary(tmp_path, monkeypatch, via_env):
    from streetzim import zim_writer

    missing = str(tmp_path / "missing")
    fallback = tmp_path / "xapianbuilder/target/release/xapianbuilder"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("#!/bin/sh\nexit 0\n")
    fallback.chmod(0o755)
    monkeypatch.setattr(zim_writer, "REPO_ROOT", tmp_path / "streetzim")
    monkeypatch.setenv("XAPIANBUILDER_BIN", missing)
    with pytest.raises(FileNotFoundError, match="not an executable file"):
        zim_writer._resolve_xapianbuilder_binary(None if via_env else missing)
