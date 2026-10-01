"""A finished archive is never lost to a failed publication, and what a
killed build leaves behind (staging archive, create_zim's folder, workspace)
is swept later, only once the build's lock shows it has ended."""
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
    cli.owner_lock(stage).touch()
    assert cli.sweep_stale(stage.parent, STAGING_KINDS,
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
        # While the build runs, both locks exist and nobody else can take them.
        for owned in (work, building):
            assert cli.owner_lock(owned).exists()
            with cli._owner_gone(cli.owner_lock(owned)) as gone:
                assert not gone
        return original(args, dl, illustration, work, building, final)

    monkeypatch.setattr(cli, "_build", build)
    assert _run(tmp_path) == 0
    stage, work = fake_builder[0], workspaces[0]
    assert cli.STAGING_NAME.match(stage.name).group("pid") == str(os.getpid())
    assert cli.STAGING_NAME.match(stage.name + ".tmp")
    assert cli.WRITER_NAME.match(f".{stage.name}.building-abcd_123").group("owner") == stage.name
    assert cli.WORKSPACE_NAME.match(work.name).group("pid") == str(os.getpid())
    # A finished build removes its lock files.
    assert not cli.owner_lock(stage).exists() and not cli.owner_lock(work).exists()


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


STAGING_KINDS = [(cli.STAGING_NAME, False), (cli.WRITER_NAME, True)]
WORKSPACE_KINDS = [(cli.WORKSPACE_NAME, True)]


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _age(path: Path, when: float = OLD) -> Path:
    for p in [path, *path.rglob("*")] if path.is_dir() else [path]:
        os.utime(p, (when, when), follow_symlinks=False)
    return path


def _unlocked(folder: Path, owner: str) -> Path:
    """The lock file a killed build leaves: present, nobody holding it."""
    lock = cli.owner_lock(folder / owner)
    lock.touch()
    return lock


def test_sweep_removes_only_stale_orphaned_staging(tmp_path, capsys):
    import fcntl
    dead, alive = _dead_pid(), os.getppid()
    out = tmp_path

    def file(name, when=OLD):
        p = out / name
        p.write_bytes(b"x")
        return _age(p, when)

    def folder(name, when=OLD, *inner):
        p = out / name
        p.mkdir()
        for sub in inner:
            (p / sub).mkdir()
        (p / "same.zim.tmp").write_bytes(b"partial")
        return _age(p, when)

    owner = f".same.zim.{dead}.abcd_123.building"
    lock = _unlocked(out, owner)
    stale = [file(owner), file(owner + ".tmp"), folder(f".{owner}.building-wxyz_987")]
    # Another container's live build: its PID means nothing here and its
    # files may be old, but it holds its lock.
    live = f".same.zim.{dead}.live1234.building"
    held = open(_unlocked(out, live), "a")
    fcntl.flock(held, fcntl.LOCK_EX)
    for o in ("recent12", "marked12", "nolock12", "packstg1"):
        if o != "nolock12":
            _unlocked(out, f".same.zim.{dead}.{o}.building")
    kept = [
        file(live), folder(f".{live}.building-wxyz_987"),
        file(f".same.zim.{dead}.recent12.building", time.time()),     # fresh
        folder(f"..same.zim.{dead}.recent12.building.building-abcd_123", time.time()),
        file(f".same.zim.{alive}.abcd_123.building"),                 # owner alive
        file(f".same.zim.{os.getpid()}.abcd_123.building"),           # this process
        file(f".same.zim.{dead}.marked12.building"),                  # kept on purpose
        file(f".same.zim.{dead}.marked12.building.tmp"),
        folder(f"..same.zim.{dead}.marked12.building.building-abcd_123"),
        file(f".same.zim.{dead}.nolock12.building"),                  # no lock file
        folder(f"..same.zim.{dead}.nolock12.building.building-abcd_123"),
        # STREETZIM_KEEP_PACK_STAGE kept the pack stage
        folder(f"..same.zim.{dead}.packstg1.building.building-abcd_123", OLD,
               "same.zim.pack-stage-abcd"),
        file(".same.zim.abcd_123.building"),                          # no PID (older format)
        file(f"same.zim.{dead}.abcd_123.building"),                   # not hidden
        file(f".same.pbf.{dead}.abcd_123.building"),                  # not an archive
        file(f".same.zim.{dead}.abcd_123.building.part"),
        file("same.zim"),
        file(".same.zim.0.abcd_123.building"),                        # PID 0
        file(".x.zim.99999999999.abcdefgh.building"),                 # PID too long
        folder(f".same.zim.{dead}.dirdir12.building"),                # a folder
        folder(".same.zim.building-abcd_123"),                        # create_zim of a
    ]                                                                 # plain output
    _unlocked(out, f".same.zim.{dead}.dirdir12.building")
    kept.append(file(f".same.zim.{dead}.marked12.building.keep"))
    removed = cli.sweep_stale(out, STAGING_KINDS)
    assert sorted(removed) == sorted(stale)
    assert not any(p.exists() for p in stale)
    assert not lock.exists(), "the lock goes with the last of its owner's entries"
    assert all(p.exists() for p in kept)
    assert "left by an interrupted build" in capsys.readouterr().out
    held.close()
    assert cli.sweep_stale(out, STAGING_KINDS) == sorted(kept[:2])


def test_a_huge_pid_does_not_break_startup(tmp_path):
    assert cli._pid_alive(10 ** 30)          # OverflowError: treated as alive
    assert not cli.STAGING_NAME.match(".x.zim.99999999999.abcdefgh.building")
    name = ".x.zim.9999999.abcdefgh.building"
    (tmp_path / name).write_bytes(b"x")
    _unlocked(tmp_path, name)
    cli.sweep_stale(tmp_path, STAGING_KINDS, now=time.time() + 2 * cli.STALE_AFTER)


def test_a_killed_build_leaves_an_unlocked_lock(tmp_path):
    import signal
    work = tmp_path / "streetzim-build-1234-abcd_123"
    work.mkdir()
    code = (f"import sys, time; sys.path.insert(0, {str(ROOT)!r})\n"
            "from pathlib import Path\nfrom streetzim import cli\n"
            f"with cli.hold_owner_lock(Path({str(work)!r})):\n"
            "    print('locked', flush=True); time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline() == "locked\n"
        with cli._owner_gone(cli.owner_lock(work)) as gone:
            assert not gone
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        proc.stdout.close()
    with cli._owner_gone(cli.owner_lock(work)) as gone:
        assert gone


def test_sweep_removes_only_stale_orphaned_workspaces(tmp_path):
    import fcntl
    dead = _dead_pid()

    def workspace(name, when=OLD, inner_when=None, lock=True):
        p = tmp_path / name
        (p / "sub").mkdir(parents=True)
        (p / "sub" / "area.mbtiles").write_bytes(b"x")
        _age(p, when)
        if inner_when is not None:
            os.utime(p / "sub" / "area.mbtiles", (inner_when, inner_when))
        if lock:
            _unlocked(tmp_path, name)
        return p

    stale = workspace(f"streetzim-build-{dead}-abcd_123")
    live = workspace(f"streetzim-build-{dead}-live1234")      # another container's
    held = open(cli.owner_lock(live), "a")
    fcntl.flock(held, fcntl.LOCK_EX)
    kept = [
        live,
        workspace(f"streetzim-build-{dead}-recent12", time.time()),
        workspace(f"streetzim-build-{dead}-inner123", inner_when=time.time()),  # in use
        workspace(f"streetzim-build-{os.getppid()}-abcd_123"),
        workspace(f"streetzim-build-{dead}-marked12"),
        workspace(f"streetzim-build-{dead}-nolock12", lock=False),
        workspace("streetzim-build-abcd_123"),                    # no PID (older format)
        workspace(f"streetzim-mbtiles-{dead}-abcd_123"),
        workspace("dl"),
    ]
    (tmp_path / f"streetzim-build-{dead}-marked12.keep").touch()
    plain = tmp_path / f"streetzim-build-{dead}-file1234"         # a file, not a folder
    plain.write_bytes(b"x")
    _age(plain)
    _unlocked(tmp_path, plain.name)
    kept.append(plain)
    link = tmp_path / f"streetzim-build-{dead}-link1234"
    link.symlink_to(kept[-2])
    _unlocked(tmp_path, link.name)
    kept.append(link)
    assert cli.sweep_stale(tmp_path, WORKSPACE_KINDS) == [stale]
    assert not stale.exists() and not cli.owner_lock(stale).exists()
    assert all(p.exists() for p in kept)
    held.close()


def test_main_sweeps_both_folders(tmp_path, fake_builder):
    dead = _dead_pid()
    scratch, out = tmp_path / "scratch", tmp_path / "out"
    work = scratch / f"streetzim-build-{dead}-abcd_123"
    work.mkdir(parents=True)
    _age(work)
    _unlocked(scratch, work.name)
    out.mkdir()
    owner = f".same.zim.{dead}.abcd_123.building"
    staging = out / (owner + ".tmp")
    staging.write_bytes(b"x")
    _age(staging)
    writer = out / f".{owner}.building-abcd_123"
    writer.mkdir()
    (writer / "same.zim.tmp").write_bytes(b"partial")
    _age(writer)
    _unlocked(out, owner)
    assert _run(tmp_path) == 0
    assert not work.exists() and not staging.exists() and not writer.exists()
    assert list(out.iterdir()) == [out / "same.zim"]
    assert not any(cli.WORKSPACE_NAME.match(p.name) or p.suffix == ".lock"
                   for p in scratch.iterdir())
