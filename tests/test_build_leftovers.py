"""A finished archive is never lost to a failed publication, and what a
killed build leaves behind (staging archive, workspace) is swept later."""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

cli = pytest.importorskip("streetzim.cli")
pytest.importorskip("regex")

REQ = ["--name", "osm_en_monaco", "--title", "Monaco", "--description", "Offline Monaco",
       "--bbox=7.4,43.72,7.44,43.76", "--mbtiles", "unused", "--no-routing",
       "--profile", "basic", "--file-name", "same"]
OLD = time.time() - cli.STALE_AFTER - 3600


@pytest.fixture
def fake_builder(monkeypatch):
    import create_osm_zim
    monkeypatch.setattr(cli, "plan", lambda *args, **kwargs: ([], {}))
    stages: list[Path] = []

    def builder(argv):
        path = Path(argv[argv.index("-o") + 1])
        stages.append(path)
        path.write_bytes(b"finished archive")

    monkeypatch.setattr(create_osm_zim, "main", builder)
    return stages


def _run(tmp_path, *extra):
    return cli.main(REQ + ["--output", str(tmp_path / "out"),
                           "--tmp", str(tmp_path / "scratch"), *extra])


def _no_links(*args, **kwargs):
    raise PermissionError(errno.EPERM, "Operation not permitted")


def test_publication_falls_back_to_rename_without_hard_links(tmp_path, monkeypatch,
                                                             fake_builder):
    monkeypatch.setattr(os, "link", _no_links)
    assert _run(tmp_path) == 0
    final = tmp_path / "out" / "same.zim"
    assert final.read_bytes() == b"finished archive"
    assert list(final.parent.iterdir()) == [final]


def test_fallback_still_refuses_an_output_that_appeared(tmp_path, monkeypatch, fake_builder):
    final = tmp_path / "out" / "same.zim"

    def link(src, dst):
        Path(dst).write_bytes(b"another build")   # published meanwhile
        _no_links()

    monkeypatch.setattr(os, "link", link)
    assert _run(tmp_path) == 2
    assert final.read_bytes() == b"another build"
    assert list(final.parent.iterdir()) == [final]


def test_failed_publication_keeps_the_finished_archive(tmp_path, monkeypatch, capsys,
                                                       fake_builder):
    monkeypatch.setattr(os, "link", _no_links)

    def no_rename(src, dst):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "replace", no_rename)
    assert _run(tmp_path) == 2
    [stage] = fake_builder
    assert stage.read_bytes() == b"finished archive"
    assert str(stage) in capsys.readouterr().err
    assert cli.keep_marker(stage).exists()
    monkeypatch.undo()
    # Not even a later build's sweep removes it, once stale and orphaned.
    os.utime(stage, (OLD, OLD))
    assert cli.sweep_stale(stage.parent, cli.STAGING_NAME, folders=False,
                           now=time.time() + 2 * cli.STALE_AFTER) == []
    assert stage.exists()


def test_overwrite_failure_keeps_the_finished_archive(tmp_path, monkeypatch, fake_builder):
    def no_rename(src, dst):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "replace", no_rename)
    assert _run(tmp_path, "--overwrite") == 2
    assert fake_builder[0].read_bytes() == b"finished archive"


def test_staging_and_workspace_names_carry_the_pid(tmp_path, monkeypatch, fake_builder):
    workspaces = []
    original = cli._build

    def build(args, dl, illustration, work, building, final):
        workspaces.append(work)
        return original(args, dl, illustration, work, building, final)

    monkeypatch.setattr(cli, "_build", build)
    assert _run(tmp_path) == 0
    assert cli.STAGING_NAME.match(fake_builder[0].name).group(1) == str(os.getpid())
    assert cli.STAGING_NAME.match(fake_builder[0].name + ".tmp")
    assert cli.WORKSPACE_NAME.match(workspaces[0].name).group(1) == str(os.getpid())


def test_keep_temp_leftovers_are_marked(tmp_path, monkeypatch):
    import create_osm_zim
    monkeypatch.setattr(cli, "plan", lambda *args, **kwargs: ([], {}))
    seen = []

    def fail(argv):
        seen.append(Path(argv[argv.index("-o") + 1]))
        seen[-1].with_name(seen[-1].name + ".tmp").write_bytes(b"partial")
        raise RuntimeError("writer failed")

    monkeypatch.setattr(create_osm_zim, "main", fail)
    with pytest.raises(RuntimeError):
        _run(tmp_path, "--keep-temp")
    assert cli.keep_marker(seen[0]).exists()
    [work] = [p for p in (tmp_path / "scratch").iterdir() if cli.WORKSPACE_NAME.match(p.name)]
    assert cli.keep_marker(work).exists()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _age(path: Path, when: float = OLD) -> Path:
    for p in [path, *path.rglob("*")] if path.is_dir() else [path]:
        os.utime(p, (when, when), follow_symlinks=False)
    return path


def test_sweep_removes_only_stale_orphaned_staging(tmp_path, capsys):
    dead, alive = _dead_pid(), os.getppid()
    out = tmp_path

    def file(name, when=OLD):
        p = out / name
        p.write_bytes(b"x")
        return _age(p, when)

    stale = file(f".same.zim.{dead}.abcd_123.building")
    stale_tmp = file(f".same.zim.{dead}.abcd_123.building.tmp")
    kept = [
        file(f".same.zim.{dead}.recent12.building", time.time()),     # fresh
        file(f".same.zim.{alive}.abcd_123.building"),                 # owner alive
        file(f".same.zim.{os.getpid()}.abcd_123.building"),           # this process
        file(f".same.zim.{dead}.marked12.building"),                  # kept on purpose
        file(f".same.zim.{dead}.marked12.building.tmp"),
        file(".same.zim.abcd_123.building"),                          # no PID (older format)
        file(f"same.zim.{dead}.abcd_123.building"),                   # not hidden
        file(f".same.pbf.{dead}.abcd_123.building"),                  # not an archive
        file(f".same.zim.{dead}.abcd_123.building.part"),
        file("same.zim"),
        file(".same.zim.0.abcd_123.building"),                        # PID 0
    ]
    kept.append(file(f".same.zim.{dead}.marked12.building.keep"))
    folder = out / f".same.zim.{dead}.dirdir12.building"              # a folder
    folder.mkdir()
    _age(folder)
    kept.append(folder)
    writer = out / ".same.zim.building-abc"                          # zim_writer's
    writer.mkdir()
    _age(writer)
    kept.append(writer)
    removed = cli.sweep_stale(out, cli.STAGING_NAME, folders=False)
    assert sorted(removed) == sorted([stale, stale_tmp])
    assert all(p.exists() for p in kept)
    assert not stale.exists() and not stale_tmp.exists()
    assert "left by an interrupted build" in capsys.readouterr().out


def test_sweep_removes_only_stale_orphaned_workspaces(tmp_path):
    dead = _dead_pid()

    def workspace(name, when=OLD, inner_when=None):
        p = tmp_path / name
        (p / "sub").mkdir(parents=True)
        (p / "sub" / "area.mbtiles").write_bytes(b"x")
        _age(p, when)
        if inner_when is not None:
            os.utime(p / "sub" / "area.mbtiles", (inner_when, inner_when))
        return p

    stale = workspace(f"streetzim-build-{dead}-abcd_123")
    kept = [
        workspace(f"streetzim-build-{dead}-recent12", time.time()),
        workspace(f"streetzim-build-{dead}-inner123", inner_when=time.time()),  # in use
        workspace(f"streetzim-build-{os.getppid()}-abcd_123"),
        workspace(f"streetzim-build-{dead}-marked12"),
        workspace("streetzim-build-abcd_123"),                    # no PID (older format)
        workspace(f"streetzim-mbtiles-{dead}-abcd_123"),
        workspace("dl"),
    ]
    (tmp_path / f"streetzim-build-{dead}-marked12.keep").touch()
    plain = tmp_path / f"streetzim-build-{dead}-file1234"         # a file, not a folder
    plain.write_bytes(b"x")
    _age(plain)
    kept.append(plain)
    link = tmp_path / f"streetzim-build-{dead}-link1234"
    link.symlink_to(kept[-2])
    kept.append(link)
    assert cli.sweep_stale(tmp_path, cli.WORKSPACE_NAME, folders=True) == [stale]
    assert not stale.exists()
    assert all(p.exists() for p in kept)


def test_main_sweeps_both_folders(tmp_path, fake_builder):
    dead = _dead_pid()
    scratch, out = tmp_path / "scratch", tmp_path / "out"
    work = scratch / f"streetzim-build-{dead}-abcd_123"
    work.mkdir(parents=True)
    _age(work)
    out.mkdir()
    staging = out / f".same.zim.{dead}.abcd_123.building.tmp"
    staging.write_bytes(b"x")
    _age(staging)
    assert _run(tmp_path) == 0
    assert not work.exists() and not staging.exists()
    assert list(out.iterdir()) == [out / "same.zim"]
